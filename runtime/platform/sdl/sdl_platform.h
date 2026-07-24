#pragma once

#include <vulkan/vulkan.h>

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace b2r::platform {

struct ControllerState {
    bool connected = false;
    uint16_t buttons = 0u;
    uint16_t keyboard_buttons = 0u;
    uint16_t gamepad_buttons = 0u;
    uint16_t latched_buttons = 0u;
    uint8_t left_trigger = 0u;
    uint8_t right_trigger = 0u;
    int16_t thumb_lx = 0;
    int16_t thumb_ly = 0;
    int16_t thumb_rx = 0;
    int16_t thumb_ry = 0;
};

struct ControllerMetadata {
    std::string name;
    std::string type;
    std::string guid;
    std::string mapping;
    uint16_t vendor_id = 0u;
    uint16_t product_id = 0u;
    uint16_t product_version = 0u;
};

enum class PlatformEventType {
    Key,
    ControllerConnected,
    ControllerDisconnected,
    ControllerDiscoveryFailed,
};

struct PlatformEvent {
    PlatformEventType type = PlatformEventType::Key;
    bool key_down = false;
    int32_t keycode = 0;
    std::string error;
    ControllerMetadata controller;
};

struct PlatformPollResult {
    bool close_requested = false;
    bool toggle_fps_counter = false;
    bool write_metrics_report = false;
    bool capture_screenshot = false;
    bool controller_state_changed = false;
    std::vector<PlatformEvent> events;
};

struct SdlPlatformStatus {
    std::string version;
    std::string revision;
    std::string gamepad_error;
    std::string custom_mapping_path;
    int custom_mapping_count = 0;
    bool gamepad_initialized = false;
};

class SdlPlatform {
public:
    SdlPlatform();
    ~SdlPlatform();

    SdlPlatform(const SdlPlatform&) = delete;
    SdlPlatform& operator=(const SdlPlatform&) = delete;

    void initialize(bool enable_gamepads);
    void create_window(const std::string& title, uint32_t width, uint32_t height);
    std::vector<const char*> vulkan_instance_extensions() const;
    VkSurfaceKHR create_vulkan_surface(VkInstance instance) const;
    PlatformPollResult poll(uint64_t guest_flip_count);
    PlatformPollResult inject_confirm(uint64_t guest_flip_count);
    void set_window_title(const std::string& title);
    void shutdown();

    const ControllerState& controller_state() const;
    const ControllerMetadata& controller_metadata() const;
    const SdlPlatformStatus& status() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

}  // namespace b2r::platform
