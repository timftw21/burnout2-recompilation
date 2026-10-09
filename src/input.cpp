#include "input.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <stdexcept>
#include <format>

namespace b2 {
Input::Input(bool keyboard,bool pump_events,std::uint32_t device_id):keyboard_(keyboard),pump_events_(pump_events),device_id_(device_id) {
    if(!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) throw std::runtime_error(SDL_GetError());
    try {refresh();update();} catch(...) {
        for(auto device:devices_) if(device) SDL_CloseGamepad(device);
        SDL_QuitSubSystem(SDL_INIT_GAMEPAD);throw;
    }
}
Input::~Input() {
    for(auto device:devices_) if(device) SDL_CloseGamepad(device);
    SDL_QuitSubSystem(SDL_INIT_GAMEPAD);
}
void Input::refresh() {
    for(auto& device:devices_) if(device && !SDL_GamepadConnected(device)) {SDL_CloseGamepad(device);device=nullptr;}
    int count=0;auto* ids=SDL_GetGamepads(&count);
    if(!ids) throw std::runtime_error(SDL_GetError());
    for(int i=0;i<count;++i) {
        if(device_id_ && ids[i]!=device_id_) continue;
        if(std::ranges::any_of(devices_,[&](auto device){return device && SDL_GetGamepadID(device)==ids[i];})) continue;
        const auto slot=std::ranges::find(devices_,nullptr);
        if(slot==devices_.end()) break;
        *slot=SDL_OpenGamepad(ids[i]);
        if(!*slot) {SDL_free(ids);throw std::runtime_error(SDL_GetError());}
    }
    SDL_free(ids);
}
void Input::event(const SDL_Event& event) {
    if(event.type==SDL_EVENT_GAMEPAD_ADDED || event.type==SDL_EVENT_GAMEPAD_REMOVED) refresh();
    if(event.type==SDL_EVENT_QUIT || event.type==SDL_EVENT_WINDOW_CLOSE_REQUESTED) quit_requested=true;
}
void Input::update() {
    const auto now=SDL_GetTicksNS();
    if(updated_ && now-updated_<8000000) return;
    updated_=now;
    if(pump_events_) {SDL_Event value;while(SDL_PollEvent(&value)) event(value);}
    const auto* keys=SDL_GetKeyboardState(nullptr);
    for(unsigned port=0;port<4;++port) {
        GamepadState next;next.packet=states_[port].packet;
        if(auto* device=devices_[port];device && SDL_GamepadConnected(device)) {
            constexpr SDL_GamepadButton digital[]={SDL_GAMEPAD_BUTTON_DPAD_UP,SDL_GAMEPAD_BUTTON_DPAD_DOWN,
                SDL_GAMEPAD_BUTTON_DPAD_LEFT,SDL_GAMEPAD_BUTTON_DPAD_RIGHT,SDL_GAMEPAD_BUTTON_START,
                SDL_GAMEPAD_BUTTON_BACK,SDL_GAMEPAD_BUTTON_LEFT_STICK,SDL_GAMEPAD_BUTTON_RIGHT_STICK};
            constexpr SDL_GamepadButton analog[]={SDL_GAMEPAD_BUTTON_SOUTH,SDL_GAMEPAD_BUTTON_EAST,
                SDL_GAMEPAD_BUTTON_WEST,SDL_GAMEPAD_BUTTON_NORTH,SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER,SDL_GAMEPAD_BUTTON_LEFT_SHOULDER};
            for(unsigned i=0;i<8;++i) if(SDL_GetGamepadButton(device,digital[i])) next.buttons|=1U<<i;
            for(unsigned i=0;i<6;++i) next.analog[i]=SDL_GetGamepadButton(device,analog[i])?255:0;
            for(unsigned i=0;i<2;++i) next.analog[i+6]=static_cast<std::uint8_t>(
                std::max<int>(0,SDL_GetGamepadAxis(device,i?SDL_GAMEPAD_AXIS_RIGHT_TRIGGER:SDL_GAMEPAD_AXIS_LEFT_TRIGGER))*255/32767);
            constexpr SDL_GamepadAxis axes[]={SDL_GAMEPAD_AXIS_LEFTX,SDL_GAMEPAD_AXIS_LEFTY,SDL_GAMEPAD_AXIS_RIGHTX,SDL_GAMEPAD_AXIS_RIGHTY};
            for(unsigned i=0;i<4;++i) {
                const int value=SDL_GetGamepadAxis(device,axes[i]);
                next.axes[i]=static_cast<std::int16_t>(i&1?std::clamp(-value,-32768,32767):value);
            }
        }
        if(port==0 && keyboard_ && !capture_keyboard_) {
            constexpr SDL_Scancode digital[]={SDL_SCANCODE_UP,SDL_SCANCODE_DOWN,SDL_SCANCODE_LEFT,SDL_SCANCODE_RIGHT,
                SDL_SCANCODE_RETURN,SDL_SCANCODE_BACKSPACE,SDL_SCANCODE_LCTRL,SDL_SCANCODE_RCTRL};
            constexpr SDL_Scancode analog[]={SDL_SCANCODE_SPACE,SDL_SCANCODE_LSHIFT,SDL_SCANCODE_E,SDL_SCANCODE_Q,
                SDL_SCANCODE_R,SDL_SCANCODE_F,SDL_SCANCODE_S,SDL_SCANCODE_W};
            for(unsigned i=0;i<8;++i) {if(keys[digital[i]]) next.buttons|=1U<<i;if(keys[analog[i]]) next.analog[i]=255;}
            if(keys[SDL_SCANCODE_A] || keys[SDL_SCANCODE_D]) next.axes[0]=static_cast<std::int16_t>(
                (keys[SDL_SCANCODE_D]?32767:0)-(keys[SDL_SCANCODE_A]?32767:0));
        }
        if(next!=states_[port]) ++next.packet;
        states_[port]=next;
    }
}
std::uint32_t Input::connected() const {
    std::uint32_t mask=keyboard_?1:0;
    for(unsigned port=0;port<4;++port) if(devices_[port] && SDL_GamepadConnected(devices_[port])) mask|=1U<<port;
    return mask;
}
const GamepadState& Input::state(unsigned port) const {return states_.at(port);}
bool Input::has_rumble(unsigned port) const {
    return port<4 && devices_[port] && SDL_GetBooleanProperty(SDL_GetGamepadProperties(devices_[port]),
        SDL_PROP_GAMEPAD_CAP_RUMBLE_BOOLEAN,false);
}
std::uint32_t Input::rumble(unsigned port,std::uint16_t low,std::uint16_t high) {
    if(port>=4 || !(connected()&(1U<<port))) return 1167;
    if(!has_rumble(port)) return 50;
    return SDL_RumbleGamepad(devices_[port],low,high,1000)?0:31;
}
std::string check_input() {
    struct VirtualDevice {
        SDL_JoystickID id=0;SDL_Joystick* joystick=nullptr;
        std::uint16_t low=0,high=0;
        VirtualDevice() {
            if(!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) throw std::runtime_error(SDL_GetError());
            SDL_VirtualJoystickDesc description{};SDL_INIT_INTERFACE(&description);
            description.type=SDL_JOYSTICK_TYPE_GAMEPAD;description.naxes=SDL_GAMEPAD_AXIS_COUNT;
            description.nbuttons=SDL_GAMEPAD_BUTTON_COUNT;description.axis_mask=(1U<<SDL_GAMEPAD_AXIS_COUNT)-1;
            description.button_mask=(1U<<SDL_GAMEPAD_BUTTON_COUNT)-1;description.name="Burnout 2 offline input diagnostic";
            description.userdata=this;
            description.Rumble=[](void* context,Uint16 low,Uint16 high) {
                auto& device=*static_cast<VirtualDevice*>(context);device.low=low;device.high=high;return true;
            };
            id=SDL_AttachVirtualJoystick(&description);
            if(id) joystick=SDL_OpenJoystick(id);
            if(!joystick) {
                const std::string error=SDL_GetError();if(id) SDL_DetachVirtualJoystick(id);
                SDL_QuitSubSystem(SDL_INIT_GAMEPAD);throw std::runtime_error(error);
            }
        }
        void detach() {if(joystick) {SDL_CloseJoystick(joystick);joystick=nullptr;}if(id) {SDL_DetachVirtualJoystick(id);id=0;}}
        ~VirtualDevice() {detach();SDL_QuitSubSystem(SDL_INIT_GAMEPAD);}
        void axis(unsigned index,Sint16 value) {if(!SDL_SetJoystickVirtualAxis(joystick,index,value)) throw std::runtime_error(SDL_GetError());}
        void button(SDL_GamepadButton button,bool value) {if(!SDL_SetJoystickVirtualButton(joystick,button,value)) throw std::runtime_error(SDL_GetError());}
    } device;
    device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,-32768);device.axis(SDL_GAMEPAD_AXIS_RIGHT_TRIGGER,-32768);
    Input input(false,true,device.id);
    unsigned cases=0;
    const auto expect=[&](bool valid,std::string_view name) {if(!valid) throw std::runtime_error("SDL3 input mismatch: "+std::string(name));++cases;};
    const auto sample=[&]() -> const GamepadState& {SDL_Delay(9);input.update();return input.state(0);};
    expect(input.connected()==1,"virtual device connection");
    constexpr SDL_GamepadButton digital[]={SDL_GAMEPAD_BUTTON_DPAD_UP,SDL_GAMEPAD_BUTTON_DPAD_DOWN,
        SDL_GAMEPAD_BUTTON_DPAD_LEFT,SDL_GAMEPAD_BUTTON_DPAD_RIGHT,SDL_GAMEPAD_BUTTON_START,
        SDL_GAMEPAD_BUTTON_BACK,SDL_GAMEPAD_BUTTON_LEFT_STICK,SDL_GAMEPAD_BUTTON_RIGHT_STICK};
    constexpr SDL_GamepadButton analog[]={SDL_GAMEPAD_BUTTON_SOUTH,SDL_GAMEPAD_BUTTON_EAST,
        SDL_GAMEPAD_BUTTON_WEST,SDL_GAMEPAD_BUTTON_NORTH,SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER,SDL_GAMEPAD_BUTTON_LEFT_SHOULDER};
    for(unsigned i=0;i<8;++i) {
        device.button(digital[i],true);expect(sample().buttons==(1U<<i),"digital button");
        device.button(digital[i],false);expect(sample().buttons==0,"digital release");
    }
    for(unsigned i=0;i<6;++i) {
        device.button(analog[i],true);expect(sample().analog[i]==255,"analog button");
        device.button(analog[i],false);expect(sample().analog[i]==0,"analog release");
    }
    for(unsigned i=0;i<4;++i) for(const Sint16 value:{Sint16(-32768),Sint16(32767)}) {
        device.axis(i,value);const auto expected=i&1?std::clamp(-int(value),-32768,32767):int(value);
        expect(sample().axes[i]==expected,"stick direction and range");
        device.axis(i,0);sample();
    }
    for(unsigned i=0;i<2;++i) {
        device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER+i,32767);expect(sample().analog[i+6]==255,"trigger range");
        device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER+i,-32768);expect(sample().analog[i+6]==0,"trigger release");
    }
    const auto packet=input.state(0).packet;expect(sample().packet==packet,"unchanged packet number");
    device.button(SDL_GAMEPAD_BUTTON_SOUTH,true);expect(sample().packet==packet+1,"changed packet number");
    expect(input.has_rumble(0),"rumble capability");
    expect(input.rumble(0,12345,54321)==0 && device.low==12345 && device.high==54321,"rumble delivery");
    device.detach();sample();expect(input.connected()==0,"hot removal");
    expect(input.rumble(0,0,0)==1167,"disconnected rumble status");
    return std::format("{{\"format\":\"b2-input-check-v1\",\"backend\":\"SDL3\",\"passed_cases\":{}}}",cases);
}
}
