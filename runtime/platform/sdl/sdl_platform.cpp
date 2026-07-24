#define SDL_MAIN_HANDLED

#include "sdl_platform.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>
#include <SDL3/SDL_vulkan.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

namespace b2r::platform {
namespace {

constexpr int32_t kStickDeadzone = 4096;
constexpr int32_t kStickPublishHysteresis = 256;
constexpr int32_t kTriggerPublishHysteresis = 8;
constexpr int64_t kMinimumPulseMs = 150;
constexpr int64_t kMaximumPulseMs = 2000;
constexpr uint64_t kMinimumPulseGuestFlips = 2u;
constexpr auto kGamepadDiscoveryInterval = std::chrono::milliseconds(500);

constexpr uint16_t kDpadUp = 0x0001u;
constexpr uint16_t kDpadDown = 0x0002u;
constexpr uint16_t kDpadLeft = 0x0004u;
constexpr uint16_t kDpadRight = 0x0008u;
constexpr uint16_t kStart = 0x0010u;
constexpr uint16_t kBack = 0x0020u;
constexpr uint16_t kLeftThumb = 0x0040u;
constexpr uint16_t kRightThumb = 0x0080u;
constexpr uint16_t kBlack = 0x0100u;
constexpr uint16_t kWhite = 0x0200u;
constexpr uint16_t kA = 0x1000u;
constexpr uint16_t kB = 0x2000u;
constexpr uint16_t kX = 0x4000u;
constexpr uint16_t kY = 0x8000u;

int16_t normalize_stick(int16_t raw_value, bool invert = false) {
    int32_t value = raw_value;
    if (invert) {
        value = value == -32768 ? 32767 : -value;
    }
    const int32_t magnitude = std::abs(value);
    if (magnitude <= kStickDeadzone) {
        return 0;
    }
    const int32_t adjusted = std::min<int32_t>(
        32767,
        (magnitude - kStickDeadzone) * 32767 / (32767 - kStickDeadzone));
    const int32_t signed_adjusted = value < 0 ? -adjusted : adjusted;
    if (adjusted == 32767) {
        return static_cast<int16_t>(signed_adjusted);
    }
    return static_cast<int16_t>((signed_adjusted / 128) * 128);
}

uint8_t normalize_trigger(int16_t raw_value) {
    const int32_t clamped = std::clamp<int32_t>(raw_value, 0, 32767);
    const int32_t value = (clamped * 255 + 16383) / 32767;
    if (value <= 4) {
        return 0u;
    }
    return static_cast<uint8_t>(value >= 252 ? 255 : (value / 4) * 4);
}

bool axis_equivalent(int32_t left, int32_t right, int32_t limit) {
    if (left == right) {
        return true;
    }
    if (left == 0 || right == 0
        || std::abs(left) == limit || std::abs(right) == limit) {
        return false;
    }
    return std::abs(left - right) <= kStickPublishHysteresis;
}

bool trigger_equivalent(uint8_t left, uint8_t right) {
    if (left == right) {
        return true;
    }
    if (left == 0u || right == 0u || left == 255u || right == 255u) {
        return false;
    }
    return std::abs(static_cast<int32_t>(left) - right)
        <= kTriggerPublishHysteresis;
}

bool controller_states_equivalent(
    const ControllerState& left,
    const ControllerState& right) {
    return left.connected == right.connected
        && left.buttons == right.buttons
        && left.keyboard_buttons == right.keyboard_buttons
        && left.gamepad_buttons == right.gamepad_buttons
        && left.latched_buttons == right.latched_buttons
        && trigger_equivalent(left.left_trigger, right.left_trigger)
        && trigger_equivalent(left.right_trigger, right.right_trigger)
        && axis_equivalent(left.thumb_lx, right.thumb_lx, 32767)
        && axis_equivalent(left.thumb_ly, right.thumb_ly, 32767)
        && axis_equivalent(left.thumb_rx, right.thumb_rx, 32767)
        && axis_equivalent(left.thumb_ry, right.thumb_ry, 32767);
}

uint16_t controller_button_for_key(SDL_Keycode key) {
    switch (key) {
        case SDLK_UP: return kDpadUp;
        case SDLK_DOWN: return kDpadDown;
        case SDLK_LEFT: return kDpadLeft;
        case SDLK_RIGHT: return kDpadRight;
        case SDLK_S: return kStart;
        case SDLK_BACKSPACE: return kBack;
        case SDLK_SPACE:
        case SDLK_RETURN: return kA;
        case SDLK_B: return kB;
        case SDLK_X: return kX;
        case SDLK_Y: return kY;
        default: return 0u;
    }
}

}  // namespace

struct SdlPlatform::Impl {
    using Clock = std::chrono::steady_clock;

