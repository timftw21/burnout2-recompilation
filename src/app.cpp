#include "app.h"
#include "input.h"
#include "file.h"
#include "xbox.h"
#include "diagnostics.h"
#include "custom_music.h"
#include "mods.h"
#include "achievements.h"
#include <SDL3/SDL.h>
#include <imgui.h>
#include <imgui_impl_sdl3.h>
#include <imgui_impl_dx11.h>
#include "host.h"
#ifdef _WIN32
#include <commdlg.h>
#elif defined(__linux__)
#include <fontconfig/fontconfig.h>
#include <dlfcn.h>
#endif
#include <d3d11.h>
#include "com_ptr.h"
#include <algorithm>
#include <atomic>
#include <bit>
#include <charconv>
#include <cfloat>
#include <cmath>
#include <deque>
#include <fstream>
#include <future>
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
    int performance=-1,benchmark=-1;bool capture=false;
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
            const bool diagnostic=name=="performance" || name=="benchmark" || name=="capture";
            const auto axis=SDL_GetGamepadAxisFromString(name.c_str());const auto button=SDL_GetGamepadButtonFromString(name.c_str());
            if((!diagnostic && axis==SDL_GAMEPAD_AXIS_INVALID && button==SDL_GAMEPAD_BUTTON_INVALID) ||
                (axis==SDL_GAMEPAD_AXIS_INVALID?(value!=0 && value!=1):(value<-32768 || value>32767)))
                throw std::runtime_error("Invalid controller name or value in script");
            const auto at=milliseconds*1000000;if(!events.empty() && at<events.back().at) throw std::runtime_error("Controller events are out of order");
            events.push_back({at,diagnostic?(name=="performance"?-1:name=="benchmark"?-2:-3):axis!=SDL_GAMEPAD_AXIS_INVALID?int(axis):int(button),value,axis!=SDL_GAMEPAD_AXIS_INVALID});
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
            if(event.control==-3) {capture=event.value!=0;continue;}
            const auto valid=event.axis?SDL_SetJoystickVirtualAxis(joystick,event.control,static_cast<Sint16>(event.value)):
                SDL_SetJoystickVirtualButton(joystick,event.control,event.value!=0);
            if(!valid) throw std::runtime_error(SDL_GetError());changed=true;
        }
        if(changed) SDL_UpdateJoysticks();
    }
};
struct Preferences {
    std::array<char,4096> xbe{},disc{},music_folder{};
    std::vector<std::string> enabled_mods;
    bool vsync=false,fullscreen=false,keyboard=true,muted=false,show_fps=false,motion_blur=true;
    int aspect_ratio=0; // 0=4:3, 1=16:9, 2=21:9.
    int refresh_rate=60,volume=100,resolution_scale=1,antialiasing=0,anisotropy=0;
    int ambient_occlusion=0,ambient_occlusion_strength=35;
    bool hdr=false;
    int hdr_paper_white=200,hdr_peak=0;
    bool custom_music=false,loop_track=false,mute_on_focus_loss=false;
    bool skip_intro=false,fast_load=false,clean_hud=true;
    int draw_distance=1;
    KeyboardBindings bindings=default_keyboard_bindings();
    ControllerProfiles controller_bindings=default_controller_profiles();
    explicit Preferences(bool load=true,const std::filesystem::path& path="data/local/settings.cfg") {
        copy_text(xbe.data(),xbe.size(),"data/local/default.xbe");
        copy_text(disc.data(),disc.size(),"Burnout 2/Burnout 2 - Point of Impact (USA).xiso.iso");
        if(!load) return;
        std::ifstream file(path);std::string line;
        bool legacy_distance=false,has_distance=false,has_aspect=false;
        while(std::getline(file,line)) {
            if(line.size()>8192) throw std::runtime_error("Settings line exceeds its bound");
            const auto split=line.find('=');if(split==std::string::npos) continue;
            const auto key=line.substr(0,split),value=line.substr(split+1);
            const auto number=[&](int& destination,int minimum,int maximum) {
                int candidate=0;const auto result=std::from_chars(value.data(),value.data()+value.size(),candidate);
                if(result.ec==std::errc{} && result.ptr==value.data()+value.size() && candidate>=minimum && candidate<=maximum) destination=candidate;
            };
            if(key=="xbe" || key=="disc" || key=="music_folder") {
                auto& destination=key=="xbe"?xbe:key=="disc"?disc:music_folder;
                if(value.size()<destination.size()) copy_text(destination.data(),destination.size(),value.c_str());
            } else if(key=="vsync") vsync=value=="1";
            else if(key=="hdr") hdr=value=="1";
            else if(key=="hdr_paper_white") number(hdr_paper_white,80,500);
            else if(key=="hdr_peak") {if(value=="0") hdr_peak=0;else number(hdr_peak,200,10000);}
            else if(key=="fullscreen") fullscreen=value=="1";
            else if(key=="widescreen" && !has_aspect) aspect_ratio=value=="1"?1:0;
            else if(key=="aspect_ratio") {
                int candidate=-1;number(candidate,0,2);
                if(candidate>=0) {aspect_ratio=candidate;has_aspect=true;}
            }
            else if(key=="motion_blur") motion_blur=value=="1";
            else if(key=="keyboard") keyboard=value=="1";
            else if(key=="muted") muted=value=="1";
            else if(key=="mute_on_focus_loss") mute_on_focus_loss=value=="1";
            else if(key=="custom_music") custom_music=value=="1";
            else if(key=="loop_track") loop_track=value=="1";
            else if(key=="skip_intro") skip_intro=value=="1";
            else if(key=="fast_load") fast_load=value=="1";
            else if(key=="clean_hud") clean_hud=value=="1";
            else if(key=="mod" && valid_mod_id(value) && enabled_mods.size()<128 &&
                    std::ranges::find(enabled_mods,value)==enabled_mods.end()) enabled_mods.push_back(value);
            else if(key=="highest_lod" || key=="unlimited_distance") legacy_distance|=value=="1";
            else if(key=="draw_distance") {
                int candidate=0;number(candidate,1,4);
                if(candidate==1 || candidate==2 || candidate==4) {draw_distance=candidate;has_distance=true;}
            }
            else if(key=="show_fps") show_fps=value=="1";
            else if(key=="refresh_rate") {if(value=="0") refresh_rate=0;else number(refresh_rate,30,1000);}
            else if(key=="resolution_scale") number(resolution_scale,1,4);
            else if(key=="antialiasing") number(antialiasing,0,6);
            else if(key=="ambient_occlusion") number(ambient_occlusion,0,3);
            else if(key=="ambient_occlusion_strength") number(ambient_occlusion_strength,0,100);
            else if(key=="anisotropy") {
                auto candidate=anisotropy;number(candidate,0,16);
                if(!candidate || std::has_single_bit(unsigned(candidate))) anisotropy=candidate;
            }
            else if(key=="volume") number(volume,0,100);
            else if(key.starts_with("key_")) {
                for(unsigned i=0;i<input_actions.size();++i) if(key.substr(4)==input_actions[i].id) {
                    auto candidate=bindings[i];number(candidate,0,SDL_SCANCODE_COUNT-1);
                    if(valid_keyboard_binding(candidate)) bindings[i]=candidate;
                }
            } else if(key.size()>5 && key.starts_with("pad") && key[3]>='1' && key[3]<='4' && key[4]=='_') {
                for(unsigned i=0;i<input_actions.size();++i) if(key.substr(5)==input_actions[i].id)
                    if(const auto binding=parse_controller_binding(value)) controller_bindings[key[3]-'1'][i]=*binding;
            }
        }
        if(!has_distance && legacy_distance) draw_distance=2;
    }
    void save(const std::filesystem::path& path="data/local/settings.cfg") const {
        if(!path.parent_path().empty()) std::filesystem::create_directories(path.parent_path());
        std::ofstream file(path,std::ios::trunc);
        file<<"xbe="<<xbe.data()<<"\ndisc="<<disc.data()<<"\nvsync="<<vsync<<"\nfullscreen="<<fullscreen<<"\nkeyboard="<<keyboard
            <<"\nrefresh_rate="<<refresh_rate<<"\nvolume="<<volume<<"\nmuted="<<muted<<"\nmute_on_focus_loss="<<mute_on_focus_loss
            <<"\nresolution_scale="<<resolution_scale<<"\nshow_fps="<<show_fps<<"\nwidescreen="<<(aspect_ratio!=0)<<"\naspect_ratio="<<aspect_ratio
            <<"\nantialiasing="<<antialiasing<<"\nanisotropy="<<anisotropy
            <<"\nambient_occlusion="<<ambient_occlusion<<"\nambient_occlusion_strength="<<ambient_occlusion_strength
            <<"\nhdr="<<hdr<<"\nhdr_paper_white="<<hdr_paper_white<<"\nhdr_peak="<<hdr_peak
            <<"\nskip_intro="<<skip_intro<<"\nfast_load="<<fast_load<<"\nclean_hud="<<clean_hud<<"\nmotion_blur="<<motion_blur<<"\ndraw_distance="<<draw_distance
            <<"\ncustom_music="<<custom_music<<"\nloop_track="<<loop_track<<"\nmusic_folder="<<music_folder.data()<<'\n';
        for(unsigned i=0;i<input_actions.size();++i) file<<"key_"<<input_actions[i].id<<'='<<bindings[i]<<'\n';
        for(unsigned port=0;port<controller_bindings.size();++port) for(unsigned i=0;i<input_actions.size();++i)
            file<<"pad"<<port+1<<'_'<<input_actions[i].id<<'='<<controller_binding_id(controller_bindings[port][i])<<'\n';
        for(const auto& id:enabled_mods) file<<"mod="<<id<<'\n';
        if(!file) throw std::runtime_error("Could not save settings");
    }
    void select_refresh_rate(int rate) {refresh_rate=rate;}
    float aspect() const {return aspect_ratio==2?21.0f/9:aspect_ratio==1?16.0f/9:4.0f/3;}
    HdrSettings hdr_options() const {return {hdr,unsigned(hdr_paper_white),unsigned(hdr_peak)};}
    float output_gain(bool focused) const {return muted || (mute_on_focus_loss && !focused)?0:volume/100.0f;}

};
void setting_help(const char* text) {
    if(ImGui::IsItemHovered(ImGuiHoveredFlags_ForTooltip|ImGuiHoveredFlags_AllowWhenDisabled) && ImGui::BeginTooltip()) {
        ImGui::PushTextWrapPos(ImGui::GetFontSize()*28);
        ImGui::TextUnformatted(text);ImGui::PopTextWrapPos();ImGui::EndTooltip();
    }
}
ImFont* settings_theme(ImFont*& italic) {
    auto& io=ImGui::GetIO();
    io.ConfigFlags|=ImGuiConfigFlags_NavEnableKeyboard|ImGuiConfigFlags_NavEnableGamepad;
    io.IniFilename=nullptr;
    ImFont* heading=nullptr;
#ifdef _WIN32
    // Use the platform's UI typeface without shipping Windows font files.
    std::array<wchar_t,MAX_PATH> directory{};
    const auto length=GetWindowsDirectoryW(directory.data(),unsigned(directory.size()));
    if(!length || length>=directory.size()) throw std::runtime_error("Cannot locate Windows fonts.");
    const auto fonts=std::filesystem::path(directory.data())/"Fonts";
    for(const auto& names:std::array<std::array<const char*,3>,2>{{{"segoeui.ttf","segoeuib.ttf","segoeuii.ttf"},{"arial.ttf","arialbd.ttf","ariali.ttf"}}}) {
        if(!std::filesystem::exists(fonts/names[0]) || !std::filesystem::exists(fonts/names[1]) || !std::filesystem::exists(fonts/names[2])) continue;
        io.FontDefault=io.Fonts->AddFontFromFileTTF(path_text((fonts/names[0])).c_str(),18);
        heading=io.Fonts->AddFontFromFileTTF(path_text((fonts/names[1])).c_str(),18);
        italic=io.Fonts->AddFontFromFileTTF(path_text((fonts/names[2])).c_str(),18);
        break;
    }
    if(!io.FontDefault || !heading || !italic) throw std::runtime_error("Cannot load a Windows UI font.");
#else
#ifdef __APPLE__
    const auto regular=std::filesystem::path("/System/Library/Fonts/Supplemental/Arial.ttf");
    const auto bold=std::filesystem::path("/System/Library/Fonts/Supplemental/Arial Bold.ttf");
    const auto slanted=std::filesystem::path("/System/Library/Fonts/Supplemental/Arial Italic.ttf");
    if(std::filesystem::exists(regular) && std::filesystem::exists(bold)) {
        io.FontDefault=io.Fonts->AddFontFromFileTTF(regular.c_str(),18);heading=io.Fonts->AddFontFromFileTTF(bold.c_str(),18);
    }
    if(std::filesystem::exists(slanted)) italic=io.Fonts->AddFontFromFileTTF(slanted.c_str(),18);
#else
    // Resolve the desktop's fonts instead of assuming Debian font directories.
    const auto font=[&](const char* name)->ImFont* {
        using Pattern=std::unique_ptr<FcPattern,decltype(&FcPatternDestroy)>;
        Pattern request(FcNameParse(reinterpret_cast<const FcChar8*>(name)),FcPatternDestroy);
        if(!request || !FcConfigSubstitute(nullptr,request.get(),FcMatchPattern)) return nullptr;
        FcDefaultSubstitute(request.get());FcResult result=FcResultNoMatch;
        Pattern match(FcFontMatch(nullptr,request.get(),&result),FcPatternDestroy);
        FcBool outline=FcFalse;
        if(!match || FcPatternGetBool(match.get(),FC_OUTLINE,0,&outline)!=FcResultMatch || !outline) return nullptr;
        FcChar8* file=nullptr;
        if(FcPatternGetString(match.get(),FC_FILE,0,&file)!=FcResultMatch) return nullptr;
        int index=0;FcPatternGetInteger(match.get(),FC_INDEX,0,&index);
        ImFontConfig config;config.FontNo=index&0xFFFF; // Collection face, excluding any variable-font instance.
        return io.Fonts->AddFontFromFileTTF(reinterpret_cast<const char*>(file),18,&config);
    };
    io.FontDefault=font("sans-serif:outline=true");heading=font("sans-serif:bold:outline=true");
    italic=font("sans-serif:slant=italic:outline=true");
#endif
    if(!io.FontDefault || !heading) {io.FontDefault=io.Fonts->AddFontDefaultVector();heading=io.FontDefault;}
    if(!italic) italic=io.FontDefault;
#endif
    ImGui::StyleColorsDark();auto& style=ImGui::GetStyle();
    style.FontSizeBase=18;style.WindowPadding={24,20};style.FramePadding={12,8};style.ItemSpacing={12,12};
    style.ItemInnerSpacing={10,6};style.CellPadding={10,7};style.IndentSpacing=20;
    style.WindowRounding=14;style.ChildRounding=10;style.FrameRounding=6;style.PopupRounding=8;
    style.GrabRounding=4;style.ScrollbarRounding=6;style.ScrollbarSize=10;style.GrabMinSize=12;
    style.WindowBorderSize=1;style.ChildBorderSize=1;style.FrameBorderSize=1;
    auto* c=style.Colors;
    c[ImGuiCol_Text]={.93f,.94f,.96f,1};c[ImGuiCol_TextDisabled]={.60f,.65f,.71f,1};
    c[ImGuiCol_WindowBg]={.055f,.067f,.086f,1};c[ImGuiCol_ChildBg]={.078f,.094f,.118f,1};
    c[ImGuiCol_PopupBg]={.09f,.11f,.14f,1};c[ImGuiCol_Border]={.19f,.23f,.28f,1};c[ImGuiCol_BorderShadow]={0,0,0,0};
    c[ImGuiCol_ModalWindowDimBg]={0,0,0,.65f};
    c[ImGuiCol_FrameBg]={.12f,.145f,.18f,1};c[ImGuiCol_FrameBgHovered]={.18f,.21f,.26f,1};
    c[ImGuiCol_FrameBgActive]={.21f,.25f,.30f,1};
    c[ImGuiCol_TitleBg]=c[ImGuiCol_TitleBgActive]=c[ImGuiCol_TitleBgCollapsed]=c[ImGuiCol_FrameBg];
    c[ImGuiCol_CheckboxSelectedBg]={.30f,.19f,.13f,1};c[ImGuiCol_InputTextCursor]={1,.49f,.19f,1};
    c[ImGuiCol_Button]={.16f,.19f,.23f,1};c[ImGuiCol_ButtonHovered]={.23f,.27f,.32f,1};
    c[ImGuiCol_ButtonActive]={.29f,.33f,.39f,1};
    c[ImGuiCol_Header]={.27f,.17f,.12f,1};c[ImGuiCol_HeaderHovered]={.23f,.19f,.17f,1};
    c[ImGuiCol_HeaderActive]={.36f,.23f,.15f,1};
    c[ImGuiCol_CheckMark]=c[ImGuiCol_SliderGrab]=c[ImGuiCol_NavCursor]={1,.49f,.19f,1};
    c[ImGuiCol_SliderGrabActive]={1,.65f,.34f,1};
    c[ImGuiCol_Separator]={.19f,.23f,.28f,1};c[ImGuiCol_SeparatorHovered]=c[ImGuiCol_SeparatorActive]={1,.49f,.19f,1};
    c[ImGuiCol_ScrollbarBg]={0,0,0,0};c[ImGuiCol_ScrollbarGrab]={.25f,.29f,.35f,1};
    c[ImGuiCol_ScrollbarGrabHovered]={.34f,.39f,.45f,1};c[ImGuiCol_ScrollbarGrabActive]={.45f,.50f,.56f,1};
    c[ImGuiCol_TableRowBg]={0,0,0,0};c[ImGuiCol_TableRowBgAlt]={1,1,1,.025f};
    c[ImGuiCol_TextSelectedBg]={1,.49f,.19f,.28f};c[ImGuiCol_PlotHistogram]={1,.49f,.19f,1};
    return heading;
}
Microsoft::WRL::ComPtr<ID3D11ShaderResourceView> settings_logo(ID3D11Device* device) {
#ifdef _WIN32
    const auto module=GetModuleHandleW(nullptr);
    const auto resource=FindResourceW(module,L"SETTINGS_LOGO",RT_RCDATA);
    const auto loaded=resource?LoadResource(module,resource):nullptr;
    const auto* bytes=loaded?LockResource(loaded):nullptr;
    if(!bytes) throw std::runtime_error("The settings logo resource is missing");
    std::unique_ptr<SDL_Surface,decltype(&SDL_DestroySurface)> pixels(
        SDL_LoadPNG_IO(SDL_IOFromConstMem(bytes,SizeofResource(module,resource)),true),SDL_DestroySurface);
#else
    std::unique_ptr<SDL_Surface,decltype(&SDL_DestroySurface)> pixels(
        SDL_LoadPNG((executable_path().parent_path()/"resources/b2.png").c_str()),SDL_DestroySurface);
#endif
    if(!pixels) throw std::runtime_error(SDL_GetError());
    if(pixels->format!=SDL_PIXELFORMAT_RGBA32) pixels.reset(SDL_ConvertSurface(pixels.get(),SDL_PIXELFORMAT_RGBA32));
    if(!pixels) throw std::runtime_error(SDL_GetError());
    D3D11_TEXTURE2D_DESC description{};
    description.Width=pixels->w;description.Height=pixels->h;description.MipLevels=description.ArraySize=1;
    description.Format=DXGI_FORMAT_R8G8B8A8_UNORM;description.SampleDesc.Count=1;
    description.Usage=D3D11_USAGE_IMMUTABLE;description.BindFlags=D3D11_BIND_SHADER_RESOURCE;
    const D3D11_SUBRESOURCE_DATA data{pixels->pixels,static_cast<UINT>(pixels->pitch),0};
    Microsoft::WRL::ComPtr<ID3D11Texture2D> texture;
    Microsoft::WRL::ComPtr<ID3D11ShaderResourceView> view;
    if(FAILED(device->CreateTexture2D(&description,&data,&texture)) ||
       FAILED(device->CreateShaderResourceView(texture.Get(),nullptr,&view)))
        throw std::runtime_error("Could not upload the settings logo");
    return view;
}
void settings_icon(ImDrawList& draw,int kind,ImVec2 p,ImU32 color) {
    const auto point=[&](float x,float y){return ImVec2{p.x+x,p.y+y};};
    const auto line=[&](float x,float y,float u,float v){draw.AddLine(point(x,y),point(u,v),color,1.6f);};
    switch(kind) {
    case 0:
        draw.AddCircle(point(10,10),8.5f,color,32,1.6f);
        draw.AddCircle(point(10,10),2.3f,color,16,1.6f);
        for(const float start:{-.95f,2.19f}) {
            draw.PathArcTo(point(10,10),5.5f,start,start+.7f,8);
            draw.PathStroke(color,0,1.6f);
        }
        break;
    case 1:for(int i=0;i<3;++i) {const float y=4.0f+6*i,x=i==1?13.0f:7.0f;line(1,y,19,y);draw.AddCircleFilled(point(x,y),2.5f,color);}break;
    case 2:line(2,7,6,7);line(6,7,11,3);line(11,3,11,18);line(11,18,6,14);line(6,14,2,14);line(2,14,2,7);
        draw.PathArcTo(point(11,10.5f),7,-.8f,.8f,10);draw.PathStroke(color,0,1.6f);break;
    case 3:draw.AddRect(point(1,2),point(19,15),color,2);line(10,15,10,19);line(5,19,15,19);break;
    case 4:
        draw.PathLineTo(point(8,2));
        draw.PathBezierCubicCurveTo(point(9,7),point(10,8),point(15,9));
        draw.PathBezierCubicCurveTo(point(10,10),point(9,11),point(8,16));
        draw.PathBezierCubicCurveTo(point(7,11),point(6,10),point(1,9));
        draw.PathBezierCubicCurveTo(point(6,8),point(7,7),point(8,2));
        draw.PathStroke(color,ImDrawFlags_Closed,1.6f);
        line(16,1,16,5);line(14,3,18,3);break;
    case 5:
        draw.PathLineTo(point(6,5));draw.PathLineTo(point(14,5));
        draw.PathBezierCubicCurveTo(point(16.5f,5),point(17.8f,6.4f),point(18.2f,9));
        draw.PathLineTo(point(19,14));
        draw.PathBezierCubicCurveTo(point(19.7f,18),point(17,18.8f),point(15,16.5f));
        draw.PathLineTo(point(12.5f,14));draw.PathLineTo(point(7.5f,14));draw.PathLineTo(point(5,16.5f));
        draw.PathBezierCubicCurveTo(point(3,18.8f),point(.3f,18),point(1,14));
        draw.PathLineTo(point(1.8f,9));
        draw.PathBezierCubicCurveTo(point(2.2f,6.4f),point(3.5f,5),point(6,5));
        draw.PathStroke(color,ImDrawFlags_Closed,1.6f);
        line(6.3f,7.5f,6.3f,11.5f);line(4.3f,9.5f,8.3f,9.5f);
        draw.AddCircleFilled(point(13,8.5f),1.1f,color);draw.AddCircleFilled(point(15.7f,11),1.1f,color);break;
    case 6:
        draw.PathLineTo(point(5,2));draw.PathLineTo(point(15,2));draw.PathLineTo(point(14,9));
        draw.PathBezierCubicCurveTo(point(13,14),point(7,14),point(6,9));
        draw.PathStroke(color,ImDrawFlags_Closed,1.6f);
        draw.PathLineTo(point(5,4));draw.PathLineTo(point(2,4));
        draw.PathBezierCubicCurveTo(point(2,9),point(3,10),point(7,10));draw.PathStroke(color,0,1.6f);
        draw.PathLineTo(point(15,4));draw.PathLineTo(point(18,4));
        draw.PathBezierCubicCurveTo(point(18,9),point(17,10),point(13,10));draw.PathStroke(color,0,1.6f);
        line(10,13,10,18);line(6,18,14,18);break;
    case 7:
        draw.PathLineTo(point(3,5));draw.PathLineTo(point(7,5));
        draw.PathBezierCubicCurveTo(point(6,2),point(7.2f,1),point(9,1));
        draw.PathBezierCubicCurveTo(point(10.8f,1),point(12,2),point(11,5));
        draw.PathLineTo(point(15,5));draw.PathLineTo(point(15,9));
        draw.PathBezierCubicCurveTo(point(18,8),point(19,9.2f),point(19,11));
        draw.PathBezierCubicCurveTo(point(19,12.8f),point(18,14),point(15,13));
        draw.PathLineTo(point(15,18));draw.PathLineTo(point(11,18));
        draw.PathBezierCubicCurveTo(point(12,15),point(10.8f,14),point(9,14));
        draw.PathBezierCubicCurveTo(point(7.2f,14),point(6,15),point(7,18));
        draw.PathLineTo(point(3,18));draw.PathLineTo(point(3,13));
        draw.PathBezierCubicCurveTo(point(6,14),point(7,12.8f),point(7,11));
        draw.PathBezierCubicCurveTo(point(7,9.2f),point(6,8),point(3,9));
        draw.PathStroke(color,ImDrawFlags_Closed,1.6f);break;
    case 8:draw.AddRect(point(2,1),point(18,19),color,2);for(int i=0;i<3;++i) line(6,6.0f+4*i,14,6.0f+4*i);break;
    }
}
void setting_label(const char* text) {
    ImGui::TextUnformatted(text);ImGui::SetNextItemWidth(-FLT_MIN);
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
        if(!rate) {next=now;period=0;return true;}
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
struct MusicFolderSelection {
    std::atomic<bool> finished{false};
    std::string path,error;
};
constexpr std::uint64_t achievement_duration=5000000000ULL;
float achievement_opacity(std::uint64_t elapsed) {
    if(elapsed>=achievement_duration) return 0;
    const auto fraction=std::min({1.0f,float(elapsed)/250000000.0f,float(achievement_duration-elapsed)/350000000.0f});
    return fraction*fraction*(3-2*fraction);
}
struct App {
    SDL_Window* window=nullptr;
    std::unique_ptr<Renderer> renderer;
    std::unique_ptr<Input> input;
    std::unique_ptr<ControlScript> controls;
    Preferences settings;
    Preferences applied_settings{false};
    bool settings_pending=false,display_changed=false;
    Achievements achievements;
    std::filesystem::path achievements_path;
    std::future<void> achievements_save;
    std::string achievements_error;
    std::optional<AchievementId> achievement_notification;
    std::uint64_t achievement_started=0;
    bool achievement_reset_open=false;
    SDL_AudioStream* achievement_audio=nullptr;
    Uint8* achievement_audio_data=nullptr;
    Uint32 achievement_audio_size=0;
    ImFont* heading_font=nullptr;ImFont* italic_font=nullptr;
    Microsoft::WRL::ComPtr<ID3D11ShaderResourceView> logo;
    std::filesystem::path settings_path,pending_window_capture;
    FramePacer pacer,source_pacer;
    FrameRate display_rate,game_rate;
    Performance performance;
    std::vector<std::uint64_t> benchmark_frames;
    std::uint64_t benchmark_started=0;
    unsigned benchmark_dropped=0;
    std::string benchmark_properties,benchmark_cpu,benchmark_build;
    Xbox* game=nullptr;
    std::array<std::uint32_t,4> watch_semaphores{};
    MusicImport music_import;
    std::shared_ptr<MusicFolderSelection> music_folder_selection;
    std::shared_ptr<MusicFolderSelection> file_selection;
    bool file_selection_iso=false;
    unsigned music_songs=0;
    std::filesystem::path mods_root;
    std::vector<Mod> available_mods;
    std::vector<ModConflict> mod_conflicts;
    std::string mods_status;
    bool mods_error=false;
    bool music_importing=false,music_import_requested=false,music_restart=false;
    std::vector<int> refresh_rates;
    float display_refresh=0;
    int category=0,select_category=-1,binding_action=-1,active_scale=1;
    int control_selection=0,controller_binding_action=-1;
    std::uint64_t controller_binding_deadline=0;
    bool controller_navigation_blocked=false;
    bool running=true,playing=false,show_settings=true,launch=false,settings_dirty=false,active_widescreen=false,active_motion_blur=true;
    int active_aspect_ratio=0;
    std::deque<std::string> log_messages;
    std::string memory_snapshot="[]";
    std::uint64_t stop_at=0,frames=0,first_frame=0,last_frame=0;
    std::uint64_t published_at=0,first_source=0,last_source=0;
    std::uint64_t mouse_activity=0;
    bool cursor_hidden=false,focused=false;
    App(bool hidden,bool warp,const std::filesystem::path& script={},const std::filesystem::path& preferences="data/local/settings.cfg"):
        settings(!hidden,preferences),settings_path(preferences) {
        achievements_path=hidden?std::filesystem::path(preferences).concat(".achievements"):preferences.parent_path()/"achievements.cfg";
        if(!hidden) try {achievements.load(achievements_path);}
        catch(const std::exception& error) {achievements_error=error.what();log_message("Achievements: "+achievements_error);}
        music_songs=custom_music_count(music_cache());
        mods_root=std::filesystem::absolute(hidden?preferences.parent_path()/"mods":executable_path().parent_path()/"mods").lexically_normal();
        refresh_mods();
        // Explicit scripts must work while the user keeps another app focused.
        // This process reads only its own virtual controller in script mode.
        if(!script.empty() && !SDL_SetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS,"1"))
            throw std::runtime_error("SDL background input is required for scripted controls");
        if(!SDL_Init(SDL_INIT_VIDEO|SDL_INIT_GAMEPAD|SDL_INIT_AUDIO)) throw std::runtime_error(SDL_GetError());
        try {
            // Request fullscreen before showing the window, without requiring a
            // separate window-manager transition to finish before the first frame.
            window=SDL_CreateWindow("Burnout 2: Point of Impact",960,720,SDL_WINDOW_RESIZABLE|SDL_WINDOW_HIGH_PIXEL_DENSITY|(hidden?SDL_WINDOW_HIDDEN:0)
                |(!hidden && settings.fullscreen?SDL_WINDOW_FULLSCREEN:0)
#ifdef __APPLE__
                |SDL_WINDOW_METAL
#elif !defined(_WIN32)
                |SDL_WINDOW_VULKAN
#endif
            );
            if(!window) throw std::runtime_error(SDL_GetError());
            SDL_SetWindowMinimumSize(window,640,480);
            mouse_activity=SDL_GetTicksNS();
            focused=(SDL_GetWindowFlags(window)&SDL_WINDOW_INPUT_FOCUS)!=0;
            refresh_options();
#ifdef _WIN32
            auto* handle=SDL_GetPointerProperty(SDL_GetWindowProperties(window),SDL_PROP_WINDOW_WIN32_HWND_POINTER,nullptr);
            const bool native_debug=hidden;
#else
            void* handle=window;const bool native_debug=false;
#endif
            active_scale=settings.resolution_scale;
            renderer=std::make_unique<Renderer>(640,480,warp,native_debug,active_scale);renderer->attach_window(handle);
            configure_enhancements();
            active_aspect_ratio=settings.aspect_ratio;active_widescreen=active_aspect_ratio!=0;active_motion_blur=settings.motion_blur;renderer->aspect_ratio(settings.aspect());
            if(!script.empty()) controls=std::make_unique<ControlScript>(script);
            input=std::make_unique<Input>(controls?false:settings.keyboard,false,controls?controls->id:0);
            input->bindings(settings.bindings);
            if(!controls) input->controller_bindings(settings.controller_bindings);
            IMGUI_CHECKVERSION();ImGui::CreateContext();
            heading_font=settings_theme(italic_font);
            if(!ImGui_ImplSDL3_InitForD3D(window) || !ImGui_ImplDX11_Init(renderer->native_device(),renderer->native_context()))
                throw std::runtime_error("Could not initialize the settings interface");
            logo=settings_logo(renderer->native_device());
            try {load_achievement_sound(!hidden);}
            catch(const std::exception& error) {
                if(achievement_audio) SDL_DestroyAudioStream(achievement_audio);
                achievement_audio=nullptr;
                log_message(std::string("Achievement sound unavailable: ")+error.what());
            }
        } catch(...) {cleanup();throw;}
    }
    void cleanup() {
        if(achievement_audio) SDL_DestroyAudioStream(achievement_audio);
        achievement_audio=nullptr;SDL_free(achievement_audio_data);achievement_audio_data=nullptr;
        if(ImGui::GetCurrentContext()) {
            if(ImGui::GetIO().BackendRendererUserData) ImGui_ImplDX11_Shutdown();
            if(ImGui::GetIO().BackendPlatformUserData) ImGui_ImplSDL3_Shutdown();
            ImGui::DestroyContext();
        }
        logo.Reset();input.reset();controls.reset();renderer.reset();if(window) SDL_DestroyWindow(window);window=nullptr;SDL_Quit();
    }
    ~App() {music_import.cancel();save_settings();save_achievements(true);cleanup();}
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
    void save_achievements(bool wait=false) {
        const auto failed=[&](const std::exception& error) {
            achievements.dirty(true);achievements_error=error.what();log_message("Achievements could not be saved: "+achievements_error);
        };
        if(achievements_save.valid()) {
            if(!wait && achievements_save.wait_for(std::chrono::seconds(0))!=std::future_status::ready) return;
            try {achievements_save.get();achievements_error.clear();}
            catch(const std::exception& error) {failed(error);}
        }
        if(!achievements.dirty() || (!wait && !achievements_error.empty())) return;
        try {
            if(wait) {achievements.save(achievements_path);achievements_error.clear();}
            else achievements_save=std::async(std::launch::async,[snapshot=achievements,path=achievements_path] {snapshot.save(path);});
            achievements.dirty(false);
        } catch(const std::exception& error) {failed(error);}
    }
    void load_achievement_sound(bool output) {
        SDL_AudioSpec format{};
#ifdef _WIN32
        const auto module=GetModuleHandleW(nullptr);
        const auto resource=FindResourceW(module,L"ACHIEVEMENT_SOUND",RT_RCDATA);
        const auto loaded=resource?LoadResource(module,resource):nullptr;
        const auto* bytes=loaded?LockResource(loaded):nullptr;
        if(!bytes) throw std::runtime_error("The achievement sound resource is missing");
        auto* source=SDL_IOFromConstMem(bytes,SizeofResource(module,resource));
#else
        auto* source=SDL_IOFromFile((executable_path().parent_path()/"resources/achievement-unlocked.wav").c_str(),"rb");
#endif
        if(!SDL_LoadWAV_IO(source,true,&format,&achievement_audio_data,&achievement_audio_size))
            throw std::runtime_error(SDL_GetError());
        achievement_audio=output?SDL_OpenAudioDeviceStream(SDL_AUDIO_DEVICE_DEFAULT_PLAYBACK,&format,nullptr,nullptr):
            SDL_CreateAudioStream(&format,&format);
        if(!achievement_audio || !SDL_SetAudioStreamGain(achievement_audio,settings.output_gain(focused)) ||
           (output && !SDL_ResumeAudioStreamDevice(achievement_audio))) throw std::runtime_error(SDL_GetError());
    }
    void update_audio_gain() {
        const auto gain=settings.output_gain(focused);
        if(game) game->output_gain(gain);
        if(achievement_audio && !SDL_SetAudioStreamGain(achievement_audio,gain)) log_message(SDL_GetError());
    }
    void stop_achievement_notification() {
        achievement_notification.reset();
        if(achievement_audio) SDL_ClearAudioStream(achievement_audio);
    }
    template<class... Args> void disabled_text(const char* format,Args... args) {
        ImGui::PushFont(italic_font,0);ImGui::PushTextWrapPos(0);
        if constexpr(sizeof...(args)) ImGui::TextDisabled(format,args...);
        else ImGui::TextDisabled("%s",format);
        ImGui::PopTextWrapPos();ImGui::PopFont();
    }
    void achievements_interface() {
        ImGui::Text("%u of %u unlocked",achievements.count(),unsigned(achievement_definitions.size()));
        const auto available=ImGui::GetContentRegionAvail().x;
        const bool enabled=achievements.enabled();
        const auto* toggle=enabled?"Disable achievements":"Enable achievements";
        if(ImGui::Button(toggle)) achievements.enabled(!enabled);
        setting_help(enabled?"Stops new unlocks and notifications. Existing progress is kept.":"Allows achievements to unlock again when you meet their criteria.");
        const auto toggle_width=ImGui::GetItemRectSize().x;
        if(toggle_width+ImGui::GetStyle().ItemSpacing.x+ImGui::CalcTextSize("Reset achievements").x+2*ImGui::GetStyle().FramePadding.x<=available) ImGui::SameLine();
        ImGui::BeginDisabled(!achievements.count());
        if(ImGui::Button("Reset achievements")) {achievement_reset_open=true;ImGui::OpenPopup("Reset achievements?");}
        setting_help("Clears achievement progress so you can earn them again.");
        ImGui::EndDisabled();
        const auto* viewport=ImGui::GetMainViewport();
        ImGui::SetNextWindowPos(viewport->GetCenter(),ImGuiCond_Appearing,{.5f,.5f});
        ImGui::SetNextWindowSize({std::min(440.0f,viewport->WorkSize.x-32),0});
        if(ImGui::BeginPopupModal("Reset achievements?",&achievement_reset_open,ImGuiWindowFlags_AlwaysAutoResize|ImGuiWindowFlags_NoSavedSettings)) {
            ImGui::TextWrapped("This will clear all achievement progress so you can earn them again. This cannot be undone. Are you sure?");
            ImGui::Spacing();
            if(ImGui::IsWindowAppearing()) {ImGui::SetKeyboardFocusHere();ImGui::SetNavCursorVisible(true);}
            if(ImGui::Button("Cancel")) {achievement_reset_open=false;ImGui::CloseCurrentPopup();}
            ImGui::SameLine();
            if(ImGui::Button("Reset achievements")) {
                achievements.reset();stop_achievement_notification();achievement_reset_open=false;ImGui::CloseCurrentPopup();
            }
            if(ImGui::IsKeyPressed(ImGuiKey_Escape) || ImGui::IsKeyPressed(ImGuiKey_GamepadFaceRight)) {
                achievement_reset_open=false;ImGui::CloseCurrentPopup();
            }
            ImGui::EndPopup();
        }
        if(!achievements.enabled()) disabled_text("Achievements are disabled. Existing progress is kept.");
        ImGui::Spacing();
        for(unsigned i=0;i<achievement_definitions.size();++i) {
            const auto& entry=achievement_definitions[i];const bool unlocked=achievements.unlocked(AchievementId(i));
            ImGui::PushStyleVar(ImGuiStyleVar_Alpha,ImGui::GetStyle().Alpha*(unlocked?1.0f:.5f));
            ImGui::SeparatorText(unlocked?"UNLOCKED":"LOCKED");
            ImGui::Image(logo.Get(),{56,56});ImGui::SameLine();ImGui::BeginGroup();
            ImGui::PushFont(heading_font,22);ImGui::TextWrapped("%s",entry.title);ImGui::PopFont();
            ImGui::TextWrapped("%s",entry.description);ImGui::EndGroup();ImGui::Spacing();
            ImGui::PopStyleVar();
        }
        if(!achievements_error.empty()) {
            ImGui::Spacing();ImGui::TextWrapped("Achievement progress: %s",achievements_error.c_str());
            if(achievements.dirty() && ImGui::Button("Retry saving achievements")) {achievements_error.clear();save_achievements();}
        }
    }
    void achievement_toast() {
        if(achievement_notification && (!achievements.enabled() || !achievements.unlocked(*achievement_notification))) stop_achievement_notification();
        if(!achievements.enabled()) return;
        if(!achievement_notification) {
            achievement_notification=achievements.take_notification();
            if(!achievement_notification) return;
            achievement_started=SDL_GetTicksNS();
            if(achievement_audio && (!SDL_ClearAudioStream(achievement_audio) ||
               !SDL_PutAudioStreamDataNoCopy(achievement_audio,achievement_audio_data,int(achievement_audio_size),nullptr,nullptr) ||
               !SDL_FlushAudioStream(achievement_audio))) log_message(std::string("Achievement sound: ")+SDL_GetError());
        }
        const auto elapsed=SDL_GetTicksNS()-achievement_started;
        if(elapsed>=achievement_duration) {stop_achievement_notification();return;}
        const auto& entry=achievement_definitions[unsigned(*achievement_notification)];
        const auto* viewport=ImGui::GetMainViewport();
        ImGui::SetNextWindowPos({viewport->WorkPos.x+viewport->WorkSize.x-16,viewport->WorkPos.y+viewport->WorkSize.y-16},ImGuiCond_Always,{1,1});
        ImGui::SetNextWindowSize({std::min(380.0f,viewport->WorkSize.x-32),0});
        ImGui::PushStyleVar(ImGuiStyleVar_Alpha,ImGui::GetStyle().Alpha*achievement_opacity(elapsed));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,{16,14});
        ImGui::PushStyleColor(ImGuiCol_WindowBg,ImVec4{.08f,.10f,.14f,.97f});
        ImGui::PushStyleColor(ImGuiCol_Border,ImVec4{1,.55f,.27f,1});
        ImGui::Begin("Achievement unlocked",nullptr,ImGuiWindowFlags_NoDecoration|ImGuiWindowFlags_AlwaysAutoResize|
            ImGuiWindowFlags_NoSavedSettings|ImGuiWindowFlags_NoFocusOnAppearing|ImGuiWindowFlags_NoNav|ImGuiWindowFlags_NoInputs);
        ImGui::Image(logo.Get(),{56,56});ImGui::SameLine();ImGui::BeginGroup();
        ImGui::TextColored({1,.55f,.27f,1},"Achievement unlocked");
        ImGui::PushFont(heading_font,22);ImGui::TextWrapped("%s",entry.title);ImGui::PopFont();
        ImGui::TextWrapped("%s",entry.description);
        ImGui::EndGroup();ImGui::End();ImGui::PopStyleColor(2);ImGui::PopStyleVar(2);
    }
    void cancel_bindings() {binding_action=controller_binding_action=-1;input->cancel_binding();}
    void exit_game() {running=false;launch=false;cancel_bindings();save_settings();}
    void start_controller_binding(unsigned action) {
        cancel_bindings();
        if(control_selection>0 && input->start_binding(unsigned(control_selection-1))) {
            controller_binding_action=int(action);controller_binding_deadline=SDL_GetTicksNS()+10000000000ULL;
        }
    }
    void update_controller_binding() {
        if(controller_binding_action<0) return;
        if(const auto binding=input->take_binding()) {
            auto& selected=settings.controller_bindings[control_selection-1][controller_binding_action];
            settings_dirty|=selected!=*binding;selected=*binding;
            input->controller_bindings(settings.controller_bindings);controller_binding_action=-1;
        } else if(input->capture_state()==ControllerCapture::idle || SDL_GetTicksNS()>=controller_binding_deadline) cancel_bindings();
    }
    PerfCounters performance_counters() const {
        auto counters=game?game->performance_counters():renderer->stats().counters;
        counters[unsigned(PerfMetric::displayed)]=frames;return counters;
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
        return std::format("\"refresh_rate\":{},\"resolution_scale\":{},\"output_resolution_scale\":{},\"draw_distance\":{},\"widescreen\":{},\"aspect_ratio\":{},\"motion_blur\":{},\"vsync\":{}",
            settings.refresh_rate,active_scale,settings.resolution_scale,settings.draw_distance,active_widescreen,active_aspect_ratio,active_motion_blur,settings.vsync);
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
        benchmark_cpu=cpu_name();benchmark_build=build_description();
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
            performance.start(renderer->adapter(),settings.refresh_rate,active_scale,settings.vsync,performance_counters());
            log_message("Recording performance. F9 saves; recording stops after 60 seconds.");
        } catch(const std::exception& error) {renderer->performance(nullptr);if(game) game->performance_recording(false);performance.reset();log_message(error.what());}
    }
    void capture_frame() {
        if(!pending_window_capture.empty()) return;
        const auto directory=std::filesystem::path("build/captures")/std::format("frame-{}",SDL_GetTicksNS());
        PerfScope capture(&performance,PerfPhase::capture);
        try {
            settings.save(directory/"settings.cfg");write_text(directory/"window.json",window_diagnostic()+'\n',false);
            if(performance.active()) performance.save(directory);
            if(game) game->capture_frame(directory,bool(controls));
            pending_window_capture=directory/"window.png";
            log_message(std::string(game?"Frame and audio capture: ":"Window capture: ")+directory.string());
        }
        catch(const std::exception& error) {log_message(error.what());}
    }
    std::string window_diagnostic() const {
        int width=0,height=0,pixel_width=0,pixel_height=0;
        if(!SDL_GetWindowSize(window,&width,&height) || !SDL_GetWindowSizeInPixels(window,&pixel_width,&pixel_height))
            throw std::runtime_error(SDL_GetError());
        const auto environment=[](const char* name) {const auto* value=SDL_getenv(name);return json(value?value:"");};
        const char* graphics_runtime="D3D11";
#ifdef __linux__
        Dl_info runtime{};
        if(const auto symbol=dlsym(RTLD_DEFAULT,"D3D11CreateDevice");symbol && dladdr(symbol,&runtime)) graphics_runtime=runtime.dli_fname;
#endif
        return std::format("{{\"format\":\"b2-window-state-v1\",\"desktop\":{},\"session\":{},\"video_driver\":{},\"graphics_runtime\":{},\"sdl_version\":{},\"settings_font\":{},\"logical_size\":[{},{}],\"pixel_size\":[{},{}],\"pixel_density\":{},\"display_scale\":{},\"fullscreen\":{}}}",
            environment("XDG_CURRENT_DESKTOP"),environment("XDG_SESSION_TYPE"),json(SDL_GetCurrentVideoDriver()),json(graphics_runtime),SDL_GetVersion(),json(ImGui::GetIO().FontDefault->GetDebugName()),
            width,height,pixel_width,pixel_height,SDL_GetWindowPixelDensity(window),SDL_GetWindowDisplayScale(window),(SDL_GetWindowFlags(window)&SDL_WINDOW_FULLSCREEN)!=0);
    }
    void refresh_options() {
        refresh_rates={0,30,60,75,90,120,144,165,180,240,360};
        const auto* current=SDL_GetCurrentDisplayMode(SDL_GetDisplayForWindow(window));
        display_refresh=current?current->refresh_rate:0;
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
        input->event(event);
        if((event.type==SDL_EVENT_WINDOW_FOCUS_GAINED || event.type==SDL_EVENT_WINDOW_FOCUS_LOST) &&
           event.window.windowID==SDL_GetWindowID(window)) {
            focused=event.type==SDL_EVENT_WINDOW_FOCUS_GAINED;
            update_audio_gain();
        }
        if((event.type==SDL_EVENT_MOUSE_MOTION && (event.motion.xrel || event.motion.yrel)) ||
           event.type==SDL_EVENT_MOUSE_BUTTON_DOWN || event.type==SDL_EVENT_MOUSE_BUTTON_UP ||
           event.type==SDL_EVENT_MOUSE_WHEEL || event.type==SDL_EVENT_WINDOW_MOUSE_ENTER ||
           event.type==SDL_EVENT_WINDOW_FOCUS_GAINED) mouse_activity=SDL_GetTicksNS();
        if(event.type==SDL_EVENT_WINDOW_DISPLAY_CHANGED || event.type==SDL_EVENT_DISPLAY_CURRENT_MODE_CHANGED) {refresh_options();display_changed=true;}
        if(event.type==SDL_EVENT_WINDOW_HDR_STATE_CHANGED || event.type==SDL_EVENT_DISPLAY_ADDED || event.type==SDL_EVENT_DISPLAY_REMOVED) display_changed=true;
        if(event.type==SDL_EVENT_QUIT || event.type==SDL_EVENT_WINDOW_CLOSE_REQUESTED) exit_game();
        if(controller_binding_action>=0 && event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat && event.key.scancode==SDL_SCANCODE_ESCAPE) {
            cancel_bindings();return;
        }
        if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat && binding_action>=0) {
            if(event.key.scancode==SDL_SCANCODE_ESCAPE) binding_action=-1;
            else if(valid_keyboard_binding(event.key.scancode)) {
                settings_dirty|=settings.bindings[binding_action]!=event.key.scancode;
                settings.bindings[binding_action]=event.key.scancode;input->bindings(settings.bindings);binding_action=-1;
            } else log_message("That key is reserved for settings or captures.");
            return;
        }
        if((event.type==SDL_EVENT_KEY_DOWN || event.type==SDL_EVENT_KEY_UP) &&
           (event.key.scancode==SDL_SCANCODE_ESCAPE || event.key.scancode==SDL_SCANCODE_F1)) {
            if(achievement_reset_open || event.type==SDL_EVENT_KEY_UP) {ImGui_ImplSDL3_ProcessEvent(&event);return;}
            if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat) {
                show_settings=!show_settings;cancel_bindings();if(!show_settings) save_settings();
            }
            // This shortcut belongs to the app. Letting ImGui also handle Escape
            // leaves the focused child and highlights its whole panel on reopen.
            return;
        }
        ImGui_ImplSDL3_ProcessEvent(&event);
        if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat && event.key.scancode==SDL_SCANCODE_F8) capture_frame();
        if(event.type==SDL_EVENT_KEY_DOWN && !event.key.repeat && event.key.scancode==SDL_SCANCODE_F9) toggle_performance();
    }
    void update_cursor() {
        const auto flags=SDL_GetWindowFlags(window);
        const bool hide=(flags&SDL_WINDOW_MOUSE_FOCUS) && (flags&SDL_WINDOW_INPUT_FOCUS) &&
            SDL_GetTicksNS()-mouse_activity>=2000000000ULL && !SDL_GetMouseState(nullptr,nullptr);
        if(hide==cursor_hidden) return;
        cursor_hidden=hide;
        auto& io=ImGui::GetIO();
        // Keep the settings backend from showing the idle cursor every frame.
        if(hide) {io.ConfigFlags|=ImGuiConfigFlags_NoMouseCursorChange;SDL_HideCursor();}
        else {io.ConfigFlags&=~ImGuiConfigFlags_NoMouseCursorChange;SDL_ShowCursor();}
    }
    bool poll() {
        SDL_Event event;
        while(SDL_PollEvent(&event)) this->event(event);
        update_cursor();
        if(file_selection && file_selection->finished.load(std::memory_order_acquire)) {
            if(!file_selection->error.empty()) log_message(file_selection->error);
            else if(!file_selection->path.empty()) {
                auto& destination=file_selection_iso?settings.disc:settings.xbe;
                if(file_selection->path.size()<destination.size()) {copy_text(destination.data(),destination.size(),file_selection->path);settings_dirty=true;}
                else log_message("The selected file path is too long.");
            }
            file_selection.reset();
        }
        if(music_folder_selection && music_folder_selection->finished.load(std::memory_order_acquire)) {
            const auto& selected=*music_folder_selection;
            if(!selected.error.empty()) log_message(selected.error);
            else if(!selected.path.empty()) {
                if(selected.path.size()<settings.music_folder.size()) {
                    copy_text(settings.music_folder.data(),settings.music_folder.size(),selected.path.c_str());settings_dirty=true;
                } else log_message("The music folder path is too long.");
            }
            music_folder_selection.reset();
        }
        if(music_importing) {
            const auto progress=music_import.status();
            if(progress.finished) {
                music_importing=false;log_message(progress.message);
                if(progress.succeeded) {
                    music_songs=progress.completed;settings.custom_music=true;settings_dirty=true;
                    if(music_restart && running) launch=true;
                }
                music_restart=false;
            }
        }
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
            if(controls->capture) {controls->capture=false;capture_frame();}
        }
        input->capture_keyboard(show_settings);input->update();update_controller_binding();
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
    std::filesystem::path music_cache() const {return settings_path.parent_path()/"custom-music";}
    void refresh_mods() {
        available_mods.clear();mod_conflicts.clear();mods_status.clear();mods_error=false;
        try {available_mods=discover_mods(mods_root);}
        catch(const std::exception& error) {mods_status=error.what();mods_error=true;}
    }
    bool mods_interface() {
        bool changed=false;
        ImGui::TextWrapped(playing?"Restart the game to apply changes.":"Enable the mods you want to use.");
        ImGui::PushFont(italic_font,0);
        ImGui::TextWrapped("Mod support currently covers replacement game files. Scripts and executable patches are not yet supported.");
        ImGui::PopFont();
        const bool reorder=settings.enabled_mods.size()>1;
        std::string action;int direction=0;
        if(available_mods.empty() && settings.enabled_mods.empty()) {
            disabled_text("No mods installed.");
        } else if(ImGui::BeginTable("Mods list",reorder?3:2,ImGuiTableFlags_RowBg|ImGuiTableFlags_BordersInnerH|ImGuiTableFlags_PadOuterX)) {
            ImGui::TableSetupColumn("Enabled",ImGuiTableColumnFlags_WidthFixed,ImGui::CalcTextSize("Enabled").x);
            ImGui::TableSetupColumn("Mod",ImGuiTableColumnFlags_WidthStretch);
            if(reorder) ImGui::TableSetupColumn("Order",ImGuiTableColumnFlags_WidthFixed,ImGui::GetFrameHeight()*2+ImGui::GetStyle().ItemSpacing.x);
            ImGui::TableHeadersRow();
            const auto row=[&](const std::string& id,const Mod* mod,std::size_t order) {
                bool enabled=order<settings.enabled_mods.size();
                ImGui::PushID(id.c_str());ImGui::TableNextRow();ImGui::TableNextColumn();
                const auto inset=(ImGui::GetContentRegionAvail().x-ImGui::GetFrameHeight())*.5f;
                if(inset>0) ImGui::SetCursorPosX(ImGui::GetCursorPosX()+inset);
                ImGui::BeginDisabled(!enabled && (!mod || !mod->error.empty() || settings.enabled_mods.size()==128));
                if(ImGui::Checkbox("##Enabled",&enabled)) action=id;
                setting_help("Applies when you start or restart the game.");ImGui::EndDisabled();
                ImGui::TableNextColumn();
                ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,ImVec2{ImGui::GetStyle().ItemSpacing.x,4});
                const bool details=ImGui::TreeNodeEx("##Details",ImGuiTreeNodeFlags_FramePadding|ImGuiTreeNodeFlags_SpanAvailWidth|ImGuiTreeNodeFlags_NoTreePushOnOpen,
                    "%s",mod?mod->name.c_str():id.c_str());
                setting_help("Expand for version, author and file details.");
                ImGui::Indent(ImGui::GetTreeNodeToLabelSpacing());
                if(mod && !mod->description.empty()) {
                    disabled_text("%s",mod->description.c_str());
                }
                if(!mod || !mod->error.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,ImVec4{1,.55f,.42f,1});
                    ImGui::TextWrapped("%s",mod?mod->error.c_str():"Missing mod. Restore its folder or turn it off.");
                    ImGui::PopStyleColor();
                }
                if(details) {
                    if(ImGui::CalcTextSize(mod?mod->name.c_str():id.c_str()).x>ImGui::GetContentRegionAvail().x)
                        ImGui::TextWrapped("%s",mod?mod->name.c_str():id.c_str());
                    if(mod) {
                        if(!mod->version.empty()) ImGui::TextWrapped("Version: %s",mod->version.c_str());
                        if(!mod->author.empty()) ImGui::TextWrapped("Author: %s",mod->author.c_str());
                        disabled_text("%zu %s",mod->files.size(),mod->files.size()==1?"file":"files");
                    }
                    ImGui::TextWrapped("Folder: %s",id.c_str());
                }
                ImGui::Unindent(ImGui::GetTreeNodeToLabelSpacing());
                ImGui::PopStyleVar();
                if(reorder) {
                    ImGui::TableNextColumn();
                    if(order<settings.enabled_mods.size()) {
                        ImGui::BeginDisabled(order==0);
                        if(ImGui::ArrowButton("Up",ImGuiDir_Up)) {action=id;direction=-1;}
                        setting_help("Move this mod up in the enabled list.");ImGui::EndDisabled();
                        ImGui::SameLine();ImGui::BeginDisabled(order+1==settings.enabled_mods.size());
                        if(ImGui::ArrowButton("Down",ImGuiDir_Down)) {action=id;direction=1;}
                        setting_help("Move this mod down in the enabled list.");ImGui::EndDisabled();
                    }
                }
                ImGui::PopID();
            };
            for(std::size_t i=0;i<settings.enabled_mods.size();++i) {
                const auto& id=settings.enabled_mods[i];const auto mod=std::ranges::find(available_mods,id,&Mod::id);
                row(id,mod==available_mods.end()?nullptr:&*mod,i);
            }
            for(const auto& mod:available_mods) if(std::ranges::find(settings.enabled_mods,mod.id)==settings.enabled_mods.end())
                row(mod.id,&mod,settings.enabled_mods.size());
            ImGui::EndTable();
        }
        // Apply after drawing so rows and their widget IDs stay stable this frame.
        if(!action.empty()) {
            const auto found=std::ranges::find(settings.enabled_mods,action);
            if(direction) {
                if(found!=settings.enabled_mods.end() && (direction<0?found!=settings.enabled_mods.begin():found+1!=settings.enabled_mods.end()))
                    std::iter_swap(found,found+direction);
            } else if(found!=settings.enabled_mods.end()) settings.enabled_mods.erase(found);
            else settings.enabled_mods.push_back(action);
            changed=true;mod_conflicts.clear();mods_status.clear();mods_error=false;
        }
        if(reorder) ImGui::TextWrapped("If files overlap, the lower enabled mod is used.");
        ImGui::Spacing();
        if(ImGui::Button("Open folder")) {
            try {
                std::filesystem::create_directories(mods_root);
                auto path=path_text(mods_root);std::replace(path.begin(),path.end(),'\\','/');
                if(!path.starts_with('/')) path='/'+path;
                std::string url="file://";
                for(unsigned char ch:path) {
                    if(ch>32 && ch<127 && ch!='%' && ch!='#' && ch!='?') url+=char(ch);
                    else url+=std::format("%{:02X}",ch);
                }
                if(!SDL_OpenURL(url.c_str())) throw std::runtime_error(SDL_GetError());
            } catch(const std::exception& error) {mods_status=error.what();mods_error=true;}
        }
        setting_help("Open the folder where your mods are installed.");
        const auto toolbar_next=[](const char* label) {
            const auto right=ImGui::GetCursorScreenPos().x+ImGui::GetContentRegionAvail().x;
            if(ImGui::GetItemRectMax().x+ImGui::CalcTextSize(label).x+ImGui::GetStyle().FramePadding.x*2+ImGui::GetStyle().ItemSpacing.x<=right)
                ImGui::SameLine();
        };
        toolbar_next("Refresh");if(ImGui::Button("Refresh")) refresh_mods();
        setting_help("Find newly installed or edited mods.");
        toolbar_next("Check mods");ImGui::BeginDisabled(settings.enabled_mods.empty());
        if(ImGui::Button("Check mods")) {
            try {
                Xbe image(utf8_path(settings.xbe.data()));Disc disc(utf8_path(settings.disc.data()));
                if(!image.supported() || disc.sha256(disc.find("default.xbe"))!=image.sha256)
                    throw std::runtime_error("Select the supported original executable and disc in Game files first.");
                Mods mounted(mods_root,settings.enabled_mods,disc);mod_conflicts=mounted.conflicts();
                mods_error=false;
                mods_status=mod_conflicts.empty()?"Enabled mods passed file checks.":std::format("{} file conflict{}.",mod_conflicts.size(),mod_conflicts.size()==1?"":"s");
                diagnostic_record(mounted.report());
            } catch(const std::exception& error) {mod_conflicts.clear();mods_status=error.what();mods_error=true;}
        }
        setting_help("Check enabled mods against your original disc and find files changed by more than one mod. Edited assets must still use the game's original formats.");
        ImGui::EndDisabled();
        if(!mods_status.empty()) {
            if(mods_error) ImGui::PushStyleColor(ImGuiCol_Text,ImVec4{1,.55f,.42f,1});
            ImGui::TextWrapped("%s",mods_status.c_str());
            if(mods_error) ImGui::PopStyleColor();
        }
        if(!mod_conflicts.empty() && ImGui::CollapsingHeader("File conflicts")) {
            const auto name=[&](const std::string& id) {
                const auto mod=std::ranges::find(available_mods,id,&Mod::id);
                return mod==available_mods.end()?id.c_str():mod->name.c_str();
            };
            for(const auto& conflict:mod_conflicts) ImGui::TextWrapped("%s\n%s replaces %s",conflict.path.c_str(),name(conflict.winner),name(conflict.previous));
        }
        ImGui::Spacing();
        if(ImGui::CollapsingHeader("Adding mods")) {
            ImGui::TextWrapped("Place each mod in its own folder, then refresh the list. A mod contains mod.cfg and a files folder matching the disc paths. See BUILDING.txt for creation steps.");
            ImGui::TextWrapped("Folder: %s",path_text(mods_root).c_str());
        }
        return changed;
    }
    void browse_music() {
        music_folder_selection=std::make_shared<MusicFolderSelection>();
        auto* request=new std::shared_ptr<MusicFolderSelection>(music_folder_selection);
        SDL_ShowOpenFolderDialog([](void* userdata,const char* const* files,int) {
            std::unique_ptr<std::shared_ptr<MusicFolderSelection>> request(static_cast<std::shared_ptr<MusicFolderSelection>*>(userdata));
            auto& result=**request;
            if(!files) result.error=SDL_GetError();
            else if(files[0]) result.path=files[0];
            result.finished.store(true,std::memory_order_release);
        },request,window,settings.music_folder[0]?settings.music_folder.data():nullptr,false);
    }
    void begin_music_import() {
        music_import_requested=false;music_importing=true;
        music_import.start(utf8_path(settings.music_folder.data()),music_cache());
        log_message("Importing custom music...");
    }
    bool browse(std::array<char,4096>& path,bool iso) {
#ifdef _WIN32
        std::array<wchar_t,32768> result{};
        OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);
        dialog.hwndOwner=static_cast<HWND>(SDL_GetPointerProperty(SDL_GetWindowProperties(window),SDL_PROP_WINDOW_WIN32_HWND_POINTER,nullptr));
        dialog.lpstrFilter=iso?L"Xbox game disc (*.iso)\0*.iso\0\0":L"Xbox executable (*.xbe)\0*.xbe\0\0";
        dialog.lpstrFile=result.data();dialog.nMaxFile=static_cast<DWORD>(result.size());dialog.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_NOCHANGEDIR;
        if(GetOpenFileNameW(&dialog)) {
            const auto text=utf8(result.data());
            if(text.size()<path.size() && text!=path.data()) {copy_text(path.data(),path.size(),text.c_str());return true;}
        }
        return false;
