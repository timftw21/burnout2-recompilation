#define NOMINMAX
#define WIN32_LEAN_AND_MEAN

#include "live_presenter_transport.h"

#include <windows.h>

#include "live_transport_layout.h"
#include "../platform/sdl/sdl_platform.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace b2r::host {
namespace {

using namespace b2r::live_transport;

std::string escape_json(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char ch : value) {
        switch (ch) {
            case '\\': out << "\\\\"; break;
            case '"': out << "\\\""; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (ch < 0x20u) {
                    constexpr char hex[] = "0123456789abcdef";
                    out << "\\u00" << hex[ch >> 4u] << hex[ch & 0x0fu];
                } else {
                    out << ch;
                }
                break;
        }
    }
    return out.str();
}

std::string json_string(const std::string& value) {
    return '"' + escape_json(value) + '"';
}

const char* manifest_field_name(uint16_t field_id) {
    switch (field_id) {
        case 1: return "resource_snapshots_unchanged";
        case 2: return "resource_snapshot_path";
        case 3: return "command_snapshot_path";
        case 4: return "frontend_text";
        case 5: return "frontend_text_x_bits";
        case 6: return "frontend_text_y_bits";
        case 7: return "frontend_text_size_bits";
        case 8: return "frontend_text_color_argb";
        case 9: return "interpreter_bootstrap_path";
        case 10: return "command_snapshot_record_count";
        case 11: return "lossless_flip_audit";
        case 12: return "audit_flip_index";
        case 13: return "audit_guest_steps";
        case 14: return "audit_flip_value";
        case 15: return "audit_command_record_count";
        case 16: return "audit_health_reason";
        case 17: return "guest_flip_count";
        case 18: return "guest_steps";
        case 19: return "guest_compiled_blocks";
        case 20: return "guest_invalidations";
        case 21: return "presentable_command_record_count";
        case 22: return "published_command_record_count";
        case 23: return "command_snapshot_base_record_count";
        case 24: return "command_transport_format";
        case 25: return "presentable_command_byte_count";
        case 26: return "command_snapshot_base_byte_count";
        case 27: return "resource_stream_generation";
        case 28: return "command_stream_generation";
        case 29: return "presentation_ack_path";
        case 30: return "presentation_event_name";
        case 31: return "publication_event_name";
        case 32: return "resource_transport_slot";
        case 33: return "resource_transport_size";
        case 34: return "resource_transport_format";
        default: return nullptr;
    }
}

std::optional<std::string> read_text_handle(HANDLE file) {
    LARGE_INTEGER start{};
    if (file == INVALID_HANDLE_VALUE
        || !SetFilePointerEx(file, start, nullptr, FILE_BEGIN)) {
        return std::nullopt;
    }
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file, &size) || size.QuadPart < 0
        || static_cast<uint64_t>(size.QuadPart)
            > std::numeric_limits<DWORD>::max()) {
        return std::nullopt;
    }
    std::string payload(static_cast<size_t>(size.QuadPart), '\0');
    DWORD bytes_read = 0u;
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

}  // namespace

struct LivePresenterTransport::Impl {
    HANDLE control_mapping = nullptr;
    uint8_t* control_view = nullptr;
    HANDLE command_mapping = nullptr;
    uint8_t* command_view = nullptr;
    uint64_t command_capacity = 0u;
    HANDLE resource_mapping = nullptr;
    uint8_t* resource_view = nullptr;
    uint64_t resource_slot_capacity = 0u;
    mutable HANDLE manifest_file = INVALID_HANDLE_VALUE;
    mutable std::filesystem::path manifest_path;
};

LivePresenterTransport::LivePresenterTransport() : impl_(new Impl()) {}

LivePresenterTransport::~LivePresenterTransport() {
    close();
    delete impl_;
}

