#pragma once
#include <array>
#include <cstdint>
#include <string>

struct SDL_Gamepad;
union SDL_Event;
namespace b2 {
struct GamepadState {
    std::uint32_t packet=0;
    std::uint16_t buttons=0;
    std::array<std::uint8_t,8> analog{};
    std::array<std::int16_t,4> axes{};
    bool operator==(const GamepadState&) const = default;
};
class Input {
public:
    explicit Input(bool keyboard=true,bool pump_events=true,std::uint32_t device_id=0);
    ~Input();
    Input(const Input&)=delete;
    Input& operator=(const Input&)=delete;
    void event(const SDL_Event&);
    void update();
    void keyboard(bool enabled) {keyboard_=enabled;updated_=0;}
    void capture_keyboard(bool captured) {capture_keyboard_=captured;}
    std::uint32_t connected() const;
    const GamepadState& state(unsigned port) const;
    bool has_rumble(unsigned port) const;
    std::uint32_t rumble(unsigned port,std::uint16_t low,std::uint16_t high);
    bool quit_requested=false;
private:
    void refresh();
    bool keyboard_,pump_events_;
    bool capture_keyboard_=false;
    std::array<SDL_Gamepad*,4> devices_{};
    std::array<GamepadState,4> states_{};
    std::uint64_t updated_=0;
    std::uint32_t device_id_=0;
};
std::string check_input();
}
