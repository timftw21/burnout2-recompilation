#include "xbox.h"
#include "custom_music.h"
#include "disc.h"
#include "mods.h"
#include "tracks.h"
#include "input.h"
#include "audio.h"
#include "spatial.h"
#include "effects.h"
#include "render.h"
#include "frame_processing.h"
#include "presentation.h"
#include "diagnostics.h"
#include "crypto.h"
#include "host.h"
#ifdef __APPLE__
#include "metal.h"
#endif
#ifdef _WIN32
#include <bcrypt.h>
#else
#include <openssl/evp.h>
#include <openssl/hmac.h>
#endif
#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <map>
#include <limits>
#include <optional>
#include <sstream>

namespace b2 {
namespace {
constexpr std::uint32_t success=0,invalid_handle=0xC0000008,invalid_parameter=0xC000000D,
    no_memory=0xC0000017,conflicting_addresses=0xC0000018,timeout=0x102,
    name_not_found=0xC0000034,access_denied=0xC0000022,write_protected=0xC00000A2,
    buffer_too_small=0xC0000023,no_media=0xC0000013,end_of_file=0xC0000011;
constexpr std::uint32_t page=4096,dma_window=0x1000000,kernel_base=0x80010000,return_sentinel=0xFFFFFFF0;
constexpr std::uint32_t memory_nozero=0x800000; // Xbox MEM_NOZERO, used by its heap allocator.
std::uint32_t align_up(std::uint32_t value,std::uint32_t alignment) {
    if(!alignment || (alignment&(alignment-1)) || value>UINT32_MAX-(alignment-1))
        throw std::runtime_error("Invalid guest allocation alignment or size");
    return (value+alignment-1)&~(alignment-1);
}
struct ThreadExit { std::uint32_t status; };
std::string folded(std::string_view text) {
    std::string result(text);
    for(auto& ch:result) {if(ch=='/') ch='\\';else if(ch>='a' && ch<='z') ch-=32;}
    return result;
}
bool wildcard(std::string_view pattern,std::string_view name) {
    const auto a=folded(pattern),b=folded(name);
    std::size_t i=0,j=0,star=std::string::npos,matched=0;
    while(j<b.size()) {
        if(i<a.size() && (a[i]=='?' || a[i]==b[j])) {++i;++j;}
        else if(i<a.size() && a[i]=='*') {star=i++;matched=j;}
        else if(star!=std::string::npos) {i=star+1;j=++matched;}
        else return false;
    }
    while(i<a.size() && a[i]=='*') ++i;
    return i==a.size();
}
std::uint32_t file_error(DWORD error) {
    switch(error) {
    case ERROR_FILE_NOT_FOUND:return name_not_found;case ERROR_PATH_NOT_FOUND:return 0xC000003A;
    case ERROR_ACCESS_DENIED:return access_denied;case ERROR_SHARING_VIOLATION:return 0xC0000043;
    case ERROR_ALREADY_EXISTS:case ERROR_FILE_EXISTS:return 0xC0000035;
    case ERROR_DISK_FULL:case ERROR_HANDLE_DISK_FULL:return 0xC000007F;
    case ERROR_INVALID_PARAMETER:case ERROR_INVALID_NAME:return invalid_parameter;
    case ERROR_DIR_NOT_EMPTY:return 0xC0000101;case ERROR_HANDLE_EOF:return end_of_file;
    case ERROR_WRITE_PROTECT:return write_protected;case ERROR_NOT_ENOUGH_MEMORY:return no_memory;
    case ERROR_OPERATION_ABORTED:return 0xC0000120;
    default:throw std::runtime_error(std::format("Unmapped native file error {}",error));
    }
}
}
struct Xbox::State {
    enum class ThreadState { ready,running,waiting,ended };
    enum class WaitKind { none,critical,objects,delay };
    struct Thread {
        State* host=nullptr;
        Cpu cpu;
        void* fiber=nullptr;
        std::uint32_t object=0,tib=0,stack=0,stack_size=0,tls_size=0;
        std::uint32_t routine=0,context=0,system=0,id=0,exit_status=0;
        ThreadState state=ThreadState::ready;
        bool suspended=false,entry=false;
        std::uint32_t wait_object=0;
        WaitKind wait_kind=WaitKind::none;
        std::array<std::uint32_t,64> wait_objects{};
        std::uint32_t wait_count=0;
        bool wait_all=false;
        std::chrono::steady_clock::time_point due=std::chrono::steady_clock::time_point::max();
    };
    struct Allocation { std::uint32_t size,protect,caller; std::vector<std::uint32_t> pages; };
    struct OpenFile {
        DiscEntry entry{};
        std::string path,link,pattern;
        std::uint64_t position=0;
        std::uint32_t access=0,share=0,options=0;
        bool device=false,disc_file=false,listing_loaded=false;
        HANDLE native=INVALID_HANDLE_VALUE;
        std::filesystem::path host_path;
        ~OpenFile() {if(native!=INVALID_HANDLE_VALUE) CloseHandle(native);}
        std::vector<DiscEntry> listing;
        std::size_t listing_position=0;
    };
    struct Object {
        enum class Kind { thread,event,semaphore,file,link };
        Kind kind; std::uint32_t address; Thread* thread=nullptr;
        std::shared_ptr<OpenFile> file;
    };
    struct PendingIo {
        OVERLAPPED overlap{};
        std::shared_ptr<OpenFile> file;
        HANDLE native=INVALID_HANDLE_VALUE;
        std::uint32_t file_object=0,event=0,status=0,ordinal=0,callsite=0,requested=0,buffer=0,thread=0;
        std::uint64_t offset=0;
        bool active=false;
#ifdef _WIN32
        ~PendingIo() {if(overlap.hEvent) CloseHandle(overlap.hEvent);}
#endif
    };
    Xbe& image;
    Memory& memory;
    std::span<std::byte> ram;
    std::unique_ptr<Disc> disc;
    std::unique_ptr<Mods> mods;
    std::shared_ptr<OpenFile> disc_io;
    std::array<PendingIo,32> pending_io{};
    unsigned pending_io_count=0;
    std::unique_ptr<Input> owned_input;
    std::unique_ptr<Audio> audio;
    float output_gain=1;
    std::unique_ptr<Spatial> audio_spatial;
    struct AudioBuffer {
        std::uint32_t raw,descriptor,voice,identifier;AudioFormat format;
        std::uint32_t flags=0,input_bin=UINT32_MAX;
        SpatialParameters spatial{},deferred_spatial{};
        EnvironmentParameters environment{},deferred_environment{};
        std::uint32_t curve=0,curve_count=0,deferred_curve=0,deferred_curve_count=0;
        bool has_spatial=false,pending_spatial=false,pending_curve=false,pending_environment=false;
    };
    std::map<std::uint32_t,AudioBuffer> audio_buffers;
    struct AudioStream {
        std::uint32_t raw=0,descriptor=0,identifier=0,callback=0,context=0;
        AudioFormat format;
        std::vector<std::array<std::uint32_t,6>> packets;
        std::uint64_t submitted=0,completed=0;
    };
    std::map<std::uint32_t,AudioStream> audio_streams;
    std::uint32_t audio_device=0,audio_references=0;
    std::uint32_t audio_effect_descriptor=0,audio_effect_count=0;
    std::uint32_t audio_effect_program_words=0,audio_effect_scratch_bytes=0;
    std::uint32_t audio_reverb=UINT32_MAX,audio_crosstalk=UINT32_MAX;
    EnvironmentListener audio_environment{},audio_deferred_environment{};
    bool audio_pending_environment=false,audio_headphones=false;
    SpatialListener audio_listener,audio_deferred_listener;
    std::uint32_t audio_pending_listener=0;
    bool audio_full_hrtf=false;
    struct AudioEvent {
        std::uint32_t clock=0,address=0,caller=0,count=0,identifier=0,source=0,start=0,bytes=0;
        std::array<std::uint32_t,6> arguments{};
        std::array<std::byte,64> effect_data{};
        std::uint32_t effect_bytes=0;
    };
    std::array<AudioEvent,8192> audio_events{};
    std::uint32_t audio_event_count=0,audio_trace_until=0;
    bool audio_trace=false;
    bool ethernet_initialized=false;
    std::uint32_t ethernet_state=0;
    Input* input=nullptr;
    std::unique_ptr<Renderer> owned_graphics;
    Renderer* graphics=nullptr;
    XboxHost host;
    bool live=false;
    std::unique_ptr<Gpu> gpu;

    std::uint32_t gpu_cursor=0;
    bool graphics_initialized=false;
    struct InputHandle {std::uint32_t id=0,port=0;};
    std::array<InputHandle,32> input_handles{};
    std::uint32_t next_input_handle=0xF2100000;
    std::filesystem::path user_root;
    std::map<std::string,std::string> links{{"\\??\\D:","\\Device\\CdRom0"}};
    std::array<std::byte,65536> kernel_image{};
    KernelServices services;
    GuestClock clock;
    ExecutionDiagnostic diagnostic;
    std::array<std::uint64_t,512> service_calls{};
    std::deque<BootIo> io_trace;
    std::map<std::uint32_t,Allocation> allocations;
    std::array<std::uint32_t,5> last_failed_allocation{};
    std::map<std::uint32_t,Object> handles;
    struct Timer { std::chrono::steady_clock::time_point due; std::uint32_t period,dpc; };
    std::map<std::uint32_t,Timer> timers;
    std::deque<std::uint32_t> dpcs;
    std::map<std::uint32_t,unsigned> object_references;
    std::map<std::uint32_t,Object> objects;
    std::array<std::uint32_t,4> watched_addresses{};
    std::vector<std::unique_ptr<Thread>> threads;
    Thread* current=nullptr;
    Thread* isolated_owner=nullptr;
    void* scheduler=nullptr;
    bool converted=false,stopping=false,entry_returned=false,thread_cleanup_pending=false;
    std::string boundary;
    std::uint32_t next_handle=0x100,next_thread=4,heap_begin=0,gdt=0,kernel_cursor=0x1000,tick_address=0,shutdown_head=0;
    std::uint32_t thread_type=0,event_type=0,semaphore_type=0,file_type=0;
    std::uint32_t flicker_filter=0;
    bool luma_filter=false;
    std::vector<std::uint32_t> shutdown_registrations;
    std::size_t next_runnable=0;
    std::chrono::steady_clock::time_point started=std::chrono::steady_clock::now();
    std::chrono::steady_clock::time_point quantum_due;
    std::chrono::steady_clock::time_point video_started;
    std::uint64_t video_ticks=0,vblank_callbacks=0;
    GameTiming game_timing;
    Presentation presentation;
    static constexpr std::uint32_t world_base=0x8A000000,world_stride=0x3C000,world_scratch=0x0E000000;
    std::vector<std::byte> world_storage;
    std::array<std::optional<DiscEntry>,2> world_streams;
    std::array<std::filesystem::path,2> world_paths;
    std::optional<DiscEntry> world_loaded;
    std::filesystem::path world_loaded_path;
    struct WorldCone {
        std::array<float,2> center{},left{},right{};
        std::array<float,3> low{},high{};
        unsigned material=0;
        bool geometry=false,bounded=false;
        bool contains(const std::array<float,2>& point) const {
            const auto x=point[0]-center[0],z=point[1]-center[1];
            return !bounded || (left[0]*z-left[1]*x>=0 && right[0]*z-right[1]*x<=0);
        }
    };
    struct WorldRecord {
        unsigned section=0,origin=0,row=0;
        std::array<std::uint64_t,2> meshes{};
        bool contains(unsigned mesh) const {return (meshes[mesh/64]>>(mesh%64))&1;}
        void add(unsigned mesh) {meshes[mesh/64]|=1ULL<<(mesh%64);}
    };
    struct WorldSection {
        std::array<float,3> low{},high{};
        std::array<std::array<float,3>,4> corners{};
        float height=0;
        std::array<float,2> center{};
        std::array<WorldCone,80> cones{};
        std::array<std::uint64_t,2> backgrounds{};
        std::vector<WorldRecord> additions;
        std::vector<WorldRecord> props;
    };
    std::vector<WorldSection> world_sections;
    WorldView world_view;
    HudLayout hud_layout;
    std::vector<WorldRecord> world_draws;
    std::vector<std::uint64_t> world_coverage;
    std::vector<unsigned> world_handoff_draws,world_handoff_backgrounds;
    std::array<std::uint64_t,128> world_background_coverage{};
    std::array<std::uint64_t,256> world_prop_coverage{};
    std::array<bool,256> world_prop_admitted{};
    std::array<std::uint64_t,8*64> traffic_wheel_coverage{};
    std::array<unsigned,256> world_section_ids{};
    std::uint64_t world_applied_coverage=UINT64_MAX;
    struct WorldHandoff {
        std::uint32_t camera=0;
        unsigned origin=0,cursor=0;
        double fraction=0;
        std::array<std::uint8_t,64> views{};
        std::array<std::uint64_t,256> props{};
    };
    std::array<WorldHandoff,32> world_handoffs{};
    unsigned world_draw_count=0,world_original_count=0,world_added_count=0;
    unsigned world_origin=0;
    std::uint64_t world_added_batches=0;
    bool world_preparing=false;
    struct WorldCamera {std::uint32_t address=0;float original=0,applied=0;};
    std::array<WorldCamera,32> world_cameras{};
    std::uint32_t world_camera=0;
    float world_original_far=0;
    bool world_ready=false,world_reflecting=false;
    std::uint32_t music_menu_storage=0;
    bool music_repeated=false;
    struct ImportedTrack {
        bool selected=false;
        WorldMaterials materials;
        std::uint32_t storage=0;
        std::array<std::uint32_t,7> names{},welcomes{},tables{};
        std::uint32_t barrier=0;
        std::array<std::uint32_t,5> frames{};
        double animation=0;
        std::uint32_t playlist=0,songs=0;
        std::array<std::uint32_t,3> music_selection{};
        std::array<std::byte,68> music_playlist{};
        std::array<std::byte,36> first_song{};
    } imported;

    State(Xbe& image,Memory& memory,std::span<std::byte> ram,const std::filesystem::path& disc_path,XboxHost host):image(image),memory(memory),ram(ram),host(std::move(host)) {
        if(ram.size()!=64*1024*1024 || !image.supported()) throw std::runtime_error("Native Xbox requires the verified image and 64 MiB RAM");
        std::memcpy(audio_environment.data(),memory.access(0x00237BD8,sizeof(audio_environment)),sizeof(audio_environment));
        audio_deferred_environment=audio_environment;
        if(this->host.draw_distance!=1 && this->host.draw_distance!=2 && this->host.draw_distance!=4)
            throw std::runtime_error("Draw distance must be 1x, 2x or 4x.");
        if(this->host.draw_distance>1) {
            world_storage.resize(64U*1024*1024+4096);
            memory.map(world_base&0x7FFFFFFF,world_storage);memory.map(world_base,world_storage);
            world_view.list=world_scratch+0x400;
        }
        if(!disc_path.empty()) {
            disc=std::make_unique<Disc>(disc_path);
            const auto executable=disc->find("default.xbe");
            if(disc->sha256(executable)!=image.sha256) throw std::runtime_error("Mounted disc executable does not match the verified target");
            disc_io=std::make_shared<OpenFile>();
            disc_io->native=CreateFileW(disc_path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED|FILE_FLAG_RANDOM_ACCESS,nullptr);
            if(disc_io->native==INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot open mounted disc for native asynchronous reads");
        }
        if(!this->host.enabled_mods.empty()) {
            if(!disc || this->host.mods_root.empty()) throw std::runtime_error("Enabled mods require a mounted disc and a mods folder.");
            mods=std::make_unique<Mods>(this->host.mods_root,this->host.enabled_mods,*disc);
            diagnostic_record(mods->report());
        }
        if(this->host.storage_root.empty()) {
            user_root=executable_path().parent_path()/"data/user";
        } else user_root=std::filesystem::absolute(this->host.storage_root).lexically_normal();
        user_root=std::filesystem::absolute(user_root).lexically_normal();
        if(std::filesystem::weakly_canonical(user_root)!=user_root)
            throw std::runtime_error("Native storage path crosses a reparse point");
        std::filesystem::create_directories(user_root/"TDATA");
        std::filesystem::create_directories(user_root/"UDATA");
        if(!this->host.custom_music_root.empty()) {
            auto& root=this->host.custom_music_root;root=std::filesystem::absolute(root).lexically_normal();
            if(std::filesystem::weakly_canonical(root)!=root) throw std::runtime_error("Custom music storage crosses a link or junction.");
        }
        if(!this->host.imported_tracks.empty()) {
            auto& root=this->host.imported_tracks;root=std::filesystem::absolute(root).lexically_normal();
            validate_interstate(root);
        }
        heap_begin=align_up(image.base+image.image_size,page);
        memory.map(kernel_base,kernel_image);
        memory.map(0x80000000U+heap_begin,ram.subspan(heap_begin));
        memory.map(0xF0000000U+heap_begin,ram.subspan(heap_begin));
        clock={this,[](void* context) {
            return static_cast<State*>(context)->clock_ticks(733333333);
        }};
        services.context=this;
        bind<252>(); bind<253>(); bind<255>(); bind<258>(); bind<187>(); bind<184>(); bind<199>(); bind<217>();
        bind<165>(); bind<166>(); bind<171>(); bind<173>(); bind<179>(); bind<180>(); bind<182>();
        bind<15>(); bind<17>(); bind<23>(); bind<291>(); bind<277>(); bind<294>(); bind<306>();
        bind<301>(); bind<103>(); bind<104>(); bind<129>(); bind<160>(); bind<161>();
        bind<126>(); bind<127>(); bind<128>(); bind<143>(); bind<99>(); bind<238>(); bind<49>(); bind<47>();
        bind<107>(); bind<108>(); bind<112>(); bind<113>(); bind<119>(); bind<137>(); bind<149>(); bind<150>(); bind<97>();
        bind<145>(); bind<159>(); bind<158>(); bind<189>(); bind<193>(); bind<225>(); bind<186>(); bind<222>();
        bind<233>(); bind<234>(); bind<246>(); bind<250>(); bind<24>(); bind<289>(); bind<279>(); bind<269>();
        bind<304>(); bind<305>();
        bind<67>(); bind<69>(); bind<190>(); bind<196>(); bind<198>(); bind<202>(); bind<203>();
        bind<207>(); bind<210>(); bind<211>(); bind<215>(); bind<218>(); bind<219>(); bind<226>(); bind<236>();
        bind<327>();bind<328>();
        bind<335>();bind<336>();bind<337>();bind<340>();bind<260>();bind<308>();
        bind<2>();
        for(const auto& section:image.sections) {
            memory.store<std::uint32_t>(section.header_address+24,section.preloaded()?1:0);
            for(const auto pointer:{section.head_reference,section.tail_reference}) if(pointer) memory.store<std::uint16_t>(pointer,0);
        }
        for(const auto& section:image.sections) if(section.preloaded()) for(const auto pointer:{section.head_reference,section.tail_reference})
            if(pointer) memory.store<std::uint16_t>(pointer,memory.load<std::uint16_t>(pointer)+1);
        gdt=allocate(64,page,true,PAGE_READWRITE)|0x80000000U;
        memory.store<std::uint64_t>(gdt+8,0x00CF9B000000FFFF);
        memory.store<std::uint64_t>(gdt+16,0x00CF93000000FFFF);
        // This describes the native compatibility module. It contains no original
        // kernel INIT section or executable instruction stream to patch.
        // The guest queries a PE32 kernel image, independent of the host OS.
        const auto field=[&]<class T>(unsigned offset,T value){std::memcpy(kernel_image.data()+offset,&value,sizeof(value));};
        constexpr unsigned pe=0x100,optional=pe+24,section=optional+224;
        field(0,std::uint16_t{0x5A4D});field(0x3C,std::uint32_t{pe});field(pe,std::uint32_t{0x4550});
        field(pe+4,std::uint16_t{0x14C});field(pe+6,std::uint16_t{1});field(pe+20,std::uint16_t{224});
        field(optional,std::uint16_t{0x10B});field(optional+28,kernel_base);
        field(optional+32,page);field(optional+36,std::uint32_t{512});
        field(optional+56,static_cast<std::uint32_t>(kernel_image.size()));field(optional+60,page);
        std::memcpy(kernel_image.data()+section,".data",5);field(section+8,static_cast<std::uint32_t>(kernel_image.size()-page));
        field(section+12,page);field(section+36,std::uint32_t{0xC0000040});
        for(const auto& import : image.imports) memory.store<std::uint32_t>(import.address,0xFFF00000U+import.ordinal*16);
        const auto version=export_data(324,8);
        memory.store<std::uint16_t>(version,1); memory.store<std::uint16_t>(version+4,5838);
        export_data(322,8); // Retail ABI flags; the host has no devkit peripherals.
        initialize_save_keys();
        tick_address=export_data(156,4);
        export_data(164,4); // Cold native launch: no dashboard launch-data page.
        memory.store<std::uint32_t>(export_data(356,4),6); // Standard AV mode: native-resolution SD profile.
        shutdown_head=export_data(0,8);
        memory.store<std::uint32_t>(shutdown_head,shutdown_head); memory.store<std::uint32_t>(shutdown_head+4,shutdown_head);
        thread_type=export_data(259,28); event_type=export_data(16,28); semaphore_type=export_data(21,28);
        file_type=export_data(71,28);
        const auto name=export_data(326,64);
        constexpr std::string_view executable="\\Device\\CdRom0\\default.xbe";
        memory.store<std::uint16_t>(name,static_cast<std::uint16_t>(executable.size()));
        memory.store<std::uint16_t>(name+2,static_cast<std::uint16_t>(executable.size()+1));
        memory.store<std::uint32_t>(name+4,name+8);
        std::memcpy(memory.access(name+8,executable.size()+1),executable.data(),executable.size()+1);
    }
    ~State() {
        for(auto& request:pending_io) if(request.active) {
            CancelIoEx(request.native,&request.overlap);
            DWORD transferred=0;GetOverlappedResult(request.native,&request.overlap,&transferred,TRUE);
        }
        if(isolated_owner && scheduler) {
            stopping=true;
            for(auto& thread:threads) if(thread.get()!=isolated_owner && thread->state!=ThreadState::ended) {
                current=thread.get();SwitchToFiber(thread->fiber);
            }
            current=isolated_owner;
        }
        for(unsigned i=0;i<watched_addresses.size();++i) if(watched_addresses[i]) diagnostic_watch(i,nullptr);
        for(auto& thread : threads) if(thread->fiber) DeleteFiber(thread->fiber);
        if(converted) ConvertFiberToThread();
    }
    template<unsigned Ordinal> static void dispatch_kernel(void* context,Cpu& cpu,Memory&) {static_cast<State*>(context)->service<Ordinal>(cpu);}
    template<unsigned Ordinal> void bind() {services.entries[Ordinal]=dispatch_kernel<Ordinal>;}
    std::uint32_t export_data(unsigned ordinal,std::uint32_t size) {
        kernel_cursor=align_up(kernel_cursor,16);
        if(size>kernel_image.size()-kernel_cursor) throw std::runtime_error("Native kernel data exceeds its module");
        const auto address=kernel_base+kernel_cursor;
        kernel_cursor+=size;
        for(const auto& import : image.imports) if(import.ordinal==ordinal) memory.store<std::uint32_t>(import.address,address);
        return address;
    }
    void initialize_save_keys() {
        // The native profile owns its console identity; no original EEPROM is required.
        const auto path=user_root/"storage.key";
        if(GetFileAttributesW(path.c_str())!=INVALID_FILE_ATTRIBUTES &&
           (GetFileAttributesW(path.c_str())&FILE_ATTRIBUTE_REPARSE_POINT))
            throw std::runtime_error("Native save identity is a reparse point");
        std::array<std::byte,32> keys{};
        if(!std::filesystem::exists(path)) {
            random_bytes(keys.data(),keys.size());
            write_text(path,std::string_view(reinterpret_cast<const char*>(keys.data()),keys.size()),false);
        }
        File file(path);
        if(file.size()!=keys.size()) throw std::runtime_error("Native save identity has an invalid size; preserve storage.key with your saves");
        file.read(0,keys);
        if(std::ranges::all_of(Bytes(keys).first(16),[](std::byte value){return value==std::byte{};}))
            throw std::runtime_error("Native save identity has an invalid empty disk key");
        std::memcpy(memory.access(export_data(323,16),16),keys.data(),16);
        const auto certificate=image.data(u32(image.data(image.base+0x118,4),0),0x1D0);
        if(u32(certificate,0)<certificate.size()) throw std::runtime_error("Executable certificate has no save signing keys");
        const auto derive=[&](std::uint32_t destination,unsigned offset) {
            const auto key=xbox_hmac(Bytes(keys).subspan(16),Bytes(certificate).subspan(offset,16));
            std::memcpy(memory.access(destination,16),key.data(),16);
        };
        derive(export_data(325,16),0xC0);derive(export_data(353,16),0xB0);
        const auto alternate=export_data(354,256);
        for(unsigned i=0;i<16;++i) derive(alternate+i*16,0xD0+i*16);
        diagnostic_record("{\"type\":\"storage_ready\",\"root\":"+json(path_text(user_root))+"}");
    }
    std::uint32_t allocate(std::uint32_t size,std::uint32_t alignment,bool committed,std::uint32_t protect,
                           std::uint32_t requested=0,std::uint32_t lowest=0,std::uint32_t highest=UINT32_MAX,bool clear=true,bool top_down=false) {
        const auto fail=[&] {last_failed_allocation={size,alignment,requested,lowest,highest};return 0U;};
        if(!size) return fail();
        size=align_up(size,page); alignment=std::max(alignment,page);
        auto cursor=align_up(std::max(heap_begin,lowest),alignment);
        if(requested) { if(requested<heap_begin || (requested&(page-1))) return fail(); cursor=requested; }
        if(top_down && !requested) {
            // Contiguous allocations search downward within their physical bounds.
            auto end=std::min<std::uint64_t>(ram.size(),std::uint64_t(highest)+1);
            const auto low=cursor;cursor=0;
            for(auto it=allocations.rbegin();it!=allocations.rend();++it) {
                const auto base=it->first;
                const auto block_end=std::uint64_t(base)+it->second.size;
                if(end>=size) {
                    const auto candidate=(end-size)&~std::uint64_t(alignment-1);
                    if(candidate>=std::max<std::uint64_t>(low,block_end)) {cursor=static_cast<std::uint32_t>(candidate);break;}
                }
                end=std::min<std::uint64_t>(end,base);
            }
            if(!cursor && end>=size) {
                const auto candidate=(end-size)&~std::uint64_t(alignment-1);
                if(candidate>=low) cursor=static_cast<std::uint32_t>(candidate);
            }
            if(!cursor) return fail();
        } else {
            const auto find_space=[&](std::uint32_t candidate) {
                for(const auto& [base,block] : allocations) {
                    if(std::uint64_t(candidate)+size<=base) break;
                    if(candidate>=std::uint64_t(base)+block.size) continue;
                    if(requested) return 0U;
                    candidate=align_up(base+block.size,alignment);
                }
                return std::uint64_t(candidate)+size<=ram.size() &&
                    std::uint64_t(candidate)+size-1<=highest?candidate:0U;
            };
            const auto first=cursor;
            // Keep ordinary heaps, stacks and objects above the 24-bit DMA
            // window, growing upward. Growing them down from the top occupied
            // the game's fixed physical track buffers before it could claim them.
            if(!requested && highest==UINT32_MAX) cursor=align_up(std::max(cursor,dma_window),alignment);
            cursor=find_space(cursor);
            if(!cursor && !requested && highest==UINT32_MAX) cursor=find_space(first);
            if(!cursor) return fail();
        }
        if(static_cast<std::uint64_t>(cursor)+size>ram.size() || static_cast<std::uint64_t>(cursor)+size-1>highest) return fail();
        allocations.emplace(cursor,Allocation{size,protect,current?current->cpu.eip:0,
            std::vector<std::uint32_t>(size/page,committed?protect:0U)});
        if(committed && clear) std::memset(memory.access(cursor,size),0,size);
        return cursor;
    }
    auto allocation(std::uint32_t address) {
        address&=0x0FFFFFFF;
        auto found=allocations.upper_bound(address);
        if(found==allocations.begin()) return allocations.end();
        --found;
        return address-found->first<found->second.size?found:allocations.end();
    }
    void release_allocation(std::map<std::uint32_t,Allocation>::iterator found) {
        if(found==allocations.end()) return;
        for(unsigned i=0;i<watched_addresses.size();++i) if((watched_addresses[i]&0x0FFFFFFF)==found->first) {
            diagnostic_watch(i,nullptr);watched_addresses[i]=0;
        }
        if(gpu) gpu->invalidate_memory(found->first,found->second.size);
        allocations.erase(found);
    }
    std::uint32_t argument(const Cpu& cpu,unsigned index) { return memory.load<std::uint32_t>(cpu.registers[esp]+4+index*4); }
    void set_last_error(Cpu& cpu,std::uint32_t error) {
        const auto stack_base=memory.load<std::uint32_t>(cpu.fs_base+4);
        const auto index=memory.load<std::uint32_t>(image.tls->index_address);
        const auto tls=memory.load<std::uint32_t>(stack_base+index*4);
        memory.store<std::uint32_t>(tls+4,error);
    }
    void finish(Cpu& cpu,unsigned cleanup,std::optional<std::uint64_t> result=std::nullopt,bool wide=false) {
        cpu.eip=memory.load<std::uint32_t>(cpu.registers[esp]);
        cpu.registers[esp]+=4+cleanup;
        if(result) cpu.registers[eax]=static_cast<std::uint32_t>(*result);
        if(wide && result) cpu.registers[edx]=static_cast<std::uint32_t>(*result>>32);
    }
    void collect_threads() {
        if(!thread_cleanup_pending) return;
        thread_cleanup_pending=false;
        for(auto position=threads.begin();position!=threads.end();) {
            auto& thread=**position;
            // The current context also supplies the final diagnostic snapshot.
            if(thread.state!=ThreadState::ended) {++position;continue;}
            if(&thread==current) {thread_cleanup_pending=true;++position;continue;}
            if(thread.fiber) {DeleteFiber(thread.fiber);thread.fiber=nullptr;}
            // Pending I/O can still write into its guest stack after execution
            // ends. Handles retain the dispatcher object, not an unused stack.
            if(std::ranges::any_of(pending_io,[&](const auto& request){return request.active && request.thread==thread.id;})) {++position;continue;}
            if(thread.stack) {release_allocation(allocation(thread.stack));thread.stack=0;}
            const auto references=object_references.find(thread.object);
            if((references!=object_references.end() && references->second) ||
               std::ranges::any_of(handles,[&](const auto& value){return value.second.address==thread.object;})) {++position;continue;}
            release_allocation(allocation(thread.object));object_references.erase(thread.object);
            position=threads.erase(position);next_runnable=0;
        }
    }
    Thread& create(std::uint32_t routine,std::uint32_t context,std::uint32_t system,std::uint32_t stack_size,
                   std::uint32_t tls_size,bool suspended,bool entry=false) {
        collect_threads();
        if(std::ranges::count_if(threads,[](const auto& thread){return thread->state!=ThreadState::ended;})>=64)
            throw NativeBoundary(current?current->cpu.eip:image.entry,"native thread limit exceeded");
        auto thread=std::make_unique<Thread>();
        thread->host=this; thread->routine=routine; thread->context=context; thread->system=system;
        thread->id=next_thread; next_thread+=4;
        thread->suspended=suspended; thread->entry=entry;
        thread->stack_size=align_up(stack_size?stack_size:image.stack_commit,page);
        thread->stack=allocate(thread->stack_size,page,true,PAGE_READWRITE);
        const auto object_page=allocate(page,page,true,PAGE_READWRITE);
        if(!thread->stack || !object_page || tls_size+32>thread->stack_size) throw std::runtime_error("Cannot allocate a native guest thread");
        thread->object=object_page|0x80000000U; thread->tib=thread->object+0x400; thread->tls_size=tls_size;
        memory.store<std::uint8_t>(thread->object,6);
        // StackBase includes the TLS reservation. The supplied original system
        // routine fills that reservation and its self-reference (0xE682C).
        const auto stack_base=thread->stack+thread->stack_size;
        const auto tls=stack_base-tls_size;
        memory.store<std::uint32_t>(thread->object+0x1C,stack_base);
        memory.store<std::uint32_t>(thread->object+0x20,thread->stack);
        memory.store<std::uint32_t>(thread->object+0x28,tls_size?tls:0);
        memory.store<std::uint8_t>(thread->object+0x32,8); memory.store<std::uint8_t>(thread->object+0x70,8);
        memory.store<std::uint32_t>(thread->tib,UINT32_MAX);
        memory.store<std::uint32_t>(thread->tib+4,stack_base);
        memory.store<std::uint32_t>(thread->tib+8,thread->stack);
        memory.store<std::uint32_t>(thread->tib+0x18,thread->tib);
        memory.store<std::uint32_t>(thread->tib+0x20,thread->object);
        memory.store<std::uint32_t>(thread->tib+0x28,thread->object);
        auto& cpu=thread->cpu;
        cpu.kernel=&services; cpu.clock=&clock; cpu.diagnostic=live && graphics_initialized?nullptr:&diagnostic;
        cpu.skip_intro=host.skip_intro;cpu.fast_load=host.fast_load;cpu.clean_hud=host.clean_hud;cpu.draw_distance=float(host.draw_distance);
        cpu.ultrawide=host.ultrawide;cpu.motion_blur=host.motion_blur;
        cpu.game_timing=host.renderer?&game_timing:nullptr;
        cpu.presentation=host.renderer?&presentation:nullptr;
        cpu.achievements=host.achievements;
        if(!host.imported_tracks.empty()) cpu.world_materials=&imported.materials;
        if(host.draw_distance>1) cpu.world_view=&world_view;
        if(host.widescreen) cpu.hud_layout=&hud_layout;
        cpu.profile=host.performance && host.performance->active()?host.performance:nullptr;
        cpu.preempt=[](Cpu& cpu) {
            auto& host=*static_cast<State*>(cpu.kernel->context);
            if(host.host.performance) host.host.performance->guest_sample(cpu.eip,host.current?host.current->id:0);
            if(!(cpu.flags&0x200) || host.memory.load<std::uint8_t>(cpu.fs_base+0x24)>1 ||
               std::chrono::steady_clock::now()<host.quantum_due) return;
            HostFloatingScope floating(cpu.floating);
            host.yield();
        };
        cpu.fs_base=thread->tib; cpu.fs_selector=0x30;
        cpu.cs_selector=8; cpu.ds_selector=cpu.ss_selector=16; cpu.gdt_base=gdt; cpu.gdt_limit=63;
        cpu.registers[esp]=(tls-16)&~15U;
        if(system) {
            memory.store<std::uint32_t>(cpu.registers[esp]+4,routine);
            memory.store<std::uint32_t>(cpu.registers[esp]+8,context);
        } else if(!entry) memory.store<std::uint32_t>(cpu.registers[esp]+4,context);
        memory.store<std::uint32_t>(cpu.registers[esp],return_sentinel);
        thread->fiber=CreateFiberEx(0,8*1024*1024,FIBER_FLAG_FLOAT_SWITCH,fiber_entry,thread.get());
        if(!thread->fiber) throw std::runtime_error("Cannot create a native thread fiber");
        auto& result=*thread;
        threads.push_back(std::move(thread));
        return result;
    }
    static void WINAPI fiber_entry(void* parameter) {
        auto& thread=*static_cast<Thread*>(parameter);
        auto& host=*thread.host;
        thread.state=ThreadState::running;
        try {
            if(host.stopping) throw ThreadExit{0xC0000120};
            invoke_native(thread.cpu,host.memory,thread.system?thread.system:thread.routine);
            if(thread.cpu.eip!=return_sentinel) throw NativeBoundary(thread.cpu.eip,"thread returned outside its original native entry");
            if(thread.entry) host.entry_returned=true;
            thread.exit_status=thread.cpu.registers[eax];
        } catch(const ThreadExit& exit) { thread.exit_status=exit.status; }
        catch(const std::exception& error) { if(!host.stopping) host.boundary=error.what(); }
        thread.state=ThreadState::ended;host.thread_cleanup_pending=true;
        host.memory.store<std::uint32_t>(thread.object+4,1);
        SwitchToFiber(host.scheduler);
        std::terminate();
    }
    void yield(ThreadState state=ThreadState::ready,std::uint32_t object=0,
               std::chrono::steady_clock::time_point due=std::chrono::steady_clock::time_point::max()) {
        if(!scheduler && current!=isolated_owner) throw NativeBoundary(current?current->cpu.eip:0,"offline native call requires an initialized scheduler to wait");
        if(memory.load<std::uint8_t>(current->tib+0x24)>1) throw NativeBoundary(current->cpu.eip,"a DPC attempted a blocking wait");
        current->state=state; current->wait_object=object; current->due=due;
        if(object) current->wait_kind=WaitKind::critical;
        if(current==isolated_owner) pump_isolated(true);
        else {PerfScope idle(host.performance,PerfPhase::scheduler);SwitchToFiber(scheduler);}
        if(stopping) throw ThreadExit{0xC0000120};
        current->state=ThreadState::running;
        current->wait_kind=WaitKind::none;
    }
    void pump_isolated(bool waiting=false) {
        if(!isolated_owner || current!=isolated_owner)
            throw std::runtime_error("Isolated services require their owning diagnostic CPU");
        if(!scheduler) {
            if(IsThreadAFiber()) scheduler=GetCurrentFiber();
            else {scheduler=ConvertThreadToFiberEx(nullptr,FIBER_FLAG_FLOAT_SWITCH);converted=scheduler!=nullptr;}
            if(!scheduler) throw std::runtime_error("Cannot enter isolated native scheduler");
        }
        HostFloatingScope floating(isolated_owner->cpu.floating);
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
        unsigned slices=0;
        for(;;) {
            dispatch_io();dispatch_timers();dispatch_dpcs(*isolated_owner);
            const auto now=std::chrono::steady_clock::now();
            if(waiting && (isolated_owner->due<=now || waiting_ready(*isolated_owner))) break;
            Thread* selected=nullptr;
            for(std::size_t offset=0;offset<threads.size();++offset) {
                const auto index=(next_runnable+offset)%threads.size();auto& candidate=*threads[index];
                if(&candidate==isolated_owner || candidate.suspended || candidate.state==ThreadState::ended) continue;
                if(candidate.state==ThreadState::waiting) {
                    if(candidate.due>now && !waiting_ready(candidate)) continue;
                    candidate.state=ThreadState::ready;
                }
                selected=&candidate;next_runnable=(index+1)%threads.size();break;
            }
            if(now>=deadline) throw NativeBoundary(isolated_owner->cpu.eip,"isolated native service wait exceeded two seconds");
            if(!selected) {if(!waiting) break;Sleep(1);continue;}
            current=selected;clock_data();quantum_due=now+std::chrono::milliseconds(1);
            SwitchToFiber(selected->fiber);current=isolated_owner;
            if(!boundary.empty()) throw std::runtime_error(boundary);
            collect_threads();
            if(!waiting && ++slices>=threads.size()) break;
        }
    }
    void dispatcher(std::uint32_t address,unsigned type,unsigned size,std::uint32_t signal) {
        memory.access(address,size);
        for(unsigned i=0;i<watched_addresses.size();++i) if(watched_addresses[i] &&
            (watched_addresses[i]&0x0FFFFFFF)==(address&0x0FFFFFFF)) diagnostic_watch(i,nullptr);
        memory.fill_elements<std::uint32_t>(address,0,size/4,false);
        memory.store<std::uint8_t>(address,static_cast<std::uint8_t>(type));
        memory.store<std::uint8_t>(address+2,static_cast<std::uint8_t>(size/4));
        memory.store<std::uint32_t>(address+4,signal);
        memory.store<std::uint32_t>(address+8,address+8); memory.store<std::uint32_t>(address+12,address+8);
        for(unsigned i=0;i<watched_addresses.size();++i) if(watched_addresses[i] &&
            (watched_addresses[i]&0x0FFFFFFF)==(address&0x0FFFFFFF)) diagnostic_watch(i,memory.access(address,1));
    }
    std::chrono::steady_clock::time_point due_time(std::int64_t interval) {
        const auto now=std::chrono::steady_clock::now();
        std::uint64_t ticks;
        if(interval>0) {
            FILETIME time; GetSystemTimeAsFileTime(&time);
            const auto current_time=(static_cast<std::uint64_t>(time.dwHighDateTime)<<32)|time.dwLowDateTime;
            ticks=static_cast<std::uint64_t>(interval)>current_time?interval-current_time:0;
        } else ticks=0U-static_cast<std::uint64_t>(interval);
        const auto available=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::time_point::max()-now).count();
        return ticks>static_cast<std::uint64_t>(available)/100?std::chrono::steady_clock::time_point::max():
            now+std::chrono::nanoseconds(static_cast<std::int64_t>(ticks*100));
    }
    bool signalled(std::uint32_t address) {
        const auto type=memory.load<std::uint8_t>(address);
        if(type!=0 && type!=1 && type!=5 && type!=6 && type!=8 && type!=9) {
            // Collect only on failure. Identify the damaged object, its backing
            // allocation and nearby allocations, plus any outstanding I/O that
            // could still write into guest RAM. Normal waits perform no logging.
            const auto header=hex_bytes(Bytes(static_cast<const std::byte*>(memory.access(address,1)),
                memory.try_access(address,20)?20U:1U));
            std::ostringstream report;
            report<<std::format("{{\"type\":\"dispatcher_failure\",\"thread\":{},\"callsite\":{},\"object\":{},\"header\":{},\"handles\":[",
                current?current->id:0,json(hex32(current?current->cpu.eip:image.entry)),json(hex32(address)),
                json(header));
            unsigned count=0;
            for(const auto& [handle,object]:handles) if(object.address+(object.kind==Object::Kind::file?0x38:0)==address) {
                if(count++) report<<',';
                report<<std::format("{{\"handle\":{},\"kind\":{}}}",json(hex32(handle)),json(std::array{"thread","event","semaphore","file","link"}[static_cast<unsigned>(object.kind)]));
            }
            report<<"],\"allocations\":[";count=0;
            auto nearby=allocations.lower_bound(address&0x0FFFFFFF);
            if(nearby!=allocations.begin()) --nearby;
            for(;nearby!=allocations.end() && count<3;++nearby) {
                const auto& [base,block]=*nearby;
                if(count++) report<<',';
                report<<std::format("{{\"address\":{},\"size\":{},\"caller\":{}}}",json(hex32(base)),block.size,json(hex32(block.caller)));
            }
            report<<"],\"pending_io\":[";count=0;
            for(const auto& request:pending_io) if(request.active) {
                if(count++) report<<',';
                report<<std::format("{{\"ordinal\":{},\"callsite\":{},\"path\":{},\"buffer\":{},\"requested\":{},\"offset\":{},\"status\":{},\"event\":{},\"file_object\":{}}}",
                    request.ordinal,json(hex32(request.callsite)),json(std::string_view(request.file->path).substr(0,1024)),json(hex32(request.buffer)),request.requested,
                    request.offset,json(hex32(request.status)),json(hex32(request.event)),json(hex32(request.file_object)));
            }
            report<<"]}";diagnostic_record(report.view());
            throw NativeBoundary(current?current->cpu.eip:image.entry,std::format(
                "unsupported guest dispatcher object type {} at {} (header {})",type,hex32(address),
                header));
        }
        return static_cast<std::int32_t>(memory.load<std::uint32_t>(address+4))>0;
    }
    void consume(std::uint32_t address) {
        const auto type=memory.load<std::uint8_t>(address);
        if(type==1 || type==9) memory.store<std::uint32_t>(address+4,0);
        else if(type==5) memory.store<std::uint32_t>(address+4,memory.load<std::uint32_t>(address+4)-1);
    }
    bool waiting_ready(const Thread& thread) {
        if(thread.wait_kind==WaitKind::critical) return !memory.load<std::uint32_t>(thread.wait_object+24);
        if(thread.wait_kind!=WaitKind::objects) return false;
        bool all=true;
        for(unsigned i=0;i<thread.wait_count;++i) {
            const bool state=signalled(thread.wait_objects[i]);
            if(state && !thread.wait_all) return true;
            all &=state;
        }
        return all;
    }
    void release_object(std::uint32_t address) {
        // Thread dispatcher storage is reclaimed by the scheduler, after its
        // native fiber has yielded. Retry only when a retaining owner changes.
        if(!objects.contains(address)) {thread_cleanup_pending=true;return;}
        if(object_references[address]) return;
        if(std::ranges::any_of(handles,[&](const auto& pair) {return pair.second.address==address;})) return;
        release_allocation(allocations.find(address&0x0FFFFFFF));objects.erase(address);object_references.erase(address);
    }
    std::uint32_t dispatcher_owner(std::uint32_t address) const {
        address=(address&0x0FFFFFFFU)|0x80000000U;
        if(objects.contains(address)) return address;
        if(std::ranges::any_of(threads,[&](const auto& thread){return thread->object==address;})) return address;
        if(address>=0x38) if(const auto file=objects.find(address-0x38);file!=objects.end() && file->second.kind==Object::Kind::file)
            return file->first;
        return 0;
    }
    std::uint32_t wait(std::span<const std::uint32_t> addresses,bool all,std::uint32_t timeout_pointer,bool alertable) {
        if(addresses.empty() || addresses.size()>64) return invalid_parameter;
        if(all) for(unsigned i=0;i<addresses.size();++i) for(unsigned j=0;j<i;++j) if(addresses[i]==addresses[j]) return invalid_parameter;
        if(alertable) throw NativeBoundary(current->cpu.eip,"alertable waits require APC delivery");
        const auto due=timeout_pointer?due_time(std::bit_cast<std::int64_t>(memory.load<std::uint64_t>(timeout_pointer))):
            std::chrono::steady_clock::time_point::max();
        struct References {
            State& host;std::array<std::uint32_t,64> retained{};unsigned count=0;
            ~References() { for(auto address:std::span(retained).first(count)) {
                --host.object_references[address]; host.release_object(address);
            }}
        } references{*this};
        for(auto address:addresses) if(const auto owner=dispatcher_owner(address)) {
            ++object_references[owner];references.retained[references.count++]=owner;
        }
        for(;;) {
            bool complete=true; std::optional<unsigned> selected;
            for(unsigned i=0;i<addresses.size();++i) {
                const bool state=signalled(addresses[i]); complete &=state;
                if(state && !selected) selected=i;
            }
            if((all && complete) || (!all && selected)) {
                if(all) for(auto address:addresses) consume(address); else consume(addresses[*selected]);
                return all?0:*selected;
            }
            if(std::chrono::steady_clock::now()>=due) return timeout;
            std::copy(addresses.begin(),addresses.end(),current->wait_objects.begin());
            current->wait_count=static_cast<std::uint32_t>(addresses.size()); current->wait_all=all;
            current->wait_kind=WaitKind::objects; yield(ThreadState::waiting,0,due);
        }
    }
    bool queue_dpc(std::uint32_t address,std::uint32_t arg1,std::uint32_t arg2) {
        memory.access(address,28);
        if(memory.load<std::uint8_t>(address+2)) return false;
        if(dpcs.size()>=1024) throw NativeBoundary(current?current->cpu.eip:image.entry,"DPC queue limit exceeded");
        memory.store<std::uint32_t>(address+20,arg1); memory.store<std::uint32_t>(address+24,arg2);
        memory.store<std::uint8_t>(address+2,1); dpcs.push_back(address); return true;
    }
    void dispatch_timers() {
        const auto now=std::chrono::steady_clock::now();
        for(auto item=timers.begin();item!=timers.end();) {
            auto& timer=item->second; const auto address=item->first;
            if(timer.due>now) {++item;continue;}
            memory.store<std::uint32_t>(address+4,1);
            if(timer.dpc) queue_dpc(timer.dpc,0,0);
            if(timer.period) {timer.due=now+std::chrono::milliseconds(timer.period);++item;}
            else {memory.store<std::uint8_t>(address+3,0);item=timers.erase(item);}
        }
    }
    void dispatch_dpcs(Thread& thread) {
        current=&thread;
        while(!dpcs.empty()) {
            const auto address=dpcs.front(); dpcs.pop_front(); memory.store<std::uint8_t>(address+2,0);
            auto callback=thread.cpu; callback.floating.active=false; callback.floating.host_context=nullptr;
            callback.registers[esp]-=64;
            const auto stack=callback.registers[esp];
            memory.store<std::uint32_t>(stack,return_sentinel); memory.store<std::uint32_t>(stack+4,address);
            for(unsigned i=0;i<3;++i) memory.store<std::uint32_t>(stack+8+i*4,memory.load<std::uint32_t>(address+16+i*4));
            const auto prior=memory.load<std::uint8_t>(thread.tib+0x24); memory.store<std::uint8_t>(thread.tib+0x24,2);
            try {
                invoke_native(callback,memory,memory.load<std::uint32_t>(address+12));
                if(callback.eip!=return_sentinel) throw NativeBoundary(callback.eip,"DPC returned outside its native continuation");
            } catch(...) {memory.store<std::uint8_t>(thread.tib+0x24,prior);throw;}
            memory.store<std::uint8_t>(thread.tib+0x24,prior);
        }
    }
    void dispatch_video(std::chrono::steady_clock::time_point now) {
        if(!graphics_initialized) return;
        const auto ns=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(now-video_started).count());
        const auto ticks=(ns/1000000000)*60+(ns%1000000000)*60/1000000000;
        if(ticks==video_ticks) return;
        video_ticks=ticks;
        const auto routine=memory.load<std::uint32_t>(0x002256C0+0x1988);
        if(!routine) return;
        for(auto& thread:threads) {
            if(thread->state==ThreadState::ended || thread->suspended || !(thread->cpu.flags&0x200) ||
               memory.load<std::uint8_t>(thread->tib+0x24)>1) continue;
            current=thread.get();
            auto callback=thread->cpu;callback.preempt=nullptr;
            callback.floating.active=false;callback.floating.host_context=nullptr;
            callback.registers[esp]-=64;
            const auto stack=callback.registers[esp],data=stack+32;
            memory.store<std::uint32_t>(stack,return_sentinel);memory.store<std::uint32_t>(stack+4,data);
            memory.store<std::uint32_t>(data,static_cast<std::uint32_t>(ticks));
            memory.store<std::uint32_t>(data+4,static_cast<std::uint32_t>(gpu->stats().flips));
            memory.store<std::uint32_t>(data+8,0);
            const auto prior=memory.load<std::uint8_t>(thread->tib+0x24);memory.store<std::uint8_t>(thread->tib+0x24,2);
            try {
                invoke_native(callback,memory,routine);
                if(callback.eip!=return_sentinel || callback.registers[esp]!=stack+4)
                    throw NativeBoundary(callback.eip,"vertical-blank callback has an unexpected ABI");
            } catch(...) {memory.store<std::uint8_t>(thread->tib+0x24,prior);throw;}
            memory.store<std::uint8_t>(thread->tib+0x24,prior);
            ++vblank_callbacks;return;
        }
    }
    std::uint64_t clock_ticks(std::uint64_t frequency) const {
        const auto ns=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now()-started).count());
        return (ns/1000000000)*frequency+(ns%1000000000)*frequency/1000000000;
    }
    void clock_data() {
        const auto ms=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now()-started).count();
        memory.store<std::uint32_t>(tick_address,static_cast<std::uint32_t>(ms));
    }
    std::string counted_string(std::uint32_t address) {
        if(!address) return {};
        const auto length=memory.load<std::uint16_t>(address),maximum=memory.load<std::uint16_t>(address+2);
        if(length>maximum) throw NativeBoundary(current->cpu.eip,"invalid counted guest string");
        if(!length) return {};
        const auto data=memory.load<std::uint32_t>(address+4);
        std::string result(reinterpret_cast<const char*>(memory.access(data,length)),length);
        if(result.find('\0')!=std::string::npos) throw NativeBoundary(current->cpu.eip,"embedded zero in guest object name");
        return result;
    }
    std::optional<std::string> object_name(std::uint32_t attributes,bool resolve=true) {
        if(!attributes) return std::nullopt;
        auto result=counted_string(memory.load<std::uint32_t>(attributes+4));
        const auto root=memory.load<std::uint32_t>(attributes);
        // Xbox ObDosDevicesDirectory() is the namespace handle -3, also
        // written by the original CreateFileA wrapper at E0A95.
        if(!result.starts_with('\\') && root==0xFFFFFFFDU) result="\\??\\"+result;
        else if(!result.starts_with('\\') && root) {
            const auto found=handles.find(root);
            if(found==handles.end() || found->second.kind!=Object::Kind::file || !found->second.file->entry.directory()) return std::nullopt;
            result=found->second.file->path+'\\'+result;
        }
        if(!result.starts_with('\\') && result.size()>=2 && result[1]==':') result="\\??\\"+result;
        for(auto& ch:result) if(ch=='/') ch='\\';
        if(resolve) for(unsigned iteration=0;iteration<16;++iteration) {
            bool changed=false;
            const auto key=folded(result);
            for(const auto& [name,target]:links) if(key==name || (key.starts_with(name) && key.size()>name.size() && key[name.size()]=='\\')) {
                result=target+result.substr(name.size());changed=true;break;
            }
            if(!changed) return result;
        }
        else return result;
        throw NativeBoundary(current->cpu.eip,"cyclic guest symbolic links");
    }
    std::optional<std::string> disc_path(std::string_view name) {
        constexpr std::string_view root="\\DEVICE\\CDROM0";
        const auto key=folded(name);
        if(key==root) return "";
        if(!key.starts_with(root) || key.size()<=root.size() || key[root.size()]!='\\') return std::nullopt;
        auto relative=std::string(name.substr(root.size()+1));
        if(relative.ends_with('\\')) relative.pop_back();
        std::size_t start=0;
        while(start<relative.size()) {
            const auto end=relative.find('\\',start);
            const auto part=std::string_view(relative).substr(start,end==std::string::npos?relative.size()-start:end-start);
            if(part.empty() || part=="." || part==".." || part.find(':')!=std::string_view::npos) return std::nullopt;
            if(end==std::string::npos) break;
            start=end+1;
        }
        return relative;
    }
    static bool soundtrack_key(std::string_view key) {
        constexpr std::string_view music="\\DEVICE\\HARDDISK0\\PARTITION1\\TDATA\\FFFE0000\\MUSIC";
        return key==music || (key.starts_with(music) && key.size()>music.size() && key[music.size()]=='\\');
    }
    std::optional<std::filesystem::path> writable_path(std::string_view name) {
        constexpr std::string_view root="\\DEVICE\\HARDDISK0\\PARTITION";
        const auto key=folded(name);
        if(!key.starts_with(root) || key.size()<root.size()+1) return std::nullopt;
        const auto partition=key[root.size()];
        if(partition!='1' && (partition<'3' || partition>'5')) return std::nullopt;
        const auto prefix_size=root.size()+1;
        if(key.size()>prefix_size && key[prefix_size]!='\\') return std::nullopt;
        constexpr std::string_view music="\\DEVICE\\HARDDISK0\\PARTITION1\\TDATA\\FFFE0000\\MUSIC";
        const bool soundtrack=soundtrack_key(key);
        if(soundtrack && host.custom_music_root.empty()) return std::nullopt;
        auto path=soundtrack?host.custom_music_root:user_root;
        auto validate=[&](const std::filesystem::path& candidate) {
            const auto attributes=GetFileAttributesW(candidate.c_str());
            return attributes==INVALID_FILE_ATTRIBUTES || !(attributes&FILE_ATTRIBUTE_REPARSE_POINT);
        };
        if(!validate(path)) throw NativeBoundary(current->cpu.eip,"native storage root is a reparse point");
        if(partition!='1') {
            path/="CACHE";if(!validate(path)) throw NativeBoundary(current->cpu.eip,"native cache root is a reparse point");
            path/="PARTITION"+std::string(1,partition);if(!validate(path)) throw NativeBoundary(current->cpu.eip,"native cache partition is a reparse point");
        }
        std::size_t start=(soundtrack?music.size():prefix_size)+1;
        while(start<name.size()) {
            const auto end=name.find('\\',start);
            const auto part=name.substr(start,end==std::string_view::npos?name.size()-start:end-start);
            if(part.empty() || part=="." || part==".." || part.ends_with('.') || part.ends_with(' ') ||
               part.find_first_of(":*?\"<>|")!=std::string_view::npos) return std::nullopt;
            if(std::ranges::any_of(part,[](unsigned char ch) {return ch<32 || ch>126;}))
                throw NativeBoundary(current->cpu.eip,"non-ASCII writable filename requires an explicit guest encoding");
            const auto base=folded(part.substr(0,part.find('.')));
            if(base=="CON" || base=="PRN" || base=="AUX" || base=="NUL" ||
                (base.size()==4 && (base.starts_with("COM") || base.starts_with("LPT")) && base[3]>='1' && base[3]<='9')) return std::nullopt;
            auto candidate=path/std::string(part);
#ifndef _WIN32
            // FATX names are case-insensitive even on a case-sensitive host.
            // Exact names stay fast; only a miss needs a bounded directory walk.
            std::error_code error;
            if(!std::filesystem::exists(candidate,error) && !error && std::filesystem::is_directory(path,error) && !error) {
                std::optional<std::filesystem::path> match;unsigned entries=0;
                const auto wanted=folded(part);
                for(const auto& entry:std::filesystem::directory_iterator(path)) {
                    if(++entries>4096) throw NativeBoundary(current->cpu.eip,"native storage directory exceeds 4096 entries");
                    if(folded(path_text(entry.path().filename()))!=wanted) continue;
                    if(match) throw NativeBoundary(current->cpu.eip,"native storage contains ambiguous FATX filename casing");
                    match=entry.path();
                }
                if(match) candidate=*match;
            }
#endif
            path=std::move(candidate);
            if(!validate(path)) throw NativeBoundary(current->cpu.eip,"writable guest path crosses a reparse point");
            if(end==std::string_view::npos) break;
            start=end+1;
        }
        return path;
    }
    void update_input() {
        if(!input) {
            input=host.input;
            if(!input) {owned_input=std::make_unique<Input>();input=owned_input.get();}
        }
        input->update();
        constexpr std::uint32_t gamepad_type=0x0028C054;
        const auto mask=input->connected(),previous=memory.load<std::uint32_t>(gamepad_type);
        memory.store<std::uint32_t>(gamepad_type+4,memory.load<std::uint32_t>(gamepad_type+4)|(previous^mask));
        memory.store<std::uint32_t>(gamepad_type,mask);
    }
    void input_api(Cpu& cpu,std::uint32_t address) {
        constexpr std::uint32_t gamepad_type=0x0028C054,memory_unit_type=0x0028BFD8;
        update_input();
        const auto type=[&](std::uint32_t value) {
            if(value!=gamepad_type && value!=memory_unit_type) throw NativeBoundary(address,"unrecognized input device type");
            memory.access(value,12);return value;
        };
        const auto last_error=[&](std::uint32_t error) {
            set_last_error(cpu,error);
        };
        if(address==0x0028D27D) {
            const auto count=argument(cpu,0),array=argument(cpu,1);
            if(count>32) throw NativeBoundary(address,"input preallocation exceeds the native device bound");
            if(count) memory.access(array,count*8);
            for(unsigned i=0;i<count;++i) type(memory.load<std::uint32_t>(array+i*8));
            finish(cpu,8);return;
        }
        if(address==0x0028D282 || address==0x0028D2A4) {
            const auto device=type(argument(cpu,0)),current_mask=memory.load<std::uint32_t>(device);
            if(address==0x0028D282) {
                memory.store<std::uint32_t>(device+4,0);memory.store<std::uint32_t>(device+8,current_mask);
                finish(cpu,4,current_mask);
            } else {
                const auto additions=argument(cpu,1),removals=argument(cpu,2);
                memory.access(additions,4);memory.access(removals,4);
                const auto changed=memory.load<std::uint32_t>(device+4),previous=memory.load<std::uint32_t>(device+8);
                const auto reconnected=changed&previous&current_mask;
                const auto added=(~previous&current_mask)|reconnected,removed=(~current_mask&previous)|reconnected;
                memory.store<std::uint32_t>(additions,added);memory.store<std::uint32_t>(removals,removed);
                memory.store<std::uint32_t>(device+4,0);memory.store<std::uint32_t>(device+8,current_mask);
                finish(cpu,12,(added|removed)!=0);
            }
            return;
        }
        if(address==0x0028CF40) {
            const auto device=type(argument(cpu,0)),port=argument(cpu,1),slot=argument(cpu,2),parameters=argument(cpu,3);
            if(parameters) {
                memory.access(parameters,4);
                if((memory.load<std::uint8_t>(parameters)&0xFC) || memory.load<std::uint8_t>(parameters+3)) {
                    last_error(87);finish(cpu,16,0);return;
                }
                if(!(memory.load<std::uint8_t>(parameters)&1)) throw NativeBoundary(address,"manual input polling is not yet bound");
            }
            if(device!=gamepad_type || port>=4 || slot) {last_error(87);finish(cpu,16,0);return;}
            if(!(input->connected()&(1U<<port))) {last_error(1167);finish(cpu,16,0);return;}
            const auto available=std::ranges::find(input_handles,0U,&InputHandle::id);
            if(available==input_handles.end() || next_input_handle>=0xF2FFFFFF) {last_error(8);finish(cpu,16,0);return;}
            *available={++next_input_handle,port};finish(cpu,16,available->id);return;
        }
        const auto handle=argument(cpu,0);
        const auto found=std::ranges::find(input_handles,handle,&InputHandle::id);
        const bool valid=handle && found!=input_handles.end();
        if(address==0x0028CF96) {
            if(!valid) throw NativeBoundary(address,"invalid native input handle");
            *found={};finish(cpu,4);return;
        }
        const auto output=argument(cpu,1);
        if(address==0x0028CFA2) {
            memory.access(output,25);memory.fill_elements<std::uint8_t>(output,0,25,false);
            if(!valid || !(input->connected()&(1U<<found->port))) {finish(cpu,8,1167);return;}
            memory.store<std::uint8_t>(output,2); // Controller-S ABI.
            memory.store<std::uint16_t>(output+3,0x00FF);
            memory.fill_elements<std::uint8_t>(output+5,255,16,false);
            if(input->has_rumble(found->port)) memory.fill_elements<std::uint8_t>(output+21,255,4,false);
            finish(cpu,8,0);return;
        }
        if(address==0x0028D180) {
            memory.access(output,22);
            if(!valid || !(input->connected()&(1U<<found->port))) {finish(cpu,8,1167);return;}
            const auto& state=input->state(found->port);
            memory.store<std::uint32_t>(output,state.packet);memory.store<std::uint16_t>(output+4,state.buttons);
            for(unsigned i=0;i<8;++i) memory.store<std::uint8_t>(output+6+i,state.analog[i]);
            for(unsigned i=0;i<4;++i) memory.store<std::uint16_t>(output+14+i*2,static_cast<std::uint16_t>(state.axes[i]));
            finish(cpu,8,0);return;
        }
        if(address==0x0028D1EC) {
            memory.access(output,70);
            const auto event_handle=memory.load<std::uint32_t>(output+4);
            if(event_handle && (!handles.contains(event_handle) || handles.at(event_handle).kind!=Object::Kind::event)) {
                memory.store<std::uint32_t>(output,6);finish(cpu,8,6);return;
            }
            const auto status=valid?input->rumble(found->port,memory.load<std::uint16_t>(output+66),memory.load<std::uint16_t>(output+68)):1167U;
            memory.store<std::uint8_t>(output+64,0);memory.store<std::uint8_t>(output+65,6);
            memory.store<std::uint32_t>(output,status);
            if(event_handle) memory.store<std::uint32_t>(handles.at(event_handle).address+4,1);
            finish(cpu,8,status);return;
        }
        throw NativeBoundary(address,"native input API is unbound");
    }
    AudioFormat audio_format(std::uint32_t address) {
        if(!address) throw std::runtime_error("Audio format pointer is null");
        AudioFormat result;
        result.tag=memory.load<std::uint16_t>(address);result.channels=memory.load<std::uint16_t>(address+2);
        result.rate=memory.load<std::uint32_t>(address+4);result.block_align=memory.load<std::uint16_t>(address+12);
        result.bits=memory.load<std::uint16_t>(address+14);
        if(result.tag==0x69) result.samples_per_block=memory.load<std::uint16_t>(address+18);
        return result;
    }
    void audio_mixbins(std::uint32_t identifier,std::uint32_t address,bool volumes_only=false) {
        if(!address) {
            if(volumes_only) throw std::runtime_error("Audio mix-bin volume pointer is null");
            constexpr std::array<MixBin,2> defaults{{{0,0},{1,0}}};audio->mixbins(identifier,defaults);return;
        }
        const auto count=memory.load<std::uint32_t>(address),pairs=memory.load<std::uint32_t>(address+4);
        if(count>(volumes_only?32U:8U)) throw std::runtime_error("Audio mix-bin count exceeds its native capacity");
        std::array<MixBin,32> bins{};
        for(unsigned i=0;i<count;++i) bins[i]={memory.load<std::uint32_t>(pairs+i*8),std::bit_cast<std::int32_t>(memory.load<std::uint32_t>(pairs+i*8+4))};
        audio->mixbins(identifier,std::span(bins).first(count),volumes_only);
    }
    SpatialResult spatial_result(const AudioBuffer& buffer) {
        if(!audio_spatial) audio_spatial=std::make_unique<Spatial>(memory);
        const auto curve=buffer.curve_count?std::span(static_cast<const std::byte*>(memory.access(buffer.curve,buffer.curve_count*4)),buffer.curve_count*4):std::span<const std::byte>{};
        return audio_spatial->calculate(audio_listener,buffer.spatial,curve,&buffer.environment,(buffer.flags&0x20000)!=0);
    }
    void update_spatial(AudioBuffer& buffer) {
        if(buffer.has_spatial) audio->spatial(buffer.identifier,spatial_result(buffer));
    }
    void update_spatial(std::span<AudioBuffer*> buffers) {
        std::array<std::uint32_t,256> ids;std::array<SpatialResult,256> values;
        unsigned count=0;
        for(const auto* buffer:buffers) if(buffer->has_spatial) {
            if(count==ids.size()) throw std::runtime_error("Spatial audio update exceeds voice capacity");
            ids[count]=buffer->identifier;values[count++]=spatial_result(*buffer);
        }
        audio->spatial(std::span(ids).first(count),std::span(values).first(count));
    }
    static void copy_listener(SpatialListener& destination,const SpatialListener& source,unsigned fields) {
        if(fields&1) destination.position=source.position;
        if(fields&2) destination.velocity=source.velocity;
        if(fields&4) {destination.front=source.front;destination.up=source.up;}
        if(fields&8) destination.distance=source.distance;
        if(fields&16) destination.rolloff=source.rolloff;
        if(fields&32) destination.doppler=source.doppler;
    }
    bool listener_needs_update(const SpatialListener& previous) const {
        return std::memcmp(&previous,&audio_listener,sizeof(audio_listener)) ||
            std::ranges::any_of(audio_buffers,[](const auto& entry){return entry.second.has_spatial && entry.second.curve_count;});
    }
    std::uint32_t effect_offset(std::uint32_t index) const {
        return memory.load<std::uint32_t>(audio_effect_descriptor+8+index*32+8)-0xFE830000U;
    }
    void update_headphones() {
        if(audio_crosstalk==UINT32_MAX) return;
        std::uint32_t flags=0;const auto offset=effect_offset(audio_crosstalk)+0x10;
        audio->effect_data(offset,std::as_writable_bytes(std::span(&flags,1)));
        flags=audio_headphones?flags&~2U:flags|2U;
        audio->effect_data(offset,std::as_bytes(std::span(&flags,1)),false);
    }
    void update_environment() {
        if(!audio_spatial) audio_spatial=std::make_unique<Spatial>(memory);
        audio_spatial->environment(audio_environment);
        if(audio_reverb==UINT32_MAX) return;
        std::array<std::byte,0x118> state;const auto offset=effect_offset(audio_reverb);
        audio->effect_data(offset,state);const auto parameters=audio_spatial->reverb(state);
        audio->effect_data(offset+0x10,std::span(parameters).subspan(0x10,4),true);
        audio->effect_data(offset+0x118,std::span(parameters).subspan(0x118),true);audio->commit_effects();
    }
    void commit_spatial() {
        const auto previous_listener=audio_listener;
        const bool listener_pending=audio_pending_listener!=0;
        const bool environment_changed=audio_pending_environment;
        if(environment_changed) {audio_environment=audio_deferred_environment;audio_pending_environment=false;update_environment();}
        copy_listener(audio_listener,audio_deferred_listener,audio_pending_listener);
        const bool listener_changed=listener_pending && listener_needs_update(previous_listener);
        audio_pending_listener=0;
        std::array<AudioBuffer*,256> changed_buffers;unsigned count=0;
        for(auto& [object,buffer]:audio_buffers) {
            const bool changed=listener_changed || environment_changed || buffer.pending_spatial || buffer.pending_curve || buffer.pending_environment;
            if(buffer.pending_spatial) {buffer.spatial=buffer.deferred_spatial;buffer.has_spatial=true;buffer.pending_spatial=false;}
            if(buffer.pending_curve) {buffer.curve=buffer.deferred_curve;buffer.curve_count=buffer.deferred_curve_count;buffer.pending_curve=false;}
            if(buffer.pending_environment) {buffer.environment=buffer.deferred_environment;buffer.pending_environment=false;}
            if(changed) changed_buffers[count++]=&buffer;
        }
        update_spatial(std::span(changed_buffers).first(count));
    }
    void publish_audio_packets(Cpu& cpu,std::uint32_t object) {
        const auto found=audio_streams.find(object);if(found==audio_streams.end()) return;
        std::array<AudioCompletion,256> completed{};
        const auto count=audio->completed(found->second.identifier,completed);
        const auto routine=found->second.callback,context=found->second.context;
        std::array<std::array<std::uint32_t,6>,256> packets{};
        for(unsigned i=0;i<count;++i) {
            auto& attached=found->second.packets.at(completed[i].token-1);packets[i]=attached;attached={};
            ++found->second.completed;
        }
        for(unsigned i=0;i<count;++i) {
            const auto& packet=packets[i];const auto result=completed[i];
            if(packet[2]) memory.store<std::uint32_t>(packet[2],result.bytes);
            if(packet[3]) memory.store<std::uint32_t>(packet[3],result.status);
            if(routine) {
                // Original 2317E9 uses this stdcall ABI. Guest callbacks run
                // here on the game thread, never inside SDL's audio callback.
                auto callback=cpu;callback.floating.active=false;callback.floating.host_context=nullptr;
                callback.preempt=nullptr;callback.registers[esp]-=64;const auto stack=callback.registers[esp];
                memory.store<std::uint32_t>(stack,return_sentinel);memory.store<std::uint32_t>(stack+4,context);
                memory.store<std::uint32_t>(stack+8,packet[4]);memory.store<std::uint32_t>(stack+12,result.status);
                invoke_native(callback,memory,routine);
                if(callback.eip!=return_sentinel || callback.registers[esp]!=stack+16)
                    throw NativeBoundary(routine,"audio packet callback returned outside its native continuation");
            } else if(packet[4]) {
                const auto event=handles.find(packet[4]);
                if(event==handles.end() || event->second.kind!=Object::Kind::event)
                    throw NativeBoundary(0x002317E9,"audio packet completion event is invalid");
                memory.store<std::uint32_t>(event->second.address+4,1);
            }
        }
    }
    std::uint32_t release_audio_buffer(std::uint32_t object) {
        const auto found=audio_buffers.find(object);auto& buffer=found->second;
        // Original AddRef (22C1E4) increments this same SDK field.
        const auto references=memory.load<std::uint32_t>(buffer.raw+4);
        if(!references) return 0;
        memory.store<std::uint32_t>(buffer.raw+4,references-1);
        if(references==1) {
            const auto output=memory.load<std::uint32_t>(buffer.descriptor+0xB0);
            audio->release(buffer.identifier);release_allocation(allocations.find(buffer.raw));audio_buffers.erase(found);
            if(output) release_audio_buffer(output+28);
        }
        return references-1;
    }
    void audio_output(std::uint32_t identifier,std::uint32_t descriptor,std::uint32_t destination,std::uint32_t address) {
        const auto target=audio_buffers.find(destination);
        if(destination && target==audio_buffers.end()) throw NativeBoundary(address,"invalid audio output buffer");
        const auto output=destination?target->second.raw:0,previous=memory.load<std::uint32_t>(descriptor+0xB0);
        if(output==previous) return;
        audio->route(identifier,destination?target->second.identifier:0);
        // Original 22DADD/231D69 retain the output at descriptor+B0 until
        // the source is released or routed elsewhere. Streams share this path.
        if(output) memory.store<std::uint32_t>(output+4,memory.load<std::uint32_t>(output+4)+1);
        memory.store<std::uint32_t>(descriptor+0xB0,output);
        if(previous) release_audio_buffer(previous+28);
    }
    bool audio_stream_api(Cpu& cpu,std::uint32_t address) {
        if(address==0x0022F90E) {
            if(argument(cpu,0)!=audio_device || argument(cpu,3)) throw NativeBoundary(address,"invalid sound-stream device or aggregation");
            const auto descriptor=argument(cpu,1),output=argument(cpu,2);
            memory.access(descriptor,24);memory.store<std::uint32_t>(output,0);
            const auto flags=memory.load<std::uint32_t>(descriptor),capacity=memory.load<std::uint32_t>(descriptor+4);
            if(flags&0x10) throw NativeBoundary(address,"3D packet audio streams are unsupported");
            const auto format=audio_format(memory.load<std::uint32_t>(descriptor+8));
            if(!capacity || capacity>256) {finish(cpu,16,0x88780032U);return true;}
            const auto identifier=audio->create(format);audio->packets(identifier,capacity);audio->headroom(identifier,600);
            if(const auto bins=memory.load<std::uint32_t>(descriptor+20)) audio_mixbins(identifier,bins);
            const auto raw=allocate(page,page,true,PAGE_READWRITE);
            if(!raw) {audio->release(identifier);finish(cpu,16,0x8007000EU);return true;}
            const auto body=raw+64,voice=raw+320;
            memory.store<std::uint32_t>(raw,0x002C948C);memory.store<std::uint32_t>(raw+4,0x002C9480);
            memory.store<std::uint32_t>(raw+8,1);memory.store<std::uint32_t>(raw+0x10,voice);
            memory.store<std::uint32_t>(raw+0x14,body);memory.store<std::uint32_t>(raw+0x20,body);memory.store<std::uint32_t>(raw+0x24,voice);
            memory.store<std::uint32_t>(body,0x002C94B4);memory.store<std::uint32_t>(body+8,flags);
            memory.store<std::uint32_t>(body+0x10,format.rate);memory.store<std::uint32_t>(body+0x14,format.block_align);
            memory.store<std::uint32_t>(body+0xB8,capacity);
            auto& stream=audio_streams[raw];stream.raw=raw;stream.descriptor=body;stream.identifier=identifier;stream.format=format;
            stream.callback=memory.load<std::uint32_t>(descriptor+12);stream.context=memory.load<std::uint32_t>(descriptor+16);
            stream.packets.resize(capacity);memory.store<std::uint32_t>(output,raw);finish(cpu,16,success);return true;
        }
        auto object=argument(cpu,0);
        if(address==0x0022D3F0) object-=4;
        const auto found=audio_streams.find(object);if(found==audio_streams.end()) return false;
        auto& stream=found->second;
        switch(address) {
        case 0x0022CBC4: {
            const auto references=memory.load<std::uint32_t>(stream.raw+8)+1;
            memory.store<std::uint32_t>(stream.raw+8,references);finish(cpu,4,references);return true;
        }
        case 0x0022CC0B: {
            const auto references=memory.load<std::uint32_t>(stream.raw+8)-1;
            memory.store<std::uint32_t>(stream.raw+8,references);
            if(!references) {
                const auto identifier=stream.identifier,raw=stream.raw,output=memory.load<std::uint32_t>(stream.descriptor+0xB0);
                audio->flush(identifier);publish_audio_packets(cpu,object);
                audio->release(identifier);release_allocation(allocations.find(raw));audio_streams.erase(object);
                if(output) release_audio_buffer(output+28);
            }
            finish(cpu,4,references);return true;
        }
        case 0x0022CC59: {
            const auto output=argument(cpu,1),lookahead=2*std::max<std::uint32_t>(stream.format.block_align,32*stream.format.channels*stream.format.bits/8);
            memory.store<std::uint32_t>(output,5);memory.store<std::uint32_t>(output+4,stream.format.block_align);
            memory.store<std::uint32_t>(output+8,0);memory.store<std::uint32_t>(output+12,lookahead);
            finish(cpu,8,success);return true;
        }
        case 0x0022CD58: {
            memory.store<std::uint32_t>(argument(cpu,1),audio->packet_status(stream.identifier));finish(cpu,8,success);return true;
        }
        case 0x0022CDA9: {
            if(argument(cpu,2)) throw NativeBoundary(address,"sound streams accept input packets only");
            const auto input=argument(cpu,1);std::array<std::uint32_t,6> packet{};
            std::memcpy(packet.data(),memory.access(input,sizeof(packet)),sizeof(packet));
            const auto slot=std::ranges::find_if(stream.packets,[](const auto& attached){return !attached[0];});
            if(slot==stream.packets.end()) {finish(cpu,12,0x88780032U);return true;}
            const auto data=std::span(static_cast<const std::byte*>(memory.access(packet[0],packet[1])),packet[1]);
            if(!audio->packet(stream.identifier,data,static_cast<std::uint32_t>(slot-stream.packets.begin()+1))) {finish(cpu,12,0x88780032U);return true;}
            *slot=packet;++stream.submitted;
            if(packet[2]) memory.store<std::uint32_t>(packet[2],0);
            if(packet[3]) memory.store<std::uint32_t>(packet[3],0x8000000A);
            finish(cpu,12,success);return true;
        }
        case 0x0022CCC0:audio->discontinuity(stream.identifier);finish(cpu,4,success);return true;
        case 0x0022CD0D:case 0x0022CE71: {
            const auto flags=address==0x0022CE71?argument(cpu,3):0U;
            if(flags && (argument(cpu,1) || argument(cpu,2)))
                throw NativeBoundary(address,"scheduled stream flush is unsupported");
            if(flags&~7U) throw NativeBoundary(address,"unsupported stream flush flags");
            audio->flush(stream.identifier,flags);
            // 22CE71 uses the asynchronous SDK path for nonzero flags.
            // Packet callbacks and events are then published by DoWork.
            if(!flags) publish_audio_packets(cpu,object);
            finish(cpu,address==0x0022CD0D?4:16,success);return true;
        }
        case 0x0022CE20: {
            const auto mode=argument(cpu,1);
            if(mode>3 || mode==2) throw NativeBoundary(address,"synchronized stream pause is unsupported");
            audio->pause(stream.identifier,mode);finish(cpu,8,success);return true;
        }
        case 0x0022D48C:audio->pitch(stream.identifier,std::bit_cast<std::int32_t>(argument(cpu,1)));finish(cpu,8,success);return true;
        case 0x0022D4DE:audio->volume(stream.identifier,std::bit_cast<std::int32_t>(argument(cpu,1)));finish(cpu,8,success);return true;
        case 0x0022D626:audio->headroom(stream.identifier,argument(cpu,1));finish(cpu,8,success);return true;
        case 0x0022D3F0:audio_mixbins(stream.identifier,argument(cpu,1),true);finish(cpu,8,success);return true;
        case 0x0022E6E6:audio_mixbins(stream.identifier,argument(cpu,1));finish(cpu,8,success);return true;
        case 0x0022E694: {
            audio_output(stream.identifier,stream.descriptor,argument(cpu,1),address);finish(cpu,8,success);return true;
        }
        case 0x0022EF6B: {
            const auto format=audio_format(argument(cpu,1));audio->format(stream.identifier,format);stream.format=format;
            memory.store<std::uint32_t>(stream.descriptor+0x10,format.rate);memory.store<std::uint32_t>(stream.descriptor+0x14,format.block_align);
            finish(cpu,8,success);return true;
        }
        default:throw NativeBoundary(address,"unsupported native sound-stream method");
        }
    }
    void audio_api(Cpu& cpu,std::uint32_t address) {
        if(audio_trace && audio) {
            const auto sample_clock=audio->sample_clock();
            if(std::bit_cast<std::int32_t>(sample_clock-audio_trace_until)>=0) audio_trace=false;
            else {
                unsigned count=0;
                switch(address) {
                case 0x0022D734:count=6;break;
                case 0x0022D75E:count=1;break;
                case 0x0022D896:case 0x0022D8D2:case 0x0022F8EA:case 0x0022F90E:count=4;break;
                case 0x0022EF4B:case 0x0022E908:case 0x0022D8F6:count=3;break;
                case 0x0022EF2F:case 0x0022E754:case 0x0022E770:case 0x0022D952:
                case 0x0022D7D2:case 0x0022E738:case 0x0022D4DE:case 0x0022D48C:case 0x0022CE20:count=2;break;
                case 0x0022CDA9:count=3;break;
                case 0x0022FA52:count=3;break;
                case 0x0022D776:count=2;break;
                case 0x0022D8BA:case 0x0022C11B:count=1;break;
                }
                if(count) {
                    auto& event=audio_events[audio_event_count++%audio_events.size()];event={};
                    event.clock=sample_clock;event.address=address;event.caller=memory.load<std::uint32_t>(cpu.registers[esp]);event.count=count;
                    for(unsigned i=0;i<count;++i) event.arguments[i]=argument(cpu,i);
                    if(address==0x0022D734) {
                        event.effect_bytes=std::min<std::uint32_t>(event.arguments[4],static_cast<std::uint32_t>(event.effect_data.size()));
                        if(event.effect_bytes) std::memcpy(event.effect_data.data(),memory.access(event.arguments[3],event.effect_bytes),event.effect_bytes);
                    }
                    if(address==0x0022FA52) {
                        event.effect_bytes=sizeof(EnvironmentListener);
                        std::memcpy(event.effect_data.data(),memory.access(event.arguments[1],event.effect_bytes),event.effect_bytes);
                    }
                    const auto found=audio_buffers.find(event.arguments[0]);
                    if(found!=audio_buffers.end()) {
                        const auto& buffer=found->second;event.identifier=buffer.identifier;
                        event.source=memory.load<std::uint32_t>(buffer.descriptor+0xB8);
                        event.start=memory.load<std::uint32_t>(buffer.descriptor+0xC0);
                        event.bytes=memory.load<std::uint32_t>(buffer.descriptor+0xC4);
                    } else if(const auto stream=audio_streams.find(event.arguments[0]);stream!=audio_streams.end()) event.identifier=stream->second.identifier;
                }
            }
        }
        if(address==0x0022D9CA) {
            // Native mixing completes buffers on SDL's audio thread. Publish
            // that state here; the original MCPX notification lists are not
            // initialized or used by the native audio provider.
            if(audio) {
                if(const auto error=audio->failure();!error.empty()) throw NativeBoundary(address,error);
                for(const auto& [object,buffer]:audio_buffers) {
                    const auto status=audio->status(buffer.identifier);
                    memory.store<std::uint16_t>(buffer.voice+18,static_cast<std::uint16_t>((status&1)?3:1));
                }
                for(auto item=audio_streams.begin();item!=audio_streams.end();) {
                    const auto object=item->first;publish_audio_packets(cpu,object);item=audio_streams.upper_bound(object);
                }
            }
            finish(cpu,0);return;
        }
        if(address==0x0022C131) {audio_full_hrtf=true;finish(cpu,0);return;}
        if(address==0x0022FC18) {
            if(argument(cpu,0) || argument(cpu,2)) throw NativeBoundary(address,"non-default audio device or aggregation is unsupported");
            if(!audio) {
                audio=std::make_unique<Audio>(host.audio_output,output_gain);
                if(host.performance && host.performance->active()) audio->performance(true);
                // RenderWare 211634 reads the real 48 kHz processing counter.
                // Preserve its original wraparound/time conversion, with native
                // output progress rather than a fabricated hardware timestamp.
                memory.map_io(0xFE80200C,4,audio.get(),
                    [](void* state,std::uint32_t offset,unsigned bytes) {
                        if(offset || bytes!=4) throw std::runtime_error("Invalid native audio sample-clock read");
                        return static_cast<Audio*>(state)->sample_clock();
                    },
                    [](void*,std::uint32_t,unsigned,std::uint32_t){throw std::runtime_error("The native audio sample clock is read-only");});
            }
            if(!audio_device) {
                const auto raw=allocate(44,page,true,PAGE_READWRITE);
                if(!raw) {finish(cpu,12,0x8007000EU);return;}
                memory.store<std::uint32_t>(raw,0x002C9450);memory.store<std::uint32_t>(raw+4,2);
                memory.store<std::uint32_t>(raw+16,raw+16);memory.store<std::uint32_t>(raw+20,raw+16);
                memory.store<std::uint32_t>(0x0024ACC0,raw);audio_device=raw+8;
            }
            ++audio_references;memory.store<std::uint32_t>(argument(cpu,1),audio_device);finish(cpu,12,success);return;
        }
        if(!audio) throw NativeBoundary(address,"native audio device has not been created");
        if(audio_stream_api(cpu,address)) return;
        const auto object=argument(cpu,0)+(address==0x0022D3F0?28:0);
        if(address==0x0022D792) {
            if(object!=audio_device) throw NativeBoundary(address,"invalid native sound device");
            audio->mixbin_headroom(argument(cpu,1),argument(cpu,2));finish(cpu,12,success);return;
        }
        if(address==0x0022D776) {
            if(object!=audio_device) throw NativeBoundary(address,"invalid native sound device");
            const bool enabled=argument(cpu,1)!=0;
            if(enabled!=audio_headphones) {audio_headphones=enabled;update_headphones();}
            finish(cpu,8,success);return;
        }
        if(address==0x0022FA52) {
            if(object!=audio_device) throw NativeBoundary(address,"invalid native sound device");
            if(audio_reverb==UINT32_MAX) {finish(cpu,12,0x88780032U);return;}
            const auto deferred=argument(cpu,2);
            if(deferred>1) throw NativeBoundary(address,"invalid environmental listener apply mode");
            EnvironmentListener parameters;std::memcpy(parameters.data(),memory.access(argument(cpu,1),sizeof(parameters)),sizeof(parameters));
            Spatial::validate(parameters);audio_deferred_environment=parameters;audio_pending_environment=deferred!=0;
            if(!deferred) {
                audio_environment=parameters;update_environment();
                std::array<AudioBuffer*,256> buffers;unsigned count=0;
                for(auto& [buffer_object,buffer]:audio_buffers) buffers[count++]=&buffer;
                update_spatial(std::span(buffers).first(count));
            }
            finish(cpu,12,success);return;
        }
        if(address==0x0022D70D || address==0x0022D734 || address==0x0022D75E) {
            if(object!=audio_device) throw NativeBoundary(address,"invalid native sound device");
            if(address==0x0022D75E) {audio->commit_effects();finish(cpu,4,success);return;}
            const auto effect=argument(cpu,1),offset=argument(cpu,2),pointer=argument(cpu,3),size=argument(cpu,4);
            const bool write=address==0x0022D734;const unsigned stack_bytes=write?24:20;
            if(effect>=audio_effect_count) {finish(cpu,stack_bytes,0x88780032U);return;}
            const auto descriptor=audio_effect_descriptor+8+effect*32;
            const auto state=memory.load<std::uint32_t>(descriptor+8),capacity=memory.load<std::uint32_t>(descriptor+12);
            if(offset>capacity || size>capacity-offset) {finish(cpu,stack_bytes,0x88780032U);return;}
            // Public effect operations use the provider's DSP state. The original
            // private MCPX path requires hardware objects this provider does not own.
            const auto native_offset=state-0xFE830000U+offset;
            if(write) audio->effect_data(native_offset,
                std::span(static_cast<const std::byte*>(memory.access(pointer,size)),size),(argument(cpu,5)&1)!=0);
            else audio->effect_data(native_offset,std::span(static_cast<std::byte*>(memory.access(pointer,size)),size));
            finish(cpu,stack_bytes,success);return;
        }
        if(address==0x0022D6E6) {
            if(object!=audio_device) throw NativeBoundary(address,"invalid native sound device");
            const auto size=argument(cpu,2),locations=argument(cpu,3),output=argument(cpu,4);
            if(size>65536) throw NativeBoundary(address,"audio effect image exceeds its bounded format");
            const auto effects=decode_effects(std::span(static_cast<const std::byte*>(memory.access(argument(cpu,1),size)),size));
            std::uint64_t scratch_size=0xC000;
            for(const auto& effect:effects.effects) scratch_size+=effect.descriptor[7];
            if(scratch_size>16*1024*1024) throw NativeBoundary(address,"audio effect scratch exceeds its native capacity");
            const auto scratch_bytes=static_cast<std::uint32_t>(scratch_size);
            if(audio_effect_descriptor && (audio_effect_count!=effects.effects.size() ||
                audio_effect_program_words!=effects.program_words || audio_effect_scratch_bytes!=scratch_bytes))
                throw NativeBoundary(address,"effect replacement differs from the fixed native memory layout");
            if(locations) for(unsigned i=0;i<2;++i) {
                const auto effect=memory.load<std::uint32_t>(locations+i*4);
                if(effect!=UINT32_MAX && effect>=effects.effects.size()) throw NativeBoundary(address,"invalid audio image location");
            }
            const auto descriptor=audio_effect_descriptor?audio_effect_descriptor:
                allocate(8+static_cast<std::uint32_t>(effects.effects.size())*32,page,true,PAGE_READWRITE);
            if(!descriptor){finish(cpu,20,0x8007000EU);return;}
            audio->effects(effects,locations?memory.load<std::uint32_t>(locations):UINT32_MAX);
            constexpr std::uint32_t x_base=0xFE830000,program_base=0xFE83A000,scratch_base=0xFE900000;
            if(!audio_effect_descriptor) {
            // CPU and SDL access the same native effect state through these
            // scalar IO handlers. Stream locking prevents concurrent state writes.
            memory.map_io(x_base,0x6000,audio.get(),
                [](void* state,std::uint32_t offset,unsigned bytes){return static_cast<Audio*>(state)->effect_x(offset,bytes);},
                [](void* state,std::uint32_t offset,unsigned bytes,std::uint32_t value){static_cast<Audio*>(state)->effect_x(offset,bytes,value);});
            memory.map_io(program_base,effects.program_words*4,audio.get(),
                [](void* state,std::uint32_t offset,unsigned bytes){return static_cast<Audio*>(state)->effect_program(offset,bytes);},
                [](void*,std::uint32_t,unsigned,std::uint32_t){throw std::runtime_error("Writing compiled DSP code requires an ahead-of-time rebuild");});
            memory.map_io(scratch_base,scratch_bytes,audio.get(),
                [](void* state,std::uint32_t offset,unsigned bytes){return static_cast<Audio*>(state)->effect_scratch(offset,bytes);},
                [](void* state,std::uint32_t offset,unsigned bytes,std::uint32_t value){static_cast<Audio*>(state)->effect_scratch(offset,bytes,value);});
            }
            memory.store<std::uint32_t>(descriptor,static_cast<std::uint32_t>(effects.effects.size()));
            memory.store<std::uint32_t>(descriptor+4,effects.descriptor_flags);
            for(unsigned i=0;i<effects.effects.size();++i) for(unsigned field=0;field<8;++field) {
                auto value=effects.effects[i].descriptor[field];
                if(field==0) value=program_base+effects.effects[i].file_offset-0x818;
                else if(field==2) value=x_base+0x200+value-effects.data_file_offset;
                else if(field==4) value+=0xFE836000;
                else if(field==6) value+=scratch_base;
                memory.store<std::uint32_t>(descriptor+8+i*32+field*4,value);
            }
            audio_effect_descriptor=descriptor;audio_effect_count=static_cast<std::uint32_t>(effects.effects.size());
            audio_effect_program_words=effects.program_words;audio_effect_scratch_bytes=scratch_bytes;
            audio_reverb=locations?memory.load<std::uint32_t>(locations):UINT32_MAX;
            audio_crosstalk=locations?memory.load<std::uint32_t>(locations+4):UINT32_MAX;
            update_headphones();
            if(output) memory.store<std::uint32_t>(output,descriptor);
            finish(cpu,20,success);return;
        }
        if(address==0x0022F932 || address==0x0022F9F9 || address==0x0022F956 || address==0x0022F9C4 ||
            address==0x0022FA1D || address==0x0022F97A || address==0x0022F3F5) {
            if(object!=audio_device) throw NativeBoundary(address,"invalid native sound device");
            const auto real=[&](unsigned index){const auto value=std::bit_cast<float>(argument(cpu,index));
                if(!std::isfinite(value)) throw NativeBoundary(address,"non-finite audio listener parameter");return value;};
            if(address==0x0022F3F5) {commit_spatial();finish(cpu,4,success);return;}
            const unsigned field=address==0x0022F9C4?1:address==0x0022FA1D?2:address==0x0022F97A?4:
                address==0x0022F932?8:address==0x0022F9F9?16:32;
            const unsigned count=field<4?3:field==4?6:1;
            const auto deferred=argument(cpu,count+1);
            if(deferred>1) throw NativeBoundary(address,"invalid audio listener apply mode");
            const auto previous_listener=audio_listener;
            auto& listener=deferred?audio_deferred_listener:audio_listener;
            if(count==1) {
                const auto factor=real(1);if(factor<0 || (field==8 && factor==0)) throw NativeBoundary(address,"invalid audio listener factor");
                if(field==8) listener.distance=factor;else if(field==16) listener.rolloff=factor;else listener.doppler=factor;
            }
            else if(count==3) for(unsigned i=0;i<3;++i) (field==1?listener.position:listener.velocity)[i]=real(i+1);
            else for(unsigned i=0;i<3;++i) {listener.front[i]=real(i+1);listener.up[i]=real(i+4);}
            if(deferred) audio_pending_listener|=field;
            else {
                copy_listener(audio_deferred_listener,audio_listener,field);
                audio_pending_listener&=~field;
                if(listener_needs_update(previous_listener)) {
                    std::array<AudioBuffer*,256> buffers;unsigned count=0;
                    for(auto& [buffer_object,buffer]:audio_buffers) buffers[count++]=&buffer;
                    update_spatial(std::span(buffers).first(count));
                }
            }
            finish(cpu,(count+2)*4,success);return;
        }
        if(address==0x0022F8EA) {
            if(object!=audio_device || argument(cpu,3)) throw NativeBoundary(address,"invalid sound buffer device or aggregation");
            const auto descriptor=argument(cpu,1),output=argument(cpu,2);
            if(memory.load<std::uint32_t>(descriptor)!=24) throw NativeBoundary(address,"unexpected sound buffer descriptor size");
            const auto flags=memory.load<std::uint32_t>(descriptor+4),size=memory.load<std::uint32_t>(descriptor+8);
            const auto bus=(flags&0x100000U)?AudioBus::effects_manual:(flags&0x80000U)?AudioBus::effects:
                (flags&0x2000U)?AudioBus::submix:AudioBus::none;
            const auto input_bin=(flags&0x180000U)?memory.load<std::uint32_t>(descriptor+20):(flags&0x2000U)?31U:UINT32_MAX;
            const auto format=audio_format(bus!=AudioBus::none?0x00237B78U:memory.load<std::uint32_t>(descriptor+12));
            const auto identifier=audio->create(format,bus,input_bin);
            audio->headroom(identifier,(flags&0x182010U)?0:600);
            if(const auto bins=memory.load<std::uint32_t>(descriptor+16)) audio_mixbins(identifier,bins);
            else if(flags&0x10) {
                constexpr std::array<MixBin,5> spatial_bins{{{6,0},{8,0},{7,0},{9,0},{10,0}}};
                audio->mixbins(identifier,spatial_bins);
            }
            const auto raw=allocate(page,page,true,PAGE_READWRITE);
            if(!raw) {audio->release(identifier);finish(cpu,16,0x8007000EU);return;}
            const auto public_object=raw+28,body=raw+64,voice=raw+320;
            // Public +0/+4 expose the original buffer descriptor and voice. The
            // SDK's hardware objects stay owned by the SDL provider.
            memory.store<std::uint32_t>(raw,0x002C9474);memory.store<std::uint32_t>(raw+4,1);
            memory.store<std::uint32_t>(raw+8,audio_device-8);
            memory.store<std::uint32_t>(public_object,body);memory.store<std::uint32_t>(public_object+4,voice);
            memory.store<std::uint32_t>(body,0x002C94A8);memory.store<std::uint32_t>(body+4,1);
            memory.store<std::uint32_t>(body+8,flags);memory.store<std::uint32_t>(body+0xD0,input_bin);
            memory.store<std::uint32_t>(body+0xBC,size);
            memory.store<std::uint32_t>(body+0xC4,size);memory.store<std::uint32_t>(body+0xCC,size);
            memory.store<std::uint16_t>(voice+18,1);
            auto& buffer=audio_buffers.emplace(public_object,AudioBuffer{raw,body,voice,identifier,format,flags,input_bin}).first->second;
            if(flags&0x10) {
                buffer.spatial[0]=76;
                std::memcpy(buffer.spatial.data()+1,memory.access(0x00237C74,72),72);
                std::memcpy(buffer.environment.data(),memory.access(0x00237C48,36),36);
                buffer.deferred_spatial=buffer.spatial;buffer.deferred_environment=buffer.environment;
                buffer.has_spatial=true;update_spatial(buffer);
            }
            if(size) {
                std::vector<std::byte> initial(size);audio->data(identifier,initial);
            }
            memory.store<std::uint32_t>(output,public_object);finish(cpu,16,success);return;
        }
        const auto found=audio_buffers.find(object);
        if(found==audio_buffers.end()) throw NativeBoundary(address,"invalid native sound buffer");
        auto& buffer=found->second;
        switch(address) {
        case 0x0022C11B: {
            finish(cpu,4,release_audio_buffer(object));return;
        }
        case 0x0022EF4B: {
            const auto pointer=argument(cpu,1),size=argument(cpu,2);
            audio->bind(buffer.identifier,std::span(static_cast<const std::byte*>(memory.access(pointer,size)),size),pointer);
            memory.store<std::uint32_t>(buffer.descriptor+0xB8,pointer);memory.store<std::uint32_t>(buffer.descriptor+0xBC,size);
            memory.store<std::uint32_t>(buffer.descriptor+0xC0,0);memory.store<std::uint32_t>(buffer.descriptor+0xC4,size);
            memory.store<std::uint32_t>(buffer.descriptor+0xC8,0);memory.store<std::uint32_t>(buffer.descriptor+0xCC,size);
            finish(cpu,12,success);return;
        }
        case 0x0022EF2F: {
            const auto format=audio_format(argument(cpu,1));audio->format(buffer.identifier,format);buffer.format=format;
            finish(cpu,8,success);return;
        }
        case 0x0022D7D2:audio->volume(buffer.identifier,std::bit_cast<std::int32_t>(argument(cpu,1)));finish(cpu,8,success);return;
        case 0x0022D85E:audio->headroom(buffer.identifier,argument(cpu,1));finish(cpu,8,success);return;
        case 0x0022E770:audio_mixbins(buffer.identifier,argument(cpu,1));finish(cpu,8,success);return;
        case 0x0022D87A:case 0x0022D3F0:audio_mixbins(buffer.identifier,argument(cpu,1),true);finish(cpu,8,success);return;
        case 0x0022E738:audio->frequency(buffer.identifier,argument(cpu,1));finish(cpu,8,success);return;
        case 0x0022E754: {
            audio_output(buffer.identifier,buffer.descriptor,argument(cpu,1),address);finish(cpu,8,success);return;
        }
        case 0x0022E8C4: {
            const auto pointer=argument(cpu,1),count=argument(cpu,2),apply=argument(cpu,3);
            if(count>1024 || (count && !pointer) || apply>1) throw NativeBoundary(address,"invalid audio distance curve or apply mode");
            for(unsigned i=0;i<count;++i) {
                const auto gain=std::bit_cast<float>(memory.load<std::uint32_t>(pointer+i*4));
                if(!std::isfinite(gain) || gain<0 || gain>1) throw NativeBoundary(address,"invalid audio distance curve gain");
            }
            buffer.deferred_curve=pointer;buffer.deferred_curve_count=count;buffer.pending_curve=apply!=0;
            if(!apply) {buffer.curve=pointer;buffer.curve_count=count;update_spatial(buffer);}
            finish(cpu,16,success);return;
        }
        case 0x0022E78C: {
            const auto apply=argument(cpu,2);if(apply>1) throw NativeBoundary(address,"invalid 3D audio apply mode");
            SpatialParameters parameters;std::memcpy(parameters.data(),memory.access(argument(cpu,1),sizeof(parameters)),sizeof(parameters));
            Spatial::validate(parameters);buffer.deferred_spatial=parameters;buffer.pending_spatial=apply!=0;
            if(!apply) {buffer.spatial=parameters;buffer.has_spatial=true;update_spatial(buffer);}
            finish(cpu,12,success);return;
        }
        case 0x0022E8E8: {
            const auto apply=argument(cpu,2);
            if(!(buffer.flags&0x10) || apply>1) throw NativeBoundary(address,"invalid environmental audio source or apply mode");
            EnvironmentParameters parameters;
            std::memcpy(parameters.data(),memory.access(argument(cpu,1),sizeof(parameters)),sizeof(parameters));
            Spatial::validate(parameters);buffer.deferred_environment=parameters;buffer.pending_environment=apply!=0;
            if(!apply) {buffer.environment=parameters;update_spatial(buffer);}
            finish(cpu,12,success);return;
        }
        case 0x0022D8F6:case 0x0022E908: {
            const auto start=argument(cpu,1),size=argument(cpu,2);const bool loop=address==0x0022D8F6;
            const auto capacity=memory.load<std::uint32_t>(buffer.descriptor+(loop?0xC4:0xBC));
            if(start>capacity || size>capacity-start) {finish(cpu,12,0x88780032U);return;}
            const auto length=size?size:capacity-start;
            audio->region(buffer.identifier,loop,start,length);const auto offset=loop?0xC8U:0xC0U;
            memory.store<std::uint32_t>(buffer.descriptor+offset,start);memory.store<std::uint32_t>(buffer.descriptor+offset+4,length);
            if(!loop) {memory.store<std::uint32_t>(buffer.descriptor+0xC8,0);memory.store<std::uint32_t>(buffer.descriptor+0xCC,length);}
            finish(cpu,12,success);return;
        }
        case 0x0022D896: {
            const auto flags=argument(cpu,3);
            if(argument(cpu,1) || argument(cpu,2) || (flags&~1U)) throw NativeBoundary(address,"unsupported sound buffer play mode");
            audio->play(buffer.identifier,(flags&1)!=0);memory.store<std::uint16_t>(buffer.voice+18,3);finish(cpu,16,success);return;
        }
        case 0x0022D8BA:audio->stop(buffer.identifier);memory.store<std::uint16_t>(buffer.voice+18,1);finish(cpu,4,success);return;
        case 0x0022D8D2:
            if(argument(cpu,1) || argument(cpu,2) || argument(cpu,3))
                throw NativeBoundary(address,"scheduled or envelope sound-buffer stops are unsupported");
            audio->stop(buffer.identifier);memory.store<std::uint16_t>(buffer.voice+18,1);finish(cpu,16,success);return;
        case 0x0022D916: {
            const auto status=audio->status(buffer.identifier);
            memory.store<std::uint32_t>(argument(cpu,1),status);memory.store<std::uint16_t>(buffer.voice+18,static_cast<std::uint16_t>((status&1)?3:1));
            finish(cpu,8,success);return;
        }
        case 0x0022D932: {
            const auto position=audio->positions(buffer.identifier);
            if(argument(cpu,1)) memory.store<std::uint32_t>(argument(cpu,1),position.play);
            if(argument(cpu,2)) memory.store<std::uint32_t>(argument(cpu,2),position.write);
            finish(cpu,12,success);return;
        }
        case 0x0022D952:audio->position(buffer.identifier,argument(cpu,1));finish(cpu,8,success);return;
        default:throw NativeBoundary(address,"native audio API is unbound");
        }
    }
    std::uint32_t world_call(Cpu& cpu,std::uint32_t entry,std::uint32_t object,
                             std::span<const std::uint32_t> arguments={},unsigned cleanup=UINT32_MAX) {
        const auto registers=cpu.registers;const auto flags=cpu.flags,pc=cpu.eip;
        const auto stack=registers[esp]-0x1000-static_cast<std::uint32_t>(arguments.size()*4);
        memory.store<std::uint32_t>(stack,entry);
        for(unsigned i=0;i<arguments.size();++i) memory.store<std::uint32_t>(stack+4+i*4,arguments[i]);
        cpu.registers[esp]=stack;cpu.registers[ecx]=object;
        invoke_native(cpu,memory,entry);
        if(cleanup==UINT32_MAX) cleanup=unsigned(arguments.size()*4);
        if(cpu.eip!=entry || cpu.registers[esp]!=stack+4+cleanup)
            throw NativeBoundary(entry,"world enhancement helper has an unexpected original ABI");
        const auto result=cpu.registers[eax];cpu.registers=registers;cpu.flags=flags;cpu.eip=pc;return result;
    }
    void select_imported_track(bool selected) {
        if(!selected && imported.playlist) {
            std::memcpy(memory.access(imported.songs,36),imported.first_song.data(),36);
            std::memcpy(memory.access(imported.playlist,68),imported.music_playlist.data(),68);
            unsigned i=0;for(const auto address:{0x57C210U,0x57C1ECU,0x57C208U})
                memory.store<std::uint32_t>(address,imported.music_selection[i++]);
            imported.playlist=0;
        }
        if(!selected) {imported.barrier=0;imported.materials={};}
        // Retained for offline imported-asset diagnostics; no frontend entry.
        if(selected && !imported.storage) {
            imported.storage=allocate(4096,page,true,PAGE_READWRITE);
            if(!imported.storage) throw std::runtime_error("Cannot allocate imported asset labels");
            constexpr char label[]="Interstate|Burnout 1",welcome[]="Welcome to Interstate";
            std::memcpy(memory.access(imported.storage+0x600,sizeof(label)),label,sizeof(label));
            std::memcpy(memory.access(imported.storage+0x640,sizeof(welcome)),welcome,sizeof(welcome));
        }
        imported.selected=selected;
        name_imported_track(selected);
    }
    void name_imported_track(bool selected) {
        auto names=imported.names,welcomes=imported.welcomes;std::array<std::uint32_t,7> tables{};
        for(unsigned language=0;language<tables.size();++language) {
            const auto table=tables[language]=memory.load<std::uint32_t>(0x35272C+language*4);
            if(!table) continue;
            if(imported.tables[language]!=table) {
                const auto previous=std::ranges::find(imported.tables,table);
                names[language]=previous==imported.tables.end()?memory.load<std::uint32_t>(table+390*4):
                    imported.names[previous-imported.tables.begin()];
                welcomes[language]=previous==imported.tables.end()?memory.load<std::uint32_t>(table+728*4):
                    imported.welcomes[previous-imported.tables.begin()];
            }
        }
        imported.tables=tables;imported.names=names;imported.welcomes=welcomes;
        for(unsigned language=0;language<tables.size();++language) if(tables[language]) {
            memory.store<std::uint32_t>(tables[language]+390*4,selected?imported.storage+0x600:names[language]);
            memory.store<std::uint32_t>(tables[language]+728*4,selected?imported.storage+0x640:welcomes[language]);
        }
    }
    void imported_arrows(std::uint32_t site,std::uint32_t object) {
        if(!imported.selected) return;
        if(site==0x8BCD0) {
            const auto master=memory.load<std::uint32_t>(object+8);
            if(!master || memory.load<std::uint32_t>(master+16)!=232)
                throw NativeBoundary(site,"imported barrier animation has no five-frame texture set");
            const auto textures=memory.load<std::uint32_t>(master+20);
            const auto materials=memory.load<std::uint32_t>(master+4);
            if(materials>512) throw NativeBoundary(site,"imported material table exceeds its allocation");
            if(materials<142) throw NativeBoundary(site,"imported material table has no source materials");
            imported.materials={memory.load<std::uint32_t>(master+12)+142*52,
                {static_cast<const std::uint8_t*>(memory.access(master+imported_alpha_offset+142,materials-142)),materials-142}};
            imported.barrier=memory.load<std::uint32_t>(master+12)+216*52;
            for(unsigned i=0;i<5;++i) imported.frames[i]=memory.load<std::uint32_t>(textures+(226+i)*4);
            imported.animation=0;
            memory.store<std::uint32_t>(imported.barrier+20,imported.frames[0]);
        } else if(imported.barrier && object==0x4B95B0) {
            const auto scene=memory.load<std::uint32_t>(0x34AB5C);
            if(!scene || memory.load<std::uint32_t>(scene+8)) return;
            const auto delta=game_timing.started?game_timing.render_delta:
                std::bit_cast<float>(memory.load<std::uint32_t>(0x598188));
            if(!std::isfinite(delta) || delta<0) throw NativeBoundary(site,"invalid game frame duration");
            // B1 AEB36 advances every five 60 Hz frames. Use game time so
            // uncapped rendering and pause keep the original animation speed.
            imported.animation=std::fmod(imported.animation+double(delta)*12,5.0);
            memory.store<std::uint32_t>(imported.barrier+20,imported.frames[static_cast<unsigned>(imported.animation)]);
        }
    }
    void music_call(Cpu& cpu,std::uint32_t entry,std::uint32_t return_pc,std::uint32_t object,
                    std::initializer_list<std::uint32_t> arguments={}) {
        const auto registers=cpu.registers;const auto flags=cpu.flags,pc=cpu.eip;
        const auto stack=registers[esp]-0x400;
        cpu.registers[esp]=stack;cpu.registers[ecx]=object;
        memory.store<std::uint32_t>(stack,return_pc);
        unsigned i=0;for(const auto argument:arguments) memory.store<std::uint32_t>(stack+4+i++*4,argument);
        invoke_native(cpu,memory,entry);
        if(cpu.eip!=return_pc || cpu.registers[esp]!=stack+4+arguments.size()*4)
            throw NativeBoundary(entry,"original music selection has an unexpected calling convention");
        cpu.registers=registers;cpu.flags=flags;cpu.eip=pc;
    }
    void imported_music(Cpu& cpu,std::uint32_t object) {
        if(!imported.selected || imported.playlist) return;
        // Use the title's own lazy catalogue and WMA playlist, just as its
        // Music Selection screen does. No audio files are imported or replaced.
        if(!memory.load<std::uint32_t>(0x57C204)) music_call(cpu,0xC8D70,0xC91CA,object);
        const auto table=memory.load<std::uint32_t>(object+4),count=memory.load<std::uint32_t>(object+0x10);
        if(!table || !count || std::uint64_t(table)+std::uint64_t(count)*68!=memory.load<std::uint32_t>(object+8) ||
            !memory.try_access(table,std::uint64_t(count)*68)) throw NativeBoundary(0xC93C0,"invalid original music catalogue");
        unsigned index=0;
        while(index<count && memory.load<std::uint32_t>(table+index*68+0x10)!=1) ++index;
        if(index==count) throw NativeBoundary(0xC93C0,"original Burnout 1 playlist is missing");
        const auto playlist=table+index*68,songs=memory.load<std::uint32_t>(playlist+4);
        if(memory.load<std::uint32_t>(playlist+0x38)!=12 || !songs)
            throw NativeBoundary(0xC93C0,"unexpected original Burnout 1 song list");
        std::memcpy(imported.music_playlist.data(),memory.access(playlist,68),68);
        std::memcpy(imported.first_song.data(),memory.access(songs,36),36);
        unsigned i=0;for(const auto address:{0x57C210U,0x57C1ECU,0x57C208U})
            imported.music_selection[i++]=memory.load<std::uint32_t>(address);
        imported.playlist=playlist;imported.songs=songs;
        // B1's usa2 course ID 0 selects music entry 0: track7.wav.
        std::memmove(memory.access(songs,36),memory.access(songs+6*36,36),36);
        memory.store<std::uint32_t>(playlist+8,songs+36);memory.store<std::uint32_t>(playlist+0x38,1);
        memory.store<std::uint32_t>(0x57C210,1);memory.store<std::uint32_t>(0x57C1EC,index);memory.store<std::uint32_t>(0x57C208,0);
        music_call(cpu,0xC91D0,0xC9AFB,object);
    }
    void set_music_loop(bool enabled) {
        if(host.loop_track==enabled) return;
        host.loop_track=enabled;
        if(host.music_loop_changed) host.music_loop_changed(enabled);
    }
    bool music_control(Cpu& cpu,std::uint32_t site) {
        if(site==0x21066E) {
            // The original stream's EOF path calls Next at 2142D8. Keep its
            // current queue entry on repeat; initial selection and manual Next
            // still advance normally. The original code reopens the song.
            if(cpu.registers[esi]==memory.load<std::uint32_t>(0x57C1F4) && cpu.registers[ecx] &&
               memory.load<std::uint32_t>(cpu.registers[esp]+8)==0x21072C &&
               memory.load<std::uint32_t>(cpu.registers[ebp]+4)==0x2142DD) {
                music_repeated=host.loop_track && memory.load<std::uint32_t>(0x57C210)!=0;
                return music_repeated;
            }
        } else if(site==0xC9333) music_repeated=false;
        else if(site==0xCA348) {const auto repeated=music_repeated;music_repeated=false;return repeated;}
        else if(site==0x462DA || site==0x4788F || site==0x46EDA) {
            if(!music_menu_storage) {
                music_menu_storage=allocate(page,page,true,PAGE_READWRITE);
                if(!music_menu_storage) throw NativeBoundary(site,"cannot allocate music menu options");
                constexpr char label[]="Loop Track",off[]="Off",on[]="On";
                std::memcpy(memory.access(music_menu_storage+0x60,sizeof(label)),label,sizeof(label));
                std::memcpy(memory.access(music_menu_storage+0x70,sizeof(off)),off,sizeof(off));
                std::memcpy(memory.access(music_menu_storage+0x78,sizeof(on)),on,sizeof(on));
                memory.store<std::uint32_t>(music_menu_storage+0x50,music_menu_storage+0x70);
                memory.store<std::uint32_t>(music_menu_storage+0x54,music_menu_storage+0x78);
            }
            const bool paused=site!=0x462DA;
            const auto object=cpu.registers[esi],descriptor=object+(paused?0x80:0x1F9C);
            const auto source=memory.load<std::uint32_t>(descriptor+4),storage=music_menu_storage+(paused?0x100:0);
            const auto count=memory.load<std::uint32_t>(descriptor);
            if(paused) {
                if((count!=5 && count!=6) || (source!=object+0xA0 && source!=storage))
                    throw NativeBoundary(site,"unexpected pause Options rows");
                if(source!=storage) std::memcpy(memory.access(storage,96),memory.access(source,96),96);
                // Preserve the original volume/vibration fields used by its
                // live-volume and confirm handlers. Music rows stay at 2/3.
                for(unsigned i=0;i<2;++i)
                    memory.store<std::uint32_t>(storage+i*24+16,memory.load<std::uint32_t>(object+0xB0+i*24));
                std::memcpy(memory.access(storage+120,24),memory.access(object+0x100,24),24);
            } else {
                if(count!=2) throw NativeBoundary(site,"unexpected Music Selection rows");
                std::memcpy(memory.access(storage,48),memory.access(source,48),48);
            }
            const std::array<std::uint32_t,6> row{music_menu_storage+0x60,0,0,music_menu_storage+0x50,host.loop_track?1U:0U,2};
            std::memcpy(memory.access(storage+(paused?96:48),sizeof(row)),row.data(),sizeof(row));
            memory.store<std::uint32_t>(descriptor,paused?6:3);memory.store<std::uint32_t>(descriptor+4,storage);
            memory.store<std::uint32_t>(object+(paused?0x216C:0x1F68),storage);
        } else if(site==0x46510) set_music_loop(memory.load<std::uint32_t>(music_menu_storage+64)!=0);
        else if(site==0x47190 || site==0x47220) {
            const auto object=cpu.registers[ecx],rows=memory.load<std::uint32_t>(object+0x84);
            if(!music_menu_storage || rows!=music_menu_storage+0x100) throw NativeBoundary(site,"pause music options are not initialized");
            for(unsigned i=0;i<2;++i)
                memory.store<std::uint32_t>(object+0xB0+i*24,memory.load<std::uint32_t>(rows+i*24+16));
            memory.store<std::uint32_t>(object+0x110,memory.load<std::uint32_t>(rows+136));
            if(site==0x47220) set_music_loop(memory.load<std::uint32_t>(rows+112)!=0);
        } else throw NativeBoundary(site,"unsupported music control site");
        return false;
    }
    bool imported_camera_zoom(Cpu& cpu,std::uint32_t object) {
        if(!imported.selected) return false;
        const auto record=memory.load<std::uint32_t>(object+8);
        if(!record) return false;
        const auto id=memory.load<std::uint32_t>(record+0x10);
        const auto source=memory.load<std::uint32_t>(0x4B8494);
        if(!id || id>47 || !source || memory.load<std::uint32_t>(0x4B8498)!=47)
            throw NativeBoundary(0xB5100,"invalid imported replay camera");
        const auto authored=source+(id-1)*44;const auto zoom=memory.load<std::uint8_t>(authored+33);
        if(!zoom) return false;
        const auto car=memory.load<std::uint32_t>(object+0x1C),camera=memory.load<std::uint32_t>(object+0x18);
        const auto transform=world_call(cpu,memory.load<std::uint32_t>(memory.load<std::uint32_t>(car)+0x18),car);
        const auto value=[&](unsigned address){return std::bit_cast<float>(memory.load<std::uint32_t>(address));};
        float squared=0;for(unsigned axis=0;axis<3;++axis) {
            const auto delta=value(transform+0x30+axis*4)-value(object+0x140+axis*4);squared+=delta*delta;
        }
        const auto distance=std::sqrt(squared);const auto model=memory.load<std::uint32_t>(car+0xCEC);
        const auto width=value(model+0xCC0)-value(model+0xCD0),length=value(model+0xCC8)-value(model+0xCD8);
        // B1 8EC92: authored zoom, smoothing and per-camera FOV limits.
        // B2 otherwise substitutes its global 3..120 degree limits.
        const auto desired=std::atan(width*zoom/(distance-length))*229.1831180523293f;
        const auto slot=memory.load<std::uint32_t>(camera+0x228),field=camera+slot*0xBC+0x18C;
        const auto delta=game_timing.started?game_timing.render_delta:value(0x598188);
        const auto weight=1.f-std::pow(0.95f,std::max(0.f,delta)*60.f);
        const auto minimum=float(memory.load<std::uint8_t>(authored+34)),maximum=float(memory.load<std::uint8_t>(authored+35));
        if(!std::isfinite(desired) || minimum>maximum) throw NativeBoundary(0xB5100,"invalid source camera zoom");
        const auto fov=std::clamp(std::lerp(value(field),desired,weight),minimum,maximum);
        const std::array arguments{std::bit_cast<std::uint32_t>(fov)};world_call(cpu,0xA04A0,camera,arguments);
        memory.store<std::uint32_t>(field,std::bit_cast<std::uint32_t>(fov));
        memory.store<std::uint32_t>(object+0xDC,std::bit_cast<std::uint32_t>(fov));
        memory.store<std::uint32_t>(object+0xE0,std::bit_cast<std::uint32_t>(distance));
        memory.store<std::uint32_t>(camera+(slot+2)*0xBC,std::bit_cast<std::uint32_t>(distance));return true;
    }
    std::filesystem::path imported_file(std::string_view relative) {
        if(!imported.selected || host.imported_tracks.empty() || memory.load<std::uint32_t>(0x4CCABC)!=8) return {};
        const auto path=folded(relative);
        if(path=="TRACKPSS\\FORWARD\\FWYL.PSS") return host.imported_tracks/"interstate.pss";
        if(path=="TRACKS1\\FORWARD\\FWYL\\FWYL.PSW" || path=="TRACKS1\\FORWARD\\FWYL\\FWYL.P2W") return host.imported_tracks/"interstate.psw";
        if(path.starts_with("TRACKS\\FORWARD\\FWYL\\") && path.ends_with(".BGD")) return host.imported_tracks/"interstate.bgd";
        if(path=="TRACKS\\FORWARD\\FWYL\\FWYL_0.BCD") return host.imported_tracks/"interstate.bcd";
        return {};
    }
    std::filesystem::path replacement_file(std::string_view relative) {
        if(mods) if(const auto* file=mods->lookup(relative)) return file->source;
        return imported_file(relative);
    }
    unsigned world_target(unsigned origin,unsigned row) const {
        const auto relative=std::bit_cast<std::int32_t>(memory.load<std::uint32_t>(world_base+origin*world_stride+0x218+row*0x54));
        const auto count=static_cast<int>(world_sections.size());
        return static_cast<unsigned>((static_cast<int>(origin)+relative%count+count)%count);
    }
    bool world_faces(const WorldRecord& record,const std::array<float,2>& position) const {
        const auto source=world_base+record.origin*world_stride;
        const auto groups=memory.load<std::uint32_t>(source+0x7AC);
        for(unsigned group=0;group<groups;++group) {
            const auto mesh=memory.load<std::uint8_t>(source+0x21C+record.row*0x54+group);
            if(mesh!=255 && !world_sections[record.section].cones[mesh].contains(position)) return false;
        }
        return true;
    }
    unsigned world_groups(std::uint32_t source) const {
        // 920F0 submits the groups after +7AC in a separate, later pass.
        return memory.load<std::uint32_t>(source+0x7AC)+memory.load<std::uint16_t>(source+0x212);
    }
    unsigned world_phase(std::uint32_t source,unsigned group) const {
        return group<memory.load<std::uint8_t>(source+0x214)?0U:group<memory.load<std::uint8_t>(source+0x215)?1U:
            group<memory.load<std::uint32_t>(source+0x7AC)?2U:3U;
    }
    bool world_visible(unsigned index,const std::array<std::array<float,4>,6>& planes) const {
        const auto& section=world_sections[index];
        // 90740's rotated base and raised corners, without enlarging the box.
        for(const auto& plane:planes) {
            float nearest=INFINITY;
            for(const auto& corner:section.corners)
                nearest=std::min(nearest,corner[0]*plane[0]+corner[1]*plane[1]+corner[2]*plane[2]);
            if(nearest+std::min(0.f,section.height*plane[1])>plane[3]) return false;
        }
        return true;
    }
    void prepare_world(Cpu& cpu) {
        if(world_preparing) return;
        const auto manager=cpu.registers[ecx],master=memory.load<std::uint32_t>(0x4B85B8);
        const auto mode=unsigned(memory.load<std::uint32_t>(manager+0x14)!=0);
        if(!master || !world_streams[mode] || !disc) throw NativeBoundary(0x8EEB0,"extended scenery has no world stream");
        const auto entry=*world_streams[mode];
        if(world_loaded && world_loaded->sector==entry.sector && world_loaded_path==world_paths[mode]) return;
        const auto count=memory.load<std::uint32_t>(master+0x5C);
        if(!count || count>256 || std::uint64_t(count)*world_stride!=entry.size)
            throw NativeBoundary(0x8EEB0,"unsupported extended scenery dimensions");
        world_view.manager=0;world_view.blending=false;world_draw_count=0;world_draws.clear();world_loaded.reset();world_preparing=true;
        world_handoffs={};world_handoff_draws.clear();world_handoff_backgrounds.clear();
        world_section_ids.fill(UINT32_MAX);
        world_coverage.assign(count*80*4,0);world_handoff_draws.reserve(count*80*4);world_handoff_backgrounds.reserve(128);
        struct Guard {bool& flag;~Guard(){flag=false;}} guard{world_preparing};
        if(gpu) gpu->invalidate_memory(world_base,64U*1024*1024);
        std::optional<File> replacement;
        if(!world_paths[mode].empty()) replacement.emplace(world_paths[mode]);
        for(std::uint32_t offset=0;offset<entry.size;) {
            const auto size=std::min(1024U*1024,entry.size-offset);
            const auto bytes=std::span(world_storage).subspan(offset,size);
            if(replacement) replacement->read(offset,bytes);else disc->read(entry,offset,bytes);
            offset+=size;
        }
        world_sections.clear();world_sections.resize(count);
        std::memset(memory.access(world_scratch,0x318),0,0x318);
        memory.store<std::uint32_t>(world_scratch+0x10,world_base);
        const auto real=[&](std::uint32_t address) {return std::bit_cast<float>(memory.load<std::uint32_t>(address));};
        for(unsigned s=0;s<count;++s) {
            const auto sector=world_base+s*world_stride;
            memory.store<std::uint32_t>(world_scratch+0x24,s);world_call(cpu,0x8EEB0,world_scratch);
            auto& section=world_sections[s];
            const auto rows=memory.load<std::uint32_t>(sector+0x204),groups=memory.load<std::uint32_t>(sector+0x7AC);
            const auto meshes=memory.load<std::uint32_t>(sector+0xBC);
            if(!rows || rows>18 || world_groups(sector)>80 || meshes>80 ||
               memory.load<std::uint8_t>(sector+0x214)>memory.load<std::uint8_t>(sector+0x215) ||
               memory.load<std::uint8_t>(sector+0x215)>groups)
                throw NativeBoundary(0x8EEB0,"unsupported scenery visibility/material table");
            // The prop-table ID is a permutation of the stream order (Airport
            // starts at ID 70), shared by resident and extended section copies.
            const auto id=memory.load<std::uint32_t>(sector+0xB8);
            if(id>=world_section_ids.size() || world_section_ids[id]!=UINT32_MAX)
                throw NativeBoundary(0x8EEB0,"invalid scenery instance section ID");
            world_section_ids[id]=s;
            section.low.fill(INFINITY);section.high.fill(-INFINITY);
            const auto height=real(sector+0x0C);
            if(!std::isfinite(height) || height<0) throw NativeBoundary(0x8EEB0,"invalid scenery bounds");
            section.height=height;
            for(unsigned corner=0;corner<4;++corner) for(unsigned axis=0;axis<3;++axis) {
                const auto value=real(sector+corner*16+axis*4);
                if(!std::isfinite(value)) throw NativeBoundary(0x8EEB0,"invalid scenery corner");
                section.corners[corner][axis]=value;
                section.low[axis]=std::min(section.low[axis],value);
                section.high[axis]=std::max(section.high[axis],value+(axis==1?height:0));
                if(axis!=1) section.center[axis/2]+=value*.25f;
            }
            for(unsigned b=0;b<4;++b) {
                const auto background=memory.load<std::uint8_t>(sector+0x208+b);
                if(background==255) continue;
                if(background>=128 || background>=memory.load<std::uint32_t>(master+0x3C))
                    throw NativeBoundary(0x8EEB0,"invalid scenery backdrop");
                section.backgrounds[background/64]|=1ULL<<(background%64);
            }
            for(unsigned m=0;m<meshes;++m) {
                const auto mesh=memory.load<std::uint32_t>(sector+0xC4+m*4),material=memory.load<std::uint32_t>(mesh+20);
                section.cones[m].material=material;
                if(material>=memory.load<std::uint32_t>(master+4)) throw NativeBoundary(0x8EEB0,"invalid scenery material");
                const auto flags=memory.load<std::uint32_t>(memory.load<std::uint32_t>(master+12)+material*52+16);
                // Same pipeline order as original 90A50/90BC0; C5550 and
                // C3C70 take the vertex stride from that pipeline's +8.
                const auto pipeline=flags&0x8000200?0x4B86B0U:flags&0xFC004A00?0x4B8690U:
                    flags&0x3E00000?0x4B8660U:flags&0x100000?0x4B8670U:flags&0x80000?0x4B8650U:flags&0x8000?0x4B8640U:0;
                if(!pipeline) continue;
                const auto stride=memory.load<std::uint32_t>(pipeline+8),vertices=memory.load<std::uint32_t>(mesh+4);
                const auto indices=memory.load<std::uint32_t>(mesh+12),n=memory.load<std::uint32_t>(mesh+16);
                if(stride<12 || stride>64 || n>32768) throw NativeBoundary(0x8EEB0,"unsupported scenery vertex layout");
                std::array<float,3> low{INFINITY,INFINITY,INFINITY},high{-INFINITY,-INFINITY,-INFINITY};
                for(unsigned j=0;j<n;++j) {
                    const auto vertex=vertices+memory.load<std::uint16_t>(indices+j*2)*stride;
                    // D7A60 clears the Xbox CPU alias bit on vertex buffers.
                    if(vertex-(sector&0x7FFFFFFF)>world_stride-stride)
                        throw NativeBoundary(0x8EEB0,std::format("scenery vertex leaves section {} mesh {} (stride {})",s,m,stride));
                    for(unsigned a=0;a<3;++a) {const auto v=real(vertex+a*4);if(!std::isfinite(v)) throw NativeBoundary(0x8EEB0,"invalid scenery vertex");low[a]=std::min(low[a],v);high[a]=std::max(high[a],v);}
                }
                if(n) {
                    section.cones[m].geometry=true;
                    section.cones[m].low=low;section.cones[m].high=high;
                    for(unsigned a=0;a<2;++a) section.cones[m].center[a]=(low[a*2]+high[a*2])*.5f;
                }
            }
        }
        // A distant view may extend an authored viewing direction, but must
        // not reveal the unbuilt reverse side of a terrain patch. Derive the
        // angular envelope from every original section that selects its mesh.
        std::vector<std::vector<float>> angles(count*80);
        for(unsigned s=0;s<count;++s) {
            const auto source=world_base+s*world_stride;
            const auto rows=memory.load<std::uint32_t>(source+0x204),groups=world_groups(source);
            for(unsigned r=0;r<rows;++r) {
                const auto target=world_target(s,r),sector=world_base+target*world_stride;
                for(unsigned g=0;g<groups;++g) {
                    const auto mesh=memory.load<std::uint8_t>(source+0x21C+r*0x54+g);
                    if(mesh==255) continue;
                    if(mesh>=memory.load<std::uint32_t>(sector+0xBC)) throw NativeBoundary(0x8EEB0,"invalid distant scenery mesh");
                    auto& metadata=world_sections[target].cones[mesh];
                    if(metadata.material!=memory.load<std::uint32_t>(source+0x7B0+g*4))
                        throw NativeBoundary(0x8EEB0,"inconsistent scenery material");
                    if(g>=memory.load<std::uint32_t>(source+0x7AC)) continue;
                    if(!world_sections[target].cones[mesh].geometry) continue;
                    const auto& center=world_sections[target].cones[mesh].center;
                    angles[target*80+mesh].push_back(std::atan2(world_sections[s].center[1]-center[1],world_sections[s].center[0]-center[0]));
                }
            }
        }
        constexpr float pi=3.14159265358979323846f;
        for(unsigned s=0;s<count;++s) for(unsigned m=0;m<80;++m) {
            auto& views=angles[s*80+m];if(views.size()<2) continue;
            std::ranges::sort(views);float gap=0,left=0,right=0;
            for(unsigned i=0;i<views.size();++i) {
                const auto next=i+1<views.size()?views[i+1]:views[0]+2*pi;
                if(next-views[i]>gap) {gap=next-views[i];left=next;right=views[i]+2*pi;}
            }
            if(gap<=pi) continue;
            auto& cone=world_sections[s].cones[m];cone.bounded=true;
            cone.left={std::cos(left),std::sin(left)};cone.right={std::cos(right),std::sin(right)};
        }
        // Choose each mesh independently from rows compatible with the active
        // backdrops. One unsafe terrain mesh must not hide an entire section,
        // nor should a partially visible section exclude its missing meshes.
        // Retain the selected row's material and phase, drawing each mesh once.
        std::size_t capacity=0;
        for(unsigned s=0;s<count;++s) {
            auto& current=world_sections[s];const auto source=world_base+s*world_stride;
            std::vector<std::array<float,80>> best(count);
            std::vector<std::array<WorldRecord,80>> chosen(count);
            std::array<float,256> prop_best;prop_best.fill(INFINITY);
            std::array<WorldRecord,256> prop_rows{};
            for(auto& distances:best) distances.fill(INFINITY);
            for(unsigned r=0;r<memory.load<std::uint32_t>(source+0x204);++r) {
                const auto target=world_target(s,r);
                prop_best[target]=-1;
                for(unsigned g=0;g<memory.load<std::uint32_t>(source+0x7AC);++g) {
                    const auto mesh=memory.load<std::uint8_t>(source+0x21C+r*0x54+g);
                    if(mesh!=255) best[target][mesh]=-1;
                }
            }
            for(unsigned from=0;from<count;++from) {
                const auto& candidate=world_sections[from];
                if((candidate.backgrounds[0]&current.backgrounds[0])!=current.backgrounds[0] ||
                   (candidate.backgrounds[1]&current.backgrounds[1])!=current.backgrounds[1]) continue;
                const auto dx=current.center[0]-candidate.center[0],dz=current.center[1]-candidate.center[1];
                const auto distance=dx*dx+dz*dz;const auto origin=world_base+from*world_stride;
                for(unsigned r=0;r<memory.load<std::uint32_t>(origin+0x204);++r) {
                    const auto target=world_target(from,r);const WorldRecord record{target,from,r};
                    if(distance<prop_best[target] && world_faces(record,current.center)) {
                        prop_rows[target]=record;prop_best[target]=distance;
                    }
                    for(unsigned g=0;g<memory.load<std::uint32_t>(origin+0x7AC);++g) {
                        const auto mesh=memory.load<std::uint8_t>(origin+0x21C+r*0x54+g);
                        if(mesh==255 || !world_sections[target].cones[mesh].geometry || distance>=best[target][mesh]) continue;
                        chosen[target][mesh]=record;best[target][mesh]=distance;
                    }
                }
            }
            for(unsigned target=0;target<count;++target) for(unsigned mesh=0;mesh<80;++mesh) {
                if(best[target][mesh]<0 || !std::isfinite(best[target][mesh])) continue;
                const auto record=chosen[target][mesh];
                auto found=std::ranges::find_if(current.additions,[&](const auto& r) {
                    return r.section==record.section && r.origin==record.origin && r.row==record.row;
                });
                if(found==current.additions.end()) {current.additions.push_back(record);found=std::prev(current.additions.end());}
                found->add(mesh);
            }
            // Props have independent bounds and may stand above or beside the
            // static mesh. Preserve their previous whole-section selection;
            // neither a culled road mesh nor a partial terrain view owns them.
            for(unsigned target=0;target<count;++target)
                if(prop_best[target]>=0 && std::isfinite(prop_best[target])) current.props.push_back(prop_rows[target]);
            capacity=std::max(capacity,current.additions.size());
        }
        world_draws.clear();world_draws.reserve(capacity);
        world_loaded=entry;world_loaded_path=world_paths[mode];world_ready=true;
    }
    void select_world(Cpu& cpu,bool late=false) {
        world_view.manager=0;world_view.blending=false;world_view.props_blending=false;world_view.count=0;world_draw_count=0;world_draws.clear();
        world_prop_admitted={};
        world_added_count=0;world_original_count=0;
        if(!world_loaded || world_preparing || world_reflecting) return;
        const auto manager=cpu.registers[late?edi:esi],origin=memory.load<std::uint32_t>(manager+0x118);
        unsigned index=0;
        for(;index<world_sections.size();++index)
            if(!std::memcmp(memory.access(origin,64),memory.access(world_base+index*world_stride,64),64)) break;
        if(index==world_sections.size()) throw NativeBoundary(0x90F68,"unknown scenery visibility origin");
        world_origin=index;
        const auto camera=memory.load<std::uint32_t>(0x5232C0),frame=memory.load<std::uint32_t>(camera+4);
        std::array<float,3> position;
        std::memcpy(position.data(),memory.access(frame+0x40,12),12);
        std::array<std::array<float,4>,6> planes;
        std::memcpy(planes.data(),memory.access(manager+0x11C,96),96);
        std::array<bool,256> present{};
        const auto append=[&](std::uint32_t sector) {
            const auto slot=world_view.count++;world_view.sectors[slot]=sector;
            world_prop_coverage[slot]=UINT64_MAX;
            memory.store<std::uint32_t>(world_view.list+slot*4,slot);
        };
        const auto original=memory.load<std::uint32_t>(manager+0x310);
        if(original>18) throw NativeBoundary(0x90F68,"invalid original scenery list");
        // Admission is independent of which way the camera points. A tree
        // outside the frustum remains available, so looking back cannot start
        // its loading fade again when the player turns forwards.
        const auto source=world_base+index*world_stride;
        for(unsigned row=0;row<memory.load<std::uint32_t>(source+0x204);++row)
            world_prop_admitted[world_target(index,row)]=true;
        for(unsigned i=0;i<original;++i) {
            const auto row=memory.load<std::uint32_t>(manager+0x2C8+i*4);
            present[world_target(index,row)]=true;
            append(memory.load<std::uint32_t>(manager+0xD0+row*4));
        }
        world_original_count=original;
        const auto range=world_original_far*host.draw_distance;
        for(const auto& record:world_sections[index].props) {
            const auto& section=world_sections[record.section];float squared=0;
            for(unsigned a=0;a<2;++a) {const auto d=position[a*2]-std::clamp(position[a*2],section.low[a*2],section.high[a*2]);squared+=d*d;}
            if(squared>range*range || !world_faces(record,{position[0],position[2]})) continue;
            world_prop_admitted[record.section]=true;
            if(!world_visible(record.section,planes)) continue;
            append(world_base+record.section*world_stride);present[record.section]=true;++world_added_count;
        }
        for(auto record:world_sections[index].additions) {
            const auto& section=world_sections[record.section];
            for(unsigned word=0;word<2;++word) {
                auto remaining=record.meshes[word];
                while(remaining) {
                    const auto bit=std::countr_zero(remaining);remaining&=remaining-1;
                    const auto& mesh=section.cones[word*64+bit];
                    if(!mesh.contains({position[0],position[2]})) {record.meshes[word]&=~(1ULL<<bit);continue;}
                    float squared=0;
                    for(unsigned a=0;a<3;++a) {const auto d=position[a]-std::clamp(position[a],mesh.low[a],mesh.high[a]);squared+=d*d;}
                    if(squared>range*range) {record.meshes[word]&=~(1ULL<<bit);continue;}
                    bool visible=true;
                    // 90740 rejects a box only if every corner has n.p > d.
                    // Test the closest corner of each mesh's actual bounds.
                    for(const auto& plane:planes) {
                        float distance=-plane[3];
                        for(unsigned a=0;a<3;++a) distance+=plane[a]*(plane[a]<0?mesh.high[a]:mesh.low[a]);
                        if(distance>0) {visible=false;break;}
                    }
                    if(!visible) record.meshes[word]&=~(1ULL<<bit);
                }
            }
            if(!(record.meshes[0]|record.meshes[1])) continue;
            if(!present[record.section]) {present[record.section]=true;++world_added_count;}
            world_draws.push_back(record);
        }
        world_draw_count=static_cast<unsigned>(world_draws.size());
        world_view.manager=manager;
        select_world_handoff(camera,index,position,planes,!late);
    }
    void select_world_handoff(std::uint32_t camera,unsigned origin,const std::array<float,3>& position,
                              const std::array<std::array<float,4>,6>& planes,bool advance=true) {
        auto found=std::ranges::find(world_handoffs,camera,&WorldHandoff::camera);
        bool reset=false;
        if(found==world_handoffs.end()) {
            found=std::ranges::find(world_handoffs,0U,&WorldHandoff::camera);
            if(found==world_handoffs.end()) throw NativeBoundary(0x90F68,"scenery handoff camera limit exceeded");
            found->camera=camera;found->origin=origin;found->views.fill(static_cast<std::uint8_t>(origin));
            reset=true;
        }
        auto& handoff=*found;
        if(handoff.origin!=origin) {
            // Unrelated visibility regions indicate a camera cut or teleport.
            // Nearby section changes retain their exact outgoing representation.
            const auto source=world_base+origin*world_stride;bool nearby=false;
            for(unsigned r=0;r<memory.load<std::uint32_t>(source+0x204);++r) nearby|=world_target(origin,r)==handoff.origin;
            if(!nearby) {handoff.views.fill(static_cast<std::uint8_t>(origin));handoff.fraction=0;handoff.cursor=0;reset=true;}
            handoff.origin=origin;
        }
        // Each of 64 ordered coverage samples moves to the newest authored
        // view over a quarter second. Interrupted/reversed handoffs preserve
        // the remaining samples, so no representation is dropped mid-fade.
        const auto delta=game_timing.started?game_timing.render_delta:
            std::bit_cast<float>(memory.load<std::uint32_t>(0x598188));
        if(advance) handoff.fraction+=(std::isfinite(delta)?std::clamp(double(delta),0.0,0.25):0.25)*256;
        const auto steps=static_cast<unsigned>(handoff.fraction);handoff.fraction-=steps;
        const auto changed=steps==64?UINT64_MAX:std::rotl((1ULL<<steps)-1,int(handoff.cursor));
        for(unsigned i=0;i<steps;++i) {handoff.views[handoff.cursor]=static_cast<std::uint8_t>(origin);handoff.cursor=(handoff.cursor+1)%64;}
        if(advance) {
            // Fade admission changes, not frustum visibility. Keep outgoing
            // instances until their last sample fades, culling them normally.
            std::array<bool,256> present{};
            for(unsigned slot=0;slot<world_view.count;++slot)
                present[world_section_ids[memory.load<std::uint32_t>(world_view.sectors[slot]+0xB8)]]=true;
            for(unsigned s=0;s<world_sections.size();++s)
                handoff.props[s]=reset?(world_prop_admitted[s]?UINT64_MAX:0):
                    (handoff.props[s]&~changed)|(world_prop_admitted[s]?changed:0);
            for(unsigned slot=0;slot<world_view.count;++slot)
                world_prop_coverage[slot]=handoff.props[world_section_ids[memory.load<std::uint32_t>(world_view.sectors[slot]+0xB8)]];
            for(unsigned s=0;s<world_sections.size();++s) {
                const auto coverage=handoff.props[s];
                if(!present[s] && !world_prop_admitted[s] && coverage && world_visible(s,planes)) {
                    const auto slot=world_view.count++;world_view.sectors[slot]=world_base+s*world_stride;
                    world_prop_coverage[slot]=coverage;memory.store<std::uint32_t>(world_view.list+slot*4,slot);
                }
            }
            for(unsigned slot=0;slot<world_view.count;++slot) world_view.props_blending|=world_prop_coverage[slot]!=UINT64_MAX;
        }
        std::array<std::uint64_t,256> views{};
        for(unsigned i=0;i<64;++i) views[handoff.views[i]]|=1ULL<<i;
        if(views[origin]==UINT64_MAX) return;
        world_view.blending=true;
        for(const auto key:world_handoff_draws) world_coverage[key]=0;
        world_handoff_draws.clear();world_handoff_backgrounds.clear();world_background_coverage={};
        const auto range=world_original_far*host.draw_distance;
        const auto add=[&](unsigned section,unsigned mesh,unsigned phase,std::uint64_t coverage,bool original) {
            const auto& bounds=world_sections[section].cones[mesh];
            if(!bounds.geometry) return;
            if(!original) {
                if(!bounds.contains({position[0],position[2]})) return;
                float squared=0;
                for(unsigned a=0;a<3;++a) {const auto d=position[a]-std::clamp(position[a],bounds.low[a],bounds.high[a]);squared+=d*d;}
                if(squared>range*range) return;
            }
            for(const auto& plane:planes) {
                float distance=-plane[3];
                for(unsigned a=0;a<3;++a) distance+=plane[a]*(plane[a]<0?bounds.high[a]:bounds.low[a]);
                if(distance>0) return;
            }
            // A few materials change render phase between authored views.
            // Keep those submissions separate with complementary coverage.
            const auto key=(section*80+mesh)*4+phase;
            if(!world_coverage[key]) world_handoff_draws.push_back(key);
            world_coverage[key]|=coverage;
        };
        const auto view=[&](unsigned s) {
            const auto coverage=views[s];const auto source=world_base+s*world_stride;
            const auto rows=memory.load<std::uint32_t>(source+0x204),groups=world_groups(source);
            // 90A50 emits rows backwards, 90BC0 forwards. Keep the current
            // view's material order; shared meshes are submitted only once.
            for(unsigned g=0;g<groups;++g) {
                const auto phase=world_phase(source,g);
                for(unsigned i=0;i<rows;++i) {
                    const auto r=phase==1?i:rows-1-i,mesh=unsigned(memory.load<std::uint8_t>(source+0x21C+r*0x54+g));
                    if(mesh!=255) add(world_target(s,r),mesh,phase,coverage,true);
                }
            }
            for(const auto& record:world_sections[s].additions) {
                const auto from=world_base+record.origin*world_stride;
                for(unsigned g=0;g<memory.load<std::uint32_t>(from+0x7AC);++g) {
                    const auto mesh=memory.load<std::uint8_t>(from+0x21C+record.row*0x54+g);
                    const auto phase=world_phase(from,g);
                    if(mesh!=255 && record.contains(mesh)) add(record.section,mesh,phase,coverage,false);
                }
            }
            for(unsigned b=0;b<4;++b) {
                const auto mesh=memory.load<std::uint8_t>(source+0x208+b);
                if(mesh==255) continue;
                if(!world_background_coverage[mesh]) world_handoff_backgrounds.push_back(mesh);
                world_background_coverage[mesh]|=coverage;
            }
        };
        if(views[origin]) view(origin);
        for(unsigned s=0;s<world_sections.size();++s) if(s!=origin && views[s]) view(s);
    }
    void draw_world_handoff(Cpu& cpu,unsigned phase) {
        std::uint64_t coverage=UINT64_MAX;
        const auto apply=[&](std::uint64_t value) {
            if(coverage==value) return;
            submit_graphics(false);gpu->coverage(value);coverage=value;
        };
        if(phase==4) {
            for(const auto mesh:world_handoff_backgrounds) {
                apply(world_background_coverage[mesh]);
                const std::array arguments{mesh,UINT32_MAX,UINT32_MAX,UINT32_MAX};
                world_call(cpu,0x8EA80,0x4B85B0,arguments);
            }
        } else {
            unsigned material=UINT32_MAX,pipeline=0;
            for(const auto key:world_handoff_draws) {
                if(key%4!=phase) continue;
                const auto section=key/4/80,mesh=key/4%80;
                const auto& metadata=world_sections[section].cones[mesh];
                if(material!=metadata.material) {
                    material=metadata.material;const std::array arguments{material,1U};
                    const auto flags=world_call(cpu,0x8D1A0,0x4B85B0,arguments);
                    // Original 90A50/90BC0's material-to-pipeline dispatch.
                    pipeline=flags&0x8000200?0x4B86B0U:flags&0xFC004A00?0x4B8690U:
                        flags&0x3E00000?0x4B8660U:flags&0x100000?0x4B8670U:flags&0x80000?0x4B8650U:flags&0x8000?0x4B8640U:0;
                }
                if(!pipeline) continue;
                apply(world_coverage[key]);
                const std::array arguments{memory.load<std::uint32_t>(world_base+section*world_stride+0xC4+mesh*4)};
                world_call(cpu,pipeline==0x4B8640?0xC3C70:0xC5550,pipeline,arguments);
            }
        }
        apply(UINT64_MAX);
    }
    void draw_world(Cpu& cpu,unsigned phase) {
        if(!world_view.manager) return;
        if(world_view.blending) {draw_world_handoff(cpu,phase);return;}
        if(phase>=3) return;
        std::memcpy(memory.access(world_scratch+0x11C,96),memory.access(world_view.manager+0x11C,96),96);
        for(unsigned i=0;i<world_draw_count;++i) {
            const auto record=world_draws[i];const auto origin=world_base+record.origin*world_stride;
            const auto opaque=memory.load<std::uint8_t>(origin+0x214),alpha=memory.load<std::uint8_t>(origin+0x215);
            const auto groups=memory.load<std::uint32_t>(origin+0x7AC);
            memory.store<std::uint32_t>(world_scratch+0x118,origin);
            memory.store<std::uint32_t>(world_scratch+0x310,1);
            memory.store<std::uint32_t>(world_scratch+0x2C8,record.row);
            memory.store<std::uint32_t>(world_scratch+0xD0+record.row*4,world_base+record.section*world_stride);
            memory.store<std::uint32_t>(world_scratch+0x88+record.row*4,4);
            const auto begin=phase==0?0U:phase==1?unsigned(opaque):unsigned(alpha);
            const auto end=phase==0?unsigned(opaque):phase==1?unsigned(alpha):groups;
            for(unsigned group=begin;group<end;++group) {
                const auto mesh=memory.load<std::uint8_t>(origin+0x21C+record.row*0x54+group);
                if(mesh==255 || !record.contains(mesh)) continue;
                const std::array arguments{group};world_call(cpu,phase==1?0x90BC0:0x90A50,world_scratch,arguments);++world_added_batches;
            }
        }
    }
    void world_prop_mask(std::uint64_t coverage) {
        if(coverage==world_applied_coverage) return;
        submit_graphics(false);gpu->coverage(coverage);world_applied_coverage=coverage;
    }
    void traffic_detail(Cpu& cpu) {
        const auto manager=cpu.registers[ecx],stack=cpu.registers[esp];
        std::array<std::uint32_t,4> arguments;
        for(unsigned i=0;i<arguments.size();++i) arguments[i]=memory.load<std::uint32_t>(stack+i*4);
        const auto vehicle=arguments[0],lod=arguments[1];
        if(lod>2) throw NativeBoundary(0x9C09A,"invalid traffic detail level");
        const bool shared_wheels=lod==0 || memory.load<std::uint32_t>(vehicle+0x1A0)!=0;
        std::uint64_t coverage=UINT64_MAX;
        if(lod<2 && !memory.load<std::uint32_t>(vehicle+0x1E8)) {
            // 9BFF3 has already measured this camera-relative vector. Fade
            // inside the current LOD's range.
            float squared=0;
            for(unsigned i=0;i<3;++i) {
                const auto delta=std::bit_cast<float>(memory.load<std::uint32_t>(stack+0x50+i*4));squared+=delta*delta;
            }
            const auto end=std::bit_cast<float>(memory.load<std::uint32_t>(manager+0x2E280+lod*4));
            const auto start=end*.8f;
            if(end>0 && squared>start*start) {
                const auto count=unsigned(std::clamp((end-std::sqrt(squared))/(end-start)*64.f,0.f,64.f));
                coverage=count==64?UINT64_MAX:(1ULL<<count)-1;
            }
        }
        // The wheel queue has no opacity field. Carry the body's final opacity
        // as sample coverage, including both sides of a wheel LOD transition.
        const auto samples=unsigned(std::clamp(std::bit_cast<float>(arguments[2])*64.f,0.f,64.f));
        const auto fade=samples==64?UINT64_MAX:(1ULL<<samples)-1;
        const auto draw=[&](std::uint64_t body_mask,std::uint64_t wheel_mask) {
            std::array<unsigned,8> wheels;
            for(unsigned i=0;i<wheels.size();++i) wheels[i]=memory.load<unsigned>(0x528130+i*4);
            world_prop_mask(body_mask);world_call(cpu,0x9A330,manager,arguments);
            for(unsigned i=0;i<wheels.size();++i) {
                const auto count=memory.load<unsigned>(0x528130+i*4);
                if(count>64 || wheels[i]>count) throw NativeBoundary(0x9C09A,"invalid traffic wheel queue");
                for(unsigned j=wheels[i];j<count;++j) traffic_wheel_coverage[i*64+j]=fade&wheel_mask;
            }
        };
        draw(coverage,shared_wheels?UINT64_MAX:coverage);
        if(coverage!=UINT64_MAX) {
            arguments[1]=lod+1;arguments[3]=0;
            cpu.traffic_shared_wheels=shared_wheels;
            draw(~coverage,~coverage);
            cpu.traffic_shared_wheels=false;
        }
        world_prop_mask(UINT64_MAX);
    }
    void extend_camera(Cpu& cpu) {
        // Reflection rendering uses its own visibility and sky geometry. Its
        // camera temporarily occupies the main camera slot, but is not part
        // of the extended scene pass.
        if(!world_ready || world_preparing || world_reflecting) return;
        const auto camera=argument(cpu,0),holder=memory.load<std::uint32_t>(0x443EB0);
        if(!holder || camera!=memory.load<std::uint32_t>(holder)) return;
        const auto far_plane=std::bit_cast<float>(memory.load<std::uint32_t>(camera+0x84));
        if(!(far_plane>0) || !std::isfinite(far_plane)) throw NativeBoundary(0xE8450,"invalid original camera distance");
        auto found=std::ranges::find(world_cameras,camera,&WorldCamera::address);
        if(found==world_cameras.end()) {
            found=std::ranges::find(world_cameras,0U,&WorldCamera::address);
            if(found==world_cameras.end()) throw NativeBoundary(0xE8450,"extended world camera limit exceeded");
            found->address=camera;
        }
        if(far_plane!=found->applied) found->original=far_plane;
        // Scale the game's own distance without compounding on each
        // frame or fitting the camera to the entire track.
        const auto extended=found->original*host.draw_distance;
        if(!std::isfinite(extended)) throw NativeBoundary(0xE8450,"extended camera distance exceeds its bound");
        if(extended!=far_plane) {
            const std::array arguments{camera,std::bit_cast<std::uint32_t>(extended)};
            world_call(cpu,0xE84C0,0,arguments,0);
        }
        found->applied=extended;world_camera=camera;world_original_far=found->original;
    }
    void enhancement(Cpu& cpu,std::uint32_t address) {
        switch(address) {
        case 0x9B380:traffic_wheel_coverage.fill(UINT64_MAX);break;
        case 0x9C09A:traffic_detail(cpu);break;
        case 0x9C12A: {
            const auto matrix=cpu.registers[edi];
            if(matrix<0x5283D8 || matrix>=0x5303D8 || (matrix-0x5283D8)%64)
                throw NativeBoundary(address,"invalid traffic wheel matrix");
            world_prop_mask(traffic_wheel_coverage[(matrix-0x5283D8)/64]);break;
        }
        case 0x9C152:world_prop_mask(UINT64_MAX);break;
        case 0x1AC27:title_hud_widget(cpu,memory);break;
        case 0x1D121: {
            const auto queue=memory.load<std::uint32_t>(0x3D8570),root=memory.load<std::uint32_t>(queue);
            hud_layout.list=memory.load<std::uint32_t>(queue+0x3C);
            hud_layout.root_x=std::bit_cast<float>(memory.load<std::uint32_t>(root+0x20));
            hud_layout.root_scale=std::bit_cast<float>(memory.load<std::uint32_t>(root+0x28));
            const auto native_scale=hud_layout.root_scale*640/memory.load<std::uint32_t>(0x443EC4);
            // Native wide split-screen already uses an approximately 854-wide
            // canvas. Compensate only the horizontal stretch still remaining.
            if(!(native_scale>0) || !std::isfinite(native_scale)) throw NativeBoundary(address,"invalid HUD canvas scale");
            hud_layout.scale=std::min(1.0f,(host.ultrawide?4.0f/7:0.75f)/native_scale);
            break;
        }
        case 0x81158:case 0x81369:case 0x817F7:
            // Shield translucent driving gauges from repeated feedback. Once
            // they are hidden, retain the original full-frame history: reward
            // messages deliberately leave a trail as they slide into view.
            if(title_hud_gauges(memory)) {submit_graphics(false);gpu->begin_overlay();}
            break;
        case 0x215CE0:
            if(gpu) {submit_graphics(false);gpu->restore_feedback();}break;
        // Extra meshes retain authored material/background compatibility;
        // reflection cameras continue to use their original visibility.
        case 0x8EEB0:prepare_world(cpu);break;
        case 0xE8450:if(host.draw_distance>1) extend_camera(cpu);break;
        case 0xA6E00:world_reflecting=true;break;
        case 0xA7306:world_reflecting=false;break;
        case 0x90F68:select_world(cpu);break;
        case 0x90F7E:draw_world(cpu,0);break;
        case 0x90F9E:draw_world(cpu,4);break;
        case 0x90FDC:draw_world(cpu,1);break;
        case 0x910F6:case 0x913E5:world_prop_mask(world_prop_coverage[cpu.registers[edx]]);break;
        case 0x91295:world_prop_mask(world_prop_coverage[cpu.registers[eax]]);break;
        case 0x91370:world_prop_mask(UINT64_MAX);break;
        case 0x914BF:world_prop_mask(UINT64_MAX);draw_world(cpu,2);break;
        case 0x9158F:world_view.manager=0;break;
        case 0x92149:select_world(cpu,true);draw_world(cpu,3);break;
        case 0x92182:world_view.manager=0;break;
        default:throw NativeBoundary(address,"unsupported title enhancement site");
        }
    }
    void graphics_helper(Cpu& cpu,std::uint32_t entry,std::uint32_t return_pc,std::span<const std::uint32_t> arguments={}) {
        const auto registers=cpu.registers;
        const auto flags=cpu.flags,pc=cpu.eip;
        // Execute original SDK setup through its AOT entry, with the return address
        // of the original initializer's call site. Keep the public caller's stack.
        const auto stack=registers[esp]-0x400-static_cast<std::uint32_t>(arguments.size()*4);
        memory.store<std::uint32_t>(stack,return_pc);
        for(unsigned i=0;i<arguments.size();++i) memory.store<std::uint32_t>(stack+4+i*4,arguments[i]);
        cpu.registers[esp]=stack;cpu.registers[ecx]=0x002256C0;
        invoke_native(cpu,memory,entry);
        if(cpu.eip!=return_pc || cpu.registers[esp]!=stack+4+arguments.size()*4)
            throw NativeBoundary(entry,"original graphics initializer helper has an unexpected ABI");
        const auto result=cpu.registers[eax];
        cpu.registers=registers;cpu.registers[eax]=result;cpu.flags=flags;cpu.eip=pc;
    }
    void initialize_graphics(Cpu& cpu) {
        constexpr std::uint32_t context=0x002256C0;
        if(graphics || cpu.registers[ecx]!=context || memory.load<std::uint32_t>(0x002256B8)!=context)
            throw NativeBoundary(cpu.eip,"unsupported graphics device initialization context");
        const auto parameters=argument(cpu,0);
        memory.access(parameters,68);
        const auto width=memory.load<std::uint32_t>(parameters),height=memory.load<std::uint32_t>(parameters+4);
        const auto format=memory.load<std::uint32_t>(parameters+8),samples=memory.load<std::uint32_t>(parameters+16);
        const auto depth=memory.load<std::uint32_t>(parameters+36);
        if(!width || !height || width>4096 || height>4096 || (format!=0x12 && format!=0x1E) || (samples && samples!=0x11) ||
            (memory.load<std::uint32_t>(parameters+32) && depth!=0x2A && depth!=0x2E))
            throw NativeBoundary(cpu.eip,std::format("unsupported graphics dimensions/formats {}x{} color {} samples {} depth {}",
                width,height,hex32(format),hex32(samples),hex32(depth)));
        graphics=host.renderer;
        if(graphics) graphics->resize(width,height);
        else {owned_graphics=std::make_unique<Renderer>(width,height);graphics=owned_graphics.get();}
        const auto notification=allocate(96,page,true,PAGE_READWRITE)|0x80000000U;
        if(notification==0x80000000U) {finish(cpu,4,0x8007000E);return;}
        // Original 21A400 establishes these notification allocations before
        // 21AB90 creates its push-buffer ring. Native submission owns completion.
        memory.store<std::uint32_t>(context+0x2058,notification);
        memory.store<std::uint32_t>(context+0x30,notification);
        memory.store<std::uint32_t>(context+0x2060,notification+32);
        memory.store<std::uint32_t>(context+0x205C,notification+64);
        graphics_helper(cpu,0x0021AB90,0x0021A487);
        if(static_cast<std::int32_t>(cpu.registers[eax])<0) {finish(cpu,4,cpu.registers[eax]);return;}
        gpu_cursor=memory.load<std::uint32_t>(context+0x24);
        gpu=std::make_unique<Gpu>(*graphics,memory,notification);
        gpu->flip=[this] {
            if(graphics->needs_motion_camera()) {
            // 92030 reads the main world camera through this original holder;
            // frontend drawing uses 443EC0. Read their real clip and view data.
                const auto holder=memory.load<std::uint32_t>(0x443EB0);
                const auto camera=holder?memory.load<std::uint32_t>(holder):memory.load<std::uint32_t>(0x443EC0);
                if(camera) {
                    const auto value=[&](unsigned offset) {return std::bit_cast<float>(memory.load<std::uint32_t>(camera+offset));};
                    MotionCamera motion;motion.identifier=camera;motion.near_plane=value(0x80);motion.far_plane=value(0x84);
                    motion.projection=memory.load<std::uint32_t>(camera+0x14);
                    for(unsigned i=0;i<motion.view.size();++i) motion.view[i]=value(0x20+i*4);
                    graphics->camera(camera,motion.near_plane,motion.far_plane,2*std::atan(value(0x6C)),&motion,value(0x68)/value(0x6C));
                } else graphics->camera(0,0,0,0);
            }
            if(host.flip) host.flip();
        };
        if(host.performance && host.performance->active()) gpu->performance(host.performance);
        memory.store<std::uint32_t>(context+0x534,0x40);
        graphics_helper(cpu,0x00219BC0,0x0021A811);
        const std::array presentation{parameters};
        graphics_helper(cpu,0x00219EA0,0x0021A822,presentation);
        if(static_cast<std::int32_t>(cpu.registers[eax])<0) {finish(cpu,4,cpu.registers[eax]);return;}
        const std::array shader{2U};
        memory.store<std::uint32_t>(context+0x37C,0x002244C8);
        graphics_helper(cpu,0x002154E0,0x0021A87D,shader);
        memory.fill_elements<std::uint32_t>(0x002243B0,0,0x46,false);
        memory.store<std::uint32_t>(0x002243B4,0x10);
        const std::array targets{memory.load<std::uint32_t>(context+0x15F4),memory.load<std::uint32_t>(context+0x1600)};
        graphics_helper(cpu,0x00215A70,0x0021A8AC,targets);
        graphics_helper(cpu,0x00219CA0,0x0021A8B1);
        const std::array clear{0U,0U,3U,0U,0x3F800000U,0U};
        graphics_helper(cpu,0x0021CA20,0x0021A8C5,clear);
        const std::array flicker{5U},luma{0U};
        graphics_helper(cpu,0x002158D0,0x0021A93E,flicker);
        graphics_helper(cpu,0x00215920,0x0021A945,luma);
        graphics_initialized=true;
        video_started=std::chrono::steady_clock::now();
        if(live) for(auto& thread:threads) thread->cpu.diagnostic=nullptr;
        finish(cpu,4,0);
    }
    void submit_graphics(bool flush=true) {
        constexpr std::uint32_t context=0x002256C0;
        if(!gpu) throw std::runtime_error("Native GPU submission precedes device initialization");
        const auto flags=memory.load<std::uint32_t>(context+8);
        const auto end=memory.load<std::uint32_t>(context+((flags&4)?0x358:0));
        const auto ring_begin=memory.load<std::uint32_t>(context+0x24),ring_end=memory.load<std::uint32_t>(context+0x28);
        if(gpu_cursor<ring_begin || end<gpu_cursor || end>ring_end) throw std::runtime_error("Original GPU submission left its push-buffer ring");
        if(end!=gpu_cursor) {gpu->submit(gpu_cursor,end,flush);gpu_cursor=end;}
        if(flush && memory.load<std::uint32_t>(0x00225210)) {gpu->wait();memory.store<std::uint32_t>(context+8,flags|0x2000);}
    }
    void platform(Cpu& cpu,std::uint32_t address) {
        if(address==0x000E03BE) {
            const auto source=argument(cpu,0);std::string message;
            for(unsigned i=0;i<65536;++i) {
                const auto value=memory.load<std::uint8_t>(source+i);
                if(!value) {
#ifdef _WIN32
                    OutputDebugStringA(message.c_str());
#endif
                    diagnostic_record("{\"type\":\"guest_debug_output\",\"message\":"+json(message)+'}');
                    finish(cpu,4);return;
                }
                message+=char(value);
            }
            throw NativeBoundary(address,"debug output exceeds its 64 KiB bound");
        }
        if(address==0x00128CF8) {
            const auto error=cancel_io(argument(cpu,0));
            if(error) set_last_error(cpu,error);
            finish(cpu,4,error?0U:1U);return;
        }
        if(address>=0x0022C11B && address<=0x0022FC18) {audio_api(cpu,address);return;}
        if(host.poll && !host.poll()) throw NativeBoundary(address,"Native window closed");
        if(address==0x0021A400) {initialize_graphics(cpu);return;}
        if(address==0x0021AE80 || address==0x0021B1C0 || address==0x00216870 || address==0x0021B080) {
            constexpr std::uint32_t context=0x002256C0;
            try {
                if(!gpu) throw std::runtime_error("Original graphics API precedes native GPU creation");
                if(address==0x00216870) {finish(cpu,0,gpu->busy()?1:0);return;}
                if(address==0x0021B080) {
                    const auto value=argument(cpu,0);
                    gpu->busy();
                    auto issued=memory.load<std::uint32_t>(context+0x2C);
                    const auto completed=memory.load<std::uint32_t>(memory.load<std::uint32_t>(context+0x30));
                    if(issued-value<issued-completed) {
                        // The SDK inserts the current fence before waiting if
                        // the caller supplied the next, not yet emitted value.
                        if(value==issued) graphics_helper(cpu,0x0021AFD0,0x0021B0AF,std::array{0U});
                        submit_graphics();issued=memory.load<std::uint32_t>(context+0x2C);
                        gpu->wait_fence(value,issued);
                    }
                    finish(cpu,8);return;
                }
                if(address==0x0021AE80) {
                    if(cpu.registers[ecx]!=context) throw std::runtime_error("Unsupported original push-buffer submission context");
                    submit_graphics();finish(cpu,0);return;
                }
                const auto flags=memory.load<std::uint32_t>(context+8);
                const auto cursor=memory.load<std::uint32_t>(context);
                if(flags&4) {
                    const auto buffer=memory.load<std::uint32_t>(context+0x34C),base=memory.load<std::uint32_t>(buffer+4);
                    memory.store<std::uint32_t>(context+0x350,memory.load<std::uint32_t>(context+0x350)+cursor-base);
                    memory.store<std::uint32_t>(context,base);finish(cpu,8,base);return;
                }
                const auto minimum=argument(cpu,0),maximum=argument(cpu,1);
                const auto begin=memory.load<std::uint32_t>(context+0x24),end=memory.load<std::uint32_t>(context+0x28);
                if(minimum>maximum || maximum>end-begin-0x204 || cursor<begin || cursor>end)
                    throw std::runtime_error("Invalid original push-buffer reservation");
                auto next=cursor;
                if(maximum+0x204>end-cursor) {
                    submit_graphics();next=begin;gpu_cursor=begin;memory.store<std::uint32_t>(context,next);
                }
                memory.store<std::uint32_t>(context+4,std::min(end,next+maximum)-0x204);
                finish(cpu,8,next);return;
            } catch(const NativeBoundary&) {throw;}
            catch(const std::exception& error) {throw NativeBoundary(address,error.what());}
        }
        if(address==0x002169A0) {
            const auto index=argument(cpu,0),tile=argument(cpu,1);
            if(!graphics || index>=8) throw NativeBoundary(address,"invalid native graphics tile mapping");
            constexpr std::uint32_t context=0x002256C0;
            const auto cache=context+0x1694+index*24;
            if(!tile || !memory.load<std::uint32_t>(tile+4)) std::memset(memory.access(cache,24),0,24);
            else {
                const auto pointer=memory.load<std::uint32_t>(tile+4),size=memory.load<std::uint32_t>(tile+8);
                const auto pitch=memory.load<std::uint32_t>(tile+12);
                if(!size || !pitch || (pitch&63U) || allocation(pointer)==allocations.end())
                    throw NativeBoundary(address,"graphics tile requires native backing storage and an aligned pitch");
                memory.access(pointer,size);
                if(graphics->stats().draws) throw NativeBoundary(address,"changing a submitted tile requires resource migration");
                // D3D11 owns layout and tiling. Preserve the original SDK's tile
                // descriptor and its visible GetTile state over native storage.
                std::memmove(memory.access(cache,24),memory.access(tile,24),24);
                if(!(memory.load<std::uint32_t>(tile)&0x80000000U)) {
                    memory.store<std::uint32_t>(cache+16,0);memory.store<std::uint32_t>(cache+20,0);
                }
            }
            finish(cpu,8);return;
        }
        if(address>=0x0028CF40 && address<=0x0028D2A4) {input_api(cpu,address);return;}
        if(address!=0x000E1DB7) throw NativeBoundary(address,"native platform API is unbound");
        const bool clean=argument(cpu,0)!=0;
        const auto title=std::format("{:08X}",image.title_id);
        const auto target="\\Device\\Harddisk0\\Partition3\\"+title;
        const auto path=writable_path(target);
        if(!path) throw NativeBoundary(address,"native cache mount has no storage path");
        const auto cache_parent=user_root/"CACHE"/"PARTITION3";
        std::filesystem::create_directories(cache_parent);
        if(clean && std::filesystem::exists(*path)) {
            // Delete only this verified title's cache after checking the exact
            // resolved parent and every entry. Saves live outside this directory.
            if(std::filesystem::weakly_canonical(path->parent_path())!=cache_parent || path->filename()!=title)
                throw NativeBoundary(address,"cache cleanup target left its native title directory");
            unsigned entries=0;
            for(const auto& entry:std::filesystem::recursive_directory_iterator(*path)) {
                if(++entries>4096 || (GetFileAttributesW(entry.path().c_str())&FILE_ATTRIBUTE_REPARSE_POINT))
                    throw NativeBoundary(address,"native cache cleanup exceeds its bound or contains a reparse point");
            }
            std::filesystem::remove_all(*path);
        }
        std::filesystem::create_directories(*path);
        const auto [link,inserted]=links.emplace("\\??\\Z:",target);
        if(!inserted && folded(link->second)!=folded(target)) throw NativeBoundary(address,"utility drive letter belongs to another guest mount");
        finish(cpu,4,1);
    }
    std::uint32_t open_writable(Cpu& cpu,const std::string& name,unsigned options_index,unsigned share_index,std::uint32_t disposition,std::uint32_t& information) {
        const auto path=writable_path(name);
        if(!path) return name_not_found;
        const auto access=argument(cpu,1),options=argument(cpu,options_index),share=argument(cpu,share_index);
        if(soundtrack_key(folded(name))) {
            if(disposition!=1 || (access&(GENERIC_WRITE|GENERIC_ALL|DELETE|FILE_WRITE_DATA|FILE_APPEND_DATA|FILE_WRITE_ATTRIBUTES|WRITE_DAC)) || (options&0x1000))
                return access_denied;
        }
        if(disposition>5 || share>7) return invalid_parameter;
        auto attributes=GetFileAttributesW(path->c_str());
        const bool existed=attributes!=INVALID_FILE_ATTRIBUTES;
        if((options&1) && !existed && (disposition==2 || disposition==3)) {
            if(!CreateDirectoryW(path->c_str(),nullptr)) return file_error(GetLastError());
            attributes=GetFileAttributesW(path->c_str());
        } else if((options&1) && existed && disposition==2) return 0xC0000035;
        const bool directory=attributes!=INVALID_FILE_ATTRIBUTES && (attributes&FILE_ATTRIBUTE_DIRECTORY)!=0;
        if((options&1) && !directory) return existed?0xC0000103:0xC000003A;
        if((options&0x40) && directory) return 0xC00000BA;
        constexpr DWORD creation[]={CREATE_ALWAYS,OPEN_EXISTING,CREATE_NEW,OPEN_ALWAYS,TRUNCATE_EXISTING,CREATE_ALWAYS};
        const auto mode=directory?OPEN_EXISTING:creation[disposition];
        const auto flags=FILE_FLAG_BACKUP_SEMANTICS|((options&0x1000)?FILE_FLAG_DELETE_ON_CLOSE:0U)|
            ((options&0x30)?0U:FILE_FLAG_OVERLAPPED);
        const auto native=CreateFileW(path->c_str(),access,share,nullptr,mode,flags,nullptr);
        if(native==INVALID_HANDLE_VALUE) return file_error(GetLastError());
        auto file=std::make_shared<OpenFile>();file->native=native;file->host_path=*path;
        file->path=name;file->access=access;file->share=share;file->options=options;
        file->entry.attributes=static_cast<std::uint8_t>(directory?0x10:0x20);
        const auto base=allocate(72,page,true,PAGE_READWRITE);
        if(!base) return no_memory;
        const auto address=base|0x80000000U,handle=next_handle;next_handle+=4;
        memory.store<std::uint16_t>(address,5);
        memory.store<std::uint8_t>(address+2,static_cast<std::uint8_t>(((access&(0x80000000U|1U))?2:0)|((access&(0x40000000U|6U))?4:0)|((share&7)<<4)));
        dispatcher(address+0x28,1,16,1);dispatcher(address+0x38,0,16,1);
        const Object object{Object::Kind::file,address,nullptr,file};handles.emplace(handle,object);objects.emplace(address,object);
        memory.store<std::uint32_t>(argument(cpu,0),handle);
        information=!existed?2:disposition==0?0:disposition==4 || disposition==5?3:1;
        return success;
    }
    std::uint32_t convert_string(Cpu& cpu,bool to_unicode) {
        const auto destination=argument(cpu,0),source=argument(cpu,1);
        const auto length=memory.load<std::uint16_t>(source),maximum=memory.load<std::uint16_t>(source+2);
        const auto input_address=memory.load<std::uint32_t>(source+4);
        if(length>maximum || (to_unicode?false:(length&1)!=0)) return invalid_parameter;
        const auto bytes=length?memory.access(input_address,length):nullptr;
        int characters=0;
#ifdef _WIN32
        if(length) characters=to_unicode?MultiByteToWideChar(1252,0,static_cast<const char*>(bytes),length,nullptr,0):
            WideCharToMultiByte(1252,0,static_cast<const wchar_t*>(bytes),length/2,nullptr,0,nullptr,nullptr);
        if(length && !characters) return invalid_parameter;
#else
        characters=to_unicode?length:length/2;
#endif
        const auto unit=to_unicode?2U:1U,required=(static_cast<unsigned>(characters)+1)*unit;
        if(required>UINT16_MAX) return 0xC0000106;
        auto output=memory.load<std::uint32_t>(destination+4);
        if(argument(cpu,2)) {
            output=allocate(required,page,true,PAGE_READWRITE);
            if(!output) return no_memory;
            output|=0x80000000U;
            memory.store<std::uint16_t>(destination+2,static_cast<std::uint16_t>(required));
            memory.store<std::uint32_t>(destination+4,output);
        } else if(memory.load<std::uint16_t>(destination+2)<required) return 0x80000005;
        auto target=memory.access(output,required);
        if(characters) {
#ifdef _WIN32
            const auto written=to_unicode?MultiByteToWideChar(1252,0,static_cast<const char*>(bytes),length,static_cast<wchar_t*>(target),characters):
                WideCharToMultiByte(1252,0,static_cast<const wchar_t*>(bytes),length/2,static_cast<char*>(target),characters,nullptr,nullptr);
            if(written!=characters) return invalid_parameter;
#else
            constexpr std::array<char16_t,32> cp1252{0x20AC,0x81,0x201A,0x192,0x201E,0x2026,0x2020,0x2021,0x2C6,0x2030,0x160,0x2039,0x152,0x8D,0x17D,0x8F,
                0x90,0x2018,0x2019,0x201C,0x201D,0x2022,0x2013,0x2014,0x2DC,0x2122,0x161,0x203A,0x153,0x9D,0x17E,0x178};
            for(unsigned i=0;i<unsigned(characters);++i) {
                if(to_unicode) {
                    const auto value=static_cast<const unsigned char*>(bytes)[i];
                    const auto converted=char16_t(value>=0x80 && value<0xA0?cp1252[value-0x80]:value);
                    std::memcpy(static_cast<std::byte*>(target)+i*2,&converted,2);
                } else {
                    char16_t value;std::memcpy(&value,static_cast<const std::byte*>(bytes)+i*2,2);
                    const auto found=std::ranges::find(cp1252,value);
                    // Report unsupported best-fit mappings instead of accepting
                    // a different filename or overwriting another save.
                    if(value>255 && found==cp1252.end()) return invalid_parameter;
                    static_cast<unsigned char*>(target)[i]=found!=cp1252.end()?static_cast<unsigned char>(0x80+std::distance(cp1252.begin(),found)):static_cast<unsigned char>(value);
                }
            }
#endif
        }
        std::memset(static_cast<std::byte*>(target)+required-unit,0,unit);
        memory.store<std::uint16_t>(destination,static_cast<std::uint16_t>(required-unit));
        return success;
    }
    void io_status(std::uint32_t address,std::uint32_t status,std::uint32_t information=0) {
        memory.store<std::uint32_t>(address,status); memory.store<std::uint32_t>(address+4,information);
    }
    void check_watched_read(Cpu& cpu,std::uint32_t buffer,std::uint32_t size,std::string_view path) {
        // Windows can complete an overlapped read on another native thread,
        // outside the guest thread's hardware watches. Catch that destination
        // before issuing the read, including already-pending reads at watch setup.
        const auto physical=buffer&0x0FFFFFFF;
        for(const auto address:watched_addresses) if(address && (address&0x0FFFFFFF)>=physical &&
            std::uint64_t(address&0x0FFFFFFF)-physical<size) {
            diagnostic_record(std::format("{{\"type\":\"watched_read_overlap\",\"callsite\":{},\"object\":{},\"buffer\":{},\"bytes\":{},\"path\":{}}}",
                json(hex32(cpu.eip)),json(hex32(address)),json(hex32(buffer)),size,json(path.substr(0,1024))));
            throw NativeBoundary(cpu.eip,"file read overlaps a watched semaphore header");
        }
    }
    void trace_io(Cpu& cpu,unsigned ordinal,std::uint32_t status,std::uint32_t information,const std::string& path,
                  std::uint32_t options=0,std::uint32_t requested=0,std::uint64_t offset=0,std::uint32_t buffer=0) {
        if(live && !host.trace_io && (status==success || status==0x103)) return;
        if(io_trace.size()==64) io_trace.pop_front();
        io_trace.push_back({ordinal,status,information,memory.load<std::uint32_t>(cpu.registers[esp]),path,options,requested,offset,buffer});
    }
    bool valid_io(Cpu& cpu) {
        if(argument(cpu,2)) throw NativeBoundary(cpu.eip,"file APC completion requires APC delivery");
        const auto event=argument(cpu,1);
        if(!event) return true;
        const auto found=handles.find(event);
        return found!=handles.end() && found->second.kind==Object::Kind::event;
    }
    void io_complete(Cpu& cpu,std::uint32_t status,std::uint32_t information=0) {
        const auto event=argument(cpu,1);
        if(event) memory.store<std::uint32_t>(handles.at(event).address+4,1);
        io_status(argument(cpu,4),status,information);
    }
    std::uint32_t queue_io(Cpu& cpu,const Object& object,bool write,std::uint64_t offset) {
        auto& file=*object.file;
        const auto output=argument(cpu,5),length=argument(cpu,6),status=argument(cpu,4),event=argument(cpu,1);
        const auto fail=[&](std::uint32_t result) {
            io_complete(cpu,result);trace_io(cpu,write?236:219,result,0,file.path,file.options,length,offset,output);return result;
        };
        if(offset>INT64_MAX) return fail(invalid_parameter);
        if(file.entry.directory() && !file.device) return fail(0xC00000BA);
        if(write && (file.disc_file || file.native==INVALID_HANDLE_VALUE)) return fail(write_protected);
        if(!write && !(file.access&(0x80000000U|1U))) return fail(access_denied);
        const auto sector=file.native!=INVALID_HANDLE_VALUE && !file.disc_file?512U:2048U;
        // Original sample.xsb/b2distfx.bin callers use ordinary heap buffers.
        // Enforce sector transfers without imposing Windows DMA alignment on
        // the guest's pointer; our mounted-image handle is buffered.
        if((file.options&8) && ((offset%sector) || (length%sector))) return fail(invalid_parameter);
        memory.access(status,8);const auto buffer=memory.access(output,length);
        const auto size=file_size(file);
        if(!length) {io_complete(cpu,success);return success;}
        if(!write && offset>=size) return fail(end_of_file);
        const auto count=write?length:static_cast<std::uint32_t>(std::min<std::uint64_t>(length,size-offset));
        if(!write) check_watched_read(cpu,output,count,file.path);
        auto request=std::ranges::find(pending_io,false,&PendingIo::active);
        if(request==pending_io.end()) throw NativeBoundary(cpu.eip,"native asynchronous file queue exceeds 32 requests");
#ifdef _WIN32
        if(!request->overlap.hEvent) {
            request->overlap.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
            if(!request->overlap.hEvent) return fail(no_memory);
        }
        const auto native_event=request->overlap.hEvent;
        request->overlap={};request->overlap.hEvent=native_event;ResetEvent(native_event);
#else
        request->overlap={};
#endif
        request->file=object.file;request->file_object=object.address;
        request->event=event?handles.at(event).address:0;request->status=status;
        request->ordinal=write?236:219;request->callsite=memory.load<std::uint32_t>(cpu.registers[esp]);
        request->offset=offset;request->requested=length;request->buffer=output;request->thread=current->id;
        request->native=file.native!=INVALID_HANDLE_VALUE?file.native:disc_io->native;
        const auto native_offset=offset+(file.native!=INVALID_HANDLE_VALUE || file.device?0:std::uint64_t(file.entry.sector)*2048);
        request->overlap.Offset=static_cast<DWORD>(native_offset);request->overlap.OffsetHigh=static_cast<DWORD>(native_offset>>32);
        io_status(status,0x103);memory.store<std::uint32_t>(object.address+0x38+4,0);
        if(request->event) memory.store<std::uint32_t>(request->event+4,0);
        // Course loads reuse fixed RAM buffers. Retire cached GPU views before
        // Windows starts replacing their bytes, and again at completion.
        if(!write && gpu) gpu->invalidate_memory(output,count);
        auto& file_references=object_references[request->file_object];
        auto* event_references=request->event?&object_references[request->event]:nullptr;
        const auto issued=write?WriteFile(request->native,buffer,count,nullptr,&request->overlap):
            ReadFile(request->native,buffer,count,nullptr,&request->overlap);
        if(!issued && GetLastError()!=ERROR_IO_PENDING) {
            const auto result=file_error(GetLastError());request->file.reset();return fail(result);
        }
        ++file_references;if(event_references) ++*event_references;
        request->active=true;++pending_io_count;
        trace_io(cpu,request->ordinal,0x103,0,file.path,file.options,length,offset,output);
        return 0x103;
    }
    void dispatch_io() {
        if(!pending_io_count) return;
        for(auto& request:pending_io) if(request.active) {
            DWORD transferred=0;
            const auto completed=GetOverlappedResult(request.native,&request.overlap,&transferred,FALSE);
            const auto error=completed?ERROR_SUCCESS:GetLastError();
            if(error==ERROR_IO_INCOMPLETE) continue;
            const auto result=completed?success:file_error(error);
            if(request.ordinal==219 && transferred && gpu) gpu->invalidate_memory(request.buffer,transferred);
            io_status(request.status,result,transferred);
            memory.store<std::uint32_t>(request.file_object+0x38+4,1);
            if(request.event) memory.store<std::uint32_t>(request.event+4,1);
            if(!live || host.trace_io || result!=success) {
                if(io_trace.size()==64) io_trace.pop_front();
                io_trace.push_back({request.ordinal,result,transferred,request.callsite,request.file->path,
                    request.file->options,request.requested,request.offset,request.buffer});
            }
            request.file.reset();request.active=false;--pending_io_count;thread_cleanup_pending=true;
            --object_references[request.file_object];release_object(request.file_object);
            if(request.event) {--object_references[request.event];release_object(request.event);}
        }
    }
    std::uint32_t cancel_io(std::uint32_t handle) {
        const auto file=handles.find(handle);
        if(file==handles.end() || file->second.kind!=Object::Kind::file) return ERROR_INVALID_HANDLE;
        for(auto& request:pending_io) if(request.active && request.file_object==file->second.address) {
            if(!CancelIoEx(request.native,&request.overlap)) {
                const auto error=GetLastError();if(error!=ERROR_NOT_FOUND) return error;
            }
            // Xbox CancelIo waits for its cancelled disc requests. Keep their
            // buffers alive until Windows completes, then publish real status
            // and signal the same guest events as ordinary I/O completion.
            DWORD transferred=0;GetOverlappedResult(request.native,&request.overlap,&transferred,TRUE);
        }
        dispatch_io();return ERROR_SUCCESS;
    }
    std::uint32_t open_file(Cpu& cpu,unsigned options_index,unsigned share_index,std::uint32_t disposition,std::uint32_t& information) {
        const auto name=object_name(argument(cpu,2));
        if(!name) return invalid_parameter;
        const auto relative=disc_path(*name);
        if(!relative) return open_writable(cpu,*name,options_index,share_index,disposition,information);
        if(!disc) return no_media;
        auto entry=disc->lookup(*relative);
        if(!entry) return name_not_found;
        const auto replacement=replacement_file(*relative);
        if(!replacement.empty()) {
            const auto size=std::filesystem::file_size(replacement);
            if(size>UINT32_MAX) return invalid_parameter;entry->size=static_cast<std::uint32_t>(size);
        }
        const auto access=argument(cpu,1),options=argument(cpu,options_index),share=argument(cpu,share_index);
        if(disposition>5 || share>7) return invalid_parameter;
        if(disposition==2) return 0xC0000035; // FILE_CREATE on an existing path.
        if(disposition!=1 && disposition!=3) return write_protected;
        if(access&(0x40000000U|0x00010000U|0x00000116U)) return access_denied;
        if((options&1) && !entry->directory()) return 0xC0000103;
        if((options&0x40) && entry->directory()) return 0xC00000BA;
        if(options&0x1000) return write_protected; // Delete on close.
        for(const auto& [handle,existing]:handles) if(existing.kind==Object::Kind::file && folded(existing.file->path)==folded(*name)) {
            if((access&(0x80000000U|1U)) && !(existing.file->share&1)) return 0xC0000043;
            if((existing.file->access&(0x80000000U|1U)) && !(share&1)) return 0xC0000043;
        }
        const auto base=allocate(72,page,true,PAGE_READWRITE);
        if(!base) return no_memory;
        const auto address=base|0x80000000U,handle=next_handle; next_handle+=4;
        auto file=std::make_shared<OpenFile>();file->entry=*entry;file->path=*name;file->disc_file=true;
        if(host.draw_distance>1) {
            const auto path=folded(*relative);
            if(path.ends_with(".PSW") || path.ends_with(".P2W")) {
                const auto mode=unsigned(path.ends_with(".P2W"));world_streams[mode]=*entry;world_paths[mode]=replacement;
            }
        }
        if(!replacement.empty()) {
            file->host_path=replacement;
            file->native=CreateFileW(replacement.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,
                (options&0x30)?FILE_ATTRIBUTE_NORMAL:FILE_FLAG_OVERLAPPED|FILE_FLAG_RANDOM_ACCESS,nullptr);
            if(file->native==INVALID_HANDLE_VALUE) {const auto error=GetLastError();release_allocation(allocations.find(base));return file_error(error);}
        }
        file->access=access;file->share=share;file->options=options;file->device=relative->empty();
        memory.store<std::uint16_t>(address,5);
        memory.store<std::uint8_t>(address+2,static_cast<std::uint8_t>(((access&(0x80000000U|1U))?2:0)|((share&7)<<4)));
        // Xbox FILE_OBJECT uses four-byte packing: byte offset at +0x14,
        // lock event at +0x28 and completion event at +0x38.
        dispatcher(address+0x28,1,16,1);dispatcher(address+0x38,0,16,1);
        const Object object{Object::Kind::file,address,nullptr,file};
        handles.emplace(handle,object);objects.emplace(address,object);
        memory.store<std::uint32_t>(argument(cpu,0),handle);
        information=1;
        return success;
    }
    std::uint32_t file_attributes(const OpenFile& file) const {
        if(!file.disc_file && file.native!=INVALID_HANDLE_VALUE) return file.entry.directory()?0x10U:0x20U;
        return file.entry.directory()?0x11U:0x21U;
    }
    std::uint64_t file_size(const OpenFile& file) const {
        if(file.native!=INVALID_HANDLE_VALUE && !file.entry.directory()) {
            LARGE_INTEGER size;if(!GetFileSizeEx(file.native,&size)) throw std::runtime_error("Cannot query native file size");
            return static_cast<std::uint64_t>(size.QuadPart);
        }
        return file.device?disc->size():file.entry.directory()?0:file.entry.size;
    }
    std::uint32_t file_information(OpenFile& file,std::uint32_t output,std::uint32_t length,unsigned type,std::uint32_t& written) {
        unsigned required;
        switch(type) {
        case 4:required=36;break;case 5:required=24;break;case 6:required=8;break;case 7:case 8:case 16:case 17:required=4;break;
        case 9:required=4+static_cast<unsigned>(file.path.size());break;case 14:case 19:case 20:required=8;break;case 34:required=52;break;
        default:throw NativeBoundary(current->cpu.eip,std::format("unsupported file information class {}",type));
        }
        if(length<required) return buffer_too_small;
        auto data=static_cast<std::byte*>(memory.access(output,required));std::memset(data,0,required);
        const auto size=file_size(file);
        const bool native_disk=!file.disc_file && file.native!=INVALID_HANDLE_VALUE;
        const auto unit=native_disk?16384ULL:2048ULL,allocated=(size+unit-1)&~(unit-1);
        if(native_disk && (type==4 || type==34)) {
            BY_HANDLE_FILE_INFORMATION native{};
            if(!GetFileInformationByHandle(file.native,&native)) return file_error(GetLastError());
            const FILETIME times[]={native.ftCreationTime,native.ftLastAccessTime,native.ftLastWriteTime,native.ftLastWriteTime};
            for(unsigned i=0;i<4;++i) memory.store<std::uint64_t>(output+i*8,(static_cast<std::uint64_t>(times[i].dwHighDateTime)<<32)|times[i].dwLowDateTime);
        }
        switch(type) {
        case 4:memory.store<std::uint32_t>(output+32,file_attributes(file));break;
        case 5:memory.store<std::uint64_t>(output,allocated);memory.store<std::uint64_t>(output+8,size);
            memory.store<std::uint32_t>(output+16,1);memory.store<std::uint8_t>(output+21,file.entry.directory());break;
        case 6:
            if(native_disk) {
                BY_HANDLE_FILE_INFORMATION native{};
                if(!GetFileInformationByHandle(file.native,&native)) return file_error(GetLastError());
                memory.store<std::uint64_t>(output,(static_cast<std::uint64_t>(native.nFileIndexHigh)<<32)|native.nFileIndexLow);
            } else memory.store<std::uint64_t>(output,file.entry.sector);
            break;
        case 8:memory.store<std::uint32_t>(output,file.access);break;
        case 9:memory.store<std::uint32_t>(output,static_cast<std::uint32_t>(file.path.size()));
            std::memcpy(data+4,file.path.data(),file.path.size());break;
        case 14:memory.store<std::uint64_t>(output,file.position);break;
        case 16:memory.store<std::uint32_t>(output,file.options);break;
        case 17:memory.store<std::uint32_t>(output,native_disk?511:2047);break;
        case 19:memory.store<std::uint64_t>(output,allocated);break;
        case 20:memory.store<std::uint64_t>(output,size);break;
        case 34:memory.store<std::uint64_t>(output+32,allocated);memory.store<std::uint64_t>(output+40,size);
            memory.store<std::uint32_t>(output+48,file_attributes(file));break;
        }
        written=required;return success;
    }
    std::uint32_t disc_control(Cpu& cpu,OpenFile& file,std::uint32_t& written) {
        if(!file.device) return invalid_parameter;
        const auto code=argument(cpu,5),request=argument(cpu,6),input_length=argument(cpu,7),output=argument(cpu,8),output_length=argument(cpu,9);
        if(code==0x4D014) {
            if(input_length<44 || memory.load<std::uint16_t>(request)!=44) return invalid_parameter;
            memory.access(request,44);
            const auto opcode=memory.load<std::uint8_t>(request+28);
            const auto mode=memory.load<std::uint8_t>(request+30)&0x3FU;
            // READ DVD STRUCTURE requests physical optical/security data absent
            // from an XISO. Return the real unsupported status;
            // the original caller decides whether it can continue.
            if(opcode==0xAD) return 0xC00000BB;
            if(opcode!=0x5A || mode!=0x3E) throw NativeBoundary(cpu.eip,std::format("unsupported virtual disc SCSI command 0x{:02X}, page 0x{:02X}",opcode,mode));
            if(memory.load<std::uint8_t>(request+8)!=1) return invalid_parameter;
            const auto capacity=memory.load<std::uint32_t>(request+12),buffer=memory.load<std::uint32_t>(request+20);
            const auto requested=(memory.load<std::uint8_t>(request+35)<<8)|memory.load<std::uint8_t>(request+36);
            const auto count=std::min({capacity,static_cast<unsigned>(requested),28U});
            // A mounted XISO is an already readable virtual Xbox
            // data partition. This reports that provider's state, not a physical
            // drive's authentication transcript or fabricated security sector.
            std::array<std::byte,28> mode_page{};
            mode_page[1]=std::byte{26};mode_page[8]=std::byte{0x3E};mode_page[9]=std::byte{18};
            mode_page[10]=mode_page[11]=mode_page[12]=std::byte{1};mode_page[13]=std::byte{0xD0};
            if(count) std::memcpy(memory.access(buffer,count),mode_page.data(),count);
            memory.store<std::uint8_t>(request+2,0);memory.store<std::uint32_t>(request+12,count);
            if(output_length) {if(output_length<44) return buffer_too_small;std::memmove(memory.access(output,44),memory.access(request,44),44);}
            written=44;return success;
        }
        throw NativeBoundary(cpu.eip,std::format("unsupported virtual disc IOCTL 0x{:08X}",code));
    }
    template<unsigned Ordinal> void service(Cpu& cpu) {
        PerfScope timing(host.performance,PerfPhase::kernel,Ordinal);
        ++service_calls[Ordinal]; clock_data();
        if constexpr(Ordinal==327 || Ordinal==328) {
            const auto pointer=argument(cpu,0);
            const auto section=std::ranges::find(image.sections,pointer,&Section::header_address);
            if(section==image.sections.end()) {finish(cpu,4,invalid_parameter);return;}
            const auto count=memory.load<std::uint32_t>(pointer+24);
            if constexpr(Ordinal==327) {
                if(count==UINT32_MAX) {finish(cpu,4,invalid_parameter);return;}
                if(!count) {
                    image.load_section(*section,std::span(static_cast<std::byte*>(memory.access(section->address,section->size)),section->size));
                    for(const auto reference:{section->head_reference,section->tail_reference}) if(reference)
                        memory.store<std::uint16_t>(reference,memory.load<std::uint16_t>(reference)+1);
                }
                memory.store<std::uint32_t>(pointer+24,count+1);
            } else {
                if(!count) {finish(cpu,4,invalid_parameter);return;}
                memory.store<std::uint32_t>(pointer+24,count-1);
                if(count==1) {
                    std::memset(memory.access(section->address,section->size),0,section->size);
                    for(const auto reference:{section->head_reference,section->tail_reference}) if(reference)
                        memory.store<std::uint16_t>(reference,memory.load<std::uint16_t>(reference)-1);
                }
            }
            finish(cpu,4,success);
        } else if constexpr(Ordinal==335 || Ordinal==336 || Ordinal==337) {
            const auto address=argument(cpu,0);Sha1 sha;
            auto storage=memory.access(address,sizeof(sha));std::memcpy(&sha,storage,sizeof(sha));
            if constexpr(Ordinal==335) sha.reset();
            else if constexpr(Ordinal==336) {
                const auto length=argument(cpu,2);
                if(length) sha.update(Bytes(static_cast<const std::byte*>(memory.access(argument(cpu,1),length)),length));
            } else {
                auto output=memory.access(argument(cpu,1),20);const auto digest=sha.finish();
                std::memcpy(storage,&sha,sizeof(sha));std::memcpy(output,digest.data(),digest.size());
                finish(cpu,8);return;
            }
            std::memcpy(storage,&sha,sizeof(sha));finish(cpu,Ordinal==335?4:12);
        } else if constexpr(Ordinal==340) {
            const auto span=[&](unsigned pointer,unsigned size,std::uint32_t limit=UINT32_MAX) {
                const auto length=std::min(argument(cpu,size),limit);
                return length?Bytes(static_cast<const std::byte*>(memory.access(argument(cpu,pointer),length)),length):Bytes{};
            };
            auto output=memory.access(argument(cpu,6),20);
            const auto digest=xbox_hmac(span(0,1,64),span(2,3),span(4,5));
            std::memcpy(output,digest.data(),digest.size());finish(cpu,28);
        } else if constexpr(Ordinal==260 || Ordinal==308) {
            finish(cpu,12,convert_string(cpu,Ordinal==260));
        } else if constexpr(Ordinal==190 || Ordinal==202) {
            std::uint32_t information=0;
            const auto result=open_file(cpu,Ordinal==190?8:5,Ordinal==190?6:4,Ordinal==190?argument(cpu,7):1U,information);
            trace_io(cpu,Ordinal,result,information,object_name(argument(cpu,2)).value_or("<invalid object attributes>"));
            io_status(argument(cpu,3),result,information);finish(cpu,Ordinal==190?36:24,result);
        } else if constexpr(Ordinal==219 || Ordinal==236 || Ordinal==196 || Ordinal==207) {
            constexpr unsigned cleanup=Ordinal==219 || Ordinal==236?32:40;
            if(!valid_io(cpu)) {io_status(argument(cpu,4),invalid_handle);finish(cpu,cleanup,invalid_handle);return;}
            const auto found=handles.find(argument(cpu,0));
            if(found==handles.end() || found->second.kind!=Object::Kind::file) {io_complete(cpu,invalid_handle);finish(cpu,cleanup,invalid_handle);return;}
            auto& file=*found->second.file;
            std::uint32_t result=success,written=0,trace_buffer=0,trace_requested=0;
            std::uint64_t trace_offset=0;
            if constexpr(Ordinal==219 || Ordinal==236) {
                const auto output=argument(cpu,5),length=argument(cpu,6),offset_pointer=argument(cpu,7);
                auto offset=offset_pointer?memory.load<std::uint64_t>(offset_pointer):UINT64_MAX-1;
                if(offset==UINT64_MAX-1) offset=file.position;
                trace_buffer=output;trace_requested=length;trace_offset=offset;
                if constexpr(Ordinal==236) if(file.disc_file) {
                    io_complete(cpu,write_protected,0);finish(cpu,cleanup,write_protected);return;
                }
                if(!(file.options&0x30)) {
                    if constexpr(Ordinal==236) if(offset==UINT64_MAX) offset=file_size(file);
                    finish(cpu,cleanup,queue_io(cpu,found->second,Ordinal==236,offset));return;
                }
                if(file.native!=INVALID_HANDLE_VALUE) {
                    if constexpr(Ordinal==236) if(offset==UINT64_MAX) offset=file_size(file);
                    if(offset>INT64_MAX) result=invalid_parameter;
                    else if(file.entry.directory()) result=0xC00000BA;
                    else {
                        const LARGE_INTEGER position{.QuadPart=static_cast<LONGLONG>(offset)};
                        if(!SetFilePointerEx(file.native,position,nullptr,FILE_BEGIN)) result=file_error(GetLastError());
                        else {
                            const auto buffer=memory.access(output,length);
                            if constexpr(Ordinal==219) if(watched_addresses!=std::array<std::uint32_t,4>{}) {
                                const auto size=file_size(file);
                                check_watched_read(cpu,output,offset<size?static_cast<std::uint32_t>(std::min<std::uint64_t>(length,size-offset)):0,file.path);
                            }
                            BOOL completed;DWORD native_written=0;
                            if constexpr(Ordinal==219) if(gpu) gpu->invalidate_memory(output,length);
                            if constexpr(Ordinal==219) completed=ReadFile(file.native,buffer,length,&native_written,nullptr);
                            else completed=WriteFile(file.native,buffer,length,&native_written,nullptr);
                            written=native_written;
                            if(!completed) result=file_error(GetLastError());
                            else if constexpr(Ordinal==219) {if(!written && length) result=end_of_file;}
                            if(completed) {file.position=offset+written;memory.store<std::uint64_t>(found->second.address+0x14,file.position);}
                        }
                    }
                } else if constexpr(Ordinal==236) result=write_protected;
                else if(offset>INT64_MAX) result=invalid_parameter;
                else if(!(file.access&(0x80000000U|1U))) result=access_denied;
                else if(file.entry.directory() && !file.device) result=0xC00000BA;
                else if(length) {
                    const auto size=file_size(file);
                    if(offset>=size) result=end_of_file;
                    else {
                        written=static_cast<std::uint32_t>(std::min<std::uint64_t>(length,size-offset));
                        check_watched_read(cpu,output,written,file.path);
                        const std::span destination(static_cast<std::byte*>(memory.access(output,written)),written);
                        if(gpu) gpu->invalidate_memory(output,written);
                        if(file.device) disc->read(offset,destination);else disc->read(file.entry,offset,destination);
                        file.position=offset+written;memory.store<std::uint64_t>(found->second.address+0x14,file.position);
                    }
                }
            } else if constexpr(Ordinal==196) result=disc_control(cpu,file,written);
            else {
                const auto output=argument(cpu,5),length=argument(cpu,6),type=argument(cpu,7),pattern=argument(cpu,8);
                if(!file.entry.directory()) result=0xC0000103;
                else if(type!=1 && type!=12) throw NativeBoundary(cpu.eip,std::format("unsupported directory information class {}",type));
                else {
                    if(argument(cpu,9) || !file.listing_loaded) {
                        file.pattern=pattern?counted_string(pattern):"*";
                        if(file.native!=INVALID_HANDLE_VALUE) {
                            file.listing.clear();
                            for(const auto& entry:std::filesystem::directory_iterator(file.host_path)) {
                                if(file.listing.size()>=4096 || entry.is_symlink()) throw NativeBoundary(cpu.eip,"native directory exceeds its bound or contains a link");
                                const auto size=entry.is_directory()?0:entry.file_size();
                                if(size>UINT32_MAX) throw NativeBoundary(cpu.eip,"native directory contains a file larger than Xbox supports");
                                file.listing.push_back({utf8(entry.path().filename().native()),0,static_cast<std::uint32_t>(size),static_cast<std::uint8_t>(entry.is_directory()?0x10:0x20)});
                            }
                            std::ranges::sort(file.listing,[](const auto& a,const auto& b) {return folded(a.name)<folded(b.name);});
                        } else {
                            const auto relative=disc_path(file.path);
                            if(!relative) throw NativeBoundary(cpu.eip,"directory left its mounted disc");
                            file.listing=disc->list(*relative);
                            if(mods || imported.selected) for(auto& entry:file.listing) if(!entry.directory()) {
                                const auto replacement=replacement_file(relative->empty()?entry.name:*relative+"/"+entry.name);
                                if(!replacement.empty()) entry.size=static_cast<std::uint32_t>(std::filesystem::file_size(replacement));
                            }
                        }
                        file.listing_position=0;file.listing_loaded=true;
                    }
                    unsigned previous=0;bool any=false;
                    while(file.listing_position<file.listing.size()) {
                        const auto& entry=file.listing[file.listing_position];
                        if(!wildcard(file.pattern,entry.name)) {++file.listing_position;continue;}
                        const auto header=type==1?64U:12U;
                        const auto name_length=static_cast<unsigned>(entry.name.size()),required=align_up(header+name_length,8);
                        if(required>length-written) {if(!any) result=0x80000005;break;}
                        const auto destination=output+written;
                        std::memset(memory.access(destination,required),0,required);
                        memory.store<std::uint32_t>(destination+4,static_cast<std::uint32_t>(file.listing_position));
                        if(type==1) {
                            memory.store<std::uint64_t>(destination+40,entry.directory()?0:entry.size);
                            const auto unit=file.native!=INVALID_HANDLE_VALUE?16384ULL:2048ULL;
                            memory.store<std::uint64_t>(destination+48,entry.directory()?0:(static_cast<std::uint64_t>(entry.size)+unit-1)&~(unit-1));
                            memory.store<std::uint32_t>(destination+56,(entry.directory()?0x10U:0x20U)|(file.native==INVALID_HANDLE_VALUE?1U:0U));
                            memory.store<std::uint32_t>(destination+60,name_length);
                        } else memory.store<std::uint32_t>(destination+8,name_length);
                        std::memcpy(memory.access(destination+header,name_length),entry.name.data(),name_length);
                        if(any) memory.store<std::uint32_t>(output+previous,written-previous);
                        previous=written;written+=required;any=true;++file.listing_position;
                    }
                    if(!any && result==success) result=0x80000006;
                }
            }
            memory.store<std::uint32_t>(found->second.address+0x10,result);
            trace_io(cpu,Ordinal,result,written,file.path,file.options,trace_requested,trace_offset,trace_buffer);
            io_complete(cpu,result,written);finish(cpu,cleanup,result);
        } else if constexpr(Ordinal==211 || Ordinal==218 || Ordinal==226 || Ordinal==198) {
            const auto found=handles.find(argument(cpu,0));
            constexpr unsigned cleanup=Ordinal==198?8:20;
            if(found==handles.end() || found->second.kind!=Object::Kind::file) {io_status(argument(cpu,1),invalid_handle);finish(cpu,cleanup,invalid_handle);return;}
            auto& file=*found->second.file;
            std::uint32_t result=success,written=0;
            if constexpr(Ordinal==211) result=file_information(file,argument(cpu,2),argument(cpu,3),argument(cpu,4),written);
            else if constexpr(Ordinal==226) {
                const auto type=argument(cpu,4);
                if(type==14) {
                    if(argument(cpu,3)<8) result=buffer_too_small;
                    else {
                        const auto offset=memory.load<std::uint64_t>(argument(cpu,2));
                        if(offset>INT64_MAX) result=invalid_parameter;
                        else {file.position=offset;memory.store<std::uint64_t>(found->second.address+0x14,offset);}
                    }
                } else if(file.disc_file || file.native==INVALID_HANDLE_VALUE) result=write_protected;
                else if(type==19 || type==20) {
                    if(argument(cpu,3)<8) result=buffer_too_small;
                    else {
                        const auto size=memory.load<std::uint64_t>(argument(cpu,2));
                        if(size>INT64_MAX) result=invalid_parameter;
                        else if(type==19) {
                            FILE_ALLOCATION_INFO information{};information.AllocationSize.QuadPart=static_cast<LONGLONG>(size);
                            if(!SetFileInformationByHandle(file.native,FileAllocationInfo,&information,sizeof(information))) result=file_error(GetLastError());
                        } else {
                            FILE_END_OF_FILE_INFO information{};information.EndOfFile.QuadPart=static_cast<LONGLONG>(size);
                            if(!SetFileInformationByHandle(file.native,FileEndOfFileInfo,&information,sizeof(information))) result=file_error(GetLastError());
                        }
                    }
                } else if(type==13) {
                    if(argument(cpu,3)<1) result=buffer_too_small;
                    else {
                        FILE_DISPOSITION_INFO information{memory.load<std::uint8_t>(argument(cpu,2))!=0};
                        if(!SetFileInformationByHandle(file.native,FileDispositionInfo,&information,sizeof(information))) result=file_error(GetLastError());
                    }
                } else if(type==4) {
                    if(argument(cpu,3)<36) result=buffer_too_small;
                    else {
                        FILE_BASIC_INFO information{};const auto request=argument(cpu,2);
                        information.CreationTime.QuadPart=std::bit_cast<std::int64_t>(memory.load<std::uint64_t>(request));
                        information.LastAccessTime.QuadPart=std::bit_cast<std::int64_t>(memory.load<std::uint64_t>(request+8));
                        information.LastWriteTime.QuadPart=std::bit_cast<std::int64_t>(memory.load<std::uint64_t>(request+16));
                        information.ChangeTime.QuadPart=std::bit_cast<std::int64_t>(memory.load<std::uint64_t>(request+24));
                        information.FileAttributes=memory.load<std::uint32_t>(request+32);
                        if(!SetFileInformationByHandle(file.native,FileBasicInfo,&information,sizeof(information))) result=file_error(GetLastError());
                    }
                }
                else throw NativeBoundary(cpu.eip,std::format("unsupported set-file information class {}",type));
            } else if constexpr(Ordinal==218) {
                const auto output=argument(cpu,2),length=argument(cpu,3),type=argument(cpu,4);
                const bool native_disk=!file.disc_file && file.native!=INVALID_HANDLE_VALUE;
                if(type==3) {
                    if(length<24) result=buffer_too_small;
                    else {
                        if(native_disk) {
                            ULARGE_INTEGER free{},total{};
                            if(!GetDiskFreeSpaceExW(user_root.c_str(),&free,&total,nullptr)) result=file_error(GetLastError());
                            else {
                                const auto capacity=std::min<std::uint64_t>(0x131F00000,total.QuadPart);
                                memory.store<std::uint64_t>(output,capacity/16384);memory.store<std::uint64_t>(output+8,std::min(capacity,free.QuadPart)/16384);
                                memory.store<std::uint32_t>(output+16,32);memory.store<std::uint32_t>(output+20,512);written=24;
                            }
                        } else {
                            memory.store<std::uint64_t>(output,disc->size()/2048);memory.store<std::uint64_t>(output+8,0);
                            memory.store<std::uint32_t>(output+16,1);memory.store<std::uint32_t>(output+20,2048);written=24;
                        }
                    }
                } else if(type==4) {
                    if(length<8) result=buffer_too_small;
                    else {memory.store<std::uint32_t>(output,native_disk?7:2);memory.store<std::uint32_t>(output+4,native_disk?0:0x24);written=8;}
                } else if(type==5) {
                    const std::string_view name=native_disk?"FATX":"XDVDFS";
                    if(length<12+name.size()) result=buffer_too_small;
                    else {
                        memory.store<std::uint32_t>(output,native_disk?2:0x00080000);memory.store<std::uint32_t>(output+4,native_disk?42:255);
                        memory.store<std::uint32_t>(output+8,static_cast<std::uint32_t>(name.size()));
                        std::memcpy(memory.access(output+12,name.size()),name.data(),name.size());written=12+static_cast<unsigned>(name.size());
                    }
                } else throw NativeBoundary(cpu.eip,std::format("unsupported volume information class {}",type));
            } else if constexpr(Ordinal==198) {if(!file.disc_file && file.native!=INVALID_HANDLE_VALUE && !FlushFileBuffers(file.native)) result=file_error(GetLastError());}
            io_status(argument(cpu,1),result,written);finish(cpu,cleanup,result);
        } else if constexpr(Ordinal==210) {
            const auto name=object_name(argument(cpu,0));const auto relative=name?disc_path(*name):std::nullopt;
            if(name && !relative) {
                const auto path=writable_path(*name);
                if(!path) {finish(cpu,8,name_not_found);return;}
                WIN32_FILE_ATTRIBUTE_DATA data{};
                if(!GetFileAttributesExW(path->c_str(),GetFileExInfoStandard,&data)) {finish(cpu,8,file_error(GetLastError()));return;}
                const auto output=argument(cpu,1);
                const FILETIME times[]={data.ftCreationTime,data.ftLastAccessTime,data.ftLastWriteTime,data.ftLastWriteTime};
                for(unsigned i=0;i<4;++i) memory.store<std::uint64_t>(output+i*8,(static_cast<std::uint64_t>(times[i].dwHighDateTime)<<32)|times[i].dwLowDateTime);
                const auto size=(static_cast<std::uint64_t>(data.nFileSizeHigh)<<32)|data.nFileSizeLow;
                memory.store<std::uint64_t>(output+32,(size+16383)&~16383ULL);memory.store<std::uint64_t>(output+40,size);
                memory.store<std::uint32_t>(output+48,data.dwFileAttributes);finish(cpu,8,success);return;
            }
            auto entry=disc && relative?disc->lookup(*relative):std::nullopt;
            if(!entry) {finish(cpu,8,disc?name_not_found:no_media);return;}
            const auto replacement=replacement_file(*relative);
            if(!replacement.empty()) entry->size=static_cast<std::uint32_t>(std::filesystem::file_size(replacement));
            OpenFile file;file.entry=*entry;file.path=*name;file.disc_file=true;
            std::uint32_t written=0;finish(cpu,8,file_information(file,argument(cpu,1),52,34,written));
        } else if constexpr(Ordinal==67 || Ordinal==69) {
            const auto name=folded(counted_string(argument(cpu,0)));
            if(name.empty() || !name.starts_with('\\')) {finish(cpu,Ordinal==67?8:4,invalid_parameter);return;}
            if constexpr(Ordinal==69) finish(cpu,4,links.erase(name)?success:name_not_found);
            else {
                const auto target=counted_string(argument(cpu,1));
                if(target.empty() || !target.starts_with('\\')) {finish(cpu,8,invalid_parameter);return;}
                const auto [entry,inserted]=links.emplace(name,target);finish(cpu,8,inserted?success:0xC0000035U);
            }
        } else if constexpr(Ordinal==203) {
            const auto name=object_name(argument(cpu,1),false);
            if(!name) {finish(cpu,8,invalid_parameter);return;}
            const auto link=links.find(folded(*name));
            if(link==links.end()) {finish(cpu,8,name_not_found);return;}
            const auto base=allocate(16,page,true,PAGE_READWRITE);
            if(!base) {finish(cpu,8,no_memory);return;}
            const auto address=base|0x80000000U,handle=next_handle;next_handle+=4;
            auto data=std::make_shared<OpenFile>();data->link=link->second;
            const Object object{Object::Kind::link,address,nullptr,data};handles.emplace(handle,object);objects.emplace(address,object);
            memory.store<std::uint32_t>(argument(cpu,0),handle);finish(cpu,8,success);
        } else if constexpr(Ordinal==215) {
            const auto found=handles.find(argument(cpu,0));
            if(found==handles.end() || found->second.kind!=Object::Kind::link) {finish(cpu,12,invalid_handle);return;}
            const auto& target=found->second.file->link;
            const auto output=argument(cpu,1),returned=argument(cpu,2);
            const auto length=static_cast<std::uint16_t>(target.size());
            if(returned) memory.store<std::uint32_t>(returned,length);
            if(memory.load<std::uint16_t>(output+2)<length) {finish(cpu,12,buffer_too_small);return;}
            std::memcpy(memory.access(memory.load<std::uint32_t>(output+4),length),target.data(),length);
            memory.store<std::uint16_t>(output,length);finish(cpu,12,success);
        } else if constexpr(Ordinal==252 || Ordinal==253) {
            // The original menu queries its Ethernet cable before starting any
            // network stack. Read the native interfaces; never initialize MCPX.
            if constexpr(Ordinal==253) if(argument(cpu,1)) throw NativeBoundary(cpu.eip,"unsupported Ethernet initialization parameter");
            if(!ethernet_initialized || argument(cpu,0)) {
                ethernet_state=ethernet_status();
                ethernet_initialized=true;
            }
            finish(cpu,Ordinal==253?8:4,Ordinal==253?success:ethernet_state);
        } else if constexpr(Ordinal==255) {
            const auto output=argument(cpu,0),extension=argument(cpu,1),tls=argument(cpu,3),identifier=argument(cpu,4);
            if(!output || extension || argument(cpu,8)) throw NativeBoundary(cpu.eip,"thread extension or debugger-thread contract is unsupported");
            auto& thread=create(argument(cpu,5),argument(cpu,6),argument(cpu,9),argument(cpu,2),tls,argument(cpu,7)!=0);
            const auto handle=next_handle; next_handle+=4;
            handles.emplace(handle,Object{Object::Kind::thread,thread.object,&thread});
            memory.store<std::uint32_t>(output,handle);
            if(identifier) memory.store<std::uint32_t>(identifier,thread.id);
            finish(cpu,40,success);
        } else if constexpr(Ordinal==258) throw ThreadExit{argument(cpu,0)};
        else if constexpr(Ordinal==187) {
            const auto found=handles.find(argument(cpu,0));
            const auto result=found==handles.end()?invalid_handle:success;
            if(found!=handles.end()) {const auto address=found->second.address; handles.erase(found); release_object(address);}
            finish(cpu,4,result);
        } else if constexpr(Ordinal==184) {
            const auto output=argument(cpu,0),size_pointer=argument(cpu,2),flags=argument(cpu,3),protect=argument(cpu,4);
            const auto base=memory.load<std::uint32_t>(output),size=memory.load<std::uint32_t>(size_pointer);
            if(!size || argument(cpu,1) || (flags&~(MEM_RESERVE|MEM_COMMIT|MEM_TOP_DOWN|memory_nozero)) || !(flags&(MEM_RESERVE|MEM_COMMIT))) {
                finish(cpu,20,invalid_parameter); return;
            }
            if(base && !(flags&MEM_RESERVE)) {
                const auto found=allocation(base);
                if(found==allocations.end() || base-found->first+static_cast<std::uint64_t>(size)>found->second.size) {
                    finish(cpu,20,conflicting_addresses); return;
                }
                const auto first=(base-found->first)/page,last=align_up(base-found->first+size,page)/page;
                if(!(flags&memory_nozero)) for(auto index=first;index<last;++index)
                    if(!found->second.pages[index]) std::memset(memory.access(found->first+index*page,page),0,page);
                std::fill(found->second.pages.begin()+first,found->second.pages.begin()+last,protect);
                memory.store<std::uint32_t>(output,base&~(page-1));
                memory.store<std::uint32_t>(size_pointer,(last-first)*page); finish(cpu,20,success); return;
            }
            if(flags&MEM_TOP_DOWN) throw NativeBoundary(cpu.eip,"top-down guest reservation is unsupported");
            const auto allocated=allocate(size,page,(flags&MEM_COMMIT)!=0,protect,base,0,UINT32_MAX,!(flags&memory_nozero));
            if(allocated) { memory.store<std::uint32_t>(output,allocated); memory.store<std::uint32_t>(size_pointer,align_up(size,page)); }
            finish(cpu,20,allocated?success:(base?conflicting_addresses:no_memory));
        } else if constexpr(Ordinal==199) {
            const auto pointer=argument(cpu,0),size_pointer=argument(cpu,1),flags=argument(cpu,2);
            const auto base=memory.load<std::uint32_t>(pointer),size=memory.load<std::uint32_t>(size_pointer);
            const auto found=allocation(base);
            if(found==allocations.end() || (flags!=MEM_RELEASE && flags!=MEM_DECOMMIT) ||
                (flags==MEM_RELEASE && (size || base!=found->first))) { finish(cpu,12,invalid_parameter); return; }
            if(flags==MEM_RELEASE) { release_allocation(found);memory.store<std::uint32_t>(pointer,0);memory.store<std::uint32_t>(size_pointer,0); }
            else {
                const auto first=(base-found->first)/page;
                const auto last=size?align_up(base-found->first+size,page)/page:found->second.size/page;
                if(last>found->second.pages.size()) { finish(cpu,12,invalid_parameter); return; }
                if(gpu) gpu->invalidate_memory(found->first+first*page,(last-first)*page);
                std::fill(found->second.pages.begin()+first,found->second.pages.begin()+last,0);
                std::memset(memory.access(found->first+first*page,(last-first)*page),0,(last-first)*page);
                memory.store<std::uint32_t>(pointer,found->first+first*page); memory.store<std::uint32_t>(size_pointer,(last-first)*page);
            }
            finish(cpu,12,success);
        } else if constexpr(Ordinal==165 || Ordinal==166 || Ordinal==15) {
            const auto size=argument(cpu,0);
            const auto alignment=Ordinal==166?argument(cpu,3):page;
            const auto lowest=Ordinal==166?argument(cpu,1):0U,highest=Ordinal==166?argument(cpu,2):UINT32_MAX;
            const auto protect=Ordinal==166?argument(cpu,4):PAGE_READWRITE;
            const auto allocated=allocate(size,alignment?alignment:page,true,protect,0,lowest,highest,true,Ordinal!=15);
            finish(cpu,Ordinal==166?20:Ordinal==15?8:4,allocated?allocated|0x80000000U:0U);
        } else if constexpr(Ordinal==171 || Ordinal==17) {
            const auto found=allocation(argument(cpu,0));
            if(found==allocations.end()) throw NativeBoundary(cpu.eip,"free of an unknown native guest allocation");
            release_allocation(found);finish(cpu,4);
        } else if constexpr(Ordinal==180 || Ordinal==23 || Ordinal==179) {
            const auto found=allocation(argument(cpu,0));
            finish(cpu,4,found==allocations.end()?0:Ordinal==179?found->second.pages[((argument(cpu,0)&0x0FFFFFFF)-found->first)/page]:found->second.size);
        } else if constexpr(Ordinal==182) {
            const auto found=allocation(argument(cpu,0));
            if(found==allocations.end() || argument(cpu,1)>found->second.size) throw NativeBoundary(cpu.eip,"protection change outside a guest allocation");
            const auto first=((argument(cpu,0)&0x0FFFFFFF)-found->first)/page;
            const auto last=align_up((argument(cpu,0)&0x0FFFFFFF)-found->first+argument(cpu,1),page)/page;
            if(last>found->second.pages.size()) throw NativeBoundary(cpu.eip,"protection change crosses a guest allocation");
            std::fill(found->second.pages.begin()+first,found->second.pages.begin()+last,argument(cpu,2)); finish(cpu,12);
        } else if constexpr(Ordinal==173) finish(cpu,4,argument(cpu,0)&0x0FFFFFFFU);
        else if constexpr(Ordinal==217) {
            const auto base=argument(cpu,0),output=argument(cpu,1);
            const auto found=allocation(base);
            if(found==allocations.end()) { finish(cpu,8,invalid_parameter); return; }
            const auto index=(base-found->first)/page,protect=found->second.pages[index];
            auto first=index,last=index+1;
            while(first && found->second.pages[first-1]==protect) --first;
            while(last<found->second.pages.size() && found->second.pages[last]==protect) ++last;
            const std::uint32_t information[]={found->first+first*page,found->first,found->second.protect,(last-first)*page,
                static_cast<std::uint32_t>(protect?MEM_COMMIT:MEM_RESERVE),protect,MEM_PRIVATE};
            std::memcpy(memory.access(output,sizeof(information)),information,sizeof(information)); finish(cpu,8,success);
        } else if constexpr(Ordinal==291) {
            const auto address=argument(cpu,0);
            memory.fill_elements<std::uint32_t>(address,0,7,false);
            memory.store<std::uint32_t>(address,0x00040001);
            memory.store<std::uint32_t>(address+8,address+8); memory.store<std::uint32_t>(address+12,address+8);
            memory.store<std::uint32_t>(address+16,UINT32_MAX); finish(cpu,4);
        } else if constexpr(Ordinal==277 || Ordinal==306) {
            const auto address=argument(cpu,0),owner=memory.load<std::uint32_t>(address+24);
            if constexpr(Ordinal==306) { if(owner && owner!=current->object) { finish(cpu,4,0); return; } }
            memory.store<std::uint32_t>(address+16,memory.load<std::uint32_t>(address+16)+1);
            while(const auto waiting=memory.load<std::uint32_t>(address+24)) {
                if(waiting==current->object) break;
                yield(ThreadState::waiting,address);
            }
            memory.store<std::uint32_t>(address+24,current->object);
            memory.store<std::uint32_t>(address+20,memory.load<std::uint32_t>(address+20)+1);
            finish(cpu,4,Ordinal==306?std::optional<std::uint64_t>{1}:std::nullopt);
        } else if constexpr(Ordinal==294) {
            const auto address=argument(cpu,0),recursion=memory.load<std::uint32_t>(address+20);
            if(!recursion || memory.load<std::uint32_t>(address+24)!=current->object)
                throw NativeBoundary(cpu.eip,"critical section released without ownership");
            memory.store<std::uint32_t>(address+20,recursion-1);
            memory.store<std::uint32_t>(address+16,memory.load<std::uint32_t>(address+16)-1);
            if(recursion==1) memory.store<std::uint32_t>(address+24,0);
            finish(cpu,4);
        } else if constexpr(Ordinal==301) {
            finish(cpu,4,dos_error(argument(cpu,0)));
        } else if constexpr(Ordinal==103) finish(cpu,0,memory.load<std::uint8_t>(cpu.fs_base+0x24));
        else if constexpr(Ordinal==104) finish(cpu,0,current->object);
        else if constexpr(Ordinal==129 || Ordinal==160 || Ordinal==161) {
            const auto prior=memory.load<std::uint8_t>(cpu.fs_base+0x24);
            memory.store<std::uint8_t>(cpu.fs_base+0x24,static_cast<std::uint8_t>(Ordinal==129?2:cpu.registers[ecx]));
            finish(cpu,0,Ordinal==161?std::nullopt:std::optional<std::uint64_t>{prior});
        } else if constexpr(Ordinal==126 || Ordinal==127) {
            // Kernel performance time uses the 3.375 MHz ACPI clock; the
            // original XAPI/RDTSC timebase separately uses the CPU clock.
            finish(cpu,0,Ordinal==126?clock_ticks(3375000):3375000,true);
        } else if constexpr(Ordinal==128) {
            FILETIME value; GetSystemTimeAsFileTime(&value);
            memory.store<std::uint64_t>(argument(cpu,0),(static_cast<std::uint64_t>(value.dwHighDateTime)<<32)|value.dwLowDateTime);
            finish(cpu,4);
        } else if constexpr(Ordinal==143) {
            const auto target=argument(cpu,0);
            const auto increment=static_cast<std::int32_t>(argument(cpu,1));
            const auto previous=memory.load<std::uint8_t>(target+0x70);
            memory.store<std::uint8_t>(target+0x70,static_cast<std::uint8_t>(std::clamp(8+increment,1,15)));
            finish(cpu,8,static_cast<std::uint32_t>(previous)-8);
        } else if constexpr(Ordinal==99) {
            const auto interval=std::bit_cast<std::int64_t>(memory.load<std::uint64_t>(argument(cpu,2)));
            if(argument(cpu,1)) throw NativeBoundary(cpu.eip,"alertable delays require APC delivery");
            current->wait_kind=WaitKind::delay; yield(ThreadState::waiting,0,due_time(interval)); finish(cpu,12,success);
        } else if constexpr(Ordinal==238) { yield(); finish(cpu,0,success); }
        else if constexpr(Ordinal==107) {
            const auto address=argument(cpu,0);
            memory.fill_elements<std::uint32_t>(address,0,7,false);
            memory.store<std::uint16_t>(address,19);
            memory.store<std::uint32_t>(address+4,address+4); memory.store<std::uint32_t>(address+8,address+4);
            memory.store<std::uint32_t>(address+12,argument(cpu,1)); memory.store<std::uint32_t>(address+16,argument(cpu,2));
            finish(cpu,12);
        } else if constexpr(Ordinal==108 || Ordinal==112 || Ordinal==113) {
            const auto address=argument(cpu,0),value=argument(cpu,1);
            if constexpr(Ordinal==108) {
                if(value>1) throw NativeBoundary(cpu.eip,"invalid event type");
                dispatcher(address,value,16,argument(cpu,2)!=0); finish(cpu,12);
            } else if constexpr(Ordinal==112) {
                const auto limit=argument(cpu,2);
                if(static_cast<std::int32_t>(value)<0 || value>limit || static_cast<std::int32_t>(limit)<=0)
                    throw NativeBoundary(cpu.eip,"invalid semaphore initialization");
                dispatcher(address,5,20,value); memory.store<std::uint32_t>(address+16,limit); finish(cpu,12);
            } else {
                if(value>1) throw NativeBoundary(cpu.eip,"invalid timer type");
                timers.erase(address); dispatcher(address,8+value,40,0);
                memory.store<std::uint32_t>(address+24,address+24); memory.store<std::uint32_t>(address+28,address+24); finish(cpu,8);
            }
        } else if constexpr(Ordinal==119) finish(cpu,12,queue_dpc(argument(cpu,0),argument(cpu,1),argument(cpu,2)));
        else if constexpr(Ordinal==137) {
            const auto address=argument(cpu,0);
            const bool queued=memory.load<std::uint8_t>(address+2)!=0;
            std::erase(dpcs,address); memory.store<std::uint8_t>(address+2,0); finish(cpu,4,queued);
        } else if constexpr(Ordinal==149 || Ordinal==150) {
            const auto address=argument(cpu,0),period=Ordinal==150?argument(cpu,3):0U;
            const auto dpc=argument(cpu,Ordinal==150?4:3);
            const auto raw=static_cast<std::uint64_t>(argument(cpu,1))|(static_cast<std::uint64_t>(argument(cpu,2))<<32);
            if(static_cast<std::int32_t>(period)<0) throw NativeBoundary(cpu.eip,"negative timer period");
            const auto due=due_time(std::bit_cast<std::int64_t>(raw)); const bool replaced=timers.contains(address);
            timers.insert_or_assign(address,Timer{due,period,dpc});
            memory.store<std::uint32_t>(address+4,0); memory.store<std::uint8_t>(address+3,1);
            memory.store<std::uint8_t>(address+1,static_cast<std::int64_t>(raw)>0);
            const auto relative=std::chrono::duration_cast<std::chrono::nanoseconds>(due-started).count()/100;
            memory.store<std::uint64_t>(address+16,static_cast<std::uint64_t>(relative));
            memory.store<std::uint32_t>(address+32,dpc); memory.store<std::uint32_t>(address+36,period);
            finish(cpu,Ordinal==150?20:16,replaced);
        } else if constexpr(Ordinal==97) {
            const auto address=argument(cpu,0); const bool cancelled=timers.erase(address)!=0;
            memory.store<std::uint8_t>(address+3,0); finish(cpu,4,cancelled);
        } else if constexpr(Ordinal==145) {
            const auto address=argument(cpu,0),prior=memory.load<std::uint32_t>(address+4);
            memory.store<std::uint32_t>(address+4,1); finish(cpu,12,prior);
        } else if constexpr(Ordinal==189 || Ordinal==193) {
            const auto attributes=argument(cpu,1),initial=argument(cpu,Ordinal==189?3:2);
            if(attributes && memory.load<std::uint32_t>(attributes+4)) throw NativeBoundary(cpu.eip,"named dispatcher objects require namespace support");
            if constexpr(Ordinal==189) {if(argument(cpu,2)>1) {finish(cpu,16,invalid_parameter);return;}}
            else if(static_cast<std::int32_t>(initial)<0 || static_cast<std::int32_t>(argument(cpu,3))<=0 || initial>argument(cpu,3)) {
                finish(cpu,16,invalid_parameter); return;
            }
            const auto base=allocate(20,page,true,PAGE_READWRITE);
            if(!base) {finish(cpu,16,no_memory);return;}
            const auto address=base|0x80000000U,handle=next_handle; next_handle+=4;
            if constexpr(Ordinal==189) dispatcher(address,argument(cpu,2),16,initial!=0);
            else {dispatcher(address,5,20,initial);memory.store<std::uint32_t>(address+16,argument(cpu,3));}
            const Object object{Ordinal==189?Object::Kind::event:Object::Kind::semaphore,address};
            objects.emplace(address,object); handles.emplace(handle,object);
            if constexpr(Ordinal==193) for(unsigned i=0;i<host.watch_semaphores.size();++i) if(host.watch_semaphores[i]==handle) {
                const auto pointer=memory.access(address,1);diagnostic_watch(i,pointer);watched_addresses[i]=address;
                diagnostic_record(std::format("{{\"type\":\"memory_watch\",\"handle\":{},\"object\":{},\"slot\":{},\"native_address\":\"0x{:016X}\"}}",
                    json(hex32(handle)),json(hex32(address)),i,reinterpret_cast<std::uintptr_t>(pointer)));
                for(const auto& request:pending_io) if(request.active && request.ordinal==219) {
                    const auto size=file_size(*request.file);
                    check_watched_read(cpu,request.buffer,request.offset<size?static_cast<std::uint32_t>(std::min<std::uint64_t>(request.requested,size-request.offset)):0,request.file->path);
                }
            }
            memory.store<std::uint32_t>(argument(cpu,0),handle); finish(cpu,16,success);
        } else if constexpr(Ordinal==225 || Ordinal==186 || Ordinal==222) {
            const auto found=handles.find(argument(cpu,0));
            constexpr auto kind=Ordinal==222?Object::Kind::semaphore:Object::Kind::event;
            const auto cleanup=Ordinal==222?12:Ordinal==225?8:4;
            if(found==handles.end() || found->second.kind!=kind) {finish(cpu,cleanup,invalid_handle);return;}
            const auto address=found->second.address,prior=memory.load<std::uint32_t>(address+4);
            if constexpr(Ordinal==222) {
                const auto increment=argument(cpu,1),limit=memory.load<std::uint32_t>(address+16);
                if(static_cast<std::int32_t>(increment)<=0 || static_cast<std::uint64_t>(prior)+increment>limit) {finish(cpu,12,0xC0000047);return;}
                memory.store<std::uint32_t>(address+4,prior+increment);
            } else memory.store<std::uint32_t>(address+4,Ordinal==225?1:0);
            if constexpr(Ordinal!=186) {const auto previous=argument(cpu,Ordinal==222?2:1);if(previous) memory.store<std::uint32_t>(previous,prior);}
            finish(cpu,cleanup,success);
        } else if constexpr(Ordinal==159 || Ordinal==158 || Ordinal==233 || Ordinal==234) {
            std::array<std::uint32_t,64> addresses{}; unsigned count=1; bool all=false,alertable;
            std::uint32_t interval;
            if constexpr(Ordinal==158) {
                count=argument(cpu,0); if(!count || count>64 || argument(cpu,2)>1) {finish(cpu,32,invalid_parameter);return;}
                for(unsigned i=0;i<count;++i) addresses[i]=memory.load<std::uint32_t>(argument(cpu,1)+i*4);
                all=argument(cpu,2)==0; alertable=argument(cpu,5)!=0; interval=argument(cpu,6);
            } else if constexpr(Ordinal==159) {addresses[0]=argument(cpu,0);alertable=argument(cpu,3)!=0;interval=argument(cpu,4);}
            else {
                const auto found=handles.find(argument(cpu,0));
                if(found==handles.end()) {finish(cpu,Ordinal==233?12:16,invalid_handle);return;}
                if(found->second.kind==Object::Kind::link) {finish(cpu,Ordinal==233?12:16,0xC0000024);return;}
                addresses[0]=found->second.address+(found->second.kind==Object::Kind::file?0x38:0);
                alertable=argument(cpu,Ordinal==233?1:2)!=0; interval=argument(cpu,Ordinal==233?2:3);
            }
            const auto result=wait(std::span(addresses).first(count),all,interval,alertable);
            finish(cpu,Ordinal==158?32:Ordinal==159?20:Ordinal==233?12:16,result);
        } else if constexpr(Ordinal==246) {
            const auto found=handles.find(argument(cpu,0));
            if(found==handles.end()) {finish(cpu,12,invalid_handle);return;}
            const auto type=argument(cpu,1);
            const auto expected=found->second.kind==Object::Kind::thread?thread_type:found->second.kind==Object::Kind::event?event_type:
                found->second.kind==Object::Kind::semaphore?semaphore_type:found->second.kind==Object::Kind::file?file_type:0;
            if(type && type!=expected) {finish(cpu,12,0xC0000024);return;}
            ++object_references[found->second.address]; memory.store<std::uint32_t>(argument(cpu,2),found->second.address); finish(cpu,12,success);
        } else if constexpr(Ordinal==250) {
            const auto address=cpu.registers[ecx];
            const auto found=object_references.find(address);
            if(found==object_references.end() || !found->second) throw NativeBoundary(cpu.eip,"object dereferenced without a native reference");
            --found->second;release_object(address);finish(cpu,0);
        } else if constexpr(Ordinal==2) {
            const auto option=argument(cpu,1),parameter=argument(cpu,2);
            if(option==6) // AV_PACK_STANDARD | AV_STANDARD_NTSC_M | AV_FLAGS_60Hz.
                memory.store<std::uint32_t>(argument(cpu,3),0x00400101|(host.widescreen?0x00010000U:0U));
            else if(option==11 && parameter<=5) flicker_filter=parameter;
            else if(option==14) luma_filter=parameter!=0;
            else throw NativeBoundary(cpu.eip,"native TV encoder option is unbound");
            // Preserve encoder controls. The D3D11 image is taken before the
            // original console's analog television filtering stage.
            finish(cpu,16);
        } else if constexpr(Ordinal==24) {
            const auto index=argument(cpu,0);
            // The compatibility profile owns these settings; they are not a dump
            // of the original machine's EEPROM or cryptographic keys.
            std::optional<std::uint32_t> value;
            if(index==7) value=1; // English.
            else if(index==8) value=host.widescreen?0x00010000U:0U; // XC_VIDEO_FLAGS: native anamorphic widescreen.
            else if(index==9 || index==10 || index==17) value=0; // Stereo, no parental restriction.
            else if(index==0x103) value=0x00400100; // NTSC-M factory AV region.
            else if(index==0x104) value=1; // North American game region.
            if(!value) {finish(cpu,20,0xC0000034);return;}
            const auto length=argument(cpu,4); if(length) memory.store<std::uint32_t>(length,4);
            if(argument(cpu,3)<4) {finish(cpu,20,0xC0000023);return;}
            memory.store<std::uint32_t>(argument(cpu,1),REG_DWORD); memory.store<std::uint32_t>(argument(cpu,2),*value); finish(cpu,20,success);
        } else if constexpr(Ordinal==289) {
            const auto output=argument(cpu,0),source=argument(cpu,1); unsigned length=0;
            if(source) while(length<65534 && memory.load<std::uint8_t>(source+length)) ++length;
            memory.store<std::uint16_t>(output,static_cast<std::uint16_t>(length));
            memory.store<std::uint16_t>(output+2,static_cast<std::uint16_t>(source?length+1:0));
            memory.store<std::uint32_t>(output+4,source);finish(cpu,8);
        } else if constexpr(Ordinal==279) {
            const auto first=argument(cpu,0),second=argument(cpu,1),insensitive=argument(cpu,2);
            const auto length=memory.load<std::uint16_t>(first); bool equal=length==memory.load<std::uint16_t>(second);
            const auto a=memory.load<std::uint32_t>(first+4),b=memory.load<std::uint32_t>(second+4);
            const auto upper=[](std::uint8_t ch) {return ch>='a' && ch<='z'?ch-32:ch;};
            for(unsigned i=0;equal && i<length;++i) {
                const auto x=memory.load<std::uint8_t>(a+i),y=memory.load<std::uint8_t>(b+i);
                equal=insensitive?upper(x)==upper(y):x==y;
            }
            finish(cpu,12,equal);
        } else if constexpr(Ordinal==269) {
            const auto address=argument(cpu,0),length=argument(cpu,1),pattern=argument(cpu,2);
            unsigned matched=0;
            while(length-matched>=4 && memory.load<std::uint32_t>(address+matched)==pattern) matched+=4;
            finish(cpu,12,matched);
        } else if constexpr(Ordinal==304 || Ordinal==305) {
            if constexpr(Ordinal==305) {
                const auto time=memory.load<std::uint64_t>(argument(cpu,0));
                const FILETIME value{static_cast<DWORD>(time),static_cast<DWORD>(time>>32)}; SYSTEMTIME system{};
                if(!FileTimeToSystemTime(&value,&system)) throw NativeBoundary(cpu.eip,"invalid guest system time");
                const std::uint16_t fields[]={system.wYear,system.wMonth,system.wDay,system.wHour,system.wMinute,system.wSecond,system.wMilliseconds,system.wDayOfWeek};
                std::memcpy(memory.access(argument(cpu,1),sizeof(fields)),fields,sizeof(fields)); finish(cpu,8);
            } else {
                const auto address=argument(cpu,0);
                const SYSTEMTIME system{memory.load<std::uint16_t>(address),memory.load<std::uint16_t>(address+2),
                    memory.load<std::uint16_t>(address+14),memory.load<std::uint16_t>(address+4),memory.load<std::uint16_t>(address+6),
                    memory.load<std::uint16_t>(address+8),memory.load<std::uint16_t>(address+10),memory.load<std::uint16_t>(address+12)};
                FILETIME time{}; const bool valid_time=SystemTimeToFileTime(&system,&time)!=0;
                if(valid_time) memory.store<std::uint64_t>(argument(cpu,1),(static_cast<std::uint64_t>(time.dwHighDateTime)<<32)|time.dwLowDateTime);
                finish(cpu,8,valid_time);
            }
        }
        else if constexpr(Ordinal==47) {
            const auto registration=argument(cpu,0);
            memory.access(registration,16);
            std::erase(shutdown_registrations,registration);
            if(argument(cpu,1)) shutdown_registrations.push_back(registration);
            std::ranges::sort(shutdown_registrations,[&](auto a,auto b) {
                return static_cast<std::int32_t>(memory.load<std::uint32_t>(a+4))>static_cast<std::int32_t>(memory.load<std::uint32_t>(b+4));
            });
            auto previous=shutdown_head;
            for(const auto item : shutdown_registrations) {
                const auto link=item+8;
                memory.store<std::uint32_t>(previous,link); memory.store<std::uint32_t>(link+4,previous); previous=link;
            }
            memory.store<std::uint32_t>(previous,shutdown_head); memory.store<std::uint32_t>(shutdown_head+4,previous);
            finish(cpu,8);
        }
        else if constexpr(Ordinal==49) throw NativeBoundary(cpu.eip,std::format("guest requested firmware action {}",argument(cpu,0)));
    }
    BootResult run(std::uint32_t budget,std::uint32_t break_address,std::uint32_t break_hit) {
        struct DiagnosticScope {~DiagnosticScope(){diagnostic_guest(nullptr);}} diagnostic_scope;
        if(scheduler) throw std::runtime_error("A native Xbox instance can boot only once");
        if(IsThreadAFiber()) scheduler=GetCurrentFiber();
        else { scheduler=ConvertThreadToFiberEx(nullptr,FIBER_FLAG_FLOAT_SWITCH); converted=scheduler!=nullptr; }
        if(!scheduler) throw std::runtime_error("Cannot enter native Xbox scheduler");
        live=budget==0;
        diagnostic.remaining=live?UINT64_MAX:budget;
        diagnostic.watch_address=B2_MAIN_ADDRESS;
        diagnostic.break_address=break_address;diagnostic.break_hit=break_hit;
        create(image.entry,0,0,image.stack_commit,0,false,true);
        const auto deadline=live?std::chrono::steady_clock::time_point::max():std::chrono::steady_clock::now()+std::chrono::seconds(30);
        while(boundary.empty()) {
            Thread* selected=nullptr;
            auto earliest=std::chrono::steady_clock::time_point::max();
            bool pending=false;
            const auto now=std::chrono::steady_clock::now();
            if(now>=deadline) { boundary="Native boot wall-clock budget exhausted"; break; }
            try {
                if(host.poll && !host.poll()) {boundary="Native window closed";break;}
                dispatch_io();
                dispatch_timers();
                dispatch_video(std::chrono::steady_clock::now());
                if(!dpcs.empty()) for(auto& candidate:threads) if(candidate->state!=ThreadState::ended && !candidate->suspended) {
                    dispatch_dpcs(*candidate);break;
                }
            } catch(const std::exception& error) {boundary=error.what();break;}
            for(std::size_t offset=0;offset<threads.size();++offset) {
                const auto index=(next_runnable+offset)%threads.size();
                auto& candidate=threads[index];
                if(candidate->state==ThreadState::ended || candidate->suspended) continue;
                pending=true;
                if(candidate->state==ThreadState::waiting) {
                    if(candidate->due<=now || waiting_ready(*candidate))
                        candidate->state=ThreadState::ready;
                    else { earliest=std::min(earliest,candidate->due); continue; }
                }
                selected=candidate.get(); next_runnable=(index+1)%threads.size(); break;
            }
            for(const auto& [address,timer]:timers) earliest=std::min(earliest,timer.due);
            if(pending_io_count) earliest=std::min(earliest,now+std::chrono::milliseconds(1));
            if(graphics_initialized && memory.load<std::uint32_t>(0x002256C0+0x1988))
                earliest=std::min(earliest,video_started+std::chrono::nanoseconds(((video_ticks+1)*1000000000+59)/60));
            if(!selected) {
                if(!pending) break;
                if(earliest==std::chrono::steady_clock::time_point::max()) { boundary="All native guest threads are waiting without a signal source"; break; }
                {PerfScope idle(host.performance,PerfPhase::scheduler);Sleep(1);}continue;
            }
            current=selected; clock_data();
            quantum_due=std::chrono::steady_clock::now()+std::chrono::milliseconds(1);
            diagnostic_guest(&selected->cpu);{PerfScope guest(host.performance,PerfPhase::guest);SwitchToFiber(selected->fiber);}
            diagnostic_guest(nullptr);collect_threads();
        }
        auto result=inspect();
        // Resume sleeping native stacks with cancellation so their C++ and floating
        // scopes unwind before DeleteFiber releases host stacks.
        stopping=true;
        for(auto& thread : threads) if(thread->state!=ThreadState::ended) { current=thread.get(); SwitchToFiber(thread->fiber); }
        return result;
    }
    BootResult inspect() {
        BootResult result;
        result.entry_returned=entry_returned; result.main_reached=diagnostic.reached_watch;
        result.graphics_device_created=graphics!=nullptr;result.graphics_initialized=graphics_initialized;
        result.audio_device_created=audio && audio->output_ready();result.audio_buffers=static_cast<std::uint32_t>(audio_buffers.size());
        result.audio_effects=audio_effect_count;if(audio) result.audio_error=audio->failure();
        if(graphics) result.graphics_adapter=graphics->adapter();
        if(gpu) result.graphics=gpu->stats();
        result.io.assign(io_trace.begin(),io_trace.end());
        if(!live) {
            result.allocations.reserve(allocations.size());
            for(const auto& [address,block]:allocations)
                result.allocations.push_back({address,block.size,static_cast<std::uint32_t>(std::ranges::count_if(block.pages,[](auto protect){return protect!=0;})*page),block.protect});
            result.last_failed_allocation=last_failed_allocation;
        }
        result.threads_created=(next_thread-4)/4;
        result.vblank_callbacks=vblank_callbacks;
        result.original_camera_far=world_original_far;
        result.original_world_sections=world_original_count;result.extra_world_sections=world_added_count;
        result.extra_world_batches=world_added_batches;
        if(world_camera) result.camera_far=std::bit_cast<float>(memory.load<std::uint32_t>(world_camera+0x84));
        for(const auto& thread:threads) {
            const auto state=std::array{"ready","running","waiting","ended"}[static_cast<unsigned>(thread->state)];
            result.threads.push_back({thread->id,thread->routine,thread->system,thread->cpu.eip,
                thread->cpu.registers[esp],thread->wait_object,state,thread->suspended});
        }
        result.boundary=boundary; result.diagnostic=diagnostic; result.service_calls=service_calls;
        if(current) { result.cpu=current->cpu; result.active_thread=current->id; }
        const auto stack=result.cpu.registers[esp];
        if(stack<ram.size()) {
            result.stack_word_count=static_cast<std::uint32_t>(std::min<std::size_t>(result.stack_words.size(),(ram.size()-stack)/4));
            for(unsigned i=0;i<result.stack_word_count;++i) result.stack_words[i]=memory.load<std::uint32_t>(stack+i*4);
        }
        result.cpu.kernel=nullptr; result.cpu.diagnostic=nullptr; result.cpu.clock=nullptr; result.cpu.preempt=nullptr; result.cpu.profile=nullptr;result.cpu.world_view=nullptr;result.cpu.hud_layout=nullptr;
        result.cpu.achievements=nullptr;
        diagnostic_guest(nullptr);
        return result;
    }
};
Xbox::Xbox(Xbe& image,Memory& memory,std::span<std::byte> ram,const std::filesystem::path& disc,XboxHost host):state_(std::make_unique<State>(image,memory,ram,disc,std::move(host))) {}
Xbox::~Xbox()=default;
bool Xbox::fast_loading() const {return state_->host.fast_load && state_->game_timing.loading_started;}
void Xbox::output_gain(float gain) {
    if(!(gain>=0 && gain<=1)) throw std::runtime_error("Output volume must be between zero and one");
    if(state_->audio) state_->audio->output_gain(gain);
    state_->output_gain=gain;
}
void Xbox::performance_recording(bool enabled) {
    if(state_->gpu) state_->gpu->performance(enabled?state_->host.performance:nullptr);
    if(state_->audio) state_->audio->performance(enabled);
    for(auto& thread:state_->threads) thread->cpu.profile=enabled?state_->host.performance:nullptr;
}
PerfCounters Xbox::performance_counters() const {
    PerfCounters counters=state_->graphics?state_->graphics->stats().counters:PerfCounters{};
    const auto set=[&](PerfMetric key,std::uint64_t value){counters[unsigned(key)]=value;};
    if(state_->gpu) {
        const auto gpu=state_->gpu->stats();set(PerfMetric::submissions,gpu.submissions);set(PerfMetric::packets,gpu.packets);
        set(PerfMetric::methods,gpu.methods);set(PerfMetric::fences,gpu.fences);
    }
    if(state_->audio) {
        const auto audio=state_->audio->performance();set(PerfMetric::audio_batches,audio.batches);set(PerfMetric::audio_mix_ns,audio.mix_ns);set(PerfMetric::audio_overruns,audio.overruns);
    }
    return counters;
}
void Xbox::capture_frame(const std::filesystem::path& directory,bool automated) {
    if(!state_->gpu) throw std::runtime_error("The game has not initialized graphics yet");
    state_->gpu->capture_frame(directory,automated);
    if(state_->mods) write_text(directory/"mods.json",state_->mods->report(),false);
    const auto& memory=state_->memory;
    std::ostringstream snapshot;
    snapshot<<std::format("{{\"format\":\"b2-game-state-v1\",\"phase\":\"capture_request\",\"source_sha256\":{},\"widescreen\":{},\"replay\":{{\"frames\":{},\"index\":{},\"loaded\":{},\"playback\":{},\"finished\":{},\"fade_state\":{},\"deadline_bits\":{},\"buffer\":{}}},\"memory\":[",
        json(state_->image.sha256),state_->host.widescreen,memory.load<std::uint32_t>(0x002FFD68),memory.load<std::uint32_t>(0x002FFD6C),
        memory.load<std::uint32_t>(0x002FFD70),memory.load<std::uint32_t>(0x002FFD74),memory.load<std::uint32_t>(0x002FFD78),
        memory.load<std::uint32_t>(0x002FFD7C),memory.load<std::uint32_t>(0x002FFD80),json(hex32(memory.load<std::uint32_t>(0x00522358))));
    // Bounded original title state; collecting it requires an explicit capture.
    constexpr std::pair<std::uint32_t,unsigned> slices[]={{0x002FFD68,0xA0},{0x002FE370,16},{0x00300080,0x60},
        {0x0034AB40,0x40},{0x003525E0,0x144},{0x0048A150,16},{0x004CD800,32},{0x00522358,12},{0x00489F70,5*0x50},
        {0x0057C190,0x100},{0x002145A4,4},{0x0024A630,8},
        {0x0033DEE4,24},{0x00559E78,24},{0x00225A7C,16},{0x004CCAA8,12}};
    for(unsigned i=0;i<std::size(slices);++i) {
        if(i) snapshot<<',';const auto [address,size]=slices[i];
        snapshot<<std::format("{{\"address\":{},\"bytes\":{}}}",json(hex32(address)),json(hex_bytes({static_cast<const std::byte*>(memory.access(address,size)),size})));
    }
    snapshot<<std::format("],\"presentation\":{{\"simulation_step\":{},\"fraction\":{},\"smoothed_poses\":{},\"loading_delta\":{},\"ui\":",
        state_->game_timing.step,double(state_->game_timing.remainder)/733333333.0,
        state_->presentation.active_poses(),state_->game_timing.loading_delta);
    state_->presentation.capture_ui(snapshot,memory,state_->game_timing.step);
    snapshot<<"},\"last_failed_allocation\":[";
    for(unsigned i=0;i<state_->last_failed_allocation.size();++i) {
        if(i) snapshot<<',';snapshot<<state_->last_failed_allocation[i];
    }
    snapshot<<std::format("],\"scenery\":{{\"origin\":{},\"original_visible\":{},\"extra_visible\":{},\"added\":[",
        state_->world_origin,state_->world_original_count,state_->world_added_count);
    const auto record=[&](const State::WorldRecord& view) {snapshot<<std::format("[{}, {}, {}]",view.section,view.origin,view.row);};
    for(unsigned i=0;i<state_->world_draw_count;++i) {if(i) snapshot<<',';record(state_->world_draws[i]);}
    snapshot<<"],\"plans\":[";
    for(unsigned s=0;s<state_->world_sections.size();++s) {
        if(s) snapshot<<',';snapshot<<'[';
        const auto& additions=state_->world_sections[s].additions;
        for(unsigned i=0;i<additions.size();++i) {if(i) snapshot<<',';record(additions[i]);}
        snapshot<<']';
    }
    snapshot<<"]},\"io\":[";
    for(unsigned i=0;i<state_->io_trace.size();++i) {
        if(i) snapshot<<',';const auto& io=state_->io_trace[i];
        snapshot<<std::format("{{\"ordinal\":{},\"status\":{},\"information\":{},\"callsite\":{},\"path\":{},\"options\":{},\"requested\":{},\"offset\":{},\"buffer\":{}}}",
            io.ordinal,json(hex32(io.status)),io.information,json(hex32(io.callsite)),json(io.path),io.options,io.requested,io.offset,json(hex32(io.buffer)));
    }
    snapshot<<"]}";
    std::filesystem::create_directories(directory);write_text(directory/"game-state.json",snapshot.view(),false);
    if(state_->audio) {
        state_->audio->capture(directory);
        const auto& listener=state_->audio_listener;
        std::ostringstream report;
        report<<std::format("{{\"format\":\"b2-audio-game-state-v1\",\"source_sha256\":{},\"master_gain\":{},\"error\":{},\"full_hrtf_requested\":{},\"listener\":{{\"position\":[{},{},{}],\"velocity\":[{},{},{}],\"front\":[{},{},{}],\"up\":[{},{},{}],\"distance_factor\":{},\"rolloff_factor\":{},\"doppler_factor\":{}}},\"buffers\":[",
            json(state_->image.sha256),state_->output_gain,json(state_->audio->failure()),state_->audio_full_hrtf,
            listener.position[0],listener.position[1],listener.position[2],listener.velocity[0],listener.velocity[1],listener.velocity[2],
            listener.front[0],listener.front[1],listener.front[2],listener.up[0],listener.up[1],listener.up[2],listener.distance,listener.rolloff,listener.doppler);
        unsigned index=0;
        for(const auto& [object,buffer]:state_->audio_buffers) {
            if(index++) report<<',';
            const auto curve=buffer.curve_count?std::span(static_cast<const std::byte*>(memory.access(buffer.curve,buffer.curve_count*4)),buffer.curve_count*4):std::span<const std::byte>{};
            report<<std::format("{{\"id\":{},\"object\":{},\"flags\":{},\"spatial_parameters\":{},\"environment_parameters\":{},\"curve_address\":{},\"curve_count\":{},\"curve_bytes\":{},\"pending_spatial\":{},\"pending_environment\":{}}}",
                buffer.identifier,json(hex32(object)),json(hex32(buffer.flags)),json(hex_bytes(std::as_bytes(std::span(buffer.spatial)))),
                json(hex_bytes(std::as_bytes(std::span(buffer.environment)))),json(hex32(buffer.curve)),buffer.curve_count,json(hex_bytes(curve)),buffer.pending_spatial,buffer.pending_environment);
        }
        report<<"],\"streams\":[";index=0;
        for(const auto& [object,stream]:state_->audio_streams) {
            if(index++) report<<',';
            report<<std::format("{{\"id\":{},\"object\":{},\"codec\":{},\"channels\":{},\"rate\":{},\"capacity\":{},\"submitted\":{},\"completed\":{},\"status\":{},\"callback\":{},\"context\":{}}}",
                stream.identifier,json(hex32(object)),stream.format.tag,stream.format.channels,stream.format.rate,
                stream.packets.size(),stream.submitted,stream.completed,json(hex32(state_->audio->packet_status(stream.identifier))),json(hex32(stream.callback)),json(hex32(stream.context)));
        }
        const auto count=std::min<std::uint32_t>(state_->audio_event_count,static_cast<std::uint32_t>(state_->audio_events.size()));
        report<<std::format("],\"events_truncated\":{},\"events\":[",state_->audio_event_count>count);
        for(unsigned i=0;i<count;++i) {
            const auto& event=state_->audio_events[(state_->audio_event_count-count+i)%state_->audio_events.size()];
            if(i) report<<',';
            report<<std::format("{{\"clock\":{},\"api\":{},\"caller\":{},\"id\":{},\"source\":{},\"start\":{},\"bytes\":{},\"arguments\":[",
                event.clock,json(hex32(event.address)),json(hex32(event.caller)),event.identifier,json(hex32(event.source)),event.start,event.bytes);
            for(unsigned a=0;a<event.count;++a) {if(a) report<<',';report<<json(hex32(event.arguments[a]));}
            report<<']';
            if(event.address==0x0022D734 || event.address==0x0022FA52)
                report<<std::format(",\"effect_data\":{},\"effect_data_truncated\":{}",
                    json(hex_bytes(std::span(event.effect_data).first(event.effect_bytes))),event.effect_bytes<event.arguments[4]);
            report<<'}';
        }
        report<<"]}";write_text(directory/"audio-game-state.json",report.view(),false);
        state_->audio_event_count=0;state_->audio_trace_until=state_->audio->sample_clock()+480000;state_->audio_trace=true;
    }
}
BootResult Xbox::run(std::uint32_t budget,std::uint32_t break_address,std::uint32_t break_hit) {
    if(break_address && !break_hit) throw std::runtime_error("A diagnostic breakpoint requires a positive hit count");
    return state_->run(budget,break_address,break_hit);
}
std::string check_audio_listener(const std::filesystem::path& executable,const std::filesystem::path& output) {
    Xbe image(executable);
    if(!image.supported() || std::filesystem::exists(output)) throw std::runtime_error("Audio binding checks require the verified XBE and a fresh output directory");
    std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
    XboxHost provider;provider.storage_root=output;Xbox xbox(image,memory,ram,{},std::move(provider));auto& state=*xbox.state_;
    state.host.audio_output=false;
    // Reference setters run their original software-only, deferred SDK path.
    // The separate native provider uses no audio device or game boot.
    constexpr std::uint32_t object=0xA000,settings=0xB000,stack=0xCFC0;
    memory.store<std::uint32_t>(object+8,settings);memory.store<std::uint32_t>(0x0024A634,0);
    Cpu cpu;cpu.kernel=&state.services;cpu.fs_base=0xD000;
    // 22C095 skips its device mutex at DPC level. Deferred setters only copy
    // settings in this supported path; no hardware or scheduler is involved.
    memory.store<std::uint8_t>(cpu.fs_base+0x24,1);
    unsigned checks=0;
    const auto require=[&](bool passed,std::string_view name) {
        if(!passed) throw std::runtime_error("Audio binding check failed: "+std::string(name));++checks;
    };
    const auto invoke=[&](std::uint32_t entry,std::span<const std::uint32_t> arguments,std::uint32_t expected=0,bool provider_only=false) {
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
        for(unsigned i=0;i<arguments.size();++i) memory.store<std::uint32_t>(stack+4+i*4,arguments[i]);
        if(provider_only) {cpu.eip=entry;state.audio_api(cpu,entry);}
        else invoke_native(cpu,memory,entry);
        require(cpu.eip==return_sentinel && cpu.registers[esp]==stack+4+arguments.size()*4 && cpu.registers[eax]==expected,
            std::format("original/native audio ABI at {}: expected {}, returned {}",hex32(entry),expected,cpu.registers[eax]));
    };
    const auto values=[](const SpatialListener& listener) {
        return std::array<float,15>{listener.position[0],listener.position[1],listener.position[2],listener.velocity[0],listener.velocity[1],listener.velocity[2],
            listener.front[0],listener.front[1],listener.front[2],listener.up[0],listener.up[1],listener.up[2],listener.distance,listener.rolloff,listener.doppler};
    };
    invoke(0x0022FC18,std::array<std::uint32_t,3>{0,0xA080,0});
    require(state.audio_device==memory.load<std::uint32_t>(0xA080),"native sound device layout");
    const auto initial=values(state.audio_listener);
    for(unsigned i=0;i<initial.size();++i) memory.store<std::uint32_t>(settings+0x3C+i*4,std::bit_cast<std::uint32_t>(initial[i]));
    struct Setter {std::uint32_t api,reference,count;std::array<float,6> data;};
    const std::array setters{
        Setter{0x0022F9C4,0x0022F6E4,3,{-355.89722f,67.815994f,-1264.2976f}},
        Setter{0x0022FA1D,0x0022F757,3,{29.20445f,0.16950999f,-41.51002f}},
        Setter{0x0022F97A,0x0022F657,6,{0.6f,0,-0.8f,0,1,0}},
        Setter{0x0022F932,0x0022F53A,1,{0.625f}},
        Setter{0x0022F9F9,0x0022F5F8,1,{0.75f}},
        Setter{0x0022F956,0x0022F599,1,{0.5f}}
    };
    for(const auto& setter:setters) {
        std::array<std::uint32_t,8> arguments{};arguments[0]=object;
        for(unsigned i=0;i<setter.count;++i) arguments[i+1]=std::bit_cast<std::uint32_t>(setter.data[i]);
        arguments[setter.count+1]=1;invoke(setter.reference,std::span(arguments).first(setter.count+2));
        arguments[0]=state.audio_device;invoke(setter.api,std::span(arguments).first(setter.count+2));
        const auto deferred=values(state.audio_deferred_listener);bool equal=true;
        for(unsigned i=0;i<deferred.size();++i) equal&=memory.load<std::uint32_t>(settings+0x3C+i*4)==std::bit_cast<std::uint32_t>(deferred[i]);
        require(equal,"all listener fields match original SDK setters");
        require(values(state.audio_listener)==initial,"deferred changes wait for commit");
    }
    invoke(0x0022F3F5,std::array<std::uint32_t,1>{state.audio_device});
    require(values(state.audio_listener)==values(state.audio_deferred_listener) && !state.audio_pending_listener,"complete deferred listener commit");
    const auto position=state.audio_listener.position;
    invoke(0x0022F9C4,std::array<std::uint32_t,5>{state.audio_device,0,0,0,1});
    invoke(0x0022FA1D,std::array<std::uint32_t,5>{state.audio_device,0,0,0,0});
    require(state.audio_listener.position==position && state.audio_listener.velocity==std::array<float,3>{} && state.audio_pending_listener==1,"immediate velocity preserves deferred position");
    invoke(0x0022F3F5,std::array<std::uint32_t,1>{state.audio_device});
    require(state.audio_listener.position==std::array<float,3>{} && !state.audio_pending_listener,"mixed immediate/deferred commit");
    constexpr std::uint32_t descriptor=0xE000,format=0xE040,result=0xE080,packet=0xE100,data=0xE200,completed=0xE300,status=0xE304;
    memory.store<std::uint32_t>(descriptor,24);memory.store<std::uint32_t>(descriptor+4,0x10);
    memory.store<std::uint32_t>(descriptor+8,0);memory.store<std::uint32_t>(descriptor+12,format);
    memory.store<std::uint16_t>(format,1);memory.store<std::uint16_t>(format+2,1);memory.store<std::uint32_t>(format+4,48000);
    memory.store<std::uint32_t>(format+8,96000);memory.store<std::uint16_t>(format+12,2);memory.store<std::uint16_t>(format+14,16);
    invoke(0x0022F8EA,std::array<std::uint32_t,4>{state.audio_device,descriptor,result,0});
    const auto curved_buffer=memory.load<std::uint32_t>(result);
    state.audio->mixbins(state.audio_buffers.at(curved_buffer).identifier,std::array<MixBin,2>{{{0,0},{1,0}}});
    auto curve_parameters=state.audio_buffers.at(curved_buffer).spatial;
    curve_parameters[1]=std::bit_cast<std::uint32_t>(5.0f);
    curve_parameters[13]=std::bit_cast<std::uint32_t>(1.0f);curve_parameters[14]=std::bit_cast<std::uint32_t>(10.0f);
    std::memcpy(memory.access(0xE400,sizeof(curve_parameters)),curve_parameters.data(),sizeof(curve_parameters));
    invoke(0x0022E78C,std::array<std::uint32_t,3>{curved_buffer,0xE400,0});
    constexpr std::uint32_t curve=0xE380;
    for(unsigned i=0;i<2;++i) memory.store<std::uint32_t>(curve+i*4,std::bit_cast<std::uint32_t>(1.0f));
    const std::array<std::int16_t,8> curve_samples{8192,8192,8192,8192,8192,8192,8192,8192};
    std::memcpy(memory.access(data,sizeof(curve_samples)),curve_samples.data(),sizeof(curve_samples));
    invoke(0x0022EF4B,std::array<std::uint32_t,3>{curved_buffer,data,sizeof(curve_samples)});
    invoke(0x0022E8C4,std::array<std::uint32_t,4>{curved_buffer,curve,2,0});
    invoke(0x0022D896,std::array<std::uint32_t,4>{curved_buffer,0,0,1});
    std::array<float,8> curve_mix{};state.audio->mix(curve_mix);
    require(std::ranges::any_of(curve_mix,[](float sample){return sample!=0;}),"live distance curve has audible gain");
    double before_curve=0;for(float sample:curve_mix) before_curve+=double(sample)*sample;
    for(unsigned i=0;i<2;++i) memory.store<std::uint32_t>(curve+i*4,0);
    invoke(0x0022F9C4,std::array<std::uint32_t,5>{state.audio_device,0,0,0,0});
    state.audio->mix(curve_mix);state.audio->mix(curve_mix); // Settle the existing per-mix gain ramp.
    double after_curve=0;for(float sample:curve_mix) after_curve+=double(sample)*sample;
    require(after_curve<before_curve,"unchanged listener still observes changed curve bytes");
    invoke(0x0022C11B,std::array<std::uint32_t,1>{curved_buffer});
    const auto make_buffer=[&](std::uint32_t flags) {
        std::memset(memory.access(descriptor,24),0,24);memory.store<std::uint32_t>(descriptor,24);
        memory.store<std::uint32_t>(descriptor+4,flags);memory.store<std::uint32_t>(descriptor+12,format);
        invoke(0x0022F8EA,std::array<std::uint32_t,4>{state.audio_device,descriptor,result,0});
        return memory.load<std::uint32_t>(result);
    };
    const auto retained=make_buffer(0),retained_raw=state.audio_buffers.at(retained).raw;
    // AddRef still runs the original SDK code; Release must use the same count.
    invoke(0x0022C1E4,std::array<std::uint32_t,1>{retained_raw},2);
    invoke(0x0022C11B,std::array<std::uint32_t,1>{retained},1);
    require(state.audio_buffers.contains(retained) && memory.load<std::uint32_t>(retained_raw+4)==1,"original AddRef retains native buffer");
    invoke(0x0022C11B,std::array<std::uint32_t,1>{retained});
    const auto routed=make_buffer(0),first_output=make_buffer(0x2000),second_output=make_buffer(0x2000),last_output=make_buffer(0x2000);
    invoke(0x0022D85E,std::array<std::uint32_t,2>{routed,0});
    invoke(0x0022D7D2,std::array<std::uint32_t,2>{first_output,std::bit_cast<std::uint32_t>(-1200)});
    invoke(0x0022EF4B,std::array<std::uint32_t,3>{routed,data,sizeof(curve_samples)});
    invoke(0x0022E754,std::array<std::uint32_t,2>{routed,first_output});
    invoke(0x0022D896,std::array<std::uint32_t,4>{routed,0,0,1});
    std::array<float,64> before_release{},after_release{};state.audio->mix(before_release);
    invoke(0x0022C11B,std::array<std::uint32_t,1>{first_output},1);
    state.audio->mix(after_release);
    require(before_release==after_release && before_release[0]>0 && before_release[0]<0.1f,"output reference release preserves continuous mixed samples");
    invoke(0x0022E754,std::array<std::uint32_t,2>{routed,first_output});
    require(memory.load<std::uint32_t>(state.audio_buffers.at(first_output).raw+4)==1,"unchanged output does not retain twice");
    invoke(0x0022E754,std::array<std::uint32_t,2>{routed,second_output});
    require(!state.audio_buffers.contains(first_output),"replacing output releases its last routing reference");
    invoke(0x0022E754,std::array<std::uint32_t,2>{routed,0});
    require(memory.load<std::uint32_t>(state.audio_buffers.at(second_output).raw+4)==1,"clearing output releases its routing reference");
    invoke(0x0022E754,std::array<std::uint32_t,2>{routed,second_output});
    invoke(0x0022E754,std::array<std::uint32_t,2>{second_output,last_output});
    invoke(0x0022C11B,std::array<std::uint32_t,1>{second_output},1);
    invoke(0x0022C11B,std::array<std::uint32_t,1>{last_output},1);
    invoke(0x0022C11B,std::array<std::uint32_t,1>{routed});
    require(state.audio_buffers.empty(),"source release frees the final output chain without leaking voices");
    std::memset(memory.access(descriptor,24),0,24);
    memory.store<std::uint32_t>(descriptor,0x20000000);memory.store<std::uint32_t>(descriptor+4,2);memory.store<std::uint32_t>(descriptor+8,format);
    memory.store<std::uint16_t>(format,1);memory.store<std::uint16_t>(format+2,2);memory.store<std::uint32_t>(format+4,48000);
    memory.store<std::uint32_t>(format+8,192000);
    memory.store<std::uint16_t>(format+12,4);memory.store<std::uint16_t>(format+14,16);
    invoke(0x0022F90E,std::array<std::uint32_t,4>{state.audio_device,descriptor,result,0});
    const auto stream=memory.load<std::uint32_t>(result),vtable=memory.load<std::uint32_t>(stream);
    invoke(0x0022CBC4,std::array<std::uint32_t,1>{stream},2);
    require(memory.load<std::uint32_t>(stream+8)==2,"stream AddRef shares the original SDK field");
    invoke(0x0022CC0B,std::array<std::uint32_t,1>{stream},1);
    invoke(memory.load<std::uint32_t>(vtable+8),std::array<std::uint32_t,2>{stream,result});
    require(memory.load<std::uint32_t>(result)==5 && memory.load<std::uint32_t>(result+4)==4 && memory.load<std::uint32_t>(result+12)==256,"original stream info layout and lookahead");
    const std::array<std::int16_t,8> samples{8192,-8192,4096,-4096,8192,-8192,4096,-4096};std::memcpy(memory.access(data,sizeof(samples)),samples.data(),sizeof(samples));
    for(unsigned i=0;i<2;++i) {
        memory.store<std::uint32_t>(packet,data+i*8);memory.store<std::uint32_t>(packet+4,8);
        memory.store<std::uint32_t>(packet+8,completed+i*8);memory.store<std::uint32_t>(packet+12,status+i*8);
        invoke(memory.load<std::uint32_t>(vtable+16),std::array<std::uint32_t,3>{stream,packet,0});
        require(!memory.load<std::uint32_t>(completed+i*8) && memory.load<std::uint32_t>(status+i*8)==0x8000000A,"stream packet remains pending until consumption");
    }
    std::array<float,8> mixed{};state.audio->mix(mixed);invoke(0x0022D9CA,{});
    require(memory.load<std::uint32_t>(completed)==8 && !memory.load<std::uint32_t>(status) && memory.load<std::uint32_t>(completed+8)==8 && !memory.load<std::uint32_t>(status+8),"stream packet completion ABI");
    require(std::abs(mixed[0]-0.25f*std::pow(10.f,-600.f/2000.f))<0.000001f && mixed[1]==-mixed[0],"stream routing and default headroom");
    // XACT calls the SDK's jump wrappers, rather than the underlying stream
    // methods. Exercise those same entries against audible packet output.
    memory.fill_elements<std::uint16_t>(0xF000,8192,1024,false);
    memory.store<std::uint32_t>(packet,0xF000);memory.store<std::uint32_t>(packet+4,2048);
    invoke(0x0022CDA9,std::array<std::uint32_t,3>{stream,packet,0});
    std::array<float,64> control_mix{};
    const auto settle=[&] {state.audio->mix(control_mix);state.audio->mix(control_mix);};
    const auto headroom=std::pow(10.f,-600.f/2000.f);
    for(const auto volume:std::array<std::int32_t,3>{-2000,-10000,0}) {
        invoke(0x0022D98A,std::array<std::uint32_t,2>{stream,std::bit_cast<std::uint32_t>(volume)});
        settle();const auto expected=volume==-10000?0.f:0.25f*headroom*std::pow(10.f,volume/2000.f);
        require(std::ranges::all_of(control_mix,[&](float sample){return std::abs(sample-expected)<0.000001f;}),
            std::format("XACT stream volume wrapper applies {} dB to audible output (sample {})",volume/100.f,control_mix[0]));
    }
    invoke(0x0022D9A8,std::array<std::uint32_t,2>{stream,1});settle();
    require(std::ranges::all_of(control_mix,[](float sample){return sample==0;}),"XACT stream pause wrapper silences queued music");
    require(memory.load<std::uint32_t>(status+8)==0x8000000A,"paused music retains its queued packet");
    invoke(0x0022D9A8,std::array<std::uint32_t,2>{stream,0});settle();
    require(control_mix[0]>0,"XACT stream resume wrapper restores queued music");
    invoke(0x0022CD0D,std::array<std::uint32_t,1>{stream});
    memory.store<std::uint32_t>(packet,data+8);memory.store<std::uint32_t>(packet+4,8);
    for(const auto flags:std::array<std::uint32_t,3>{1,3,5}) {
        invoke(0x0022CDA9,std::array<std::uint32_t,3>{stream,packet,0});
        // The original adapter and these four arguments match the live crash.
        invoke(0x0022D9AD,std::array<std::uint32_t,4>{stream,0,0,flags});
        require(!memory.load<std::uint32_t>(completed+8) && memory.load<std::uint32_t>(status+8)==0x8000000A,"asynchronous flush defers packet publication");
        state.audio->mix(mixed);require(std::ranges::all_of(mixed,[](float value){return value==0;}),"released stream stops mixing");
        invoke(0x0022D9CA,{});
        require(memory.load<std::uint32_t>(completed+8)==8 && memory.load<std::uint32_t>(status+8)==(flags&4?0:0x80004004U),"asynchronous flush completion status and size");
        invoke(0x0022CD58,std::array<std::uint32_t,2>{stream,result});
        require(memory.load<std::uint32_t>(result)==(flags&4?0x80001U:1U),"stream envelope completion status");
    }
    invoke(0x0022CDA9,std::array<std::uint32_t,3>{stream,packet,0});
    invoke(0x0022D9AD,std::array<std::uint32_t,4>{stream,0,0,5});
    state.audio->mix(mixed);invoke(0x0022D9CA,{});
    require(std::ranges::all_of(mixed,[](float value){return value==0;}) && memory.load<std::uint32_t>(status+8)==0x8000000A,"completed envelope retains queued packets without restarting");
    invoke(0x0022CD0D,std::array<std::uint32_t,1>{stream});
    require(memory.load<std::uint32_t>(completed+8)==8 && memory.load<std::uint32_t>(status+8)==0x80004004,"regular flush publishes pending packets synchronously");
    invoke(0x0022CDA9,std::array<std::uint32_t,3>{stream,packet,0});state.audio->mix(mixed);invoke(0x0022D9CA,{});
    require(mixed[0]!=0 && !memory.load<std::uint32_t>(status+8),"regular flush resets envelope completion for stream reuse");
    const auto stream_output=make_buffer(0x2000);
    // This SDK method is not reached by the title's fixed entry table. Check
    // its provider directly without claiming another translated game function.
    invoke(0x0022E694,std::array<std::uint32_t,2>{stream,stream_output},0,true);
    invoke(0x0022C11B,std::array<std::uint32_t,1>{stream_output},1);
    invoke(0x0022CC0B,std::array<std::uint32_t,1>{stream});
    require(state.audio_buffers.empty() && state.audio_streams.empty(),"stream release frees its retained output buffer");
    const auto low_memory=std::span(ram).first(0xC0);std::array<std::byte,0xC0> before_low{};
    std::copy(low_memory.begin(),low_memory.end(),before_low.begin());
    invoke(0x0022D776,std::array<std::uint32_t,2>{state.audio_device,1});
    invoke(0x0022D776,std::array<std::uint32_t,2>{state.audio_device,0});
    require(std::ranges::equal(before_low,low_memory),"headphone mode preserves unrelated guest memory without an effect image");
    std::memcpy(memory.access(0xE500,sizeof(state.audio_environment)),state.audio_environment.data(),sizeof(state.audio_environment));
    invoke(0x0022FA52,std::array<std::uint32_t,3>{state.audio_device,0xE500,0},0x88780032U);
    require(std::ranges::equal(before_low,low_memory),"unassigned reverb rejects listener changes without touching guest memory");
    const auto effects_path=executable.parent_path()/"b2distfx.bin";
    const bool environment_checked=std::filesystem::exists(effects_path);
    if(environment_checked) {
        File effects(effects_path);require(effects.size()<=65536,"bounded binding effect image");
        const auto storage=state.allocate(static_cast<std::uint32_t>(effects.size()),page,true,PAGE_READWRITE);
        effects.read(0,std::span(static_cast<std::byte*>(memory.access(storage,effects.size())),effects.size()));
        // The title's verified 10AA10 upload assigns reverb 2 and crosstalk 4.
        memory.store<std::uint32_t>(0xE540,2);memory.store<std::uint32_t>(0xE544,4);
        invoke(0x0022D6E6,std::array<std::uint32_t,5>{state.audio_device,storage,static_cast<std::uint32_t>(effects.size()),0xE540,0xE548});
        constexpr std::uint32_t legacy_shadow=0x017D0618;
        std::array<std::byte,0x300> sound_data;sound_data.fill(std::byte{0x5A});
        std::memcpy(memory.access(legacy_shadow,sound_data.size()),sound_data.data(),sound_data.size());
        auto environment=state.audio_environment;const auto previous_environment=environment;
        environment[0]=std::bit_cast<std::uint32_t>(-2500);environment[3]=std::bit_cast<std::uint32_t>(2.5f);
        std::memcpy(memory.access(0xE500,sizeof(environment)),environment.data(),sizeof(environment));
        invoke(0x0022F7CA,std::array<std::uint32_t,3>{object,0xE500,1});
        require(std::memcmp(memory.access(settings+0x88,sizeof(environment)),environment.data(),sizeof(environment))==0,"listener layout matches the original deferred SDK setter");
        std::array<std::byte,0x108> before_parameters,after_parameters;
        state.audio->effect_data(state.effect_offset(2)+0x118,before_parameters);
        invoke(0x0022FA52,std::array<std::uint32_t,3>{state.audio_device,0xE500,1});
        state.audio->effect_data(state.effect_offset(2)+0x118,after_parameters);
        require(state.audio_environment==previous_environment && state.audio_deferred_environment==environment && state.audio_pending_environment && before_parameters==after_parameters,"deferred environment waits for settings commit");
        invoke(0x0022F3F5,std::array<std::uint32_t,1>{state.audio_device});
        state.audio->effect_data(state.effect_offset(2)+0x118,after_parameters);
        require(state.audio_environment==environment && !state.audio_pending_environment && before_parameters!=after_parameters,"settings commit updates original reverb coefficients");
        environment[3]=std::bit_cast<std::uint32_t>(1.2f);std::memcpy(memory.access(0xE500,sizeof(environment)),environment.data(),sizeof(environment));
        invoke(0x0022FA52,std::array<std::uint32_t,3>{state.audio_device,0xE500,0});
        require(state.audio_environment==environment && !state.audio_pending_environment,"immediate environment updates active settings");
        std::uint32_t flags=0;
        invoke(0x0022D776,std::array<std::uint32_t,2>{state.audio_device,1});
        state.audio->effect_data(state.effect_offset(4)+0x10,std::as_writable_bytes(std::span(&flags,1)));
        require(!(flags&2),"headphones disable crosstalk processing");
        invoke(0x0022D776,std::array<std::uint32_t,2>{state.audio_device,0});
        state.audio->effect_data(state.effect_offset(4)+0x10,std::as_writable_bytes(std::span(&flags,1)));
        require((flags&2)!=0,"speakers restore crosstalk processing");
        std::array<float,64> silence;state.audio->mix(silence);
        require(std::ranges::all_of(silence,[](float value){return std::isfinite(value);}),"updated reverb processes an original DSP block");
        require(std::memcmp(memory.access(legacy_shadow,sound_data.size()),sound_data.data(),sound_data.size())==0 && std::ranges::equal(before_low,low_memory),"environment and headphone changes preserve sound-bank and unrelated guest bytes");
    }
    const auto report=std::format("{{\"format\":\"b2-audio-binding-check-v1\",\"passed\":true,\"game_booted\":false,\"audio_device_opened\":false,\"cases\":{},\"original_setters\":{},\"environment_image_checked\":{}}}",checks,environment_checked?7:6,environment_checked);
    write_text(output/"audio-bindings.json",report,false);return report;
}
bool Xbox::save_frame(const std::filesystem::path& path) {
    if(!state_->gpu || (!state_->gpu->stats().clears && !state_->gpu->stats().draws)) return false;
    state_->gpu->snapshot();save_png(state_->graphics->read_frame(),path);return true;
}
std::string check_custom_music(const std::filesystem::path& executable,const std::filesystem::path& output,const std::filesystem::path& library) {
    Xbe image(executable);
    if(!image.supported() || std::filesystem::exists(output)) throw std::runtime_error("Custom music checks require the verified XBE and a fresh output directory.");
    const auto source=output/"Custom validation",cache=library.empty()?output/"cache":library;
    std::filesystem::create_directories(library.empty()?source:output);
    const bool fixture=library.empty();
    if(fixture) {
    std::vector<float> tone(48000*2);
    for(unsigned frame=0;frame<48000;++frame) {
        tone[frame*2]=0.2f*std::sin(frame*6.283185307179586*440/48000);
        tone[frame*2+1]=0.15f*std::sin(frame*6.283185307179586*660/48000);
    }
    for(unsigned i=0;i<7;++i) save_audio_wave(source/utf8_path(std::format("{:02} - Café.wav",i+1)),tone);
    if(import_custom_music(source,cache)!=7 || custom_music_count(cache)!=7) throw std::runtime_error("Custom music import did not produce the expected catalogue.");
    File database(cache/"ST.DB");const auto before=hash(database,0,database.size());
    try {import_custom_music(source,cache,[](unsigned completed,unsigned,std::string_view){return completed==0;});throw std::runtime_error("Cancellation did not stop the music import.");}
    catch(const std::exception& error) {if(std::string_view(error.what())!="Music import cancelled.") throw;}
    File unchanged(cache/"ST.DB");if(hash(unchanged,0,unchanged.size())!=before) throw std::runtime_error("Cancelled import changed the existing catalogue.");
    }
    const auto songs=custom_music_count(cache);
    if(!songs) throw std::runtime_error("Custom music check needs an imported catalogue.");
    std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
    XboxHost provider;provider.storage_root=output/"storage";Xbox xbox(image,memory,ram,{},std::move(provider));auto& state=*xbox.state_;
    auto& thread=state.create(0,0,0,image.stack_commit,0,false);state.current=&thread;
    auto& cpu=thread.cpu;cpu.preempt=nullptr;state.diagnostic.remaining=4000000;diagnostic_guest(&cpu);
    const auto stack=cpu.registers[esp]-4096;
    unsigned checks=0;
    const auto require=[&](bool passed,std::string_view name) {if(!passed) throw std::runtime_error("Custom music check failed: "+std::string(name));++checks;};
    const auto invoke=[&](std::uint32_t address,std::initializer_list<std::uint32_t> arguments) {
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
        unsigned index=0;for(auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
        try {invoke_native(cpu,memory,address);}
        catch(const std::exception& error) {
            std::ostringstream failure;
            failure<<"{\"call\":"<<json(hex32(address))<<",\"eip\":"<<json(hex32(cpu.eip))<<",\"cases\":"<<checks
                <<",\"error\":"<<json(error.what())<<",\"registers\":[";
            for(unsigned i=0;i<8;++i) failure<<(i?",":"")<<json(hex32(cpu.registers[i]));
            failure<<"],\"stack\":[";
            for(unsigned i=0;i<12 && memory.try_access(cpu.registers[esp]+i*4,4);++i)
                failure<<(i?",":"")<<json(hex32(memory.load<std::uint32_t>(cpu.registers[esp]+i*4)));
            failure<<"]}";write_text(output/"failure.json",failure.str(),false);
            throw std::runtime_error(std::format("Custom music call {} stopped at {}: {}",hex32(address),hex32(cpu.eip),error.what()));
        }
        require(cpu.eip==return_sentinel && cpu.registers[esp]==stack+4+arguments.size()*4,"original calling convention");
        return cpu.registers[eax];
    };
    memory.store<std::uint32_t>(0xA000,0x30);
    const auto heap=invoke(0x000E435A,{2,0,image.heap_reserve,image.heap_commit,0,0xA000});
    memory.store<std::uint32_t>(0x005A619C,heap);require(heap!=0,"original CRT heap");
    // Original XapiInitProcess at E609C creates the separate LocalAlloc heap
    // and stores it at 5A8974. Normal boot already performs this initialization.
    memory.fill_elements<std::uint32_t>(0xA080,0,12,false);memory.store<std::uint32_t>(0xA080,0x30);
    const auto sdk_heap=invoke(0x000E435A,{2,0,image.heap_reserve,image.heap_commit,0,0xA080});
    memory.store<std::uint32_t>(0x005A8974,sdk_heap);require(sdk_heap!=0,"original soundtrack SDK heap");
    constexpr std::uint32_t info=0xA100,song=0xA200,time=0xA204,title=0xA240,descriptor=0xA300,result=0xA340,packet=0xA400;
    require(invoke(0x000E0C86,{info})==UINT32_MAX,"custom music disabled without a mounted catalogue");
    state.host.custom_music_root=std::filesystem::canonical(cache);
    const auto enumeration=invoke(0x000E0C86,{info});require(enumeration!=UINT32_MAX,"original soundtrack discovery");
    require(memory.load<std::uint32_t>(info)==1 && memory.load<std::uint32_t>(info+4)==songs,"original soundtrack identity and song count");
    if(fixture) require(std::u16string(static_cast<const char16_t*>(memory.access(info+12,64)))==u"Custom validation","original soundtrack name");
    unsigned first_duration=0;
    for(unsigned i=0;i<songs;++i) {
        require(invoke(0x000E0D35,{1,i,song,time,title,32})!=0,"original song metadata lookup");
        require(memory.load<std::uint32_t>(song)==(((i/6+1)<<16)|(i%6)),"original song identifiers across a six-song group boundary");
        if(!i) first_duration=memory.load<std::uint32_t>(time);
        if(fixture) {
            require(memory.load<std::uint32_t>(time)>=990 && memory.load<std::uint32_t>(time)<=1010,"original song duration");
            require(std::u16string(static_cast<const char16_t*>(memory.access(title,64)))==utf16(std::format("{:02} - Café",i+1)),"original Unicode song names");
        }
    }
    require(invoke(0x000E0D35,{1,songs+1,song,time,title,32})==0,"out-of-range song rejected by the original SDK");
    require(invoke(0x000E0C67,{enumeration,info})==0,"original catalogue enumeration ends");
    require(invoke(0x000E17F6,{enumeration})!=0,"original soundtrack enumeration closes");
    const auto handle=invoke(0x000E0F5E,{0x00010000,1});require(handle!=UINT32_MAX,"original song file opens");
    memory.fill_elements<std::uint32_t>(descriptor,0,4,false);memory.store<std::uint32_t>(descriptor+4,handle);
    memory.store<std::uint32_t>(descriptor+12,0x20000);
    require(invoke(0x0024C13F,{descriptor,result})==0,"original WMA decoder accepts the imported song");
    const auto decoder=memory.load<std::uint32_t>(result),vtable=memory.load<std::uint32_t>(decoder);
    const auto ready=[&] {
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
        do {
            state.dispatch_io();invoke(memory.load<std::uint32_t>(vtable+0x24),{decoder});
            require(invoke(memory.load<std::uint32_t>(vtable+12),{decoder,result})==0,"original custom WMA status");
            if(memory.load<std::uint32_t>(result)&2) return;
            Sleep(1);
        } while(std::chrono::steady_clock::now()<deadline);
        throw std::runtime_error("Custom music check timed out waiting for original WMA input.");
    };
    ready();require(invoke(memory.load<std::uint32_t>(vtable+8),{decoder,info})==0,"original custom WMA packet information");
    const auto parser=memory.load<std::uint32_t>(decoder+0x30);
    require(memory.load<std::uint32_t>(parser+0x2C)==44100 && memory.load<std::uint16_t>(parser+0x38)==2,"Xbox-compatible stereo format");
    const auto packet_bytes=memory.load<std::uint32_t>(info+8);
    require(packet_bytes && packet_bytes<=65536 && !(packet_bytes%4),"bounded original decoder packets");
    const auto buffer=state.allocate(65536,page,true,PAGE_READWRITE);require(buffer!=0,"native PCM storage");
    std::uint64_t decoded_frames=0;float peak=0;double energy=0;
    const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(60);
    while(true) {
        require(std::chrono::steady_clock::now()<deadline,"bounded whole-song decoding");
        state.diagnostic.remaining=4000000;
        ready();memory.fill_elements<std::uint32_t>(packet,0,6,false);
        memory.store<std::uint32_t>(packet,buffer);memory.store<std::uint32_t>(packet+4,packet_bytes);
        memory.store<std::uint32_t>(packet+8,result);memory.store<std::uint32_t>(packet+12,result+4);
        const auto processed=invoke(memory.load<std::uint32_t>(vtable+16),{decoder,0,packet});
        const auto bytes=memory.load<std::uint32_t>(result),status=memory.load<std::uint32_t>(result+4);
        // The original decoder signals EOF with E_FAIL on its final packet,
        // which can still contain decoded samples.
        // Require the complete catalogue duration below, so a premature
        // decoder failure cannot pass as a successful end of the song.
        const bool ended=status==0x80004005;
        require(!processed && (!status || ended),std::format("original WMA decoding at {} ms (result {}, packet {})",decoded_frames*1000/44100,hex32(processed),hex32(status)));
        require(bytes<=packet_bytes && !(bytes%4),"original decoded PCM completion");
        if(!bytes) break;
        decoded_frames+=bytes/4;
        for(unsigned sample=0;sample<bytes/2;++sample) {
            const auto value=std::bit_cast<std::int16_t>(memory.load<std::uint16_t>(buffer+sample*2))/32768.0f;
            peak=std::max(peak,std::abs(value));energy+=double(value)*value;
        }
        require(decoded_frames*1000/44100<=std::uint64_t(first_duration)+1000,"decoder stays within catalogue duration");
        if(ended) break;
    }
    require(decoded_frames && peak>0,"audible imported PCM");
    if(fixture) require(peak>0.1f && peak<0.3f,"imported tone preserves level");
    const auto decoded_ms=decoded_frames*1000/44100;
    require(decoded_ms+100>=first_duration && decoded_ms<=std::uint64_t(first_duration)+100,
        std::format("complete song duration (decoded {} ms, catalogue {} ms)",decoded_ms,first_duration));
    invoke(memory.load<std::uint32_t>(vtable+4),{decoder});invoke(0x000E099C,{handle});
    if(fixture) {
        // Use the original catalogue and XACT queue/next methods to check the
        // menu option without opening a window, an audio device or a game save.
        const auto menu=state.allocate(0x3000,page,true,PAGE_READWRITE),manager=state.allocate(page,page,true,PAGE_READWRITE);
        require(menu && manager,"bounded music menu storage");
        cpu.registers[ecx]=0x352728;invoke(0x184C0,{});
        cpu.registers[ecx]=manager;invoke(0xC8D70,{});memory.store<std::uint32_t>(0x443FA0,manager);
        const auto prepare_menu=[&] {
            memory.store<std::uint32_t>(menu+0x1F9C,2);memory.store<std::uint32_t>(menu+0x1FA0,menu+0x1F6C);
            cpu.registers[esi]=menu;title_music(cpu,memory,0x462DA);
        };
        prepare_menu();const auto rows=state.music_menu_storage;
        require(memory.load<std::uint32_t>(menu+0x1F9C)==3 && memory.load<std::uint32_t>(menu+0x1FA0)==rows &&
            memory.load<std::uint32_t>(menu+0x1F68)==rows,"native menu and music selector share all three rows");
        require(!std::strcmp(static_cast<const char*>(memory.access(memory.load<std::uint32_t>(rows+48),11)),"Loop Track") &&
            !memory.load<std::uint32_t>(rows+64),"loop option is labelled and defaults off");
        cpu.registers[ecx]=rows+48;require(invoke(0x45F60,{})==1 && memory.load<std::uint32_t>(rows+64)==1,"original right-arrow handler enables loop");
        prepare_menu();require(!memory.load<std::uint32_t>(rows+64) && !state.host.loop_track,"reopening discards an unconfirmed loop change");
        bool saved=false;state.host.music_loop_changed=[&](bool value){saved=value;};
        memory.store<std::uint32_t>(0x57C210,0);memory.store<std::uint32_t>(rows+64,1);title_music(cpu,memory,0x46510);
        prepare_menu();require(saved && state.host.loop_track && memory.load<std::uint32_t>(rows+64)==1,"confirmed loop option survives reopening");
        memory.store<std::uint32_t>(rows+64,0);title_music(cpu,memory,0x46510);
        require(!saved && !state.host.loop_track,"confirmed off restores ordinary playback");
        const auto audio=menu+0x2400;
        cpu.registers[ecx]=audio;invoke(0x4B7B0,{});
        require(memory.load<std::uint32_t>(audio+0xE8)==2 && memory.load<std::uint32_t>(audio+0xEC)==audio+0x108,
            "front-end Audio retains its original two volume rows");
        const auto pause=state.allocate(0x3000,page,true,PAGE_READWRITE);
        require(pause!=0,"bounded race pause menu storage");
        const auto language=memory.load<std::uint32_t>(0x35272C+memory.load<std::uint32_t>(0x352728)*4);
        const auto label=[&](unsigned index) {return memory.load<std::uint32_t>(language+index*4);};
        memory.store<std::uint32_t>(pause+0x80,5);memory.store<std::uint32_t>(pause+0x84,pause+0xA0);
        for(unsigned i=0;i<2;++i) {
            const std::array<std::uint32_t,6> volume{label(i?218:219),1,0,0,i?12U:9U,18};
            std::memcpy(memory.access(pause+0xA0+i*24,sizeof(volume)),volume.data(),sizeof(volume));
        }
        memory.store<std::uint32_t>(pause+0x74,label(359));memory.store<std::uint32_t>(pause+0x78,label(360));
        const std::array<std::uint32_t,6> vibration{label(221),0,0,pause+0x74,0,2};
        std::memcpy(memory.access(pause+0x100,sizeof(vibration)),vibration.data(),sizeof(vibration));
        cpu.registers[ecx]=pause+0x208;invoke(0x54F70,{pause+0x80,pause+0xA0,2,3});
        cpu.registers[esi]=pause;title_music(cpu,memory,0x4788F);
        const auto pause_rows=memory.load<std::uint32_t>(pause+0x84);
        require(memory.load<std::uint32_t>(pause+0x80)==6 && pause_rows==rows+0x100 &&
            memory.load<std::uint32_t>(pause+0x216C)==pause_rows,"race pause view and music selector share all six rows");
        require(memory.load<std::uint32_t>(pause_rows+48)==label(881) && memory.load<std::uint32_t>(pause_rows+72)==label(882) &&
            memory.load<std::uint32_t>(pause_rows+96)==memory.load<std::uint32_t>(rows+48) && memory.load<std::uint32_t>(pause_rows+120)==label(221),
            "race Options order is Soundtrack, Track, Loop Track, Vibration");
        memory.store<std::uint32_t>(pause+0x88,3);cpu.registers[ecx]=pause+0x80;
        require(invoke(0x419A0,{})==1 && memory.load<std::uint32_t>(pause+0x88)==4,"native Down reaches Loop Track after Track");
        const auto apply_pause=[&] {cpu.registers[ecx]=pause;title_music(cpu,memory,0x47220);};
        cpu.registers[ecx]=pause_rows+96;require(invoke(0x45F60,{})==1 && !state.host.loop_track,"pause loop edits await confirmation");
        cpu.registers[esi]=pause;title_music(cpu,memory,0x46EDA);
        require(!memory.load<std::uint32_t>(pause_rows+112) && !saved,"reopening cancels an unconfirmed pause loop change");
        cpu.registers[ecx]=pause_rows+96;require(invoke(0x45F60,{})==1,"pause loop uses original option navigation");apply_pause();
        require(saved && state.host.loop_track,"confirming pause Options saves Loop Track");
        prepare_menu();require(memory.load<std::uint32_t>(rows+64)==1,"Music Selection reflects pause loop change");
        cpu.registers[ecx]=pause_rows;require(invoke(0x45F60,{})==1,"pause music volume remains adjustable");
        cpu.registers[ecx]=pause;title_music(cpu,memory,0x47190);
        require(memory.load<std::uint32_t>(pause+0xB0)==10 && memory.load<std::uint32_t>(pause+0xC8)==12,"original pause volume fields follow the extended menu");
        cpu.registers[ecx]=pause+0x80;require(invoke(0x419A0,{})==1 && memory.load<std::uint32_t>(pause+0x88)==5,"native Down reaches Vibration after Loop Track");
        cpu.registers[ecx]=pause+0x80;require(invoke(0x419A0,{})==0,"native Down stops at the sixth row");
        cpu.registers[ecx]=pause_rows+120;require(invoke(0x45F60,{})==1,"Vibration retains its native toggle");apply_pause();
        require(memory.load<std::uint32_t>(pause+0x110)==1,"original vibration confirmation follows its relocated row");
        cpu.registers[ecx]=pause_rows+96;require(invoke(0x45F40,{})==1,"pause left-arrow disables loop");apply_pause();
        require(!saved && !state.host.loop_track,"both menus share the saved off setting");
        state.host.music_loop_changed={};
        const auto table=memory.load<std::uint32_t>(manager+4),count=memory.load<std::uint32_t>(manager+0x10);
        unsigned sources=0;
        for(unsigned list=0;list<count;++list) {
            const auto record=table+list*68,track_count=memory.load<std::uint32_t>(record+0x38);
            if(!track_count) continue;
            ++sources;memory.store<std::uint32_t>(0x57C210,1);memory.store<std::uint32_t>(0x57C1EC,list);
            for(const bool repeat:{false,true}) for(const auto index:std::array<unsigned,3>{0,track_count/2,track_count-1}) {
                state.host.loop_track=repeat;memory.store<std::uint32_t>(0x57C208,index);
                const auto playlist=state.allocate(page,page,true,PAGE_READWRITE);
                cpu.registers[ecx]=playlist;invoke(0x20FEF1,{});memory.store<std::uint32_t>(playlist+0x58,2);
                memory.store<std::uint32_t>(0x57C1F4,playlist);
                const bool included=memory.load<std::uint32_t>(record+0x10)!=0;
                if(included) {
                    for(unsigned song_index=0;song_index<12;++song_index) {
                        memory.store<std::uint32_t>(descriptor,2);
                        memory.store<std::uint32_t>(descriptor+4,memory.load<std::uint32_t>(0x3402D8+song_index*4));
                        invoke(0x20DDB8,{playlist,descriptor,0});
                    }
                } else {
                    memory.fill_elements<std::uint32_t>(descriptor,0,5,false);memory.store<std::uint32_t>(descriptor,3);
                    memory.store<std::uint32_t>(descriptor+8,memory.load<std::uint32_t>(record+0x14));
                    invoke(0x20DDB8,{playlist,descriptor,0});
                }
                require(memory.load<std::uint32_t>(playlist+0x60)==track_count,"loop retains the entire original playlist");
                const auto next_song=[&](std::uint32_t caller) {
                    cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,caller);memory.store<std::uint32_t>(stack+4,playlist);
                    invoke_native(cpu,memory,0x20DDBD);
                    require(cpu.eip==caller && cpu.registers[esp]==stack+8,"original Next preserves its caller ABI");
                };
                for(unsigned selection=0;selection<=index;++selection) next_song(0xC931C);
                const auto first=memory.load<std::uint32_t>(playlist+0x18);
                require(included?!std::strcmp(static_cast<const char*>(memory.access(first+0x10,1)),
                    static_cast<const char*>(memory.access(memory.load<std::uint32_t>(0x3402D8+index*4),1))):
                    memory.load<std::uint32_t>(first+0x10)==memory.load<std::uint32_t>(memory.load<std::uint32_t>(record+4)+index*36),
                    "initial selection reaches the requested song with loop on or off");
                title_music(cpu,memory,0xC9333);
                require(!title_music(cpu,memory,0xCA348),"initial song notification advances the original index");
                for(unsigned end=0;end<40;++end) {
                    next_song(0x2142DD);
                    require(memory.load<std::uint32_t>(playlist+0x18)==first || !repeat,"original playlist keeps repeating the selected song");
                    if(!repeat && end==0) require(memory.load<std::uint32_t>(playlist+0x18)!=first,"turning loop off advances to another song");
                    require(title_music(cpu,memory,0xCA348)==repeat,"song notifications follow actual repeat transitions");
                }
                const auto unchanged=[&](auto&& toggle) {
                    std::array<std::byte,0xA0> queue{};std::memcpy(queue.data(),memory.access(playlist,queue.size()),queue.size());
                    toggle();
                    require(memory.load<std::uint32_t>(0x57C1F4)==playlist &&
                        !std::memcmp(queue.data(),memory.access(playlist,queue.size()),queue.size()),"toggling preserves the active playlist, song and decoder handle");
                };
                unchanged([&] {memory.store<std::uint32_t>(rows+64,!repeat);title_music(cpu,memory,0x46510);});
                auto current=memory.load<std::uint32_t>(playlist+0x18);next_song(0x2142DD);
                require((memory.load<std::uint32_t>(playlist+0x18)==current)==!repeat,"Music Selection toggle affects the next EOF without restarting");
                require(title_music(cpu,memory,0xCA348)==!repeat,"pending notifications use the repeat that actually occurred");
                unchanged([&] {memory.store<std::uint32_t>(pause_rows+112,repeat);apply_pause();});
                current=memory.load<std::uint32_t>(playlist+0x18);next_song(0x2142DD);
                require((memory.load<std::uint32_t>(playlist+0x18)==current)==repeat,"pause toggle resumes repeat or ordinary advancement at EOF");
                state.host.loop_track=!repeat;
                require(title_music(cpu,memory,0xCA348)==repeat,"toggling before a pending notification preserves its song index");
                state.host.loop_track=true;current=memory.load<std::uint32_t>(playlist+0x18);next_song(0xC6128);
                require(memory.load<std::uint32_t>(playlist+0x18)!=current,"explicit Next can still select a new song");
                memory.store<std::uint32_t>(0x57C1F4,0);current=memory.load<std::uint32_t>(playlist+0x18);next_song(0x2142DD);
                require(memory.load<std::uint32_t>(playlist+0x18)!=current,"loop does not affect other audio playlists");
            }
        }
        require(sources==2,"both B1 and custom soundtrack loop paths exercised");
    }
    const auto report=std::format("{{\"format\":\"b2-custom-music-check-v1\",\"passed\":true,\"game_booted\":false,\"audio_device_opened\":false,\"songs\":{},\"cases\":{},\"decoded_frames\":{},\"decoded_ms\":{},\"catalogue_ms\":{},\"rate\":44100,\"peak\":{},\"rms\":{},\"cancel_preserves_library\":{},\"original_soundtrack_apis\":true,\"original_wma_decoder\":true}}",songs,checks,decoded_frames,decoded_ms,first_duration,peak,std::sqrt(energy/(decoded_frames*2)),fixture?"true":"null");
    write_text(output/"custom-music.json",report,false);return report;
}
std::string check_music(const std::filesystem::path& executable,const std::filesystem::path& disc_path,const std::filesystem::path& output) {
    Xbe image(executable);
    if(!image.supported() || std::filesystem::exists(output)) throw std::runtime_error("Music checks require the verified XBE and a fresh output directory");
    std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
    XboxHost provider;provider.storage_root=output;Xbox xbox(image,memory,ram,disc_path,std::move(provider));auto& state=*xbox.state_;
    auto& thread=state.create(0,0,0,image.stack_commit,0,false);state.current=&thread;
    auto& cpu=thread.cpu;cpu.preempt=nullptr;state.diagnostic.remaining=2000000;diagnostic_guest(&cpu);
    const auto stack=cpu.registers[esp]-4096;
    const auto invoke=[&](std::uint32_t address,std::initializer_list<std::uint32_t> arguments) {
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
        unsigned index=0;for(auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
        try {invoke_native(cpu,memory,address);}
        catch(const std::exception& error) {
            std::ostringstream failure;
            failure<<"{\"type\":\"music_check_failure\",\"call\":"<<json(hex32(address))<<",\"eip\":"<<json(hex32(cpu.eip))
                <<",\"error\":"<<json(error.what())<<",\"registers\":[";
            for(unsigned i=0;i<8;++i) failure<<(i?",":"")<<json(hex32(cpu.registers[i]));
            failure<<"],\"stack\":[";
            for(unsigned i=0;i<16 && memory.try_access(cpu.registers[esp]+i*4,4);++i)
                failure<<(i?",":"")<<json(hex32(memory.load<std::uint32_t>(cpu.registers[esp]+i*4)));
            failure<<"]}";diagnostic_record(failure.str());write_text(output/"failure.json",failure.str(),false);throw;
        }
        if(cpu.eip!=return_sentinel || cpu.registers[esp]!=stack+4+arguments.size()*4)
            throw std::runtime_error("Music check calling convention differs at "+hex32(address));
        return cpu.registers[eax];
    };
    const auto require=[](bool passed,std::string_view name) {if(!passed) throw std::runtime_error("Music check failed: "+std::string(name));};
    memory.store<std::uint32_t>(0xA000,0x30);
    const auto heap=invoke(0x000E435A,{2,0,image.heap_reserve,image.heap_commit,0,0xA000});
    require(heap!=0,"original SDK heap initialization");memory.store<std::uint32_t>(0x005A8974,heap);
    state.audio=std::make_unique<Audio>(false);state.audio_device=0xA008;
    constexpr std::uint32_t descriptor=0xB000,path=0xB100,result=0xB200,info=0xB240,format=0xB280,stream_descriptor=0xB300,packets=0xB400;
    const auto buffers=state.allocate(65536*2,page,true,PAGE_READWRITE);require(buffers!=0,"bounded packet buffers");
    std::ostringstream tracks;
    for(unsigned track=1;track<=12;++track) {
        const auto name=std::format("D:\\music1\\b1wma\\{:02}.wma",track);std::memcpy(memory.access(path,name.size()+1),name.c_str(),name.size()+1);
        diagnostic_record("{\"type\":\"music_check_track\",\"path\":"+json(name)+'}');state.diagnostic.remaining=2000000;
        // XACT 213A72 uses this original WMA file decoder. Its descriptor is
        // {filename, existing_handle, offset, lookahead_bytes}; use the real
        // file provider and its asynchronous reads, without game startup.
        memory.fill_elements<std::uint32_t>(descriptor,0,4,false);
        memory.store<std::uint32_t>(descriptor,path);memory.store<std::uint32_t>(descriptor+12,0x20000);
        require(invoke(0x0024C13F,{descriptor,result})==0,"original WMA file creation");
        const auto source=memory.load<std::uint32_t>(result),vtable=memory.load<std::uint32_t>(source);
        const auto ready=[&] {
            const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
            do {
                state.dispatch_io();invoke(memory.load<std::uint32_t>(vtable+0x24),{source});
                require(invoke(memory.load<std::uint32_t>(vtable+12),{source,result})==0,"original WMA status");
                if(memory.load<std::uint32_t>(result)&2) return;
                Sleep(1);
            } while(std::chrono::steady_clock::now()<deadline);
            throw std::runtime_error("Music check timed out waiting for original WMA input");
        };
        ready();require(invoke(memory.load<std::uint32_t>(vtable+8),{source,info})==0,"original WMA packet info");
        const auto parser=memory.load<std::uint32_t>(source+0x30),rate=memory.load<std::uint32_t>(parser+0x2C);
        const auto channels=memory.load<std::uint16_t>(parser+0x38),alignment=static_cast<std::uint16_t>(channels*2);
        const auto packet_bytes=memory.load<std::uint32_t>(info+8);
        require((channels==1 || channels==2) && rate>=8000 && rate<=48000 && packet_bytes && packet_bytes<=65536 && !(packet_bytes%alignment),"decoded PCM format and packet bounds");
        memory.store<std::uint16_t>(format,1);memory.store<std::uint16_t>(format+2,channels);memory.store<std::uint32_t>(format+4,rate);
        memory.store<std::uint32_t>(format+8,rate*alignment);memory.store<std::uint16_t>(format+12,alignment);memory.store<std::uint16_t>(format+14,16);
        memory.fill_elements<std::uint32_t>(stream_descriptor,0,6,false);memory.store<std::uint32_t>(stream_descriptor,0x20000000);
        memory.store<std::uint32_t>(stream_descriptor+4,2);memory.store<std::uint32_t>(stream_descriptor+8,format);
        require(invoke(0x0022F90E,{state.audio_device,stream_descriptor,result,0})==0,"native packet stream creation");
        const auto stream=memory.load<std::uint32_t>(result);memory.fill_elements<std::uint32_t>(packets,0,32,false);
        std::vector<float> mixed(96000*2);std::uint64_t decoded_bytes=0;unsigned submissions=0;
        const auto started=std::chrono::steady_clock::now();
        for(unsigned frame=0;frame<96000;frame+=512) {
            for(unsigned slot=0;slot<2;++slot) {
                const auto packet=packets+slot*64,completed=packet+24,status=packet+28;
                if(memory.load<std::uint32_t>(status)==0x8000000A) continue;
                ready();memory.store<std::uint32_t>(packet,buffers+slot*65536);memory.store<std::uint32_t>(packet+4,packet_bytes);
                memory.store<std::uint32_t>(packet+8,completed);memory.store<std::uint32_t>(packet+12,status);
                require(invoke(memory.load<std::uint32_t>(vtable+16),{source,0,packet})==0 && memory.load<std::uint32_t>(status)==0,"original WMA decoding");
                const auto produced=memory.load<std::uint32_t>(completed);require(produced && produced<=packet_bytes && !(produced%alignment),"decoded packet completion");
                decoded_bytes+=produced;++submissions;memory.store<std::uint32_t>(packet+4,produced);
                require(invoke(0x0022CDA9,{stream,packet,0})==0,"native PCM packet submission");
            }
            state.audio->mix(std::span(mixed).subspan(frame*2,std::min(512U,96000-frame)*2));invoke(0x0022D9CA,{});
        }
        double energy=0;float peak=0;for(float value:mixed) {require(std::isfinite(value),"finite mixed output");energy+=double(value)*value;peak=std::max(peak,std::abs(value));}
        require(peak>0,"audible music output");save_audio_wave(output/std::format("burnout1-{:02}.wav",track),mixed);
        if(track>1) tracks<<',';
        tracks<<std::format("{{\"track\":{},\"rate\":{},\"channels\":{},\"frames\":96000,\"packets\":{},\"decoded_bytes\":{},\"peak\":{},\"rms\":{},\"processing_ms\":{}}}",
            track,rate,channels,submissions,decoded_bytes,peak,std::sqrt(energy/mixed.size()),std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count());
        require(invoke(0x0022D9AD,{stream,0,0,5})==0,"original XACT asynchronous envelope stop adapter");invoke(0x0022D9CA,{});
        require(state.audio->packet_status(state.audio_streams.at(stream).identifier)==0x80001,"music stream envelope completion");
        require(std::ranges::all_of(state.audio_streams.at(stream).packets,[](const auto& packet){return !packet[0];}),"music stop packet publication");
        require(invoke(0x0022CC0B,{stream})==0,"native stream release");invoke(memory.load<std::uint32_t>(vtable+4),{source});
    }
    const auto report=std::format("{{\"format\":\"b2-music-check-v1\",\"passed\":true,\"game_booted\":false,\"audio_device_opened\":false,\"runtime_decoder\":false,\"tracks\":[{}]}}",tracks.view());
    write_text(output/"music.json",report,false);return report;
}
std::string check_visibility(const std::filesystem::path& executable,const std::filesystem::path& output,bool warp) {
    Xbe image(executable);
    if(!image.supported()) throw std::runtime_error("Visibility checks require the verified XBE");
    if(hex_bytes(image.data(0x2168B3,6))!="C700C8170800" || hex_bytes(image.data(0x216972,6))!="C700CC170800" ||
       hex_bytes(image.data(0x215851,7))!="8B48088B542408")
        throw std::runtime_error("Original visibility query layout differs from its verified bytes");
    if(std::filesystem::exists(output)) throw std::runtime_error("Visibility output directory already exists; choose a fresh directory");
    std::filesystem::create_directories(output);
    unsigned checks=0;
    const auto require=[&](bool condition,std::string_view name) {
        if(!condition) throw std::runtime_error("Visibility check failed: "+std::string(name));++checks;
    };
    std::string results;
    for(const auto config:std::array<std::array<unsigned,2>,4>{{{1,0},{3,0},{1,5},{2,6}}}) {
        const auto scale=config[0],aa=config[1];
        Renderer renderer(64,64,warp,render_debug_layer,scale);renderer.enhancements(aa,0);
        std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
        XboxHost provider;provider.storage_root=output/std::format("storage-{}-{}",scale,aa);
        Xbox xbox(image,memory,ram,{},std::move(provider));auto& state=*xbox.state_;
        auto& thread=state.create(0,0,0,image.stack_commit,0,false);state.current=&thread;
        auto& cpu=thread.cpu;cpu.preempt=nullptr;state.diagnostic.remaining=2000000;
        const auto stack=cpu.registers[esp]-4096;
        const auto invoke=[&](std::uint32_t address,std::initializer_list<std::uint32_t> arguments) {
            cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
            unsigned index=0;for(const auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
            invoke_native(cpu,memory,address);
            require(cpu.eip==return_sentinel && cpu.registers[esp]==stack+4+arguments.size()*4,"original SDK calling convention");
            return cpu.registers[eax];
        };
        // Reproduce the competing unrestricted graphics allocation before the
        // SDK requests a report page with a 24-bit DMA address limit.
        const auto graphics=invoke(0xFFF00000U+166*16,{8*1024*1024,0,UINT32_MAX,65536,PAGE_READWRITE});
        require(graphics && (graphics&0x0FFFFFFF)>=0x1000000 && !(graphics&65535),"unrestricted contiguous storage preserves low DMA addresses");
        const auto commands=state.allocate(65536,4096,true,PAGE_READWRITE);
        require(commands!=0,"bounded native command storage");
        constexpr std::uint32_t context=0x2256C0,notification=0xC000;
        memory.store<std::uint32_t>(0x2256B8,context);memory.store<std::uint32_t>(context+0x2C,1);
        memory.store<std::uint32_t>(context,commands);memory.store<std::uint32_t>(context+4,commands+65536);
        // The previous policy left ordinary heaps growing up through the report
        // DMA range. Execute the original failing End call, then free that heap
        // and repeat under the same pressure through the corrected kernel APIs.
        constexpr unsigned heap_size=20*1024*1024;
        const auto old_heap=state.allocate(dma_window-state.heap_begin,4096,true,PAGE_READWRITE,0,0,dma_window-1);
        require(old_heap!=0,"reproduce earlier low-address heap placement");
        require(invoke(0x216940,{0})==0x8007000E && memory.load<std::uint32_t>(context)==commands,
            "earlier heap policy prevents the original SDK from emitting its report");
        memory.store<std::uint32_t>(0x7000,old_heap);memory.store<std::uint32_t>(0x7004,0);
        require(invoke(0xFFF00000U+199*16,{0x7000,0x7004,MEM_RELEASE})==0,"release only the reproduced old heap");
        memory.store<std::uint32_t>(0x7000,0);memory.store<std::uint32_t>(0x7004,heap_size);
        require(invoke(0xFFF00000U+184*16,{0x7000,0,0x7004,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE})==0 &&
            memory.load<std::uint32_t>(0x7000)>=0x1000000,"unrestricted ordinary heap preserves low DMA addresses");
        const auto pool=invoke(0xFFF00000U+15*16,{4*1024*1024,0});
        require(pool && (pool&0x0FFFFFFF)>=0x1000000,"unrestricted pool preserves low DMA addresses");
        Gpu gpu(renderer,memory,notification);
        const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values) {
            auto cursor=memory.load<std::uint32_t>(context);
            require(cursor+4+values.size()*4<=commands+65536,"bounded GPU packets");
            memory.store<std::uint32_t>(cursor,method|(static_cast<std::uint32_t>(values.size())<<18));cursor+=4;
            for(const auto value:values) {memory.store<std::uint32_t>(cursor,value);cursor+=4;}
            memory.store<std::uint32_t>(context,cursor);
        };
        packet(0,{0xD});packet(0x1A8,{12});packet(0x1A4,{8});packet(0x1D6C,{0});
        packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x20000,0x30000});
        packet(0x1D98,{63U<<16,63U<<16});packet(0x1D8C,{0xFFFFFF00,0xFF303030});
        packet(0x30C,{1});packet(0x350,{0x8006,0x201,0x01010101,1,255,0x207,0,255,0x1E00,0x1E00,0x1E00});packet(0x39C,{0x405,0x901});
        packet(0x288,{4,0x1400});packet(0x1E94,{0});packet(0x1760,{0x0C32});
        packet(0xAF0,{0x42000000,0xC2000000,0x3F800000,0});packet(0xA20,{0x42022000,0x42022000,0,0});
        packet(0x680,{0x42000000,0,0,0,0,0xC2000000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
        packet(0x1A30,{0x3F800000,0x3F800000,0x3F800000,0x3F800000});
        // The exact XYZRHW program in captured draw 647 (SDK 00223108).
        const auto& probe_program=screen_vertex_programs()[0];
        require(std::memcmp(image.data(0x223108,192).data(),probe_program.tokens.data(),192)==0,"captured sun-probe vertex program matches original bytes");
        packet(0x1E9C,{0,0});
        for(const auto& token:std::span(probe_program.tokens).first(12)) packet(0xB00,{token[0],token[1],token[2],token[3]});
        const auto rectangle=[&](unsigned index,float left,float right,float top,float bottom,float z) {
            const std::array<std::array<float,3>,4> vertices{{{left,top,z},{right,top,z},{right,bottom,z},{left,bottom,z}}};
            const auto address=0x8000+index*64;
            std::memcpy(memory.access(address,sizeof(vertices)),vertices.data(),sizeof(vertices));
            packet(0x1720,{address});packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
        };
        // Queue enough real SDK reports to exercise pool reuse, reset ordering,
        // full/partial occlusion and completion before a following semaphore.
        constexpr unsigned reports=96;
        for(unsigned i=0;i<reports;++i) {
            packet(0x304,{0});
            packet(0x1E94,{0});packet(0x1760,{0x0C32});packet(0xAF8,{0x3F800000});
            packet(0x680,{0x42000000,0,0,0,0,0xC2000000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
            packet(0x35C,{1});packet(0x358,{0x01010101});packet(0x1D94,{0xF3});
            if(i%3) rectangle(2*i,-1,i%3==1?0.0f:1.0f,1,-1,0.25f);
            invoke(0x2168A0,{});
            // Alternate the original colourless sun probe with an equivalent
            // colour-writing affine pass. Its extra layer draws must not
            // enter the original SDK's pixel count.
            packet(0x35C,{0});packet(0x358,{i&1?0x01010101U:0U});packet(0x304,{i&1});packet(0x344,{0x302,0x303});
            packet(0x1E94,{2});packet(0x1760,{0x1042});packet(0xAF8,{0x4B7FFFFF});
            packet(0x1EA4,{0});packet(0xB80,{0x3F800000,0x3F800000,0x4B7FFFFF,0x3F800000,0x3F080000,0x3F080000,0,0});
            const std::array<Float4,4> probe{{{25,25,0.999f,0},{39,25,0.999f,0},{39,39,0.999f,0},{25,39,0.999f,0}}};
            const auto probe_address=0x8040+i*128;
            std::memcpy(memory.access(probe_address,sizeof(probe)),probe.data(),sizeof(probe));
            packet(0x1720,{probe_address});packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
            require(invoke(0x216940,{i})==0,"original EndVisibilityTest allocates its constrained report page");
            memory.store<std::uint32_t>(context+0x2C,i+2);
            require(invoke(0x215810,{i,0xB000,0})==0x88760828,"unsubmitted report remains pending");
            packet(0x1D70,{i+1});
        }
        const auto report_page=memory.load<std::uint32_t>(context+0x3BC);
        require(report_page && (report_page&0x0FFFFFFF)+4096<=0x1000000,"report allocation fits the real 24-bit DMA limit");
        gpu.submit(commands,memory.load<std::uint32_t>(context));gpu.wait();
        require(memory.load<std::uint32_t>(notification)==reports,"following semaphore publishes after all reports");
        for(unsigned i=0;i<reports;++i) {
            const auto expected=i%3==0?196U:i%3==1?98U:0U;
            require(invoke(0x215810,{i,0xB000,0xB008})==0,"original GetVisibilityTestResult completes");
            const auto pixels=memory.load<std::uint32_t>(0xB000);
            if(pixels!=expected) throw std::runtime_error(std::format("Sun probe {} at scale {} AA {} returned {} pixels, expected {}",i,scale,aa,pixels,expected));
            ++checks;
            require(memory.load<std::uint64_t>(0xB008)>0,"completed report carries a real timestamp");
        }
        require(renderer.errors().empty(),"D3D11 debug validation");
        require(renderer.stats().shader_compilations==0,"original sun shader was compiled ahead of time");
        gpu.snapshot();save_png(renderer.readback(),output/std::format("probe-{}-{}.png",scale,aa));
        if(!results.empty()) results+=',';
        results+=std::format("{{\"scale\":{},\"aa\":{},\"reports\":{},\"visible_pixels\":196,\"half_visible_pixels\":98,\"blocked_pixels\":0,\"debug_messages\":0}}",scale,aa,reports);
    }
    const auto report=std::format("{{\"format\":\"b2-visibility-check-v1\",\"source_sha256\":{},\"passed\":true,\"game_booted\":false,\"warp\":{},\"checks\":{},\"configurations\":[{}]}}",json(image.sha256),warp,checks,results);
    write_text(output/"report.json",report,false);return report;
}
std::string check_storage(const std::filesystem::path& executable,const std::filesystem::path& output) {
    Xbe image(executable);
    if(!image.supported() || std::filesystem::exists(output)) throw std::runtime_error("Storage checks require the verified XBE and a fresh output directory");
#ifdef __APPLE__
    const bool graphics_checked=b2_metal_device().supported;
#else
    constexpr bool graphics_checked=true;
#endif
    if(hex_bytes(image.data(0x00018390,17))!="E81A7D0C000FB6C083E001A3F8034D00C3" ||
       hex_bytes(image.data(0x000183B0,17))!="A1F8034D0050B970853D00E840030A00C3" ||
       hex_bytes(image.data(0x000B8713,6))!="C700ABAAAA3F" ||
       hex_bytes(image.data(0x000B8739,6))!="C700398E633F" ||
       hex_bytes(image.data(0x000B8741,6))!="C700398EE33F")
        throw std::runtime_error("Original widescreen selection differs from its verified layout");
    std::filesystem::create_directories(output);
    unsigned checks=0;
    const auto require=[&](bool passed,std::string_view name) {
        if(!passed) throw std::runtime_error("Storage check failed: "+std::string(name));
        ++checks;
    };
    const auto reference=[&](Bytes key,Bytes bytes) {
#ifdef _WIN32
        BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
        struct Scope {BCRYPT_ALG_HANDLE& algorithm;BCRYPT_HASH_HANDLE& hash;
            ~Scope(){if(hash) BCryptDestroyHash(hash);if(algorithm) BCryptCloseAlgorithmProvider(algorithm,0);}} scope{algorithm,hash};
        const auto check=[](NTSTATUS status){if(status<0) throw std::runtime_error("Storage reference hash failed");};
        check(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA1_ALGORITHM,nullptr,key.empty()?0:BCRYPT_ALG_HANDLE_HMAC_FLAG));
        check(BCryptCreateHash(algorithm,&hash,nullptr,0,reinterpret_cast<PUCHAR>(const_cast<std::byte*>(key.data())),static_cast<ULONG>(key.size()),0));
        if(!bytes.empty()) check(BCryptHashData(hash,reinterpret_cast<PUCHAR>(const_cast<std::byte*>(bytes.data())),static_cast<ULONG>(bytes.size()),0));
        ShaDigest digest{};check(BCryptFinishHash(hash,reinterpret_cast<PUCHAR>(digest.data()),static_cast<ULONG>(digest.size()),0));return digest;
#else
        ShaDigest digest{};unsigned size=0;
        if(key.empty()) {
            if(EVP_Digest(bytes.data(),bytes.size(),reinterpret_cast<unsigned char*>(digest.data()),&size,EVP_sha1(),nullptr)!=1)
                throw std::runtime_error("Storage reference hash failed");
        } else if(!HMAC(EVP_sha1(),key.data(),static_cast<int>(key.size()),reinterpret_cast<const unsigned char*>(bytes.data()),bytes.size(),reinterpret_cast<unsigned char*>(digest.data()),&size))
            throw std::runtime_error("Storage reference HMAC failed");
        if(size!=digest.size()) throw std::runtime_error("Storage reference digest length differs");return digest;
#endif
    };
    std::array<std::byte,128> input{};
    for(unsigned i=0;i<input.size();++i) input[i]=static_cast<std::byte>(i*73+19);
    for(unsigned size:{0U,3U,55U,56U,63U,64U,65U,128U}) {
        Sha1 sha;sha.reset();sha.update(Bytes(input).first(size/2));auto clone=sha;
        sha.update(Bytes(input).subspan(size/2,size-size/2));clone.update(Bytes(input).subspan(size/2,size-size/2));
        const auto expected=reference({},Bytes(input).first(size));
        require(sha.finish()==expected && clone.finish()==expected,"SHA partial blocks and copied context");
    }
    require(xbox_hmac(input,Bytes(input).first(7),Bytes(input).subspan(7,13))==reference(Bytes(input).first(64),Bytes(input).first(20)),"Xbox HMAC two inputs and long-key truncation");
    std::string saved_directory;
    std::array<std::byte,32> saved_keys{};
    for(unsigned reload=0;reload<3;++reload) {
        std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
        XboxHost provider;provider.storage_root=output;provider.widescreen=reload==1;
        Xbox xbox(image,memory,ram,{},std::move(provider));auto& state=*xbox.state_;
        auto& thread=state.create(0,0,0,image.stack_commit,0,false);state.current=&thread;
        auto& cpu=thread.cpu;cpu.preempt=nullptr;state.diagnostic.remaining=2000000;
        const auto stack=cpu.registers[esp]-4096;
        const auto invoke=[&](std::uint32_t address,std::initializer_list<std::uint32_t> arguments) {
            cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
            unsigned index=0;for(auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
            invoke_native(cpu,memory,address);
            require(cpu.eip==return_sentinel && cpu.registers[esp]==stack+4+arguments.size()*4,"original SDK calling convention");
            return cpu.registers[eax];
        };
        constexpr std::uint32_t area=0xA000,profile=0x1000,profile_size=0x7C04,signed_size=0x7AB8,aligned_size=0x7E00;
        if(!reload) {
            memory.store<std::uint64_t>(area,3375000);
            require(invoke(0x00288226,{area})==1000000 && cpu.registers[edx]==0,"original timer converts one ACPI second to one million microseconds");
            const auto epoch=state.started;state.started=std::chrono::steady_clock::now()-std::chrono::seconds(1);
            require(invoke(0xFFF00000U+127*16,{})==3375000 && cpu.registers[edx]==0,"original kernel ACPI performance frequency");
            invoke(0x00288214,{area});const auto counter=memory.load<std::uint64_t>(area);
            const auto timestamp=read_timestamp(cpu);state.started=epoch;
            require(counter>=3375000 && counter<6750000 && timestamp>=733333333 && timestamp<1466666666,
                "kernel ACPI counter and original CPU timestamp retain separate clock rates");
            const bool convert=!IsThreadAFiber();
            state.scheduler=convert?ConvertThreadToFiberEx(nullptr,FIBER_FLAG_FLOAT_SWITCH):GetCurrentFiber();
            require(state.scheduler!=nullptr,"isolated native thread scheduler");
            struct SchedulerScope {
                Xbox::State& host;Xbox::State::Thread* previous;bool converted;
                ~SchedulerScope(){host.current=previous;host.scheduler=nullptr;if(converted) ConvertFiberToThread();}
            } scheduler_scope{state,&thread,convert};
            const auto allocations=state.allocations.size();
            std::array<std::uint32_t,68> completed_handles{};
            for(unsigned cycle=0;cycle<70;++cycle) {
                // Run the original XGetVideoFlags routine on real native fibers;
                // no game boot or replacement guest callback is involved.
                require(invoke(0xFFF00000U+255*16,{area,0,16384,0,0,0x000E00AF,0,0,0,0})==success,"native worker creation");
                const auto handle=memory.load<std::uint32_t>(area);auto* worker=state.handles.at(handle).thread;
                require(u16(worker->cpu.floating.image,0)==0x027F && worker->cpu.floating.mxcsr==0x1F80,
                    "Xbox worker starts with the kernel's 53-bit x87 precision and masked SSE exceptions");
                const auto object=worker->object,worker_stack=worker->stack;
                if(cycle==1) require(invoke(0xFFF00000U+246*16,{handle,state.thread_type,area+8})==success,"worker object reference");
                if(cycle==1) require(invoke(0xFFF00000U+187*16,{handle})==success,"close a running worker handle");
                require(state.allocation(object)!=state.allocations.end() && memory.load<std::uint8_t>(object)==6,"running worker retains its dispatcher storage");
                worker->cpu.preempt=nullptr;state.current=worker;SwitchToFiber(worker->fiber);state.current=&thread;
                require(worker->state==Xbox::State::ThreadState::ended && state.boundary.empty(),"original worker routine exits");
                state.collect_threads();
                if(cycle==0) {
                    require(state.allocation(object)!=state.allocations.end() && memory.load<std::uint32_t>(object+4)==1 &&
                        !worker->fiber && state.allocation(worker_stack)==state.allocations.end(),"terminated worker retains its signalled object without retaining unused stacks");
                    memory.store<std::uint64_t>(area+16,0);
                    require(invoke(0xFFF00000U+233*16,{handle,0,area+16})==success,"wait for a terminated worker");
                    require(invoke(0xFFF00000U+187*16,{handle})==success,"close the final worker handle");
                } else if(cycle==1) {
                    require(state.allocation(object)!=state.allocations.end(),"explicit reference retains a terminated worker");
                    cpu.registers[ecx]=object;invoke(0xFFF00000U+250*16,{});
                } else {
                    completed_handles[cycle-2]=handle;
                    require(state.allocation(object)!=state.allocations.end() && !worker->fiber && !worker->stack &&
                        memory.load<std::uint32_t>(object+4)==1,"completed handles retain their signalled dispatcher without consuming active contexts");
                }
                state.collect_threads();
                if(cycle<2) require(state.threads.size()==1 && state.allocations.size()==allocations && state.allocation(object)==state.allocations.end(),
                    "terminated worker fibers, stacks and dispatcher storage are reclaimed");
            }
            require(state.threads.size()==completed_handles.size()+1,"more than 64 retained completed threads do not exhaust the live-context limit");
            for(auto handle:completed_handles) require(invoke(0xFFF00000U+187*16,{handle})==success,"close a retained completed worker");
            state.collect_threads();
            require(state.threads.size()==1 && state.allocations.size()==allocations,"all retained completed workers are reclaimed after their handles close");
        }
        const bool widescreen=reload==1;
        invoke(0xFFF00000U+2*16,{0,6,0,area});
        require(memory.load<std::uint32_t>(area)==(widescreen?0x00410101U:0x00400101U),"console video capabilities");
        require(invoke(0x000E00AF,{})==unsigned(widescreen),"original SDK video flags");
        invoke(0x00018390,{});
        require(memory.load<std::uint32_t>(0x004D03F8)==unsigned(widescreen),"original game widescreen selection");
        constexpr std::uint32_t view=0x003D8570,aspect=view+0x6AC84;
        memory.store<std::uint32_t>(view+0x6AC7C,0);invoke(0x000183B0,{});
        const auto expected_aspect=widescreen?0x3FE38E39U:0x3FAAAAABU;
        require(memory.load<std::uint32_t>(aspect)==expected_aspect && memory.load<std::uint32_t>(aspect+0x660)==expected_aspect,
            "original 16:9 and 4:3 camera aspect");
        if(widescreen) {
            memory.store<std::uint32_t>(view+0x6AC7C,2);invoke(0x000183B0,{});
            require(memory.load<std::uint32_t>(aspect)==0x3F638E39U && memory.load<std::uint32_t>(aspect+0x660)==0x3F638E39U,
                "original widescreen split-screen camera aspect");
        }
        const auto text=[&](std::uint32_t address,std::string_view value) {
            std::memcpy(memory.access(address,value.size()),value.data(),value.size());memory.store<std::uint8_t>(address+static_cast<std::uint32_t>(value.size()),0);
        };
        File identity(std::filesystem::absolute(output)/"storage.key");std::array<std::byte,32> keys{};identity.read(0,keys);
        if(!reload) saved_keys=keys;else require(saved_keys==keys,"save identity survives a fresh runtime");
        // Reproduce the SDK's verified process-heap setup at E6075..E60A3 only.
        std::memset(memory.access(area,0x30),0,0x30);memory.store<std::uint32_t>(area,0x30);
        const auto heap=invoke(0x000E435A,{2,0,image.heap_reserve,image.heap_commit,0,area});require(heap!=0,"original process heap");
        memory.store<std::uint32_t>(0x005A8974,heap);
        // The crash at E30A7 waits on this original SDK semaphore wrapper.
        // Keep its header intact while the original heap grows and releases
        // both ordinary blocks and separately reserved large allocations.
        const auto semaphore=invoke(0x000E3009,{0,1,1,0});require(semaphore!=0,"original shared semaphore creation");
        const auto semaphore_address=state.handles.at(semaphore).address;
        // 13590..136FA claims the title's fixed physical buffers. The former
        // descending heap policy occupied them before these SDK calls arrived.
        if(!reload) {
            const auto misplaced=state.allocate(2*1024*1024,page,true,PAGE_READWRITE,0,0,UINT32_MAX,true,true);
            require(misplaced!=0,"reproduce descending ordinary heap placement");
            require(invoke(0x000DFA30,{0x40000,0x3EC0000,0,4})==0,
                "descending ordinary heap prevents the original fixed physical claim");
            state.release_allocation(state.allocations.find(misplaced));
        }
        const auto claim=[&](std::uint32_t size,std::uint32_t address,std::uint32_t protect) {
            require(invoke(0x000DFA30,{size,address,0,protect})==(address|0x80000000U),
                std::format("original fixed physical buffer {}",hex32(address)));
        };
        for(const auto& range:std::array<std::array<std::uint32_t,3>,9>{{
            {0x40000,0x3EC0000,4},{0x64000,0x381C000,4},{0x640000,0x3880000,0x404},
            {0x64000,0x2D10000,4},{0x5D000,0x2877000,4},{0x43C000,0x28D4000,0x404},
            {0x200000,0x2677000,4},{0x200000,0x2477000,4},{0x100000,0x2377000,4}}})
            claim(range[0],range[1],range[2]);
        for(std::uint32_t base=0x31AC000;base<0x383C000;base+=0x3C000) {
            claim(0x20000,base-0x20000,4);claim(0x1C000,base,0x404);
        }
        for(std::uint32_t base=0x2D82000;base<0x319A000;base+=0x106000) {
            claim(0xE000,base-0xE000,4);claim(0xF8000,base,0x404);
        }
        const auto semaphore_intact=[&] {
            require(memory.load<std::uint8_t>(semaphore_address)==5 &&
                memory.load<std::uint32_t>(semaphore_address+16)==1,"shared semaphore survives heap allocation and release");
        };
        std::array<std::uint32_t,48> blocks{};
        for(unsigned i=0;i<blocks.size();++i) {
            const auto size=i%3==0?0x180000U:i%3==1?0x30000U:0x1038U;
            blocks[i]=invoke(0x000E477E,{heap,0,size});require(blocks[i]!=0,"original heap grows under mixed allocation pressure");
            memory.fill_elements<std::uint8_t>(blocks[i],0xA5,size,false);semaphore_intact();
        }
        for(unsigned offset=0;offset<3;++offset) for(unsigned i=offset;i<blocks.size();i+=3) {
            require((invoke(0x000E4F35,{heap,0,blocks[i]})&0xFF)!=0,"original heap releases mixed allocations");semaphore_intact();
        }
        require(invoke(0x000E3120,{semaphore,0})==success && invoke(0x000E3120,{semaphore,0})==timeout,
            "original single wait consumes one semaphore signal");
        require(invoke(0x000E3064,{semaphore,1,0})!=0 && invoke(0x000E3120,{semaphore,0})==success,
            "original shared semaphore release and wait");
        // Validate failure reporting with deliberate damage; this is not a
        // reproduction of the unknown write from the user's lesson-menu crash.
        const auto saved_cpu=cpu;
        memory.store<std::uint8_t>(semaphore_address,0xFF);
        bool rejected=false;
        try {invoke(0x000E3120,{semaphore,0});}
        catch(const NativeBoundary& error) {
            rejected=error.address==0x000E30A7 && std::string_view(error.what()).find(hex32(semaphore_address))!=std::string_view::npos;
        }
        cpu=saved_cpu;memory.store<std::uint8_t>(semaphore_address,5);
        require(rejected,"damaged semaphore stops at the original wait with its address reported");
        require(invoke(0xFFF00000U+187*16,{semaphore})==success,"shared semaphore close");
        require(invoke(0x000E5D08,{image.title_id,0})==success,"original title storage mount");
        text(area,"U:\\");const std::u16string name=u"Offline storage check";
        std::memcpy(memory.access(area+0x40,(name.size()+1)*2),name.c_str(),(name.size()+1)*2);
        require(invoke(0x000E1445,{area,area+0x40,reload?3U:4U,0,area+0x100,260})==0,"original save-directory creation/reopening");
        std::string path;
        for(unsigned i=0;i<260 && memory.load<std::uint8_t>(area+0x100+i);++i) path+=static_cast<char>(memory.load<std::uint8_t>(area+0x100+i));
        require(!path.empty() && (!reload || path==saved_directory),"save metadata identifies the same directory");
        saved_directory=path;
        path+="Profile 1";text(area+0x300,path);
        const auto handle=invoke(0x000E09BA,{area+0x300,reload?GENERIC_READ:GENERIC_READ|GENERIC_WRITE,3,0,reload?3U:2U,0x80,0});
        require(handle!=UINT32_MAX,"original save file open");
        if(!reload) {
            cpu.registers[ecx]=profile;cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
            invoke_native(cpu,memory,0x000D6290);
            require(cpu.eip==return_sentinel && cpu.registers[esp]==stack+4,"original signed player-profile initialization");
        }
        memory.store<std::uint64_t>(area+0x700,0);
        require(invoke(0xFFF00000U+(reload?219U:236U)*16,{handle,0,0,0,area+0x710,profile,profile_size,area+0x700})==success &&
            memory.load<std::uint32_t>(area+0x714)==profile_size,"complete original profile write/read");
        if(reload==1 && graphics_checked) {
            // File reads replace texture bytes without releasing the allocation,
            // just as the original fixed course buffers do. Exercise both APIs.
            Renderer renderer(64,64,false,render_debug_layer);state.gpu=std::make_unique<Gpu>(renderer,memory,0x3EFE000);
            constexpr std::uint32_t texture=0x3880000,positions=0x3EF0000,coordinates=positions+128,commands=positions+256;
            constexpr std::array<Float4,4> vertices{{{-1,1,0.25f,1},{1,1,0.25f,1},{1,-1,0.25f,1},{-1,-1,0.25f,1}}};
            std::memcpy(memory.access(positions,sizeof(vertices)),vertices.data(),sizeof(vertices));
            memory.fill_elements<std::uint32_t>(coordinates,0,16,false);
            for(unsigned i=0;i<4;++i) memory.store<std::uint32_t>(coordinates+i*16+12,0x3F800000);
            std::vector<std::uint32_t> words;
            const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values) {
                words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18));words.insert(words.end(),values);
            };
            const auto paint=[&] {
                packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
                const auto bytes=std::as_bytes(std::span(words));std::memcpy(memory.access(commands,bytes.size()),bytes.data(),bytes.size());
                state.gpu->submit(commands,commands+static_cast<std::uint32_t>(bytes.size()));state.gpu->wait();words.clear();
            };
            packet(0,{0xD});packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x3EC0000,0x3EC4000});
            packet(0x1D98,{63U<<16,63U<<16});packet(0x1D8C,{0xFFFFFF00,0xFF000000});packet(0x1D94,{0xF3});
            packet(0x33C,{0x207,0,1,0});packet(0x350,{0x8006,0x203,0x01010101,0,255,0x207,0,255,0x1E00,0x1E00,0x1E00});
            packet(0x288,{8,0x1800});packet(0x1E70,{1});
            for(unsigned stage=0;stage<4;++stage) packet(0x1B08+stage*0x40,{0x030303});
            packet(0x1B00,{texture,0x02210620,0x030303,0x40000000});packet(0x1B14,{0x01010000});
            packet(0xAF0,{0x3F800000,0x3F800000,0x3F800000,0});packet(0xA20,{0x42000000,0x42000000,0,0});
            packet(0x680,{0x42000000,0,0,0,0,0xC2000000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
            packet(0x1720,{positions});packet(0x1760,{0x1042});packet(0x1744,{coordinates});packet(0x1784,{0x1042});
            const auto expected=memory.load<std::uint32_t>(profile),old=(expected^0x00FFFFFFU)|0xFF000000U;
            const auto verify_pixel=[&](std::uint32_t color) {
                const auto image=renderer.readback();const auto pixel=image.rgba.data()+(32*64+32)*4;
                require(unsigned(pixel[0])==((color>>16)&255) && unsigned(pixel[1])==((color>>8)&255) && unsigned(pixel[2])==(color&255),
                    "file read refreshes a resident GPU texture");
            };
            for(bool asynchronous:{false,true}) {
                state.gpu->invalidate_memory(texture,64);memory.fill_elements<std::uint32_t>(texture,old,16,false);
                if(asynchronous) {state.gpu->capture_frame(output/"texture-read-capture");packet(0x12C,{0});}
                paint();verify_pixel(old);
                const auto source=asynchronous?invoke(0x000E09BA,{area+0x300,GENERIC_READ,3,0,3,0x40000000,0}):handle;
                require(source!=UINT32_MAX,"texture read opens the saved input");
                const auto result=invoke(0xFFF00000U+219*16,{source,0,0,0,area+0x710,texture,64,area+0x700});
                require(result==(asynchronous?0x103U:success),"texture replacement read reports its real status");
                if(asynchronous) {
                    const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
                    while(state.pending_io_count && std::chrono::steady_clock::now()<deadline) {state.dispatch_io();if(state.pending_io_count) Sleep(1);}
                    require(!state.pending_io_count && memory.load<std::uint32_t>(area+0x710)==success,"asynchronous texture read completes");
                    require(invoke(0xFFF00000U+187*16,{source})==success,"texture input close");
                }
                require(memory.load<std::uint32_t>(texture)==expected,"texture source bytes match the saved file");
                paint();verify_pixel(expected);save_png(renderer.readback(),output/(asynchronous?"texture-read-async.png":"texture-read-sync.png"));
                if(asynchronous) {
                    packet(0x12C,{0});paint();
                    File captured(output/"texture-read-capture"/"texture-0001.bin");const auto bytes=captured.read(0,64);
                    require(captured.size()==64 && std::memcmp(bytes.data(),memory.access(profile,64),64)==0,"capture retains the replaced texture bytes");
                }
            }
            require(renderer.errors().empty(),"texture reads retain valid D3D11 bindings");state.gpu.reset();
        }
        if(!reload) require(invoke(0xFFF00000U+198*16,{handle,area+0x710})==success,"save flush");
        require(invoke(0xFFF00000U+187*16,{handle})==success,"save file close");
        const auto key=memory.load<std::uint32_t>(0x00293D24),disk=memory.load<std::uint32_t>(0x00293D20);
        const auto span=[&](std::uint32_t address,unsigned size){return Bytes(static_cast<const std::byte*>(memory.access(address,size)),size);};
        const auto inner=reference(span(key,16),span(profile,signed_size)),expected=reference(span(disk,16),inner);
        require(std::equal(expected.begin(),expected.end(),span(profile+signed_size,20).begin()),"persisted profile signature matches independent HMAC");
        const auto signature=invoke(0x000E1A84,{1});require(signature!=UINT32_MAX,"original save signature begin");
        require(invoke(0x000E1A0D,{signature,profile,signed_size})==0,"original load signature update");
        require(invoke(0x000E1A27,{signature,area+0x740})==0 && std::equal(expected.begin(),expected.end(),span(area+0x740,20).begin()),"original save signature validates after reopening");
        if(reload==1) {
            // Use the same overlapped/unbuffered request flags as D8E86's title save writer.
            const auto overwrite=invoke(0x000E09BA,{area+0x300,GENERIC_WRITE,3,0,3,0x60000000,0});
            require(overwrite!=UINT32_MAX,"existing profile overwrite open");
            memory.store<std::uint32_t>(profile+0x7AB4,7);
            const auto changed=invoke(0x000E1A84,{1});require(changed!=UINT32_MAX,"overwrite signature begin");
            require(invoke(0x000E1A0D,{changed,profile,signed_size})==0 && invoke(0x000E1A27,{changed,profile+signed_size})==0,"original overwrite signature");
            require(invoke(0xFFF00000U+236*16,{overwrite,0,0,0,area+0x710,profile,profile_size,area+0x700})==invalid_parameter,"unbuffered writes reject a non-sector length");
            require(invoke(0xFFF00000U+236*16,{overwrite,0,0,0,area+0x710,profile,aligned_size,area+0x700})==0x103,"original asynchronous save write is pending");
            const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
            while(state.pending_io_count && std::chrono::steady_clock::now()<deadline) {state.dispatch_io();if(state.pending_io_count) Sleep(1);}
            require(!state.pending_io_count && memory.load<std::uint32_t>(area+0x710)==success && memory.load<std::uint32_t>(area+0x714)==aligned_size,"asynchronous save completes with the real byte count");
            require(invoke(0xFFF00000U+198*16,{overwrite,area+0x710})==success && invoke(0xFFF00000U+187*16,{overwrite})==success,"overwritten profile flush and close");
            // An IRP retains its file and event objects after their handles
            // close; completion must never signal reallocated guest storage.
            const auto reader=invoke(0x000E09BA,{area+0x300,GENERIC_READ,3,0,3,0x40000000,0});
            require(reader!=UINT32_MAX,"pending-close read handle");
            require(invoke(0xFFF00000U+189*16,{area+0x720,0,0,0})==success,"pending-close completion event");
            const auto event=memory.load<std::uint32_t>(area+0x720),file_object=state.handles.at(reader).address,event_object=state.handles.at(event).address;
            const auto read_buffer=state.allocate(aligned_size,page,true,PAGE_READWRITE);require(read_buffer!=0,"pending-close read storage");
            require(invoke(0xFFF00000U+219*16,{reader,event,0,0,area+0x710,read_buffer,aligned_size,area+0x700})==0x103,
                "read remains pending until native completion publication");
            require(invoke(0xFFF00000U+187*16,{reader})==success && invoke(0xFFF00000U+187*16,{event})==success &&
                state.objects.contains(file_object) && state.objects.contains(event_object),"pending I/O retains closed file and event storage");
            const auto close_deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
            while(state.pending_io_count && std::chrono::steady_clock::now()<close_deadline) {state.dispatch_io();if(state.pending_io_count) Sleep(1);}
            require(!state.pending_io_count && memory.load<std::uint32_t>(area+0x710)==success && memory.load<std::uint32_t>(area+0x714)==aligned_size &&
                !std::memcmp(memory.access(read_buffer,aligned_size),memory.access(profile,aligned_size),aligned_size),"closed-handle read completes with the original payload");
            require(!state.objects.contains(file_object) && !state.objects.contains(event_object),"completion releases the final native object references");
        }
        if(reload==2) {
            require(memory.load<std::uint32_t>(profile+0x7AB4)==7,"overwritten profile persists in another fresh runtime");
            memory.store<std::uint8_t>(profile,memory.load<std::uint8_t>(profile)^1U);
            const auto changed=reference(span(key,16),span(profile,signed_size));
            require(reference(span(disk,16),changed)!=expected,"changed save data fails integrity verification");
        }
    }
    return std::format("{{\"format\":\"b2-storage-check-v1\",\"passed\":true,\"game_booted\":false,\"cases\":{},\"profile_bytes\":31748,\"reopened\":true,\"overwritten\":true,\"async_write\":true,\"runtime_decoder\":false,\"graphics_checked\":{},\"graphics_status\":{},\"storage\":{}}}",checks,graphics_checked,json(graphics_checked?"checked":"unsupported-metal-gpu"),json(utf8(std::filesystem::absolute(output).wstring())));
}
std::string check_mods(const std::filesystem::path& executable,const std::filesystem::path& disc_path,const std::filesystem::path& output) {
    Xbe image(executable);Disc disc(disc_path);
    if(!image.supported() || disc.sha256(disc.find("default.xbe"))!=image.sha256 || std::filesystem::exists(output))
        throw std::runtime_error("Mod checks require the verified executable/disc and a fresh output directory.");
    const auto root=std::filesystem::absolute(output).lexically_normal()/"mods";
    std::filesystem::create_directories(root);
    create_mod(root/"base","Base assets");create_mod(root/"override","Override assets");create_mod(root/"broken","Broken mod");
    constexpr std::string_view asset="trackpss/forward/fwyl.pss";
    const auto original=disc.find(asset);
    std::string base(4097,'B'),override_data(3001,'O');
    for(unsigned i=0;i<override_data.size();++i) override_data[i]=char(i%251);
    write_text(root/"base/files"/asset,base,false);
    write_text(root/"base/files/tracks1/forward/fwyl/fwyl.psw","world-stream-fixture",false);
    write_text(root/"override/files/TrAcKpSs/Forward/FwYl.PsS",override_data,false);
    write_text(root/"broken/files/default.xbe","unsupported executable",false);
    unsigned checks=0;
    const auto require=[&](bool passed,std::string_view name) {
        if(!passed) throw std::runtime_error("Mod check failed: "+std::string(name));++checks;
    };
    const auto rejects=[&](const auto& operation,std::string_view message) {
        bool rejected=false;
        try {operation();} catch(const std::exception& error) {rejected=std::string_view(error.what()).find(message)!=std::string_view::npos;}
        require(rejected,message);
    };
    const std::vector<std::string> order{"base","override"},reversed{"override","base"};
    Mods mounted(root,order,disc),reverse(root,reversed,disc),disabled(root,{},disc);
    require(mounted.lookup("TrAcKpSs\\FORWARD\\FWYL.pss")->size==override_data.size(),"case-insensitive mixed-separator lookup");
    require(mounted.conflicts().size()==1 && mounted.conflicts()[0].winner=="override","last enabled mod wins with conflict details");
    require(reverse.lookup(asset)->mod=="base" && !disabled.lookup(asset),"reordering and disabling change the mount");
    require(!valid_mod_id("../base") && !valid_mod_id("C:base") && !valid_mod_id("BASE"),"mod ids cannot escape their folder");
    rejects([&]{Mods invalid(root,std::vector<std::string>{"missing"},disc);},"Enabled mod is missing");
    rejects([&]{Mods invalid(root,std::vector<std::string>{"base","base"},disc);},"duplicate enabled mod");
    rejects([&]{Mods invalid(root,std::vector<std::string>{"broken"},disc);},"compiled DSP replacements");
    write_text(root/"base/files/not-on-disc.dat","not a disc asset",false);
    rejects([&]{Mods invalid(root,order,disc);},"existing disc file");
    std::filesystem::remove(root/"base/files/not-on-disc.dat");
    const auto run=[&](const std::vector<std::string>& enabled,std::string_view expected,std::uint32_t size,unsigned session) {
        std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
        XboxHost provider;provider.storage_root=output/std::format("storage-{}",session);provider.mods_root=root;provider.enabled_mods=enabled;provider.audio_output=false;provider.draw_distance=2;
        Xbox xbox(image,memory,ram,disc_path,std::move(provider));auto& state=*xbox.state_;
        auto& thread=state.create(0,0,0,image.stack_commit,0,false);state.current=&thread;
        auto& cpu=thread.cpu;cpu.preempt=nullptr;const auto stack=cpu.registers[esp]-4096;
        const auto invoke=[&](unsigned ordinal,std::initializer_list<std::uint32_t> arguments) {
            cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
            unsigned index=0;for(auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
            invoke_native(cpu,memory,0xFFF00000U+ordinal*16);
            require(cpu.eip==return_sentinel && cpu.registers[esp]==stack+4+arguments.size()*4,"native file calling convention");
            return cpu.registers[eax];
        };
        const auto name=[&](std::string_view path) {
            memory.store<std::uint32_t>(0xA010,0);memory.store<std::uint32_t>(0xA014,0xA030);
            memory.store<std::uint16_t>(0xA030,static_cast<std::uint16_t>(path.size()));memory.store<std::uint16_t>(0xA032,static_cast<std::uint16_t>(path.size()+1));
            memory.store<std::uint32_t>(0xA034,0xA100);std::memcpy(memory.access(0xA100,path.size()),path.data(),path.size());memory.store<std::uint8_t>(0xA100+static_cast<std::uint32_t>(path.size()),0);
        };
        name("\\Device\\CdRom0\\TRACKPSS\\forward\\FWYL.PSS");
        require(invoke(210,{0xA010,0xB000})==success && memory.load<std::uint64_t>(0xB028)==size,"path query reports mounted size");
        for(const unsigned options:{0x20U,0U}) {
            require(invoke(190,{0xA000,GENERIC_READ,0xA010,0xA050,0,0,1,1,options})==success,"open mod file");
            const auto handle=memory.load<std::uint32_t>(0xA000);
            require(invoke(211,{handle,0xA050,0xB000,24,5})==success && memory.load<std::uint64_t>(0xB008)==size,"handle query reports mounted size");
            memory.store<std::uint64_t>(0xA060,32);
            const auto result=invoke(219,{handle,0,0,0,0xA050,0xC000,8192,0xA060});
            require(result==(options?success:0x103),"synchronous and asynchronous read status");
            const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
            while(state.pending_io_count && std::chrono::steady_clock::now()<deadline) {state.dispatch_io();if(state.pending_io_count) Sleep(1);}
            const auto count=std::min(8192U,size-32);
            require(!state.pending_io_count && memory.load<std::uint32_t>(0xA050)==success && memory.load<std::uint32_t>(0xA054)==count &&
                expected.size()>=32+count && !std::memcmp(memory.access(0xC000,count),expected.data()+32,count),"native reads return mounted bytes and partial EOF length");
            memory.store<std::uint64_t>(0xA060,size);
            require(invoke(219,{handle,0,0,0,0xA050,0xC000,8,0xA060})==end_of_file,"replacement EOF");
            memory.store<std::uint64_t>(0xA060,0);
            require(invoke(236,{handle,0,0,0,0xA050,0xC000,8,0xA060})==write_protected,"mod files retain disc write protection");
            require(invoke(187,{handle})==success,"close mod file");
        }
        name("\\Device\\CdRom0\\trackpss\\forward");
        require(invoke(190,{0xA000,GENERIC_READ,0xA010,0xA050,0,0,1,1,0x21})==success,"open original directory");
        const auto handle=memory.load<std::uint32_t>(0xA000);
        require(invoke(207,{handle,0,0,0,0xA050,0xB000,4096,1,0,1})==success,"enumerate original directory with mod sizes");
        const auto& listing=state.handles.at(handle).file->listing;
        const auto entry=std::ranges::find_if(listing,[](const auto& file){return folded(file.name)=="FWYL.PSS";});
        require(entry!=listing.end() && entry->size==size,"directory listing agrees with reads");
        require(invoke(187,{handle})==success,"close directory");
        name("\\Device\\CdRom0\\trackpss\\forward\\fwyl.pss");
        require(invoke(190,{0xA000,GENERIC_WRITE,0xA010,0xA050,0,0,1,1,0x20})==access_denied,"opening mods for writing is denied");
        name("\\Device\\CdRom0\\tracks1\\forward\\fwyl\\fwyl.psw");
        require(invoke(190,{0xA000,GENERIC_READ,0xA010,0xA050,0,0,1,1,0x20})==success,"world stream remains available");
        require(state.world_streams[0].has_value() && (enabled.empty()?state.world_paths[0].empty():state.world_paths[0]==root/"base/files/tracks1/forward/fwyl/fwyl.psw"),"extended scenery uses the mounted source");
        require(invoke(187,{memory.load<std::uint32_t>(0xA000)})==success,"close world stream");
    };
    run(order,override_data,static_cast<std::uint32_t>(override_data.size()),0);run(reversed,base,static_cast<std::uint32_t>(base.size()),1);
    std::array<std::byte,8224> original_bytes{};disc.read(original,0,original_bytes);
    run({},std::string_view(reinterpret_cast<const char*>(original_bytes.data()),original_bytes.size()),original.size,2);
    const auto fallback=disc.find(asset);require(fallback.size==original.size && disc.sha256(disc.find("default.xbe"))==image.sha256,"original disc stays unchanged");
    const auto report=std::format("{{\"format\":\"b2-mod-check-v1\",\"passed\":true,\"game_booted\":false,\"cases\":{},\"sync_reads\":true,\"async_reads\":true,\"read_only\":true,\"priority\":true,\"directory_sizes\":true,\"mount\":{}}}",checks,mounted.report());
    write_text(output/"mods-check.json",report,false);return report;
}
std::string check_replays(const std::filesystem::path& executable,const std::filesystem::path& disc_path,const std::filesystem::path& output) {
    Xbe image(executable);Disc disc(disc_path);
    if(!image.supported() || std::filesystem::exists(output)) throw std::runtime_error("Replay checks require the verified XBE and a fresh output directory");
    std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
    XboxHost provider;provider.storage_root=output;
    Xbox xbox(image,memory,ram,disc_path,std::move(provider));auto& state=*xbox.state_;
    auto& thread=state.create(0,0,0,image.stack_commit,0,false);state.current=&thread;
    auto& cpu=thread.cpu;cpu.preempt=nullptr;state.diagnostic.remaining=2000000;
    diagnostic_guest(&cpu);
    const auto stack=cpu.registers[esp]-4096;
    const auto invoke=[&](std::uint32_t address,std::initializer_list<std::uint32_t> arguments) {
        diagnostic_record("{\"type\":\"replay_check_call\",\"address\":"+json(hex32(address))+'}');
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
        unsigned index=0;for(auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
        invoke_native(cpu,memory,address);
        if(cpu.eip!=return_sentinel || cpu.registers[esp]!=stack+4+arguments.size()*4)
            throw std::runtime_error("Replay check calling convention differs at "+hex32(address));
        return cpu.registers[eax];
    };
    // Original SDK heap setup, file-pool constructor and startup arguments.
    memory.store<std::uint32_t>(0xA000,0x30);
    const auto heap=invoke(0x000E435A,{2,0,image.heap_reserve,image.heap_commit,0,0xA000});
    if(!heap) throw std::runtime_error("Replay check could not initialize the original SDK heap");
    memory.store<std::uint32_t>(0x005A8974,heap);
    const auto semaphore=invoke(0x000E3009,{0,1,1,0});
    if(!semaphore) throw std::runtime_error("Track read check could not create its loading semaphore");
    const auto semaphore_address=state.handles.at(semaphore).address;
    std::array<std::byte,20> semaphore_header{};
    std::memcpy(semaphore_header.data(),memory.access(semaphore_address,semaphore_header.size()),semaphore_header.size());
    for(const auto& range:std::array<std::pair<std::uint32_t,std::uint32_t>,3>{{
        {0x64000,0x381C000},{0x640000,0x3880000},{0x64000,0x2D10000}}})
        if(invoke(0x000DFA30,{range.first,range.second,0,4})!=(range.second|0x80000000U))
            throw std::runtime_error("Original fixed track/replay allocation failed at "+hex32(range.second));
    // The real crash read spans the original cached/noncached allocation
    // boundary at 83880000. Replay it without booting or truncating the read.
    constexpr std::string_view track_path="D:\\trackpss\\forward\\fwyl.PSS";
    std::memcpy(memory.access(0xA000,track_path.size()+1),track_path.data(),track_path.size()+1);
    const auto file=invoke(0x000E09BA,{0xA000,GENERIC_READ,FILE_SHARE_READ,0,OPEN_EXISTING,FILE_FLAG_OVERLAPPED|FILE_FLAG_NO_BUFFERING,0});
    if(file==UINT32_MAX) throw std::runtime_error("Original track read check could not open FWYL.PSS");
    memory.fill_elements<std::uint32_t>(0xA080,0,16,false);
    memory.store<std::uint32_t>(0xA0A8,0x60000);
    if(invoke(0x000E04F3,{file,0x8387C000,0x20000,0xA080,0xA0A0})!=0 || state.pending_io_count!=1)
        throw std::runtime_error("Original track read check did not queue its asynchronous read");
    const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(2);
    while(state.pending_io_count && std::chrono::steady_clock::now()<deadline) {state.dispatch_io();if(state.pending_io_count) Sleep(1);}
    std::vector<std::byte> expected_track(0x20000);disc.read(disc.find("trackpss/forward/fwyl.pss"),0x60000,expected_track);
    if(state.pending_io_count || memory.load<std::uint32_t>(0xA0A0)!=success || memory.load<std::uint32_t>(0xA0A4)!=expected_track.size() ||
       std::memcmp(memory.access(0x8387C000,expected_track.size()),expected_track.data(),expected_track.size()) ||
       std::memcmp(memory.access(semaphore_address,semaphore_header.size()),semaphore_header.data(),semaphore_header.size()))
        throw std::runtime_error("Original track read differs or damages its loading semaphore");
    if(invoke(0xFFF00000U+187*16,{file})!=success) throw std::runtime_error("Original track read check could not close its file");
    invoke(0x00123801,{});
    invoke(0x00139380,{});
    cpu.registers[ecx]=0x00303C70;invoke(0x000D9570,{5,0x00489F70,0x003D6864,0x00295B14});
    memory.store<std::uint32_t>(0x0034AB48,0x00303C70);
    constexpr std::uint32_t controller=0x002FFD68,buffer=0x02D10000;
    memory.store<std::uint32_t>(0x003D686C,buffer|0x80000000U);
    std::ostringstream report;unsigned cases=0;
    const auto check=[&](std::string_view path,unsigned track,unsigned mode,unsigned lesson,unsigned pal) {
        const auto entry=disc.find(path);
        if(entry.size<32 || entry.size-32>0x64000) throw std::runtime_error("Replay check file is outside the original replay-buffer bounds");
        std::vector<std::byte> expected(entry.size);disc.read(entry,0,expected);
        const auto frames=u32(expected,4);
        if(u32(expected,0)!=32 || entry.size!=32+std::uint64_t(frames)*6)
            throw std::runtime_error("Replay header/length differs for "+std::string(path));
        cpu.registers[ecx]=controller;invoke(0x00088850,{0});
        memory.store<std::uint32_t>(0x0034AB58,mode);memory.store<std::uint32_t>(0x0048A150,track);
        memory.store<std::uint32_t>(0x00352600,lesson);memory.store<std::uint32_t>(0x00352720,pal);
        memory.store<std::uint32_t>(0x004CD80C,1);
        std::fill_n(ram.begin()+buffer,entry.size-32,std::byte{0xA5});
        cpu.registers[ecx]=controller;const auto loaded=invoke(0x00088D30,{});
        if(loaded!=frames || memory.load<std::uint32_t>(controller+8)!=1 ||
           !std::equal(expected.begin()+32,expected.end(),ram.begin()+buffer)) {
            const auto& io=state.io_trace;
            throw std::runtime_error(std::format("Original replay load differs for {}: {} of {} frames, loaded {}, last I/O {}",
                path,loaded,frames,memory.load<std::uint32_t>(controller+8),io.empty()?"none":hex32(io.back().status)));
        }
        if(cases++) report<<',';
        report<<std::format("{{\"path\":{},\"frames\":{},\"bytes\":{},\"identical\":true}}",json(path),frames,entry.size);
    };
    for(unsigned pal=0;pal<2;++pal) {
        const auto region=pal?'P':'N';
        for(const auto& item:std::array<std::pair<std::string_view,unsigned>,3>{{{"ctyl",1},{"fwyl",7},{"cstl",9}}})
            check(std::format("tracks/forward/{}/DReplay{}XBOX.dat",item.first,region),item.second,1,0,pal);
        for(unsigned lesson=0;lesson<6;++lesson)
            check(std::format("tracks/{}/fwyl/L{}Intro{}XBOX.dat",lesson==3?"reverse":"forward",lesson,region),lesson==3?22:7,15,lesson,pal);
    }
    if(std::memcmp(memory.access(semaphore_address,semaphore_header.size()),semaphore_header.data(),semaphore_header.size()) ||
       invoke(0xFFF00000U+187*16,{semaphore})!=success)
        throw std::runtime_error("Original lesson replays damaged their loading semaphore");
    return std::format("{{\"format\":\"b2-replay-check-v1\",\"passed\":true,\"game_booted\":false,\"cases\":{},\"fixed_track_read\":true,\"files\":[{}]}}",cases,report.view());
}
void native_platform(Cpu& cpu,Memory& memory,std::uint32_t address) {
    if(!cpu.kernel || !cpu.kernel->context || cpu.kernel->entries[255]!=Xbox::State::dispatch_kernel<255>)
        throw NativeBoundary(address,"native platform API requires the Xbox host provider");
    auto& host=*static_cast<Xbox::State*>(cpu.kernel->context);
    if(&host.memory!=&memory) throw NativeBoundary(address,"native platform API received another guest memory");
    HostFloatingScope floating(cpu.floating);
    PerfScope timing(host.host.performance,PerfPhase::platform,address);
    host.platform(cpu,address);
}
std::string check_track_import(const std::filesystem::path& executable,const std::filesystem::path& disc_path,
                               const std::filesystem::path& assets,const std::filesystem::path& output) {
    using State=Xbox::State;
    Xbe image(executable);if(!image.supported()) throw std::runtime_error("Track import checks require the verified XBE");
    std::filesystem::create_directories(output);
    std::vector<std::byte> ram(64*1024*1024);image.load(std::span(ram).subspan(image.base,image.image_size));Memory memory(0,ram);
    XboxHost provider;provider.storage_root=output/"storage";provider.imported_tracks=assets;
    Xbox xbox(image,memory,ram,disc_path,std::move(provider));auto& state=*xbox.state_;
    auto& thread=state.create(0,0,0,image.stack_commit,0,false);state.current=&thread;
    auto& cpu=thread.cpu;cpu.preempt=nullptr;state.diagnostic.remaining=2000000;
    const auto stack=cpu.registers[esp]-8192;
    unsigned checks=0;
    const auto require=[&](bool condition,std::string_view name) {if(!condition) throw std::runtime_error("Track import check: "+std::string(name));++checks;};
    const auto invoke=[&](unsigned address,unsigned object,std::initializer_list<unsigned> arguments={}) {
        state.diagnostic.remaining=2000000;cpu.registers[ecx]=object;cpu.registers[esp]=stack;
        memory.store<std::uint32_t>(stack,return_sentinel);unsigned index=0;
        for(auto value:arguments) memory.store<std::uint32_t>(stack+4+index++*4,value);
        try {invoke_native(cpu,memory,address);} catch(const std::exception& error) {
            throw std::runtime_error(std::format("Track call {} stopped at {}: {}",hex32(address),hex32(cpu.eip),error.what()));
        }
        require(cpu.eip==return_sentinel,"original call returns to its caller");return cpu.registers[eax];
    };
    memory.store<std::uint32_t>(0xA000,0x30);
    const auto heap=invoke(0xE435A,0,{2,0,image.heap_reserve,image.heap_commit,0,0xA000});
    require(heap!=0,"original SDK heap initialization");memory.store<std::uint32_t>(0x5A8974,heap);
    memory.store<std::uint32_t>(0x5A619C,heap);require(invoke(0x123801,0)==1,"original CRT lock initialization");
    constexpr unsigned master=0x8381C000,manager=0x0E000000;
    const auto race=state.allocate(2*1024*1024,page,true,PAGE_READWRITE);
    require(race!=0,"race diagnostic allocation does not overlap the original heap");
    File master_file(assets/"interstate.pss");require(master_file.size()<=0x6A4000,"fixed master allocation");
    master_file.read(0,{static_cast<std::byte*>(memory.access(master,master_file.size())),static_cast<std::size_t>(master_file.size())});
    memory.store<std::uint32_t>(0x4B85B8,master);
    memory.store<std::uint32_t>(0x5ACE74,0x34);
    const auto texture_table=memory.load<std::uint32_t>(master+20);
    require(memory.load<std::uint32_t>(master+16)==232,"all original, imported, barrier and environment texture headers are present");
    state.imported.selected=true;invoke(0x8BCD0,0x4B85B0);
    for(unsigned i=146;i<231;++i) {
        const auto texture=memory.load<std::uint32_t>(texture_table+i*4);
        const auto raster=memory.load<std::uint32_t>(texture),resource=memory.load<std::uint32_t>(raster+0x34);
        require(memory.load<std::uint32_t>(resource+4)>=(master&0x7FFFFFFF),"original texture registration retains imported pixels");
        require(memory.load<std::uint32_t>(resource+12)!=0,"original texture registration creates an Xbox texture header");
    }
    File race_file(assets/"interstate.bgd");require(race_file.size()<=2*1024*1024,"fixed race allocation");
    race_file.read(0,{static_cast<std::byte*>(memory.access(race,race_file.size())),static_cast<std::size_t>(race_file.size())});
    memory.store<std::uint32_t>(0x2FFFF8,1);memory.store<std::uint32_t>(0x2FFFFC,race);
    require(invoke(0x79C70,0x2FFFF8)==1,"original race loader accepts imported arrays");
    require(memory.load<std::uint32_t>(0x4B8498)==47,"original loader retains all authored replay cameras");
    const auto authored_cameras=memory.load<std::uint32_t>(0x4B8494);
    require(memory.load<std::uint16_t>(authored_cameras+24)==0 && memory.load<std::uint8_t>(authored_cameras+26)==0x87 &&
        memory.load<std::uint8_t>(authored_cameras+27)==5,"original starting camera and mode are retained");
    File camera_file(assets/"interstate.bcd");require(camera_file.size()<0x20000,"native camera allocation bounds");
    const auto camera_data=state.allocate(0x20000,page,true,PAGE_READWRITE);
    require(camera_data!=0,"camera diagnostic allocation");
    camera_file.read(0,{static_cast<std::byte*>(memory.access(camera_data,camera_file.size())),static_cast<std::size_t>(camera_file.size())});
    memory.store<std::uint32_t>(0x34AB50,camera_data);memory.store<std::uint32_t>(0x300050,1);
    require(invoke(0x891A0,0x300000)==1,"native cinematic loader accepts the imported camera file");
    require(memory.load<std::uint32_t>(0x300020)==1 && memory.load<std::uint32_t>(0x300024)==722 &&
        memory.load<std::uint32_t>(0x300040)==1,"opening keeps the native duration and uses the authored starting view");
    const auto replay_records=memory.load<std::uint32_t>(0x300018),replay_positions=memory.load<std::uint32_t>(0x30001C);
    const auto replay_count=memory.load<std::uint32_t>(0x300038),replay_points=memory.load<std::uint32_t>(0x30003C);
    require(replay_count==57 && replay_points>47 && memory.load<std::uint32_t>(0x30004C)==1,"all replay cameras and spline end markers are loaded");
    unsigned source_cameras=0,spline_cameras=0;
    for(unsigned i=0;i<replay_count;++i) {
        const auto record=replay_records+i*104,flags=memory.load<std::uint32_t>(record+0x34),mode=flags&0x7E0;
        const auto id=memory.load<std::uint32_t>(record+0x10);
        require(id>=1 && id<=47 && memory.load<std::uint32_t>(record+0x38)<437,"camera identity and source road index");
        require(memory.load<std::uint32_t>(record+0x3C)<=memory.load<std::uint32_t>(record+0x40) &&
            memory.load<std::uint32_t>(record+0x40)<replay_points,"camera path bounds");
        if(mode==0x400) continue;
        ++source_cameras;const auto original=authored_cameras+(id-1)*44;
        if(mode==0x200) {++spline_cameras;require((memory.load<std::uint32_t>(record+104+0x34)&0x7E0)==0x400,"spline has its own end gate");}
        else {
            const auto position=replay_positions+memory.load<std::uint32_t>(record+0x3C)*12;
            require(!std::memcmp(memory.access(position,12),memory.access(original,12),12),"static camera retains its source position");
        }
        require(memory.load<std::uint32_t>(record+0x14)==std::bit_cast<std::uint32_t>(float(memory.load<std::uint8_t>(original+32))),
            "camera retains its authored field of view");
    }
    require(source_cameras==47 && spline_cameras==10,"all five authored camera modes are represented");
    require(invoke(0x89400,0x300054)==1,"native replay controller accepts converted gates and paths");
    const auto road=memory.load<std::uint32_t>(0x4B8450),curve=memory.load<std::uint32_t>(road);
    const auto route_points=memory.load<std::uint32_t>(curve);
    require(route_points==437,"original loader finds Interstate's closed lap route without a duplicate segment");
    require(memory.load<std::uint32_t>(curve+0x1C)==race+0x160,"route vertices refer to imported positions");
    const auto distances=memory.load<std::uint32_t>(curve+12);
    const auto metres=std::bit_cast<float>(memory.load<std::uint32_t>(distances+(route_points-1)*8));
    require(std::isfinite(metres) && metres>8039 && metres<8040,"original lap length is 8.04 km");
    invoke(0x7D750,road,{0,0xA100});float direction=0;
    for(unsigned axis=0;axis<3;++axis) direction+=std::bit_cast<float>(memory.load<std::uint32_t>(0xA100+axis*4))*
        std::bit_cast<float>(memory.load<std::uint32_t>(race+0x60+axis*4));
    require(direction>0,"original road direction agrees with the starting grid");
    invoke(0x1F4950,0);
    const auto checkpoints=memory.load<std::uint32_t>(0x4B8464),checkpoint_count=memory.load<std::uint32_t>(0x4B8468);
    require(checkpoint_count==6 && memory.load<std::uint32_t>(0x4B8470)==48,"original loader finds six checkpoint planes and their per-lap events");
    constexpr unsigned checkpoint_car=0xC000;
    memory.fill_elements<std::uint32_t>(checkpoint_car,0,1024,false);
    constexpr unsigned bonuses[6][3]={{55000,42000,0},{30000,25000,25000},{27000,22000,22000},
        {24000,22000,22000},{38000,32000,30000},{22000,20000,25000}};
    unsigned checkpoint_crossings=0;
    for(unsigned lap=0;lap<3;++lap) {
        unsigned previous=0;
        memory.store<std::uint32_t>(checkpoint_car+0x644,lap);
        for(unsigned gate=0;gate<checkpoint_count;++gate) {
            const auto object=checkpoints+gate*28,plane=memory.load<std::uint32_t>(object+4);
            require(memory.load<std::uint32_t>(plane+0x20)==8,"checkpoint retains eight per-lap event slots");
            const auto event=memory.load<std::uint32_t>(plane+0x24+lap*4),event_data=memory.load<std::uint32_t>(event+4);
            require(memory.load<std::uint32_t>(event_data)==bonuses[gate][lap],"checkpoint retains the original Burnout 1 time bonus");
            invoke(0x9F610,object);bool crossed=false;
            for(unsigned point=0;point<route_points && !crossed;++point) {
                memory.store<std::uint32_t>(checkpoint_car+0xD4C,point+1);
                memory.store<std::uint32_t>(0xA140,UINT32_MAX);
                state.diagnostic.break_address=0x9F660;state.diagnostic.break_hit=1;
                try {require(invoke(0x79B00,object,{checkpoint_car,point,0xA140})==0,"checkpoint does not trigger before its crossing");}
                catch(const std::runtime_error&) {
                    if(cpu.eip!=0x9F660) throw;
                    crossed=true;require(memory.load<std::uint32_t>(0xA140)==bonuses[gate][lap],"original crossing code awards the imported checkpoint time");
                    require(!gate || point>previous,"original route crosses the imported gates in order");previous=point;++checkpoint_crossings;
                }
                state.diagnostic.break_address=0;
            }
            require(crossed,"original game code recognizes the checkpoint crossing");
        }
    }
    File world_file(assets/"interstate.psw");const auto sections=memory.load<std::uint32_t>(master+0x5C);
    require(sections==59 && world_file.size()==sections*State::world_stride,"world stream dimensions");
    std::vector<std::byte> world(static_cast<std::size_t>(world_file.size())),scratch(4096);
    world_file.read(0,world);memory.map(State::world_base,world);memory.map(State::world_base&0x7FFFFFFF,world);memory.map(manager,scratch);
    memory.store<std::uint32_t>(manager+0x10,State::world_base);
    std::uint64_t triangles=0;unsigned instances=0;
    for(unsigned i=0;i<sections;++i) {
        const auto sector=State::world_base+i*State::world_stride;
        memory.store<std::uint32_t>(manager+0x24,i);invoke(0x8EEB0,manager);
        const auto tree=memory.load<std::uint32_t>(sector+0xB0),vertices=memory.load<std::uint32_t>(sector+0x84);
        require(tree>=sector && tree<sector+State::world_stride,"original collision tree relocation");
        require(memory.load<std::uint32_t>(tree+4)==tree+16,"original collision branch relocation");
        require(vertices>=sector && vertices<sector+State::world_stride,"original collision vertex relocation");
        for(unsigned j=0;j<memory.load<std::uint32_t>(sector+0xBC);++j) {
            const auto mesh=memory.load<std::uint32_t>(sector+0xC4+j*4);
            require(mesh>=sector && mesh<sector+State::world_stride,"original mesh relocation");
            require(memory.load<std::uint32_t>(mesh+4)>=(sector&0x7FFFFFFF),"original vertex buffer relocation");
        }
        const auto groups=memory.load<std::uint32_t>(sector+0x8F0);
        for(unsigned prop=0;prop<memory.load<std::uint32_t>(master+0x18);++prop) {
            const auto group=groups+prop*12,count=memory.load<std::uint32_t>(group);
            if(!count) continue;
            instances+=count;
            const auto matrices=memory.load<std::uint32_t>(group+4),bounds=memory.load<std::uint32_t>(group+8);
            require(matrices>=sector && matrices+count*64<=sector+State::world_stride,"original instance matrix relocation");
            require(bounds>=sector && bounds+count*60<=sector+State::world_stride,"original instance bounds relocation");
        }
        memory.store<std::uint32_t>(sector+0xB4,2);
        triangles+=memory.load<std::uint32_t>(sector+0x90);
    }
    require(instances==894,"all original roadside instances are present");
    memory.store<std::uint32_t>(manager+4,18);memory.store<std::uint32_t>(manager+0x34,8);
    require(invoke(0x8F240,manager,{race+0x20})==8,"original streamer finds the starting grid in its initial contact window");
    constexpr unsigned collision_manager=0x48A150+0x2F460;
    std::memcpy(memory.access(collision_manager,0x318),memory.access(manager,0x318),0x318);
    const auto ray_contact=[&](const std::array<float,3>& from,const std::array<float,3>& to) {
        for(unsigned axis=0;axis<3;++axis) {
            memory.store<std::uint32_t>(0xA160+axis*4,std::bit_cast<std::uint32_t>(from[axis]));
            memory.store<std::uint32_t>(0xA180+axis*4,std::bit_cast<std::uint32_t>(to[axis]));
        }
        return invoke(0x92F20,0x48A150,{0,0,0xA160,0xA180,0xA1A0,0})!=0;
    };
    const auto road_contact=[&](std::array<float,3> point) {
        auto from=point;from[1]+=20;point[1]-=20;return ray_contact(from,point);
    };
    std::array<float,3> grid;
    for(unsigned axis=0;axis<3;++axis) grid[axis]=std::bit_cast<float>(memory.load<std::uint32_t>(race+0x20+axis*4));
    require(road_contact(grid),"original collision code finds road beneath the starting grid");
    memory.store<std::uint32_t>(collision_manager+4,sections);
    const auto positions=memory.load<std::uint32_t>(curve+0x1C),pairs=memory.load<std::uint32_t>(curve+8);
    const auto contact_section=[&](const std::array<float,3>& point) {
        for(unsigned section=0;section<sections;++section) {
            bool inside=true;const auto sector=State::world_base+section*State::world_stride;
            for(unsigned plane=0;plane<4;++plane) {
                const auto at=sector+64+plane*16;float distance=-std::bit_cast<float>(memory.load<std::uint32_t>(at+12));
                for(unsigned axis=0;axis<3;++axis) distance+=std::bit_cast<float>(memory.load<std::uint32_t>(at+axis*4))*point[axis];
                inside&=distance>=0;
            }
            if(inside) return section;
        }
        return UINT32_MAX;
    };
    unsigned contacts=0;std::string missed;
    for(unsigned i=0;i<route_points;++i) {
        const auto left=positions+memory.load<std::uint16_t>(pairs+i*4)*16,right=positions+memory.load<std::uint16_t>(pairs+i*4+2)*16;
        std::array<float,3> point;
        for(unsigned axis=0;axis<3;++axis) point[axis]=(std::bit_cast<float>(memory.load<std::uint32_t>(left+axis*4))+
            std::bit_cast<float>(memory.load<std::uint32_t>(right+axis*4)))*0.5f;
        const auto containing=contact_section(point);
        require(containing!=UINT32_MAX,"lap route remains inside the streamed world");
        memory.store<std::uint32_t>(collision_manager+0x34,containing);
        bool hit=false;
        // A freeway cross-section can span a median. Check the carriageways,
        // rather than requiring the exact midpoint to contain a road triangle.
        for(const float fraction:{0.5f,0.1f,0.3f,0.7f,0.9f}) {
            for(unsigned axis=0;axis<3;++axis) point[axis]=
                std::bit_cast<float>(memory.load<std::uint32_t>(left+axis*4))*(1-fraction)+
                std::bit_cast<float>(memory.load<std::uint32_t>(right+axis*4))*fraction;
            if(road_contact(point)) {hit=true;break;}
        }
        contacts+=hit;
        if(!hit && missed.size()<200) missed+=std::format(" {}:[{},{},{}]@{}",i,point[0],point[1],point[2],containing);
    }
    require(contacts==route_points,std::format("original collision code finds road across every lap cross-section ({}/{}; missing{})",contacts,route_points,missed));
    require(memory.load<std::uint32_t>(0x4B8458)==1,"imported respawn route is registered");
    const auto ai=memory.load<std::uint32_t>(memory.load<std::uint32_t>(0x4B8454));
    const auto ai_pairs=memory.load<std::uint32_t>(ai+8),ai_attrs=memory.load<std::uint32_t>(ai+20);
    require(memory.load<std::uint32_t>(ai)==route_points,"respawn route follows the complete lap");
    unsigned respawns=0;
    for(unsigned i=0;i<route_points;++i) {
        if(!memory.load<std::uint8_t>(ai_attrs+i*32+4)) continue;
        require(memory.load<std::uint8_t>(ai_attrs+i*32+4)==2,"respawn uses the authored carriageway edge");
        const auto vertex=positions+memory.load<std::uint16_t>(ai_pairs+i*4+2)*16;
        std::array<float,3> from,to;
        for(unsigned axis=0;axis<3;++axis) from[axis]=to[axis]=std::bit_cast<float>(memory.load<std::uint32_t>(vertex+axis*4));
        const auto containing=contact_section(from);require(containing!=UINT32_MAX,"respawn remains inside the streamed world");
        memory.store<std::uint32_t>(collision_manager+0x34,containing);
        from[1]+=1;to[1]-=1;
        require(ray_contact(from,to),std::format("native collision beneath respawn point {}",i));++respawns;
    }
    require(respawns==378,"original prohibited respawn sections are retained");
    // The longest B1 exclusion needs four skips; B2's three-skip fallback
    // would put the car back in a prohibited section. Exercise the real search.
    constexpr unsigned respawn_car=0xC000,respawn_body=0xB000,respawn_matrix=0xB100;
    for(const bool imported:{false,true}) {
        state.imported.selected=imported;
        memory.fill_elements<std::uint32_t>(respawn_car,0,1024,false);
        memory.fill_elements<std::uint32_t>(respawn_body,0,128,false);
        memory.store<std::uint32_t>(respawn_car+8,respawn_body);
        memory.store<std::uint32_t>(respawn_body+16,respawn_matrix);
        for(unsigned axis=0;axis<3;++axis) memory.store<std::uint32_t>(respawn_matrix+axis*20,std::bit_cast<std::uint32_t>(1.0f));
        const auto vertex=positions+memory.load<std::uint16_t>(ai_pairs+139*4+2)*16;
        std::memcpy(memory.access(respawn_matrix+48,12),memory.access(vertex,12),12);
        memory.store<std::uint32_t>(respawn_car+0xD4C,139);
        memory.store<std::uint32_t>(respawn_car+0xD54,memory.load<std::uint32_t>(0x4B8454));
        memory.store<std::uint32_t>(respawn_car+0xD58,139);
        // Checkpoints are at function entries: the stock fallback logs before
        // placement, while the extended search reaches the body reset.
        const auto boundary=imported?0x5F360U:0x11E2EEU;
        state.diagnostic.break_address=boundary;state.diagnostic.break_hit=1;
        try {invoke(0x84510,respawn_car,{0});throw std::runtime_error("Respawn search boundary was not reached");}
        catch(const std::runtime_error&) {if(cpu.eip!=boundary) throw;}
        state.diagnostic.break_address=0;
        if(!imported) require(cpu.registers[edi]==155,"native B2 respawn retains its original three-skip limit");
        else {
            const auto expected=positions+memory.load<std::uint16_t>(ai_pairs+159*4+2)*16;
            require(std::memcmp(memory.access(respawn_matrix+48,12),memory.access(expected,12),12)==0,
                "native respawn clears B1's longer exclusion and uses the next permitted point");
        }
    }
    require(memory.load<std::uint32_t>(0x4B8460)==32 && memory.load<std::uint32_t>(0x4B84A0)>1,
        "native traffic lanes and spatial activation are registered");
    // Check individual upward-facing surfaces too: one hit across the whole
    // freeway width cannot detect a missing lane or a bad section boundary.
    unsigned road_faces=0,missing_faces=0;std::string collision_misses="section,triangle,x,y,z,selected,direct\n";
    unsigned wheel_faces=0;
    for(unsigned section=0;section<sections;++section) {
        const auto sector=State::world_base+section*State::world_stride;
        const auto vertex_data=memory.load<std::uint32_t>(sector+0x84),triangle_data=memory.load<std::uint32_t>(sector+0x94);
        memory.store<std::uint32_t>(collision_manager+0x34,section);
        for(unsigned face=0;face<memory.load<std::uint32_t>(sector+0x90);++face) {
            std::array<std::array<float,3>,3> points;std::array<float,3> center{},normal{};
            for(unsigned corner=0;corner<3;++corner) for(unsigned axis=0;axis<3;++axis) {
                const auto vertex=memory.load<std::uint32_t>(triangle_data+face*16+corner*4);
                points[corner][axis]=std::bit_cast<float>(memory.load<std::uint32_t>(vertex_data+vertex*16+axis*4));
                center[axis]+=points[corner][axis]/3;
            }
            for(unsigned axis=0;axis<3;++axis) {
                const auto a=(axis+1)%3,b=(axis+2)%3;
                normal[axis]=(points[1][a]-points[0][a])*(points[2][b]-points[0][b])-
                    (points[1][b]-points[0][b])*(points[2][a]-points[0][a]);
            }
            const auto magnitude=std::hypot(normal[0],normal[1],normal[2]);
            if(magnitude<0.001f || normal[1]/magnitude<0.5f) continue;
            ++road_faces;auto from=center,to=center;from[1]+=0.1f;to[1]-=0.1f;
            for(unsigned axis=0;axis<3;++axis)
                memory.store<std::uint32_t>(0xA250+axis*4,std::bit_cast<std::uint32_t>(from[axis]));
            memory.store<std::uint32_t>(0xA25C,std::bit_cast<std::uint32_t>(0.25f));
            memory.store<std::uint32_t>(0x4F08E0,0);
            invoke(0x90420,collision_manager,{section,0xA250,0x56EA0,0});
            const auto hit=ray_contact(from,to);
            const auto surface=memory.load<std::uint32_t>(triangle_data+face*16+12)&0xFFFF;
            if(hit && surface<=13) {
                cpu.registers[ebx]=0xA260;
                require(invoke(0x64DA0,0,{0xA400,0xA160,0xA180,0xA270})!=0,
                    std::format("wheel contact on road face {}:{} (surface {})",section,face,surface));
                ++wheel_faces;
            }
            if(hit) continue;
            const auto selected=invoke(0x8F240,collision_manager,{0xA160});
            for(unsigned axis=0;axis<3;++axis) {
                memory.store<std::uint32_t>(0xA200+axis*4,std::bit_cast<std::uint32_t>(from[axis]));
                memory.store<std::uint32_t>(0xA210+axis*4,std::bit_cast<std::uint32_t>(to[axis]));
            }
            memory.store<std::uint32_t>(0xA22C,std::bit_cast<std::uint32_t>(1.1f));
            invoke(0x8F3D0,collision_manager,{section,0xA200,0x92EE0,0xA220});
            const auto direct=std::bit_cast<float>(memory.load<std::uint32_t>(0xA22C))<1;
            collision_misses+=std::format("{},{},{},{},{},{},{}\n",section,face,center[0],center[1],center[2],selected,direct);
            // Original HIT files include scenery beyond every source section's
            // contact bounds. Preserve those faces without treating them as road.
            bool in_world=false;
            for(unsigned candidate=0;candidate<sections && !in_world;++candidate) {
                bool inside=true;
                for(unsigned plane=0;plane<4;++plane) {
                    const auto at=State::world_base+candidate*State::world_stride+64+plane*16;
                    float distance=-std::bit_cast<float>(memory.load<std::uint32_t>(at+12));
                    for(unsigned axis=0;axis<3;++axis)
                        distance+=std::bit_cast<float>(memory.load<std::uint32_t>(at+axis*4))*center[axis];
                    inside&=distance>=0;
                }
                in_world=inside;
            }
            missing_faces+=in_world || !direct;
        }
    }
    write_text(output/"collision-misses.csv",collision_misses,true);
    require(wheel_faces!=0,"native wheels accept the imported road surfaces");
    require(!missing_faces,std::format("individual road surfaces retain native collision ({}/{} missed)",missing_faces,road_faces));
    // Exercise the original material dispatcher and vertex binding, stopping
    // at the draw wrapper so no game window or gameplay session is needed.
    constexpr unsigned context=0x2256C0,commands=0x02000000;
    memory.store<std::uint32_t>(0x2256B8,context);
    memory.store<std::uint32_t>(context,commands);memory.store<std::uint32_t>(context+4,commands+65536);
    memory.store<std::uint32_t>(context+0x2C,1);
    const auto stride=invoke(0xD8410,0,{0x2C51D4});
    require(stride==24,"original textured world declaration matches imported vertices");
    memory.store<std::uint32_t>(0x4B8698,stride);
    const auto material_table=memory.load<std::uint32_t>(master+12);
    const auto world_materials=memory.load<std::uint32_t>(State::world_base+0x7AC);
    for(unsigned material=142;material<142+world_materials;++material) {
        const auto flags=memory.load<std::uint32_t>(material_table+material*52+16);
        require(flags==0x80000000U || flags==0x20000000U,"imported material selects the original opaque or cutout triangle-strip renderer");
    }
    unsigned world_draws=0,odd_draws=0,barrier_triangles=0,barrier_contacts=0;
    memory.store<std::uint32_t>(manager+0x310,1);memory.store<std::uint32_t>(manager+0x2C8,8);
    for(unsigned i=0;i<sections;++i) {
        const auto sector=State::world_base+i*State::world_stride;
        memory.store<std::uint32_t>(manager+0x118,sector);memory.store<std::uint32_t>(manager+0xD0+8*4,sector);
        for(unsigned j=0;j<memory.load<std::uint32_t>(sector+0xBC);++j) {
            const auto mesh=memory.load<std::uint32_t>(sector+0xC4+j*4),material=memory.load<std::uint32_t>(mesh+20);
            memory.store<std::uint32_t>(context,commands);
            state.diagnostic.break_address=0xEE140;state.diagnostic.break_hit=1;
            try {invoke(0x90BC0,manager,{material-142});throw std::runtime_error("World draw boundary was not reached");}
            catch(const std::runtime_error&) {if(cpu.eip!=0xEE140) throw;}
            state.diagnostic.break_address=0;
            const auto arguments=cpu.registers[esp],count=memory.load<std::uint32_t>(mesh+16);
            require(memory.load<std::uint32_t>(arguments)==0xC5570 && memory.load<std::uint32_t>(arguments+4)==6,
                "original world dispatcher submits a triangle strip");
            require(memory.load<std::uint32_t>(arguments+8)==count && memory.load<std::uint32_t>(arguments+12)==memory.load<std::uint32_t>(mesh+12),
                "original draw retains imported index count and storage");
            require(memory.load<std::uint32_t>(0x2242F0)==stride && memory.load<std::uint32_t>(0x2242F8)==mesh,
                "original SDK binds the imported vertex buffer with its correct stride");
            const auto texture=memory.load<std::uint32_t>(material_table+material*52+20),raster=memory.load<std::uint32_t>(texture);
            require(memory.load<std::uint32_t>(context+0xB78)==memory.load<std::uint32_t>(raster+0x38),
                "original material dispatcher binds the imported texture");
            const auto cutout=memory.load<std::uint32_t>(material_table+material*52+16)==0x20000000U;
            require(memory.load<std::uint32_t>(0x5AD170)==unsigned(cutout) && (!cutout ||
                (memory.load<std::uint32_t>(0x5AD168)==0x204 && memory.load<std::uint32_t>(0x5AD174)==
                    memory.load<std::uint8_t>(master+imported_alpha_offset+material))),
                "original material dispatcher preserves transparent cutouts");
            ++world_draws;odd_draws+=count&1;
            if(material!=142+world_materials-1) continue;
            memory.store<std::uint32_t>(collision_manager+0x34,i);
            const auto indices=memory.load<std::uint32_t>(mesh+12),vertices=memory.load<std::uint32_t>(mesh+4);
            for(unsigned k=2;k<count;++k) {
                std::array<unsigned,3> index;
                for(unsigned corner=0;corner<3;++corner) index[corner]=memory.load<std::uint16_t>(indices+(k-2+corner)*2);
                if(index[0]==index[1] || index[1]==index[2] || index[0]==index[2]) continue;
                std::array<std::array<float,3>,3> points;
                for(unsigned corner=0;corner<3;++corner) for(unsigned axis=0;axis<3;++axis)
                    points[corner][axis]=std::bit_cast<float>(memory.load<std::uint32_t>(vertices+index[corner]*24+axis*4));
                std::array<float,3> center{},normal{};
                for(unsigned axis=0;axis<3;++axis) {
                    center[axis]=(points[0][axis]+points[1][axis]+points[2][axis])/3;
                    const auto a=(axis+1)%3,b=(axis+2)%3;
                    normal[axis]=(points[1][a]-points[0][a])*(points[2][b]-points[0][b])-
                        (points[1][b]-points[0][b])*(points[2][a]-points[0][a]);
                }
                const auto length=std::hypot(normal[0],normal[2]);require(length>0,"barrier face has a horizontal collision direction");
                center[1]=std::min({points[0][1],points[1][1],points[2][1]})+0.5f;
                auto from=center,to=center;
                for(unsigned axis:{0U,2U}) {from[axis]-=normal[axis]/length;to[axis]+=normal[axis]/length;}
                ++barrier_triangles;barrier_contacts+=ray_contact(from,to) || ray_contact(to,from);
            }
        }
    }
    require(world_draws && odd_draws,"native draw checks include strips that previously failed as incomplete line lists");
    require(invoke(0xD8410,0,{0x2C51D4})==24,"environment declaration matches imported positions, UVs and colour");
    require(memory.load<std::uint32_t>(master+0x3C)==3,"all original horizon materials are present");
    unsigned environment_triangles=0;
    for(unsigned part=0;part<4;++part) {
        const auto mesh=part?memory.load<std::uint32_t>(master+0x40)+(part-1)*24:memory.load<std::uint32_t>(master+0x38);
        const auto count=memory.load<std::uint32_t>(mesh+16),indices=memory.load<std::uint32_t>(mesh+12);
        unsigned faces=0;
        for(unsigned i=2;i<count;++i) {
            const auto a=memory.load<std::uint16_t>(indices+(i-2)*2),b=memory.load<std::uint16_t>(indices+(i-1)*2),c=memory.load<std::uint16_t>(indices+i*2);
            faces+=a!=b && b!=c && a!=c;
        }
        constexpr unsigned expected[]={528,146,4,331};
        require(faces==expected[part],"environment strips retain every original triangle");environment_triangles+=faces;
        memory.store<std::uint32_t>(context,commands);
        state.diagnostic.break_address=0xEE140;state.diagnostic.break_hit=1;
        try {invoke(0xC5550,0x4B8690,{mesh});throw std::runtime_error("Environment draw boundary was not reached");}
        catch(const std::runtime_error&) {if(cpu.eip!=0xEE140) throw;}
        state.diagnostic.break_address=0;
        const auto arguments=cpu.registers[esp];
        require(memory.load<std::uint32_t>(arguments+4)==6 && memory.load<std::uint32_t>(arguments+8)==count &&
            memory.load<std::uint32_t>(arguments+12)==indices,"native sky and horizon rendering uses the converted strips");
    }
    require(environment_triangles==1009,"complete sky and skyline geometry");
    require(barrier_triangles==712 && barrier_contacts==barrier_triangles,"every imported barrier face has its original wall collision at car height");
    const auto scene=memory.load<std::uint32_t>(0x34AB5C);
    memory.store<std::uint32_t>(0x34AB5C,0xAB80);memory.store<std::uint32_t>(0xAB88,0);
    for(const unsigned rate:{60,144,360}) {
        state.imported_arrows(0x8BCD0,0x4B85B0);
        memory.store<std::uint32_t>(0x598188,std::bit_cast<std::uint32_t>(1.0f/rate));
        const auto frame=[&](unsigned viewport) {
            state.diagnostic.break_address=0x8E500;state.diagnostic.break_hit=1;
            try {invoke(0x90D30,viewport,{0xAA00,0});throw std::runtime_error("World animation boundary was not reached");}
            catch(const std::runtime_error&) {if(cpu.eip!=0x8E500) throw;}
            state.diagnostic.break_address=0;
        };
        for(unsigned tick=1;tick<=rate*2;++tick) {
            frame(0x4B95B0);
            const auto expected=(tick*12/rate)%5;
            require(memory.load<std::uint32_t>(state.imported.barrier+20)==state.imported.frames[expected],
                "native world rendering advances the five arrow frames at the original speed");
        }
        const auto texture=memory.load<std::uint32_t>(state.imported.barrier+20);
        memory.store<std::uint32_t>(0xAB88,1);frame(0x4B95B0);
        require(memory.load<std::uint32_t>(state.imported.barrier+20)==texture,"paused arrows retain their current frame");
        memory.store<std::uint32_t>(0xAB88,0);frame(0x4B98C8);
        require(memory.load<std::uint32_t>(state.imported.barrier+20)==texture,"additional viewports do not advance the arrows");
    }
    state.imported_arrows(0x8BCD0,0x4B85B0);
    for(unsigned i=0;i<5;++i) {
        const auto texture=state.imported.frames[i];const auto name=std::format("d_arrws{}",i+1);
        require(!std::strcmp(static_cast<const char*>(memory.access(texture+16,32)),name.c_str()),"arrow frames retain their original sequence");
        memory.store<std::uint32_t>(state.imported.barrier+20,texture);memory.store<std::uint32_t>(context,commands);
        invoke(0x8D1A0,0x4B85B0,{216,1});
        const auto raster=memory.load<std::uint32_t>(texture);
        require(memory.load<std::uint32_t>(context+0xB78)==memory.load<std::uint32_t>(raster+0x38),"native material binding uses the selected arrow frame");
    }
    memory.store<std::uint32_t>(state.imported.barrier+20,state.imported.frames[0]);
    memory.store<std::uint32_t>(0x34AB5C,scene);state.imported.selected=false;state.imported.barrier=0;
    Renderer cutout_renderer(128,128,false,render_debug_layer);
    std::array<Vertex,4> quad;
    for(unsigned i=0;i<4;++i) {
        quad[i].position={i&1?1.0f:-1.0f,i&2?-1.0f:1.0f,0.5f,1};
        for(auto& uv:quad[i].uv) uv={i&1?1.0f:0.0f,i&2?1.0f:0.0f,0,1};
    }
    for(const auto name:{"ftrees","telepole","farrw"}) {
        unsigned material=142;
        for(;material<142+world_materials;++material) {
            const auto texture=memory.load<std::uint32_t>(material_table+material*52+20);
            if(!std::strcmp(static_cast<const char*>(memory.access(texture+16,32)),name)) break;
        }
        require(material<142+world_materials,"captured cutout material is present");
        memory.store<std::uint32_t>(context,commands);invoke(0x8D1A0,0x4B85B0,{material,1});
        const auto texture=memory.load<std::uint32_t>(material_table+material*52+20);
        const auto width=memory.load<std::uint32_t>(texture+0x6C),height=memory.load<std::uint32_t>(texture+0x70);
        const auto format=(memory.load<std::uint32_t>(texture+0xA4)>>8)&255;
        const auto pixels=memory.load<std::uint32_t>(texture+0xB4),size=((width+3)/4)*((height+3)/4)*(format==12?8:16);
        RenderState draw;draw.primitive=6;draw.texture_modes[0]=1;draw.combiner_control=1;
        draw.color_inputs[0]=0x04080000;draw.alpha_inputs[0]=0x14180000;draw.color_outputs[0]=draw.alpha_outputs[0]=0xC0;
        draw.alpha_test=memory.load<std::uint32_t>(0x5AD170)!=0;
        draw.alpha_function=memory.load<std::uint32_t>(0x5AD168);draw.alpha_reference=memory.load<std::uint32_t>(0x5AD174);
        require(draw.alpha_test,"original game enables alpha testing for the captured artifact");
        require(draw.alpha_reference==240,"first-pass B1 cutouts retain their original alpha reference");
        draw.textures[0]=cutout_renderer.upload({width,height,static_cast<TextureFormat>(format),
            {static_cast<const std::byte*>(memory.access(pixels,size)),size}});
        const auto render=[&](bool alpha) {
            draw.alpha_test=alpha;cutout_renderer.prepare(draw);cutout_renderer.begin(0xFF204080);
            cutout_renderer.draw(draw,quad);cutout_renderer.end();return cutout_renderer.readback();
        };
        const auto opaque=render(false),cutout=render(true);
        unsigned preserved=0,opaque_background=0;
        for(unsigned y=4;y<124;++y) for(unsigned x=4;x<124;++x) {
            const auto at=(y*128+x)*4;
            const auto background=[&](const Image& image) {return image.rgba[at]==std::byte{32} && image.rgba[at+1]==std::byte{64} && image.rgba[at+2]==std::byte{128};};
            preserved+=background(cutout);opaque_background+=background(opaque);
        }
        require(preserved && preserved<120*120 && !opaque_background,"transparent pixels retain the scene and opaque pixels remain visible");
        save_png(cutout,output/(std::string(name)+"-cutout.png"));cutout_renderer.release(draw.textures[0]);
    }
    require(cutout_renderer.errors().empty(),"imported cutouts pass D3D11 debug validation");
    const auto prop_stride=invoke(0xD8410,0,{0x2C5034});
    require(prop_stride==20,"original instance declaration matches imported vertices");
    memory.store<std::uint32_t>(0x4B8620,prop_stride);memory.store<std::uint32_t>(0x4B863C,1);
    const auto props=memory.load<std::uint32_t>(master+0x1C),prop_count=memory.load<std::uint32_t>(master+0x18);
    for(unsigned i=0;i<prop_count;++i) {
        const auto mesh=props+i*120,material=memory.load<std::uint32_t>(mesh+20);
        require(material>=142+world_materials && material<memory.load<std::uint32_t>(master+4) &&
            memory.load<std::uint32_t>(material_table+material*52+16)==0x40000U,"instance material uses its own vertex pipeline");
        memory.store<std::uint32_t>(context,commands);
        state.diagnostic.break_address=0xC39A0;state.diagnostic.break_hit=1;
        try {invoke(0x8D1A0,0x4B85B0,{material,1});throw std::runtime_error("Instance material boundary was not reached");}
        catch(const std::runtime_error&) {if(cpu.eip!=0xC39A0) throw;}
        state.diagnostic.break_address=0;
        require(cpu.registers[ecx]==0x4B8618,"original material dispatcher selects the instance pipeline");
        invoke(0xC3160,0x4B8618,{mesh});
        state.diagnostic.break_address=0xEE140;state.diagnostic.break_hit=1;
        try {invoke(0xC3C30,0x4B8618,{mesh});throw std::runtime_error("Instance draw boundary was not reached");}
        catch(const std::runtime_error&) {if(cpu.eip!=0xEE140) throw;}
        state.diagnostic.break_address=0;
        const auto arguments=cpu.registers[esp];
        require(memory.load<std::uint32_t>(arguments)==0xC3C4A && memory.load<std::uint32_t>(arguments+4)==6 &&
            memory.load<std::uint32_t>(arguments+8)==memory.load<std::uint32_t>(mesh+16) &&
            memory.load<std::uint32_t>(arguments+12)==memory.load<std::uint32_t>(mesh+12),"original instance draw submits the imported triangle strip");
        require(memory.load<std::uint32_t>(0x2242F0)==prop_stride && memory.load<std::uint32_t>(0x2242F8)==mesh,
            "original SDK binds the imported instance with its correct stride");
    }
    require(invoke(0x184C0,0x352728)==1,"original language initialization");
    // Read the original main-menu row's mode and label operands, then pass
    // them through its original row builder. This catches mode-ID mix-ups.
    const auto attack_mode=std::to_integer<unsigned char>(image.data(0x437A2,1)[0]);
    const auto attack_label=std::to_integer<unsigned char>(image.data(0x43786,1)[0])/4U;
    memory.store<std::uint32_t>(0xA800,0);memory.store<std::uint32_t>(0xA804,0xA820);
    invoke(0x43220,0xA800,{attack_label,0,attack_mode});
    const auto label=memory.load<std::uint32_t>(0xA820);
    require(!std::memcmp(memory.access(label,12),"Time Attack",12) && attack_mode==8,
        "original main-menu label identifies Time Attack mode 8");
    require(u32(image.data(0x16529,4),0)==attack_mode,"original Time Attack initializer uses the same gameplay mode");
    std::array<unsigned,7> tables{},names{},welcomes{};
    for(unsigned language=0;language<tables.size();++language) {
        tables[language]=memory.load<std::uint32_t>(0x35272C+language*4);
        names[language]=memory.load<std::uint32_t>(tables[language]+390*4);
        welcomes[language]=memory.load<std::uint32_t>(tables[language]+728*4);
    }
    // Verify original frontend list construction without booting the game.
    constexpr std::pair<unsigned,unsigned> widgets[]={
        {0x4CB330,0x328D28},{0x4CB354,0x326508},{0x4CB35C,0x320BE0},
        {0x4CB360,0x320690},{0x4CB364,0x31E8D8},{0x4CB368,0x322C80},
        {0x4CB374,0x31CEA0},{0x4CB370,0x324B70},{0x4CB358,0x31C958},{0x4CB380,0x31DD7C}};
    for(const auto& [destination,source]:widgets) memory.store<std::uint32_t>(destination,memory.load<std::uint32_t>(source));
    const auto menu_fixture=[&] {
        // Stop after original list construction, before the
        // original buttons allocate draw queues for the on-screen frontend.
        state.diagnostic.break_address=0x2AB40;state.diagnostic.break_hit=1;
        try {invoke(0x4D290,0x3344D8);throw std::runtime_error("Track menu boundary was not reached");}
        catch(const std::runtime_error&) {
            if(cpu.eip!=0x2AB40) throw;
        }
        state.diagnostic.break_address=0;
    };
    memory.store<std::uint32_t>(0x4CCABC,attack_mode);menu_fixture();
    require(memory.load<std::uint32_t>(0x3344E4)==30,"Time Attack retains only its original tracks");
    for(unsigned language=0;language<tables.size();++language) {
        require(memory.load<std::uint32_t>(tables[language]+390*4)==names[language],"Time Attack retains original track labels");
        require(memory.load<std::uint32_t>(tables[language]+728*4)==welcomes[language],"Time Attack retains original opening captions");
    }
    // Imported content is exercised only by this explicit offline diagnostic.
    state.select_imported_track(true);memory.store<std::uint32_t>(0x4CCAE0,3);
    memory.store<std::uint32_t>(0x34AB58,attack_mode);
    require(title_track(cpu,memory,0x18030),"imported laps cannot enter built-in track records");
    std::vector<std::byte> records(0x6AA0);std::memcpy(records.data(),memory.access(0x34ABC8,records.size()),records.size());
    invoke(0x18030,0x34ABC8,{0});
    require(cpu.registers[esp]==stack+8 && !std::memcmp(records.data(),memory.access(0x34ABC8,records.size()),records.size()),
        "native record update preserves built-in records for imported laps");
    require(state.imported_file("trackpss/forward/fwyl.pss")==std::filesystem::absolute(assets)/"interstate.pss","selected master mount");
    require(state.imported_file("tracks/forward/fwyl/fwyl1_01.bgd")==std::filesystem::absolute(assets)/"interstate.bgd","selected race mount");
    require(state.imported_file("tracks/forward/fwyl/fwyl_0.bcd")==std::filesystem::absolute(assets)/"interstate.bcd","selected cinematic camera mount");
    constexpr unsigned music_object=0xAD00;
    const std::array<unsigned,3> music_before{0,0,5};
    unsigned music_value=0;for(const auto address:{0x57C210U,0x57C1ECU,0x57C208U})
        memory.store<std::uint32_t>(address,music_before[music_value++]);
    state.diagnostic.break_address=0xC91D0;state.diagnostic.break_hit=1;
    try {invoke(0xC93C0,music_object);throw std::runtime_error("Music selection boundary was not reached");}
    catch(const std::runtime_error&) {if(cpu.eip!=0xC91D0) throw;}
    state.diagnostic.break_address=0;
    const auto music_playlist=state.imported.playlist,music_songs=state.imported.songs;
    const auto saved_playlist=state.imported.music_playlist;const auto saved_song=state.imported.first_song;
    require(music_playlist && memory.load<std::uint32_t>(music_playlist+0x38)==1 &&
        memory.load<std::uint32_t>(music_songs)==6,"original catalogue selects Interstate's seventh Burnout 1 song");
    require(memory.load<std::uint32_t>(0x57C210)==1 && memory.load<std::uint32_t>(0x57C208)==0,
        "Interstate enables its native music playlist automatically");
    const auto music_course=memory.load<std::uint32_t>(0x48A150);
    for(const unsigned file_table:{0x3401D8,0x3402D8}) {
        unsigned course=0;
        for(;course<32;++course) {
            const auto directory=memory.load<std::uint32_t>(0x33FDD8+course*4);
            if(memory.load<std::uint8_t>(directory+5)==(file_table==0x3401D8?'0':'1')) break;
        }
        require(course<32,"original track music metadata supplies both included WMA banks");
        memory.store<std::uint32_t>(0x48A150,course);
        state.diagnostic.break_address=0x1091D0;state.diagnostic.break_hit=1;
        try {invoke(0xC91D0,music_object);throw std::runtime_error("Music stream boundary was not reached");}
        catch(const std::runtime_error&) {if(cpu.eip!=0x1091D0) throw;}
        state.diagnostic.break_address=0;
        require(cpu.registers[edi]==file_table+24,"native music routing selects the included seventh song in either disc bank");
        const auto filename=memory.load<std::uint32_t>(cpu.registers[edi]);
        const auto name=std::format("d:\\music{}\\b1wma\\07.wma",file_table==0x3401D8?0:1);
        require(!std::strcmp(static_cast<const char*>(memory.access(filename,name.size()+1)),name.c_str()) &&
            title_track(cpu,memory,0xC92D3),"native one-song policy uses Interstate's included WMA file");
    }
    memory.store<std::uint32_t>(0x48A150,music_course);
    constexpr std::pair<const char*,const char*> mounted[]={
        {"D:\\trackpss\\forward\\fwyl.pss","interstate.pss"},
        {"D:\\tracks1\\forward\\fwyl\\fwyl.psw","interstate.psw"},
        {"D:\\tracks\\forward\\fwyl\\fwyl1_01.bgd","interstate.bgd"}};
    for(const auto& [name,asset]:mounted) {
        std::memcpy(memory.access(0xA300,std::strlen(name)+1),name,std::strlen(name)+1);
        const auto handle=invoke(0xE09BA,0,{0xA300,GENERIC_READ,FILE_SHARE_READ,0,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,0});
        require(handle!=UINT32_MAX,"original SDK opens the imported asset mount");
        memory.store<std::uint64_t>(0xA240,0);
        require(invoke(0xFFF00000U+219*16,0,{handle,0,0,0,0xA250,0xA400,256,0xA240})==success &&
            memory.load<std::uint32_t>(0xA254)==256,"original file service reads imported asset bytes");
        File file(assets/asset);const auto expected=file.read(0,256);
        require(!std::memcmp(memory.access(0xA400,256),expected.data(),256),"mounted bytes match the converted file");
        require(invoke(0xFFF00000U+211*16,0,{handle,0xA250,0xA600,52,34})==success &&
            memory.load<std::uint64_t>(0xA628)==file.size() && memory.load<std::uint32_t>(0xA630)==0x21,
            "mounted files report converted sizes and read-only disc attributes");
        require(invoke(0xFFF00000U+218*16,0,{handle,0xA250,0xA600,32,5})==success &&
            memory.load<std::uint32_t>(0xA600)==0x00080000,"mounted files retain the original disc filesystem");
        memory.store<std::uint32_t>(0xA200,0xFFFFFFFD);memory.store<std::uint32_t>(0xA204,0xA210);
        memory.store<std::uint16_t>(0xA210,static_cast<std::uint16_t>(std::strlen(name)));
        memory.store<std::uint16_t>(0xA212,static_cast<std::uint16_t>(std::strlen(name)));
        memory.store<std::uint32_t>(0xA214,0xA300);
        require(invoke(0xFFF00000U+210*16,0,{0xA200,0xA600})==success &&
            memory.load<std::uint64_t>(0xA628)==file.size() && memory.load<std::uint32_t>(0xA630)==0x21,
            "path metadata matches the imported file handle");
        require(invoke(0xFFF00000U+187*16,0,{handle})==success,"imported asset handle closes");
    }
    // Retain another owner to exercise the original playlist Release without
    // constructing an audio device in this asset/menu diagnostic.
    memory.store<std::uint32_t>(0xAF00,2);memory.store<std::uint32_t>(0x57C1F4,0xAF00);
    state.select_imported_track(false);
    state.music_call(cpu,0x20DDB3,0xC925C,0,{0xAF00});memory.store<std::uint32_t>(0x57C1F4,0);
    menu_fixture();
    require(memory.load<std::uint32_t>(0x3344E4)==30,"original menu remains unchanged after offline asset checks");
    memory.store<std::uint32_t>(0x3344E8,3);invoke(0x4CC40,0x3344D8);
    require(!state.imported.selected && state.imported_file("trackpss/forward/fwyl.pss").empty(),"original Interstate Loop remains selectable");
    require(!state.imported.playlist && !std::memcmp(memory.access(music_playlist,68),saved_playlist.data(),68) &&
        !std::memcmp(memory.access(music_songs,36),saved_song.data(),36),"leaving Interstate restores the original Burnout 1 catalogue");
    music_value=0;for(const auto address:{0x57C210U,0x57C1ECU,0x57C208U})
        require(memory.load<std::uint32_t>(address)==music_before[music_value++],"leaving Interstate restores the user's music selection");
    require(!memory.load<std::uint32_t>(0x57C1F4) && memory.load<std::uint32_t>(0xAF00)==1,
        "leaving Interstate releases its native music playlist");
    require(!title_track(cpu,memory,0x18030),"original track record updates remain enabled");
    for(unsigned language=0;language<tables.size();++language) {
        require(memory.load<std::uint32_t>(tables[language]+390*4)==names[language],"original track names are restored");
        require(memory.load<std::uint32_t>(tables[language]+728*4)==welcomes[language],"original opening captions are restored");
    }
    memory.store<std::uint32_t>(0x352730,tables[0]);
    state.select_imported_track(true);state.select_imported_track(false);
    require(memory.load<std::uint32_t>(tables[0]+390*4)==names[0],"shared language tables retain their original track name");
    require(memory.load<std::uint32_t>(tables[0]+728*4)==welcomes[0],"shared language tables retain their original opening caption");
    for(const unsigned mode:{2,5}) {
        memory.store<std::uint32_t>(0x4CCABC,mode);menu_fixture();
        require(memory.load<std::uint32_t>(0x3344E4)==30,"other modes keep their original track list");
    }
    const auto report=std::format("{{\"format\":\"b2-track-import-check-v1\",\"passed\":true,\"game_booted\":false,\"checks\":{},\"sections\":{},\"collision_triangles\":{},\"road_contacts\":{},\"instances\":{},\"lap_metres\":{},\"native_world_draws\":{},\"native_prop_draws\":{},\"odd_index_draws\":{},\"checkpoints\":{},\"native_checkpoint_crossings\":{},\"barrier_triangles\":{},\"barrier_contacts\":{},\"barrier_frames\":5,\"arrow_rates\":[60,144,360],\"music_track\":7,\"native_music_selection\":true,\"cutout_images\":3,\"native_time_attack_menu\":true,\"offline_import_selection\":true,\"time_attack_mode\":{}}}",checks,sections,triangles,contacts,instances,metres,world_draws,prop_count,odd_draws,checkpoint_count,checkpoint_crossings,barrier_triangles,barrier_contacts,attack_mode);
    write_text(output/"report.json",report+'\n',true);return report;
}
bool title_track(Cpu& cpu,Memory& memory,std::uint32_t address,std::uint32_t object) {
    if(!cpu.kernel || !cpu.kernel->context) return false;
    if(cpu.kernel->entries[255]!=Xbox::State::dispatch_kernel<255>) throw NativeBoundary(address,"imported track requires the verified Xbox host provider");
    auto& host=*static_cast<Xbox::State*>(cpu.kernel->context);
    if(&host.memory!=&memory) throw NativeBoundary(address,"imported track received another guest memory");
    if(host.host.imported_tracks.empty()) return false;
    HostFloatingScope floating(cpu.floating);
    if(address==0x18030) return host.imported.selected && memory.load<std::uint32_t>(0x34AB58)==8;
    if(address==0x84850) return host.imported.selected;
    if(address==0xB5100) return host.imported_camera_zoom(cpu,object);
    if(address==0x8BCD0 || address==0x90D30) {host.imported_arrows(address,object);return false;}
    if(address==0xC93C0) {host.imported_music(cpu,object);return false;}
    if(address==0xC91F7) {if(host.imported.playlist) cpu.registers[edi]+=24;return false;}
    if(address==0xC92D3) return host.imported.playlist!=0;
    return false;
}
bool title_music(Cpu& cpu,Memory& memory,std::uint32_t address) {
    if(!cpu.kernel || !cpu.kernel->context) return false;
    if(cpu.kernel->entries[255]!=Xbox::State::dispatch_kernel<255>) throw NativeBoundary(address,"music controls require the verified Xbox host provider");
    auto& host=*static_cast<Xbox::State*>(cpu.kernel->context);
    if(&host.memory!=&memory) throw NativeBoundary(address,"music controls received another guest memory");
    HostFloatingScope floating(cpu.floating);return host.music_control(cpu,address);
}
Cpu& Xbox::isolated_cpu() {
    if(state_->current || !state_->threads.empty()) throw std::runtime_error("Isolated CPU requires an unused Xbox provider");
    auto& thread=state_->create(0,0,0,state_->image.stack_commit,0,false);
    state_->current=state_->isolated_owner=&thread;thread.cpu.preempt=nullptr;return thread.cpu;
}
void Xbox::isolated_io() {state_->pump_isolated();}
BootResult Xbox::isolated_result() const {return state_->inspect();}
void title_enhancement(Cpu& cpu,Memory& memory,std::uint32_t address) {
    if(!cpu.kernel || !cpu.kernel->context || cpu.kernel->entries[255]!=Xbox::State::dispatch_kernel<255>)
        throw NativeBoundary(address,"title enhancement requires the verified Xbox host provider");
    auto& host=*static_cast<Xbox::State*>(cpu.kernel->context);
    if(&host.memory!=&memory) throw NativeBoundary(address,"title enhancement received another guest memory");
    const auto registers=cpu.registers;const auto flags=cpu.flags,pc=cpu.eip;
    HostFloatingScope floating(cpu.floating);
    host.enhancement(cpu,address);
    cpu.registers=registers;cpu.flags=flags;cpu.eip=pc;
}
}