LivePresenterTransportInfo LivePresenterTransport::open(
    const std::wstring& control_mapping_name) {
    close();
    if (control_mapping_name.empty()) {
        return {};
    }
    impl_->control_mapping = OpenFileMappingW(
        FILE_MAP_ALL_ACCESS, FALSE, control_mapping_name.c_str());
    if (!impl_->control_mapping) {
        throw std::runtime_error("cannot open live control transport");
    }
    impl_->control_view = static_cast<uint8_t*>(MapViewOfFile(
        impl_->control_mapping, FILE_MAP_ALL_ACCESS, 0u, 0u, kLiveControlSize));
    if (!impl_->control_view) {
        throw std::runtime_error("cannot map live control transport");
    }
    uint32_t schema_version = 0u;
    uint32_t mapping_size = 0u;
    std::memcpy(&schema_version, impl_->control_view + 8u, 4u);
    std::memcpy(&mapping_size, impl_->control_view + 12u, 4u);
    if (std::memcmp(
            impl_->control_view, kLiveControlMagic.data(), kLiveControlMagic.size()) != 0
        || schema_version != kLiveControlSchemaVersion
        || mapping_size != kLiveControlSize) {
        throw std::runtime_error("incompatible live control transport schema");
    }

    const std::wstring command_name = control_mapping_name + L"_commands";
    impl_->command_mapping = OpenFileMappingW(
        FILE_MAP_ALL_ACCESS, FALSE, command_name.c_str());
    if (!impl_->command_mapping) {
        throw std::runtime_error("cannot open live command transport");
    }
    impl_->command_view = static_cast<uint8_t*>(MapViewOfFile(
        impl_->command_mapping, FILE_MAP_ALL_ACCESS, 0u, 0u, kLiveCommandSize));
    if (!impl_->command_view) {
        throw std::runtime_error("cannot map live command transport");
    }
    uint32_t command_schema = 0u;
    uint32_t command_size = 0u;
    uint64_t command_capacity = 0u;
    std::memcpy(&command_schema, impl_->command_view + 8u, 4u);
    std::memcpy(&command_size, impl_->command_view + 12u, 4u);
    std::memcpy(&command_capacity, impl_->command_view + 16u, 8u);
    if (std::memcmp(
            impl_->command_view, kLiveCommandMagic.data(), kLiveCommandMagic.size()) != 0
        || command_schema != kLiveControlSchemaVersion
        || command_size != kLiveCommandSize
        || command_capacity != kLiveCommandSize - kLiveCommandDataOffset) {
        throw std::runtime_error("incompatible live command transport schema");
    }
    impl_->command_capacity = command_capacity;

    const std::wstring resource_name = control_mapping_name + L"_resources";
    impl_->resource_mapping = OpenFileMappingW(
        FILE_MAP_ALL_ACCESS, FALSE, resource_name.c_str());
    if (!impl_->resource_mapping) {
        throw std::runtime_error("cannot open live resource transport");
    }
    impl_->resource_view = static_cast<uint8_t*>(MapViewOfFile(
        impl_->resource_mapping, FILE_MAP_ALL_ACCESS, 0u, 0u, kLiveResourceSize));
    if (!impl_->resource_view) {
        throw std::runtime_error("cannot map live resource transport");
    }
    uint32_t resource_schema = 0u;
    uint32_t resource_size = 0u;
    uint64_t resource_capacity = 0u;
    std::memcpy(&resource_schema, impl_->resource_view + 8u, 4u);
    std::memcpy(&resource_size, impl_->resource_view + 12u, 4u);
    std::memcpy(&resource_capacity, impl_->resource_view + 16u, 8u);
    if (std::memcmp(
            impl_->resource_view, kLiveResourceMagic.data(), kLiveResourceMagic.size()) != 0
        || resource_schema != kLiveControlSchemaVersion
        || resource_size != kLiveResourceSize
        || resource_capacity != (kLiveResourceSize - kLiveResourceHeaderSize) / 2u) {
        throw std::runtime_error("incompatible live resource transport schema");
    }
    impl_->resource_slot_capacity = resource_capacity;
    return {
        schema_version,
        mapping_size,
        command_capacity,
        resource_capacity,
    };
}