    SDL_Window* window = nullptr;
    SDL_Gamepad* gamepad = nullptr;
    SDL_JoystickID gamepad_id = 0u;
    bool video_initialized = false;
    bool vulkan_loaded = false;
    bool gamepad_initialized = false;
    std::array<bool, SDL_SCANCODE_COUNT> key_down{};
    uint16_t keyboard_buttons = 0u;
    uint16_t latched_buttons = 0u;
    std::array<uint64_t, 16> latch_guest_flip_counts{};
    std::array<Clock::time_point, 16> latch_deadlines{};
    std::array<Clock::time_point, 16> latch_max_deadlines{};
    ControllerState controller{};
    ControllerMetadata metadata{};
    SdlPlatformStatus status{};
    Clock::time_point next_gamepad_discovery{};

    uint16_t buttons_from_keys() const {
        uint16_t buttons = 0u;
        for (int index = 0; index < SDL_SCANCODE_COUNT; ++index) {
            if (!key_down[static_cast<size_t>(index)]) {
                continue;
            }
            buttons |= controller_button_for_key(
                SDL_GetKeyFromScancode(
                    static_cast<SDL_Scancode>(index), SDL_KMOD_NONE, false));
        }
        return buttons;
    }

    void latch_pressed_buttons(
        uint16_t previous,
        uint16_t current,
        uint64_t guest_flip_count) {
        const uint16_t pressed = static_cast<uint16_t>(current & ~previous);
        if (pressed == 0u) {
            return;
        }
        const auto now = Clock::now();
        for (size_t bit_index = 0u; bit_index < 16u; ++bit_index) {
            const uint16_t bit = static_cast<uint16_t>(1u << bit_index);
            if ((pressed & bit) == 0u) {
                continue;
            }
            latched_buttons |= bit;
            latch_deadlines[bit_index] = now + std::chrono::milliseconds(kMinimumPulseMs);
            latch_max_deadlines[bit_index] = now + std::chrono::milliseconds(kMaximumPulseMs);
            latch_guest_flip_counts[bit_index] = guest_flip_count;
        }
    }

    void update_latches(uint64_t guest_flip_count) {
        if (latched_buttons == 0u) {
            return;
        }
        const auto now = Clock::now();
        for (size_t bit_index = 0u; bit_index < 16u; ++bit_index) {
            const uint16_t bit = static_cast<uint16_t>(1u << bit_index);
            if ((latched_buttons & bit) == 0u || now < latch_deadlines[bit_index]) {
                continue;
            }
            const uint64_t latched_flip = latch_guest_flip_counts[bit_index];
            const uint64_t flip_pulse = guest_flip_count >= latched_flip
                ? guest_flip_count - latched_flip
                : 0u;
            if (flip_pulse >= kMinimumPulseGuestFlips
                || now >= latch_max_deadlines[bit_index]) {
                latched_buttons &= static_cast<uint16_t>(~bit);
            }
        }
    }

