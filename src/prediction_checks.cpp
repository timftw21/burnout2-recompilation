#include "cpu_checks.h"
#include "cpu.h"
#include "file.h"
#include "xbe.h"
#include "xbox.h"
#include "disc.h"
#include "input.h"
#include <SDL3/SDL.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <sstream>

namespace b2 {
namespace {
constexpr std::uint32_t arena=0x01000000,stack=arena+0xF000,returned=0xABCDEF00;
constexpr std::uint32_t body(unsigned player) {return arena+player*0x3000;}
constexpr std::uint32_t matrix(unsigned player) {return arena+0x7000+player*0x100;}
// Local diagnostic checkpoints only, never a packet or a guest-memory wire format.
// These are the mutable body fields of original 5AD00 and its math helpers.
constexpr std::pair<unsigned,unsigned> fields[]={{0x60,0xA0},{0x100,0x10},
    {0x134,4},{0x140,0x70},{0x844,12},{0x868,4}};
constexpr unsigned body_words=(0xA0+0x10+4+0x70+12+4)/4+16;
struct Checkpoint {
    std::array<std::array<std::uint32_t,body_words>,2> bodies{};
    FloatingState floating;
    std::uint32_t ticks=0;
};
void require(bool value,std::string_view message) {
    if(!value) throw std::runtime_error(std::string(message));
}
struct Fixture {
    std::vector<std::byte> image_bytes,storage;
    Memory memory;
    ExecutionDiagnostic diagnostic;
    Cpu cpu;
    explicit Fixture(Xbe& image):image_bytes(image.base+image.image_size),storage(0x10000),memory(0,image_bytes) {
        image.load(std::span(image_bytes).subspan(image.base,image.image_size));memory.map(arena,storage);
        cpu.diagnostic=&diagnostic;
        // Real car-body virtual table, verified against the original image below.
        for(unsigned p=0;p<2;++p) {
            memory.store<std::uint32_t>(body(p),0x295E08);
            memory.store(body(p)+0x10,matrix(p));
            identity(matrix(p));identity(body(p)+0x140);identity(body(p)+0x180);
            set(body(p)+0x130,1200);
            set(body(p)+0x100,1);
            set(body(p)+0x60,p?17.f:23.f);set(body(p)+0x68,p?-3.f:2.f);
            set(matrix(p)+0x30,p?10.f:-10.f);
        }
        // An explicitly synthetic free-body fixture: no road, gravity or contacts.
        // This must never be presented as a driving/traffic/collision check.
        std::memset(memory.access(0x4F08F0,12),0,12);
        memory.store<std::uint32_t>(0x352720,0);call(0xD7240,0);
    }
    void set(std::uint32_t address,float value) {memory.store(address,std::bit_cast<std::uint32_t>(value));}
    float get(std::uint32_t address) {return std::bit_cast<float>(memory.load<std::uint32_t>(address));}
    void identity(std::uint32_t address) {for(unsigned i=0;i<3;++i) set(address+i*0x14,1);}
    void call(std::uint32_t entry,std::uint32_t object,std::optional<std::uint32_t> argument={}) {
        diagnostic.remaining=50000;cpu.registers[esp]=stack;cpu.registers[ecx]=object;
        memory.store(stack,returned);if(argument) memory.store(stack+4,*argument);
        invoke_native(cpu,memory,entry);
        require(cpu.eip==returned && cpu.registers[esp]==stack+(argument?8:4),"Original diagnostic calling convention differs");
        require(!cpu.floating.active,"Checkpoint captured an active guest floating context");
    }
    void step(unsigned tick,bool wrong_second=false) {
        for(unsigned p=0;p<2;++p) {
            // Inputs here are force/torque samples, not invented throttle physics.
            const auto sign=((tick/3+p)%2)?-1.f:1.f;
            set(body(p)+0xB0,sign*(p?160.f:240.f)*(wrong_second && p==1?-1.f:1.f));
            set(body(p)+0xB8,sign*(p?40.f:-20.f));
            set(body(p)+0xC4,sign*(p?0.02f:-0.03f));
            call(0x5AD00,body(p),0x3C888889);
            for(const auto offset:{0x30U,0x34U,0x38U}) require(std::isfinite(get(matrix(p)+offset)),"Non-finite original body position");
            for(unsigned axis=0;axis<3;++axis) for(unsigned component=0;component<3;++component)
                require(std::isfinite(get(matrix(p)+axis*16+component*4)),"Non-finite original body orientation");
        }
        call(0xD73B0,0);
    }
    Checkpoint save() {
        Checkpoint result;
        for(unsigned p=0;p<2;++p) {
            auto* output=result.bodies[p].data();
            for(const auto [offset,size]:fields) {std::memcpy(output,memory.access(body(p)+offset,size),size);output+=size/4;}
            std::memcpy(output,memory.access(matrix(p),64),64);
        }
        result.floating=cpu.floating;result.ticks=memory.load<std::uint32_t>(0x59818C);
        return result;
    }
    void restore(const Checkpoint& value,bool omit_velocity=false) {
        for(unsigned p=0;p<2;++p) {
            const auto* input=value.bodies[p].data();
            for(const auto [offset,size]:fields) {
                if(omit_velocity && offset==0x60) {
                    // Deliberately omit just the linear velocity for the negative control.
                    std::memcpy(memory.access(body(p)+offset+12,size-12),input+3,size-12);
                } else std::memcpy(memory.access(body(p)+offset,size),input,size);
                input+=size/4;
            }
            std::memcpy(memory.access(matrix(p),64),input,64);
        }
        cpu.floating=value.floating;memory.store(0x59818C,value.ticks);
    }
    bool equal(const Checkpoint& value) {
        const auto actual=save();
        return actual.bodies==value.bodies && actual.ticks==value.ticks &&
            actual.floating.image==value.floating.image && actual.floating.mxcsr==value.floating.mxcsr &&
            actual.floating.xmm==value.floating.xmm;
    }
    bool same_bodies(Fixture& other) {
        for(unsigned p=0;p<2;++p) {
            if(std::memcmp(memory.access(body(p),0x2100),other.memory.access(body(p),0x2100),0x2100) ||
                std::memcmp(memory.access(matrix(p),64),other.memory.access(matrix(p),64),64)) return false;
        }
        return true;
    }
};
}
std::string check_prediction(const std::filesystem::path& path,const std::filesystem::path& output) {
    if(std::filesystem::exists(output)) throw std::runtime_error("Prediction checks require a fresh output directory");
    Xbe image(path);require(image.supported(),"Prediction checks require the verified US target");
    const auto started=std::chrono::steady_clock::now();
    unsigned checks=0,replayed=0;bool entries_verified=false;
    std::ostringstream results;
    const auto record=[&](std::string_view name,const auto& run) {
        if(checks) results<<',';
        results<<"{\"name\":"<<json(name);
        try {run();results<<",\"passed\":true}";} catch(const std::exception& error) {
            results<<",\"passed\":false,\"error\":"<<json(error.what())<<'}';
        }
        ++checks;
    };
    record("original_entry_points",[&] {
        require(hex_bytes(image.data(0x5AD00,6))=="558BEC83E4F0","Original body integration signature differs");
        require(u32(image.data(0x295E08+0x14,4),0)==0x66410,"Original car-body update table differs");
        for(const auto address:{0x5AD00U,0x66410U,0x822B0U,0x74000U,0x88850U,0x88890U,0xD7240U,0xD73B0U}) {
            const auto found=std::ranges::lower_bound(compiled_batch,address,{},&CompiledFunction::address);
            require(found!=compiled_batch.end() && found->address==address && found->source_sha256==image.sha256,
                "Required original function was not compiled from this image");
        }
        entries_verified=true;
    });
    record("two_bodies_restore_and_replay",[&] {
        require(entries_verified,"Original entry points must be verified before execution");
        Fixture reference(image),predicted(image);
        constexpr unsigned ticks=24;
        std::array<Checkpoint,ticks+1> history{};history[0]=reference.save();
        for(unsigned tick=0;tick<ticks;++tick) {reference.step(tick);history[tick+1]=reference.save();}
        require(reference.get(matrix(0)+0x30)>-9 && reference.get(matrix(1)+0x30)>11,
            std::format("Both original bodies must move: x0={}, x1={}",reference.get(matrix(0)+0x30),reference.get(matrix(1)+0x30)));
        for(unsigned delay:{0U,3U,6U,9U}) {
            predicted.restore(history[0]);
            for(unsigned tick=0;tick<ticks;++tick) predicted.step(tick,true);
            const auto wrong=predicted.save();
            require(wrong.bodies[0]==history[ticks].bodies[0] && wrong.bodies[1]!=history[ticks].bodies[1],
                "A wrong player-two history must change only the second body's state");
            const auto acknowledged=ticks-delay;predicted.restore(history[acknowledged]);
            for(unsigned tick=acknowledged;tick<ticks;++tick) {predicted.step(tick);++replayed;}
            require(predicted.equal(history[ticks]),"Restored original bodies differ from uninterrupted updates");
            require(predicted.same_bodies(reference),"Replay left body state outside the checkpoint inconsistent");
            require(reference.equal(history[ticks]),"Restoring another peer modified authoritative state");
        }
        predicted.restore(history[0]);for(unsigned tick=0;tick<ticks;++tick) predicted.step(tick,true);
        predicted.restore(history[11],true);
        for(unsigned tick=11;tick<ticks;++tick) predicted.step(tick);
        require(!predicted.equal(history[ticks]),"Omitting velocity incorrectly passed restoration");
    });
    record("original_replay_does_not_restore_two_player_state",[&] {
        require(entries_verified,"Original entry points must be verified before execution");
        Fixture fixture(image);auto& memory=fixture.memory;
        constexpr unsigned controller=0x2FFD68,input=arena+0x9000,buffer=arena+0xA000;
        memory.store(0x3D686C,buffer);
        for(unsigned p=0;p<4;++p) {
            const auto car=0x464220+p*0x3090;
            memory.store<std::uint32_t>(0x2FE370+p*4,p<2?car:0);
            if(p<2) memory.store(car+8,body(p));
        }
        fixture.call(0x88850,controller,0);
        fixture.set(input+8,0.75f);fixture.set(input+12,-0.5f);fixture.set(input+16,0.25f);
        for(unsigned tick=0;tick<32;++tick) fixture.call(0x88890,controller,input);
        require(memory.load<std::uint32_t>(controller)==32 && memory.load<std::uint32_t>(controller+4)==32,
            "Original replay did not record one six-byte control record per update");
        fixture.call(0x88850,controller,1);
        fixture.set(matrix(0)+0x30,-9);fixture.set(matrix(1)+0x30,9);
        fixture.call(0x88890,controller,input);
        require(memory.load<std::uint32_t>(controller+0x14)==0 && fixture.get(matrix(0)+0x30)==-9 &&
            fixture.get(matrix(1)+0x30)==9,"Replay unexpectedly restored positions or detected offsetting position errors");
        for(unsigned p=0;p<2;++p) {
            fixture.call(0x88850,controller,1);
            fixture.set(matrix(0)+0x30,0);fixture.set(matrix(1)+0x30,0);
            memory.store<std::uint32_t>(matrix(p)+0x30,0x3F800001);
            fixture.call(0x88890,controller,input);
            require(memory.load<std::uint32_t>(controller+0x14)==1,
                "Replay checksum did not observe the selected player's position");
        }
    });
    const auto component=results.str();
    const bool components_passed=component.find("\"passed\":false")==std::string::npos;
    const auto elapsed=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
    const auto report=std::format("{{\"format\":\"b2-prediction-feasibility-v1\",\"source_sha256\":{},"
        "\"game_booted\":false,\"runtime_decoder\":false,\"status\":\"incomplete\",\"gate_passed\":false,\"components_passed\":{},"
        "\"simulation_hz\":60,\"correction_history_ticks\":[0,3,6,9],"
        "\"fixture\":\"two_free_bodies_without_contacts\",\"checkpoint_bytes\":{},\"replayed_ticks\":{},"
        "\"elapsed_ms\":{},\"checks\":[{}],\"unverified\":[\"original_player_controls\","
        "\"two_car_contacts\",\"road_and_traffic\",\"random_state\",\"crash_and_respawn\","
        "\"audio_effects_and_storage_side_effects\"]}}",
        json(image.sha256),components_passed,sizeof(Checkpoint),replayed,elapsed,component);
    std::filesystem::create_directories(output);write_text(output/"report.json",report,false);
    return report;
}
}