void LivePresenterTransport::close() {
    if (impl_->manifest_file != INVALID_HANDLE_VALUE) {
        CloseHandle(impl_->manifest_file);
        impl_->manifest_file = INVALID_HANDLE_VALUE;
    }
    impl_->manifest_path.clear();
    if (impl_->control_view) {
        UnmapViewOfFile(impl_->control_view);
        impl_->control_view = nullptr;
    }
    if (impl_->command_view) {
        UnmapViewOfFile(impl_->command_view);
        impl_->command_view = nullptr;
    }
    if (impl_->resource_view) {
        UnmapViewOfFile(impl_->resource_view);
        impl_->resource_view = nullptr;
    }
    if (impl_->control_mapping) {
        CloseHandle(impl_->control_mapping);
        impl_->control_mapping = nullptr;
    }
    if (impl_->command_mapping) {
        CloseHandle(impl_->command_mapping);
        impl_->command_mapping = nullptr;
    }
    if (impl_->resource_mapping) {
        CloseHandle(impl_->resource_mapping);
        impl_->resource_mapping = nullptr;
    }
    impl_->command_capacity = 0u;
    impl_->resource_slot_capacity = 0u;
}

bool LivePresenterTransport::active() const {
    return impl_->control_view != nullptr;
}

uint64_t LivePresenterTransport::command_capacity() const {
    return impl_->command_capacity;
}

uint64_t LivePresenterTransport::resource_slot_capacity() const {
    return impl_->resource_slot_capacity;
}

uint32_t LivePresenterTransport::hot_path_profile_state() const {
    if (!impl_->control_view) {
        return kLiveHotPathProfileDisabled;
    }
    const auto* state = reinterpret_cast<volatile const LONG*>(
        impl_->control_view + kLiveHotPathProfileStateOffset);
    return static_cast<uint32_t>(*state);
}

uint32_t LivePresenterTransport::toggle_hot_path_profile() {
    if (!impl_->control_view) {
        return kLiveHotPathProfileDisabled;
    }
    auto* state = reinterpret_cast<volatile LONG*>(
        impl_->control_view + kLiveHotPathProfileStateOffset);
    const uint32_t current = static_cast<uint32_t>(*state);
    uint32_t next = current;
    if (current == kLiveHotPathProfileArmed) {
        next = kLiveHotPathProfileActive;
    } else if (current == kLiveHotPathProfileActive) {
        next = kLiveHotPathProfileComplete;
    }
    if (next != current) {
        InterlockedExchange(state, static_cast<LONG>(next));
    }
    return next;
}

