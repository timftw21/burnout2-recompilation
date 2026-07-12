#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#define VK_USE_PLATFORM_WIN32_KHR

#include <windows.h>
#include <shellapi.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstring>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <map>
#include <optional>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

constexpr uint32_t kDefaultWidth = 640;
constexpr uint32_t kDefaultHeight = 480;
constexpr uint32_t kDefaultFrames = 120;
constexpr int64_t kTargetFrameMs = 16;
constexpr int64_t kControllerMinimumPulseMs = 150;

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
    uint32_t flip_audit_max_flips = 0;
    uint32_t flip_audit_health_interval = 30;
    bool inject_input = false;
    bool list_adapters_only = false;
    std::wstring title = L"B2 Recomp - First Interactive Frame";
    std::filesystem::path debug_json;
    std::filesystem::path render_stream_json;
    std::filesystem::path screenshot;
    std::filesystem::path hotkey_screenshot_directory;
    std::filesystem::path flip_audit_ack;
    std::filesystem::path flip_audit_frame_directory;
    std::filesystem::path vertex_shader;
    std::filesystem::path fragment_shader;
    bool live_render_stream = false;
    bool strict_render_validation = false;
    std::filesystem::path controller_state_json;
};

struct QueueFamilySelection {
    uint32_t index = 0;
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
    std::vector<uint8_t> payload;
};

struct PushBufferWord {
    uint32_t address = 0;
    uint32_t value = 0;
    bool run_start = false;
};

struct NativeVertex {
    uint32_t raw_x_bits = 0;
    uint32_t raw_y_bits = 0;
    float x = 0.0f;
    float y = 0.0f;
    float r = 1.0f;
    float g = 1.0f;
    float b = 1.0f;
    float a = 1.0f;
    float u = 0.0f;
    float v = 0.0f;
};

struct NativeDraw {
    uint32_t first_vertex = 0;
    uint32_t vertex_count = 0;
    uint32_t primitive = 0;
    uint32_t texture_address = 0;
    bool texture_enabled = false;
    uint32_t texture_stage = 0;
    std::array<uint32_t, 4> texture_offsets{};
    std::array<uint32_t, 4> texture_formats{};
    std::array<uint32_t, 4> texture_controls{};
    std::array<uint32_t, 16> vertex_formats{};
    uint32_t blend_enable = 0;
    uint32_t blend_source_factor = 1;
    uint32_t blend_destination_factor = 0;
    uint32_t blend_equation = 0x8006;
    uint32_t surface_format = 0;
    uint32_t surface_pitch = 0;
    uint32_t surface_color_offset = 0;
    uint32_t color_mask = 0x01010101;
    uint32_t depth_test_enable = 0;
    uint32_t shader_stage_program = 0;
    uint32_t combiner_control = 0;
    uint32_t transform_execution_mode = 0;
    uint32_t transform_program_start = 0;
    std::array<std::array<uint32_t, 4>, 136> transform_program{};
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
    std::vector<uint8_t> payload;
};

struct HostTexture {
    uint32_t guest_address = 0;
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
    VkImageView view = VK_NULL_HANDLE;
    VkSampler sampler = VK_NULL_HANDLE;
    VkDescriptorSet descriptor_set = VK_NULL_HANDLE;
};

struct InterpretedD3DStream {
    VkClearValue diagnostic_clear_color{};
    bool clear_color_valid = false;
    uint32_t clear_color_argb = 0;
    bool surface_payload_color_valid = false;
    uint32_t surface_payload_argb = 0;
    uint32_t surface_payload_sample_count = 0;
    uint32_t surface_payload_dominant_count = 0;
    uint32_t push_buffer_word_count = 0;
    uint32_t method_packet_count = 0;
    uint32_t interpreted_method_count = 0;
    uint32_t zero_count_method_word_count = 0;
    uint32_t control_flow_packet_count = 0;
    uint32_t mmio_setup_write_count = 0;
    uint32_t submission_kick_count = 0;
    uint32_t unknown_packet_count = 0;
    uint32_t truncated_packet_count = 0;
    uint32_t unsupported_draw_arrays_count = 0;
    uint32_t state_seed = 0;
    std::array<uint32_t, 16> vertex_formats{};
    std::array<uint32_t, 4> texture_offsets{};
    std::array<uint32_t, 4> texture_formats{};
    std::array<uint32_t, 4> texture_controls{};
    uint32_t blend_enable = 0;
    uint32_t blend_source_factor = 1;
    uint32_t blend_destination_factor = 0;
    uint32_t blend_equation = 0x8006;
    uint32_t surface_format = 0;
    uint32_t surface_pitch = 0;
    uint32_t surface_color_offset = 0;
    uint32_t color_mask = 0x01010101;
    uint32_t depth_test_enable = 0;
    uint32_t shader_stage_program = 0;
    uint32_t combiner_control = 0;
    uint32_t transform_execution_mode = 0;
    uint32_t transform_program_load = 0;
    uint32_t transform_program_start = 0;
    uint32_t transform_constant_load = 0;
    std::array<std::array<uint32_t, 4>, 136> transform_program{};
    std::array<std::array<uint32_t, 4>, 192> transform_constants{};
    uint32_t active_primitive = 0;
    uint32_t frame_draw_begin = 0;
    uint32_t presented_draw_begin = 0;
    uint32_t presented_draw_count = 0;
    uint32_t flip_count = 0;
    std::vector<uint32_t> inline_words;
    std::vector<NativeVertex> vertices;
    std::vector<NativeDraw> draws;
};

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
        commands.push_back(
            {kind, address, value, size, std::move(payload)});
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
        && magic_text != "B2APPND1") {
        throw std::runtime_error("invalid live command snapshot header");
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
        std::vector<uint8_t> payload(bytes.begin(), bytes.begin() + size);
        uint32_t value = 0;
        std::memcpy(&value, payload.data(), std::min<size_t>(payload.size(), sizeof(value)));
        commands.push_back({
            kind_raw == 0u ? RecoveredD3DCommandKind::MmioWrite
                           : RecoveredD3DCommandKind::PushBufferWrite,
            address,
            value,
            size,
            std::move(payload),
        });
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
            std::vector<uint8_t> payload(ring.begin() + start, ring.begin() + offset);
            uint32_t value = 0;
            std::memcpy(&value, payload.data(), std::min<size_t>(payload.size(), sizeof(value)));
            commands.push_back({
                RecoveredD3DCommandKind::PushBufferWrite,
                static_cast<uint32_t>(0x80000000u + start),
                value,
                size,
                std::move(payload),
            });
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
        std::vector<uint8_t> payload(bytes.begin(), bytes.begin() + size);
        uint32_t value = 0;
        std::memcpy(&value, payload.data(), std::min<size_t>(payload.size(), sizeof(value)));
        commands.push_back({
            kind_raw == 0u ? RecoveredD3DCommandKind::MmioWrite
                           : RecoveredD3DCommandKind::PushBufferWrite,
            address,
            value,
            size,
            std::move(payload),
        });
    }
    return true;
}

std::vector<RecoveredTextureResource> load_recovered_texture_resources(
    const std::filesystem::path& path) {
    std::ifstream file(path);
    if (!file) {
        return {};
    }
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
        const auto bytes_hex = json_object_field_text(object, "bytes_hex");
        if (!address || !width || !height || !format || !bytes_hex) {
            continue;
        }
        RecoveredTextureResource resource{};
        resource.address = parse_json_u32_text(*address, "texture address");
        resource.width = parse_json_u32_text(*width, "texture width");
        resource.height = parse_json_u32_text(*height, "texture height");
        resource.format = *format;
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
    bool resources_unchanged = false;
    std::filesystem::path resource_source;
};

std::string load_recovered_frontend_text(const std::filesystem::path& path) {
    std::ifstream file(path);
    if (!file) {
        return {};
    }
    std::ostringstream buffer;
    buffer << file.rdbuf();
    const std::string text = buffer.str();
    const std::regex field_regex("\"frontend_text\"\\s*:\\s*\"([^\"]*)\"");
    std::smatch match;
    return std::regex_search(text, match, field_regex) ? match[1].str() : std::string{};
}

std::filesystem::path load_recovered_resource_snapshot_path(
    const std::filesystem::path& stream_path) {
    std::ifstream file(stream_path);
    if (!file) {
        return stream_path;
    }
    std::ostringstream buffer;
    buffer << file.rdbuf();
    const auto field = json_object_field_text(buffer.str(), "resource_snapshot_path");
    return field.has_value() ? std::filesystem::path(*field) : stream_path;
}

std::filesystem::path load_recovered_command_snapshot_path(
    const std::filesystem::path& stream_path) {
    std::ifstream file(stream_path);
    if (!file) {
        return {};
    }
    std::ostringstream buffer;
    buffer << file.rdbuf();
    const auto field = json_object_field_text(buffer.str(), "command_snapshot_path");
    return field.has_value() ? std::filesystem::path(*field) : std::filesystem::path{};
}

