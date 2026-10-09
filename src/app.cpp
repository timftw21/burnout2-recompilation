#include "app.h"
#include "input.h"
#include "file.h"
#include "xbox.h"
#include <SDL3/SDL.h>
#include <imgui.h>
#include <imgui_impl_sdl3.h>
#include <imgui_impl_dx11.h>
#include <Windows.h>
#include <commdlg.h>
#include <algorithm>
#include <fstream>
#include <iostream>
#include <sstream>

namespace b2 {
namespace {
// Explicit live diagnostics use their own SDL controller. Physical controllers
// and global keyboard state are never changed by a script.
struct ControlScript {
    struct Event {std::uint64_t at;int control,value;bool axis;};
    std::vector<Event> events;
    SDL_JoystickID id=0;SDL_Joystick* joystick=nullptr;
    std::size_t next=0;std::uint64_t started=0;
    explicit ControlScript(const std::filesystem::path& path) {
        File file(path);if(file.size()>16384) throw std::runtime_error("Control script exceeds 16 KiB");
        const auto bytes=file.read(0,static_cast<std::size_t>(file.size()));
        std::istringstream lines(std::string(reinterpret_cast<const char*>(bytes.data()),bytes.size()));std::string line;
        while(std::getline(lines,line)) {
            std::istringstream fields(line.substr(0,line.find('#')));std::string name,extra;std::uint64_t milliseconds;int value;
            fields>>std::ws;if(fields.eof()) continue;
            if(!(fields>>milliseconds>>name>>value) || fields>>extra || milliseconds>3600000 || events.size()==128)
                throw std::runtime_error("Controls require ordered milliseconds, an SDL button/axis name and a value");
            const auto axis=SDL_GetGamepadAxisFromString(name.c_str());const auto button=SDL_GetGamepadButtonFromString(name.c_str());
            if((axis==SDL_GAMEPAD_AXIS_INVALID && button==SDL_GAMEPAD_BUTTON_INVALID) ||
                (axis==SDL_GAMEPAD_AXIS_INVALID?(value!=0 && value!=1):(value<-32768 || value>32767)))
                throw std::runtime_error("Invalid controller name or value in script");
            const auto at=milliseconds*1000000;if(!events.empty() && at<events.back().at) throw std::runtime_error("Controller events are out of order");
            events.push_back({at,axis!=SDL_GAMEPAD_AXIS_INVALID?int(axis):int(button),value,axis!=SDL_GAMEPAD_AXIS_INVALID});
        }
        SDL_VirtualJoystickDesc description{};SDL_INIT_INTERFACE(&description);
        description.type=SDL_JOYSTICK_TYPE_GAMEPAD;description.naxes=SDL_GAMEPAD_AXIS_COUNT;description.nbuttons=SDL_GAMEPAD_BUTTON_COUNT;
        description.axis_mask=(1U<<SDL_GAMEPAD_AXIS_COUNT)-1;description.button_mask=(1U<<SDL_GAMEPAD_BUTTON_COUNT)-1;
        description.name="Burnout 2 live diagnostic controller";id=SDL_AttachVirtualJoystick(&description);
        if(id) joystick=SDL_OpenJoystick(id);
        if(!joystick) {const std::string error=SDL_GetError();if(id) SDL_DetachVirtualJoystick(id);throw std::runtime_error(error);}
        SDL_SetJoystickVirtualAxis(joystick,SDL_GAMEPAD_AXIS_LEFT_TRIGGER,-32768);
        SDL_SetJoystickVirtualAxis(joystick,SDL_GAMEPAD_AXIS_RIGHT_TRIGGER,-32768);SDL_UpdateJoysticks();
    }
    ~ControlScript() {if(joystick) SDL_CloseJoystick(joystick);if(id) SDL_DetachVirtualJoystick(id);}
    void advance() {
        if(!started) return;const auto elapsed=SDL_GetTicksNS()-started;bool changed=false;
        while(next<events.size() && events[next].at<=elapsed) {
            const auto& event=events[next++];
            const auto valid=event.axis?SDL_SetJoystickVirtualAxis(joystick,event.control,static_cast<Sint16>(event.value)):
                SDL_SetJoystickVirtualButton(joystick,event.control,event.value!=0);
            if(!valid) throw std::runtime_error(SDL_GetError());changed=true;
        }
        if(changed) SDL_UpdateJoysticks();
    }
};
struct Preferences {
    std::array<char,4096> xbe{},disc{};
    bool vsync=true,fullscreen=false,keyboard=true;
    Preferences() {
        strcpy_s(xbe.data(),xbe.size(),"data/local/default.xbe");
        strcpy_s(disc.data(),disc.size(),"Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso");
        std::ifstream file("data/local/settings.cfg");std::string line;
        while(std::getline(file,line)) {
            if(line.size()>8192) throw std::runtime_error("Settings line exceeds its bound");
            const auto split=line.find('=');if(split==std::string::npos) continue;
            const auto key=line.substr(0,split),value=line.substr(split+1);
            if(key=="xbe" || key=="disc") {
                auto& destination=key=="xbe"?xbe:disc;
                if(value.size()<destination.size()) strcpy_s(destination.data(),destination.size(),value.c_str());
            } else if(key=="vsync") vsync=value=="1";
            else if(key=="fullscreen") fullscreen=value=="1";
            else if(key=="keyboard") keyboard=value=="1";
        }
    }
    void save() const {
        std::filesystem::create_directories("data/local");
        std::ofstream file("data/local/settings.cfg",std::ios::trunc);
        file<<"xbe="<<xbe.data()<<"\ndisc="<<disc.data()<<"\nvsync="<<vsync<<"\nfullscreen="<<fullscreen<<"\nkeyboard="<<keyboard<<'\n';
        if(!file) throw std::runtime_error("Could not save settings");
    }
};
struct App {
    SDL_Window* window=nullptr;
    std::unique_ptr<Renderer> renderer;
    std::unique_ptr<Input> input;
    std::unique_ptr<ControlScript> controls;
    Preferences settings;
    bool running=true,playing=false,show_settings=true,launch=false;
    std::string status,memory_snapshot="[]";
    std::uint64_t stop_at=0,frames=0;
    App(bool hidden,bool warp,const std::filesystem::path& script={}) {
        // Explicit scripts must work while the user keeps another app focused.
        // This process reads only its own virtual controller in script mode.
        if(!script.empty() && !SDL_SetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS,"1"))
            throw std::runtime_error("SDL background input is required for scripted controls");
        if(!SDL_Init(SDL_INIT_VIDEO|SDL_INIT_GAMEPAD|SDL_INIT_AUDIO)) throw std::runtime_error(SDL_GetError());
        try {
            window=SDL_CreateWindow("Burnout 2: Point of Impact",960,720,SDL_WINDOW_RESIZABLE|SDL_WINDOW_HIGH_PIXEL_DENSITY|(hidden?SDL_WINDOW_HIDDEN:0));
            if(!window) throw std::runtime_error(SDL_GetError());
            auto* handle=SDL_GetPointerProperty(SDL_GetWindowProperties(window),SDL_PROP_WINDOW_WIN32_HWND_POINTER,nullptr);
            renderer=std::make_unique<Renderer>(640,480,warp,hidden);renderer->attach_window(handle);
            if(!script.empty()) controls=std::make_unique<ControlScript>(script);
            input=std::make_unique<Input>(controls?false:settings.keyboard,false,controls?controls->id:0);
            IMGUI_CHECKVERSION();ImGui::CreateContext();
            auto& io=ImGui::GetIO();io.ConfigFlags|=ImGuiConfigFlags_NavEnableKeyboard;io.IniFilename=nullptr;
            ImGui::StyleColorsDark();auto& style=ImGui::GetStyle();style.WindowRounding=8;style.FrameRounding=4;style.FramePadding={10,8};
            style.Colors[ImGuiCol_Button]={0.67f,0.25f,0.08f,1};style.Colors[ImGuiCol_ButtonHovered]={0.9f,0.35f,0.1f,1};
            if(!ImGui_ImplSDL3_InitForD3D(window) || !ImGui_ImplDX11_Init(renderer->native_device(),renderer->native_context()))
                throw std::runtime_error("Could not initialize the settings interface");
            if(!hidden && settings.fullscreen && !SDL_SetWindowFullscreen(window,true)) throw std::runtime_error(SDL_GetError());
        } catch(...) {cleanup();throw;}
    }
    void cleanup() {
        if(ImGui::GetCurrentContext()) {
            if(ImGui::GetIO().BackendRendererUserData) ImGui_ImplDX11_Shutdown();
            if(ImGui::GetIO().BackendPlatformUserData) ImGui_ImplSDL3_Shutdown();
            ImGui::DestroyContext();
        }
        input.reset();controls.reset();renderer.reset();if(window) SDL_DestroyWindow(window);window=nullptr;SDL_Quit();
    }
    ~App() {cleanup();}
    bool poll() {
        SDL_Event event;
        while(SDL_PollEvent(&event)) {
            ImGui_ImplSDL3_ProcessEvent(&event);input->event(event);
            if(event.type==SDL_EVENT_QUIT || event.type==SDL_EVENT_WINDOW_CLOSE_REQUESTED) running=false;
            if(event.type==SDL_EVENT_KEY_DOWN && event.key.scancode==SDL_SCANCODE_F1 && !event.key.repeat && playing) show_settings=!show_settings;
        }
        if(stop_at && SDL_GetTicksNS()>=stop_at) running=false;
        if(controls) controls->advance();input->capture_keyboard(playing && show_settings);input->update();return running;
    }
    void browse(std::array<char,4096>& path,bool iso) {
        std::array<wchar_t,32768> result{};
        OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);
        dialog.hwndOwner=static_cast<HWND>(SDL_GetPointerProperty(SDL_GetWindowProperties(window),SDL_PROP_WINDOW_WIN32_HWND_POINTER,nullptr));
        dialog.lpstrFilter=iso?L"Xbox game disc (*.iso)\0*.iso\0\0":L"Xbox executable (*.xbe)\0*.xbe\0\0";
        dialog.lpstrFile=result.data();dialog.nMaxFile=static_cast<DWORD>(result.size());dialog.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_NOCHANGEDIR;
        if(GetOpenFileNameW(&dialog)) {const auto text=utf8(result.data());if(text.size()<path.size()) strcpy_s(path.data(),path.size(),text.c_str());}
    }
    void interface() {
        ImGui_ImplDX11_NewFrame();ImGui_ImplSDL3_NewFrame();ImGui::NewFrame();
        if(show_settings) {
            const auto* viewport=ImGui::GetMainViewport();
            ImGui::SetNextWindowPos(viewport->GetCenter(),ImGuiCond_Always,{0.5f,0.5f});
            ImGui::SetNextWindowSize({std::min(620.0f,viewport->Size.x-32),0});
            ImGui::Begin("Burnout 2",nullptr,ImGuiWindowFlags_NoCollapse|ImGuiWindowFlags_AlwaysAutoResize|ImGuiWindowFlags_NoResize);
            ImGui::TextUnformatted("POINT OF IMPACT");ImGui::Spacing();
            if(!playing) {
                ImGui::TextUnformatted("Choose your game files");
                ImGui::InputText("Executable",settings.xbe.data(),settings.xbe.size());ImGui::SameLine();if(ImGui::Button("Browse##xbe")) browse(settings.xbe,false);
                ImGui::InputText("Game disc",settings.disc.data(),settings.disc.size());ImGui::SameLine();if(ImGui::Button("Browse##disc")) browse(settings.disc,true);
            }
            ImGui::SeparatorText("Graphics");
            ImGui::Checkbox("V-sync",&settings.vsync);
            if(ImGui::Checkbox("Fullscreen",&settings.fullscreen) && !SDL_SetWindowFullscreen(window,settings.fullscreen)) status=SDL_GetError();
            ImGui::TextUnformatted("Native rendering: 640 x 480");
            ImGui::SeparatorText("Controls");
            if(ImGui::Checkbox("Keyboard controller",&settings.keyboard)) input->keyboard(settings.keyboard);
            ImGui::TextWrapped("Arrows: menu | Enter: Start | Space: A | W: accelerate | S: brake | A/D: steer");
            ImGui::TextUnformatted("Connected gamepads work automatically. F1 opens settings.");
            if(!status.empty()) {ImGui::Separator();ImGui::TextWrapped("%s",status.c_str());}
            ImGui::Spacing();
            if(!playing && ImGui::Button("Start game",{160,0})) {settings.save();launch=true;}
            if(playing && ImGui::Button("Return to game",{160,0})) show_settings=false;
            ImGui::SameLine();if(ImGui::Button("Save settings")) {settings.save();status="Settings saved.";}
            ImGui::End();
        }
        ImGui::Render();ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
    }
    void display(bool clear) {
        if(clear) {renderer->set_target(0);renderer->begin(0xFF16191D);renderer->end();}
        if(renderer->display()) {interface();renderer->present(settings.vsync);}
    }
    BootResult play(const std::filesystem::path& executable,const std::filesystem::path& disc,const std::filesystem::path& frame,
                    std::span<const MemorySlice> slices={},bool trace_io=false) {
        Xbe image(executable);if(!image.supported()) throw std::runtime_error("The executable does not match the supported Burnout 2 release.");
        std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
        XboxHost host{renderer.get(),input.get(),[&]{return poll();},[&]{
            if(controls && !controls->started) controls->started=SDL_GetTicksNS();++frames;display(false);
        }};
        host.trace_io=trace_io;
        Xbox xbox(image,memory,ram,disc,std::move(host));
        playing=true;show_settings=false;
        const auto result=xbox.run(0);
        std::ostringstream captured;captured<<'[';
        for(unsigned i=0;i<slices.size();++i) {
            if(i) captured<<',';const auto& slice=slices[i];
            const auto* bytes=static_cast<const std::byte*>(memory.access(slice.address,slice.size));
            captured<<std::format("{{\"address\":{},\"bytes\":{}}}",json(hex32(slice.address)),json(hex_bytes({bytes,slice.size})));
        }
        captured<<']';memory_snapshot=captured.str();
        if(!frame.empty()) xbox.save_frame(frame);
        playing=false;show_settings=true;renderer->set_target(0);return result;
    }
};
}
int run_app(const std::filesystem::path& xbe,const std::filesystem::path& disc,unsigned seconds,const std::filesystem::path& frame,
            const std::filesystem::path& controls,std::span<const MemorySlice> slices,bool trace_io) {
    App app(false,false,controls);
    if(seconds) app.stop_at=SDL_GetTicksNS()+std::uint64_t(seconds)*1000000000;
    if(!xbe.empty()) {
        const auto result=app.play(xbe,disc,frame,slices,trace_io);
        std::ostringstream report;
        report<<std::format("{{\"format\":\"b2-live-run-v2\",\"main_reached\":{},\"draws\":{},\"flips\":{},\"presented_frames\":{},\"boundary\":{},\"eip\":{},\"visits\":{},\"audio_error\":{},\"controller_connected\":{},\"controller_packet\":{},\"control_events_applied\":{},\"history\":[",
            result.main_reached,result.graphics.draws,result.graphics.flips,app.frames,json(result.boundary),json(hex32(result.cpu.eip)),
            result.diagnostic.visits,json(result.audio_error),app.input->connected(),app.input->state(0).packet,app.controls?app.controls->next:0);
        const auto count=std::min<std::uint64_t>(result.diagnostic.visits,result.diagnostic.history.size());
        for(std::uint64_t i=0;i<count;++i) {if(i) report<<',';report<<json(hex32(result.diagnostic.history[(result.diagnostic.visits-count+i)%result.diagnostic.history.size()]));}
        report<<"],\"stack_words\":[";
        for(unsigned i=0;i<result.stack_word_count;++i) {if(i) report<<',';report<<json(hex32(result.stack_words[i]));}
        report<<"],\"threads\":[";
        for(unsigned i=0;i<result.threads.size();++i) {
            if(i) report<<',';const auto& thread=result.threads[i];
            report<<std::format("{{\"id\":{},\"eip\":{},\"state\":{},\"wait_object\":{}}}",thread.id,json(hex32(thread.eip)),json(thread.state),json(hex32(thread.wait_object)));
        }
        const auto stats=app.renderer->stats();
        report<<"],\"io\":[";
        for(unsigned i=0;i<result.io.size();++i) {
            if(i) report<<',';const auto& io=result.io[i];
            report<<std::format("{{\"ordinal\":{},\"status\":{},\"information\":{},\"path\":{},\"buffer\":{},\"requested\":{},\"offset\":{}}}",
                io.ordinal,json(hex32(io.status)),io.information,json(io.path),json(hex32(io.buffer)),io.requested,io.offset);
        }
        report<<std::format("],\"adapter\":{},\"shader_compilations\":{},\"uploaded_vertex_bytes\":{},\"vblank_callbacks\":{},\"memory\":{}}}",
            json(result.graphics_adapter),stats.shader_compilations,stats.vertex_bytes,result.vblank_callbacks,app.memory_snapshot);
        std::cout<<report.str()<<'\n';
        return result.boundary.empty() || result.boundary.find("Native window closed")!=std::string::npos?0:3;
    }
    while(app.poll()) {
        app.display(true);
        if(app.launch) {
            app.launch=false;app.status="Starting game...";app.display(true);
            try {const auto result=app.play(app.settings.xbe.data(),app.settings.disc.data(),{});app.status=result.boundary.empty()?"Game exited.":result.boundary;}
            catch(const std::exception& error) {app.playing=false;app.show_settings=true;app.status=error.what();}
        }
        SDL_Delay(8);
    }
    return 0;
}
std::string check_window(const std::filesystem::path& path,bool warp) {
    App app(true,warp);
    for(unsigned frame=0;frame<3;++frame) {
        app.poll();app.renderer->begin(0xFF16344A);app.renderer->end();
        if(!app.renderer->display()) throw std::runtime_error("Hidden SDL3 window could not display an image");
        app.interface();if(frame!=2) app.renderer->present(false);
    }
    if(ImGui::GetDrawData()->TotalVtxCount==0) throw std::runtime_error("Settings interface emitted no visible geometry");
    const auto image=app.renderer->read_display();save_png(image,path);
    if(image.width!=960 || image.height!=720) throw std::runtime_error("SDL3 window image dimensions differ");
    const auto messages=app.renderer->errors();
    if(!messages.empty()) throw std::runtime_error("SDL3/ImGui validation: "+messages.front());
    app.renderer->present(false);
    return std::format("{{\"format\":\"b2-window-check-v1\",\"passed\":true,\"backend\":\"SDL3/D3D11/ImGui\",\"width\":{},\"height\":{},\"image\":{}}}",image.width,image.height,json(utf8(path.wstring())));
}
}