std::optional<std::string> LivePresenterTransport::read_manifest() const {
    if (!impl_->control_view) {
        return std::nullopt;
    }
    const auto* sequence = reinterpret_cast<volatile const LONG*>(
        impl_->control_view + kLiveManifestSequenceOffset);
    const uint32_t published_sequence = static_cast<uint32_t>(*sequence);
    if (!sequence_is_published(published_sequence)) {
        return std::nullopt;
    }
    MemoryBarrier();
    uint32_t payload_size = 0u;
    std::memcpy(
        &payload_size, impl_->control_view + kLiveManifestSizeOffset, 4u);
    if (payload_size < 16u
        || payload_size > kLiveControlSize - kLiveManifestPayloadOffset) {
        return std::nullopt;
    }
    std::vector<uint8_t> payload(payload_size);
    std::memcpy(
        payload.data(),
        impl_->control_view + kLiveManifestPayloadOffset,
        payload.size());
    MemoryBarrier();
    if (!sequence_is_stable(
            published_sequence, static_cast<uint32_t>(*sequence))) {
        return std::nullopt;
    }
    uint32_t schema_version = 0u;
    uint32_t field_count = 0u;
    std::memcpy(&schema_version, payload.data() + 8u, 4u);
    std::memcpy(&field_count, payload.data() + 12u, 4u);
    if (std::memcmp(
            payload.data(), kLiveManifestMagic.data(), kLiveManifestMagic.size()) != 0
        || schema_version != kLiveControlSchemaVersion) {
        return std::nullopt;
    }
    size_t offset = 16u;
    bool first = true;
    std::ostringstream json;
    json << '{';
    for (uint32_t index = 0u; index < field_count; ++index) {
        if (offset + 8u > payload.size()) {
            return std::nullopt;
        }
        uint16_t field_id = 0u;
        uint32_t value_size = 0u;
        std::memcpy(&field_id, payload.data() + offset, 2u);
        const uint8_t field_type = payload[offset + 2u];
        std::memcpy(&value_size, payload.data() + offset + 4u, 4u);
        offset += 8u;
        if (value_size > payload.size() - offset) {
            return std::nullopt;
        }
        const char* field_name = manifest_field_name(field_id);
        if (field_name) {
            std::string encoded_value;
            if (field_type == kLiveManifestTypeU64 && value_size == 8u) {
                uint64_t value = 0u;
                std::memcpy(&value, payload.data() + offset, 8u);
                encoded_value = std::to_string(value);
            } else if (field_type == kLiveManifestTypeBool && value_size == 1u) {
                encoded_value = payload[offset] != 0u ? "true" : "false";
            } else if (field_type == kLiveManifestTypeUtf8) {
                encoded_value = json_string(std::string(
                    reinterpret_cast<const char*>(payload.data() + offset),
                    value_size));
            } else {
                return std::nullopt;
            }
            if (!first) {
                json << ',';
            }
            first = false;
            json << json_string(field_name) << ':' << encoded_value;
        }
        offset += value_size;
    }
    json << "}\n";
    return json.str();
}

std::optional<std::string> LivePresenterTransport::read_manifest_file(
    const std::filesystem::path& path,
    bool reopen) const {
    if (reopen && impl_->manifest_file != INVALID_HANDLE_VALUE) {
        CloseHandle(impl_->manifest_file);
        impl_->manifest_file = INVALID_HANDLE_VALUE;
        impl_->manifest_path.clear();
    }
    if (impl_->manifest_file == INVALID_HANDLE_VALUE || path != impl_->manifest_path) {
        if (impl_->manifest_file != INVALID_HANDLE_VALUE) {
            CloseHandle(impl_->manifest_file);
        }
        impl_->manifest_file = CreateFileW(
            path.c_str(),
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            nullptr,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            nullptr);
        impl_->manifest_path = path;
    }
    return read_text_handle(impl_->manifest_file);
}

bool LivePresenterTransport::read_command_bytes(
    uint64_t first_byte_count,
    uint64_t required_byte_count,
    std::vector<uint8_t>& payload) const {
    payload.clear();
    if (!impl_->command_view
        || required_byte_count < first_byte_count
        || required_byte_count - first_byte_count > impl_->command_capacity) {
        return false;
    }
    const auto* write_cursor = reinterpret_cast<volatile const LONGLONG*>(
        impl_->command_view + kLiveCommandWriteCursorOffset);
    const uint64_t published = static_cast<uint64_t>(*write_cursor);
    MemoryBarrier();
    if (required_byte_count > published
        || first_byte_count < published - std::min(
            published, impl_->command_capacity)) {
        return false;
    }
    const uint64_t appended_byte_count = required_byte_count - first_byte_count;
    if (appended_byte_count == 0u) {
        return true;
    }
    payload.resize(static_cast<size_t>(appended_byte_count));
    const uint64_t start = first_byte_count % impl_->command_capacity;
    const size_t first = static_cast<size_t>(std::min<uint64_t>(
        appended_byte_count, impl_->command_capacity - start));
    std::memcpy(
        payload.data(),
        impl_->command_view + kLiveCommandDataOffset + start,
        first);
    if (first < payload.size()) {
        std::memcpy(
            payload.data() + first,
            impl_->command_view + kLiveCommandDataOffset,
            payload.size() - first);
    }
    MemoryBarrier();
    if (static_cast<uint64_t>(*write_cursor) < required_byte_count) {
        payload.clear();
        return false;
    }
    return true;
}