RecoveredD3DStreamSource load_or_build_recovered_d3d_command_stream(
    const std::filesystem::path& render_stream_json,
    bool load_textures = true,
    const std::string* supplied_manifest_text = nullptr,
    bool load_commands = true) {
    if (!render_stream_json.empty()) {
        std::string owned_manifest_text;
        if (supplied_manifest_text == nullptr) {
            std::ifstream marker_file(render_stream_json);
            std::ostringstream marker_buffer;
            marker_buffer << marker_file.rdbuf();
            owned_manifest_text = marker_buffer.str();
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
        const std::filesystem::path resource_source = resource_field.has_value()
            ? std::filesystem::path(*resource_field)
            : render_stream_json;
        const std::filesystem::path command_source = command_field.has_value()
            ? std::filesystem::path(*command_field)
            : std::filesystem::path{};
        return {
            load_commands
                ? (command_source.empty()
                    ? load_recovered_d3d_command_stream(render_stream_json)
                    : load_recovered_d3d_binary_stream(command_source))
                : std::vector<RecoveredD3DCommand>{},
            load_textures ? load_recovered_texture_resources(resource_source)
                          : std::vector<RecoveredTextureResource>{},
            render_stream_json.string(),
            frontend_field.value_or(std::string{}),
            resources_unchanged,
            resource_source,
        };
    }
    return {build_recovered_d3d_command_stream(), {}, "built_in_seed", {}, false, {}};
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

std::vector<PushBufferWord> push_buffer_words_from_recovered_stream(
    const std::vector<RecoveredD3DCommand>& stream,
    size_t prefix_end,
    size_t tail_begin) {
    std::vector<PushBufferWord> words;
    std::array<uint8_t, 0x10000> pending_bytes{};
    std::array<uint8_t, 0x10000> pending_valid{};
    uint32_t pending_min_offset = 0x10000u;
    uint32_t pending_max_offset = 0u;
    uint32_t pending_max_address = 0;

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
            const uint32_t address = 0x80000000u + offset;
            const bool contiguous = emitted_word && address == previous_address + 4u;
            const uint32_t value = static_cast<uint32_t>(pending_bytes[offset]) |
                (static_cast<uint32_t>(pending_bytes[offset + 1u]) << 8u) |
                (static_cast<uint32_t>(pending_bytes[offset + 2u]) << 16u) |
                (static_cast<uint32_t>(pending_bytes[offset + 3u]) << 24u);
            words.push_back({address, value, !contiguous});
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
        pending_min_offset = 0x10000u;
        pending_max_offset = 0u;
        pending_max_address = 0;
    };

    auto consume_range = [&](size_t begin, size_t end) {
    for (size_t command_index = begin; command_index < end; ++command_index) {
        const RecoveredD3DCommand& command = stream[command_index];
        if (command.kind != RecoveredD3DCommandKind::PushBufferWrite) {
            flush_pending();
            continue;
        }
        std::array<uint8_t, 8> fallback_payload{};
        const uint8_t* payload_data = command.payload.data();
        size_t payload_size = command.payload.size();
        if (command.payload.empty()) {
            payload_size = std::min<size_t>(command.size, fallback_payload.size());
            std::memcpy(fallback_payload.data(), &command.value, payload_size);
            payload_data = fallback_payload.data();
        }
        if (pending_min_offset != 0x10000u
            && command.address + 0x1000u < pending_max_address) {
            flush_pending();
        }
        for (size_t offset = 0; offset < payload_size; ++offset) {
            const uint32_t ring_offset = command.address + static_cast<uint32_t>(offset)
                - 0x80000000u;
            if (ring_offset >= pending_bytes.size()) {
                continue;
            }
            pending_bytes[ring_offset] = payload_data[offset];
            pending_valid[ring_offset] = 1u;
            pending_min_offset = std::min(pending_min_offset, ring_offset);
            pending_max_offset = std::max(pending_max_offset, ring_offset + 1u);
        }
        pending_max_address = std::max(
            pending_max_address,
            command.address + static_cast<uint32_t>(payload_size));
    }
    };
    consume_range(0, prefix_end);
    if (tail_begin > prefix_end && tail_begin < stream.size()) {
        flush_pending();
        consume_range(tail_begin, stream.size());
    }
    flush_pending();
    return words;
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
    NativeDraw draw{};
    draw.first_vertex = static_cast<uint32_t>(interpreted.vertices.size());
    draw.primitive = interpreted.active_primitive;
    draw.texture_offsets = interpreted.texture_offsets;
    draw.texture_formats = interpreted.texture_formats;
    draw.texture_controls = interpreted.texture_controls;
    draw.vertex_formats = interpreted.vertex_formats;
    draw.blend_enable = interpreted.blend_enable;
    draw.blend_source_factor = interpreted.blend_source_factor;
    draw.blend_destination_factor = interpreted.blend_destination_factor;
    draw.blend_equation = interpreted.blend_equation;
    draw.surface_format = interpreted.surface_format;
    draw.surface_pitch = interpreted.surface_pitch;
    draw.surface_color_offset = interpreted.surface_color_offset;
    draw.color_mask = interpreted.color_mask;
    draw.depth_test_enable = interpreted.depth_test_enable;
    draw.shader_stage_program = interpreted.shader_stage_program;
    draw.combiner_control = interpreted.combiner_control;
    draw.transform_execution_mode = interpreted.transform_execution_mode;
    draw.transform_program_start = interpreted.transform_program_start;
    draw.transform_program = interpreted.transform_program;
    for (uint32_t stage = 0; stage < interpreted.texture_controls.size(); ++stage) {
        if ((interpreted.texture_controls[stage] & (1u << 30u)) == 0u) {
            continue;
        }
        draw.texture_enabled = true;
        draw.texture_stage = stage;
        draw.texture_address = interpreted.texture_offsets[stage];
        break;
    }
    for (size_t base = 0; base + stride_words <= interpreted.inline_words.size(); base += stride_words) {
        size_t cursor = base;
        NativeVertex vertex{};
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
            } else if (slot == 3 && word_count >= 1u) {
                const uint32_t argb = interpreted.inline_words[cursor];
                vertex.a = static_cast<float>((argb >> 24u) & 0xFFu) / 255.0f;
                vertex.r = static_cast<float>((argb >> 16u) & 0xFFu) / 255.0f;
                vertex.g = static_cast<float>((argb >> 8u) & 0xFFu) / 255.0f;
                vertex.b = static_cast<float>(argb & 0xFFu) / 255.0f;
            } else if (slot == 9 && type == 2u && components >= 2u) {
                vertex.u = float_from_u32(interpreted.inline_words[cursor]);
                vertex.v = float_from_u32(interpreted.inline_words[cursor + 1u]);
            }
            cursor += word_count;
        }
        interpreted.vertices.push_back(vertex);
    }
    draw.vertex_count = static_cast<uint32_t>(interpreted.vertices.size()) - draw.first_vertex;
    if (draw.vertex_count != 0u) {
        interpreted.draws.push_back(draw);
    }
    interpreted.inline_words.clear();
}

void interpret_nv2a_method(
    uint32_t method,
    uint32_t data,
    InterpretedD3DStream& interpreted) {
    if (method == 0x012Cu) {
        finish_inline_draw(interpreted);
        interpreted.presented_draw_begin = interpreted.frame_draw_begin;
        interpreted.presented_draw_count =
            static_cast<uint32_t>(interpreted.draws.size()) - interpreted.frame_draw_begin;
        interpreted.frame_draw_begin = static_cast<uint32_t>(interpreted.draws.size());
        ++interpreted.flip_count;
    } else if (method == 0x1810u) {
        // DRAW_ARRAYS is retained for the next indexed/array-backed path.
        ++interpreted.unsupported_draw_arrays_count;
    } else if (method == 0x17FCu) {
        if (data == 0u) {
            finish_inline_draw(interpreted);
            interpreted.active_primitive = 0u;
        } else {
            finish_inline_draw(interpreted);
            interpreted.active_primitive = data;
        }
    } else if (method == 0x1818u && interpreted.active_primitive != 0u) {
        interpreted.inline_words.push_back(data);
    } else if (method >= 0x1760u && method <= 0x179Cu && (method - 0x1760u) % 4u == 0u) {
        interpreted.vertex_formats[(method - 0x1760u) / 4u] = data;
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
    } else if (method == 0x1E9Cu) {
        interpreted.transform_program_load = data;
    } else if (method == 0x1EA0u) {
        interpreted.transform_program_start = data;
    } else if (method == 0x1EA4u) {
        interpreted.transform_constant_load = data;
    } else if (method == 0x0304u) {
        interpreted.blend_enable = data;
    } else if (method == 0x0344u) {
        interpreted.blend_source_factor = data;
    } else if (method == 0x0348u) {
        interpreted.blend_destination_factor = data;
    } else if (method == 0x0350u) {
        interpreted.blend_equation = data;
    } else if (method == 0x0208u) {
        interpreted.surface_format = data;
    } else if (method == 0x020Cu) {
        interpreted.surface_pitch = data;
    } else if (method == 0x0210u) {
        interpreted.surface_color_offset = data;
    } else if (method == 0x0358u) {
        interpreted.color_mask = data;
    } else if (method == 0x030Cu) {
        interpreted.depth_test_enable = data;
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
            } else if (register_offset == 0x0Cu) {
                interpreted.texture_controls[stage] = data;
            }
        }
    }
    if (method == 0x1D90u) {
        interpreted.clear_color_valid = true;
        interpreted.clear_color_argb = data;
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
        interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, command);
        return;
    }
    ++interpreted.method_packet_count;
    interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, command);
    for (uint32_t method_index = 0; method_index < method_count; ++method_index) {
        if (index >= words.size() || words[index].run_start) {
            ++interpreted.truncated_packet_count;
            return;
        }
        const uint32_t method = non_increasing ? first_method : first_method + method_index * 4u;
        const uint32_t data = words[index].value;
        interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, method);
        interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, data);
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
    interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, command);
    ++index;
    if (index >= words.size() || words[index].run_start) {
        ++interpreted.truncated_packet_count;
        return;
    }
    const uint32_t method_count = words[index].value & 0x00FFFFFFu;
    interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, method_count);
    ++index;
    if (method_count == 0) {
        ++interpreted.zero_count_method_word_count;
        return;
    }
    ++interpreted.method_packet_count;
    for (uint32_t method_index = 0; method_index < method_count; ++method_index) {
        if (index >= words.size() || words[index].run_start) {
            ++interpreted.truncated_packet_count;
            return;
        }
        interpret_nv2a_method(first_method, words[index].value, interpreted);
        interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, first_method);
        interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, words[index].value);
        ++interpreted.interpreted_method_count;
        ++index;
    }
}

