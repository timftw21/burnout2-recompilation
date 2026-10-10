#include <winsock2.h>
#include <ws2tcpip.h>
#include "xbox.h"
#include "disc.h"
#include "input.h"
#include "audio.h"
#include "spatial.h"
#include "effects.h"
#include "render.h"
#include "diagnostics.h"
#include "crypto.h"
#include <Windows.h>
#include <bcrypt.h>
#include <iphlpapi.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <map>
#include <optional>
#include <sstream>

namespace b2 {
namespace {
constexpr std::uint32_t success=0,invalid_handle=0xC0000008,invalid_parameter=0xC000000D,
    no_memory=0xC0000017,conflicting_addresses=0xC0000018,timeout=0x102,
    name_not_found=0xC0000034,access_denied=0xC0000022,write_protected=0xC00000A2,
    buffer_too_small=0xC0000023,no_media=0xC0000013,end_of_file=0xC0000011;
constexpr std::uint32_t page=4096,kernel_base=0x80010000,return_sentinel=0xFFFFFFF0;
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
    struct Allocation { std::uint32_t size,protect; std::vector<std::uint32_t> pages; };
    struct OpenFile {
        DiscEntry entry{};
        std::string path,link,pattern;
        std::uint64_t position=0;
        std::uint32_t access=0,share=0,options=0;
        bool device=false,listing_loaded=false;
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
        std::uint32_t file_object=0,event=0,status=0,ordinal=0,callsite=0,requested=0,buffer=0;
        std::uint64_t offset=0;
        bool active=false;
        ~PendingIo() {if(overlap.hEvent) CloseHandle(overlap.hEvent);}
    };
    Xbe& image;
    Memory& memory;
    std::span<std::byte> ram;
    std::unique_ptr<Disc> disc;
    std::shared_ptr<OpenFile> disc_io;
    std::array<PendingIo,32> pending_io{};
    unsigned pending_io_count=0;
    std::unique_ptr<Input> owned_input;
    std::unique_ptr<Audio> audio;
    float output_gain=1;
    std::unique_ptr<Spatial> audio_spatial;
    struct AudioBuffer {
        std::uint32_t raw,descriptor,voice,identifier,references=1;AudioFormat format;
        std::uint32_t flags=0,input_bin=UINT32_MAX;
        SpatialParameters spatial{},deferred_spatial{};
        EnvironmentParameters environment{},deferred_environment{};
        std::uint32_t curve=0,curve_count=0,deferred_curve=0,deferred_curve_count=0;
        bool has_spatial=false,pending_spatial=false,pending_curve=false,pending_environment=false;
    };
    std::map<std::uint32_t,AudioBuffer> audio_buffers;
    std::uint32_t audio_device=0,audio_references=0;
    std::uint32_t audio_effect_descriptor=0,audio_effect_count=0;
    std::uint32_t audio_effect_program_words=0,audio_effect_scratch_bytes=0;
    SpatialListener audio_listener,audio_deferred_listener;
    std::uint32_t audio_pending_listener=0;
    bool audio_full_hrtf=false;
    struct AudioEvent {
        std::uint32_t clock=0,address=0,caller=0,count=0,identifier=0,source=0,start=0,bytes=0;
        std::array<std::uint32_t,6> arguments{};
    };
    std::array<AudioEvent,512> audio_events{};
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
    std::vector<std::unique_ptr<Thread>> threads;
    Thread* current=nullptr;
    void* scheduler=nullptr;
    bool converted=false,stopping=false,entry_returned=false;
    std::string boundary;
    std::uint32_t next_handle=0x100,next_thread=4,heap_begin=0,gdt=0,kernel_cursor=0x1000,tick_address=0,shutdown_head=0;
    std::uint32_t thread_type=0,event_type=0,semaphore_type=0,file_type=0;
    std::uint32_t flicker_filter=0;
    bool luma_filter=false;
    std::vector<std::uint32_t> shutdown_registrations;
    std::size_t next_runnable=0;
    LARGE_INTEGER performance_frequency{};
    std::chrono::steady_clock::time_point started=std::chrono::steady_clock::now();
    std::chrono::steady_clock::time_point quantum_due;
    std::chrono::steady_clock::time_point video_started;
    std::uint64_t video_ticks=0,vblank_callbacks=0;