    ControllerState read_controller() const {
        ControllerState state{};
        state.keyboard_buttons = keyboard_buttons;
        state.latched_buttons = latched_buttons;
        if (gamepad != nullptr && SDL_GamepadConnected(gamepad)) {
            state.connected = true;
            const auto button_down = [this](SDL_GamepadButton button) {
                return SDL_GetGamepadButton(gamepad, button);
            };
            if (button_down(SDL_GAMEPAD_BUTTON_SOUTH)) state.gamepad_buttons |= kA;
            if (button_down(SDL_GAMEPAD_BUTTON_EAST)) state.gamepad_buttons |= kB;
            if (button_down(SDL_GAMEPAD_BUTTON_WEST)) state.gamepad_buttons |= kX;
            if (button_down(SDL_GAMEPAD_BUTTON_NORTH)) state.gamepad_buttons |= kY;
            if (button_down(SDL_GAMEPAD_BUTTON_LEFT_SHOULDER)) state.gamepad_buttons |= kWhite;
            if (button_down(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER)) state.gamepad_buttons |= kBlack;
            if (button_down(SDL_GAMEPAD_BUTTON_BACK)) state.gamepad_buttons |= kBack;
            if (button_down(SDL_GAMEPAD_BUTTON_START)) state.gamepad_buttons |= kStart;
            if (button_down(SDL_GAMEPAD_BUTTON_LEFT_STICK)) state.gamepad_buttons |= kLeftThumb;
            if (button_down(SDL_GAMEPAD_BUTTON_RIGHT_STICK)) state.gamepad_buttons |= kRightThumb;
            if (button_down(SDL_GAMEPAD_BUTTON_DPAD_UP)) state.gamepad_buttons |= kDpadUp;
            if (button_down(SDL_GAMEPAD_BUTTON_DPAD_DOWN)) state.gamepad_buttons |= kDpadDown;
            if (button_down(SDL_GAMEPAD_BUTTON_DPAD_LEFT)) state.gamepad_buttons |= kDpadLeft;
            if (button_down(SDL_GAMEPAD_BUTTON_DPAD_RIGHT)) state.gamepad_buttons |= kDpadRight;
            state.thumb_lx = normalize_stick(
                SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFTX));
            state.thumb_ly = normalize_stick(
                SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFTY), true);
            state.thumb_rx = normalize_stick(
                SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_RIGHTX));
            state.thumb_ry = normalize_stick(
                SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_RIGHTY), true);
            state.left_trigger = normalize_trigger(
                SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_LEFT_TRIGGER));
            state.right_trigger = normalize_trigger(
                SDL_GetGamepadAxis(gamepad, SDL_GAMEPAD_AXIS_RIGHT_TRIGGER));
        }
        state.buttons = state.keyboard_buttons | state.latched_buttons
            | state.gamepad_buttons;
        return state;
    }

    PlatformEvent controller_event(PlatformEventType type) const {
        PlatformEvent event{};
        event.type = type;
        event.controller = metadata;
        return event;
    }

    bool open_gamepad(SDL_JoystickID id, PlatformPollResult* result) {
        if (gamepad != nullptr) {
            return false;
        }
        SDL_Gamepad* candidate = SDL_OpenGamepad(id);
        if (candidate == nullptr) {
            return false;
        }
        gamepad = candidate;
        gamepad_id = id;
        const char* name = SDL_GetGamepadName(candidate);
        metadata.name = name ? name : "Unknown gamepad";
        const char* type_name = SDL_GetGamepadStringForType(SDL_GetGamepadType(candidate));
        metadata.type = type_name ? type_name : "unknown";
        metadata.vendor_id = SDL_GetGamepadVendor(candidate);
        metadata.product_id = SDL_GetGamepadProduct(candidate);
        metadata.product_version = SDL_GetGamepadProductVersion(candidate);
        std::array<char, 33> guid{};
        SDL_GUIDToString(
            SDL_GetGamepadGUIDForID(gamepad_id), guid.data(), static_cast<int>(guid.size()));
        metadata.guid = guid.data();
        char* mapping = SDL_GetGamepadMapping(candidate);
        metadata.mapping = mapping ? mapping : "";
        SDL_free(mapping);
        if (result != nullptr) {
            result->events.push_back(controller_event(PlatformEventType::ControllerConnected));
        }
        return true;
    }

    void close_gamepad(PlatformPollResult* result) {
        if (gamepad == nullptr) {
            return;
        }
        if (result != nullptr) {
            result->events.push_back(
                controller_event(PlatformEventType::ControllerDisconnected));
        }
        SDL_CloseGamepad(gamepad);
        gamepad = nullptr;
        gamepad_id = 0u;
        metadata = {};
        next_gamepad_discovery = Clock::now();
    }

    void discover_gamepad(PlatformPollResult* result) {
        int count = 0;
        SDL_ClearError();
        SDL_JoystickID* ids = SDL_GetGamepads(&count);
        if (ids == nullptr && SDL_GetError()[0] != '\0' && result != nullptr) {
            PlatformEvent event{};
            event.type = PlatformEventType::ControllerDiscoveryFailed;
            event.error = SDL_GetError();
            result->events.push_back(std::move(event));
        }
        for (int index = 0; index < count && gamepad == nullptr; ++index) {
            open_gamepad(ids[index], result);
        }
        SDL_free(ids);
        next_gamepad_discovery = Clock::now() + kGamepadDiscoveryInterval;
    }

    void handle_key(
        const SDL_KeyboardEvent& key,
        uint64_t guest_flip_count,
        PlatformPollResult& result) {
        PlatformEvent event{};
        event.type = PlatformEventType::Key;
        event.key_down = key.down;
        event.keycode = static_cast<int32_t>(key.key);
        result.events.push_back(std::move(event));
        if (key.down && !key.repeat && key.key == SDLK_ESCAPE) {
            result.close_requested = true;
        }
        if (key.down && !key.repeat && key.key == SDLK_F9) {
            result.toggle_fps_counter = true;
        } else if (key.down && !key.repeat && key.key == SDLK_F11) {
            result.write_metrics_report = true;
        } else if (key.down && !key.repeat && key.key == SDLK_F12) {
            result.capture_screenshot = true;
        }
        const uint16_t mask = controller_button_for_key(key.key);
        if (mask == 0u || key.scancode >= SDL_SCANCODE_COUNT) {
            return;
        }
        const uint16_t previous = keyboard_buttons;
        key_down[static_cast<size_t>(key.scancode)] = key.down;
        keyboard_buttons = buttons_from_keys();
        latch_pressed_buttons(previous, keyboard_buttons, guest_flip_count);
    }
};