InterpretedD3DStream interpret_recovered_d3d_stream(
    const std::vector<RecoveredD3DCommand>& stream,
    size_t prefix_end,
    size_t tail_begin) {
    InterpretedD3DStream interpreted{};
    interpreted.state_seed = 0xB200D3D8u;
    auto consume_command_state = [&](size_t begin, size_t end) {
    for (size_t command_index = begin; command_index < end; ++command_index) {
        const RecoveredD3DCommand& command = stream[command_index];
        interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, command.address);
        interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, command.value);
        if (command.kind == RecoveredD3DCommandKind::MmioWrite) {
            const uint32_t offset = command.address - 0xFED00000u;
            if (offset == 0x0008u) {
                ++interpreted.submission_kick_count;
            } else if (offset == 0x0040u || offset == 0x0048u || offset == 0x004Cu || offset == 0x0050u) {
                ++interpreted.mmio_setup_write_count;
            }
        }
    }
    };
    consume_command_state(0, prefix_end);
    if (tail_begin > prefix_end && tail_begin < stream.size()) {
        consume_command_state(tail_begin, stream.size());
    }
    const std::vector<PushBufferWord> words = push_buffer_words_from_recovered_stream(
        stream, prefix_end, tail_begin);
    interpreted.push_buffer_word_count = static_cast<uint32_t>(words.size());
    const SurfacePayloadCandidate surface_payload = dominant_surface_payload_from_words(words);
    interpreted.surface_payload_color_valid = surface_payload.valid;
    interpreted.surface_payload_argb = surface_payload.argb;
    interpreted.surface_payload_sample_count = surface_payload.sample_count;
    interpreted.surface_payload_dominant_count = surface_payload.dominant_count;
    size_t index = 0;
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
            interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, word);
            ++index;
        } else {
            ++interpreted.unknown_packet_count;
            interpreted.state_seed = mix_d3d_state_seed(interpreted.state_seed, word);
            ++index;
        }
    }
    finish_inline_draw(interpreted);
    if (interpreted.flip_count == 0u) {
        interpreted.presented_draw_begin = 0u;
        interpreted.presented_draw_count = static_cast<uint32_t>(interpreted.draws.size());
    }
    if (interpreted.clear_color_valid) {
        interpreted.diagnostic_clear_color = color_from_d3d_argb(interpreted.clear_color_argb);
    } else if (interpreted.surface_payload_color_valid) {
        interpreted.diagnostic_clear_color = color_from_d3d_argb(interpreted.surface_payload_argb);
    } else {
        interpreted.diagnostic_clear_color = color_from_interpreted_d3d_state(interpreted.state_seed);
    }
    return interpreted;
}

InterpretedD3DStream interpret_recovered_d3d_stream(
    const std::vector<RecoveredD3DCommand>& stream) {
    return interpret_recovered_d3d_stream(stream, stream.size(), stream.size());
}

std::pair<size_t, size_t> live_interpretation_ranges(
    const std::vector<RecoveredD3DCommand>& stream) {
    constexpr size_t prefix_commands = 4096;
    constexpr size_t tail_commands = 32768;
    constexpr size_t bounded_threshold = 49152;
    if (stream.size() <= bounded_threshold) {
        return {stream.size(), stream.size()};
    }
    const size_t prefix_end = std::min(prefix_commands, stream.size());
    const size_t desired_tail = stream.size() - tail_commands;
    size_t tail_begin = desired_tail;
    uint32_t pending_max_address = 0;
    for (size_t index = prefix_end; index < stream.size(); ++index) {
        const RecoveredD3DCommand& command = stream[index];
        if (command.kind != RecoveredD3DCommandKind::PushBufferWrite) {
            pending_max_address = 0;
            continue;
        }
        if (pending_max_address != 0
            && command.address + 0x1000u < pending_max_address) {
            if (index >= desired_tail) {
                tail_begin = index;
                break;
            }
            pending_max_address = 0;
        }
        pending_max_address = std::max(
            pending_max_address,
            command.address + std::max<uint32_t>(1u, command.size));
    }
    return {prefix_end, tail_begin};
}

class DebugLog {
public:
    void configure_live_mode(bool live_mode) {
        echo_stdout_ = !live_mode;
        flush_interval_ = live_mode ? 120u : 1u;
    }

    void open(const std::filesystem::path& path) {
        if (path.empty()) {
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

private:
    uint64_t sequence_ = 0;
    uint32_t pending_lines_ = 0;
    uint32_t flush_interval_ = 1;
    bool echo_stdout_ = true;
    std::ofstream file_;
    std::filesystem::path path_;
};

class VulkanFirstFrameApp {
public:
    explicit VulkanFirstFrameApp(Options options) : options_(std::move(options)) {}

    int run() {
        log_.configure_live_mode(options_.live_render_stream);
        log_.open(options_.debug_json);
        log_.emit(
            "startup",
            {
                {"target_platform", json_string("windows")},
                {"renderer_backend", json_string("vulkan")},
                {"width", std::to_string(options_.width)},
                {"height", std::to_string(options_.height)},
                {"max_frames", std::to_string(options_.max_frames)},
            });

        create_instance();
        if (options_.list_adapters_only) {
            list_adapters();
            cleanup();
            return 0;
        }
        create_window();
        create_surface();
        pick_physical_device();
        create_logical_device();
        create_swapchain();
        create_image_views();
        create_render_pass();
        create_framebuffers();
        create_command_pool();
        load_recovered_render_work();
        create_native_graphics_pipeline();
        create_native_render_resources();
        create_readback_buffer();
        create_command_buffers();
        create_sync_objects();
        main_loop();
        vkDeviceWaitIdle(device_);
        cleanup();
        log_.emit("shutdown", {{"status", json_string("ok")}});
        return 0;
    }

private:
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

    bool append_live_command_stream(
        const std::filesystem::path& path,
        std::vector<RecoveredD3DCommand>& commands) {
        if (path != live_command_file_path_ || !live_command_file_.is_open()) {
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
        }
        std::error_code size_error;
        const uintmax_t byte_size = std::filesystem::file_size(path, size_error);
        constexpr uintmax_t header_size = 8;
        constexpr uintmax_t record_size = 16;
        if (size_error || byte_size < header_size
            || (byte_size - header_size) % record_size != 0) {
            return false;
        }
        const size_t record_count = static_cast<size_t>(
            (byte_size - header_size) / record_size);
        if (record_count < commands.size()) {
            return false;
        }
        live_command_file_.clear();
        live_command_file_.seekg(
            static_cast<std::streamoff>(header_size + commands.size() * record_size),
            std::ios::beg);
        commands.reserve(record_count);
        while (commands.size() < record_count) {
            uint8_t kind_raw = 0;
            uint8_t size = 0;
            uint16_t reserved = 0;
            uint32_t address = 0;
            std::array<uint8_t, 8> bytes{};
            live_command_file_.read(reinterpret_cast<char*>(&kind_raw), sizeof(kind_raw));
            live_command_file_.read(reinterpret_cast<char*>(&size), sizeof(size));
            live_command_file_.read(reinterpret_cast<char*>(&reserved), sizeof(reserved));
            live_command_file_.read(reinterpret_cast<char*>(&address), sizeof(address));
            live_command_file_.read(
                reinterpret_cast<char*>(bytes.data()),
                static_cast<std::streamsize>(bytes.size()));
            if (!live_command_file_ || size == 0 || size > bytes.size() || kind_raw > 1u) {
                return false;
            }
            std::vector<uint8_t> payload(bytes.begin(), bytes.begin() + size);
            uint32_t value = 0;
            std::memcpy(&value, payload.data(), std::min<size_t>(payload.size(), sizeof(value)));
            commands.push_back({
                kind_raw == 0u ? RecoveredD3DCommandKind::MmioWrite
                               : RecoveredD3DCommandKind::PushBufferWrite,
                address,
                value,
                size,
                std::move(payload),
            });
        }
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
            if ((families[index].queueFlags & VK_QUEUE_GRAPHICS_BIT) && present_supported == VK_TRUE) {
                return QueueFamilySelection{index};
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
        log_.emit("logical_device_created", {{"queue_family", std::to_string(queue_family_.index)}});
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
        create_info.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
        if (readback_enabled()) {
            if ((support.capabilities.supportedUsageFlags & VK_IMAGE_USAGE_TRANSFER_SRC_BIT) == 0) {
                throw std::runtime_error("surface does not support swapchain transfer-source readback");
            }
            create_info.imageUsage |= VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
        }
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
            });
    }

    VkSurfaceFormatKHR choose_surface_format(const std::vector<VkSurfaceFormatKHR>& formats) const {
        const auto preferred = std::find_if(
            formats.begin(),
            formats.end(),
            [](const VkSurfaceFormatKHR& format) {
                return format.format == VK_FORMAT_B8G8R8A8_SRGB
                    && format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
            });
        if (preferred != formats.end()) {
            return *preferred;
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

    void create_render_pass() {
        VkAttachmentDescription color_attachment{};
        color_attachment.format = swapchain_format_;
        color_attachment.samples = VK_SAMPLE_COUNT_1_BIT;
        color_attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
        color_attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        color_attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        color_attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        color_attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        color_attachment.finalLayout = readback_enabled()
            ? VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL
            : VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

        VkAttachmentReference color_ref{};
        color_ref.attachment = 0;
        color_ref.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

        VkSubpassDescription subpass{};
        subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
        subpass.colorAttachmentCount = 1;
        subpass.pColorAttachments = &color_ref;

        VkSubpassDependency dependency{};
        dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
        dependency.dstSubpass = 0;
        dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
        dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
        dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;

        VkRenderPassCreateInfo create_info{};
        create_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
        create_info.attachmentCount = 1;
        create_info.pAttachments = &color_attachment;
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
            VkImageView attachments[] = {swapchain_image_views_[index]};
            VkFramebufferCreateInfo create_info{};
            create_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
            create_info.renderPass = render_pass_;
            create_info.attachmentCount = 1;
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

    void load_recovered_render_work(bool command_growth_only = false) {
        const auto command_load_begin = std::chrono::steady_clock::now();
        if (command_growth_only) {
            if (!append_live_command_stream(
                    live_command_file_path_, recovered_source_.commands)) {
                return;
            }
            recovered_source_.resources_unchanged = true;
            const auto before_interpret = std::chrono::steady_clock::now();
            const auto [interpret_prefix_end, interpret_tail_begin] =
                live_interpretation_ranges(recovered_source_.commands);
            interpreted_stream_ = interpret_recovered_d3d_stream(
                recovered_source_.commands,
                interpret_prefix_end,
                interpret_tail_begin);
            interpreted_source_command_count_ =
                interpret_prefix_end + recovered_source_.commands.size() - interpret_tail_begin;
            const auto after_interpret = std::chrono::steady_clock::now();
            last_command_load_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
                before_interpret - command_load_begin).count();
            last_interpret_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
                after_interpret - before_interpret).count();
            return;
        }
        bool resources_unchanged = false;
        std::filesystem::path resource_source;
        std::filesystem::file_time_type resource_write_time{};
        std::string manifest_text;
        if (options_.live_render_stream && !options_.render_stream_json.empty()) {
            std::ifstream manifest_file(options_.render_stream_json);
            std::ostringstream manifest_buffer;
            manifest_buffer << manifest_file.rdbuf();
            manifest_text = manifest_buffer.str();
            if (!options_.flip_audit_ack.empty()) {
                const auto deadline = std::chrono::steady_clock::now()
                    + std::chrono::seconds(2);
                while (!update_flip_audit_manifest_state(manifest_text)) {
                    if (std::chrono::steady_clock::now() >= deadline) {
                        throw std::runtime_error(
                            "lossless flip audit manifest is missing required fields");
                    }
                    std::this_thread::sleep_for(std::chrono::milliseconds(2));
                    std::ifstream retry_file(options_.render_stream_json);
                    std::ostringstream retry_buffer;
                    retry_buffer << retry_file.rdbuf();
                    manifest_text = retry_buffer.str();
                }
            }
            const auto resource_field = json_object_field_text(
                manifest_text, "resource_snapshot_path");
            const auto resource_generation_field = json_object_field_text(
                manifest_text, "resource_stream_generation");
            resource_source = resource_field.has_value()
                ? std::filesystem::path(*resource_field)
                : options_.render_stream_json;
            if (resource_generation_field.has_value()
                && !live_resource_generation_.empty()
                && *resource_generation_field == live_resource_generation_) {
                resources_unchanged = true;
            } else if (resource_generation_field.has_value()) {
                live_resource_generation_ = *resource_generation_field;
            } else {
                std::error_code resource_error;
                resource_write_time = std::filesystem::last_write_time(
                    resource_source, resource_error);
                if (!resource_error
                    && resource_source == recovered_source_.resource_source
                    && resource_write_time == live_resource_write_time_) {
                    resources_unchanged = true;
                } else if (!resource_error) {
                    live_resource_write_time_ = resource_write_time;
                }
            }
        }
        const auto command_field = manifest_text.empty()
            ? std::optional<std::string>{}
            : json_object_field_text(manifest_text, "command_snapshot_path");
        const auto generation_field = manifest_text.empty()
            ? std::optional<std::string>{}
            : json_object_field_text(manifest_text, "command_stream_generation");
        const bool incremental_commands = options_.live_render_stream
            && command_field.has_value()
            && generation_field.has_value()
            && *generation_field == live_command_generation_;
        std::vector<RecoveredD3DCommand> retained_commands;
        if (incremental_commands) {
            retained_commands = std::move(recovered_source_.commands);
        }
        RecoveredD3DStreamSource next_source = load_or_build_recovered_d3d_command_stream(
            options_.render_stream_json,
            !resources_unchanged,
            manifest_text.empty() ? nullptr : &manifest_text,
            !incremental_commands);
        if (incremental_commands) {
            if (append_live_command_stream(
                    std::filesystem::path(*command_field), retained_commands)) {
                next_source.commands = std::move(retained_commands);
            } else {
                next_source.commands = load_recovered_d3d_binary_stream(
                    std::filesystem::path(*command_field));
            }
        }
        if (options_.live_render_stream) {
            if (generation_field.has_value()
                && *generation_field != live_command_generation_
                && command_field.has_value()) {
                live_command_file_.close();
                live_command_file_.clear();
                live_command_file_path_ = std::filesystem::path(*command_field);
            }
            live_command_generation_ = generation_field.value_or(std::string{});
        }
        next_source.resources_unchanged = resources_unchanged;
        recovered_source_ = std::move(next_source);
        if (!options_.flip_audit_ack.empty()
            && current_audit_command_count_ > recovered_source_.commands.size()) {
            std::ostringstream message;
            message << "lossless flip audit command stream is short for flip "
                    << current_audit_flip_ << ": manifest="
                    << current_audit_command_count_ << " loaded="
                    << recovered_source_.commands.size();
            throw std::runtime_error(message.str());
        }
        if (!options_.flip_audit_ack.empty()
            && recovered_source_.commands.size() > current_audit_command_count_) {
            recovered_source_.commands.resize(current_audit_command_count_);
        }
        const auto before_interpret = std::chrono::steady_clock::now();
        const auto [interpret_prefix_end, interpret_tail_begin] =
            options_.live_render_stream
                ? live_interpretation_ranges(recovered_source_.commands)
                : std::pair<size_t, size_t>{
                    recovered_source_.commands.size(),
                    recovered_source_.commands.size()};
        interpreted_stream_ = interpret_recovered_d3d_stream(
            recovered_source_.commands,
            interpret_prefix_end,
            interpret_tail_begin);
        interpreted_source_command_count_ =
            interpret_prefix_end + recovered_source_.commands.size() - interpret_tail_begin;
        const auto after_interpret = std::chrono::steady_clock::now();
        last_command_load_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
            before_interpret - command_load_begin).count();
        last_interpret_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
            after_interpret - before_interpret).count();
        recovered_frontend_text_ = recovered_source_.frontend_text;
        if (options_.live_render_stream) {
            std::error_code error;
            live_render_write_time_ = std::filesystem::last_write_time(
                options_.render_stream_json, error);
        }
    }