    State(Xbe& image,Memory& memory,std::span<std::byte> ram,const std::filesystem::path& disc_path,XboxHost host):image(image),memory(memory),ram(ram),host(std::move(host)) {
        if(ram.size()!=64*1024*1024 || !image.supported()) throw std::runtime_error("Native Xbox requires the verified image and 64 MiB RAM");
        if(!disc_path.empty()) {
            disc=std::make_unique<Disc>(disc_path);
            const auto executable=disc->find("default.xbe");
            if(disc->sha256(executable)!=image.sha256) throw std::runtime_error("Mounted disc executable does not match the verified target");
            disc_io=std::make_shared<OpenFile>();
            disc_io->native=CreateFileW(disc_path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED|FILE_FLAG_RANDOM_ACCESS,nullptr);
            if(disc_io->native==INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot open mounted disc for native asynchronous reads");
        }
        if(this->host.storage_root.empty()) {
            std::array<wchar_t,32768> executable{};
            const auto length=GetModuleFileNameW(nullptr,executable.data(),static_cast<DWORD>(executable.size()));
            if(!length || length==executable.size()) throw std::runtime_error("Cannot locate native save storage");
            user_root=std::filesystem::path(std::wstring_view(executable.data(),length)).parent_path()/"data/user";
        } else user_root=std::filesystem::absolute(this->host.storage_root).lexically_normal();
        user_root=std::filesystem::absolute(user_root).lexically_normal();
        if(std::filesystem::weakly_canonical(user_root)!=user_root)
            throw std::runtime_error("Native storage path crosses a reparse point");
        std::filesystem::create_directories(user_root/"TDATA");
        std::filesystem::create_directories(user_root/"UDATA");
        heap_begin=align_up(image.base+image.image_size,page);
        memory.map(kernel_base,kernel_image);
        memory.map(0x80000000U+heap_begin,ram.subspan(heap_begin));
        memory.map(0xF0000000U+heap_begin,ram.subspan(heap_begin));
        QueryPerformanceFrequency(&performance_frequency);
        clock={this,[](void* context) {
            const auto& host=*static_cast<State*>(context);
            const auto ns=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now()-host.started).count());
            return (ns/1000000000)*733333333+(ns%1000000000)*733333333/1000000000;
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
        IMAGE_DOS_HEADER dos{}; dos.e_magic=IMAGE_DOS_SIGNATURE; dos.e_lfanew=0x100;
        IMAGE_NT_HEADERS32 pe{};
        pe.Signature=IMAGE_NT_SIGNATURE; pe.FileHeader.Machine=IMAGE_FILE_MACHINE_I386;
        pe.FileHeader.NumberOfSections=1; pe.FileHeader.SizeOfOptionalHeader=sizeof(IMAGE_OPTIONAL_HEADER32);
        pe.OptionalHeader.Magic=IMAGE_NT_OPTIONAL_HDR32_MAGIC; pe.OptionalHeader.ImageBase=kernel_base;
        pe.OptionalHeader.SectionAlignment=page; pe.OptionalHeader.FileAlignment=512;
        pe.OptionalHeader.SizeOfImage=static_cast<DWORD>(kernel_image.size()); pe.OptionalHeader.SizeOfHeaders=page;
        IMAGE_SECTION_HEADER section{};
        std::memcpy(section.Name,".data",5); section.VirtualAddress=page;
        section.Misc.VirtualSize=static_cast<DWORD>(kernel_image.size()-page);
        section.Characteristics=IMAGE_SCN_CNT_INITIALIZED_DATA|IMAGE_SCN_MEM_READ|IMAGE_SCN_MEM_WRITE;
        std::memcpy(kernel_image.data(),&dos,sizeof(dos));
        std::memcpy(kernel_image.data()+0x100,&pe,sizeof(pe));
        std::memcpy(kernel_image.data()+0x100+sizeof(pe),&section,sizeof(section));
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
            if(BCryptGenRandom(nullptr,reinterpret_cast<PUCHAR>(keys.data()),static_cast<ULONG>(keys.size()),BCRYPT_USE_SYSTEM_PREFERRED_RNG)<0)
                throw std::runtime_error("Cannot create native save identity");
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
        diagnostic_record("{\"type\":\"storage_ready\",\"root\":"+json(utf8(user_root.wstring()))+"}");
    }
    std::uint32_t allocate(std::uint32_t size,std::uint32_t alignment,bool committed,std::uint32_t protect,
                           std::uint32_t requested=0,std::uint32_t lowest=0,std::uint32_t highest=UINT32_MAX,bool clear=true) {
        const auto fail=[&] {last_failed_allocation={size,alignment,requested,lowest,highest};return 0U;};
        if(!size) return fail();
        size=align_up(size,page); alignment=std::max(alignment,page);
        auto cursor=align_up(std::max(heap_begin,lowest),alignment);
        if(requested) { if(requested<heap_begin || (requested&(page-1))) return fail(); cursor=requested; }
        for(const auto& [base,block] : allocations) {
            if(static_cast<std::uint64_t>(cursor)+size<=base) break;
            if(cursor>=static_cast<std::uint64_t>(base)+block.size) continue;
            if(requested) return fail();
            cursor=align_up(base+block.size,alignment);
        }
        if(static_cast<std::uint64_t>(cursor)+size>ram.size() || static_cast<std::uint64_t>(cursor)+size-1>highest) return fail();
        allocations.emplace(cursor,Allocation{size,protect,std::vector<std::uint32_t>(size/page,committed?protect:0U)});
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
    std::uint32_t argument(const Cpu& cpu,unsigned index) { return memory.load<std::uint32_t>(cpu.registers[esp]+4+index*4); }
    void finish(Cpu& cpu,unsigned cleanup,std::optional<std::uint64_t> result=std::nullopt,bool wide=false) {
        cpu.eip=memory.load<std::uint32_t>(cpu.registers[esp]);
        cpu.registers[esp]+=4+cleanup;
        if(result) cpu.registers[eax]=static_cast<std::uint32_t>(*result);
        if(wide && result) cpu.registers[edx]=static_cast<std::uint32_t>(*result>>32);
    }
    Thread& create(std::uint32_t routine,std::uint32_t context,std::uint32_t system,std::uint32_t stack_size,
                   std::uint32_t tls_size,bool suspended,bool entry=false) {
        if(threads.size()>=64) throw NativeBoundary(current?current->cpu.eip:image.entry,"native thread limit exceeded");
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
        thread.state=ThreadState::ended;
        host.memory.store<std::uint32_t>(thread.object+4,1);
        SwitchToFiber(host.scheduler);
        std::terminate();
    }
    void yield(ThreadState state=ThreadState::ready,std::uint32_t object=0,
               std::chrono::steady_clock::time_point due=std::chrono::steady_clock::time_point::max()) {
        if(!scheduler) throw NativeBoundary(current?current->cpu.eip:0,"offline native call requires an initialized scheduler to wait");
        if(memory.load<std::uint8_t>(current->tib+0x24)>1) throw NativeBoundary(current->cpu.eip,"a DPC attempted a blocking wait");
        current->state=state; current->wait_object=object; current->due=due;
        if(object) current->wait_kind=WaitKind::critical;
        {PerfScope idle(host.performance,PerfPhase::scheduler);SwitchToFiber(scheduler);}
        if(stopping) throw ThreadExit{0xC0000120};
        current->state=ThreadState::running;
        current->wait_kind=WaitKind::none;
    }
    void dispatcher(std::uint32_t address,unsigned type,unsigned size,std::uint32_t signal) {
        memory.access(address,size);
        memory.fill_elements<std::uint32_t>(address,0,size/4,false);
        memory.store<std::uint8_t>(address,static_cast<std::uint8_t>(type));
        memory.store<std::uint8_t>(address+2,static_cast<std::uint8_t>(size/4));
        memory.store<std::uint32_t>(address+4,signal);
        memory.store<std::uint32_t>(address+8,address+8); memory.store<std::uint32_t>(address+12,address+8);
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
        if(type!=0 && type!=1 && type!=5 && type!=6 && type!=8 && type!=9)
            throw NativeBoundary(current?current->cpu.eip:image.entry,"unsupported guest dispatcher object type");
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
        if(!objects.contains(address) || object_references[address]) return;
        if(std::ranges::any_of(handles,[&](const auto& pair) {return pair.second.address==address;})) return;
        allocations.erase(address&0x0FFFFFFF); objects.erase(address); object_references.erase(address);
    }
    std::uint32_t wait(std::span<const std::uint32_t> addresses,bool all,std::uint32_t timeout_pointer,bool alertable) {
        if(addresses.empty() || addresses.size()>64) return invalid_parameter;
        if(all) for(unsigned i=0;i<addresses.size();++i) for(unsigned j=0;j<i;++j) if(addresses[i]==addresses[j]) return invalid_parameter;
        if(alertable) throw NativeBoundary(current->cpu.eip,"alertable waits require APC delivery");
        const auto due=timeout_pointer?due_time(std::bit_cast<std::int64_t>(memory.load<std::uint64_t>(timeout_pointer))):
            std::chrono::steady_clock::time_point::max();
        struct References {
            State& host; std::span<const std::uint32_t> addresses;
            ~References() { for(auto address:addresses) if(host.objects.contains(address)) {
                --host.object_references[address]; host.release_object(address);
            }}
        } references{*this,addresses};
        for(auto address:addresses) if(objects.contains(address)) ++object_references[address];
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
    std::optional<std::filesystem::path> writable_path(std::string_view name) {
        constexpr std::string_view root="\\DEVICE\\HARDDISK0\\PARTITION";
        const auto key=folded(name);
        if(!key.starts_with(root) || key.size()<root.size()+1) return std::nullopt;
        const auto partition=key[root.size()];
        if(partition!='1' && (partition<'3' || partition>'5')) return std::nullopt;
        const auto prefix_size=root.size()+1;
        if(key.size()>prefix_size && key[prefix_size]!='\\') return std::nullopt;
        auto path=user_root;
        auto validate=[&](const std::filesystem::path& candidate) {
            const auto attributes=GetFileAttributesW(candidate.c_str());
            return attributes==INVALID_FILE_ATTRIBUTES || !(attributes&FILE_ATTRIBUTE_REPARSE_POINT);
        };
        if(!validate(path)) throw NativeBoundary(current->cpu.eip,"native storage root is a reparse point");
        if(partition!='1') {
            path/="CACHE";if(!validate(path)) throw NativeBoundary(current->cpu.eip,"native cache root is a reparse point");
            path/="PARTITION"+std::string(1,partition);if(!validate(path)) throw NativeBoundary(current->cpu.eip,"native cache partition is a reparse point");
        }
        std::size_t start=prefix_size+1;
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
            path/=std::string(part);
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
            const auto stack_base=memory.load<std::uint32_t>(cpu.fs_base+4);
            const auto index=memory.load<std::uint32_t>(image.tls->index_address);
            const auto tls=memory.load<std::uint32_t>(stack_base+index*4);
            memory.store<std::uint32_t>(tls+4,error);
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
    void update_spatial(AudioBuffer& buffer) {
        if(!buffer.has_spatial) return;
        if(!audio_spatial) audio_spatial=std::make_unique<Spatial>(memory);
        const auto curve=buffer.curve_count?std::span(static_cast<const std::byte*>(memory.access(buffer.curve,buffer.curve_count*4)),buffer.curve_count*4):std::span<const std::byte>{};
        const auto result=audio_spatial->calculate(audio_listener,buffer.spatial,curve,&buffer.environment,(buffer.flags&0x20000)!=0);
        audio->spatial(buffer.identifier,result);
    }
    static void copy_listener(SpatialListener& destination,const SpatialListener& source,unsigned fields) {
        if(fields&1) destination.position=source.position;
        if(fields&2) destination.velocity=source.velocity;
        if(fields&4) {destination.front=source.front;destination.up=source.up;}
        if(fields&8) destination.distance=source.distance;
        if(fields&16) destination.rolloff=source.rolloff;
        if(fields&32) destination.doppler=source.doppler;
    }
    void commit_spatial() {
        const bool listener_changed=audio_pending_listener!=0;
        copy_listener(audio_listener,audio_deferred_listener,audio_pending_listener);
        audio_pending_listener=0;
        for(auto& [object,buffer]:audio_buffers) {
            const bool changed=listener_changed || buffer.pending_spatial || buffer.pending_curve || buffer.pending_environment;
            if(buffer.pending_spatial) {buffer.spatial=buffer.deferred_spatial;buffer.has_spatial=true;buffer.pending_spatial=false;}
            if(buffer.pending_curve) {buffer.curve=buffer.deferred_curve;buffer.curve_count=buffer.deferred_curve_count;buffer.pending_curve=false;}
            if(buffer.pending_environment) {buffer.environment=buffer.deferred_environment;buffer.pending_environment=false;}
            if(changed) update_spatial(buffer);
        }
    }
    void audio_api(Cpu& cpu,std::uint32_t address) {
        if(audio_trace && audio) {
            const auto sample_clock=audio->sample_clock();
            if(std::bit_cast<std::int32_t>(sample_clock-audio_trace_until)>=0) audio_trace=false;
            else {
                unsigned count=0;
                switch(address) {
                case 0x0022D896:case 0x0022D8D2:case 0x0022F8EA:count=4;break;
                case 0x0022EF4B:case 0x0022E908:case 0x0022D8F6:count=3;break;
                case 0x0022EF2F:case 0x0022E754:case 0x0022E770:case 0x0022D952:count=2;break;
                case 0x0022D8BA:case 0x0022C11B:count=1;break;
                }
                if(count) {
                    auto& event=audio_events[audio_event_count++%audio_events.size()];event={};
                    event.clock=sample_clock;event.address=address;event.caller=memory.load<std::uint32_t>(cpu.registers[esp]);event.count=count;
                    for(unsigned i=0;i<count;++i) event.arguments[i]=argument(cpu,i);
                    const auto found=audio_buffers.find(event.arguments[0]);
                    if(found!=audio_buffers.end()) {
                        const auto& buffer=found->second;event.identifier=buffer.identifier;
                        event.source=memory.load<std::uint32_t>(buffer.descriptor+0xB8);
                        event.start=memory.load<std::uint32_t>(buffer.descriptor+0xC0);
                        event.bytes=memory.load<std::uint32_t>(buffer.descriptor+0xC4);
                    }
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
            }
            finish(cpu,0);return;
        }
        if(address==0x0022C131) {audio_full_hrtf=true;finish(cpu,0);return;}
        if(address==0x0022FC18) {
            if(argument(cpu,0) || argument(cpu,2)) throw NativeBoundary(address,"non-default audio device or aggregation is unsupported");
            if(!audio) {
                audio=std::make_unique<Audio>(true,output_gain);
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
        const auto object=argument(cpu,0);
        if(address==0x0022D792) {
            if(object!=audio_device) throw NativeBoundary(address,"invalid native sound device");
            audio->mixbin_headroom(argument(cpu,1),argument(cpu,2));finish(cpu,12,success);return;
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
                for(auto& [buffer_object,buffer]:audio_buffers) update_spatial(buffer);
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
            auto& buffer=audio_buffers.emplace(public_object,AudioBuffer{raw,body,voice,identifier,1,format,flags,input_bin}).first->second;
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
            const auto references=--buffer.references;
            if(!references){audio->release(buffer.identifier);allocations.erase(buffer.raw);audio_buffers.erase(found);}
            finish(cpu,4,references);return;
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
        case 0x0022D87A:audio_mixbins(buffer.identifier,argument(cpu,1),true);finish(cpu,8,success);return;
        case 0x0022E738:audio->frequency(buffer.identifier,argument(cpu,1));finish(cpu,8,success);return;
        case 0x0022E754: {
            const auto destination=argument(cpu,1);
            const auto target=audio_buffers.find(destination);
            if(destination && target==audio_buffers.end()) throw NativeBoundary(address,"invalid audio output buffer");
            audio->route(buffer.identifier,destination?target->second.identifier:0);finish(cpu,8,success);return;
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
        gpu->flip=host.flip;
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
    void submit_graphics() {
        constexpr std::uint32_t context=0x002256C0;
        if(!gpu) throw std::runtime_error("Native GPU submission precedes device initialization");
        const auto flags=memory.load<std::uint32_t>(context+8);
        const auto end=memory.load<std::uint32_t>(context+((flags&4)?0x358:0));
        const auto ring_begin=memory.load<std::uint32_t>(context+0x24),ring_end=memory.load<std::uint32_t>(context+0x28);
        if(gpu_cursor<ring_begin || end<gpu_cursor || end>ring_end) throw std::runtime_error("Original GPU submission left its push-buffer ring");
        if(end!=gpu_cursor) {gpu->submit(gpu_cursor,end);gpu_cursor=end;}
        if(memory.load<std::uint32_t>(0x00225210)) {gpu->wait();memory.store<std::uint32_t>(context+8,flags|0x2000);}
    }
    void platform(Cpu& cpu,std::uint32_t address) {
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
        if(length) characters=to_unicode?MultiByteToWideChar(1252,0,static_cast<const char*>(bytes),length,nullptr,0):
            WideCharToMultiByte(1252,0,static_cast<const wchar_t*>(bytes),length/2,nullptr,0,nullptr,nullptr);
        if(length && !characters) return invalid_parameter;
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
            const auto written=to_unicode?MultiByteToWideChar(1252,0,static_cast<const char*>(bytes),length,static_cast<wchar_t*>(target),characters):
                WideCharToMultiByte(1252,0,static_cast<const wchar_t*>(bytes),length/2,static_cast<char*>(target),characters,nullptr,nullptr);
            if(written!=characters) return invalid_parameter;
        }
        std::memset(static_cast<std::byte*>(target)+required-unit,0,unit);
        memory.store<std::uint16_t>(destination,static_cast<std::uint16_t>(required-unit));
        return success;
    }
    void io_status(std::uint32_t address,std::uint32_t status,std::uint32_t information=0) {
        memory.store<std::uint32_t>(address,status); memory.store<std::uint32_t>(address+4,information);
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
        if(write && file.native==INVALID_HANDLE_VALUE) return fail(write_protected);
        if(!write && !(file.access&(0x80000000U|1U))) return fail(access_denied);
        const auto sector=file.native!=INVALID_HANDLE_VALUE?512U:2048U;
        // Original sample.xsb/b2distfx.bin callers use ordinary heap buffers.
        // Enforce sector transfers without imposing Windows DMA alignment on
        // the guest's pointer; our mounted-image handle is buffered.
        if((file.options&8) && ((offset%sector) || (length%sector))) return fail(invalid_parameter);
        memory.access(status,8);const auto buffer=memory.access(output,length);
        const auto size=file_size(file);
        if(!length) {io_complete(cpu,success);return success;}
        if(!write && offset>=size) return fail(end_of_file);
        const auto count=write?length:static_cast<std::uint32_t>(std::min<std::uint64_t>(length,size-offset));
        auto request=std::ranges::find(pending_io,false,&PendingIo::active);
        if(request==pending_io.end()) throw NativeBoundary(cpu.eip,"native asynchronous file queue exceeds 32 requests");
        if(!request->overlap.hEvent) {
            request->overlap.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
            if(!request->overlap.hEvent) return fail(no_memory);
        }
        const auto native_event=request->overlap.hEvent;
        request->overlap={};request->overlap.hEvent=native_event;ResetEvent(native_event);
        request->file=object.file;request->file_object=object.address;
        request->event=event?handles.at(event).address:0;request->status=status;
        request->ordinal=write?236:219;request->callsite=memory.load<std::uint32_t>(cpu.registers[esp]);
        request->offset=offset;request->requested=length;request->buffer=output;
        request->native=file.native!=INVALID_HANDLE_VALUE?file.native:disc_io->native;
        const auto native_offset=offset+(file.native!=INVALID_HANDLE_VALUE || file.device?0:std::uint64_t(file.entry.sector)*2048);
        request->overlap.Offset=static_cast<DWORD>(native_offset);request->overlap.OffsetHigh=static_cast<DWORD>(native_offset>>32);
        io_status(status,0x103);memory.store<std::uint32_t>(object.address+0x38+4,0);
        if(request->event) memory.store<std::uint32_t>(request->event+4,0);
        const auto issued=write?WriteFile(request->native,buffer,count,nullptr,&request->overlap):
            ReadFile(request->native,buffer,count,nullptr,&request->overlap);
        if(!issued && GetLastError()!=ERROR_IO_PENDING) {
            const auto result=file_error(GetLastError());request->file.reset();return fail(result);
        }
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
            io_status(request.status,result,transferred);
            memory.store<std::uint32_t>(request.file_object+0x38+4,1);
            if(request.event) memory.store<std::uint32_t>(request.event+4,1);
            if(!live || host.trace_io || result!=success) {
                if(io_trace.size()==64) io_trace.pop_front();
                io_trace.push_back({request.ordinal,result,transferred,request.callsite,request.file->path,
                    request.file->options,request.requested,request.offset,request.buffer});
            }
            request.file.reset();request.active=false;--pending_io_count;
        }
    }
    std::uint32_t open_file(Cpu& cpu,unsigned options_index,unsigned share_index,std::uint32_t disposition,std::uint32_t& information) {
        const auto name=object_name(argument(cpu,2));
        if(!name) return invalid_parameter;
        const auto relative=disc_path(*name);
        if(!relative) return open_writable(cpu,*name,options_index,share_index,disposition,information);
        if(!disc) return no_media;
        const auto entry=disc->lookup(*relative);
        if(!entry) return name_not_found;
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
        auto file=std::make_shared<OpenFile>();file->entry=*entry;file->path=*name;
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
        if(file.native!=INVALID_HANDLE_VALUE) return file.entry.directory()?0x10U:0x20U;
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
        const auto unit=file.native!=INVALID_HANDLE_VALUE?16384ULL:2048ULL,allocated=(size+unit-1)&~(unit-1);
        if(file.native!=INVALID_HANDLE_VALUE && (type==4 || type==34)) {
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
            if(file.native!=INVALID_HANDLE_VALUE) {
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
        case 17:memory.store<std::uint32_t>(output,file.native!=INVALID_HANDLE_VALUE?511:2047);break;
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
            // from a partition-only XISO. Return the real unsupported status;
            // the original caller decides whether it can continue.
            if(opcode==0xAD) return 0xC00000BB;
            if(opcode!=0x5A || mode!=0x3E) throw NativeBoundary(cpu.eip,std::format("unsupported virtual disc SCSI command 0x{:02X}, page 0x{:02X}",opcode,mode));
            if(memory.load<std::uint8_t>(request+8)!=1) return invalid_parameter;
            const auto capacity=memory.load<std::uint32_t>(request+12),buffer=memory.load<std::uint32_t>(request+20);
            const auto requested=(memory.load<std::uint8_t>(request+35)<<8)|memory.load<std::uint8_t>(request+36);
            const auto count=std::min({capacity,static_cast<unsigned>(requested),28U});
            // A mounted partition-only XISO is an already readable virtual Xbox
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
                            BOOL completed;DWORD native_written=0;
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
                        const std::span destination(static_cast<std::byte*>(memory.access(output,written)),written);
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
                } else if(file.native==INVALID_HANDLE_VALUE) result=write_protected;
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
                if(type==3) {
                    if(length<24) result=buffer_too_small;
                    else {
                        if(file.native!=INVALID_HANDLE_VALUE) {
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
                    else {memory.store<std::uint32_t>(output,file.native!=INVALID_HANDLE_VALUE?7:2);memory.store<std::uint32_t>(output+4,file.native!=INVALID_HANDLE_VALUE?0:0x24);written=8;}
                } else if(type==5) {
                    const std::string_view name=file.native!=INVALID_HANDLE_VALUE?"FATX":"XDVDFS";
                    if(length<12+name.size()) result=buffer_too_small;
                    else {
                        memory.store<std::uint32_t>(output,file.native!=INVALID_HANDLE_VALUE?2:0x00080000);memory.store<std::uint32_t>(output+4,file.native!=INVALID_HANDLE_VALUE?42:255);
                        memory.store<std::uint32_t>(output+8,static_cast<std::uint32_t>(name.size()));
                        std::memcpy(memory.access(output+12,name.size()),name.data(),name.size());written=12+static_cast<unsigned>(name.size());
                    }
                } else throw NativeBoundary(cpu.eip,std::format("unsupported volume information class {}",type));
            } else if constexpr(Ordinal==198) {if(file.native!=INVALID_HANDLE_VALUE && !FlushFileBuffers(file.native)) result=file_error(GetLastError());}
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
            const auto entry=disc && relative?disc->lookup(*relative):std::nullopt;
            if(!entry) {finish(cpu,8,disc?name_not_found:no_media);return;}
            OpenFile file;file.entry=*entry;file.path=*name;
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
                MIB_IF_TABLE2* table=nullptr;const auto result=GetIfTable2(&table);
                if(result!=NO_ERROR) {
                    if constexpr(Ordinal==253) {finish(cpu,8,static_cast<std::uint32_t>(HRESULT_FROM_WIN32(result)));return;}
                    else throw NativeBoundary(cpu.eip,"native Ethernet status query failed");
                }
                std::unique_ptr<MIB_IF_TABLE2,decltype(&FreeMibTable)> interfaces(table,FreeMibTable);
                ethernet_state=0;
                for(unsigned i=0;i<table->NumEntries;++i) {
                    const auto& link=table->Table[i];
                    if(link.Type!=IF_TYPE_ETHERNET_CSMACD || !link.InterfaceAndOperStatusFlags.HardwareInterface ||
                        link.OperStatus!=IfOperStatusUp || link.MediaConnectState!=MediaConnectStateConnected) continue;
                    const auto speed=std::min(link.TransmitLinkSpeed,link.ReceiveLinkSpeed);
                    ethernet_state=1|(speed>=100000000?2U:speed>=10000000?4U:0U);break;
                }
                // Windows does not expose duplex here; do not invent either bit.
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
            if(flags==MEM_RELEASE) { allocations.erase(found); memory.store<std::uint32_t>(pointer,0); memory.store<std::uint32_t>(size_pointer,0); }
            else {
                const auto first=(base-found->first)/page;
                const auto last=size?align_up(base-found->first+size,page)/page:found->second.size/page;
                if(last>found->second.pages.size()) { finish(cpu,12,invalid_parameter); return; }
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
            const auto allocated=allocate(size,alignment?alignment:page,true,protect,0,lowest,highest);
            finish(cpu,Ordinal==166?20:Ordinal==15?8:4,allocated?allocated|0x80000000U:0U);
        } else if constexpr(Ordinal==171 || Ordinal==17) {
            const auto found=allocation(argument(cpu,0));
            if(found==allocations.end()) throw NativeBoundary(cpu.eip,"free of an unknown native guest allocation");
            allocations.erase(found); finish(cpu,4);
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
            using Convert=ULONG(WINAPI*)(LONG);
            static const auto convert=reinterpret_cast<Convert>(GetProcAddress(GetModuleHandleW(L"ntdll.dll"),"RtlNtStatusToDosError"));
            if(!convert) throw std::runtime_error("Native NTSTATUS conversion is unavailable");
            finish(cpu,4,convert(static_cast<LONG>(argument(cpu,0))));
        } else if constexpr(Ordinal==103) finish(cpu,0,memory.load<std::uint8_t>(cpu.fs_base+0x24));
        else if constexpr(Ordinal==104) finish(cpu,0,current->object);
        else if constexpr(Ordinal==129 || Ordinal==160 || Ordinal==161) {
            const auto prior=memory.load<std::uint8_t>(cpu.fs_base+0x24);
            memory.store<std::uint8_t>(cpu.fs_base+0x24,static_cast<std::uint8_t>(Ordinal==129?2:cpu.registers[ecx]));
            finish(cpu,0,Ordinal==161?std::nullopt:std::optional<std::uint64_t>{prior});
        } else if constexpr(Ordinal==126 || Ordinal==127) {
            LARGE_INTEGER value=performance_frequency;
            if constexpr(Ordinal==126) QueryPerformanceCounter(&value);
            finish(cpu,0,static_cast<std::uint64_t>(value.QuadPart),true);
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
                memory.store<std::uint32_t>(argument(cpu,3),0x00400101);
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
            else if(index==8 || index==9 || index==10 || index==17) value=0; // Native SD video, stereo, no parental restriction.
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
        }
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
        result.threads_created=static_cast<std::uint32_t>(threads.size());
        result.vblank_callbacks=vblank_callbacks;
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
        result.cpu.kernel=nullptr; result.cpu.diagnostic=nullptr; result.cpu.clock=nullptr; result.cpu.preempt=nullptr;
        diagnostic_guest(nullptr);
        // Resume sleeping native stacks with cancellation so their C++ and floating
        // scopes unwind before DeleteFiber releases host stacks.
        stopping=true;
        for(auto& thread : threads) if(thread->state!=ThreadState::ended) { current=thread.get(); SwitchToFiber(thread->fiber); }
        return result;
    }
};
Xbox::Xbox(Xbe& image,Memory& memory,std::span<std::byte> ram,const std::filesystem::path& disc,XboxHost host):state_(std::make_unique<State>(image,memory,ram,disc,std::move(host))) {}
Xbox::~Xbox()=default;
void Xbox::output_gain(float gain) {
    if(!(gain>=0 && gain<=1)) throw std::runtime_error("Output volume must be between zero and one");
    if(state_->audio) state_->audio->output_gain(gain);
    state_->output_gain=gain;
}
void Xbox::performance_recording(bool enabled) {
    if(state_->gpu) state_->gpu->performance(enabled?state_->host.performance:nullptr);
    if(state_->audio) state_->audio->performance(enabled);
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
void Xbox::capture_frame(const std::filesystem::path& directory) {
    if(!state_->gpu) throw std::runtime_error("The game has not initialized graphics yet");
    state_->gpu->capture_frame(directory);
    const auto& memory=state_->memory;
    std::ostringstream snapshot;
    snapshot<<std::format("{{\"format\":\"b2-game-state-v1\",\"phase\":\"capture_request\",\"source_sha256\":{},\"replay\":{{\"frames\":{},\"index\":{},\"loaded\":{},\"playback\":{},\"finished\":{},\"fade_state\":{},\"deadline_bits\":{},\"buffer\":{}}},\"memory\":[",
        json(state_->image.sha256),memory.load<std::uint32_t>(0x002FFD68),memory.load<std::uint32_t>(0x002FFD6C),
        memory.load<std::uint32_t>(0x002FFD70),memory.load<std::uint32_t>(0x002FFD74),memory.load<std::uint32_t>(0x002FFD78),
        memory.load<std::uint32_t>(0x002FFD7C),memory.load<std::uint32_t>(0x002FFD80),json(hex32(memory.load<std::uint32_t>(0x00522358))));
    // Bounded original title state; collecting it requires an explicit capture.
    constexpr std::pair<std::uint32_t,unsigned> slices[]={{0x002FFD68,0xA0},{0x002FE370,16},{0x00300080,0x60},
        {0x0034AB40,0x40},{0x003525E0,0x144},{0x0048A150,16},{0x004CD800,32},{0x00522358,12},{0x00489F70,5*0x50}};
    for(unsigned i=0;i<std::size(slices);++i) {
        if(i) snapshot<<',';const auto [address,size]=slices[i];
        snapshot<<std::format("{{\"address\":{},\"bytes\":{}}}",json(hex32(address)),json(hex_bytes({static_cast<const std::byte*>(memory.access(address,size)),size})));
    }
    snapshot<<"],\"io\":[";
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
        const auto count=std::min<std::uint32_t>(state_->audio_event_count,static_cast<std::uint32_t>(state_->audio_events.size()));
        report<<std::format("],\"events_truncated\":{},\"events\":[",state_->audio_event_count>count);
        for(unsigned i=0;i<count;++i) {
            const auto& event=state_->audio_events[(state_->audio_event_count-count+i)%state_->audio_events.size()];
            if(i) report<<',';
            report<<std::format("{{\"clock\":{},\"api\":{},\"caller\":{},\"id\":{},\"source\":{},\"start\":{},\"bytes\":{},\"arguments\":[",
                event.clock,json(hex32(event.address)),json(hex32(event.caller)),event.identifier,json(hex32(event.source)),event.start,event.bytes);
            for(unsigned a=0;a<event.count;++a) {if(a) report<<',';report<<json(hex32(event.arguments[a]));}
            report<<"]}";
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
    state.audio=std::make_unique<Audio>(false);state.audio_device=0xA008;
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
    const auto invoke=[&](std::uint32_t entry,std::span<const std::uint32_t> arguments) {
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,return_sentinel);
        for(unsigned i=0;i<arguments.size();++i) memory.store<std::uint32_t>(stack+4+i*4,arguments[i]);
        invoke_native(cpu,memory,entry);
        require(cpu.eip==return_sentinel && cpu.registers[esp]==stack+4+arguments.size()*4 && cpu.registers[eax]==0,"original/native setter ABI");
    };
    const auto values=[](const SpatialListener& listener) {
        return std::array<float,15>{listener.position[0],listener.position[1],listener.position[2],listener.velocity[0],listener.velocity[1],listener.velocity[2],
            listener.front[0],listener.front[1],listener.front[2],listener.up[0],listener.up[1],listener.up[2],listener.distance,listener.rolloff,listener.doppler};
    };
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
    const auto report=std::format("{{\"format\":\"b2-audio-binding-check-v1\",\"passed\":true,\"game_booted\":false,\"audio_device_opened\":false,\"cases\":{},\"original_setters\":6}}",checks);
    write_text(output/"audio-bindings.json",report,false);return report;
}
bool Xbox::save_frame(const std::filesystem::path& path) {
    if(!state_->gpu || (!state_->gpu->stats().clears && !state_->gpu->stats().draws)) return false;
    state_->gpu->snapshot();save_png(state_->graphics->readback(),path);return true;
}
std::string check_storage(const std::filesystem::path& executable,const std::filesystem::path& output) {
    Xbe image(executable);
    if(!image.supported() || std::filesystem::exists(output)) throw std::runtime_error("Storage checks require the verified XBE and a fresh output directory");
    std::filesystem::create_directories(output);
    unsigned checks=0;
    const auto require=[&](bool passed,std::string_view name) {
        if(!passed) throw std::runtime_error("Storage check failed: "+std::string(name));
        ++checks;
    };
    const auto reference=[&](Bytes key,Bytes bytes) {
        BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
        struct Scope {BCRYPT_ALG_HANDLE& algorithm;BCRYPT_HASH_HANDLE& hash;
            ~Scope(){if(hash) BCryptDestroyHash(hash);if(algorithm) BCryptCloseAlgorithmProvider(algorithm,0);}} scope{algorithm,hash};
        const auto check=[](NTSTATUS status){if(status<0) throw std::runtime_error("Storage reference hash failed");};
        check(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA1_ALGORITHM,nullptr,key.empty()?0:BCRYPT_ALG_HANDLE_HMAC_FLAG));
        check(BCryptCreateHash(algorithm,&hash,nullptr,0,reinterpret_cast<PUCHAR>(const_cast<std::byte*>(key.data())),static_cast<ULONG>(key.size()),0));
        if(!bytes.empty()) check(BCryptHashData(hash,reinterpret_cast<PUCHAR>(const_cast<std::byte*>(bytes.data())),static_cast<ULONG>(bytes.size()),0));
        ShaDigest digest{};check(BCryptFinishHash(hash,reinterpret_cast<PUCHAR>(digest.data()),static_cast<ULONG>(digest.size()),0));return digest;
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
        XboxHost provider;provider.storage_root=output;
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
        const auto text=[&](std::uint32_t address,std::string_view value) {
            std::memcpy(memory.access(address,value.size()),value.data(),value.size());memory.store<std::uint8_t>(address+static_cast<std::uint32_t>(value.size()),0);
        };
        File identity(std::filesystem::absolute(output)/"storage.key");std::array<std::byte,32> keys{};identity.read(0,keys);
        if(!reload) saved_keys=keys;else require(saved_keys==keys,"save identity survives a fresh runtime");
        // Reproduce the SDK's verified process-heap setup at E6075..E60A3 only.
        std::memset(memory.access(area,0x30),0,0x30);memory.store<std::uint32_t>(area,0x30);
        const auto heap=invoke(0x000E435A,{2,0,image.heap_reserve,image.heap_commit,0,area});require(heap!=0,"original process heap");
        memory.store<std::uint32_t>(0x005A8974,heap);
        require(invoke(0x000E5D08,{image.title_id,0})==success,"original title storage mount");
        text(area,"U:\\");const std::wstring name=L"Offline storage check";
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
        }
        if(reload==2) {
            require(memory.load<std::uint32_t>(profile+0x7AB4)==7,"overwritten profile persists in another fresh runtime");
            memory.store<std::uint8_t>(profile,memory.load<std::uint8_t>(profile)^1U);
            const auto changed=reference(span(key,16),span(profile,signed_size));
            require(reference(span(disk,16),changed)!=expected,"changed save data fails integrity verification");
        }
    }
    return std::format("{{\"format\":\"b2-storage-check-v1\",\"passed\":true,\"game_booted\":false,\"cases\":{},\"profile_bytes\":31748,\"reopened\":true,\"overwritten\":true,\"async_write\":true,\"runtime_decoder\":false,\"storage\":{}}}",checks,json(utf8(std::filesystem::absolute(output).wstring())));
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
    invoke(0x00123801,{});
    invoke(0x00139380,{});
    cpu.registers[ecx]=0x00303C70;invoke(0x000D9570,{5,0x00489F70,0x003D6864,0x00295B14});
    memory.store<std::uint32_t>(0x0034AB48,0x00303C70);
    constexpr std::uint32_t controller=0x002FFD68,buffer=0x01000000;
    memory.store<std::uint32_t>(0x003D686C,buffer);
    std::ostringstream report;unsigned cases=0;
    const auto check=[&](std::string_view path,unsigned track,unsigned mode,unsigned lesson,unsigned pal) {
        const auto entry=disc.find(path);
        if(entry.size<32 || entry.size>0x66000) throw std::runtime_error("Replay check file is outside the original replay-buffer bounds");
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
    return std::format("{{\"format\":\"b2-replay-check-v1\",\"passed\":true,\"game_booted\":false,\"cases\":{},\"files\":[{}]}}",cases,report.view());
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
}
