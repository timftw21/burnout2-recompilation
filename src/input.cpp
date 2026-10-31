#include "input.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <stdexcept>
#include <format>
#include <climits>

namespace b2 {
KeyboardBindings default_keyboard_bindings() {
    return {SDL_SCANCODE_UP,SDL_SCANCODE_DOWN,SDL_SCANCODE_LEFT,SDL_SCANCODE_RIGHT,
        SDL_SCANCODE_RETURN,SDL_SCANCODE_BACKSPACE,SDL_SCANCODE_LCTRL,SDL_SCANCODE_RCTRL,
        SDL_SCANCODE_SPACE,SDL_SCANCODE_LSHIFT,SDL_SCANCODE_E,SDL_SCANCODE_Q,
        SDL_SCANCODE_R,SDL_SCANCODE_F,SDL_SCANCODE_S,SDL_SCANCODE_W,
        SDL_SCANCODE_A,SDL_SCANCODE_D,0,0,0,0,0,0};
}
bool valid_keyboard_binding(int key) {
    return key>=0 && key<SDL_SCANCODE_COUNT && key!=SDL_SCANCODE_ESCAPE && key!=SDL_SCANCODE_F1 && key!=SDL_SCANCODE_F8 && key!=SDL_SCANCODE_F9;
}
void apply_keyboard(GamepadState& state,const KeyboardBindings& bindings,std::span<const bool> keys) {
    const auto held=[&](unsigned action) {const auto key=bindings[action];return key>0 && std::size_t(key)<keys.size() && keys[key];};
    for(unsigned i=0;i<8;++i) {if(held(i)) state.buttons|=1U<<i;if(held(i+8)) state.analog[i]=255;}
    for(unsigned axis=0;axis<4;++axis) {
        const auto negative=held(16+axis*2),positive=held(17+axis*2);
        if(negative || positive) state.axes[axis]=static_cast<std::int16_t>((positive?32767:0)-(negative?32767:0));
    }
}
ControllerBindings default_controller_bindings() {
    using enum ControllerSource;
    return {{{button,SDL_GAMEPAD_BUTTON_DPAD_UP},{button,SDL_GAMEPAD_BUTTON_DPAD_DOWN},
        {button,SDL_GAMEPAD_BUTTON_DPAD_LEFT},{button,SDL_GAMEPAD_BUTTON_DPAD_RIGHT},
        {button,SDL_GAMEPAD_BUTTON_START},{button,SDL_GAMEPAD_BUTTON_BACK},
        {button,SDL_GAMEPAD_BUTTON_LEFT_STICK},{button,SDL_GAMEPAD_BUTTON_RIGHT_STICK},
        {button,SDL_GAMEPAD_BUTTON_SOUTH},{button,SDL_GAMEPAD_BUTTON_EAST},
        {button,SDL_GAMEPAD_BUTTON_WEST},{button,SDL_GAMEPAD_BUTTON_NORTH},
        {button,SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER},{button,SDL_GAMEPAD_BUTTON_LEFT_SHOULDER},
        {positive_axis,SDL_GAMEPAD_AXIS_LEFT_TRIGGER},{positive_axis,SDL_GAMEPAD_AXIS_RIGHT_TRIGGER},
        {negative_axis,SDL_GAMEPAD_AXIS_LEFTX},{positive_axis,SDL_GAMEPAD_AXIS_LEFTX},
        {positive_axis,SDL_GAMEPAD_AXIS_LEFTY},{negative_axis,SDL_GAMEPAD_AXIS_LEFTY},
        {negative_axis,SDL_GAMEPAD_AXIS_RIGHTX},{positive_axis,SDL_GAMEPAD_AXIS_RIGHTX},
        {positive_axis,SDL_GAMEPAD_AXIS_RIGHTY},{negative_axis,SDL_GAMEPAD_AXIS_RIGHTY}}};
}
ControllerProfiles default_controller_profiles() {ControllerProfiles result;result.fill(default_controller_bindings());return result;}
bool valid_controller_binding(ControllerBinding binding) {
    switch(binding.source) {
    case ControllerSource::none:return binding.control==0;
    case ControllerSource::button:return binding.control<SDL_GAMEPAD_BUTTON_COUNT;
    case ControllerSource::negative_axis:return binding.control<SDL_GAMEPAD_AXIS_LEFT_TRIGGER;
    case ControllerSource::positive_axis:return binding.control<SDL_GAMEPAD_AXIS_COUNT;
    }
    return false;
}
std::string controller_binding_id(ControllerBinding binding) {
    if(!valid_controller_binding(binding)) throw std::runtime_error("Invalid controller binding");
    if(binding.source==ControllerSource::none) return "none";
    if(binding.source==ControllerSource::button) return SDL_GetGamepadStringForButton(SDL_GamepadButton(binding.control));
    return std::string(binding.source==ControllerSource::negative_axis?"-":"+")+SDL_GetGamepadStringForAxis(SDL_GamepadAxis(binding.control));
}
std::optional<ControllerBinding> parse_controller_binding(std::string_view value) {
    if(value=="none") return ControllerBinding{};
    if(value.empty() || value.size()>32) return {};
    ControllerBinding binding;
    if(value.front()=='+' || value.front()=='-') {
        const auto axis=SDL_GetGamepadAxisFromString(std::string(value.substr(1)).c_str());
        if(axis==SDL_GAMEPAD_AXIS_INVALID) return {};
        binding={value.front()=='-'?ControllerSource::negative_axis:ControllerSource::positive_axis,std::uint8_t(axis)};
    } else {
        const auto button=SDL_GetGamepadButtonFromString(std::string(value).c_str());
        if(button==SDL_GAMEPAD_BUTTON_INVALID) return {};
        binding={ControllerSource::button,std::uint8_t(button)};
    }
    return valid_controller_binding(binding)?std::optional{binding}:std::nullopt;
}
Input::Input(bool keyboard,bool pump_events,std::uint32_t device_id)
    :Input(keyboard,pump_events,std::span(&device_id,device_id?1:0)) {}
Input::Input(bool keyboard,bool pump_events,std::span<const std::uint32_t> device_ids)
    :keyboard_(keyboard),pump_events_(pump_events),filtered_(!device_ids.empty()) {
    if(device_ids.size()>device_ids_.size()) throw std::runtime_error("Input supports at most four devices");
    std::ranges::copy(device_ids,device_ids_.begin());
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
    for(unsigned port=0;port<devices_.size();++port) if(auto& device=devices_[port];device && !SDL_GamepadConnected(device)) {
        SDL_CloseGamepad(device);device=nullptr;rumble_[port]={};
    }
    int count=0;auto* ids=SDL_GetGamepads(&count);
    if(!ids) throw std::runtime_error(SDL_GetError());
    for(int i=0;i<count;++i) {
        const auto selected=std::ranges::find(device_ids_,ids[i]);
        if(filtered_ && selected==device_ids_.end()) continue;
        if(std::ranges::any_of(devices_,[&](auto device){return device && SDL_GetGamepadID(device)==ids[i];})) continue;
        const auto slot=filtered_?devices_.begin()+(selected-device_ids_.begin()):std::ranges::find(devices_,nullptr);
        if(slot==devices_.end()) break;
        *slot=SDL_OpenGamepad(ids[i]);
        if(!*slot) {SDL_free(ids);throw std::runtime_error(SDL_GetError());}
    }
    SDL_free(ids);
    updated_=0;
    if(capture_.state!=ControllerCapture::idle && controller_id(capture_.port)!=capture_.device) capture_={};
}
void Input::event(const SDL_Event& event) {
    if(event.type==SDL_EVENT_GAMEPAD_ADDED || event.type==SDL_EVENT_GAMEPAD_REMOVED) refresh();
    if(event.type==SDL_EVENT_QUIT || event.type==SDL_EVENT_WINDOW_CLOSE_REQUESTED) quit_requested=true;
}
void Input::update(bool force) {
    const auto now=SDL_GetTicksNS();
    if(!force && updated_ && now-updated_<8000000) return;
    updated_=now;
    if(pump_events_) {SDL_Event value;while(SDL_PollEvent(&value)) event(value);}
    if(capture_.state!=ControllerCapture::idle && controller_id(capture_.port)!=capture_.device) capture_={};
    int key_count=0;const auto* keys=SDL_GetKeyboardState(&key_count);
    for(unsigned port=0;port<4;++port) {
        GamepadState next;next.packet=states_[port].packet;
        if(auto* device=devices_[port];device && SDL_GamepadConnected(device)) {
            auto& feedback=rumble_[port];
            if(feedback.renew && now>=feedback.renew) {
                if(SDL_RumbleGamepad(device,feedback.low,feedback.high,65535)) feedback.renew=now+30000000000ULL;
                else feedback={};
            }
            std::array<bool,SDL_GAMEPAD_BUTTON_COUNT> buttons{};
            std::array<int,SDL_GAMEPAD_AXIS_COUNT> axes{};
            for(unsigned i=0;i<buttons.size();++i) buttons[i]=SDL_GetGamepadButton(device,SDL_GamepadButton(i));
            for(unsigned i=0;i<axes.size();++i) axes[i]=SDL_GetGamepadAxis(device,SDL_GamepadAxis(i));
            const bool capturing=capture_.state!=ControllerCapture::idle && capture_.port==port;
            if(capturing) {
                bool neutral=std::ranges::none_of(buttons,[](bool held){return held;});
                for(unsigned i=0;i<axes.size();++i) neutral&=std::abs(axes[i])<=(i<4?8192:4096);
                if(neutral) {
                    if(capture_.state==ControllerCapture::neutral) capture_.state=ControllerCapture::listening;
                    else if(capture_.state==ControllerCapture::release) capture_.state=ControllerCapture::idle;
                } else if(capture_.state==ControllerCapture::listening) {
                    ControllerBinding binding;
                    const auto button=std::ranges::find(buttons,true);
                    if(button!=buttons.end()) binding={ControllerSource::button,std::uint8_t(button-buttons.begin())};
                    else {
                        const auto axis=std::ranges::max_element(axes,[](int a,int b){return std::abs(a)<std::abs(b);});
                        if(std::abs(*axis)>=16384) binding={*axis<0?ControllerSource::negative_axis:ControllerSource::positive_axis,std::uint8_t(axis-axes.begin())};
                    }
                    if(binding.source!=ControllerSource::none) {capture_.result=binding;capture_.state=ControllerCapture::release;}
                }
            } else {
                const auto value=[&](unsigned action) {
                    const auto binding=controller_bindings_[port][action];
                    if(binding.source==ControllerSource::button) return buttons[binding.control]?32767:0;
                    if(binding.source==ControllerSource::negative_axis) return std::max(0,-axes[binding.control]);
                    if(binding.source==ControllerSource::positive_axis) return std::max(0,axes[binding.control]);
                    return 0;
                };
                for(unsigned i=0;i<8;++i) {
                    if(value(i)>=16384) next.buttons|=1U<<i;
                    next.analog[i]=static_cast<std::uint8_t>(std::min(value(i+8),32767)*255/32767);
                }
                for(unsigned i=0;i<4;++i) next.axes[i]=static_cast<std::int16_t>(std::clamp(value(17+i*2)-value(16+i*2),-32768,32767));
            }
        }
        if(port==0 && keyboard_ && !capture_keyboard_) {
            apply_keyboard(next,bindings_,{keys,static_cast<std::size_t>(key_count)});
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
void Input::controller_bindings(const ControllerProfiles& bindings) {
    for(const auto& profile:bindings) if(!std::ranges::all_of(profile,valid_controller_binding)) throw std::runtime_error("Invalid controller bindings");
    controller_bindings_=bindings;updated_=0;
}
std::uint32_t Input::controller_id(unsigned port) const {
    return port<devices_.size() && devices_[port] && SDL_GamepadConnected(devices_[port])?SDL_GetGamepadID(devices_[port]):0;
}
const char* Input::controller_name(unsigned port) const {
    if(!controller_id(port)) return "Not connected";
    const auto* name=SDL_GetGamepadName(devices_[port]);return name?name:"Controller";
}
std::string Input::controller_binding_name(unsigned port,ControllerBinding binding) const {
    if(!valid_controller_binding(binding) || binding.source==ControllerSource::none) return "Unbound";
    if(binding.source!=ControllerSource::button) {
        constexpr const char* names[][2]={{"Left stick left","Left stick right"},{"Left stick up","Left stick down"},
            {"Right stick left","Right stick right"},{"Right stick up","Right stick down"},{"Left trigger","Left trigger"},{"Right trigger","Right trigger"}};
        return names[binding.control][binding.source==ControllerSource::positive_axis];
    }
    const auto button=SDL_GamepadButton(binding.control);
    const auto label=controller_id(port)?SDL_GetGamepadButtonLabel(devices_[port],button):SDL_GetGamepadButtonLabelForType(SDL_GAMEPAD_TYPE_XBOXONE,button);
    constexpr const char* labels[]={"","A","B","X","Y","Cross","Circle","Square","Triangle"};
    if(label>SDL_GAMEPAD_BUTTON_LABEL_UNKNOWN && unsigned(label)<std::size(labels)) return labels[label];
    constexpr const char* names[]={"South button","East button","West button","North button","Back / View","Guide","Start / Menu",
        "Left stick click","Right stick click","Left bumper","Right bumper","D-pad up","D-pad down","D-pad left","D-pad right",
        "Extra button 1","Right paddle 1","Left paddle 1","Right paddle 2","Left paddle 2","Touchpad click",
        "Extra button 2","Extra button 3","Extra button 4","Extra button 5","Extra button 6"};
    static_assert(std::size(names)==SDL_GAMEPAD_BUTTON_COUNT);return names[binding.control];
}
bool Input::start_binding(unsigned port) {
    const auto id=controller_id(port);if(!id) return false;
    capture_={ControllerCapture::neutral,port,id,{}};updated_=0;return true;
}
void Input::cancel_binding() {
    capture_.result.reset();if(capture_.state!=ControllerCapture::idle) capture_.state=ControllerCapture::release;updated_=0;
}
std::optional<ControllerBinding> Input::take_binding() {auto result=capture_.result;capture_.result.reset();return result;}
bool Input::has_rumble(unsigned port) const {
    return port<4 && devices_[port] && SDL_GetBooleanProperty(SDL_GetGamepadProperties(devices_[port]),
        SDL_PROP_GAMEPAD_CAP_RUMBLE_BOOLEAN,false);
}
std::uint32_t Input::rumble(unsigned port,std::uint16_t low,std::uint16_t high) {
    if(port>=4 || !(connected()&(1U<<port))) return 1167;
    if(!has_rumble(port)) return 50;
    // Xbox feedback is a persistent motor state. SDL limits each effect to
    // 65535 ms, so renew it until the title supplies a replacement or stop.
    if(!SDL_RumbleGamepad(devices_[port],low,high,65535)) return 31;
    rumble_[port]={low,high,(low || high)?SDL_GetTicksNS()+30000000000ULL:0};return 0;
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
    std::array<bool,SDL_SCANCODE_COUNT> keys{};
    auto bindings=default_keyboard_bindings();GamepadState keyboard;
    keys[SDL_SCANCODE_W]=true;apply_keyboard(keyboard,bindings,keys);expect(keyboard.analog[7]==255,"default accelerate binding");
    bindings[15]=SDL_SCANCODE_Z;keyboard={};apply_keyboard(keyboard,bindings,keys);expect(keyboard.analog[7]==0,"old binding released");
    keys[SDL_SCANCODE_Z]=true;apply_keyboard(keyboard,bindings,keys);expect(keyboard.analog[7]==255,"custom accelerate binding");
    bindings[15]=0;keyboard={};apply_keyboard(keyboard,bindings,keys);expect(keyboard.analog[7]==0,"unbound action");
    bindings[20]=SDL_SCANCODE_J;bindings[21]=SDL_SCANCODE_L;keys[SDL_SCANCODE_J]=true;keyboard={};keyboard.analog[0]=123;
    apply_keyboard(keyboard,bindings,keys);expect(keyboard.axes[2]==-32767 && keyboard.analog[0]==123,"custom stick and gamepad mixing");
    keys[SDL_SCANCODE_L]=true;apply_keyboard(keyboard,bindings,keys);expect(keyboard.axes[2]==0,"opposite directions cancel");
    bindings[0]=INT_MAX;apply_keyboard(keyboard,bindings,keys);expect(keyboard.buttons==0,"out of range binding is inert");
    expect(!valid_keyboard_binding(SDL_SCANCODE_ESCAPE) && !valid_keyboard_binding(SDL_SCANCODE_F8) && !valid_keyboard_binding(SDL_SCANCODE_F9) && valid_keyboard_binding(0),"reserved shortcuts");
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
    using enum ControllerSource;
    auto profiles=default_controller_profiles();
    for(const auto binding:profiles[0]) expect(parse_controller_binding(controller_binding_id(binding))==binding,"controller binding serialization");
    expect(parse_controller_binding("none")==ControllerBinding{} && !parse_controller_binding("-lefttrigger") &&
        !parse_controller_binding("+missing") && !parse_controller_binding("999999"),"invalid controller binding rejected");
    profiles[0][8]={button,SDL_GAMEPAD_BUTTON_WEST};input.controller_bindings(profiles);
    device.button(SDL_GAMEPAD_BUTTON_SOUTH,true);expect(sample().analog[0]==0,"old controller button released");
    device.button(SDL_GAMEPAD_BUTTON_SOUTH,false);device.button(SDL_GAMEPAD_BUTTON_WEST,true);
    expect(sample().analog[0]==255,"remapped controller button");
    profiles[0][8]={};input.controller_bindings(profiles);expect(sample().analog[0]==0,"cleared controller binding");
    device.button(SDL_GAMEPAD_BUTTON_WEST,false);
    profiles[0][14]={positive_axis,SDL_GAMEPAD_AXIS_RIGHT_TRIGGER};profiles[0][15]={positive_axis,SDL_GAMEPAD_AXIS_LEFT_TRIGGER};input.controller_bindings(profiles);
    device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,0);expect(sample().analog[7]==127 && input.state(0).analog[6]==0,"remapped trigger keeps analog pressure");
    device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,32767);expect(sample().analog[7]==255,"remapped trigger reaches full pressure");
    device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,-32768);sample();
    profiles[0][16]={positive_axis,SDL_GAMEPAD_AXIS_LEFTX};profiles[0][17]={negative_axis,SDL_GAMEPAD_AXIS_LEFTX};input.controller_bindings(profiles);
    device.axis(SDL_GAMEPAD_AXIS_LEFTX,12345);expect(sample().axes[0]==-12345,"inverted stick preserves magnitude");
    device.axis(SDL_GAMEPAD_AXIS_LEFTX,-32768);expect(sample().axes[0]==32767,"inverted stick clamps asymmetric endpoint");
    device.axis(SDL_GAMEPAD_AXIS_LEFTX,0);
    profiles[0][16]={button,SDL_GAMEPAD_BUTTON_LEFT_SHOULDER};profiles[0][17]={button,SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER};input.controller_bindings(profiles);
    device.button(SDL_GAMEPAD_BUTTON_LEFT_SHOULDER,true);expect(sample().axes[0]==-32767,"button steering");
    device.button(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER,true);expect(sample().axes[0]==0,"opposite controller directions cancel");
    device.button(SDL_GAMEPAD_BUTTON_LEFT_SHOULDER,false);device.button(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER,false);
    profiles=default_controller_profiles();profiles[0][4]={positive_axis,SDL_GAMEPAD_AXIS_LEFT_TRIGGER};input.controller_bindings(profiles);
    device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,0);expect((sample().buttons&16)==0,"axis to button below threshold");
    device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,1);expect((sample().buttons&16)!=0,"axis to button above threshold");
    device.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,-32768);profiles=default_controller_profiles();input.controller_bindings(profiles);sample();
    device.button(SDL_GAMEPAD_BUTTON_SOUTH,true);sample();expect(input.start_binding(0),"start controller capture");
    sample();expect(input.capture_state()==ControllerCapture::neutral && !input.take_binding() && input.state(0).analog[0]==0,"activation press cannot bind or reach gameplay");
    device.button(SDL_GAMEPAD_BUTTON_SOUTH,false);sample();expect(input.capture_state()==ControllerCapture::listening,"release arms controller capture");
    device.axis(SDL_GAMEPAD_AXIS_LEFTX,1000);sample();expect(!input.take_binding(),"stick drift cannot bind");device.axis(SDL_GAMEPAD_AXIS_LEFTX,0);
    device.axis(SDL_GAMEPAD_AXIS_RIGHT_TRIGGER,1);sample();
    expect(input.take_binding()==ControllerBinding{positive_axis,SDL_GAMEPAD_AXIS_RIGHT_TRIGGER} && input.state(0).analog[7]==0,"trigger capture suppresses gameplay");
    sample();expect(input.capture_state()==ControllerCapture::release && !input.take_binding(),"captured control waits for release");
    device.axis(SDL_GAMEPAD_AXIS_RIGHT_TRIGGER,-32768);sample();expect(input.capture_state()==ControllerCapture::idle,"release finishes capture");
    input.start_binding(0);sample();input.cancel_binding();device.button(SDL_GAMEPAD_BUTTON_EAST,true);sample();
    expect(!input.take_binding() && input.state(0).analog[1]==0,"canceled capture consumes held control");
    device.button(SDL_GAMEPAD_BUTTON_EAST,false);sample();expect(input.capture_state()==ControllerCapture::idle,"cancel release recovers capture");
    {
        VirtualDevice second;second.axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,-32768);second.axis(SDL_GAMEPAD_AXIS_RIGHT_TRIGGER,-32768);
        const std::array ids{device.id,second.id};Input players(false,true,ids);
        profiles[1][15]={button,SDL_GAMEPAD_BUTTON_WEST};players.controller_bindings(profiles);
        const auto tick=[&] {SDL_Delay(9);players.update(true);};
        second.button(SDL_GAMEPAD_BUTTON_WEST,true);tick();
        expect(players.state(0).analog[7]==0 && players.state(1).analog[7]==255,"controller profiles are independent per player");
        players.start_binding(0);tick();expect(players.capture_state()==ControllerCapture::listening && !players.take_binding(),"other controller cannot supply binding");
        second.button(SDL_GAMEPAD_BUTTON_WEST,false);device.button(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER,true);tick();
        expect(players.take_binding()==ControllerBinding{button,SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER},"selected controller supplies binding");
        device.button(SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER,false);tick();players.start_binding(1);tick();second.detach();tick();
        expect(players.capture_state()==ControllerCapture::idle && players.connected()==1,"unplug cancels capture without losing other player");
        expect(!players.start_binding(1),"disconnected controller cannot capture");
    }
    sample();
    const auto packet=input.state(0).packet;expect(sample().packet==packet,"unchanged packet number");
    device.button(SDL_GAMEPAD_BUTTON_SOUTH,true);expect(sample().packet==packet+1,"changed packet number");
    expect(input.has_rumble(0),"rumble capability");
    expect(input.rumble(0,12345,54321)==0 && device.low==12345 && device.high==54321,"rumble delivery");
    SDL_Delay(1100);sample();expect(device.low==12345 && device.high==54321,"motor state survives without repeated game requests");
    expect(input.rumble(0,0,0)==0 && device.low==0 && device.high==0,"explicit motor stop");
    device.detach();sample();expect(input.connected()==0,"hot removal");
    expect(input.rumble(0,0,0)==1167,"disconnected rumble status");
    return std::format("{{\"format\":\"b2-input-check-v2\",\"backend\":\"SDL3\",\"controller_remapping\":true,\"controller_capture\":true,\"per_player_bindings\":true,\"passed_cases\":{}}}",cases);
}
}