#else
        if(file_selection) return false;
        file_selection=std::make_shared<MusicFolderSelection>();file_selection_iso=iso;
        auto* request=new std::shared_ptr<MusicFolderSelection>(file_selection);
        const SDL_DialogFileFilter filter{iso?"Xbox game disc":"Xbox executable",iso?"iso":"xbe"};
        SDL_ShowOpenFileDialog([](void* userdata,const char* const* files,int) {
            std::unique_ptr<std::shared_ptr<MusicFolderSelection>> request(static_cast<std::shared_ptr<MusicFolderSelection>*>(userdata));
            auto& result=**request;
            if(!files) result.error=SDL_GetError();else if(files[0]) result.path=files[0];
            result.finished.store(true,std::memory_order_release);
        },request,window,&filter,1,path[0]?path.data():nullptr,false);
        return false;
#endif
    }
    void settings_interface() {
        PerfScope timing(&performance,PerfPhase::ui);
        save_achievements();
        const bool blocked=input->capture_state()!=ControllerCapture::idle;
        if(blocked!=controller_navigation_blocked) {
            // ImGui reads physical gamepad state itself. Stop that polling while
            // capturing, including the release, so a binding cannot activate UI.
            ImGui_ImplSDL3_SetGamepadMode(blocked?ImGui_ImplSDL3_GamepadMode_Manual:ImGui_ImplSDL3_GamepadMode_AutoFirst,nullptr,0);
            auto& io=ImGui::GetIO();
            if(blocked) {
                io.ConfigFlags&=~ImGuiConfigFlags_NavEnableGamepad;
                for(int key=ImGuiKey_GamepadStart;key<=ImGuiKey_GamepadRStickDown;++key) io.AddKeyEvent(ImGuiKey(key),false);
            } else io.ConfigFlags|=ImGuiConfigFlags_NavEnableGamepad;
            controller_navigation_blocked=blocked;
        }
        ImGui_ImplDX11_NewFrame();ImGui_ImplSDL3_NewFrame();ImGui::NewFrame();
        if(show_settings) {
            const auto* viewport=ImGui::GetMainViewport();
            auto& backdrop=*ImGui::GetBackgroundDrawList();
            if(!playing) {
                const auto p=viewport->Pos,size=viewport->Size;
                backdrop.AddRectFilledMultiColor(p,{p.x+size.x,p.y+size.y},IM_COL32(15,22,32,255),IM_COL32(26,31,40,255),IM_COL32(10,14,21,255),IM_COL32(10,14,21,255));
                for(int i=0;i<5;++i) {
                    const float x=p.x+size.x*.6f+i*48;
                    backdrop.AddLine({x,p.y},{x-size.y*.6f,p.y+size.y},IM_COL32(255,126,48,12),16);
                }
            } else backdrop.AddRectFilled(viewport->Pos,{viewport->Pos.x+viewport->Size.x,viewport->Pos.y+viewport->Size.y},IM_COL32(0,0,0,100));
            ImGui::SetNextWindowPos(viewport->GetCenter(),ImGuiCond_Always,{0.5f,0.5f});
            ImGui::SetNextWindowSize({std::min(1040.0f,viewport->Size.x-32),std::min(740.0f,viewport->Size.y-32)});
            ImGui::Begin("Burnout 2: Point of Impact",nullptr,ImGuiWindowFlags_NoTitleBar|ImGuiWindowFlags_NoResize|ImGuiWindowFlags_NoMove|ImGuiWindowFlags_NoScrollbar);
            const auto header=ImGui::GetCursorPos();
            ImGui::Image(logo.Get(),{64,64});
            ImGui::SetCursorPos({header.x,header.y+74});ImGui::Separator();ImGui::Spacing();
            bool changed=false;
            constexpr const char* categories[]={"Game files","General","Audio","Graphics","Enhancements","Controls","Achievements","Mods","Diagnostics"};
            constexpr const char* navigation[]={"        Game files","        General","        Audio","        Graphics","        Enhancements","        Controls","        Achievements","        Mods","        Diagnostics"};
            if(select_category>=0 && select_category<int(std::size(categories))) {category=select_category;cancel_bindings();}
            select_category=-1;
            ImGui::BeginChild("Settings body",{0,-66},0,ImGuiWindowFlags_NoScrollbar|ImGuiWindowFlags_NoScrollWithMouse);
            const float navigation_width=ImGui::GetContentRegionAvail().x<760?170.0f:190.0f;
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,{10,14});
            ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,{8,6});
            ImGui::BeginChild("Navigation",{navigation_width,0},ImGuiChildFlags_Borders);
            ImGui::PushFont(nullptr,12);ImGui::TextDisabled("SETTINGS");ImGui::PopFont();ImGui::Spacing();
            ImGui::PushStyleVar(ImGuiStyleVar_SelectableTextAlign,{0,.5f});
            for(int tab=0;tab<int(std::size(categories));++tab) {
                if(ImGui::Selectable(navigation[tab],category==tab,0,{0,40})) {category=tab;cancel_bindings();}
                const auto p=ImGui::GetItemRectMin();
                const auto color=category==tab?IM_COL32(255,139,70,255):IM_COL32(153,166,181,255);
                settings_icon(*ImGui::GetWindowDrawList(),tab,{p.x+6,p.y+10},color);
                if(category==tab) ImGui::GetWindowDrawList()->AddRectFilled({p.x-5,p.y+9},{p.x-2,p.y+31},color,2);
            }
            ImGui::PopStyleVar();ImGui::EndChild();ImGui::PopStyleVar(2);
            ImGui::SameLine();
            ImGui::BeginChild("Settings page",{0,0},0,ImGuiWindowFlags_NoScrollbar|ImGuiWindowFlags_NoScrollWithMouse);
            ImGui::PushFont(heading_font,26);ImGui::TextUnformatted(categories[category]);ImGui::PopFont();
            ImGui::Spacing();
            ImGui::BeginChild("Options",{0,0},ImGuiChildFlags_Borders);
                    switch(category) {
                    case 0:
                        ImGui::SeparatorText("ORIGINAL GAME FILES");
                        setting_label("Game executable");ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x-104);
                        changed|=ImGui::InputText("##Executable",settings.xbe.data(),settings.xbe.size());
                        setting_help("The supported US Xbox game executable (default.xbe). Changes apply to the next game session.");
                        ImGui::SameLine();if(ImGui::Button("Browse##xbe",{92,0})) changed|=browse(settings.xbe,false);
                        setting_help("Select the game executable. Changes save automatically.");
                        setting_label("Game disc image");ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x-104);
                        changed|=ImGui::InputText("##Game disc",settings.disc.data(),settings.disc.size());
                        setting_help("Your US Xbox XISO disc image. Changes apply to the next game session.");
                        ImGui::SameLine();if(ImGui::Button("Browse##disc",{92,0})) changed|=browse(settings.disc,true);
                        setting_help("Select the Xbox game disc image. Changes save automatically.");
                        ImGui::Spacing();ImGui::TextWrapped("Use the US Xbox release of Burnout 2: Point of Impact. Select default.xbe and its XISO disc image.");
                        if(playing) ImGui::TextWrapped("Use Restart game to load different game files. The window stays open.");
                        break;
                    case 1: {
                        ImGui::SeparatorText("MOTION & PERFORMANCE");
                        const auto selected=settings.refresh_rate?std::format("{} Hz",settings.refresh_rate):"Unlimited";
                        setting_label("Refresh rate");
                        if(ImGui::BeginCombo("##Refresh rate",selected.c_str())) {
                            for(const auto rate:refresh_rates) {
                                const auto label=rate?std::format("{} Hz",rate):"Unlimited";const bool active=rate==settings.refresh_rate;
                                if(ImGui::Selectable(label.c_str(),active)) {settings.select_refresh_rate(rate);changed=true;}
                                if(active) ImGui::SetItemDefaultFocus();
                            }
                            ImGui::EndCombo();
                        }
                        setting_help("Limits the display frame rate without changing game speed. Unlimited removes this limit; V-sync still follows the display.");
                        ImGui::Spacing();ImGui::SeparatorText("ON-SCREEN INFO");
                        changed|=ImGui::Checkbox("Show frame rate",&settings.show_fps);
                        setting_help("Displays the game and display frame rates in the upper-right corner.");
                        break;
                    }
                    case 2: {
                        ImGui::SeparatorText("OUTPUT");setting_label("Master volume");
                        bool audio_changed=ImGui::SliderInt("##Master volume",&settings.volume,0,100,"%d%%");
                        setting_help("Applies to both music and sound effects.");
                        audio_changed|=ImGui::Checkbox("Mute",&settings.muted);
                        setting_help("Mutes all game audio without changing the master volume.");
                        audio_changed|=ImGui::Checkbox("Mute on focus loss",&settings.mute_on_focus_loss);
                        setting_help("Silences game audio while another window has focus. Restores your volume when you return.");
                        changed|=audio_changed;
                        if(audio_changed) update_audio_gain();
                        ImGui::Spacing();ImGui::SeparatorText("YOUR SOUNDTRACK");
                        ImGui::BeginDisabled(music_songs==0 || music_importing);
                        changed|=ImGui::Checkbox("Custom music",&settings.custom_music);
                        setting_help("Makes imported music available in the game's Music Selection menu. Applies when you start or restart the game.");
                        ImGui::EndDisabled();
                        disabled_text("%u songs imported",music_songs);
                        ImGui::BeginDisabled(music_importing || music_folder_selection!=nullptr);
                        setting_label("Music folder");ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x-104);
                        ImGui::InputText("##Music folder",settings.music_folder.data(),settings.music_folder.size(),ImGuiInputTextFlags_ReadOnly);
                        ImGui::SameLine();if(ImGui::Button("Browse##music",{92,0})) browse_music();
                        ImGui::BeginDisabled(settings.music_folder[0]==0);
                        if(ImGui::Button(playing?"Import music and restart":"Import music")) {
                            music_import_requested=true;music_restart=playing;launch=true;
                        }
                        setting_help("Imports this folder for Music Selection. Importing while playing restarts the game from the beginning.");
                        ImGui::EndDisabled();ImGui::EndDisabled();
                        ImGui::TextWrapped("MP3, WAV, WMA, FLAC, M4A and AAC. Your original files stay in their folder.");
                        const auto progress=music_import.status();
                        if(music_importing) {
                            ImGui::ProgressBar(progress.total?float(progress.completed)/progress.total:0);
                            ImGui::TextWrapped("%s",progress.file.c_str());
                            if(ImGui::Button("Cancel import")) {music_import.cancel();music_restart=false;}
                        } else if(progress.finished) ImGui::TextWrapped("%s",progress.message.c_str());
                        break;
                    }
                    case 3: {
                        ImGui::SeparatorText("DISPLAY");
                        constexpr const char* aspects[]={"Standard (4:3)","Widescreen (16:9)","Ultrawide (21:9)"};
                        setting_label("Aspect ratio");
                        changed|=ImGui::Combo("##Aspect ratio",&settings.aspect_ratio,aspects,int(std::size(aspects)));
                        setting_help("Wider views reveal more of the scene horizontally. HUD proportions and edge placement are preserved. Applies immediately.");
                        changed|=ImGui::Checkbox("V-sync",&settings.vsync);
                        setting_help("Synchronizes presentation with the display to prevent tearing. The display's refresh rate limits the maximum frame rate.");
                        if(ImGui::Checkbox("Fullscreen",&settings.fullscreen)) {
                            if(!SDL_SetWindowFullscreen(window,settings.fullscreen)) {settings.fullscreen=!settings.fullscreen;log_message(SDL_GetError());}
                            else changed=true;
                        }
                        setting_help("Switches between a window and fullscreen immediately.");
                        ImGui::Spacing();ImGui::SeparatorText("RESOLUTION");
                        {
                            const auto label=std::format("{}x ({} x {})",settings.resolution_scale,640*settings.resolution_scale,480*settings.resolution_scale);
                            setting_label("Internal resolution");
                            if(ImGui::BeginCombo("##Resolution",label.c_str())) {
                                for(int scale=1;scale<=4;++scale) {
                                    const auto option=std::format("{}x ({} x {})",scale,640*scale,480*scale);
                                    if(ImGui::Selectable(option.c_str(),settings.resolution_scale==scale)) {settings.resolution_scale=scale;changed=true;}
                                }
                                ImGui::EndCombo();
                            }
                            setting_help("Higher resolutions sharpen geometry and use more GPU power. Original proportions are preserved. Applies immediately.");
                        }
                        ImGui::Spacing();ImGui::SeparatorText("HDR");
                        const auto hdr=renderer->hdr_status();
                        changed|=ImGui::Checkbox("HDR output",&settings.hdr);
                        setting_help("Expands bright highlights for an HDR display while keeping supported HUD elements at normal brightness. Switches without restarting.");
#ifdef _WIN32
                        if(settings.hdr && !hdr.available) ImGui::TextWrapped("Enable HDR in Windows for this display. SDR output is used until HDR is available.");
#else
                        ImGui::TextWrapped("HDR output is currently supported on Windows.");
#endif
                        ImGui::BeginDisabled(!settings.hdr);
                        setting_label("Paper white");
                        changed|=ImGui::SliderInt("##HDR paper white",&settings.hdr_paper_white,80,500,"%d nits");
                        setting_help("Sets normal image and interface brightness. Adjust for your room; peak highlights are controlled separately.");
                        bool automatic_peak=settings.hdr_peak==0;
                        if(ImGui::Checkbox("Use display peak brightness",&automatic_peak)) {
                            settings.hdr_peak=automatic_peak?0:std::clamp(int(std::lround(hdr.display_peak>0?hdr.display_peak:1000)),200,10000);changed=true;
                        }
                        setting_help("Uses the display's reported maximum brightness, or 1000 nits if it is unavailable. Turn off to use your display's calibrated peak.");
                        if(!automatic_peak) {
                            setting_label("Peak brightness");
                            changed|=ImGui::SliderInt("##HDR peak",&settings.hdr_peak,200,10000,"%d nits",ImGuiSliderFlags_Logarithmic);
                            setting_help("Match your display's peak brightness. Paper white is capped at this limit.");
                        }
                        ImGui::EndDisabled();
                        break;
                    }
                    case 4: {
                        ImGui::SeparatorText("WORLD DETAIL");
                        constexpr int distances[]={1,2,4};
                        constexpr const char* distance_labels[]={"Original (1x)","2x original","4x original"};
                        int distance=0;for(int i=0;i<3;++i) if(settings.draw_distance==distances[i]) distance=i;
                        setting_label("Draw distance");
                        if(ImGui::Combo("##Draw distance",&distance,distance_labels,3)) {settings.draw_distance=distances[distance];changed=true;}
                        setting_help("Draws compatible scenery and traffic farther away, with smoother detail changes and fades. Applies immediately; enabling extra scenery can briefly pause to load it. Higher settings can increase rendering work.");
                        ImGui::Spacing();ImGui::SeparatorText("IMAGE QUALITY");
                        constexpr const char* levels[]={"Off (original)","FXAA Low","FXAA Medium","FXAA High","MSAA 2x","MSAA 4x","MSAA 8x"};
                        setting_label("Antialiasing");
                        if(ImGui::BeginCombo("##Antialiasing",levels[settings.antialiasing])) {
                            for(int i=0;i<int(std::size(levels));++i) {
                                const bool supported=i<4 || renderer->supports_msaa(1U<<(i-3));
                                ImGui::BeginDisabled(!supported);
                                if(ImGui::Selectable(levels[i],settings.antialiasing==i)) {settings.antialiasing=i;changed=true;}
                                if(!supported) setting_help("This MSAA level is unavailable on your graphics device.");
                                ImGui::EndDisabled();
                            }
                            ImGui::EndCombo();
                        }
                        setting_help("MSAA smooths geometry edges while keeping textures sharp. Higher sample counts use more GPU power and memory. FXAA smooths the whole image and can soften fine detail and in-game text.");
                        constexpr const char* ao_levels[]={"Off (original)","GTAO Low","GTAO Medium","GTAO High"};
                        setting_label("Ambient occlusion");
                        if(ImGui::Combo("##Ambient occlusion",&settings.ambient_occlusion,ao_levels,int(std::size(ao_levels)))) changed=true;
                        setting_help("Adds contact shading around cars and scenery. Higher quality uses more GPU power. Off preserves the original appearance.");
                        ImGui::BeginDisabled(!settings.ambient_occlusion);
                        setting_label("AO strength");
                        if(ImGui::SliderInt("##AO strength",&settings.ambient_occlusion_strength,0,100,"%d%%")) changed=true;
                        setting_help("Controls the added shading. Lower strength avoids making the game's existing shadows too dark.");
                        ImGui::EndDisabled();
                        constexpr int values[]={0,1,2,4,8,16};
                        constexpr const char* filters[]={"Original (game setting)","Off (1x)","2x","4x","8x","16x"};
                        int selected=0;for(int i=0;i<int(std::size(values));++i) if(settings.anisotropy==values[i]) selected=i;
                        setting_label("Texture filtering");
                        if(ImGui::Combo("##Anisotropic filtering",&selected,filters,int(std::size(filters)))) {settings.anisotropy=values[selected];changed=true;}
                        setting_help("Keeps road and other textures sharper at an angle. The original game uses anisotropic filtering. Original preserves each texture's settings; higher values use more GPU power.");
                        ImGui::Spacing();ImGui::SeparatorText("MISCELLANEOUS");
                        changed|=ImGui::Checkbox("Clean HUD",&settings.clean_hud);
                        setting_help("Redraws the original HUD lettering, outlines, arrows, menu boxes, wreaths, Xbox button icons and gauges with smooth edges. Also prevents faded Crash bonus numbers from reappearing. Applies immediately.");
                        changed|=ImGui::Checkbox("Fast load",&settings.fast_load);
                        setting_help("Finishes loading as soon as the game's assets are ready, without the original loading fades or frame delays. Applies immediately.");
                        changed|=ImGui::Checkbox("Motion blur",&settings.motion_blur);
                        setting_help("Enables the original game's scene blur during boosting and other speed effects. Applies immediately.");
                        changed|=ImGui::Checkbox("Skip intro splash screens",&settings.skip_intro);
                        setting_help("Skips any remaining intro splash screens after initial loading. If the intro has already finished, this affects the next game session.");
                        ImGui::Spacing();
                        break;
                    }
                    case 5: {
                        ImGui::TextWrapped("Gamepads connect automatically. Use Tab or a gamepad to navigate settings.");
                        constexpr const char* devices[]={"Keyboard","Controller 1","Controller 2","Controller 3","Controller 4"};
                        ImGui::SetNextItemWidth(-FLT_MIN);
                        if(ImGui::Combo("##Control device",&control_selection,devices,int(std::size(devices)))) cancel_bindings();
                        const bool controller=control_selection>0;const auto port=unsigned(controller?control_selection-1:0);
                        if(controller) {
                            ImGui::TextWrapped("%s",input->controller_name(port));
                            setting_help("Bindings are saved for this player slot and work with compatible controllers. Settings navigation keeps its standard controls.");
                            ImGui::SeparatorText("CONTROLLER BINDINGS");
                            if(ImGui::Button("Restore controller defaults")) {
                                settings.controller_bindings[port]=default_controller_bindings();input->controller_bindings(settings.controller_bindings);cancel_bindings();changed=true;
                            }
                            setting_help("Restores this player's controller bindings. Other players keep their settings.");
                            if(!input->controller_id(port)) ImGui::TextWrapped("Connect a compatible controller to assign its controls.");
                            if(controller_binding_action>=0) {
                                ImGui::TextWrapped(input->capture_state()==ControllerCapture::neutral?"Release the controls, then press a button, pull a trigger or move a stick.":"Press a button, pull a trigger or move a stick. Esc cancels; waiting 10 seconds also cancels.");
                                if(ImGui::Button("Cancel binding")) cancel_bindings();
                            }
                        } else {
                            if(ImGui::Checkbox("Keyboard controller",&settings.keyboard)) {input->keyboard(settings.keyboard);changed=true;}
                            setting_help("Enables keyboard controls. Connected gamepads work automatically.");
                            ImGui::SeparatorText("KEYBOARD BINDINGS");
                            if(ImGui::Button("Restore keyboard defaults")) {settings.bindings=default_keyboard_bindings();input->bindings(settings.bindings);cancel_bindings();changed=true;}
                            setting_help("Restores the original keyboard bindings.");
                        }
                        if(ImGui::BeginTable("Bindings",3,ImGuiTableFlags_RowBg)) {
                            ImGui::TableSetupColumn("Action",ImGuiTableColumnFlags_WidthStretch);
                            ImGui::TableSetupColumn(controller?"Control":"Key",ImGuiTableColumnFlags_WidthFixed,std::clamp(ImGui::GetContentRegionAvail().x*.4f,120.0f,230.0f));
                            ImGui::TableSetupColumn("Clear",ImGuiTableColumnFlags_WidthFixed,55);
                            constexpr unsigned order[]={15,14,16,17,8,9,4,5,0,1,2,3,10,11,12,13,6,7,18,19,20,21,22,23};
                            for(const auto i:order) {
                                ImGui::PushID(int(i));ImGui::TableNextRow();ImGui::TableNextColumn();ImGui::TextWrapped("%s",input_actions[i].label.data());
                                ImGui::TableNextColumn();
                                if(controller) {
                                    const auto name=input->controller_binding_name(port,settings.controller_bindings[port][i]);
                                    ImGui::BeginDisabled(!input->controller_id(port));
                                    if(ImGui::Button(controller_binding_action==int(i)?"Press a control...":name.c_str(),{-FLT_MIN,0})) start_controller_binding(i);
                                    ImGui::EndDisabled();
                                    setting_help("Select, release the controls, then press a button, pull a trigger or move a stick. Analog controls keep their full range. Esc cancels.");
                                } else {
                                    const auto code=SDL_Scancode(settings.bindings[i]);
                                    const auto* name=code?SDL_GetKeyName(SDL_GetKeyFromScancode(code,SDL_KMOD_NONE,false)):"Unbound";
                                    if(ImGui::Button(binding_action==int(i)?"Press a key...":name,{-FLT_MIN,0})) {cancel_bindings();binding_action=int(i);}
                                    setting_help("Click, then press a key. Esc cancels. Esc, F1, F8 and F9 are reserved for settings and captures.");
                                }
                                ImGui::TableNextColumn();if(ImGui::SmallButton("Clear")) {
                                    if(controller) {
                                        auto& binding=settings.controller_bindings[port][i];changed|=binding.source!=ControllerSource::none;binding={};input->controller_bindings(settings.controller_bindings);
                                    } else {changed|=settings.bindings[i]!=0;settings.bindings[i]=0;input->bindings(settings.bindings);}
                                    cancel_bindings();
                                }
                                setting_help("Removes this binding.");
                                ImGui::PopID();
                            }
                            ImGui::EndTable();
                        }
                        break;
                    }
                    case 6:achievements_interface();break;
                    case 7:changed|=mods_interface();break;
                    case 8:
                        ImGui::SeparatorText("CAPTURE & PERFORMANCE");
                        if(ImGui::Button("Capture frame (F8)")) capture_frame();
                        setting_help("Saves a window image and display details in build/captures. During a game, also saves the game frame and audio.");
                        ImGui::BeginDisabled(!playing);
                        if(ImGui::Button(performance.active()?"Save performance (F9)":"Record performance (F9)")) toggle_performance();
                        setting_help("Record a slowdown, then press F9 to save. Recording stops after 60 seconds. F8 also saves performance while recording.");
                        ImGui::EndDisabled();
                        if(!playing) disabled_text("Performance recording is available while a game is running.");
                        ImGui::Spacing();ImGui::SeparatorText("ACTIVITY");
                        if(log_messages.empty()) ImGui::TextUnformatted("No messages yet.");
                        for(auto message=log_messages.rbegin();message!=log_messages.rend();++message) {
                            ImGui::TextWrapped("%s",message->c_str());ImGui::Spacing();
                        }
                        break;
                    }
            ImGui::EndChild();ImGui::EndChild();ImGui::EndChild();
            settings_dirty|=changed;
            settings_pending|=changed;
            ImGui::Separator();
            const auto available=ImGui::GetContentRegionAvail().x,spacing=ImGui::GetStyle().ItemSpacing.x;
            const float count=playing?3.0f:2.0f;
            const auto button_width=std::clamp((available-spacing*(count-1))/count,1.0f,145.0f);
            const auto row_width=button_width*count+spacing*(count-1);
            const auto footer=ImGui::GetCursorPos();
            if(available-row_width>175) {ImGui::AlignTextToFramePadding();disabled_text("Saved automatically");}
            ImGui::SetCursorPos({footer.x+available-row_width,footer.y});
            ImGui::BeginDisabled(music_importing || music_import_requested);
            ImGui::PushStyleColor(ImGuiCol_Text,ImVec4{.08f,.07f,.06f,1});
            ImGui::PushStyleColor(ImGuiCol_Button,ImVec4{1,.49f,.19f,1});
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,ImVec4{1,.61f,.34f,1});
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,ImVec4{.90f,.40f,.12f,1});
            if(ImGui::Button(playing?"Restart game":"Start game",{button_width,0})) {launch=true;cancel_bindings();}
            ImGui::PopStyleColor(4);
            setting_help(playing?"Restarts the game from the beginning with the saved settings. The window stays open.":
                "Starts the game with these settings. Changes save automatically. Esc opens or closes settings.");
            ImGui::EndDisabled();
            if(playing) {
                ImGui::SameLine();
                if(ImGui::Button("Return to game",{button_width,0})) {show_settings=false;cancel_bindings();}
                setting_help("Returns to the game. Changes save automatically. Esc opens or closes settings.");
            }
            ImGui::SameLine();
            if(ImGui::Button("Exit game",{button_width,0})) exit_game();
            setting_help("Closes the game and the window. Pending setting changes save automatically.");
            if(!ImGui::IsAnyItemActive() || !show_settings || launch) save_settings();
            ImGui::End();
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
        achievement_toast();
        ImGui::Render();ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
        if(!pending_window_capture.empty()) {
            PerfScope capture(&performance,PerfPhase::capture);
            auto path=std::move(pending_window_capture);pending_window_capture.clear();
            try {save_png(renderer->read_display(),path);}
            catch(const std::exception& error) {log_message(error.what());}
        }
    }
    void frame_presented() {
        last_frame=SDL_GetTicksNS();display_rate.sample(last_frame);
        if(!frames) first_frame=last_frame;++frames;
    }
    bool display_paced() const {
        return settings.vsync && !(SDL_GetWindowFlags(window)&(SDL_WINDOW_HIDDEN|SDL_WINDOW_MINIMIZED|SDL_WINDOW_OCCLUDED)) &&
            display_refresh>=30 && (!settings.refresh_rate || settings.refresh_rate+0.5f>=display_refresh);
    }
    void display(bool clear) {
        if(clear) {renderer->set_target(0);renderer->begin(0xFF16191D);renderer->end();}
        if(renderer->display()) {
            settings_interface();if(!display_paced()) {PerfScope wait(&performance,PerfPhase::pacing);pacer.wait(settings.refresh_rate);}renderer->present(settings.vsync);
            if(playing) frame_presented();
        } else pacer.wait(settings.refresh_rate);
        if(!playing) apply_settings();
    }
    void display_game(bool wait=true) {
        if(renderer->display(wait)) {
            settings_interface();
            if(renderer->present(settings.vsync,wait)) frame_presented();
        }
    }
    void game_flip() {
        const bool fast_loading=game && game->fast_loading();
        auto now=SDL_GetTicksNS();source_pacer.due(settings.refresh_rate,now);
        while(running && !fast_loading && !display_paced() && now<source_pacer.next) {
            if(!poll_game()) return;
            now=SDL_GetTicksNS();source_pacer.due(settings.refresh_rate,now);
            if(now<source_pacer.next) {PerfScope wait(&performance,PerfPhase::pacing);SDL_DelayPrecise(source_pacer.next-now);}
            now=SDL_GetTicksNS();
        }
        if(!running || launch) return;
        published_at=now;
        if(performance.active()) performance.source_frame(renderer->stats().published_frames+1,performance_counters());
        if(benchmark_started) {
            if(now-benchmark_started<30000000000ULL) {
                if(benchmark_frames.size()<8192) benchmark_frames.push_back(now);else ++benchmark_dropped;
            }
        }
        renderer->publish_frame();source_pacer.advance();
        game_rate.sample(now);
        if(!first_source) first_source=now;last_source=now;
        if(controls && !controls->started) controls->started=now;
        if(fast_loading) {
            // Keep input/settings responsive, but never stall asset loading on
            // the display queue. The first gameplay flip resumes normal pacing.
            display_game(false);
            source_pacer.next=now;
        } else display_game();
        if(!launch && running) apply_settings();
    }
    void configure_enhancements() {
        if(settings.antialiasing>=4 && !renderer->supports_msaa(1U<<(settings.antialiasing-3))) {
            log_message(std::format("MSAA {}x is unavailable on this graphics device. Antialiasing has been set to Off.",1U<<(settings.antialiasing-3)));
            settings.antialiasing=0;settings_dirty=true;
        }
        renderer->configure(settings.resolution_scale,settings.antialiasing,settings.anisotropy,
            unsigned(settings.ambient_occlusion),settings.ambient_occlusion_strength/100.0f,settings.hdr_options());
        renderer->clean_hud(settings.clean_hud);
        applied_settings=settings;settings_pending=false;
    }
    void apply_settings() {
        if(!settings_pending && !display_changed) return;
        settings_pending=false;
        if(display_changed) {renderer->refresh_display();display_changed=false;}
        const auto apply=[&](const Preferences& selected) {
            renderer->configure(selected.resolution_scale,selected.antialiasing,selected.anisotropy,
                unsigned(selected.ambient_occlusion),selected.ambient_occlusion_strength/100.0f,selected.hdr_options());
            if(game) game->settings({selected.aspect_ratio!=0,selected.aspect_ratio==2,selected.motion_blur,
                selected.skip_intro,selected.fast_load,selected.clean_hud,unsigned(selected.draw_distance)});
            renderer->clean_hud(selected.clean_hud);renderer->aspect_ratio(selected.aspect());
        };
        try {apply(settings);}
        catch(const std::exception& error) {
            apply(applied_settings);
            for(auto member:{&Preferences::resolution_scale,&Preferences::antialiasing,&Preferences::anisotropy,
                &Preferences::ambient_occlusion,&Preferences::ambient_occlusion_strength,&Preferences::aspect_ratio,&Preferences::draw_distance,
                &Preferences::hdr_paper_white,&Preferences::hdr_peak})
                settings.*member=applied_settings.*member;
            for(auto member:{&Preferences::motion_blur,&Preferences::skip_intro,&Preferences::fast_load,&Preferences::clean_hud,&Preferences::hdr})
                settings.*member=applied_settings.*member;
            settings_dirty=true;log_message(std::string("Settings change could not be applied: ")+error.what());
        }
        active_scale=settings.resolution_scale;active_aspect_ratio=settings.aspect_ratio;
        active_widescreen=active_aspect_ratio!=0;active_motion_blur=settings.motion_blur;
        applied_settings=settings;
    }
    void configure_graphics() {
        // Preserve pipelines/UI while discarding the previous game's resources.
        // Live changes use apply_settings() to preserve the current session.
        active_scale=settings.resolution_scale;
        renderer->reset(active_scale);
        configure_enhancements();
        active_aspect_ratio=settings.aspect_ratio;active_widescreen=active_aspect_ratio!=0;active_motion_blur=settings.motion_blur;renderer->aspect_ratio(settings.aspect());
    }
    BootResult play(const std::filesystem::path& executable,const std::filesystem::path& disc,const std::filesystem::path& frame,
                    std::span<const MemorySlice> slices={},bool trace_io=false) {
        Xbe image(executable);if(!image.supported()) throw std::runtime_error("The executable does not match the supported Burnout 2 release.");
        configure_graphics();
        diagnostic_record(std::format("{{\"type\":\"game_start\",\"xbe\":{},\"disc\":{},\"sha256\":{}}}",
            json(path_text(executable)),json(path_text(disc)),json(image.sha256)));
        std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
        XboxHost host{renderer.get(),input.get(),[&]{return poll_game();},[&]{game_flip();}};
        if(controls) host.storage_root=settings_path.parent_path()/"automation-user";
        host.widescreen=active_widescreen;
        host.ultrawide=active_aspect_ratio==2;host.motion_blur=active_motion_blur;
        host.skip_intro=settings.skip_intro;host.fast_load=settings.fast_load;host.clean_hud=settings.clean_hud;host.draw_distance=unsigned(settings.draw_distance);
        if(settings.custom_music && music_songs) host.custom_music_root=music_cache();
        host.mods_root=mods_root;host.enabled_mods=settings.enabled_mods;
        host.loop_track=settings.loop_track;
        host.music_loop_changed=[this](bool enabled) {settings.loop_track=enabled;settings_dirty=true;save_settings();};
        host.achievements=&achievements;
        host.performance=&performance;
        host.trace_io=trace_io;
        host.watch_semaphores=watch_semaphores;
        Xbox xbox(image,memory,ram,disc,std::move(host));
        xbox.output_gain(settings.output_gain(focused));
        struct Session {App& app;~Session(){app.stop_benchmark();app.stop_performance();app.game=nullptr;app.playing=false;app.show_settings=true;app.published_at=0;}} session{*this};
        game=&xbox;playing=true;show_settings=false;cancel_bindings();frames=first_frame=last_frame=published_at=first_source=last_source=0;pacer={};source_pacer={};display_rate={};game_rate={};memory_snapshot="[]";
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
        report<<std::format("],\"adapter\":{},\"shader_compilations\":{},\"uploaded_vertex_bytes\":{},\"reused_vertex_bytes\":{},\"vblank_callbacks\":{},\"refresh_rate\":{},\"resolution_scale\":{},\"widescreen\":{},\"aspect_ratio\":{},\"motion_blur\":{},\"source_fps\":{:.3f},\"presented_fps\":{:.3f},\"original_camera_far\":{},\"camera_far\":{},\"original_world_sections\":{},\"extra_world_sections\":{},\"extra_world_batches\":{},\"display_refresh\":{},\"output_resolution_scale\":{},\"memory\":{}}}",
            json(result.graphics_adapter),stats.shader_compilations,stats.vertex_bytes,stats.reused_vertex_bytes,result.vblank_callbacks,settings.refresh_rate,active_scale,active_widescreen,active_aspect_ratio,active_motion_blur,source_fps,fps,result.original_camera_far,result.camera_far,result.original_world_sections,result.extra_world_sections,result.extra_world_batches,display_refresh,settings.resolution_scale,memory_snapshot);
        return report.str();
    }
};
}
int run_app(const std::filesystem::path& xbe,const std::filesystem::path& disc,unsigned seconds,const std::filesystem::path& frame,
            const std::filesystem::path& controls,std::span<const MemorySlice> slices,bool trace_io,const std::filesystem::path& settings,std::span<const std::uint32_t> watch_semaphores) {
    if(watch_semaphores.size()>4) throw std::runtime_error("At most four semaphore memory watches are supported");
    if(xbe.empty()) {
        std::filesystem::current_path(executable_path().parent_path());
    }
    App app(false,false,controls,settings);
    diagnostic_record("{\"type\":\"window_ready\",\"window\":"+app.window_diagnostic()+'}');
    std::copy(watch_semaphores.begin(),watch_semaphores.end(),app.watch_semaphores.begin());
    if(seconds) app.stop_at=SDL_GetTicksNS()+std::uint64_t(seconds)*1000000000;
    if(!xbe.empty()) {
        const auto executable=path_text(xbe),image=path_text(disc);
        if(executable.size()>=app.settings.xbe.size() || image.size()>=app.settings.disc.size())
            throw std::runtime_error("Game-file path exceeds the settings limit.");
        copy_text(app.settings.xbe.data(),app.settings.xbe.size(),executable.c_str());
        copy_text(app.settings.disc.data(),app.settings.disc.size(),image.c_str());
        for(;;) {
            app.launch=false;
            const auto result=app.play(utf8_path(app.settings.xbe.data()),utf8_path(app.settings.disc.data()),frame,slices,trace_io);
            std::cout<<app.run_report(result)<<'\n';
            if(!app.running || !app.launch) return result.boundary.empty() || result.boundary.find("Native window closed")!=std::string::npos?0:3;
        }
    }
    while(app.poll()) {
        app.display(true);
        if(app.launch) {
            app.launch=false;
            if(app.music_import_requested) {app.begin_music_import();continue;}
            app.log_message("Starting game...");app.display(true);
            try {
                const auto result=app.play(utf8_path(app.settings.xbe.data()),utf8_path(app.settings.disc.data()),{});
                app.log_message(app.launch?"Restarting game...":result.boundary.empty()?"Game exited.":result.boundary);
            }
            catch(const std::exception& error) {
                diagnostic_record("{\"type\":\"game_error\",\"message\":"+json(error.what())+'}');
                app.launch=false;app.playing=false;app.show_settings=true;app.log_message(error.what());
            }
            if(!app.launch && !diagnostic_log().empty()) app.log_message("Log file: "+path_text(diagnostic_log()));
        }
    }
    return 0;
}
std::string check_window(const std::filesystem::path& path,bool warp) {
    auto preferences=path;preferences+=".cfg";
    App app(true,warp,{},preferences);
    if(!app.achievement_audio || !app.achievement_audio_size ||
       !std::any_of(app.achievement_audio_data,app.achievement_audio_data+app.achievement_audio_size,[](Uint8 value){return value!=0;}))
        throw std::runtime_error("Achievement notification sound is missing or silent");
    const auto resize=[&](int width,int height) {
        if(!SDL_SetWindowSize(app.window,width,height) || !SDL_SyncWindow(app.window))
            throw std::runtime_error(SDL_GetError());
    };
    if(app.settings.refresh_rate!=60) throw std::runtime_error("Default refresh rate differs from 60 Hz");
    if(app.settings.fast_load) throw std::runtime_error("Fast load must default to disabled");
    if(app.settings.hdr || app.settings.hdr_paper_white!=200 || app.settings.hdr_peak) throw std::runtime_error("Invalid default HDR settings");
    if(!app.settings.clean_hud) throw std::runtime_error("Clean HUD must preserve the enabled default");
    if(app.settings.mute_on_focus_loss) throw std::runtime_error("Focus muting must default to disabled");
    if(!path.parent_path().empty()) std::filesystem::create_directories(path.parent_path());
    FrameRate counter_rate;counter_rate.sample(1);
    for(unsigned frame=1;frame<=60;++frame) counter_rate.sample(1+frame*1000000000ULL/120);
    if(std::abs(counter_rate.value-120)>0.01) throw std::runtime_error("Frame rate counter did not measure 120 FPS");
    Preferences saved(false);saved.resolution_scale=3;saved.select_refresh_rate(144);saved.show_fps=true;saved.aspect_ratio=2;saved.motion_blur=false;saved.bindings[15]=SDL_SCANCODE_Z;
    saved.custom_music=true;saved.loop_track=true;saved.mute_on_focus_loss=true;copy_text(saved.music_folder.data(),saved.music_folder.size(),"Music/Custom playlist");
    saved.antialiasing=6;saved.anisotropy=16;saved.ambient_occlusion=3;saved.ambient_occlusion_strength=47;
    saved.hdr=true;saved.hdr_paper_white=240;saved.hdr_peak=800;
    saved.skip_intro=true;saved.fast_load=true;saved.clean_hud=false;saved.draw_distance=4;
    saved.enabled_mods={"paint","sound"};
    saved.controller_bindings[0][15]={ControllerSource::button,SDL_GAMEPAD_BUTTON_SOUTH};
    saved.controller_bindings[1][16]={ControllerSource::negative_axis,SDL_GAMEPAD_AXIS_RIGHTX};saved.controller_bindings[3][4]={};
    saved.save(preferences);Preferences restored(true,preferences);
    if(restored.bindings!=saved.bindings || restored.controller_bindings!=saved.controller_bindings || restored.resolution_scale!=3 || restored.refresh_rate!=144 || !restored.show_fps || restored.aspect_ratio!=2 || restored.motion_blur ||
       !restored.custom_music || !restored.loop_track || !restored.mute_on_focus_loss || restored.music_folder!=saved.music_folder || restored.antialiasing!=6 || restored.anisotropy!=16 || restored.ambient_occlusion!=3 || restored.ambient_occlusion_strength!=47 ||
       !restored.skip_intro || !restored.fast_load || restored.clean_hud || restored.draw_distance!=4 || restored.enabled_mods!=saved.enabled_mods ||
       !restored.hdr || restored.hdr_paper_white!=240 || restored.hdr_peak!=800)
        throw std::runtime_error("New settings did not survive save/load");
    restored.volume=37;
    if(restored.output_gain(false)!=0 || restored.output_gain(true)!=.37f)
        throw std::runtime_error("Focus muting did not preserve the selected volume");
    restored.muted=true;
    if(restored.output_gain(false)!=0 || restored.output_gain(true)!=0)
        throw std::runtime_error("Focus restored manually muted audio");
    restored.muted=false;restored.mute_on_focus_loss=false;restored.save(preferences);
    if(restored.output_gain(false)!=.37f || Preferences(true,preferences).mute_on_focus_loss)
        throw std::runtime_error("Focus muting could not be disabled");
    const auto default_volume=app.settings.volume;
    app.settings.volume=37;app.settings.mute_on_focus_loss=true;
    SDL_Event focus{};focus.type=SDL_EVENT_WINDOW_FOCUS_GAINED;focus.window.windowID=SDL_GetWindowID(app.window);app.event(focus);
    if(!app.focused || SDL_GetAudioStreamGain(app.achievement_audio)!=.37f) throw std::runtime_error("Focus gain or notification volume was ignored");
    focus.type=SDL_EVENT_WINDOW_FOCUS_LOST;app.event(focus);
    if(app.focused || SDL_GetAudioStreamGain(app.achievement_audio)!=0) throw std::runtime_error("Focus loss or notification muting was ignored");
    app.settings.mute_on_focus_loss=false;app.settings.muted=true;app.update_audio_gain();
    if(SDL_GetAudioStreamGain(app.achievement_audio)!=0) throw std::runtime_error("Mute did not silence achievement notifications");
    app.settings.muted=false;app.settings.volume=default_volume;app.update_audio_gain();
    saved.clean_hud=true;saved.save(preferences);
    if(!Preferences(true,preferences).clean_hud) throw std::runtime_error("Enabled Clean HUD did not survive save/load");
    for(const auto value:{1,2,4}) {
        saved.draw_distance=value;saved.save(preferences);
        if(Preferences(true,preferences).draw_distance!=value) throw std::runtime_error("Draw distance did not survive save/load");
    }
    saved.select_refresh_rate(0);saved.save(preferences);
    if(Preferences(true,preferences).refresh_rate) throw std::runtime_error("Unlimited refresh rate did not survive save/load");
    FramePacer unlimited;
    unlimited.due(60,1000000000);unlimited.advance();
    if(!unlimited.due(0,1000000001) || unlimited.period || unlimited.next!=1000000001)
        throw std::runtime_error("Unlimited refresh rate retained a capped deadline");
    unlimited.advance();
    if(!unlimited.due(144,1000000002) || unlimited.period!=1000000000ULL/144 || unlimited.next!=1000000002)
        throw std::runtime_error("Capped refresh rate did not resume after Unlimited");
    {std::ofstream file(preferences);file<<"highest_lod=1\nunlimited_distance=1\n";}
    if(Preferences(true,preferences).draw_distance!=2) throw std::runtime_error("Legacy distance settings were not migrated");
    {std::ofstream file(preferences);file<<"draw_distance=1\nunlimited_distance=1\n";}
    if(Preferences(true,preferences).draw_distance!=1) throw std::runtime_error("Legacy distance replaced an explicit new setting");
    {std::ofstream file(preferences);file<<"widescreen=1\n";}
    if(Preferences(true,preferences).aspect_ratio!=1 || !Preferences(true,preferences).motion_blur)
        throw std::runtime_error("Legacy settings did not preserve widescreen and original motion blur");
    {std::ofstream file(preferences);file<<"aspect_ratio=2\nwidescreen=0\n";}
    if(Preferences(true,preferences).aspect_ratio!=2) throw std::runtime_error("Legacy widescreen replaced an explicit aspect ratio");
    {std::ofstream file(preferences);file<<"key_accelerate="<<SDL_SCANCODE_ESCAPE<<"\nkey_brake=999999\nrefresh_rate=1\nresolution_scale=99\nantialiasing=7\nanisotropy=3\ndraw_distance=3\nambient_occlusion=4\nambient_occlusion_strength=101\naspect_ratio=3\nhdr_paper_white=79\nhdr_peak=10001\n";}
    Preferences invalid(true,preferences);std::filesystem::remove(preferences);
    if(invalid.bindings!=default_keyboard_bindings() || invalid.refresh_rate!=60 || invalid.resolution_scale!=1 || invalid.antialiasing || invalid.anisotropy || invalid.draw_distance!=1 || invalid.ambient_occlusion || invalid.ambient_occlusion_strength!=35 || invalid.aspect_ratio || !invalid.motion_blur) throw std::runtime_error("Invalid settings replaced valid defaults");
    if(invalid.hdr || invalid.hdr_paper_white!=200 || invalid.hdr_peak) throw std::runtime_error("Invalid HDR settings replaced valid defaults");
    {std::ofstream file(preferences);file<<"pad1_accelerate=-lefttrigger\npad2_start=unknown\npad5_a=south\npad1_unknown=east\n";}
    if(Preferences(true,preferences).controller_bindings!=default_controller_profiles()) throw std::runtime_error("Invalid controller bindings replaced defaults");
    std::filesystem::remove(preferences);
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
    constexpr const char* names[]={"game-files","general","audio","graphics","enhancements","controls","achievements","mods","log"};
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
        images<<std::format("{{\"category\":{},\"image\":{}}}",json(names[tab]),json(path_text(output)));
        app.renderer->present(false);
    }
    // Reopen a panel after mouse focus, then verify keyboard focus still works.
    // Escape must only toggle settings, not select the child panel itself.
    const auto settings_frame=[&](bool present=true) {
        app.renderer->begin(0xFF16344A);app.renderer->end();app.renderer->display();
        app.settings_interface();if(present) app.renderer->present(false);
    };
    {
        // Exercise the settings workflow with an isolated virtual controller,
        // without starting the game or reading the user's physical controls.
        auto fixture=path;fixture+=".controller";{std::ofstream file(fixture);file<<"# Settings controller diagnostic\n";}
        const auto* previous_hint=SDL_GetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS);
        const std::optional<std::string> background=previous_hint?std::optional<std::string>{previous_hint}:std::nullopt;
        SDL_SetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS,"1");
        ControlScript pad(fixture);std::filesystem::remove(fixture);
        auto original_input=std::move(app.input);app.input=std::make_unique<Input>(false,false,pad.id);
        const auto tick=[&] {SDL_UpdateJoysticks();app.input->update(true);app.update_controller_binding();};
        app.control_selection=1;app.select_category=5;settings_frame();
        SDL_SetJoystickVirtualButton(pad.joystick,SDL_GAMEPAD_BUTTON_SOUTH,true);tick();app.start_controller_binding(15);tick();settings_frame();
        if(app.input->capture_state()!=ControllerCapture::neutral || !app.controller_navigation_blocked || app.controller_binding_action!=15)
            throw std::runtime_error("Controller binding captured its activation press");
        SDL_SetJoystickVirtualButton(pad.joystick,SDL_GAMEPAD_BUTTON_SOUTH,false);tick();settings_frame();
        SDL_SetJoystickVirtualButton(pad.joystick,SDL_GAMEPAD_BUTTON_EAST,true);tick();settings_frame();
        const ControllerBinding bound{ControllerSource::button,SDL_GAMEPAD_BUTTON_EAST};
        if(app.settings.controller_bindings[0][15]!=bound || app.controller_binding_action!=-1 || !app.controller_navigation_blocked || !app.show_settings)
            throw std::runtime_error("Controller capture did not update the selected action safely");
        SDL_SetJoystickVirtualButton(pad.joystick,SDL_GAMEPAD_BUTTON_EAST,false);tick();settings_frame(false);
        if(app.controller_navigation_blocked || Preferences(true,preferences).controller_bindings[0][15]!=bound)
            throw std::runtime_error("Controller binding did not save or restore navigation");
        auto controller_image=path;controller_image.replace_filename(controller_image.stem().string()+"-controllers.png");
        save_png(app.renderer->read_display(),controller_image);app.renderer->present(false);
        app.start_controller_binding(14);tick();app.event(escape);tick();
        if(app.controller_binding_action!=-1 || !app.show_settings) throw std::runtime_error("Escape did not cancel controller binding");
        app.start_controller_binding(14);tick();app.controller_binding_deadline=0;app.update_controller_binding();tick();
        if(app.controller_binding_action!=-1) throw std::runtime_error("Controller binding did not time out");
        app.settings.controller_bindings=default_controller_profiles();app.settings_dirty=true;app.input->controller_bindings(app.settings.controller_bindings);
        app.cancel_bindings();tick();settings_frame();app.control_selection=0;app.input=std::move(original_input);
        if(background) SDL_SetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS,background->c_str());else SDL_ResetHint(SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS);
    }
    auto& ui=ImGui::GetIO();
    ui.ClearInputKeys();ui.AddFocusEvent(true);
    app.playing=true;app.select_category=7;
    for(unsigned frame=0;frame<3;++frame) settings_frame();
    ui.AddMousePosEvent(550,450);settings_frame();
    ui.AddMouseButtonEvent(0,true);settings_frame();
    ui.AddMouseButtonEvent(0,false);settings_frame();
    for(const auto key:{SDL_SCANCODE_ESCAPE,SDL_SCANCODE_F1}) {
        SDL_Event toggle{};toggle.key.windowID=SDL_GetWindowID(app.window);
        toggle.key.scancode=key;toggle.key.key=key==SDL_SCANCODE_ESCAPE?SDLK_ESCAPE:SDLK_F1;
        for(unsigned press=0;press<2;++press) {
            toggle.type=SDL_EVENT_KEY_DOWN;app.event(toggle);settings_frame();
            toggle.type=SDL_EVENT_KEY_UP;app.event(toggle);settings_frame();
            if(app.show_settings!=(press==1)) throw std::runtime_error("Settings shortcut did not toggle the panel");
            if(ui.NavVisible) throw std::runtime_error("Settings shortcut selected a panel navigation outline");
        }
    }
    ui.AddKeyEvent(ImGuiKey_Tab,true);settings_frame();
    ui.AddKeyEvent(ImGuiKey_Tab,false);settings_frame();
    if(!ui.NavVisible) throw std::runtime_error("Keyboard settings navigation lost its focus indicator");
    ui.AddMouseButtonEvent(0,true);settings_frame();
    ui.AddMouseButtonEvent(0,false);settings_frame();
    app.playing=false;
    for(const int category:{4,5,6,7}) {
        resize(640,480);
        app.select_category=category;
        for(unsigned frame=0;frame<3;++frame) {
            app.poll();app.renderer->begin(0xFF16344A);app.renderer->end();app.renderer->display();
            app.settings_interface();if(frame!=2) app.renderer->present(false);
        }
        const auto image=app.renderer->read_display();
        if(image.width!=640 || image.height!=480) throw std::runtime_error("Minimum settings window dimensions differ");
        auto output=path.parent_path()/path.stem();output+="-small-";output+=names[category];output+=path.extension();
        save_png(image,output);app.renderer->present(false);
        images<<std::format(",{{\"category\":{},\"image\":{}}}",json(std::string("small-")+names[category]),json(path_text(output)));
    }
    resize(960,720);
    if(achievement_opacity(0)!=0 || achievement_opacity(250000000)!=1 || achievement_opacity(4650000000ULL)!=1 ||
       achievement_opacity(achievement_duration)!=0 || std::abs(achievement_opacity(125000000)-.5f)>.0001f ||
       std::abs(achievement_opacity(4825000000ULL)-.5f)>.0001f)
        throw std::runtime_error("Achievement fades do not cover the notification's start and end");
    app.achievements.crash_result(0,1);app.show_settings=false;
    for(unsigned frame=0;frame<3;++frame) settings_frame();
    if(app.achievement_notification!=AchievementId::crash_course || !app.achievements.unlocked(AchievementId::crash_course))
        throw std::runtime_error("Achievement notification did not appear with settings closed");
    if(SDL_GetAudioStreamQueued(app.achievement_audio)!=int(app.achievement_audio_size))
        throw std::runtime_error("Achievement sound was omitted or played more than once");
    for(const auto [name,elapsed]:std::array<std::pair<const char*,std::uint64_t>,3>{{
        {"achievement-unlocked",1000000000ULL},{"achievement-fade-in",125000000},{"achievement-fade-out",4825000000ULL}}}) {
        app.achievement_started=SDL_GetTicksNS()-elapsed;settings_frame(false);
        auto toast_image=path.parent_path()/path.stem();toast_image+="-";toast_image+=name;toast_image+=".png";
        save_png(app.renderer->read_display(),toast_image);app.renderer->present(false);
        images<<std::format(",{{\"category\":{},\"image\":{}}}",json(name),json(path_text(toast_image)));
    }
    app.save_achievements(true);
    Achievements restored_achievements;restored_achievements.load(app.achievements_path);
    restored_achievements.crash_result(29,3);
    if(!app.achievements_error.empty() || app.achievements.dirty() || !restored_achievements.unlocked(AchievementId::crash_course) ||
       restored_achievements.dirty() || restored_achievements.take_notification())
        throw std::runtime_error("Achievement save/load lost progress or repeated its notification");
    app.achievement_started=SDL_GetTicksNS()-achievement_duration;settings_frame();
    if(app.achievement_notification) throw std::runtime_error("Achievement notification did not expire");
    app.show_settings=true;app.select_category=6;
    for(unsigned frame=0;frame<3;++frame) settings_frame(frame!=2);
    auto unlocked_image=path.parent_path()/path.stem();unlocked_image+="-achievements-complete.png";
    save_png(app.renderer->read_display(),unlocked_image);
    app.renderer->present(false);
    images<<std::format(",{{\"category\":\"achievements-complete\",\"image\":{}}}",json(path_text(unlocked_image)));
    const auto click_reset=[&] {
        ui.AddMousePosEvent(500,252);settings_frame();
        ui.AddMouseButtonEvent(0,true);settings_frame();
        ui.AddMouseButtonEvent(0,false);settings_frame();
        for(unsigned frame=0;frame<2;++frame) settings_frame();
        if(!app.achievement_reset_open || app.achievements.count()!=1)
            throw std::runtime_error("Reset did not request confirmation before clearing progress");
    };
    const auto press=[&](ImGuiKey key) {
        ui.AddKeyEvent(key,true);settings_frame();ui.AddKeyEvent(key,false);settings_frame();
    };
    click_reset();SDL_Delay(180);settings_frame(false);
    auto confirmation_image=path.parent_path()/path.stem();confirmation_image+="-achievements-reset.png";
    save_png(app.renderer->read_display(),confirmation_image);app.renderer->present(false);
    images<<std::format(",{{\"category\":\"achievements-reset\",\"image\":{}}}",json(path_text(confirmation_image)));
    SDL_Event cancel{};cancel.type=SDL_EVENT_KEY_DOWN;cancel.key.windowID=SDL_GetWindowID(app.window);
    cancel.key.scancode=SDL_SCANCODE_ESCAPE;cancel.key.key=SDLK_ESCAPE;app.event(cancel);settings_frame();
    cancel.type=SDL_EVENT_KEY_UP;app.event(cancel);settings_frame();
    if(app.achievement_reset_open || !app.show_settings || app.achievements.count()!=1 || ImGui::IsKeyDown(ImGuiKey_Escape))
        throw std::runtime_error("Escape did not cancel the reset while keeping settings and progress");
    click_reset();press(ImGuiKey_Enter);
    if(app.achievement_reset_open || app.achievements.count()!=1)
        throw std::runtime_error("Reset confirmation did not default to Cancel");
    click_reset();press(ImGuiKey_Tab);press(ImGuiKey_Enter);
    if(app.achievement_reset_open || app.achievements.count())
        throw std::runtime_error("Confirming Reset did not clear achievement progress");
    auto legacy=app.achievements_path;legacy+=".legacy";
    write_text(legacy,"b2-achievements-v1\ncrash_course=1\n",true);
    Achievements legacy_achievements;legacy_achievements.load(legacy);
    if(!legacy_achievements.enabled() || legacy_achievements.count()!=1 || legacy_achievements.take_notification())
        throw std::runtime_error("Existing achievement files lost progress or became disabled");
    auto complete_path=app.achievements_path;complete_path+=".complete";
    Achievements complete;
    for(unsigned i=0;i<achievement_definitions.size();++i) complete.unlock(AchievementId(i));
    complete.save(complete_path);Achievements restored_complete;restored_complete.load(complete_path);
    if(restored_complete.count()!=achievement_definitions.size() || restored_complete.take_notification())
        throw std::runtime_error("The full achievement list did not persist cleanly");
    app.achievements.reset();app.achievements.crash_result(0,1);settings_frame();
    app.achievements.enabled(false);settings_frame(false);
    auto disabled_image=path.parent_path()/path.stem();disabled_image+="-achievements-disabled.png";
    save_png(app.renderer->read_display(),disabled_image);app.renderer->present(false);app.save_achievements(true);
    images<<std::format(",{{\"category\":\"achievements-disabled\",\"image\":{}}}",json(path_text(disabled_image)));
    Achievements disabled;disabled.load(app.achievements_path);
    if(disabled.enabled() || disabled.count()!=1 || app.achievement_notification || app.achievements.take_notification() || SDL_GetAudioStreamQueued(app.achievement_audio))
        throw std::runtime_error("Disabling achievements lost progress, kept a notification or did not persist");
    app.achievements.reset();app.achievements.crash_result(29,3);settings_frame();app.save_achievements(true);
    Achievements reset;reset.load(app.achievements_path);
    if(reset.count() || reset.enabled() || app.achievements.count() || app.achievement_notification || app.achievements.take_notification())
        throw std::runtime_error("Reset did not persist or disabled achievements continued unlocking");
    app.achievements.enabled(true);settings_frame();
    if(app.achievement_notification || app.achievements.count()) throw std::runtime_error("Enabling achievements replayed a disabled result");
    app.achievements.crash_result(29,1);settings_frame();
    if(app.achievement_notification!=AchievementId::crash_course) throw std::runtime_error("Reset achievements could not be earned again");
    app.achievements.reset();settings_frame();app.save_achievements(true);
    Achievements cleared;cleared.load(app.achievements_path);
    if(app.achievement_notification || cleared.count() || !cleared.enabled() || !app.achievements_error.empty() || SDL_GetAudioStreamQueued(app.achievement_audio))
        throw std::runtime_error("Reset kept an active notification or enabling did not persist");
    images<<']';app.select_category=0;app.settings.vsync=false;
    app.capture_frame();const auto window_capture=app.pending_window_capture.parent_path();app.display(true);
    const auto captured_window=load_image(window_capture/"window.png");
    if(captured_window.width!=960 || captured_window.height!=720 || !std::filesystem::exists(window_capture/"window.json") ||
       !std::filesystem::exists(window_capture/"settings.cfg") || !app.pending_window_capture.empty())
        throw std::runtime_error("Settings capture omitted the window image or display details");
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
    app.settings.aspect_ratio=1;app.configure_graphics();
    for(unsigned wide=0;wide<4;++wide) {
        app.settings.aspect_ratio=wide>=2?2:1;app.configure_graphics();
        if(wide==2) resize(960,720);
        if(wide%2) resize(1280,540);
        app.poll();app.renderer->begin(0xFF4080C0);app.renderer->end();
        if(!app.renderer->display()) throw std::runtime_error("Widescreen image could not be displayed");
        const auto image=app.renderer->read_display();
        const auto black=[&](unsigned x,unsigned y) {
            const auto pixel=(std::size_t(y)*image.width+x)*4;
            return image.rgba[pixel]==std::byte{} && image.rgba[pixel+1]==std::byte{} && image.rgba[pixel+2]==std::byte{};
        };
        if(black(image.width/2,image.height/2) ||
           (wide%2?(wide==1?(!black(40,image.height/2) || !black(image.width-40,image.height/2)):
                              (black(40,image.height/2) || black(image.width-40,image.height/2))):
                    (!black(image.width/2,40) || !black(image.width/2,image.height-40))))
            throw std::runtime_error("Selected display aspect ratio was not preserved");
        auto output=path.parent_path()/path.stem();output+=wide>=2?"-ultrawide":"-widescreen";output+=wide%2?"-wide-window":"";output+=path.extension();
        save_png(image,output);app.renderer->present(false);
    }
    app.settings.aspect_ratio=0;app.configure_graphics();
    resize(960,720);
    app.poll();
    std::array<double,2> source_rates{},display_rates{};
    for(unsigned test=0;test<2;++test) {
        app.settings.refresh_rate=test?144:120;app.playing=true;app.pacer={};app.source_pacer={};app.published_at=app.frames=0;
        std::uint64_t started=0;
        for(unsigned source=0;source<=30;++source) {
            app.renderer->set_target();app.renderer->begin(source&1?0xFF16344A:0xFF345616);app.renderer->end();app.game_flip();
            if(!source) started=app.published_at;
        }
        source_rates[test]=30*1000000000.0/(app.published_at-started);
        display_rates[test]=(app.frames-1)*1000000000.0/(app.last_frame-app.first_frame);
        if(std::abs(source_rates[test]-app.settings.refresh_rate)>app.settings.refresh_rate*0.08 || std::abs(display_rates[test]-app.settings.refresh_rate)>app.settings.refresh_rate*0.08 || app.frames!=31)
            throw std::runtime_error(std::format("Original-frame timing: source {:.2f}, display {:.2f}",source_rates[test],display_rates[test]));
        app.playing=false;app.published_at=0;
    }
    if(const auto messages=app.renderer->errors();!messages.empty()) throw std::runtime_error("Settings validation: "+messages.front());
    // Use the live application path with existing resources, without booting.
    app.playing=true;
    const auto retained_target=app.renderer->create_target(32,32);
    app.renderer->set_target(retained_target);app.renderer->begin(0xFF4080C0);app.renderer->end();app.renderer->set_target();
    app.settings.resolution_scale=2;app.settings.antialiasing=3;app.settings.anisotropy=16;
    app.settings_pending=true;app.apply_settings();
    const auto retained=app.renderer->read_target(retained_target);
    if(retained.width!=64 || retained.height!=64 || retained.rgba[0]!=std::byte{64})
        throw std::runtime_error("Live settings discarded an existing game target");
    app.renderer->begin(0xFF4080C0);app.renderer->end();app.renderer->publish_frame();
    const auto scaled=app.renderer->read_frame();
    if(scaled.width!=1280 || scaled.height!=960) throw std::runtime_error("Selected internal resolution was not applied");
    if(scaled.rgba[0]!=std::byte{64} || scaled.rgba[1]!=std::byte{128} || scaled.rgba[2]!=std::byte{192} || scaled.rgba[3]!=std::byte{255})
        throw std::runtime_error("Built antialiasing shader changed a flat colour");
    if(const auto messages=app.renderer->errors();!messages.empty()) throw std::runtime_error("Resolution validation: "+messages.front());
    if(app.renderer->supports_msaa(4)) {
        app.settings.antialiasing=5;app.settings_pending=true;app.apply_settings();
        const auto target=app.renderer->create_target(32,32);app.renderer->set_target(target);
        app.renderer->begin(0xFF0000FF,0.25f);app.renderer->clear_stencil(0xA5);app.renderer->end();
        app.renderer->grow_target(target,64,64);
        const auto copied=app.renderer->readback();
        if(copied.rgba[0]!=std::byte{0} || copied.rgba[2]!=std::byte{255} || copied.rgba[3]!=std::byte{255})
            throw std::runtime_error("Built MSAA transfer shader changed the source colour");
        app.renderer->set_target();app.renderer->begin(0xFF4080C0);app.renderer->end();app.renderer->publish_frame();
        if(!app.renderer->display()) throw std::runtime_error("MSAA image could not be displayed");
        const auto displayed=app.renderer->read_display();const auto centre=(std::size_t(displayed.height/2)*displayed.width+displayed.width/2)*4;
        if(displayed.rgba[centre]!=std::byte{64} || displayed.rgba[centre+1]!=std::byte{128} || displayed.rgba[centre+2]!=std::byte{192})
            throw std::runtime_error("MSAA resolve was not presented to the window");
        app.renderer->present(false);
    }
    app.playing=false;app.settings.resolution_scale=1;app.settings.antialiasing=app.settings.anisotropy=0;app.configure_graphics();
    resize(640,480);
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
    // Exercise the actual opt-in recorder, source/display separation, GPU
    // query association and no-boot export through the same app path.
    app.settings.refresh_rate=60;app.playing=true;
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
    for(bool enabled:{true,false,true,false}) {
        app.settings.hdr=enabled;app.settings.hdr_paper_white=200;app.settings.hdr_peak=1000;app.settings_pending=true;
        SDL_Event display{};display.type=SDL_EVENT_WINDOW_HDR_STATE_CHANGED;app.event(display);app.apply_settings();
        const auto hdr=renderer.hdr_status();
        if(hdr.active!=(enabled && hdr.available)) throw std::runtime_error("HDR did not follow the display's capabilities");
        renderer.begin(0xFF808080);renderer.end();renderer.publish_frame();
        if(!renderer.display()) throw std::runtime_error("HDR switch lost window presentation");
        ImGui_ImplDX11_NewFrame();ImGui_ImplSDL3_NewFrame();ImGui::NewFrame();
        ImGui::GetForegroundDrawList()->AddRectFilled({16,16},{48,48},IM_COL32_WHITE);
        ImGui::Render();ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());
        const auto shown=renderer.read_display();
        const auto offset=(std::size_t(24)*shown.width+24)*4;
        if(shown.rgba[offset]!=std::byte{255} || shown.rgba[offset+1]!=std::byte{255} || shown.rgba[offset+2]!=std::byte{255})
            throw std::runtime_error("HDR changed interface reference white");
        renderer.present(false);
    }
    if(renderer.stats().shader_compilations) throw std::runtime_error("Settings compiled shaders during runtime");
    const auto messages=app.renderer->errors();
    if(!messages.empty()) throw std::runtime_error("SDL3/ImGui validation: "+messages.front());
    app.settings.volume=37;app.settings_dirty=true;app.launch=true;app.binding_action=15;
    SDL_Event close{};close.type=SDL_EVENT_WINDOW_CLOSE_REQUESTED;app.event(close);
    Preferences closed(true,preferences);
    if(app.running || app.launch || app.binding_action!=-1 || app.settings_dirty || closed.volume!=37)
        throw std::runtime_error("Exit did not cancel launch and save pending settings");
    std::filesystem::remove(preferences);
    return std::format("{{\"format\":\"b2-window-check-v4\",\"passed\":true,\"backend\":\"SDL3/D3D11/ImGui\",\"adapter\":{},\"window\":{},\"settings_font\":{},\"heading_font\":{},\"default_refresh_rate\":60,"
        "\"bindings_and_settings\":true,\"controller_remapping\":true,\"controller_capture\":true,\"automatic_settings_save\":true,\"restart_request\":true,\"exit_request\":true,\"widescreen_presentation\":true,\"ultrawide_presentation\":true,\"motion_blur_setting\":true,\"frame_counter\":true,\"counter_image\":{},\"escape_toggle\":true,\"resolution_change\":true,"
        "\"pacing_60_hz\":{:.3f},\"pacing_120_hz\":{:.3f},\"source_at_120_hz\":{:.3f},\"source_at_144_hz\":{:.3f},\"display_120_hz\":{:.3f},\"display_144_hz\":{:.3f},\"hdr_settings\":true,\"hdr_switching\":true,\"hdr_display_available\":{},"
        "\"achievements_save_load\":true,\"achievements_reset\":true,\"achievement_reset_confirmation\":true,\"achievements_disable\":true,\"achievement_notification\":true,\"achievement_sound\":true,\"achievement_fades\":true,\"window_capture\":true,\"performance_capture\":true,\"shader_compilations\":0,\"game_booted\":false,\"game_fps_established\":false,\"images\":{}}}",
        json(renderer.adapter()),app.window_diagnostic(),json(ImGui::GetIO().FontDefault->GetDebugName()),json(app.heading_font->GetDebugName()),
        json(path_text(counter_image)),measured[0],measured[1],source_rates[0],source_rates[1],display_rates[0],display_rates[1],renderer.hdr_status().available,images.str());
}
}
