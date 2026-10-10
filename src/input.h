#pragma once
#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <span>

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
struct KeyboardAction {std::string_view id,label;};
inline constexpr std::array<KeyboardAction,24> keyboard_actions{{
    {"up","Menu up"},{"down","Menu down"},{"left","Menu left"},{"right","Menu right"},
    {"start","Start / Pause"},{"back","Back"},{"left_click","Left stick click"},{"right_click","Right stick click"},
    {"a","A / Confirm"},{"b","B / Back"},{"x","X"},{"y","Y"},{"black","Black"},{"white","White"},
    {"brake","Brake / Left trigger"},{"accelerate","Accelerate / Right trigger"},
    {"steer_left","Steer left"},{"steer_right","Steer right"},{"stick_down","Left stick down"},{"stick_up","Left stick up"},
    {"look_left","Right stick left"},{"look_right","Right stick right"},{"look_down","Right stick down"},{"look_up","Right stick up"}}};
using KeyboardBindings=std::array<int,keyboard_actions.size()>;
KeyboardBindings default_keyboard_bindings();
bool valid_keyboard_binding(int);
void apply_keyboard(GamepadState&,const KeyboardBindings&,std::span<const bool> keys);
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
    void bindings(const KeyboardBindings& value) {bindings_=value;updated_=0;}
    std::uint32_t connected() const;
    const GamepadState& state(unsigned port) const;
    bool has_rumble(unsigned port) const;
    std::uint32_t rumble(unsigned port,std::uint16_t low,std::uint16_t high);
    bool quit_requested=false;
private:
    void refresh();
    bool keyboard_,pump_events_;
    bool capture_keyboard_=false;
    KeyboardBindings bindings_=default_keyboard_bindings();
    std::array<SDL_Gamepad*,4> devices_{};
    std::array<GamepadState,4> states_{};
    std::uint64_t updated_=0;
    std::uint32_t device_id_=0;
};
std::string check_input();
}