SdlPlatform::SdlPlatform() : impl_(std::make_unique<Impl>()) {}

SdlPlatform::~SdlPlatform() {
    shutdown();
}

void SdlPlatform::initialize(bool enable_gamepads) {
    SDL_SetMainReady();
    if (!SDL_InitSubSystem(SDL_INIT_VIDEO)) {
        throw std::runtime_error(std::string("SDL video initialization failed: ") + SDL_GetError());
    }
    impl_->video_initialized = true;
    if (!SDL_Vulkan_LoadLibrary(nullptr)) {
        throw std::runtime_error(std::string("SDL Vulkan loading failed: ") + SDL_GetError());
    }
    impl_->vulkan_loaded = true;

    const int version = SDL_GetVersion();
    std::ostringstream version_text;
    version_text << SDL_VERSIONNUM_MAJOR(version) << '.'
                 << SDL_VERSIONNUM_MINOR(version) << '.'
                 << SDL_VERSIONNUM_MICRO(version);
    impl_->status.version = version_text.str();
    const char* revision = SDL_GetRevision();
    impl_->status.revision = revision ? revision : "";

    if (!enable_gamepads) {
        return;
    }
    if (!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) {
        impl_->status.gamepad_error = SDL_GetError();
        return;
    }
    impl_->gamepad_initialized = true;
    impl_->status.gamepad_initialized = true;

    const char* base_path = SDL_GetBasePath();
    if (base_path != nullptr) {
        const std::filesystem::path mapping_path =
            std::filesystem::u8path(base_path) / "gamecontrollerdb.txt";
        impl_->status.custom_mapping_path = mapping_path.string();
        if (std::filesystem::is_regular_file(mapping_path)) {
            const int count = SDL_AddGamepadMappingsFromFile(
                mapping_path.u8string().c_str());
            if (count < 0) {
                impl_->status.gamepad_error = SDL_GetError();
            } else {
                impl_->status.custom_mapping_count = count;
            }
        }
    }
    impl_->discover_gamepad(nullptr);
    impl_->controller = impl_->read_controller();
}

void SdlPlatform::create_window(
    const std::string& title,
    uint32_t width,
    uint32_t height) {
    impl_->window = SDL_CreateWindow(
        title.c_str(),
        static_cast<int>(width),
        static_cast<int>(height),
        SDL_WINDOW_VULKAN | SDL_WINDOW_HIGH_PIXEL_DENSITY);
    if (impl_->window == nullptr) {
        throw std::runtime_error(std::string("SDL window creation failed: ") + SDL_GetError());
    }
}

std::vector<const char*> SdlPlatform::vulkan_instance_extensions() const {
    Uint32 count = 0u;
    const char* const* extensions = SDL_Vulkan_GetInstanceExtensions(&count);
    if (extensions == nullptr) {
        throw std::runtime_error(
            std::string("SDL Vulkan extension query failed: ") + SDL_GetError());
    }
    return std::vector<const char*>(extensions, extensions + count);
}