    void destroy_native_render_resources(
        bool preserve_textures = false,
        bool preserve_vertex_buffer = false) {
        if (!command_buffers_.empty()) {
            vkFreeCommandBuffers(
                device_, command_pool_, static_cast<uint32_t>(command_buffers_.size()),
                command_buffers_.data());
            command_buffers_.clear();
        }
        if (!preserve_vertex_buffer) {
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
            if (texture_descriptor_pool_) {
                vkDestroyDescriptorPool(device_, texture_descriptor_pool_, nullptr);
                texture_descriptor_pool_ = VK_NULL_HANDLE;
            }
            for (const HostTexture& texture : host_textures_) {
                if (texture.sampler) vkDestroySampler(device_, texture.sampler, nullptr);
                if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
                if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
                if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
            }
            host_textures_.clear();
        }
        presented_half_quad_recovered_ = false;
        presented_overscan_height_recovered_ = false;
        frontend_text_rectangle_count_ = 0;
    }

    void reload_live_render_work() {
        if (!options_.live_render_stream) {
            return;
        }
        std::error_code error;
        const auto write_time = std::filesystem::last_write_time(
            options_.render_stream_json, error);
        bool command_growth = false;
        if (!live_command_file_path_.empty()) {
            std::error_code size_error;
            const uintmax_t command_bytes = std::filesystem::file_size(
                live_command_file_path_, size_error);
            const uintmax_t consumed_bytes = 8u
                + static_cast<uintmax_t>(recovered_source_.commands.size()) * 16u;
            command_growth = !size_error && command_bytes > consumed_bytes;
        }
        // Audit manifests carry the exact flip boundary. Never take the
        // command-sidecar-only fast path because it could consume writes from
        // the following frame without advancing the audited flip identity.
        if (!options_.flip_audit_ack.empty()) {
            command_growth = false;
        }
        bool resource_changed = false;
        if (!recovered_source_.resource_source.empty()) {
            std::error_code resource_error;
            const auto resource_time = std::filesystem::last_write_time(
                recovered_source_.resource_source, resource_error);
            resource_changed = !resource_error
                && resource_time != live_resource_write_time_;
        }
        if (command_growth && resource_changed) {
            command_growth = false;
        }
        if (!command_growth && !resource_changed
            && (error || write_time == live_render_write_time_)) {
            return;
        }
        const auto reload_begin = std::chrono::steady_clock::now();
        vk_check(vkDeviceWaitIdle(device_), "vkDeviceWaitIdle(live reload)");
        const auto after_wait = std::chrono::steady_clock::now();
        load_recovered_render_work(command_growth);
        if (command_growth) {
            std::error_code latest_manifest_error;
            const auto latest_manifest_time = std::filesystem::last_write_time(
                options_.render_stream_json, latest_manifest_error);
            if (!latest_manifest_error) {
                live_render_write_time_ = latest_manifest_time;
            }
        } else {
            std::error_code latest_resource_error;
            const auto latest_resource_time = std::filesystem::last_write_time(
                recovered_source_.resource_source, latest_resource_error);
            if (!latest_resource_error) {
                live_resource_write_time_ = latest_resource_time;
            }
        }
        const auto after_load = std::chrono::steady_clock::now();
        const bool preserve_textures = recovered_source_.resources_unchanged
            && !host_textures_.empty();
        const VkDeviceSize required_vertex_bytes =
            interpreted_stream_.vertices.size() * sizeof(NativeVertex);
        const bool preserve_vertex_buffer = vertex_buffer_ != VK_NULL_HANDLE
            && required_vertex_bytes != 0
            && required_vertex_bytes <= vertex_buffer_size_;
        destroy_native_render_resources(preserve_textures, preserve_vertex_buffer);
        create_native_render_resources(!preserve_textures);
        const auto after_resources = std::chrono::steady_clock::now();
        create_command_buffers();
        const auto after_commands = std::chrono::steady_clock::now();
        ++live_render_reload_count_;
        log_.emit(
            "live_render_stream_reloaded",
            {
                {"reload", std::to_string(live_render_reload_count_)},
                {"writes", std::to_string(recovered_source_.commands.size())},
                {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
                {"interpreted_source_commands", std::to_string(interpreted_source_command_count_)},
                {"resources_unchanged", json_bool(recovered_source_.resources_unchanged)},
                {"wait_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_wait - reload_begin).count())},
                {"load_interpret_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_load - after_wait).count())},
                {"command_load_us", std::to_string(last_command_load_us_)},
                {"interpret_us", std::to_string(last_interpret_us_)},
                {"resource_update_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_resources - after_load).count())},
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

    void create_native_graphics_pipeline() {
        VkDescriptorSetLayoutBinding sampler_binding{};
        sampler_binding.binding = 0;
        sampler_binding.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
        sampler_binding.descriptorCount = 1;
        sampler_binding.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
        VkDescriptorSetLayoutCreateInfo descriptor_info{};
        descriptor_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        descriptor_info.bindingCount = 1;
        descriptor_info.pBindings = &sampler_binding;
        vk_check(
            vkCreateDescriptorSetLayout(device_, &descriptor_info, nullptr, &texture_descriptor_layout_),
            "vkCreateDescriptorSetLayout");

        VkPipelineLayoutCreateInfo layout_info{};
        layout_info.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        layout_info.setLayoutCount = 1;
        layout_info.pSetLayouts = &texture_descriptor_layout_;
        vk_check(vkCreatePipelineLayout(device_, &layout_info, nullptr, &pipeline_layout_), "vkCreatePipelineLayout");

        const VkShaderModule vertex_shader = create_shader_module(options_.vertex_shader);
        const VkShaderModule fragment_shader = create_shader_module(options_.fragment_shader);
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
        std::array<VkVertexInputAttributeDescription, 3> attributes{};
        attributes[0] = {0, 0, VK_FORMAT_R32G32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, x))};
        attributes[1] = {1, 0, VK_FORMAT_R32G32B32A32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, r))};
        attributes[2] = {2, 0, VK_FORMAT_R32G32_SFLOAT, static_cast<uint32_t>(offsetof(NativeVertex, u))};
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
        VkPipelineRasterizationStateCreateInfo raster{};
        raster.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
        raster.polygonMode = VK_POLYGON_MODE_FILL;
        raster.cullMode = VK_CULL_MODE_NONE;
        raster.frontFace = VK_FRONT_FACE_CLOCKWISE;
        raster.lineWidth = 1.0f;
        VkPipelineMultisampleStateCreateInfo multisample{};
        multisample.sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;
        multisample.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT;
        VkPipelineColorBlendAttachmentState blend_attachment{};
        blend_attachment.blendEnable = VK_TRUE;
        blend_attachment.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
        blend_attachment.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        blend_attachment.colorBlendOp = VK_BLEND_OP_ADD;
        blend_attachment.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
        blend_attachment.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        blend_attachment.alphaBlendOp = VK_BLEND_OP_ADD;
        blend_attachment.colorWriteMask = VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT |
            VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
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
        pipeline_info.pColorBlendState = &blend;
        pipeline_info.layout = pipeline_layout_;
        pipeline_info.renderPass = render_pass_;
        pipeline_info.subpass = 0;
        vk_check(vkCreateGraphicsPipelines(device_, VK_NULL_HANDLE, 1, &pipeline_info, nullptr, &graphics_pipeline_), "vkCreateGraphicsPipelines");
        vkDestroyShaderModule(device_, fragment_shader, nullptr);
        vkDestroyShaderModule(device_, vertex_shader, nullptr);
        log_.emit("nv2a_graphics_pipeline_created");
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

    void submit_immediate(const std::function<void(VkCommandBuffer)>& record) {
        VkCommandBufferAllocateInfo allocation{};
        allocation.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        allocation.commandPool = command_pool_;
        allocation.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        allocation.commandBufferCount = 1;
        VkCommandBuffer command = VK_NULL_HANDLE;
        vk_check(vkAllocateCommandBuffers(device_, &allocation, &command), "vkAllocateCommandBuffers(immediate)");
        VkCommandBufferBeginInfo begin{};
        begin.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vk_check(vkBeginCommandBuffer(command, &begin), "vkBeginCommandBuffer(immediate)");
        record(command);
        vk_check(vkEndCommandBuffer(command), "vkEndCommandBuffer(immediate)");
        VkSubmitInfo submit{};
        submit.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        submit.commandBufferCount = 1;
        submit.pCommandBuffers = &command;
        vk_check(vkQueueSubmit(graphics_queue_, 1, &submit, VK_NULL_HANDLE), "vkQueueSubmit(immediate)");
        vk_check(vkQueueWaitIdle(graphics_queue_), "vkQueueWaitIdle(immediate)");
        vkFreeCommandBuffers(device_, command_pool_, 1, &command);
    }

    HostTexture create_host_texture(
        uint32_t guest_address,
        uint32_t width,
        uint32_t height,
        const std::vector<uint8_t>& rgba) {
        HostTexture texture{};
        texture.guest_address = guest_address;
        VkBuffer staging = VK_NULL_HANDLE;
        VkDeviceMemory staging_memory = VK_NULL_HANDLE;
        create_buffer(
            rgba.size(),
            VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
            VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
            staging,
            staging_memory);
        void* mapped = nullptr;
        vk_check(vkMapMemory(device_, staging_memory, 0, rgba.size(), 0, &mapped), "vkMapMemory(texture)");
        std::memcpy(mapped, rgba.data(), rgba.size());
        vkUnmapMemory(device_, staging_memory);

        VkImageCreateInfo image_info{};
        image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
        image_info.imageType = VK_IMAGE_TYPE_2D;
        image_info.extent = {width, height, 1};
        image_info.mipLevels = 1;
        image_info.arrayLayers = 1;
        image_info.format = VK_FORMAT_R8G8B8A8_UNORM;
        image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
        image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        image_info.usage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
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
            to_transfer.subresourceRange.levelCount = 1;
            to_transfer.subresourceRange.layerCount = 1;
            vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_transfer);
            VkBufferImageCopy copy{};
            copy.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            copy.imageSubresource.layerCount = 1;
            copy.imageExtent = {width, height, 1};
            vkCmdCopyBufferToImage(command, staging, texture.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
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
            vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0, nullptr, 0, nullptr, 1, &to_shader);
        });
        vkDestroyBuffer(device_, staging, nullptr);
        vkFreeMemory(device_, staging_memory, nullptr);

        VkImageViewCreateInfo view_info{};
        view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        view_info.image = texture.image;
        view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        view_info.format = VK_FORMAT_R8G8B8A8_UNORM;
        view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        view_info.subresourceRange.levelCount = 1;
        view_info.subresourceRange.layerCount = 1;
        vk_check(vkCreateImageView(device_, &view_info, nullptr, &texture.view), "vkCreateImageView(texture)");
        VkSamplerCreateInfo sampler_info{};
        sampler_info.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
        sampler_info.magFilter = VK_FILTER_LINEAR;
        sampler_info.minFilter = VK_FILTER_LINEAR;
        sampler_info.mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST;
        sampler_info.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
        sampler_info.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
        sampler_info.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
        sampler_info.maxLod = 0.0f;
        vk_check(vkCreateSampler(device_, &sampler_info, nullptr, &texture.sampler), "vkCreateSampler(texture)");
        return texture;
    }

    void create_native_render_resources(bool create_textures = true) {
        unsupported_texture_resource_count_ = 0;
        if (!interpreted_stream_.vertices.empty()) {
            std::vector<NativeVertex> vertices = interpreted_stream_.vertices;
            const size_t first_draw = std::min<size_t>(
                interpreted_stream_.presented_draw_begin,
                interpreted_stream_.draws.size());
            const size_t end_draw = std::min<size_t>(
                first_draw + interpreted_stream_.presented_draw_count,
                interpreted_stream_.draws.size());
            for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
                presented_half_quad_recovered_ |= recover_presented_half_surface_quad(
                    vertices,
                    interpreted_stream_.draws[draw_index],
                    swapchain_extent_.width,
                    swapchain_extent_.height,
                    presented_overscan_height_recovered_);
            }
            for (NativeVertex& vertex : vertices) {
                vertex.x = vertex.x * 2.0f / static_cast<float>(swapchain_extent_.width) - 1.0f;
                // A positive-height Vulkan viewport maps NDC -1 to the top edge.
                // Guest inline vertices use top-left screen coordinates.
                vertex.y = vertex.y * 2.0f / static_cast<float>(swapchain_extent_.height) - 1.0f;
            }
            const VkDeviceSize required_size = vertices.size() * sizeof(NativeVertex);
            if (vertex_buffer_ == VK_NULL_HANDLE || required_size > vertex_buffer_size_) {
                const VkDeviceSize allocation_size = options_.live_render_stream
                    ? std::max<VkDeviceSize>(required_size, 1024u * 1024u)
                    : required_size;
                vertex_buffer_size_ = allocation_size;
                create_buffer(
                    vertex_buffer_size_,
                    VK_BUFFER_USAGE_VERTEX_BUFFER_BIT,
                    VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,
                    vertex_buffer_,
                    vertex_memory_);
            }
            void* mapped = nullptr;
            vk_check(vkMapMemory(device_, vertex_memory_, 0, required_size, 0, &mapped), "vkMapMemory(vertices)");
            std::memcpy(mapped, vertices.data(), static_cast<size_t>(required_size));
            vkUnmapMemory(device_, vertex_memory_);
        }

        if (create_textures) {
        const uint32_t descriptor_count = static_cast<uint32_t>(recovered_source_.textures.size() + 1u);
        VkDescriptorPoolSize pool_size{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, descriptor_count};
        VkDescriptorPoolCreateInfo pool_info{};
        pool_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
        pool_info.maxSets = descriptor_count;
        pool_info.poolSizeCount = 1;
        pool_info.pPoolSizes = &pool_size;
        vk_check(vkCreateDescriptorPool(device_, &pool_info, nullptr, &texture_descriptor_pool_), "vkCreateDescriptorPool");

        host_textures_.push_back(create_host_texture(0, 1, 1, {255, 255, 255, 255}));
        for (const RecoveredTextureResource& resource : recovered_source_.textures) {
            std::vector<uint8_t> rgba;
            if (resource.format == "DXT1") {
                rgba = decompress_dxt1(resource);
            } else if (resource.format == "DXT5") {
                rgba = decompress_dxt5(resource);
            } else if (resource.payload.size() >= static_cast<size_t>(resource.width) * resource.height * 4u) {
                rgba.assign(resource.payload.begin(), resource.payload.begin() + static_cast<size_t>(resource.width) * resource.height * 4u);
            } else {
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
            host_textures_.push_back(create_host_texture(resource.address, resource.width, resource.height, rgba));
        }
        std::vector<VkDescriptorSetLayout> layouts(host_textures_.size(), texture_descriptor_layout_);
        std::vector<VkDescriptorSet> descriptor_sets(host_textures_.size());
        VkDescriptorSetAllocateInfo descriptor_allocation{};
        descriptor_allocation.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        descriptor_allocation.descriptorPool = texture_descriptor_pool_;
        descriptor_allocation.descriptorSetCount = static_cast<uint32_t>(layouts.size());
        descriptor_allocation.pSetLayouts = layouts.data();
        vk_check(vkAllocateDescriptorSets(device_, &descriptor_allocation, descriptor_sets.data()), "vkAllocateDescriptorSets");
        for (size_t index = 0; index < host_textures_.size(); ++index) {
            HostTexture& texture = host_textures_[index];
            texture.descriptor_set = descriptor_sets[index];
            VkDescriptorImageInfo image_info{texture.sampler, texture.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
            VkWriteDescriptorSet write{};
            write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            write.dstSet = texture.descriptor_set;
            write.dstBinding = 0;
            write.descriptorCount = 1;
            write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
            write.pImageInfo = &image_info;
            vkUpdateDescriptorSets(device_, 1, &write, 0, nullptr);
        }
        }
        log_.emit(
            "nv2a_native_resources_created",
            {
                {"vertices", std::to_string(interpreted_stream_.vertices.size())},
                {"draws", std::to_string(interpreted_stream_.draws.size())},
                {"presented_draws", std::to_string(interpreted_stream_.presented_draw_count)},
                {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
                {"textures", std::to_string(host_textures_.size() - 1u)},
                {"presented_half_quad_recovered", json_bool(presented_half_quad_recovered_)},
                {"presented_overscan_height_recovered", json_bool(presented_overscan_height_recovered_)},
            });
        const size_t diagnostic_first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t diagnostic_end_draw = std::min<size_t>(
            diagnostic_first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        uint32_t textured_presented_draw_count = 0;
        uint32_t unmatched_presented_texture_draw_count = 0;
        uint32_t unsupported_presented_primitive_count = 0;
        for (size_t draw_index = diagnostic_first_draw;
             draw_index < diagnostic_end_draw;
             ++draw_index) {
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            if (draw.primitive != 6u) {
                ++unsupported_presented_primitive_count;
            }
            if (!draw.texture_enabled || draw.texture_address == 0u) {
                continue;
            }
            ++textured_presented_draw_count;
            const bool texture_matched = std::any_of(
                host_textures_.begin() + 1,
                host_textures_.end(),
                [&](const HostTexture& texture) {
                    return texture.guest_address == draw.texture_address;
                });
            if (!texture_matched) {
                ++unmatched_presented_texture_draw_count;
            }
        }
        log_.emit(
            "render_validation",
            {
                {"strict", json_bool(options_.strict_render_validation)},
                {"texture_resource_count", std::to_string(recovered_source_.textures.size())},
                {"unsupported_texture_resource_count", std::to_string(unsupported_texture_resource_count_)},
                {"textured_presented_draw_count", std::to_string(textured_presented_draw_count)},
                {"unmatched_presented_texture_draw_count", std::to_string(unmatched_presented_texture_draw_count)},
                {"unsupported_presented_primitive_count", std::to_string(unsupported_presented_primitive_count)},
                {"unsupported_draw_arrays_count", std::to_string(interpreted_stream_.unsupported_draw_arrays_count)},
                {"passed", json_bool(
                    unsupported_texture_resource_count_ == 0u
                    && unmatched_presented_texture_draw_count == 0u
                    && unsupported_presented_primitive_count == 0u
                    && interpreted_stream_.unsupported_draw_arrays_count == 0u)},
            });
        if (options_.strict_render_validation
            && (unsupported_texture_resource_count_ != 0u
                || unmatched_presented_texture_draw_count != 0u
                || unsupported_presented_primitive_count != 0u
                || interpreted_stream_.unsupported_draw_arrays_count != 0u)) {
            std::ostringstream message;
            message << "strict render validation failed: "
                    << unsupported_texture_resource_count_ << " unsupported texture resources, "
                    << unmatched_presented_texture_draw_count << " unmatched textured presented draws, "
                    << unsupported_presented_primitive_count << " unsupported presented primitives, "
                    << interpreted_stream_.unsupported_draw_arrays_count << " unsupported DRAW_ARRAYS methods";
            throw std::runtime_error(message.str());
        }
        for (size_t draw_index = diagnostic_first_draw;
             draw_index < diagnostic_end_draw;
             ++draw_index) {
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            if (draw.vertex_count == 0u ||
                draw.first_vertex + draw.vertex_count > interpreted_stream_.vertices.size()) {
                continue;
            }
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
                    return texture.guest_address == draw.texture_address;
                });
            log_.emit(
                "nv2a_presented_draw_details",
                {
                    {"presented_index", std::to_string(draw_index - diagnostic_first_draw)},
                    {"draw_index", std::to_string(draw_index)},
                    {"primitive", std::to_string(draw.primitive)},
                    {"first_vertex", std::to_string(draw.first_vertex)},
                    {"vertex_count", std::to_string(draw.vertex_count)},
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
                    {"blend_enable", std::to_string(draw.blend_enable)},
                    {"blend_source_factor", std::to_string(draw.blend_source_factor)},
                    {"blend_destination_factor", std::to_string(draw.blend_destination_factor)},
                    {"blend_equation", std::to_string(draw.blend_equation)},
                    {"surface_format", std::to_string(draw.surface_format)},
                    {"surface_pitch", std::to_string(draw.surface_pitch)},
                    {"surface_color_offset", std::to_string(draw.surface_color_offset)},
                    {"color_mask", std::to_string(draw.color_mask)},
                    {"depth_test_enable", std::to_string(draw.depth_test_enable)},
                    {"shader_stage_program", std::to_string(draw.shader_stage_program)},
                    {"combiner_control", std::to_string(draw.combiner_control)},
                    {"vertex_format_0", std::to_string(draw.vertex_formats[0])},
                    {"vertex_format_3", std::to_string(draw.vertex_formats[3])},
                    {"vertex_format_9", std::to_string(draw.vertex_formats[9])},
                    {"transform_execution_mode", std::to_string(draw.transform_execution_mode)},
                    {"transform_program_start", std::to_string(draw.transform_program_start)},
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
        if (interpreted_stream_.presented_draw_count != 0u &&
            interpreted_stream_.presented_draw_begin < interpreted_stream_.draws.size()) {
            const NativeDraw& draw = interpreted_stream_.draws[interpreted_stream_.presented_draw_begin];
            const NativeVertex& vertex = interpreted_stream_.vertices[draw.first_vertex];
            const bool texture_matched = std::any_of(
                host_textures_.begin() + 1,
                host_textures_.end(),
                [&](const HostTexture& texture) { return texture.guest_address == draw.texture_address; });
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

    VkDescriptorSet descriptor_for_texture(uint32_t guest_address) const {
        for (const HostTexture& texture : host_textures_) {
            if (texture.guest_address == guest_address) {
                return texture.descriptor_set;
            }
        }
        return host_textures_.front().descriptor_set;
    }

    void record_native_draws(VkCommandBuffer command_buffer) const {
        if (interpreted_stream_.draws.empty() || vertex_buffer_ == VK_NULL_HANDLE) {
            return;
        }
        vkCmdBindPipeline(command_buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, graphics_pipeline_);
        const VkDeviceSize offset = 0;
        vkCmdBindVertexBuffers(command_buffer, 0, 1, &vertex_buffer_, &offset);
        const size_t first_draw = std::min<size_t>(
            interpreted_stream_.presented_draw_begin,
            interpreted_stream_.draws.size());
        const size_t end_draw = std::min<size_t>(
            first_draw + interpreted_stream_.presented_draw_count,
            interpreted_stream_.draws.size());
        for (size_t draw_index = first_draw; draw_index < end_draw; ++draw_index) {
            const NativeDraw& draw = interpreted_stream_.draws[draw_index];
            const VkDescriptorSet descriptor = descriptor_for_texture(
                draw.texture_enabled ? draw.texture_address : 0u);
            vkCmdBindDescriptorSets(command_buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline_layout_, 0, 1, &descriptor, 0, nullptr);
            vkCmdDraw(command_buffer, draw.vertex_count, 1, draw.first_vertex, 0);
        }
    }

    static std::array<uint8_t, 7> glyph_rows(char input) {
        switch (static_cast<char>(std::toupper(static_cast<unsigned char>(input)))) {
            case 'A': return {14, 17, 17, 31, 17, 17, 17};
            case 'B': return {30, 17, 17, 30, 17, 17, 30};
            case 'C': return {14, 17, 16, 16, 16, 17, 14};
            case 'D': return {30, 17, 17, 17, 17, 17, 30};
            case 'E': return {31, 16, 16, 30, 16, 16, 31};
            case 'F': return {31, 16, 16, 30, 16, 16, 16};
            case 'G': return {14, 17, 16, 23, 17, 17, 15};
            case 'H': return {17, 17, 17, 31, 17, 17, 17};
            case 'I': return {14, 4, 4, 4, 4, 4, 14};
            case 'J': return {7, 2, 2, 2, 18, 18, 12};
            case 'K': return {17, 18, 20, 24, 20, 18, 17};
            case 'L': return {16, 16, 16, 16, 16, 16, 31};
            case 'M': return {17, 27, 21, 21, 17, 17, 17};
            case 'N': return {17, 25, 21, 19, 17, 17, 17};
            case 'O': return {14, 17, 17, 17, 17, 17, 14};
            case 'P': return {30, 17, 17, 30, 16, 16, 16};
            case 'Q': return {14, 17, 17, 17, 21, 18, 13};
            case 'R': return {30, 17, 17, 30, 20, 18, 17};
            case 'S': return {15, 16, 16, 14, 1, 1, 30};
            case 'T': return {31, 4, 4, 4, 4, 4, 4};
            case 'U': return {17, 17, 17, 17, 17, 17, 14};
            case 'V': return {17, 17, 17, 17, 17, 10, 4};
            case 'W': return {17, 17, 17, 21, 21, 21, 10};
            case 'X': return {17, 17, 10, 4, 10, 17, 17};
            case 'Y': return {17, 17, 10, 4, 4, 4, 4};
            case 'Z': return {31, 1, 2, 4, 8, 16, 31};
            case '\'': return {4, 4, 8, 0, 0, 0, 0};
            case '.': return {0, 0, 0, 0, 0, 4, 4};
            case ',': return {0, 0, 0, 0, 4, 4, 8};
            case '-': return {0, 0, 0, 31, 0, 0, 0};
            default: return {};
        }
    }

    std::vector<std::string> wrap_frontend_text(const std::string& text, size_t max_chars) const {
        std::vector<std::string> lines;
        std::istringstream words(text);
        std::string word;
        std::string line;
        while (words >> word) {
            if (!line.empty() && line.size() + 1u + word.size() > max_chars) {
                lines.push_back(line);
                line.clear();
            }
            if (!line.empty()) {
                line.push_back(' ');
            }
            line += word;
        }
        if (!line.empty()) {
            lines.push_back(line);
        }
        return lines;
    }

    uint32_t record_frontend_text(VkCommandBuffer command_buffer, const std::string& text) const {
        if (text.empty()) {
            return 0;
        }
        constexpr uint32_t scale = 2;
        constexpr uint32_t glyph_advance = 6u * scale;
        constexpr uint32_t line_advance = 10u * scale;
        const size_t max_chars = std::max<size_t>(1u, swapchain_extent_.width / glyph_advance - 4u);
        const std::vector<std::string> lines = wrap_frontend_text(text, max_chars);
        std::vector<VkClearRect> rectangles;
        const int32_t total_height = static_cast<int32_t>(lines.size() * line_advance);
        int32_t y = std::max<int32_t>(16, (static_cast<int32_t>(swapchain_extent_.height) - total_height) / 2);
        for (const std::string& line : lines) {
            int32_t x = std::max<int32_t>(
                8,
                (static_cast<int32_t>(swapchain_extent_.width)
                    - static_cast<int32_t>(line.size() * glyph_advance)) / 2);
            for (const char ch : line) {
                const std::array<uint8_t, 7> rows = glyph_rows(ch);
                for (uint32_t row = 0; row < rows.size(); ++row) {
                    for (uint32_t column = 0; column < 5; ++column) {
                        if ((rows[row] & (1u << (4u - column))) == 0) {
                            continue;
                        }
                        VkClearRect rectangle{};
                        rectangle.rect.offset = {
                            x + static_cast<int32_t>(column * scale),
                            y + static_cast<int32_t>(row * scale),
                        };
                        rectangle.rect.extent = {scale, scale};
                        rectangle.baseArrayLayer = 0;
                        rectangle.layerCount = 1;
                        rectangles.push_back(rectangle);
                    }
                }
                x += static_cast<int32_t>(glyph_advance);
            }
            y += static_cast<int32_t>(line_advance);
        }
        if (!rectangles.empty()) {
            VkClearAttachment attachment{};
            attachment.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            attachment.colorAttachment = 0;
            attachment.clearValue.color = {{1.0f, 1.0f, 1.0f, 1.0f}};
            vkCmdClearAttachments(
                command_buffer,
                1,
                &attachment,
                static_cast<uint32_t>(rectangles.size()),
                rectangles.data());
        }
        return static_cast<uint32_t>(rectangles.size());
    }

    void create_command_buffers() {
        command_buffers_.resize(framebuffers_.size());
        const std::vector<RecoveredD3DCommand>& recovered_d3d_stream =
            recovered_source_.commands;
        const InterpretedD3DStream& interpreted_stream = interpreted_stream_;
        recovered_d3d_command_count_ = static_cast<uint32_t>(recovered_d3d_stream.size());
        recovered_d3d_mmio_count_ = static_cast<uint32_t>(
            std::count_if(
                recovered_d3d_stream.begin(),
                recovered_d3d_stream.end(),
                [](const RecoveredD3DCommand& command) {
                    return command.kind == RecoveredD3DCommandKind::MmioWrite;
                }));
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

        const bool record_frame_readback = readback_enabled()
            && (options_.flip_audit_ack.empty()
                || !flip_audit_health_check_reason().empty());
        current_work_has_readback_ = record_frame_readback;
        for (size_t index = 0; index < command_buffers_.size(); ++index) {
            VkCommandBufferBeginInfo begin_info{};
            begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
            vk_check(vkBeginCommandBuffer(command_buffers_[index], &begin_info), "vkBeginCommandBuffer");

            VkRenderPassBeginInfo render_pass_info{};
            render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
            render_pass_info.renderPass = render_pass_;
            render_pass_info.framebuffer = framebuffers_[index];
            render_pass_info.renderArea.offset = {0, 0};
            render_pass_info.renderArea.extent = swapchain_extent_;
            render_pass_info.clearValueCount = 1;
            render_pass_info.pClearValues = &interpreted_stream.diagnostic_clear_color;

            vkCmdBeginRenderPass(command_buffers_[index], &render_pass_info, VK_SUBPASS_CONTENTS_INLINE);
            record_native_draws(command_buffers_[index]);
            if (interpreted_stream_.draws.empty()) {
                frontend_text_rectangle_count_ = record_frontend_text(
                    command_buffers_[index], recovered_frontend_text_);
            }
            vkCmdEndRenderPass(command_buffers_[index]);
            if (record_frame_readback) {
                VkImageMemoryBarrier readback_barrier{};
                readback_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
                readback_barrier.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
                readback_barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
                readback_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                readback_barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                readback_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                readback_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                readback_barrier.image = swapchain_images_[index];
                readback_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
                readback_barrier.subresourceRange.levelCount = 1;
                readback_barrier.subresourceRange.layerCount = 1;
                vkCmdPipelineBarrier(
                    command_buffers_[index],
                    VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
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
            } else if (readback_enabled()) {
                VkImageMemoryBarrier present_barrier{};
                present_barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
                present_barrier.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
                present_barrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT;
                present_barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
                present_barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
                present_barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                present_barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
                present_barrier.image = swapchain_images_[index];
                present_barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
                present_barrier.subresourceRange.levelCount = 1;
                present_barrier.subresourceRange.layerCount = 1;
                vkCmdPipelineBarrier(
                    command_buffers_[index],
                    VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                    VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
                    0,
                    0,
                    nullptr,
                    0,
                    nullptr,
                    1,
                    &present_barrier);
            }
            vk_check(vkEndCommandBuffer(command_buffers_[index]), "vkEndCommandBuffer");
        }
        log_.emit(
            "recovered_d3d_command_stream_loaded",
            {
                {"commands", std::to_string(recovered_d3d_stream.size())},
                {"mmio_writes", std::to_string(recovered_d3d_mmio_count_)},
                {"push_buffer_writes", std::to_string(recovered_d3d_push_buffer_count_)},
                {"source", json_string(recovered_source_.source)},
            });
        log_.emit(
            "d3d8_stream_interpreted",
            {
                {"push_buffer_words", std::to_string(interpreted_stream.push_buffer_word_count)},
                {"method_packets", std::to_string(interpreted_stream.method_packet_count)},
                {"interpreted_methods", std::to_string(interpreted_stream.interpreted_method_count)},
                {"zero_count_method_words", std::to_string(interpreted_stream.zero_count_method_word_count)},
                {"control_flow_packets", std::to_string(interpreted_stream.control_flow_packet_count)},
                {"mmio_setup_writes", std::to_string(interpreted_stream.mmio_setup_write_count)},
                {"submission_kicks", std::to_string(interpreted_stream.submission_kick_count)},
                {"unknown_packets", std::to_string(interpreted_stream.unknown_packet_count)},
                {"truncated_packets", std::to_string(interpreted_stream.truncated_packet_count)},
                {"surface_payload_samples", std::to_string(interpreted_stream.surface_payload_sample_count)},
                {"surface_payload_dominant_count", std::to_string(interpreted_stream.surface_payload_dominant_count)},
                {"surface_payload_color_valid", interpreted_stream.surface_payload_color_valid ? "true" : "false"},
                {"surface_payload_argb", std::to_string(interpreted_stream.surface_payload_argb)},
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
                {"source_commands", std::to_string(recovered_d3d_stream.size())},
                {"method_packets", std::to_string(interpreted_stream.method_packet_count)},
                {"interpreted_methods", std::to_string(interpreted_stream.interpreted_method_count)},
                {"zero_count_method_words", std::to_string(interpreted_stream.zero_count_method_word_count)},
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
                    {"translation_semantics", json_string("recovered-title-text-hle")},
                });
        }
        log_.emit(
            "command_buffers_recorded",
            {
                {"count", std::to_string(command_buffers_.size())},
                {"translated_commands", std::to_string(translated_command_count_)},
                {"source_d3d_commands", std::to_string(recovered_d3d_stream.size())},
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
        write_controller_state();
        auto last_frame = std::chrono::steady_clock::now();
        auto next_frame_time = last_frame + std::chrono::milliseconds(kTargetFrameMs);
        while (running_ && (options_.max_frames == 0u || frame_count_ < options_.max_frames)) {
            const auto frame_start = std::chrono::steady_clock::now();
            pump_window_messages();
            update_controller_button_latches();
            if (!running_) {
                break;
            }
            reload_live_render_work();
            if (options_.inject_input && !input_injected_) {
                PostMessageW(hwnd_, WM_KEYDOWN, VK_SPACE, 0);
                PostMessageW(hwnd_, WM_KEYUP, VK_SPACE, 0);
                input_injected_ = true;
                log_.emit("input_injected", {{"virtual_key", std::to_string(VK_SPACE)}});
                const uint32_t pumped_messages = pump_window_messages();
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
            draw_frame();
            ++frame_count_;
            const auto after_draw = std::chrono::steady_clock::now();
            const auto draw_ms =
                std::chrono::duration_cast<std::chrono::milliseconds>(after_draw - frame_start).count();
            const auto sleep_ms = std::max<int64_t>(
                0,
                std::chrono::duration_cast<std::chrono::milliseconds>(next_frame_time - after_draw).count());
            if (sleep_ms > 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(sleep_ms));
            }
            const auto now = std::chrono::steady_clock::now();
            const auto elapsed_ms =
                std::chrono::duration_cast<std::chrono::milliseconds>(now - last_frame).count();
            last_frame = now;
            next_frame_time += std::chrono::milliseconds(kTargetFrameMs);
            if (next_frame_time < now) {
                next_frame_time = now + std::chrono::milliseconds(kTargetFrameMs);
            }
            log_.emit(
                "frame_presented",
                {
                    {"frame", std::to_string(frame_count_)},
                    {"elapsed_ms", std::to_string(elapsed_ms)},
                    {"draw_ms", std::to_string(draw_ms)},
                    {"target_frame_ms", std::to_string(kTargetFrameMs)},
                    {"pacing_sleep_ms", std::to_string(sleep_ms)},
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

    void draw_frame() {
        vk_check(vkWaitForFences(device_, 1, &in_flight_, VK_TRUE, UINT64_MAX), "vkWaitForFences");
        vk_check(vkResetFences(device_, 1, &in_flight_), "vkResetFences");

        uint32_t image_index = 0;
        VkResult acquire = vkAcquireNextImageKHR(
            device_, swapchain_, UINT64_MAX, image_available_, VK_NULL_HANDLE, &image_index);
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
        vk_check(vkQueueSubmit(graphics_queue_, 1, &submit_info, in_flight_), "vkQueueSubmit");

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
            vk_check(vkWaitForFences(device_, 1, &in_flight_, VK_TRUE, UINT64_MAX), "vkWaitForFences(readback)");
            if (capture_automatic) {
                capture_screenshot(options_.screenshot, "automatic");
                screenshot_captured_ = true;
            }
            if (capture_hotkey) {
                capture_screenshot(next_hotkey_screenshot_path(), "f12");
                hotkey_screenshot_pending_ = false;
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
        }

        VkPresentInfoKHR present_info{};
        present_info.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
        present_info.waitSemaphoreCount = 1;
        present_info.pWaitSemaphores = signal_semaphores;
        present_info.swapchainCount = 1;
        present_info.pSwapchains = &swapchain_;
        present_info.pImageIndices = &image_index;
        const VkResult present = vkQueuePresentKHR(graphics_queue_, &present_info);
        if (present == VK_ERROR_OUT_OF_DATE_KHR || present == VK_SUBOPTIMAL_KHR) {
            running_ = false;
            log_.emit("swapchain_present_suboptimal", {{"result", std::to_string(present)}});
            return;
        }
        vk_check(present, "vkQueuePresentKHR");
        if (capture_flip_audit) {
            acknowledge_current_flip_audit(
                flip_audit_readback ? &*flip_audit_readback : nullptr,
                flip_audit_frame_path,
                audit_health_reason);
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
        log_.emit(
            "frame_readback_captured",
            {
                {"output", json_string(screenshot_path.string())},
                {"trigger", json_string(trigger)},
                {"width", std::to_string(swapchain_extent_.width)},
                {"height", std::to_string(swapchain_extent_.height)},
                {"pixel_count", std::to_string(readback.pixel_count)},
                {"unique_colors", std::to_string(readback.unique_colors)},
                {"dominant_rgba", std::to_string(readback.dominant_rgba)},
                {"dominant_count", std::to_string(readback.dominant_count)},
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
        if (key == VK_F12) {
            if (message == WM_KEYDOWN
                && !hotkey_screenshot_key_down_
                && !options_.hotkey_screenshot_directory.empty()) {
                hotkey_screenshot_pending_ = true;
                log_.emit(
                    "hotkey_screenshot_queued",
                    {
                        {"frame", std::to_string(frame_count_ + 1u)},
                        {"directory", json_string(options_.hotkey_screenshot_directory.string())},
                    });
            }
            hotkey_screenshot_key_down_ = message == WM_KEYDOWN;
            return;
        }
        const uint16_t mask = controller_button_for_key(key);
        if (mask != 0u) {
            if (message == WM_KEYDOWN) {
                if ((controller_buttons_ & mask) != mask) {
                    controller_latched_buttons_ |= mask;
                    controller_latch_deadline_ = std::chrono::steady_clock::now()
                        + std::chrono::milliseconds(kControllerMinimumPulseMs);
                }
                controller_buttons_ |= mask;
            } else {
                controller_buttons_ &= static_cast<uint16_t>(~mask);
            }
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
            case VK_RETURN: return 0x1010u;
            case 'B': return 0x2000u;
            case 'X': return 0x4000u;
            case 'Y': return 0x8000u;
            default: return 0u;
        }
    }

    uint16_t effective_controller_buttons() const {
        return controller_buttons_ | controller_latched_buttons_;
    }

    void update_controller_button_latches() {
        if (controller_latched_buttons_ == 0u
            || std::chrono::steady_clock::now() < controller_latch_deadline_) {
            return;
        }
        const uint16_t before = effective_controller_buttons();
        controller_latched_buttons_ = 0u;
        if (effective_controller_buttons() != before) {
            write_controller_state();
        }
    }

    void write_controller_state() {
        if (options_.controller_state_json.empty()) {
            return;
        }
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
                   << ",\"left_trigger\":0,\"right_trigger\":0,"
                      "\"thumb_lx\":0,\"thumb_ly\":0,\"thumb_rx\":0,\"thumb_ry\":0}}}\n";
        }
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::seconds(10);
        while (!MoveFileExW(
            temporary.c_str(), options_.controller_state_json.c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            if (std::chrono::steady_clock::now() >= deadline) {
                throw std::runtime_error("cannot publish controller state JSON");
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        ++controller_publish_count_;
        log_.emit(
            "controller_state_published",
            {
                {"publish", std::to_string(controller_publish_count_)},
                {"buttons", std::to_string(effective_controller_buttons())},
                {"physical_buttons", std::to_string(controller_buttons_)},
                {"latched_buttons", std::to_string(controller_latched_buttons_)},
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
        if (device_) {
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
            if (vertex_buffer_) {
                vkDestroyBuffer(device_, vertex_buffer_, nullptr);
            }
            if (vertex_memory_) {
                vkFreeMemory(device_, vertex_memory_, nullptr);
            }
            if (texture_descriptor_pool_) {
                vkDestroyDescriptorPool(device_, texture_descriptor_pool_, nullptr);
            }
            for (const HostTexture& texture : host_textures_) {
                if (texture.sampler) vkDestroySampler(device_, texture.sampler, nullptr);
                if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
                if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
                if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
            }
            if (graphics_pipeline_) {
                vkDestroyPipeline(device_, graphics_pipeline_, nullptr);
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
            if (render_pass_) {
                vkDestroyRenderPass(device_, render_pass_, nullptr);
            }
            for (VkImageView image_view : swapchain_image_views_) {
                vkDestroyImageView(device_, image_view, nullptr);
            }
            if (swapchain_) {
                vkDestroySwapchainKHR(device_, swapchain_, nullptr);
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
    uint16_t controller_buttons_ = 0;
    uint16_t controller_latched_buttons_ = 0;
    std::chrono::steady_clock::time_point controller_latch_deadline_{};
    uint32_t controller_publish_count_ = 0;
    uint32_t live_render_reload_count_ = 0;
    int64_t last_command_load_us_ = 0;
    int64_t last_interpret_us_ = 0;
    std::filesystem::file_time_type live_render_write_time_{};
    std::filesystem::file_time_type live_resource_write_time_{};
    std::string live_resource_generation_;
    std::string live_command_generation_;
    std::ifstream live_command_file_;
    std::filesystem::path live_command_file_path_;
    uint32_t recovered_d3d_command_count_ = 0;
    size_t interpreted_source_command_count_ = 0;
    uint32_t recovered_d3d_mmio_count_ = 0;
    uint32_t recovered_d3d_push_buffer_count_ = 0;
    uint32_t interpreted_push_buffer_word_count_ = 0;
    uint32_t interpreted_method_packet_count_ = 0;
    uint32_t interpreted_method_count_ = 0;
    uint32_t interpreted_zero_count_method_word_count_ = 0;
    uint32_t translated_command_count_ = 0;
    uint32_t frontend_text_rectangle_count_ = 0;
    std::string recovered_frontend_text_;
    RecoveredD3DStreamSource recovered_source_;
    InterpretedD3DStream interpreted_stream_;
    bool presented_half_quad_recovered_ = false;
    bool presented_overscan_height_recovered_ = false;
    bool screenshot_captured_ = false;
    bool hotkey_screenshot_pending_ = false;
    bool hotkey_screenshot_key_down_ = false;
    uint32_t hotkey_screenshot_count_ = 0;
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
    VkSwapchainKHR swapchain_ = VK_NULL_HANDLE;
    VkFormat swapchain_format_ = VK_FORMAT_UNDEFINED;
    VkExtent2D swapchain_extent_{};
    std::vector<VkImage> swapchain_images_;
    std::vector<VkImageView> swapchain_image_views_;
    VkRenderPass render_pass_ = VK_NULL_HANDLE;
    VkDescriptorSetLayout texture_descriptor_layout_ = VK_NULL_HANDLE;
    VkDescriptorPool texture_descriptor_pool_ = VK_NULL_HANDLE;
    VkPipelineLayout pipeline_layout_ = VK_NULL_HANDLE;
    VkPipeline graphics_pipeline_ = VK_NULL_HANDLE;
    VkBuffer vertex_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory vertex_memory_ = VK_NULL_HANDLE;
    VkDeviceSize vertex_buffer_size_ = 0;
    std::vector<HostTexture> host_textures_;
    std::vector<VkFramebuffer> framebuffers_;
    VkCommandPool command_pool_ = VK_NULL_HANDLE;
    VkBuffer readback_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory readback_memory_ = VK_NULL_HANDLE;
    VkDeviceSize readback_size_ = 0;
    std::vector<VkCommandBuffer> command_buffers_;
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
        } else if (arg == L"--vertex-shader") {
            options.vertex_shader = std::filesystem::path(require_value(L"--vertex-shader"));
        } else if (arg == L"--fragment-shader") {
            options.fragment_shader = std::filesystem::path(require_value(L"--fragment-shader"));
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
                << L"                          [--debug-json PATH] [--render-stream-json PATH]\n"
                << L"                          [--live-render-stream-json PATH] [--controller-state-json PATH]\n"
                << L"                          [--strict-render-validation]\n"
                << L"                          [--flip-audit-ack PATH] [--flip-audit-frame-directory PATH]\n"
                << L"                          [--flip-audit-max-flips N]\n"
                << L"                          [--flip-audit-health-interval N]\n"
                << L"                          [--screenshot PATH] [--hotkey-screenshot-directory PATH]\n"
                << L"  Press F12 to save the current frame in the hotkey screenshot directory.\n"
                << L"                          [--vertex-shader PATH] [--fragment-shader PATH]\n"
                << L"                          [--inject-input]\n"
                << L"                          [--list-adapters]\n";
            ExitProcess(0);
        } else {
            throw std::runtime_error("unknown argument: " + narrow(arg));
        }
    }
    if (options.vertex_shader.empty() || options.fragment_shader.empty()) {
        throw std::runtime_error("--vertex-shader and --fragment-shader are required");
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