void LivePresenterTransport::commit_command_read_cursor(uint64_t byte_count) {
    auto* read_cursor = reinterpret_cast<volatile LONGLONG*>(
        impl_->command_view + kLiveCommandReadCursorOffset);
    InterlockedExchange64(read_cursor, static_cast<LONGLONG>(byte_count));
}

bool LivePresenterTransport::read_resource_slot(
    uint32_t slot,
    uint64_t expected_size,
    const std::string& expected_generation,
    std::vector<uint8_t>& payload) const {
    if (!impl_->resource_view || slot > 1u
        || expected_size > impl_->resource_slot_capacity) {
        return false;
    }
    const size_t metadata_offset = kLiveResourceMetadataOffsets[slot];
    const auto* sequence = reinterpret_cast<volatile const LONG*>(
        impl_->resource_view + metadata_offset);
    const uint32_t published_sequence = static_cast<uint32_t>(*sequence);
    if (!sequence_is_published(published_sequence)) {
        return false;
    }
    MemoryBarrier();
    uint32_t payload_size = 0u;
    uint64_t generation = 0u;
    std::memcpy(&payload_size, impl_->resource_view + metadata_offset + 4u, 4u);
    std::memcpy(&generation, impl_->resource_view + metadata_offset + 8u, 8u);
    if (payload_size != expected_size
        || generation != std::stoull(expected_generation)) {
        return false;
    }
    payload.resize(payload_size);
    const uint64_t slot_offset = kLiveResourceHeaderSize
        + static_cast<uint64_t>(slot) * impl_->resource_slot_capacity;
    std::memcpy(
        payload.data(), impl_->resource_view + slot_offset, payload.size());
    MemoryBarrier();
    return sequence_is_stable(
        published_sequence, static_cast<uint32_t>(*sequence));
}

void LivePresenterTransport::publish_controller(
    const b2r::platform::ControllerState& controller) {
    auto* sequence = reinterpret_cast<volatile LONG*>(
        impl_->control_view + kLiveControllerSequenceOffset);
    InterlockedIncrement(sequence);
    std::array<uint8_t, 16> payload{};
    std::memcpy(payload.data(), &controller.buttons, 2u);
    payload[2] = controller.left_trigger;
    payload[3] = controller.right_trigger;
    std::memcpy(payload.data() + 4u, &controller.thumb_lx, 2u);
    std::memcpy(payload.data() + 6u, &controller.thumb_ly, 2u);
    std::memcpy(payload.data() + 8u, &controller.thumb_rx, 2u);
    std::memcpy(payload.data() + 10u, &controller.thumb_ry, 2u);
    payload[12] = 1u;
    payload[13] = controller.connected ? 1u : 0u;
    std::memcpy(
        impl_->control_view + kLiveControllerPayloadOffset,
        payload.data(),
        payload.size());
    MemoryBarrier();
    InterlockedIncrement(sequence);
}

void LivePresenterTransport::publish_presentation(
    uint64_t generation,
    uint64_t guest_flip_count) {
    auto* sequence = reinterpret_cast<volatile LONG*>(
        impl_->control_view + kLivePresentationSequenceOffset);
    InterlockedIncrement(sequence);
    std::memcpy(
        impl_->control_view + kLivePresentationPayloadOffset,
        &generation,
        sizeof(generation));
    std::memcpy(
        impl_->control_view + kLivePresentationPayloadOffset + sizeof(generation),
        &guest_flip_count,
        sizeof(guest_flip_count));
    MemoryBarrier();
    InterlockedIncrement(sequence);
}

}  // namespace b2r::host