namespace b2 {
namespace {
struct OfflinePads {
    struct Feedback {unsigned calls=0;std::uint16_t low=0,high=0;};
    std::array<std::uint32_t,2> ids{};
    std::array<SDL_Joystick*,2> joysticks{};
    std::array<Feedback,2> feedback{};
    OfflinePads() {
        if(!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) throw std::runtime_error(SDL_GetError());
        try {
            for(unsigned p=0;p<2;++p) {
                SDL_VirtualJoystickDesc description{};SDL_INIT_INTERFACE(&description);
                description.type=SDL_JOYSTICK_TYPE_GAMEPAD;description.naxes=SDL_GAMEPAD_AXIS_COUNT;
                description.nbuttons=SDL_GAMEPAD_BUTTON_COUNT;description.axis_mask=(1U<<SDL_GAMEPAD_AXIS_COUNT)-1;
                description.button_mask=(1U<<SDL_GAMEPAD_BUTTON_COUNT)-1;description.name="Burnout 2 headless race controller";
                description.userdata=&feedback[p];description.Rumble=[](void* context,Uint16 low,Uint16 high) {
                    auto& feedback=*static_cast<Feedback*>(context);++feedback.calls;feedback.low=low;feedback.high=high;return true;
                };
                ids[p]=SDL_AttachVirtualJoystick(&description);if(ids[p]) joysticks[p]=SDL_OpenJoystick(ids[p]);
                if(!joysticks[p]) throw std::runtime_error(SDL_GetError());
                controls(p,0,0,0);
            }
        } catch(...) {close();throw;}
    }
    void close() {
        for(unsigned p=0;p<2;++p) {
            if(joysticks[p]) SDL_CloseJoystick(joysticks[p]);if(ids[p]) SDL_DetachVirtualJoystick(ids[p]);
        }
        SDL_QuitSubSystem(SDL_INIT_GAMEPAD);
    }
    ~OfflinePads() {close();}
    void controls(unsigned p,float throttle,float brake,float steer) {
        const auto axis=[&](SDL_GamepadAxis axis,int value) {
            require(SDL_SetJoystickVirtualAxis(joysticks[p],axis,static_cast<Sint16>(value)),SDL_GetError());
        };
        axis(SDL_GAMEPAD_AXIS_RIGHT_TRIGGER,int(throttle*65535)-32768);
        axis(SDL_GAMEPAD_AXIS_LEFT_TRIGGER,int(brake*65535)-32768);
        axis(SDL_GAMEPAD_AXIS_LEFTX,int(steer*32767));
    }
};
}
std::string check_prediction_race(const std::filesystem::path& executable,const std::filesystem::path& disc_path,
                                  const std::filesystem::path& output) {
    Xbe image(executable);Disc disc(disc_path);
    if(!image.supported() || std::filesystem::exists(output))
        throw std::runtime_error("Race prediction requires the verified image and a fresh output directory");
    std::filesystem::create_directories(output);
    std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
    OfflinePads pads;Input input(false,true,pads.ids);
    XboxHost provider;provider.storage_root=output/"storage";provider.input=&input;provider.audio_output=false;
    Xbox xbox(image,memory,ram,disc_path,std::move(provider));auto& cpu=xbox.isolated_cpu();
    constexpr unsigned return_sentinel=0xFFFFFFF0;
    struct OfflineServices {Xbox* xbox;std::uint64_t cycles=0;} services{&xbox};
    GuestClock clock{&services,[](void* context) {return static_cast<OfflineServices*>(context)->cycles;}};
    cpu.clock=&clock;cpu.preempt=[](Cpu& cpu) {static_cast<OfflineServices*>(cpu.clock->context)->xbox->isolated_io();};
    const auto stack=cpu.registers[esp]-8192;
    std::vector<std::byte> world,scratch(4096);
    std::ostringstream calls,frames;unsigned count=0,frame_count=0;
    std::string failure,phase="initialization";
    bool sound_initialized=false;
    const auto invoke=[&](unsigned address,unsigned object,std::initializer_list<unsigned> arguments={},bool callee_pop=true) {
        cpu.diagnostic->remaining=2000000;cpu.registers[ecx]=object;cpu.registers[esp]=stack;
        memory.store<std::uint32_t>(stack,return_sentinel);unsigned index=0;
        for(auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
        if(count++) calls<<',';
        calls<<"{\"address\":"<<json(hex32(address))<<",\"object\":"<<json(hex32(object))<<'}';
        invoke_native(cpu,memory,address);
        if(cpu.eip!=return_sentinel || cpu.registers[esp]!=stack+4+(callee_pop?arguments.size()*4:0))
            throw std::runtime_error("Original race fixture calling convention differs at "+hex32(address));
        return cpu.registers[eax];
    };
    try {
        invoke(0xE65CB,0);invoke(0xE6053,0);
        const auto heap=memory.load<std::uint32_t>(0x5A8974);
        if(!heap) throw std::runtime_error("Original SDK heap initialization failed");
        memory.store<std::uint32_t>(0x5A619C,heap);
        invoke(0xE653E,0);invoke(0xE64E6,0);
        invoke(0xD5310,0x3D6870);
        invoke(0x5BD90,0);invoke(0x5F0F0,0);invoke(0xD7240,0);
        invoke(0x13000,0);invoke(0x12BC0,0x3525E0);
        invoke(0xD5BE0,0x3D70D0);
        const auto allocate=[&](unsigned address,unsigned size) {
            require(invoke(0xDFA30,0,{size,address,0,4})==(address|0x80000000U),"Original fixed asset allocation failed at "+hex32(address));
        };
        const auto load=[&](std::string_view path,unsigned address,unsigned limit) {
            const auto entry=disc.find(path);require(entry.size<=limit,"Original fixture asset exceeds allocation");
            disc.read(entry,0,{static_cast<std::byte*>(memory.access(address,entry.size)),entry.size});
        };
        allocate(0x381C000,0x64000);allocate(0x3880000,0x640000);allocate(0x2477000,0x200000);
        allocate(0x2D10000,0x64000);memory.store<std::uint32_t>(0x3D686C,0x82D10000);
        allocate(0x2877000,0x5D000);allocate(0x28D4000,0x43C000);memory.store<std::uint32_t>(0x34AB44,0x82877000);
        for(unsigned p=0;p<2;++p) {
            const auto model=0x82D74000+p*0x106000;
            allocate(model&0x7FFFFFFF,0x106000);memory.store<std::uint32_t>(0x52180C+p*4,model);
        }
        require(invoke(0xE8DA0,0,{0,1,0x4000},false)!=0,"Original RenderWare core initialization failed");
        require(invoke(0xE9270,0,{0},false)!=0,"Original RenderWare frame arena configuration failed");
        invoke(0xFE660,0);invoke(0x106900,0);invoke(0xA0480,0);
        memory.store<std::uint32_t>(0xA240,0xA280);
        memory.store<std::uint32_t>(0xA280,0x11);memory.store<std::uint32_t>(0xA284,0x2A);
        memory.store<std::uint32_t>(0xA288,0x120);memory.store<std::uint32_t>(0xA28C,60);
        require(invoke(0xE8CF0,0,{0xA240},false)!=0,"Original RenderWare driver open failed");
        require(invoke(0xF2B20,0,{0x3445C0,0x5ADCA0},false)!=0,"Original RenderWare plugin construction failed");
        // 13550 installs the title's file provider before loading sound banks.
        // RenderWare's default CRT provider has no title-relative disc paths.
        require(invoke(0x13550,0x34AB58)==1,"Original title file provider installation failed");
        constexpr char dictionary_path[]="frontend/global.dic";
        std::memcpy(memory.access(0xA100,sizeof(dictionary_path)),dictionary_path,sizeof(dictionary_path));
        const auto dictionary_stream=invoke(0xECB30,0,{2,1,0xA100},false);
        require(dictionary_stream!=0,"Original effects dictionary stream did not open");
        require(invoke(0xED2C0,0,{dictionary_stream,0xA200,4},false)!=0,"Original effects dictionary header did not load");
        require(invoke(0xED440,0,{dictionary_stream,0x16,0,0},false)!=0,"Original effects dictionary chunk is absent");
        const auto dictionary=invoke(0xED030,0,{dictionary_stream},false);
        require(dictionary!=0,"Original effects dictionary did not load");memory.store<std::uint32_t>(0x443EEC,dictionary);
        invoke(0xECA60,0,{dictionary_stream,0},false);invoke(0xEB510,0,{dictionary},false);
        require(invoke(0xCC3E0,0x443FA0)!=0,"Original sound service initialization failed");
        invoke(0xD4240,invoke(0xCB3E0,0x443FA0));
        sound_initialized=true;phase="race_initialization";
        invoke(0x92480,0x48A150);
        load("trackpss/forward/fwyl.pss",0x8381C000,0x6A4000);
        memory.store<std::uint32_t>(0x4B85B8,0x8381C000);memory.store<std::uint32_t>(0x5ACE74,0x34);
        invoke(0x8BCD0,0x4B85B0);
        load("tracks/forward/fwyl/fwyl1_01.bgd",0x82477000,0x200000);
        memory.store<std::uint32_t>(0x2FFFF8,1);memory.store<std::uint32_t>(0x2FFFFC,0x82477000);
        require(invoke(0x79C70,0x2FFFF8)==1,"Original race asset fixup failed");
        const auto sectors=memory.load<std::uint32_t>(0x8381C000+0x5C);
        const auto stream=disc.find("tracks1/forward/fwyl/fwyl.psw");
        require(sectors>0 && sectors<=256 && stream.size==sectors*0x3C000,"Original world dimensions differ");
        world.resize(stream.size);disc.read(stream,0,world);memory.map(0x8A000000,world);memory.map(0x0A000000,world);
        memory.map(0x0E000000,scratch);memory.store<std::uint32_t>(0x0E000010,0x8A000000);
        for(unsigned sector=0;sector<sectors;++sector) {
            memory.store<std::uint32_t>(0x0E000024,sector);invoke(0x8EEB0,0x0E000000);
            memory.store<std::uint32_t>(0x8A000000+sector*0x3C000+0xB4,2);
        }
        memory.store<std::uint32_t>(0x0E000004,sectors);memory.store<std::uint32_t>(0x0E000034,8);
        invoke(0x8F240,0x0E000000,{0x82477020});
        std::memcpy(memory.access(0x4B95B0,0x318),memory.access(0x0E000000,0x318),0x318);
        std::memcpy(memory.access(0x4B98C8,0x318),memory.access(0x0E000000,0x318),0x318);
        for(unsigned p=0;p<2;++p) {
            const auto car=0x4B9C50+p*0x4570;
            invoke(0x7E780,car);
            memory.store<std::uint32_t>(car+4,0);
            memory.store<std::uint32_t>(0x34AB58,0);invoke(0x7E7C0,car,{p,0});
            const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
            while(!memory.load<std::uint32_t>(car+0xF84) && std::chrono::steady_clock::now()<deadline) {
                xbox.isolated_io();invoke(0xD5460,0x3D6870);
            }
            require(memory.load<std::uint32_t>(car+0xF84)!=0,"Original car asset read did not finish");
            memory.store<std::uint32_t>(0x2FE370+p*4,car);
            memory.store<std::uint32_t>(0x34AB58,11);
            require(invoke(0x7F9F0,car,{0x82477020+p*0x10,0x82477060+p*0x10})==1,"Original human car model binding failed");
            invoke(0x16860,0x34AB68+p*0x18,{p});
            memory.store<std::uint32_t>(0x34AB68+p*0x18,0x3D80D0+p*0x104);
            memory.store<std::uint32_t>(car+0x4560,0x34AB68+p*0x18);
            const auto camera=invoke(0xE8670,0),frame=invoke(0xE9010,0);
            require(camera && frame,"Original headless camera/frame allocation failed");
            invoke(0xEF9C0,0,{camera,frame},false);
            memory.store<std::uint32_t>(0x4431F0+p*0x660,camera);
            memory.store<std::uint32_t>(0x34AB68+p*0x18+8,0x4431F0+p*0x660);
        }
        // Gameplay constructor blocks from 7FC50. Its preceding block loads
        // four frontend meshes used exclusively by the physics debug drawing.
        for(unsigned p=0;p<32;++p) invoke(0x5BE00,0x445010+p*0xF90);
        for(unsigned p=0;p<48;++p) invoke(0x5DF60,0x470470+p*0x890);
        invoke(0xCFDD0,0x2FE380);invoke(0x9F810,0x2FE370+0x1A44);invoke(0x787D0,0x2FE370+0x1F0);
        memory.store<std::uint32_t>(0x2FE370+0x1C78,2);
        memory.store<std::uint32_t>(0x2FE370+0x1D60,1);memory.store<std::uint32_t>(0x2FE370+0x1C60,1);
        memory.store<std::uint32_t>(0x2FE370+0x1A3C,1);
        invoke(0x88850,0x2FFD68,{0});
        memory.store<std::uint32_t>(0x2FFFE8,2);
        invoke(0x9A140,0x48A1C0,{0,2});
        memory.store<std::uint32_t>(0x48A150+0x2FAC4,3);
        require(invoke(0x93070,0x48A150,{0})!=0,"Original traffic and road initialization failed");
        for(unsigned p=0;p<2;++p) {memory.store<std::uint32_t>(0x3D8548+p*4,p);invoke(0x16880,0x34AB68+p*0x18);}
        invoke(0x3E430,0x4CB2C8);invoke(0x172D0,0x303BC4);invoke(0x198F0,0x3006F8);
        memory.store<std::uint32_t>(0x34AB5C,0x3006F8);memory.store<std::uint32_t>(0x3006F8+4,0x3006F8);
        for(unsigned p=0;p<2;++p) invoke(0x7ECD0,0x4B9C50+p*0x4570,{0});
        phase="race_updates";
        for(unsigned tick=0;tick<120;++tick) {
            pads.controls(0,0.5f,0,0);pads.controls(1,0.5f,0,0);input.update(true);
            invoke(0x19B00,0x3006F8);invoke(0xD73B0,0);
        }
        unsigned traffic=0;float nearest=1e30f;
        const auto human_matrix=memory.load<std::uint32_t>(0x4BCCE0+0x10);
        for(unsigned i=0;i<254;++i) {
            const auto candidate=0x48A1C0+i*0x1F0;
            if(!(memory.load<std::uint32_t>(candidate+0x100)&1) || memory.load<std::uint32_t>(candidate+0x1A0)) continue;
            float squared=0;for(unsigned axis=0;axis<3;++axis) {
                const auto delta=std::bit_cast<float>(memory.load<std::uint32_t>(candidate+0x30+axis*4))-
                    std::bit_cast<float>(memory.load<std::uint32_t>(human_matrix+0x30+axis*4));squared+=delta*delta;
            }
            if(squared<nearest) {nearest=squared;traffic=candidate;}
        }
        require(traffic!=0,"The original traffic simulation did not create an impact target");
        std::array<std::uint32_t,16> initial_pose{};
        std::memcpy(initial_pose.data(),memory.access(traffic,64),64);
        for(unsigned p=0;p<2;++p) {
            const auto body=0x4BCCE0+p*0x4570,matrix=memory.load<std::uint32_t>(body+0x10);
            std::memcpy(memory.access(matrix,64),initial_pose.data(),64);
            for(unsigned axis=0;axis<3;++axis) {
                const auto start=std::bit_cast<float>(initial_pose[12+axis]),direction=std::bit_cast<float>(initial_pose[8+axis]);
                const auto side=std::bit_cast<float>(initial_pose[axis]);
                memory.store<std::uint32_t>(matrix+0x30+axis*4,std::bit_cast<std::uint32_t>(start-direction*(p?20:6)+side*4*p));
            }
            invoke(0x5F360,body);invoke(0x606B0,body,{std::bit_cast<std::uint32_t>(p?0.f:80.f)});
        }
        for(unsigned tick=0;tick<480;++tick) {
            pads.controls(0,1,0,0);pads.controls(1,0,1,0);input.update(true);
            invoke(0x19B00,0x3006F8);invoke(0xD73B0,0);
            if(frame_count++) frames<<',';frames<<"{\"tick\":"<<tick<<",\"cars\":[";
            for(unsigned p=0;p<2;++p) {
                if(p) frames<<',';const auto actor=0x4B9C50+p*0x4570,body=memory.load<std::uint32_t>(actor+8);
                const auto matrix=memory.load<std::uint32_t>(body+0x10);
                frames<<"{\"body\":"<<json(hex32(body))<<",\"crash\":"<<memory.load<std::uint32_t>(actor+0xCD0)
                    <<",\"crash_time\":"<<std::bit_cast<float>(memory.load<std::uint32_t>(actor+0xCD4))
                    <<",\"race_state\":"<<memory.load<std::uint32_t>(actor+0xCC8)<<",\"road_index\":"<<memory.load<std::uint32_t>(actor+0xD4C)
                    <<",\"held\":"<<memory.load<std::uint32_t>(actor+0x34)<<",\"autopilot\":"<<memory.load<std::uint32_t>(actor+0x38)
                    <<",\"wheels\":"<<memory.load<std::uint32_t>(body+0xBA4)<<",\"pose_velocity_controls\":[";
                unsigned values=0;for(const auto at:{matrix+0x30,matrix+0x34,matrix+0x38,body+0x60,body+0x64,body+0x68,
                    actor+0x3090+0xDC8,actor+0x3090+0xDC0,actor+0x3090+0xDC4}) {
                    if(values++) frames<<',';const auto value=std::bit_cast<float>(memory.load<std::uint32_t>(at));
                    frames<<(std::isfinite(value)?std::format("{}",value):"null");
                }
                frames<<"],\"feedback\":"<<pads.feedback[p].calls<<'}';
            }
            unsigned active=0;for(unsigned i=0;i<254;++i) active+=(memory.load<std::uint32_t>(0x48A1C0+i*0x1F0+0x100)&1)!=0;
            frames<<"],\"traffic\":"<<active<<'}';
        }
    } catch(const std::exception& error) {failure=error.what();}
    std::ostringstream report;
    report<<"{\"format\":\"b2-race-prediction-v1\",\"source_sha256\":"<<json(image.sha256)
        <<",\"game_booted\":false,\"gate_passed\":false,\"status\":\"incomplete\",\"simulation_hz\":60,\"phase\":"<<json(phase)
        <<",\"sound_initialized\":"<<(sound_initialized?"true":"false")<<",\"failure\":"<<json(failure)
        <<",\"last_address\":"<<json(hex32(cpu.eip))<<",\"registers\":[";
    for(unsigned i=0;i<8;++i) {if(i) report<<',';report<<json(hex32(cpu.registers[i]));}
    report<<"],\"history\":[";
    const auto visits=cpu.diagnostic->visits;
    for(auto i=visits>64?visits-64:0;i<visits;++i) {
        if(i!=(visits>64?visits-64:0)) report<<',';
        report<<json(hex32(cpu.diagnostic->history[i%64]));
    }
    report<<"],\"stack\":[";
    for(unsigned i=0;i<32;++i) {
        if(i) report<<',';const auto value=memory.load<std::uint32_t>(cpu.registers[esp]+i*4);
        report<<"{\"value\":"<<json(hex32(value));
        if(value>=image.base && value<ram.size()-160) {
            std::string text;for(unsigned j=0;j<160;++j) {
                const auto c=memory.load<std::uint8_t>(value+j);if(c<32 || c>126) break;text+=char(c);
            }
            report<<",\"text\":"<<json(text);
        }
        report<<'}';
    }
    report<<"],\"cars\":[";
    for(unsigned p=0;p<2;++p) {
        if(p) report<<',';const auto car=0x4B9C50+p*0x4570;
        report<<"{\"address\":"<<json(hex32(car))<<",\"body\":"<<json(hex32(memory.load<std::uint32_t>(car+8)))
            <<",\"mass_bits\":"<<json(hex32(memory.load<std::uint32_t>(car+0x3090+0x130)))<<'}';
    }
    report<<"],\"services\":[";
    const auto host=xbox.isolated_result();
    for(unsigned i=0;i<host.service_calls.size();++i) if(host.service_calls[i]) report<<"{\"ordinal\":"<<i<<",\"calls\":"<<host.service_calls[i]<<"},";
    report<<"{}],\"threads_created\":"<<host.threads_created<<",\"audio_buffers\":"<<host.audio_buffers
        <<",\"audio_effects\":"<<host.audio_effects<<",\"audio_output_open\":"<<(host.audio_device_created?"true":"false")<<",\"failed_allocation\":[";
    for(unsigned i=0;i<host.last_failed_allocation.size();++i) {if(i) report<<',';report<<host.last_failed_allocation[i];}
    report<<"],\"sound_root\":[";
    const auto sound_root=memory.load<std::uint32_t>(0x443FA0+0x58);
    report<<sound_root<<','<<(sound_root?memory.load<std::uint32_t>(sound_root):0);
    report<<"],\"threads\":[";
    for(unsigned i=0;i<host.threads.size();++i) {if(i) report<<',';const auto& thread=host.threads[i];
        report<<"{\"routine\":"<<json(hex32(thread.routine))<<",\"state\":"<<json(thread.state)<<'}';}
    report<<"],\"io\":[";
    for(unsigned i=0;i<host.io.size();++i) {if(i) report<<',';const auto& io=host.io[i];report<<"{\"path\":"<<json(io.path)<<",\"status\":"<<json(hex32(io.status))<<'}';}
    report<<"],\"traffic_state\":[";
    unsigned items=0;for(const auto address:{0x48A154U,0x4B8458U,0x4B84A8U,0x4B82D0U,
        0x48A1C0U+0x2E188,0x48A1C0U+0x2E194,0x48A1C0U+0x2E120,0x48A1C0U+0x28EA0,0x48A1C0U+0x28EC0,0x5274E0U}) {
        if(items++) report<<',';report<<"{\"address\":"<<json(hex32(address))<<",\"value\":"<<memory.load<std::uint32_t>(address)<<'}';
    }
    report<<"],\"traffic_configuration\":[";
    const auto config=memory.load<std::uint32_t>(0x48A154);
    for(unsigned i=0;i<20;++i) {if(i) report<<',';report<<(config?memory.load<std::uint32_t>(config+i*4):0);}
    report<<"],\"traffic_resources\":[";
    const auto resource=memory.load<std::uint32_t>(0x48A1C0+0x2E11C);
    for(unsigned i=0;i<32;++i) {if(i) report<<',';report<<(resource?memory.load<std::uint32_t>(resource+i*4):0);}
    report<<"],\"argument_blocks\":[";
    for(unsigned i=0;i<3;++i) {
        if(i) report<<',';const auto pointer=memory.load<std::uint32_t>(cpu.registers[esp]+4+i*4);
        report<<"{\"pointer\":"<<json(hex32(pointer));
        if(pointer>=0x10000 && pointer<ram.size()-128) report<<",\"data\":"<<json(hex_bytes({static_cast<const std::byte*>(memory.access(pointer,128)),128}));
        report<<'}';
    }
    report<<"],\"frames\":["<<frames.str()<<"],\"calls\":["<<calls.str()
        <<"],\"unverified\":[\"complete_race_state_restore\",\"bounded_prediction\",\"replay_side_effects\"]}";
    write_text(output/"report.json",report.str(),false);return report.str();
}
}
