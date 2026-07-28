#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

namespace b2r::platform {
struct ControllerState;
}

namespace b2r::host {

struct LivePresenterTransportInfo {
    uint32_t schema_version = 0u;
    uint32_t control_mapping_size = 0u;
    uint64_t command_capacity = 0u;
    uint64_t resource_slot_capacity = 0u;
};

class LivePresenterTransport {
public:
    LivePresenterTransport();
    ~LivePresenterTransport();

    LivePresenterTransport(const LivePresenterTransport&) = delete;
    LivePresenterTransport& operator=(const LivePresenterTransport&) = delete;

    LivePresenterTransportInfo open(const std::wstring& control_mapping_name);
    void close();

    bool active() const;
    uint64_t command_capacity() const;
    uint64_t resource_slot_capacity() const;
    uint32_t hot_path_profile_state() const;
    uint32_t toggle_hot_path_profile();

    std::optional<std::string> read_manifest() const;
    std::optional<std::string> read_manifest_file(
        const std::filesystem::path& path,
        bool reopen) const;
    bool read_command_bytes(
        uint64_t first_byte_count,
        uint64_t required_byte_count,
        std::vector<uint8_t>& payload) const;
    void commit_command_read_cursor(uint64_t byte_count);
    bool read_resource_slot(
        uint32_t slot,
        uint64_t expected_size,
        const std::string& expected_generation,
        std::vector<uint8_t>& payload) const;
    void publish_controller(const b2r::platform::ControllerState& controller);
    void publish_presentation(uint64_t generation, uint64_t guest_flip_count);

private:
    struct Impl;
    Impl* impl_;
};

}  // namespace b2r::host