VkSurfaceKHR SdlPlatform::create_vulkan_surface(VkInstance instance) const {
    if (impl_->window == nullptr) {
        throw std::runtime_error("SDL Vulkan surface requested before window creation");
    }
    VkSurfaceKHR surface = VK_NULL_HANDLE;
    if (!SDL_Vulkan_CreateSurface(impl_->window, instance, nullptr, &surface)) {
        throw std::runtime_error(
            std::string("SDL Vulkan surface creation failed: ") + SDL_GetError());
    }
    return surface;
}

PlatformPollResult SdlPlatform::poll(uint64_t guest_flip_count) {
    PlatformPollResult result{};
    SDL_Event event{};
    while (SDL_PollEvent(&event)) {
        if (event.type == SDL_EVENT_QUIT
            || event.type == SDL_EVENT_WINDOW_CLOSE_REQUESTED) {
            result.close_requested = true;
        } else if (event.type == SDL_EVENT_KEY_DOWN
                   || event.type == SDL_EVENT_KEY_UP) {
            impl_->handle_key(event.key, guest_flip_count, result);
        } else if (event.type == SDL_EVENT_GAMEPAD_ADDED) {
            impl_->open_gamepad(event.gdevice.which, &result);
        } else if (event.type == SDL_EVENT_GAMEPAD_REMOVED
                   && impl_->gamepad_id == event.gdevice.which) {
            impl_->close_gamepad(&result);
        }
    }
    if (impl_->gamepad_initialized) {
        SDL_UpdateGamepads();
        if (impl_->gamepad != nullptr && !SDL_GamepadConnected(impl_->gamepad)) {
            impl_->close_gamepad(&result);
        }
        if (impl_->gamepad == nullptr
            && Impl::Clock::now() >= impl_->next_gamepad_discovery) {
            impl_->discover_gamepad(&result);
        }
    }
    impl_->update_latches(guest_flip_count);
    const ControllerState next = impl_->read_controller();
    result.controller_state_changed =
        !controller_states_equivalent(impl_->controller, next);
    if (result.controller_state_changed) {
        impl_->controller = next;
    }
    return result;
}

PlatformPollResult SdlPlatform::inject_confirm(uint64_t guest_flip_count) {
    PlatformPollResult result{};
    const uint16_t previous = impl_->keyboard_buttons;
    impl_->latch_pressed_buttons(previous, static_cast<uint16_t>(previous | kA), guest_flip_count);
    const ControllerState next = impl_->read_controller();
    result.controller_state_changed =
        !controller_states_equivalent(impl_->controller, next);
    impl_->controller = next;
    PlatformEvent down{};
    down.type = PlatformEventType::Key;
    down.key_down = true;
    down.keycode = static_cast<int32_t>(SDLK_SPACE);
    result.events.push_back(down);
    PlatformEvent up = down;
    up.key_down = false;
    result.events.push_back(up);
    return result;
}

void SdlPlatform::set_window_title(const std::string& title) {
    if (impl_->window != nullptr && !SDL_SetWindowTitle(impl_->window, title.c_str())) {
        throw std::runtime_error(
            std::string("SDL window title update failed: ") + SDL_GetError());
    }
}

void SdlPlatform::shutdown() {
    if (!impl_) {
        return;
    }
    impl_->close_gamepad(nullptr);
    if (impl_->window != nullptr) {
        SDL_DestroyWindow(impl_->window);
        impl_->window = nullptr;
    }
    if (impl_->gamepad_initialized) {
        SDL_QuitSubSystem(SDL_INIT_GAMEPAD);
        impl_->gamepad_initialized = false;
    }
    if (impl_->vulkan_loaded) {
        SDL_Vulkan_UnloadLibrary();
        impl_->vulkan_loaded = false;
    }
    if (impl_->video_initialized) {
        SDL_QuitSubSystem(SDL_INIT_VIDEO);
        impl_->video_initialized = false;
    }
}

const ControllerState& SdlPlatform::controller_state() const {
    return impl_->controller;
}

const ControllerMetadata& SdlPlatform::controller_metadata() const {
    return impl_->metadata;
}

const SdlPlatformStatus& SdlPlatform::status() const {
    return impl_->status;
}

}  // namespace b2r::platform
