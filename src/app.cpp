#include "app.h"
#include "input.h"
#include "file.h"
#include "xbox.h"
#include "diagnostics.h"
#include <SDL3/SDL.h>
#include <imgui.h>
#include <imgui_impl_sdl3.h>
#include <imgui_impl_dx11.h>
#include <Windows.h>
#include <commdlg.h>
#include <d3d11.h>
#include <wrl/client.h>
#include <algorithm>
#include <charconv>
#include <cfloat>
#include <cmath>
#include <deque>
#include <fstream>
#include <iostream>
#include <sstream>

#if defined(__LLVM_INSTR_PROFILE_GENERATE)
extern "C" void __llvm_profile_reset_counters();
#endif

namespace b2 {
namespace {
// Explicit live diagnostics use their own SDL controller. Physical controllers
// and global keyboard state are never changed by a script.
struct ControlScript {
    struct Event {std::uint64_t at;int control,value;bool axis;};
    std::vector<Event> events;
    int performance=-1,benchmark=-1;
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
            const bool recording=name=="performance" || name=="benchmark";
            const auto axis=SDL_GetGamepadAxisFromString(name.c_str());const auto button=SDL_GetGamepadButtonFromString(name.c_str());
            if((!recording && axis==SDL_GAMEPAD_AXIS_INVALID && button==SDL_GAMEPAD_BUTTON_INVALID) ||
                (axis==SDL_GAMEPAD_AXIS_INVALID?(value!=0 && value!=1):(value<-32768 || value>32767)))
                throw std::runtime_error("Invalid controller name or value in script");
            const auto at=milliseconds*1000000;if(!events.empty() && at<events.back().at) throw std::runtime_error("Controller events are out of order");
            events.push_back({at,recording?(name=="performance"?-1:-2):axis!=SDL_GAMEPAD_AXIS_INVALID?int(axis):int(button),value,axis!=SDL_GAMEPAD_AXIS_INVALID});
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
            if(event.control==-1) {performance=event.value;continue;}
            if(event.control==-2) {benchmark=event.value;continue;}
            const auto valid=event.axis?SDL_SetJoystickVirtualAxis(joystick,event.control,static_cast<Sint16>(event.value)):
                SDL_SetJoystickVirtualButton(joystick,event.control,event.value!=0);
            if(!valid) throw std::runtime_error(SDL_GetError());changed=true;
        }
        if(changed) SDL_UpdateJoysticks();
    }
};
struct Preferences {
    std::array<char,4096> xbe{},disc{};
    bool vsync=false,fullscreen=false,keyboard=true,muted=false,interpolation=false,show_fps=false,widescreen=false;
    int refresh_rate=60,volume=100,resolution_scale=1;
    KeyboardBindings bindings=default_keyboard_bindings();
    explicit Preferences(bool load=true,const std::filesystem::path& path="data/local/settings.cfg") {
        strcpy_s(xbe.data(),xbe.size(),"data/local/default.xbe");
        strcpy_s(disc.data(),disc.size(),"Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso");
        if(!load) return;
        std::ifstream file(path);std::string line;
        while(std::getline(file,line)) {
            if(line.size()>8192) throw std::runtime_error("Settings line exceeds its bound");
            const auto split=line.find('=');if(split==std::string::npos) continue;
            const auto key=line.substr(0,split),value=line.substr(split+1);
            const auto number=[&](int& destination,int minimum,int maximum) {
                int candidate=0;const auto result=std::from_chars(value.data(),value.data()+value.size(),candidate);
                if(result.ec==std::errc{} && result.ptr==value.data()+value.size() && candidate>=minimum && candidate<=maximum) destination=candidate;
            };
            if(key=="xbe" || key=="disc") {
                auto& destination=key=="xbe"?xbe:disc;
                if(value.size()<destination.size()) strcpy_s(destination.data(),destination.size(),value.c_str());
            } else if(key=="vsync") vsync=value=="1";
            else if(key=="fullscreen") fullscreen=value=="1";
            else if(key=="widescreen") widescreen=value=="1";
            else if(key=="keyboard") keyboard=value=="1";
            else if(key=="muted") muted=value=="1";
            else if(key=="interpolation") interpolation=value=="1";
            else if(key=="show_fps") show_fps=value=="1";
            else if(key=="refresh_rate") number(refresh_rate,30,1000);
            else if(key=="resolution_scale") number(resolution_scale,1,4);
            else if(key=="volume") number(volume,0,100);
            else if(key.starts_with("key_")) {
                for(unsigned i=0;i<keyboard_actions.size();++i) if(key.substr(4)==keyboard_actions[i].id) {
                    auto candidate=bindings[i];number(candidate,0,SDL_SCANCODE_COUNT-1);
                    if(valid_keyboard_binding(candidate)) bindings[i]=candidate;
                }
            }
        }
    }
    void save(const std::filesystem::path& path="data/local/settings.cfg") const {
        if(!path.parent_path().empty()) std::filesystem::create_directories(path.parent_path());
        std::ofstream file(path,std::ios::trunc);
        file<<"xbe="<<xbe.data()<<"\ndisc="<<disc.data()<<"\nvsync="<<vsync<<"\nfullscreen="<<fullscreen<<"\nkeyboard="<<keyboard
            <<"\nrefresh_rate="<<refresh_rate<<"\nvolume="<<volume<<"\nmuted="<<muted
            <<"\nresolution_scale="<<resolution_scale<<"\ninterpolation="<<interpolation<<"\nshow_fps="<<show_fps<<"\nwidescreen="<<widescreen<<'\n';
        for(unsigned i=0;i<keyboard_actions.size();++i) file<<"key_"<<keyboard_actions[i].id<<'='<<bindings[i]<<'\n';
        if(!file) throw std::runtime_error("Could not save settings");
    }
    void select_refresh_rate(int rate) {refresh_rate=rate;interpolation=rate>60;}
    bool interpolate() const {return interpolation && refresh_rate>60;}
    int source_rate() const {return interpolate()?60:refresh_rate;}
};
void setting_help(const char* text) {
    if(ImGui::IsItemHovered(ImGuiHoveredFlags_ForTooltip|ImGuiHoveredFlags_AllowWhenDisabled) && ImGui::BeginTooltip()) {
        ImGui::PushTextWrapPos(ImGui::GetFontSize()*28);
        ImGui::TextUnformatted(text);ImGui::PopTextWrapPos();ImGui::EndTooltip();
    }
}
struct FrameRate {
    std::uint64_t started=0,frames=0;
    double value=0;
    void sample(std::uint64_t now) {
        if(!started) {started=now;frames=0;return;}
        ++frames;
        if(now-started>=500000000) {value=frames*1000000000.0/(now-started);started=now;frames=0;}
    }
};
struct FramePacer {
    std::uint64_t next=0,period=0;
    bool due(int rate,std::uint64_t now) {
        const auto interval=1000000000ULL/rate;
        if(period!=interval || !next || now>=next+interval) next=now;
        period=interval;
        return now>=next;
    }
    void advance() {next+=period;}
    void wait(int rate) {
        // Absolute deadlines avoid adding render/present time to each interval.
        // A stall or rate change starts a new schedule, without a catch-up burst.
        const auto now=SDL_GetTicksNS();due(rate,now);
        if(now<next) SDL_DelayPrecise(next-now);
        advance();
    }
};
struct App {
    SDL_Window* window=nullptr;
    std::unique_ptr<Renderer> renderer;
    std::unique_ptr<Input> input;
    std::unique_ptr<ControlScript> controls;
    Preferences settings;
    std::filesystem::path settings_path;
    FramePacer pacer,source_pacer;
    FrameRate display_rate,game_rate;
    Performance performance;
    std::vector<std::uint64_t> benchmark_frames;
    std::uint64_t benchmark_started=0;
    unsigned benchmark_dropped=0;
    std::string benchmark_properties,benchmark_cpu,benchmark_build;
    Xbox* game=nullptr;
    std::vector<int> refresh_rates;
    int category=0,select_category=-1,binding_action=-1,active_scale=1;
    bool running=true,playing=false,show_settings=true,launch=false,settings_dirty=false,active_widescreen=false;
    std::deque<std::string> log_messages;
    std::string memory_snapshot="[]";
    std::uint64_t stop_at=0,frames=0,first_frame=0,last_frame=0;
    std::uint64_t published_at=0,first_source=0,last_source=0,source_interval=1000000000ULL/60;
    App(bool hidden,bool warp,const std::filesystem::path& script={},const std::filesystem::path& preferences="data/local/settings.cfg"):
        settings(!hidden,preferences),settings_path(preferences) {
        // Explicit scripts must work while the user keeps another app focused.
        // This process reads only its own virtual controller in script mode.
        if(!script.empty() && !SDL_SetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS,"1"))
            throw std::runtime_error("SDL background input is required for scripted controls");
        if(!SDL_Init(SDL_INIT_VIDEO|SDL_INIT_GAMEPAD|SDL_INIT_AUDIO)) throw std::runtime_error(SDL_GetError());
        try {
            window=SDL_CreateWindow("Burnout 2: Point of Impact",960,720,SDL_WINDOW_RESIZABLE|SDL_WINDOW_HIGH_PIXEL_DENSITY|(hidden?SDL_WINDOW_HIDDEN:0));
            if(!window) throw std::runtime_error(SDL_GetError());
            refresh_options();
            auto* handle=SDL_GetPointerProperty(SDL_GetWindowProperties(window),SDL_PROP_WINDOW_WIN32_HWND_POINTER,nullptr);
            active_scale=settings.resolution_scale;
            renderer=std::make_unique<Renderer>(640,480,warp,hidden,active_scale);renderer->attach_window(handle);
            active_widescreen=settings.widescreen;renderer->widescreen(active_widescreen);
            if(!script.empty()) controls=std::make_unique<ControlScript>(script);
            input=std::make_unique<Input>(controls?false:settings.keyboard,false,controls?controls->id:0);
            input->bindings(settings.bindings);
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
    ~App() {save_settings();cleanup();}
    void log_message(std::string message) {
        if(message.empty()) return;
        if(log_messages.size()==64) log_messages.pop_front();
        log_messages.push_back(std::move(message));
    }
    void save_settings() {
        if(!settings_dirty) return;
        settings_dirty=false;
        try {settings.save(settings_path);}
        catch(const std::exception& error) {log_message(error.what());}
    }
    void exit_game() {running=false;launch=false;binding_action=-1;save_settings();}
    PerfCounters performance_counters() const {
        auto counters=game?game->performance_counters():renderer->stats().counters;
        counters[unsigned(PerfMetric::displayed)]=frames;counters[unsigned(PerfMetric::generated)]=renderer->stats().interpolated_frames;return counters;
    }
    void stop_performance() {
        if(!performance.active()) return;
        const auto directory=std::filesystem::path("build/captures")/std::format("performance-{}",SDL_GetTicksNS());
        try {
            renderer->performance(nullptr);if(game) game->performance_recording(false);performance.stop();performance.save(directory);
            performance.reset();
            log_message("Performance saved: "+directory.string());
        } catch(const std::exception& error) {performance.reset();log_message(error.what());}
    }
    std::string benchmark_settings() const {
        return std::format("\"refresh_rate\":{},\"resolution_scale\":{},\"widescreen\":{},\"interpolation\":{},\"vsync\":{}",
            settings.refresh_rate,active_scale,active_widescreen,settings.interpolate(),settings.vsync);
    }
    void stop_benchmark() {
        if(!benchmark_started) return;
        const auto duration=std::min<std::uint64_t>(SDL_GetTicksNS()-benchmark_started,30000000000ULL);benchmark_started=0;
        try {
            if(benchmark_frames.size()<2) throw std::runtime_error("Benchmark has fewer than two original frames");
            std::vector<std::uint64_t> intervals;intervals.reserve(benchmark_frames.size()-1);
            for(std::size_t i=1;i<benchmark_frames.size();++i) intervals.push_back(benchmark_frames[i]-benchmark_frames[i-1]);
            auto sorted=intervals;std::ranges::sort(sorted);
            const auto percentile=[&](double p) {return sorted[std::min(sorted.size()-1,std::size_t(std::ceil(p*sorted.size()))-1)]/1000000.0;};
            const auto elapsed=benchmark_frames.back()-benchmark_frames.front();
            const auto directory=std::filesystem::path("build/captures")/std::format("benchmark-{}",SDL_GetTicksNS());
            std::filesystem::create_directories(directory);
            std::ofstream out(directory/"benchmark.json");
            out<<std::format("{{\"format\":\"b2-game-benchmark-v1\",\"cpu\":{},\"gpu\":{},\"build\":{},{} ,\"settings_unchanged\":{},\"duration_ms\":{:.6f},\"measured_ms\":{:.6f},\"frames\":{},\"fps\":{:.6f},\"median_ms\":{:.6f},\"p95_ms\":{:.6f},\"p99_ms\":{:.6f},\"max_ms\":{:.6f},\"frames_dropped\":{},\"detailed_recording\":false,\"interval_ns\":[",
                json(benchmark_cpu),json(renderer->adapter()),json(benchmark_build),benchmark_properties,benchmark_properties==benchmark_settings(),duration/1000000.0,elapsed/1000000.0,intervals.size(),
                intervals.size()*1000000000.0/elapsed,percentile(.5),percentile(.95),percentile(.99),percentile(1),benchmark_dropped);
            for(std::size_t i=0;i<intervals.size();++i) {if(i) out<<',';out<<intervals[i];}
            out<<"]}\n";if(!out) throw std::runtime_error("Cannot save frame benchmark");
            log_message("Benchmark saved: "+directory.string());
        } catch(const std::exception& error) {log_message(error.what());}
    }
    void start_benchmark() {
        if(!playing || benchmark_started) return;
        stop_performance();benchmark_frames.clear();benchmark_frames.reserve(8192);benchmark_dropped=0;
        std::array<char,49> brand{};int info[4];__cpuid(info,0x80000000);
        if(unsigned(info[0])>=0x80000004) for(unsigned i=0;i<3;++i) {__cpuid(info,0x80000002+i);std::memcpy(brand.data()+i*16,info,16);}
        benchmark_cpu=brand[0]?brand.data():"Unknown x64 CPU";
        const auto* module=reinterpret_cast<const std::byte*>(GetModuleHandleW(nullptr));
        const auto* dos=reinterpret_cast<const IMAGE_DOS_HEADER*>(module);
        const auto* image=reinterpret_cast<const IMAGE_NT_HEADERS64*>(module+dos->e_lfanew);
        benchmark_build=std::format("PE {:08X}, image {} bytes",image->FileHeader.TimeDateStamp,image->OptionalHeader.SizeOfImage);
        benchmark_properties=benchmark_settings();benchmark_started=SDL_GetTicksNS();
    }
    void toggle_performance() {
        if(performance.active()) {stop_performance();return;}if(!playing) return;
        stop_benchmark();
        try {
#if defined(__LLVM_INSTR_PROFILE_GENERATE)
            // Train native compilation on the selected recording, excluding
            // boot and menu navigation from the compiler's branch counts.
            __llvm_profile_reset_counters();
#endif
            renderer->performance(&performance);if(game) game->performance_recording(true);
            performance.start(renderer->adapter(),settings.refresh_rate,active_scale,settings.interpolate(),settings.vsync,performance_counters());
            log_message("Recording performance. F9 saves; recording stops after 60 seconds.");
        } catch(const std::exception& error) {renderer->performance(nullptr);if(game) game->performance_recording(false);performance.reset();log_message(error.what());}
    }
    void capture_frame() {
        if(!game) return;
        const auto directory=std::filesystem::path("build/captures")/std::format("frame-{}",SDL_GetTicksNS());
        PerfScope capture(&performance,PerfPhase::capture);
        try {if(performance.active()) performance.save(directory);game->capture_frame(directory);log_message("Frame and audio capture: "+directory.string());}
        catch(const std::exception& error) {log_message(error.what());}
    }
    void refresh_options() {
        refresh_rates={30,60,75,90,120,144,165,180,240,360};
        int count=0;auto** modes=SDL_GetFullscreenDisplayModes(SDL_GetDisplayForWindow(window),&count);
        if(modes) {
            for(int i=0;i<count;++i) {
                const auto rate=static_cast<int>(std::round(modes[i]->refresh_rate));
                if(rate>=30 && rate<=1000) refresh_rates.push_back(rate);
            }
            SDL_free(modes);
        }
        refresh_rates.push_back(settings.refresh_rate);
        std::ranges::sort(refresh_rates);
        refresh_rates.erase(std::unique(refresh_rates.begin(),refresh_rates.end()),refresh_rates.end());
    }
    void event(const SDL_Event& event) {
        ImGui_ImplSDL3_ProcessEvent(&event);input->event(event);
        if(event.type==SDL_EVENT_QUIT || event.type==SDL_EVENT_WINDOW_CLOSE_REQUESTED) exit_game();
        if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat && binding_action>=0) {
            if(event.key.scancode==SDL_SCANCODE_ESCAPE) binding_action=-1;
            else if(valid_keyboard_binding(event.key.scancode)) {
                settings_dirty|=settings.bindings[binding_action]!=event.key.scancode;
                settings.bindings[binding_action]=event.key.scancode;input->bindings(settings.bindings);binding_action=-1;
            } else log_message("That key is reserved for settings or captures.");
            return;
        }
        if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat &&
           (event.key.scancode==SDL_SCANCODE_ESCAPE || event.key.scancode==SDL_SCANCODE_F1)) {
            show_settings=!show_settings;binding_action=-1;if(!show_settings) save_settings();
        }
        if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat && event.key.scancode==SDL_SCANCODE_F8) capture_frame();
        if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat && event.key.scancode==SDL_SCANCODE_F9) toggle_performance();
        if(event.type==SDL_EVENT_WINDOW_DISPLAY_CHANGED) refresh_options();
    }
    bool poll() {
        SDL_Event event;
        while(SDL_PollEvent(&event)) this->event(event);
        if(stop_at && SDL_GetTicksNS()>=stop_at) running=false;
        if(controls) {
            controls->advance();
            if(controls->performance>=0) {
                if(bool(controls->performance)!=performance.active()) toggle_performance();
                controls->performance=-1;
            }
            if(controls->benchmark!=-1) {
                if(controls->benchmark) start_benchmark();else stop_benchmark();
                controls->benchmark=-1;
            }
        }
        input->capture_keyboard(show_settings);input->update();
        if(running && playing && !launch && published_at) display_due();
        if(performance.active()) {
            if(performance.resources_due()) {
                PerfScope overhead(&performance,PerfPhase::diagnostics);const auto r=renderer->resources();
                performance.resources(r.local,r.budget,r.shared,r.gpu_valid,r.textures,r.targets,r.texture_bytes,r.target_bytes);
            }
            if(performance.expired()) stop_performance();
        }
        if(benchmark_started && SDL_GetTicksNS()-benchmark_started>=30000000000ULL) stop_benchmark();
        return running;
    }
    bool poll_game() {return poll() && !launch;}
    bool browse(std::array<char,4096>& path,bool iso) {
        std::array<wchar_t,32768> result{};
        OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);
        dialog.hwndOwner=static_cast<HWND>(SDL_GetPointerProperty(SDL_GetWindowProperties(window),SDL_PROP_WINDOW_WIN32_HWND_POINTER,nullptr));
        dialog.lpstrFilter=iso?L"Xbox game disc (*.iso)\0*.iso\0\0":L"Xbox executable (*.xbe)\0*.xbe\0\0";
        dialog.lpstrFile=result.data();dialog.nMaxFile=static_cast<DWORD>(result.size());dialog.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_NOCHANGEDIR;
        if(GetOpenFileNameW(&dialog)) {
            const auto text=utf8(result.data());
            if(text.size()<path.size() && text!=path.data()) {strcpy_s(path.data(),path.size(),text.c_str());return true;}
        }
        return false;
    }
    void settings_interface() {
        PerfScope timing(&performance,PerfPhase::ui);
        ImGui_ImplDX11_NewFrame();ImGui_ImplSDL3_NewFrame();ImGui::NewFrame();
        if(show_settings) {
            const auto* viewport=ImGui::GetMainViewport();
            ImGui::SetNextWindowPos(viewport->GetCenter(),ImGuiCond_Always,{0.5f,0.5f});
            ImGui::SetNextWindowSize({std::min(640.0f,viewport->Size.x-32),std::min(440.0f,viewport->Size.y-32)});
            ImGui::Begin("Burnout 2: Point of Impact",nullptr,ImGuiWindowFlags_NoCollapse|ImGuiWindowFlags_NoResize);
            bool changed=false;
            if(ImGui::BeginTabBar("Settings")) {
                constexpr const char* categories[]={"Game Files","General","Audio","Graphics","Controls","Log"};
                for(int tab=0;tab<int(std::size(categories));++tab) if(ImGui::BeginTabItem(categories[tab],nullptr,select_category==tab?ImGuiTabItemFlags_SetSelected:0)) {
                    if(category!=tab) binding_action=-1;
                    category=tab;
                    ImGui::BeginChild("Options",{0,-70});
                    switch(tab) {
                    case 0:
                        ImGui::BeginDisabled(playing);
                        changed|=ImGui::InputText("Executable",settings.xbe.data(),settings.xbe.size());
                        setting_help(playing?"Restart the app to change game files.":"The supported US Xbox game executable (default.xbe).");
                        ImGui::SameLine();if(ImGui::Button("Browse##xbe")) changed|=browse(settings.xbe,false);
                        setting_help(playing?"Restart the app to change game files.":"Select the game executable. Changes save automatically.");
                        changed|=ImGui::InputText("Game disc",settings.disc.data(),settings.disc.size());
                        setting_help(playing?"Restart the app to change game files.":"Your US Xbox XISO disc image. Game files are read from this image.");
                        ImGui::SameLine();if(ImGui::Button("Browse##disc")) changed|=browse(settings.disc,true);
                        setting_help(playing?"Restart the app to change game files.":"Select the Xbox game disc image. Changes save automatically.");
                        ImGui::EndDisabled();
                        break;
                    case 1: {
                        const auto selected=std::format("{} Hz",settings.refresh_rate);
                        if(ImGui::BeginCombo("Refresh rate",selected.c_str())) {
                            for(const auto rate:refresh_rates) {
                                const auto label=std::format("{} Hz",rate);const bool active=rate==settings.refresh_rate;
                                if(ImGui::Selectable(label.c_str(),active)) {settings.select_refresh_rate(rate);changed=true;}
                                if(active) ImGui::SetItemDefaultFocus();
                            }
                            ImGui::EndCombo();
                        }
                        setting_help("Sets the frame rate. Selecting a rate above 60 Hz turns on frame interpolation.");
                        changed|=ImGui::Checkbox("Frame interpolation",&settings.interpolation);
                        setting_help("Requires a refresh rate above 60 Hz. Keeps normal game speed and adds one game frame of display delay. Uncheck to run original frames at the selected rate; higher rates can speed up movement.");
                        changed|=ImGui::Checkbox("Show frame rate",&settings.show_fps);
                        setting_help("Displays the game and display frame rates in the upper-right corner.");
                        break;
                    }
                    case 2: {
                        bool audio_changed=ImGui::SliderInt("Master volume",&settings.volume,0,100,"%d%%");
                        setting_help("Applies to both music and sound effects.");
                        audio_changed|=ImGui::Checkbox("Mute",&settings.muted);
                        setting_help("Mutes all game audio without changing the master volume.");
                        changed|=audio_changed;
                        if(audio_changed && game) game->output_gain(settings.muted?0:settings.volume/100.0f);
                        break;
                    }
                    case 3: {
                        changed|=ImGui::Checkbox("Widescreen (16:9)",&settings.widescreen);
                        setting_help("Uses the game's native 16:9 camera and screen layout. Applies when you start or restart the game.");
                        changed|=ImGui::Checkbox("V-sync",&settings.vsync);
                        setting_help("V-sync has known issues. Leave it disabled for now.");
                        if(ImGui::Checkbox("Fullscreen",&settings.fullscreen)) {
                            if(!SDL_SetWindowFullscreen(window,settings.fullscreen)) {settings.fullscreen=!settings.fullscreen;log_message(SDL_GetError());}
                            else changed=true;
                        }
                        setting_help("Switches between a window and fullscreen immediately.");
                        {
                            const auto label=std::format("{}x ({} x {})",settings.resolution_scale,640*settings.resolution_scale,480*settings.resolution_scale);
                            if(ImGui::BeginCombo("Internal resolution",label.c_str())) {
                                for(int scale=1;scale<=4;++scale) {
                                    const auto option=std::format("{}x ({} x {})",scale,640*scale,480*scale);
                                    if(ImGui::Selectable(option.c_str(),settings.resolution_scale==scale)) {settings.resolution_scale=scale;changed=true;}
                                }
                                ImGui::EndCombo();
                            }
                            setting_help(playing?"Higher resolutions sharpen geometry and use more GPU power. Original proportions are preserved. Use Restart game to apply changes.":
                                "Higher resolutions sharpen geometry and use more GPU power. Original proportions are preserved. Applies when you start the game.");
                        }
                        ImGui::Spacing();ImGui::BeginDisabled(!playing);
                        if(ImGui::Button("Capture frame (F8)")) capture_frame();
                        setting_help("Saves the current frame and audio in build/captures. Also includes performance data while recording.");
                        const bool diagnostics=ImGui::CollapsingHeader("Performance diagnostics");
                        setting_help("Record a slowdown, then press F9 to save. Recording stops after 60 seconds. Reports are in build/captures.");
                        if(diagnostics) {
                            if(ImGui::Button(performance.active()?"Save performance (F9)":"Record performance (F9)")) toggle_performance();
                            setting_help("Record a slowdown, then press F9 to save. Recording stops after 60 seconds. F8 also saves performance while recording.");
                        }
                        ImGui::EndDisabled();
                        break;
                    }
                    case 4:
                        if(ImGui::Checkbox("Keyboard controller",&settings.keyboard)) {input->keyboard(settings.keyboard);changed=true;}
                        setting_help("Enables keyboard controls. Connected gamepads work automatically.");
                        ImGui::SeparatorText("Keyboard");
                        if(ImGui::Button("Restore keyboard defaults")) {settings.bindings=default_keyboard_bindings();input->bindings(settings.bindings);binding_action=-1;changed=true;}
                        setting_help("Restores the original keyboard bindings.");
                        if(ImGui::BeginTable("Keys",3,ImGuiTableFlags_RowBg)) {
                            ImGui::TableSetupColumn("Action",ImGuiTableColumnFlags_WidthStretch);
                            ImGui::TableSetupColumn("Key",ImGuiTableColumnFlags_WidthFixed,150);
                            ImGui::TableSetupColumn("Clear",ImGuiTableColumnFlags_WidthFixed,65);
                            constexpr unsigned order[]={15,14,16,17,8,9,4,5,0,1,2,3,10,11,12,13,6,7,18,19,20,21,22,23};
                            for(const auto i:order) {
                                ImGui::PushID(int(i));ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::TextUnformatted(keyboard_actions[i].label.data());
                                ImGui::TableNextColumn();const auto code=SDL_Scancode(settings.bindings[i]);
                                const auto* name=code?SDL_GetKeyName(SDL_GetKeyFromScancode(code,SDL_KMOD_NONE,false)):"Unbound";
                                if(ImGui::Button(binding_action==int(i)?"Press a key...":name,{-FLT_MIN,0})) binding_action=int(i);
                                setting_help("Click, then press a key. Esc cancels. Esc, F1, F8 and F9 are reserved for settings and captures.");
                                ImGui::TableNextColumn();if(ImGui::SmallButton("Clear")) {changed|=settings.bindings[i]!=0;settings.bindings[i]=0;input->bindings(settings.bindings);binding_action=-1;}
                                setting_help("Removes this keyboard binding.");
                                ImGui::PopID();
                            }
                            ImGui::EndTable();
                        }
                        break;
                    case 5:
                        if(log_messages.empty()) ImGui::TextUnformatted("No messages yet.");
                        for(auto message=log_messages.rbegin();message!=log_messages.rend();++message) {
                            ImGui::TextWrapped("%s",message->c_str());ImGui::Spacing();
                        }
                        break;
                    }
                    ImGui::EndChild();ImGui::EndTabItem();
                }
                ImGui::EndTabBar();
            }
            select_category=-1;
            settings_dirty|=changed;
            ImGui::Separator();
            const auto available=ImGui::GetContentRegionAvail().x,spacing=ImGui::GetStyle().ItemSpacing.x;
            const float count=playing?3.0f:2.0f;
            const auto button_width=std::clamp((available-spacing*(count-1))/count,1.0f,160.0f);
            const auto row_width=button_width*count+spacing*(count-1);
            ImGui::SetCursorPosX(ImGui::GetCursorPosX()+std::max(0.0f,(available-row_width)/2));
            if(ImGui::Button(playing?"Restart game":"Start game",{button_width,0})) {launch=true;binding_action=-1;}
            setting_help(playing?"Restarts the game from the beginning with the saved settings. The window stays open.":
                "Starts the game with these settings. Changes save automatically. Esc opens or closes settings.");
            if(playing) {
                ImGui::SameLine();
                if(ImGui::Button("Return to game",{button_width,0})) {show_settings=false;binding_action=-1;}
                setting_help("Returns to the game. Changes save automatically. Esc opens or closes settings.");
            }
            ImGui::SameLine();
            if(ImGui::Button("Exit game",{button_width,0})) exit_game();
            setting_help("Closes the game and the window. Pending setting changes save automatically.");
            if(!ImGui::IsAnyItemActive() || !show_settings || launch) save_settings();
            ImGui::End();
            if(playing) renderer->interpolation(settings.interpolate());
        }
        if(playing && settings.show_fps) {
            const auto* viewport=ImGui::GetMainViewport();
            ImGui::SetNextWindowPos({viewport->WorkPos.x+viewport->WorkSize.x-12,viewport->WorkPos.y+12},ImGuiCond_Always,{1,0});
            ImGui::SetNextWindowBgAlpha(0.65f);
            ImGui::Begin("Frame rate",nullptr,ImGuiWindowFlags_NoDecoration|ImGuiWindowFlags_AlwaysAutoResize|
                ImGuiWindowFlags_NoSavedSettings|ImGuiWindowFlags_NoFocusOnAppearing|ImGuiWindowFlags_NoNav|ImGuiWindowFlags_NoInputs);
            ImGui::Text("Display: %.0f FPS",display_rate.value);
            ImGui::Text("Game: %.0f FPS",game_rate.value);
            ImGui::End();
        }
        if(performance.active()) {
            ImGui::SetNextWindowPos({12,12},ImGuiCond_Always);ImGui::SetNextWindowBgAlpha(.65f);
            ImGui::Begin("Performance recording",nullptr,ImGuiWindowFlags_NoDecoration|ImGuiWindowFlags_AlwaysAutoResize|ImGuiWindowFlags_NoSavedSettings|ImGuiWindowFlags_NoInputs|ImGuiWindowFlags_NoFocusOnAppearing);
            ImGui::TextUnformatted("Recording performance - F9 saves");ImGui::End();
        }
        ImGui::Render();ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
    }
    void frame_presented() {
        last_frame=SDL_GetTicksNS();display_rate.sample(last_frame);
        if(!frames) first_frame=last_frame;++frames;
    }
    void display(bool clear) {
        if(clear) {renderer->set_target(0);renderer->begin(0xFF16191D);renderer->end();}
        if(renderer->display()) {
            settings_interface();{PerfScope wait(&performance,PerfPhase::pacing);pacer.wait(settings.refresh_rate);}renderer->present(settings.vsync);
            if(playing) frame_presented();
        } else pacer.wait(settings.refresh_rate);
    }
    void display_due(bool source_frame=false) {
        const bool interpolate=settings.interpolate();
        if(!interpolate && !source_frame) return;
        const auto now=SDL_GetTicksNS();
        if(interpolate && !pacer.due(settings.refresh_rate,now)) return;
        // Interpolate the two completed frames one source interval behind the
        // simulation. Menus/settings are drawn afterwards at the display rate.
        const auto phase=interpolate?std::clamp(float(double(now-published_at)/source_interval),0.0f,1.0f):1.0f;
        // Extra presentations run between guest checkpoints. Waiting for a
        // full display queue here can consume every slice of game execution.
        if(renderer->display(phase,!interpolate)) {
            settings_interface();
            if(renderer->present(settings.vsync,!interpolate)) frame_presented();
        }
        if(interpolate) pacer.advance();
    }
    void game_flip() {
        // Interpolation presents extra images between 60 Hz original frames.
        // Without it, each original frame uses the selected refresh rate.
        auto now=SDL_GetTicksNS();source_pacer.due(settings.source_rate(),now);
        while(running && now<source_pacer.next) {
            if(!poll_game()) return;
            now=SDL_GetTicksNS();source_pacer.due(settings.source_rate(),now);
            auto deadline=source_pacer.next;
            if(settings.interpolate() && published_at && pacer.next>now) deadline=std::min(deadline,pacer.next);
            if(now<deadline) {PerfScope wait(&performance,PerfPhase::pacing);SDL_DelayPrecise(deadline-now);}
            now=SDL_GetTicksNS();
        }
        if(!running || launch) return;
        const bool reset=!published_at || now-published_at>100000000;
        source_interval=reset?1000000000ULL/settings.source_rate():now-published_at;published_at=now;
        if(performance.active()) performance.source_frame(renderer->stats().published_frames+1,performance_counters());
        if(benchmark_started) {
            if(now-benchmark_started<30000000000ULL) {
                if(benchmark_frames.size()<8192) benchmark_frames.push_back(now);else ++benchmark_dropped;
            }
        }
        renderer->publish_frame(reset);source_pacer.advance();
        game_rate.sample(now);
        if(!first_source) first_source=now;last_source=now;
        if(controls && !controls->started) controls->started=now;
        display_due(true);
    }
    void configure_graphics() {
        // Preserve pipelines/UI while discarding the previous game's resources.
        // Mid-race recreation would lose feedback, depth and texture identities.
        renderer->reset(settings.resolution_scale);active_scale=settings.resolution_scale;
        active_widescreen=settings.widescreen;renderer->widescreen(active_widescreen);
        renderer->interpolation(settings.interpolate());
    }
    BootResult play(const std::filesystem::path& executable,const std::filesystem::path& disc,const std::filesystem::path& frame,
                    std::span<const MemorySlice> slices={},bool trace_io=false) {
        Xbe image(executable);if(!image.supported()) throw std::runtime_error("The executable does not match the supported Burnout 2 release.");
        configure_graphics();
        diagnostic_record(std::format("{{\"type\":\"game_start\",\"xbe\":{},\"disc\":{},\"sha256\":{}}}",
            json(utf8(executable.wstring())),json(utf8(disc.wstring())),json(image.sha256)));
        std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
        XboxHost host{renderer.get(),input.get(),[&]{return poll_game();},[&]{game_flip();}};
        host.widescreen=active_widescreen;
        host.performance=&performance;
        host.trace_io=trace_io;
        Xbox xbox(image,memory,ram,disc,std::move(host));
        xbox.output_gain(settings.muted?0:settings.volume/100.0f);
        struct Session {App& app;~Session(){app.stop_benchmark();app.stop_performance();app.game=nullptr;app.playing=false;app.show_settings=true;app.published_at=0;app.renderer->interpolation(false);}} session{*this};
        game=&xbox;playing=true;show_settings=false;binding_action=-1;frames=first_frame=last_frame=published_at=first_source=last_source=0;pacer={};source_pacer={};display_rate={};game_rate={};memory_snapshot="[]";
        auto result=xbox.run(0);
        if(launch && running) {
            if(result.boundary.find("Native window closed")!=std::string::npos) result.boundary="Game restart requested";
            else if(!result.boundary.empty()) launch=false;
        }
        stop_performance();
        try {diagnostic_record(run_report(result));} catch(...) {diagnostic_record("{\"type\":\"log_error\",\"message\":\"Could not serialize game result\"}");}
        std::ostringstream captured;captured<<'[';
        for(unsigned i=0;i<slices.size();++i) {
            if(i) captured<<',';const auto& slice=slices[i];
            const auto* bytes=static_cast<const std::byte*>(memory.access(slice.address,slice.size));
            captured<<std::format("{{\"address\":{},\"bytes\":{}}}",json(hex32(slice.address)),json(hex_bytes({bytes,slice.size})));
        }
        captured<<']';memory_snapshot=captured.str();
        if(!frame.empty() && !launch) {
            xbox.save_frame(frame);
            if(settings.interpolate()) for(const auto& sample:std::array<std::pair<float,const char*>,3>{{{0,"previous"},{0.5f,"interpolated"},{1,"current"}}}) {
                if(renderer->display(sample.first)) {
                    auto output=frame.parent_path()/(frame.stem().string()+"-"+sample.second+".png");
                    save_png(renderer->read_display(),output);renderer->present(false);
                }
            }
        }
        renderer->set_target(0);return result;
    }
    std::string run_report(const BootResult& result) const {
        std::ostringstream report;
        report<<std::format("{{\"type\":\"game_end\",\"format\":\"b2-live-run-v2\",\"entry_returned\":{},\"main_reached\":{},\"active_thread\":{},\"draws\":{},\"flips\":{},\"presented_frames\":{},\"boundary\":{},\"eip\":{},\"flags\":{},\"visits\":{},\"audio_error\":{},\"controller_connected\":{},\"controller_packet\":{},\"control_events_applied\":{},\"registers\":[",
            result.entry_returned,result.main_reached,result.active_thread,result.graphics.draws,result.graphics.flips,frames,json(result.boundary),json(hex32(result.cpu.eip)),json(hex32(result.cpu.flags)),
            result.diagnostic.visits,json(result.audio_error),input->connected(),input->state(0).packet,controls?controls->next:0);
        for(unsigned i=0;i<result.cpu.registers.size();++i) {if(i) report<<',';report<<json(hex32(result.cpu.registers[i]));}
        report<<"],\"history\":[";
        const auto count=std::min<std::uint64_t>(result.diagnostic.visits,result.diagnostic.history.size());
        for(std::uint64_t i=0;i<count;++i) {if(i) report<<',';report<<json(hex32(result.diagnostic.history[(result.diagnostic.visits-count+i)%result.diagnostic.history.size()]));}
        report<<"],\"stack_words\":[";
        for(unsigned i=0;i<result.stack_word_count;++i) {if(i) report<<',';report<<json(hex32(result.stack_words[i]));}
        report<<"],\"threads\":[";
        for(unsigned i=0;i<result.threads.size();++i) {
            if(i) report<<',';const auto& thread=result.threads[i];
            report<<std::format("{{\"id\":{},\"eip\":{},\"state\":{},\"wait_object\":{}}}",thread.id,json(hex32(thread.eip)),json(thread.state),json(hex32(thread.wait_object)));
        }
        const auto stats=renderer->stats();
        report<<"],\"io\":[";
        for(unsigned i=0;i<result.io.size();++i) {
            if(i) report<<',';const auto& io=result.io[i];
            report<<std::format("{{\"ordinal\":{},\"status\":{},\"information\":{},\"callsite\":{},\"path\":{},\"buffer\":{},\"requested\":{},\"offset\":{}}}",
                io.ordinal,json(hex32(io.status)),io.information,json(hex32(io.callsite)),json(io.path),json(hex32(io.buffer)),io.requested,io.offset);
        }
        const auto fps=last_frame>first_frame?(frames-1)*1000000000.0/(last_frame-first_frame):0;
        const auto source_fps=last_source>first_source?(stats.published_frames-1)*1000000000.0/(last_source-first_source):0;
        report<<std::format("],\"adapter\":{},\"shader_compilations\":{},\"uploaded_vertex_bytes\":{},\"vblank_callbacks\":{},\"refresh_rate\":{},\"resolution_scale\":{},\"widescreen\":{},\"interpolation\":{},\"interpolation_active\":{},\"source_fps\":{:.3f},\"interpolated_frames\":{},\"presented_fps\":{:.3f},\"memory\":{}}}",
            json(result.graphics_adapter),stats.shader_compilations,stats.vertex_bytes,result.vblank_callbacks,settings.refresh_rate,active_scale,active_widescreen,settings.interpolation,settings.interpolate(),source_fps,stats.interpolated_frames,fps,memory_snapshot);
        return report.str();
    }
};
}
int run_app(const std::filesystem::path& xbe,const std::filesystem::path& disc,unsigned seconds,const std::filesystem::path& frame,
            const std::filesystem::path& controls,std::span<const MemorySlice> slices,bool trace_io) {
    if(xbe.empty()) {
        std::array<wchar_t,32768> module{};
        const auto length=GetModuleFileNameW(nullptr,module.data(),static_cast<DWORD>(module.size()));
        if(!length || length==module.size()) throw std::runtime_error("Cannot locate the game folder");
        std::filesystem::current_path(std::filesystem::path(std::wstring_view(module.data(),length)).parent_path());
    }
    App app(false,false,controls);
    if(seconds) app.stop_at=SDL_GetTicksNS()+std::uint64_t(seconds)*1000000000;
    if(!xbe.empty()) {
        for(;;) {
            app.launch=false;
            const auto result=app.play(xbe,disc,frame,slices,trace_io);
            std::cout<<app.run_report(result)<<'\n';
            if(!app.running || !app.launch) return result.boundary.empty() || result.boundary.find("Native window closed")!=std::string::npos?0:3;
        }
    }
    while(app.poll()) {
        app.display(true);
        if(app.launch) {
            app.launch=false;app.log_message("Starting game...");app.display(true);
            try {
                const auto result=app.play(utf8_path(app.settings.xbe.data()),utf8_path(app.settings.disc.data()),{});
                app.log_message(app.launch?"Restarting game...":result.boundary.empty()?"Game exited.":result.boundary);
            }
            catch(const std::exception& error) {
                diagnostic_record("{\"type\":\"game_error\",\"message\":"+json(error.what())+'}');
                app.launch=false;app.playing=false;app.show_settings=true;app.log_message(error.what());
            }
            if(!app.launch && !diagnostic_log().empty()) app.log_message("Log file: "+utf8(diagnostic_log().wstring()));
        }
    }
    return 0;
}
std::string check_window(const std::filesystem::path& path,bool warp) {
    auto preferences=path;preferences+=".cfg";
    App app(true,warp,{},preferences);
    if(app.settings.refresh_rate!=60) throw std::runtime_error("Default refresh rate differs from 60 Hz");
    if(!path.parent_path().empty()) std::filesystem::create_directories(path.parent_path());
    Preferences selected(false);selected.select_refresh_rate(120);
    if(!selected.interpolation) throw std::runtime_error("Higher refresh selection did not enable interpolation");
    selected.interpolation=false;selected.select_refresh_rate(144);
    if(!selected.interpolation) throw std::runtime_error("Another higher refresh selection did not enable interpolation");
    selected.select_refresh_rate(60);
    if(selected.interpolation) throw std::runtime_error("60 Hz selection did not disable interpolation");
    FrameRate counter_rate;counter_rate.sample(1);
    for(unsigned frame=1;frame<=60;++frame) counter_rate.sample(1+frame*1000000000ULL/120);
    if(std::abs(counter_rate.value-120)>0.01) throw std::runtime_error("Frame rate counter did not measure 120 FPS");
    Preferences saved(false);saved.resolution_scale=3;saved.select_refresh_rate(144);saved.interpolation=false;saved.show_fps=true;saved.widescreen=true;saved.bindings[15]=SDL_SCANCODE_Z;
    saved.save(preferences);Preferences restored(true,preferences);
    if(restored.bindings!=saved.bindings || restored.resolution_scale!=3 || restored.refresh_rate!=144 || restored.interpolation || !restored.show_fps || !restored.widescreen)
        throw std::runtime_error("New settings did not survive save/load");
    {std::ofstream file(preferences);file<<"key_accelerate="<<SDL_SCANCODE_ESCAPE<<"\nkey_brake=999999\nresolution_scale=99\n";}
    Preferences invalid(true,preferences);std::filesystem::remove(preferences);
    if(invalid.bindings!=default_keyboard_bindings() || invalid.resolution_scale!=1) throw std::runtime_error("Invalid settings replaced valid defaults");
    SDL_Event escape{};escape.type=SDL_EVENT_KEY_DOWN;escape.key.scancode=SDL_SCANCODE_ESCAPE;
    app.event(escape);if(app.show_settings) throw std::runtime_error("Esc did not hide settings");
    escape.key.repeat=true;app.event(escape);if(app.show_settings) throw std::runtime_error("Held Esc repeated the toggle");
    escape.key.repeat=false;app.event(escape);if(!app.show_settings) throw std::runtime_error("Esc did not reopen settings");
    escape.type=SDL_EVENT_KEY_UP;app.event(escape);
    app.binding_action=15;escape.type=SDL_EVENT_KEY_DOWN;app.event(escape);
    if(!app.show_settings || app.binding_action!=-1) throw std::runtime_error("Esc did not cancel rebinding");
    SDL_Event rebind{};rebind.type=SDL_EVENT_KEY_DOWN;rebind.key.scancode=SDL_SCANCODE_Z;app.binding_action=15;app.event(rebind);
    if(app.settings.bindings[15]!=SDL_SCANCODE_Z || app.binding_action!=-1) throw std::runtime_error("Keyboard binding did not update");
    rebind.type=SDL_EVENT_KEY_UP;app.event(rebind);
    app.event(escape);Preferences automatic(true,preferences);
    if(automatic.bindings[15]!=SDL_SCANCODE_Z || app.settings_dirty) throw std::runtime_error("Edited settings were not saved when closing settings");
    app.event(escape);
    app.log_message("Starting game...");
    constexpr const char* names[]={"game-files","general","audio","graphics","controls","log"};
    std::ostringstream images;images<<'[';
    for(int tab=0;tab<int(std::size(names));++tab) {
        app.select_category=tab;
        app.playing=tab==1;
        for(unsigned frame=0;frame<3;++frame) {
            app.poll();app.renderer->begin(0xFF16344A);app.renderer->end();
            if(!app.renderer->display()) throw std::runtime_error("Hidden SDL3 window could not display an image");
            app.settings_interface();if(frame!=2) app.renderer->present(false);
        }
        if(app.category!=tab || ImGui::GetDrawData()->TotalVtxCount==0) throw std::runtime_error("Settings category emitted no visible geometry");
        const auto image=app.renderer->read_display();
        if(image.width!=960 || image.height!=720) throw std::runtime_error("SDL3 window image dimensions differ");
        auto output=path;
        if(tab) {output=path.parent_path()/path.stem();output+="-";output+=names[tab];output+=path.extension();}
        save_png(image,output);if(tab) images<<',';
        images<<std::format("{{\"category\":{},\"image\":{}}}",json(names[tab]),json(utf8(output.wstring())));
        app.renderer->present(false);
    }
    images<<']';app.select_category=0;app.settings.vsync=false;
    app.launch=true;
    if(app.poll_game() || !app.running) throw std::runtime_error("Restart request did not stop the game while keeping the app open");
    const auto before_restart=app.renderer->stats().published_frames;app.game_flip();
    if(app.renderer->stats().published_frames!=before_restart) throw std::runtime_error("Restart request published another game frame");
    app.launch=false;
    app.show_settings=false;app.playing=true;app.settings.show_fps=true;app.display_rate.value=144;app.game_rate.value=60;
    for(unsigned frame=0;frame<3;++frame) {
        app.renderer->begin(0xFF16344A);app.renderer->end();app.renderer->display();app.settings_interface();
        if(frame!=2) app.renderer->present(false);
    }
    if(!ImGui::GetDrawData()->TotalVtxCount) throw std::runtime_error("Frame rate counter emitted no visible geometry");
    auto counter_image=path.parent_path()/path.stem();counter_image+="-fps";counter_image+=path.extension();
    save_png(app.renderer->read_display(),counter_image);app.renderer->present(false);
    app.settings.show_fps=false;app.settings_interface();
    if(ImGui::GetDrawData()->TotalVtxCount) throw std::runtime_error("Disabled frame rate counter stayed visible");
    app.playing=false;app.show_settings=true;
    std::array<double,2> measured{};
    for(unsigned test=0;test<measured.size();++test) {
        const int rate=test?120:60;app.settings.refresh_rate=rate;
        std::uint64_t started=0,finished=0;
        for(int frame=0;frame<=rate/2;++frame) {
            app.display(true);finished=SDL_GetTicksNS();if(!frame) started=finished;
        }
        measured[test]=(rate/2)*1000000000.0/(finished-started);
        if(std::abs(measured[test]-rate)>rate*0.08) throw std::runtime_error(std::format("Frame pacing differs from {} Hz: {:.2f}",rate,measured[test]));
    }
    app.show_settings=false;
    app.settings.widescreen=true;app.configure_graphics();
    for(unsigned wide=0;wide<2;++wide) {
        if(wide && !SDL_SetWindowSize(app.window,1280,540)) throw std::runtime_error(SDL_GetError());
        app.poll();app.renderer->begin(0xFF4080C0);app.renderer->end();
        if(!app.renderer->display()) throw std::runtime_error("Widescreen image could not be displayed");
        const auto image=app.renderer->read_display();
        const auto black=[&](unsigned x,unsigned y) {
            const auto pixel=(std::size_t(y)*image.width+x)*4;
            return image.rgba[pixel]==std::byte{} && image.rgba[pixel+1]==std::byte{} && image.rgba[pixel+2]==std::byte{};
        };
        if(black(image.width/2,image.height/2) ||
           (wide?(!black(40,image.height/2) || !black(image.width-40,image.height/2)):
                 (!black(image.width/2,40) || !black(image.width/2,image.height-40))))
            throw std::runtime_error("Native 16:9 presentation did not preserve its aspect ratio");
        auto output=path.parent_path()/path.stem();output+=wide?"-widescreen-ultrawide":"-widescreen";output+=path.extension();
        save_png(image,output);app.renderer->present(false);
    }
    app.settings.widescreen=false;app.configure_graphics();
    if(!SDL_SetWindowSize(app.window,960,720)) throw std::runtime_error(SDL_GetError());
    app.poll();
    std::array<double,2> source_rates{},display_rates{};
    for(unsigned test=0;test<2;++test) {
        app.settings.refresh_rate=test?144:120;app.playing=true;app.pacer={};app.source_pacer={};app.published_at=app.frames=0;
        app.settings.interpolation=false;const auto generated_before=app.renderer->stats().interpolated_frames;
        std::uint64_t started=0;
        for(unsigned source=0;source<=30;++source) {
            app.renderer->set_target();app.renderer->begin(source&1?0xFF16344A:0xFF345616);app.renderer->end();app.game_flip();
            if(!source) started=app.published_at;
        }
        source_rates[test]=30*1000000000.0/(app.published_at-started);
        display_rates[test]=(app.frames-1)*1000000000.0/(app.last_frame-app.first_frame);
        if(std::abs(source_rates[test]-app.settings.refresh_rate)>app.settings.refresh_rate*0.08 || std::abs(display_rates[test]-app.settings.refresh_rate)>app.settings.refresh_rate*0.08 || app.frames!=31 || app.renderer->stats().interpolated_frames!=generated_before)
            throw std::runtime_error(std::format("Original-frame timing: source {:.2f}, display {:.2f}",source_rates[test],display_rates[test]));
        app.playing=false;app.published_at=0;
    }
    if(const auto messages=app.renderer->errors();!messages.empty()) throw std::runtime_error("Settings validation: "+messages.front());
    // Exercise resolution changes before a game starts, without guest execution.
    app.settings.resolution_scale=2;app.configure_graphics();app.renderer->begin();app.renderer->end();
    const auto scaled=app.renderer->readback();
    if(scaled.width!=1280 || scaled.height!=960) throw std::runtime_error("Selected internal resolution was not applied");
    if(const auto messages=app.renderer->errors();!messages.empty()) throw std::runtime_error("Resolution validation: "+messages.front());
    app.settings.resolution_scale=1;app.configure_graphics();
    if(!SDL_SetWindowSize(app.window,640,480)) throw std::runtime_error(SDL_GetError());
    app.poll();
    auto& renderer=*app.renderer;
    const auto pattern=[](int dx,int dy,bool occlusion=false,bool overlay=true) {
        std::vector<std::byte> bytes(640*480*4);
        for(int y=0;y<480;++y) for(int x=0;x<640;++x) {
            const auto value=static_cast<unsigned>(std::clamp(128+31*std::sin((x-dx)*0.071)+34*std::cos((y-dy)*0.083)+
                42*std::sin((x-dx)*0.043+(y-dy)*0.057),0.0,255.0));
            const bool hud=overlay && x>=40 && x<120 && y>=20 && y<44;
            const auto offset=(y*640+x)*4;
            bytes[offset]=bytes[offset+1]=bytes[offset+2]=std::byte(hud?255:value);bytes[offset+3]=std::byte{255};
            if(occlusion && x>=240 && x<320 && y>=180 && y<260) {bytes[offset]=bytes[offset+2]=std::byte{0};bytes[offset+1]=std::byte{255};}
        }
        return bytes;
    };
    const auto original=pattern(0,0),translated=pattern(16,8),middle=pattern(8,4);
    const auto upload=[&](const std::vector<std::byte>& bytes) {return renderer.upload({640,480,TextureFormat::argb8_linear,bytes});};
    const auto previous_texture=upload(original),current_texture=upload(translated),occluded_texture=upload(pattern(16,8,true));
    const auto draw=[&](std::uint32_t texture) {
        RenderState state;state.primitive=8;state.textures[0]=texture;state.texture_modes[0]=1;state.final_inputs={8,0x1800};
        constexpr Float4 p[]={{-1,1,0,1},{1,1,0,1},{1,-1,0,1},{-1,-1,0,1}};
        std::array<Vertex,4> quad{};for(unsigned i=0;i<4;++i) {quad[i].position=p[i];quad[i].uv[0]={(i==1 || i==2)?1.0f:0.0f,i>=2?1.0f:0.0f,0,1};}
        renderer.set_target();renderer.prepare(state);renderer.begin();renderer.draw(state,quad);renderer.end();
    };
    const auto read=[&](float phase,std::string_view name) {
        if(!renderer.display(phase)) throw std::runtime_error("Interpolation did not display an image");
        auto image=renderer.read_display();renderer.present(false);
        auto output=path.parent_path()/(path.stem().string()+"-"+std::string(name)+".png");save_png(image,output);return image;
    };
    renderer.interpolation(true);draw(previous_texture);renderer.publish_frame(true);
    const auto before=read(1,"previous");draw(current_texture);
    using Microsoft::WRL::ComPtr;
    ComPtr<ID3D11Query> timer,start,finish;auto* device=renderer.native_device();auto* context=renderer.native_context();
    D3D11_QUERY_DESC query{D3D11_QUERY_TIMESTAMP_DISJOINT,0};
    if(FAILED(device->CreateQuery(&query,&timer))) throw std::runtime_error("Interpolation timing query unavailable");
    query.Query=D3D11_QUERY_TIMESTAMP;
    if(FAILED(device->CreateQuery(&query,&start)) || FAILED(device->CreateQuery(&query,&finish))) throw std::runtime_error("Interpolation timestamp query unavailable");
    const auto auxiliary_target=renderer.create_target(32,16);renderer.set_target(auxiliary_target);renderer.begin(0xFF00FF00);renderer.end();
    context->Begin(timer.Get());context->End(start.Get());renderer.publish_frame(false);
    renderer.display(0.5f);context->End(finish.Get());context->End(timer.Get());
    const auto interpolated=renderer.read_display();renderer.present(false);
    const auto intact_auxiliary=renderer.readback();
    if(intact_auxiliary.width!=32 || intact_auxiliary.height!=16 || unsigned(intact_auxiliary.rgba[1])!=255)
        throw std::runtime_error("Extra presentation changed an active game render target");
    auto midpoint=path.parent_path()/(path.stem().string()+"-interpolated.png");save_png(interpolated,midpoint);
    D3D11_QUERY_DATA_TIMESTAMP_DISJOINT timing{};UINT64 first=0,last=0;
    const auto timeout=SDL_GetTicksNS()+5000000000ULL;
    while(context->GetData(timer.Get(),&timing,sizeof(timing),0)==S_FALSE && SDL_GetTicksNS()<timeout) SDL_Delay(1);
    if(timing.Disjoint || !timing.Frequency || context->GetData(start.Get(),&first,sizeof(first),0)!=S_OK || context->GetData(finish.Get(),&last,sizeof(last),0)!=S_OK)
        throw std::runtime_error("Interpolation GPU timing unavailable");
    const auto first_gpu_ms=double(last-first)*1000/timing.Frequency;
    const auto exact_previous=read(0,"endpoint-previous"),exact_current=read(1,"endpoint-current");
    if(interpolated.width!=640 || interpolated.height!=480 || before.rgba!=exact_previous.rgba || exact_current.rgba!=translated)
        throw std::runtime_error("Interpolation changed an endpoint or the window dimensions");
    // Report steady GPU work separately from first-use driver setup.
    draw(previous_texture);renderer.publish_frame(true);draw(current_texture);
    context->Begin(timer.Get());context->End(start.Get());renderer.publish_frame(false);
    renderer.display(0.5f);context->End(finish.Get());context->End(timer.Get());
    renderer.read_display();renderer.present(false);
    if(context->GetData(timer.Get(),&timing,sizeof(timing),0)!=S_OK || timing.Disjoint || !timing.Frequency ||
        context->GetData(start.Get(),&first,sizeof(first),0)!=S_OK || context->GetData(finish.Get(),&last,sizeof(last),0)!=S_OK)
        throw std::runtime_error("Steady interpolation GPU timing unavailable");
    const auto gpu_ms=double(last-first)*1000/timing.Frequency;
    double interpolated_error=0,latest_error=0,blend_error=0;
    for(unsigned y=80;y<448;++y) for(unsigned x=32;x<608;++x) {
        const auto offset=(y*640+x)*4;
        const auto expected=unsigned(middle[offset]);
        interpolated_error+=std::abs(int(unsigned(interpolated.rgba[offset]))-int(expected));
        latest_error+=std::abs(int(unsigned(exact_current.rgba[offset]))-int(expected));
        blend_error+=std::abs((int(unsigned(exact_previous.rgba[offset]))+int(unsigned(exact_current.rgba[offset])))/2-int(expected));
    }
    if(interpolated_error>=latest_error*0.55 || interpolated_error>=blend_error*0.8)
        throw std::runtime_error(std::format("Motion interpolation errors: generated {}, latest {}, blend {}",interpolated_error,latest_error,blend_error));
    const auto hud=(30*640+70)*4;
    if(unsigned(interpolated.rgba[hud])!=255) throw std::runtime_error("Stationary HUD moved during interpolation");
    // A newly revealed object must use current pixels when motion is unreliable.
    draw(current_texture);renderer.publish_frame(true);draw(occluded_texture);renderer.publish_frame(false);
    const auto occluded=read(0.5f,"occlusion");const auto object=(220*640+280)*4;
    if(unsigned(occluded.rgba[object])>2 || unsigned(occluded.rgba[object+1])<253) throw std::runtime_error("Disocclusion did not retain current pixels");
    renderer.set_target();
    renderer.begin(0xFF000000);renderer.end();renderer.publish_frame(true);
    renderer.begin(0xFFFFFFFF);renderer.end();renderer.publish_frame(false);
    const auto cut=read(0.5f,"scene-cut");
    if(std::ranges::any_of(cut.rgba,[](std::byte v){return v!=std::byte{255};})) throw std::runtime_error("Scene cut blended unrelated frames");
    renderer.begin(0xFF000000);renderer.end();renderer.publish_frame(true);
    const auto reset=read(0.5f,"reset");
    if(unsigned(reset.rgba[0])!=0) throw std::runtime_error("Interpolation reset retained old history");
    // Smooth images hide blurred detail and mixed foreground/background motion.
    const auto detail=[&](int dx,int dy) {
        auto bytes=pattern(dx,dy,false,false);
        for(int y=0;y<480;++y) for(int x=0;x<640;++x) {
            const auto offset=(y*640+x)*4;
            if(y>=80) {
                const int checker=(((x-dx+1200)/3+(y-dy+1200)/3)&1)?28:-28;
                bytes[offset]=bytes[offset+1]=bytes[offset+2]=std::byte(std::clamp(int(unsigned(bytes[offset]))+checker,0,255));
            } else if(x>=40 && x<120 && y>=20 && y<44 && (x%6==0 || y%6==0)) {
                bytes[offset]=bytes[offset+1]=bytes[offset+2]=std::byte{255};
            }
        }
        return bytes;
    };
    const auto boundary=[&](int dx,int dy) {
        auto bytes=pattern(dx,dy);
        for(int y=160+dy;y<280+dy;++y) for(int x=220+2*dx;x<340+2*dx;++x) {
            const auto offset=(y*640+x)*4;
            bytes[offset]=std::byte{35};bytes[offset+1]=std::byte(170+int(15*std::sin((x-2*dx)*0.053)+12*std::cos((y-dy)*0.077)));bytes[offset+2]=std::byte{40};
        }
        return bytes;
    };
    unsigned detail_hud_error=0,boundary_perimeter=0;
    const auto quality=[&](const auto& source,const auto& destination,const auto& expected,std::string_view name) {
        const auto old_texture=upload(source),new_texture=upload(destination);
        draw(old_texture);renderer.publish_frame(true);draw(new_texture);renderer.publish_frame(false);
        const auto generated=read(0.5f,name);
        auto reference=path.parent_path()/(path.stem().string()+"-"+std::string(name)+"-reference.png");
        Image reference_image{640,480,expected};
        for(std::size_t offset=0;offset<reference_image.rgba.size();offset+=4) std::swap(reference_image.rgba[offset],reference_image.rgba[offset+2]);
        save_png(reference_image,reference);
        if(name=="fine-detail") for(unsigned y=22;y<42;++y) for(unsigned x=42;x<118;++x) if(x%6==0 || y%6==0) {
            const auto offset=(y*640+x)*4;
            detail_hud_error=std::max(detail_hud_error,unsigned(std::abs(int(unsigned(generated.rgba[offset]))-int(unsigned(expected[offset])))));
        }
        std::array<double,3> errors{};
        const unsigned left=name=="moving-edge"?208:32,right=name=="moving-edge"?400:608;
        const unsigned top=name=="moving-edge"?140:80,bottom=name=="moving-edge"?304:448;
        if(name=="moving-edge") {
            const auto foreground=[&](unsigned x,unsigned y) {
                const auto offset=(y*640+x)*4;
                return unsigned(generated.rgba[offset])<80 && unsigned(generated.rgba[offset+1])>80 && unsigned(generated.rgba[offset+2])<80;
            };
            for(unsigned y=top+1;y<bottom;++y) for(unsigned x=left+1;x<right;++x)
                boundary_perimeter+=(foreground(x,y)!=foreground(x-1,y))+(foreground(x,y)!=foreground(x,y-1));
        }
        for(unsigned y=top;y<bottom;++y) for(unsigned x=left;x<right;++x) for(unsigned c=0;c<3;++c) {
            const auto offset=(y*640+x)*4,channel=c==0?2U:c==2?0U:1U;const auto value=int(unsigned(expected[offset+channel]));
            errors[0]+=std::abs(int(unsigned(generated.rgba[offset+c]))-value);
            errors[1]+=std::abs(int(unsigned(destination[offset+channel]))-value);
            errors[2]+=std::abs((int(unsigned(source[offset+channel]))+int(unsigned(destination[offset+channel])))/2-value);
        }
        for(auto& error:errors) error/=(bottom-top)*(right-left)*3;
        renderer.release(old_texture);renderer.release(new_texture);return errors;
    };
    const auto detail_errors=quality(detail(0,0),detail(14,8),detail(7,4),"fine-detail");
    const auto boundary_errors=quality(boundary(0,0),boundary(16,8),boundary(8,4),"moving-edge");
    // Equal luminance does not make two differently coloured scenes related.
    renderer.set_target();renderer.begin(0xFFFF0000);renderer.end();renderer.publish_frame(true);
    renderer.begin(0xFF008200);renderer.end();renderer.publish_frame(false);
    const auto colour_cut=read(0.25f,"colour-cut");
    if(unsigned(colour_cut.rgba[0])!=0 || unsigned(colour_cut.rgba[1])!=130 || unsigned(colour_cut.rgba[2])!=0)
        throw std::runtime_error(std::format("Equal-luminance scene cut retained the old scene; detail errors {:.3f}/{:.3f}/{:.3f}, boundary errors {:.3f}/{:.3f}/{:.3f}",
            detail_errors[0],detail_errors[1],detail_errors[2],boundary_errors[0],boundary_errors[1],boundary_errors[2]));
    if(detail_errors[0]>=1 || boundary_errors[0]>=1 || detail_hud_error>1 || boundary_perimeter>720)
        throw std::runtime_error(std::format("Interpolation detail/edge regression: detail {:.3f}, boundary {:.3f}, HUD {}, perimeter {}",
            detail_errors[0],boundary_errors[0],detail_hud_error,boundary_perimeter));
    std::string generated_rate="null",generated_source_rate="null",backpressure_source_rate="null";
    if(!warp) {
        // Validate the app's actual extra-frame path, as well as the GPU pass.
        // Software rendering above is a correctness check, not a speed target.
        renderer.interpolation(false);renderer.interpolation(true);
        app.settings.interpolation=true;app.settings.refresh_rate=144;app.playing=true;
        app.pacer={};app.source_pacer={};app.published_at=app.frames=app.first_frame=app.last_frame=0;
        const auto generated_before=renderer.stats().interpolated_frames;
        std::uint64_t started=0;
        for(unsigned source=0;source<12;++source) {
            draw(source&1?current_texture:previous_texture);app.game_flip();
            if(!source) started=app.published_at;
        }
        const auto source_hz=11*1000000000.0/(app.published_at-started);
        const auto display_hz=(app.frames-1)*1000000000.0/(app.last_frame-app.first_frame);
        if(renderer.stats().interpolated_frames-generated_before<12 || std::abs(source_hz-60)>4 || std::abs(display_hz-144)>12)
            throw std::runtime_error(std::format("Generated presentation: source {:.2f}, display {:.2f}",source_hz,display_hz));
        generated_rate=std::format("{:.3f}",display_hz);generated_source_rate=std::format("{:.3f}",source_hz);
        app.playing=false;app.published_at=0;
        // Saturate presentation while doing nine 1 ms slices of work per
        // source frame. A display wait must not starve that simulated work.
        app.settings.vsync=true;app.settings.refresh_rate=1000;app.playing=true;
        app.pacer={};app.source_pacer={};app.published_at=0;
        for(unsigned source=0;source<31;++source) {
            for(unsigned slice=0;slice<9;++slice) {
                app.poll();const auto until=SDL_GetTicksNS()+1000000;
                while(SDL_GetTicksNS()<until) {}
            }
            draw(source&1?current_texture:previous_texture);app.game_flip();
            if(!source) started=app.published_at;
        }
        const auto busy_source_hz=30*1000000000.0/(app.published_at-started);
        if(std::abs(busy_source_hz-60)>4)
            throw std::runtime_error(std::format("Presentation backpressure starved source work: {:.2f} Hz",busy_source_hz));
        backpressure_source_rate=std::format("{:.3f}",busy_source_hz);
        app.settings.vsync=false;app.playing=false;app.published_at=0;
    }
    // Apply a live opt-out through the same settings path as the checkbox.
    app.settings.interpolation=false;app.settings.refresh_rate=144;app.playing=true;app.show_settings=true;
    renderer.display();app.settings_interface();renderer.present(false);app.show_settings=false;
    const auto generated_before_opt_out=renderer.stats().interpolated_frames,presented_before_opt_out=app.frames;
    std::uint64_t opt_out_started=0;
    for(unsigned source=0;source<12;++source) {
        draw(source&1?current_texture:previous_texture);app.game_flip();
        if(!source) opt_out_started=app.published_at;
        const auto presented=app.frames;app.pacer.next=0;app.poll();
        if(app.frames!=presented) throw std::runtime_error("Disabled interpolation presented an extra image between original frames");
    }
    const auto opt_out_source_hz=11*1000000000.0/(app.published_at-opt_out_started);
    if(std::abs(opt_out_source_hz-144)>12 || app.frames-presented_before_opt_out!=12 || renderer.stats().interpolated_frames!=generated_before_opt_out)
        throw std::runtime_error(std::format("Live interpolation opt-out: source {:.2f}, displayed {}, generated {}",opt_out_source_hz,app.frames-presented_before_opt_out,renderer.stats().interpolated_frames-generated_before_opt_out));
    app.playing=false;app.published_at=0;
    // Exercise the actual opt-in recorder, source/display separation, GPU
    // query association and no-boot export through the same app path.
    renderer.interpolation(false);app.settings.interpolation=false;app.settings.refresh_rate=60;app.playing=true;
    app.pacer={};app.source_pacer={};app.published_at=0;
    app.toggle_performance();
    if(!app.performance.active()) throw std::runtime_error("Performance shortcut did not start recording");
    for(unsigned source=0;source<36;++source) {
        if(source==2) for(unsigned extra=0;extra<70;++extra) draw(current_texture); // Exercise explicit GPU timing capacity loss.
        draw(source&1?current_texture:previous_texture);app.game_flip();app.poll();
    }
    renderer.performance(nullptr);app.performance.stop();
    if(!app.performance.has_gpu_samples()) throw std::runtime_error("Nonblocking performance GPU queries produced no samples");
    auto performance_path=path;performance_path+=".performance";app.performance.save(performance_path);
    app.playing=false;app.published_at=0;
    if(renderer.stats().shader_compilations) throw std::runtime_error("Settings or interpolation compiled shaders during runtime");
    const auto messages=app.renderer->errors();
    if(!messages.empty()) throw std::runtime_error("SDL3/ImGui validation: "+messages.front());
    app.settings.volume=37;app.settings_dirty=true;app.launch=true;app.binding_action=15;
    SDL_Event close{};close.type=SDL_EVENT_WINDOW_CLOSE_REQUESTED;app.event(close);
    Preferences closed(true,preferences);
    if(app.running || app.launch || app.binding_action!=-1 || app.settings_dirty || closed.volume!=37)
        throw std::runtime_error("Exit did not cancel launch and save pending settings");
    std::filesystem::remove(preferences);
    return std::format("{{\"format\":\"b2-window-check-v3\",\"passed\":true,\"backend\":\"SDL3/D3D11/ImGui\",\"adapter\":{},\"default_refresh_rate\":60,"
        "\"bindings_and_settings\":true,\"automatic_settings_save\":true,\"restart_request\":true,\"exit_request\":true,\"widescreen_presentation\":true,\"automatic_interpolation\":true,\"interpolation_opt_out\":true,\"source_hz_after_opt_out\":{:.3f},\"frame_counter\":true,\"counter_image\":{},\"escape_toggle\":true,\"resolution_change\":true,"
        "\"pacing_60_hz\":{:.3f},\"pacing_120_hz\":{:.3f},\"source_at_120_hz\":{:.3f},\"source_at_144_hz\":{:.3f},\"display_120_hz\":{:.3f},\"display_144_hz\":{:.3f},\"generated_display_hz\":{},\"source_hz_with_interpolation\":{},\"source_hz_under_presentation_backpressure\":{},"
        "\"performance_capture\":true,\"interpolation\":{{\"motion\":true,\"endpoints\":true,\"stationary_hud\":true,\"disocclusion\":true,\"scene_cut\":true,\"colour_cut\":true,\"reset\":true,\"first_gpu_ms\":{:.3f},\"gpu_ms\":{:.3f},\"mean_error\":{:.3f},\"detail_errors\":[{:.3f},{:.3f},{:.3f}],\"detail_hud_error\":{},\"boundary_errors\":[{:.3f},{:.3f},{:.3f}],\"boundary_perimeter\":{},\"boundary_reference_perimeter\":480,\"shader_compilations\":0}},\"game_booted\":false,\"game_fps_established\":false,\"images\":{}}}",
        json(renderer.adapter()),opt_out_source_hz,json(utf8(counter_image.wstring())),measured[0],measured[1],source_rates[0],source_rates[1],display_rates[0],display_rates[1],generated_rate,generated_source_rate,backpressure_source_rate,first_gpu_ms,gpu_ms,interpolated_error/(368*576),detail_errors[0],detail_errors[1],detail_errors[2],detail_hud_error,boundary_errors[0],boundary_errors[1],boundary_errors[2],boundary_perimeter,images.str());
}
}
