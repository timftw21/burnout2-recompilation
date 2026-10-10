#include "performance.h"
#include "file.h"
#include "target.h"
#include "kernel_names.h"
#include "native_api.h"
#include <Windows.h>
#include <Psapi.h>
#include <intrin.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>
#include <format>
#include <span>
#include <sstream>
#include <vector>

namespace b2 {
namespace {
constexpr std::array phase_names{"guest code","kernel services","platform services","GPU commands","draw submission","texture uploads","resource preparation","buffer uploads","GPU waits","frame publication","display/interpolation","Present","settings UI","frame pacing","scheduler/idle","recorder overhead","capture IO"};
constexpr std::array metric_names{"draws","vertices","buffer_maps","buffer_bytes","texture_uploads","texture_upload_bytes","target_creations","target_copies","state_cache_misses","layout_cache_misses","sampler_cache_misses","program_creations","shader_compilations","submissions","packets","methods","fences","displayed_frames","generated_frames","audio_batches","audio_mix_ns","audio_batches_over_budget"};
constexpr std::array gpu_wait_names{"unspecified","completion polling","completion queue capacity","requested semaphore value","GPU idle"};
std::string_view gpu_wait_name(std::uint32_t id) {return id<gpu_wait_names.size()?gpu_wait_names[id]:"unknown";}
constexpr unsigned frame_limit=4096,sample_limit=65536,resource_limit=128,cost_limit=2048;
struct Frame {
    std::uint64_t id=0,time=0,interval=0;
    std::array<std::uint64_t,phase_names.size()> phases{};
    PerfCounters counters{};
    std::array<std::uint64_t,3> gpu{};
    std::uint32_t spans=0,dropped=0,gpu_status=0,partial=0;
};
struct Sample {std::uint64_t time;std::uint32_t address,thread;};
struct Resource {
    std::uint64_t time=0,working_set=0,private_bytes=0,process_cpu_ns=0,main_cpu_ns=0,read_bytes=0,write_bytes=0;
    std::uint64_t local=0,budget=0,shared=0,textures=0,targets=0,texture_bytes=0,target_bytes=0;
    std::uint32_t page_faults=0,handles=0,valid=0,pad=0;
};
struct Cost {std::uint64_t ns=0,calls=0;std::uint32_t phase=0,id=0,used=0,pad=0;};
struct Header {
    std::array<char,8> magic{'B','2','P','E','R','F','1',0};
    std::uint32_t frame_size=sizeof(Frame),sample_size=sizeof(Sample),resource_size=sizeof(Resource),cost_size=sizeof(Cost);
    std::uint32_t frames=0,samples=0,resources=0,costs=0,frames_dropped=0,samples_dropped=0,costs_dropped=0,resources_dropped=0;
    std::uint32_t refresh=0,scale=0,interpolation=0,vsync=0,processors=0,pad=0;
    std::uint64_t duration=0,recorder_bytes=0;
    std::array<char,64> cpu{},build{};
    std::array<char,128> gpu{};
    std::array<char,65> target_sha256{};
};
struct Data {
    Header header;
    std::array<Frame,frame_limit> frames{};
    std::array<Sample,sample_limit> samples{};
    std::array<Resource,resource_limit> resources{};
    std::array<Cost,cost_limit> costs{};
};
static_assert(sizeof(Data)<4*1024*1024);
std::uint64_t filetime(FILETIME value) {return (std::uint64_t(value.dwHighDateTime)<<32)|value.dwLowDateTime;}
template<class T,std::size_t N> void write(std::ofstream& file,std::span<T,N> values) {file.write(reinterpret_cast<const char*>(values.data()),values.size_bytes());}
template<class T,std::size_t N> void read(std::ifstream& file,std::span<T,N> values) {
    if(!file.read(reinterpret_cast<char*>(values.data()),values.size_bytes())) throw std::runtime_error("Truncated performance capture");
}
double milliseconds(std::uint64_t ns) {return ns/1000000.0;}
std::string summary(const Data& data) {
    const auto& h=data.header;std::vector<double> intervals,work,gpu;
    std::array<std::uint64_t,phase_names.size()> phases{};PerfCounters counters{};
    unsigned spikes=0,diagnostic_frames=0,gpu_complete=0,gpu_partial=0;
    for(const auto& frame:std::span(data.frames).first(h.frames)) {
        if(frame.gpu_status==1) ++gpu_complete;
        else if(frame.gpu_status==2) ++gpu_partial;
        for(unsigned i=0;i<phases.size();++i) phases[i]+=frame.phases[i];
        for(unsigned i=0;i<counters.size();++i) counters[i]+=frame.counters[i];
        if(frame.partial) continue;
        if(frame.phases[unsigned(PerfPhase::capture)]) {++diagnostic_frames;continue;}
        intervals.push_back(milliseconds(frame.interval));
        const auto idle=frame.phases[unsigned(PerfPhase::pacing)]+frame.phases[unsigned(PerfPhase::scheduler)]+frame.phases[unsigned(PerfPhase::diagnostics)];
        work.push_back(milliseconds(frame.interval>idle?frame.interval-idle:0));
        spikes+=frame.interval>20000000;
        if(frame.gpu_status==1) gpu.push_back(milliseconds(frame.gpu[0]+frame.gpu[1]+frame.gpu[2]));
    }
    const auto distribution=[](std::vector<double> values) {
        std::ranges::sort(values);
        const auto percentile=[&](double p){return values.empty()?0.0:values[std::min(values.size()-1,std::size_t(std::ceil(p*values.size()))-1)];};
        return std::format("{{\"samples\":{},\"median_ms\":{:.6f},\"p95_ms\":{:.6f},\"p99_ms\":{:.6f},\"max_ms\":{:.6f}}}",values.size(),percentile(.5),percentile(.95),percentile(.99),percentile(1));
    };
    std::ostringstream out;
    out<<std::format("{{\"format\":\"b2-performance-summary-v1\",\"cpu\":{},\"gpu\":{},\"build\":{},\"logical_processors\":{},\"refresh_rate\":{},\"resolution_scale\":{},\"interpolation\":{},\"vsync\":{},\"duration_ms\":{:.3f},\"recorder_bytes\":{},\"source_intervals\":{},\"work_wall_time\":{},\"measured_gpu_spans\":{},\"frames_over_20_ms\":{},\"diagnostic_frames_excluded\":{},\"gpu_complete_frames\":{},\"gpu_partial_frames\":{},\"gpu_pending_frames\":{},\"limits\":{{\"frames_dropped\":{},\"samples_dropped\":{},\"costs_dropped\":{},\"resources_dropped\":{}}},\"phases\":[",
        json(h.cpu.data()),json(h.gpu.data()),json(h.build.data()),h.processors,h.refresh,h.scale,bool(h.interpolation),bool(h.vsync),milliseconds(h.duration),h.recorder_bytes,distribution(intervals),distribution(work),distribution(gpu),spikes,diagnostic_frames,gpu_complete,gpu_partial,h.frames-gpu_complete-gpu_partial,h.frames_dropped,h.samples_dropped,h.costs_dropped,h.resources_dropped);
    for(unsigned i=0;i<phases.size();++i) {if(i) out<<',';out<<std::format("{{\"name\":{},\"wall_ms\":{:.6f}}}",json(phase_names[i]),milliseconds(phases[i]));}
    out<<"],\"counters\":{";
    for(unsigned i=0;i<counters.size();++i) {if(i) out<<',';out<<json(metric_names[i])<<':'<<counters[i];}
    std::vector<Cost> costs;
    for(const auto& cost:data.costs) if(cost.used && cost.phase!=unsigned(PerfPhase::pacing) && cost.phase!=unsigned(PerfPhase::scheduler) && cost.phase!=unsigned(PerfPhase::diagnostics) && cost.phase!=unsigned(PerfPhase::capture)) costs.push_back(cost);
    std::ranges::sort(costs,[](const Cost& a,const Cost& b){return a.ns>b.ns;});
    out<<"},\"hot_operations\":[";
    for(unsigned i=0;i<std::min<std::size_t>(32,costs.size());++i) {
        const auto& cost=costs[i];if(i) out<<',';
        std::string_view name;
        if(cost.phase==unsigned(PerfPhase::kernel)) name=kernel_name(cost.id);
        if(cost.phase==unsigned(PerfPhase::platform)) {const auto api=std::ranges::find(native_apis,cost.id,&NativeApi::address);if(api!=native_apis.end()) name=api->name;}
        if(cost.phase==unsigned(PerfPhase::gpu_wait)) name=gpu_wait_name(cost.id);
        out<<std::format("{{\"phase\":{},\"id\":{},\"name\":{},\"calls\":{},\"exclusive_wall_ms\":{:.6f}}}",json(phase_names[cost.phase]),json(hex32(cost.id)),json(name),cost.calls,milliseconds(cost.ns));
    }
    out<<"],\"gpu_wait_operations\":[";bool first_wait=true;
    for(const auto& cost:costs) if(cost.phase==unsigned(PerfPhase::gpu_wait)) {
        if(!first_wait) out<<',';first_wait=false;
        out<<std::format("{{\"id\":{},\"name\":{},\"calls\":{},\"exclusive_wall_ms\":{:.6f}}}",json(hex32(cost.id)),json(gpu_wait_name(cost.id)),cost.calls,milliseconds(cost.ns));
    }
    struct Hot {std::uint32_t address;std::uint64_t samples;};std::vector<Hot> hot;
    std::vector<std::uint32_t> addresses;addresses.reserve(h.samples);
    for(const auto& sample:std::span(data.samples).first(h.samples)) addresses.push_back(sample.address);
    std::ranges::sort(addresses);
    for(auto address:addresses) {if(hot.empty() || hot.back().address!=address) hot.push_back({address,1});else ++hot.back().samples;}
    std::ranges::sort(hot,[](const Hot& a,const Hot& b){return a.samples>b.samples;});out<<"],\"guest_checkpoint_samples\":[";
    for(unsigned i=0;i<std::min<std::size_t>(32,hot.size());++i) {if(i) out<<',';out<<std::format("{{\"address\":{},\"samples\":{}}}",json(hex32(hot[i].address)),hot[i].samples);}
    std::uint64_t peak_working=0,peak_private=0,peak_local=0;unsigned resource_valid=0;
    for(const auto& r:std::span(data.resources).first(h.resources)) {resource_valid|=r.valid;peak_working=std::max(peak_working,r.working_set);peak_private=std::max(peak_private,r.private_bytes);peak_local=std::max(peak_local,r.local);}
    std::string process_cpu="null",main_cpu="null";
    if(h.resources>1) {
        const auto& first=data.resources[0];const auto& last=data.resources[h.resources-1];const auto wall=last.time-first.time;
        if(wall && h.processors && (first.valid&last.valid&2) && last.process_cpu_ns>=first.process_cpu_ns) process_cpu=std::format("{:.3f}",100.0*(last.process_cpu_ns-first.process_cpu_ns)/wall/h.processors);
        if(wall && (first.valid&last.valid&4) && last.main_cpu_ns>=first.main_cpu_ns) main_cpu=std::format("{:.3f}",100.0*(last.main_cpu_ns-first.main_cpu_ns)/wall);
    }
    out<<std::format("],\"process_cpu_percent_machine\":{},\"main_thread_cpu_percent_one_core\":{},\"peak_working_set_bytes\":{},\"peak_private_bytes\":{},\"peak_local_gpu_bytes\":{},\"target_sha256\":{},\"sampling\":\"Cooperative guest checkpoints, at most 1000 per second; addresses are hints, not per-function CPU times. CPU phase wall times include OS scheduling and service callbacks. GPU spans exclude idle gaps and the settings overlay; missing spans and pending queries are explicit. Settings describe the start of recording.\"}}",process_cpu,main_cpu,resource_valid&1?std::to_string(peak_working):"null",resource_valid&1?std::to_string(peak_private):"null",resource_valid&8?std::to_string(peak_local):"null",json(h.target_sha256.data()));
    return out.str();
}
void reports(const Data& data,const std::filesystem::path& directory) {
    std::filesystem::create_directories(directory);const auto report=summary(data);
    std::ofstream text(directory/"performance.txt"),file(directory/"performance.json");
    if(!text || !file) throw std::runtime_error("Cannot create performance reports");
    const auto& h=data.header;
    text<<"Burnout 2 performance capture\n"<<h.cpu.data()<<" / "<<h.gpu.data()<<"\n"<<h.refresh<<" Hz, "<<h.scale<<"x resolution, interpolation "<<(h.interpolation?"on":"off")<<", VSync "<<(h.vsync?"on":"off")<<"\n"
        <<h.frames<<" game-frame records, "<<h.samples<<" guest checkpoint samples, "<<milliseconds(h.duration)/1000<<" seconds\n"
        <<"Recorder storage: "<<h.recorder_bytes<<" bytes. Diagnostics are opt-in.\n"
        <<"See performance.json for frame percentiles, resource trends, hot operations and original code addresses.\n"
        <<"Work time excludes intentional frame pacing and scheduler idle. Present can include display/driver waits.\n"
        <<"GPU timings sum measured command spans, excluding idle gaps and the settings overlay. Check coverage before comparing to 16.67 ms.\n"
        <<"Checkpoint sampling is cooperative; it does not measure exact function costs. Kernel IDs are ordinals, platform IDs are original addresses, draw IDs are prepared shader handles.\n\n";
    std::vector<double> intervals;
    for(const auto& frame:std::span(data.frames).first(h.frames)) if(!frame.partial && !frame.phases[unsigned(PerfPhase::capture)]) intervals.push_back(milliseconds(frame.interval));
    std::ranges::sort(intervals);
    if(!intervals.empty()) text<<std::format("Game frames: median {:.3f} ms; 99th percentile {:.3f} ms; slowest {:.3f} ms. Target: 16.67 ms.\n",intervals[(intervals.size()-1)/2],intervals[std::min(intervals.size()-1,std::size_t(std::ceil(.99*intervals.size()))-1)],intervals.back());
    std::uint64_t peak_working=0,peak_private=0;
    for(const auto& r:std::span(data.resources).first(h.resources)) {peak_working=std::max(peak_working,r.working_set);peak_private=std::max(peak_private,r.private_bytes);}
    text<<std::format("Peak process RAM: {:.1f} MiB working set, {:.1f} MiB private memory.\n\n",peak_working/1048576.0,peak_private/1048576.0);
    std::array<std::uint64_t,phase_names.size()> phases{};
    for(const auto& frame:std::span(data.frames).first(h.frames)) if(!frame.partial && !frame.phases[unsigned(PerfPhase::capture)]) for(unsigned i=0;i<phases.size();++i) phases[i]+=frame.phases[i];
    std::array<unsigned,phase_names.size()> order{};for(unsigned i=0;i<order.size();++i) order[i]=i;
    std::ranges::sort(order,[&](auto a,auto b){return phases[a]>phases[b];});
    text<<"Work areas, largest first (exclusive main-thread wall time):\n";
    for(auto i:order) if(phases[i] && i!=unsigned(PerfPhase::pacing) && i!=unsigned(PerfPhase::scheduler) && i!=unsigned(PerfPhase::diagnostics) && i!=unsigned(PerfPhase::capture)) text<<std::format("  {}: {:.3f} ms\n",phase_names[i],milliseconds(phases[i]));
    text<<std::format("Waiting: frame pacing {:.3f} ms; scheduler/idle {:.3f} ms. Recorder sampling/collection: {:.3f} ms.\n",milliseconds(phases[unsigned(PerfPhase::pacing)]),milliseconds(phases[unsigned(PerfPhase::scheduler)]),milliseconds(phases[unsigned(PerfPhase::diagnostics)]));
    text<<"GPU completion waits (including small operations omitted from the hot list):\n";
    for(const auto& cost:data.costs) if(cost.used && cost.phase==unsigned(PerfPhase::gpu_wait))
        text<<std::format("  {}: {} calls, {:.3f} ms\n",gpu_wait_name(cost.id),cost.calls,milliseconds(cost.ns));
    file<<"{\"summary\":"<<report<<",\"phase_columns\":[";
    for(unsigned i=0;i<phase_names.size();++i) {if(i) file<<',';file<<json(phase_names[i]);}
    file<<"],\"counter_columns\":[";for(unsigned i=0;i<metric_names.size();++i) {if(i) file<<',';file<<json(metric_names[i]);}
    file<<"],\"gpu_columns\":[\"render\",\"copies/resources\",\"display/interpolation\"],\"gpu_status\":{\"pending\":0,\"complete\":1,\"partial_or_invalid\":2},\"resource_valid_bits\":{\"memory\":1,\"process_cpu\":2,\"main_thread_cpu\":4,\"gpu_memory\":8,\"io\":16,\"handles\":32},\"frames\":[";
    for(unsigned n=0;n<h.frames;++n) {
        const auto& f=data.frames[n];if(n) file<<',';
        file<<std::format("{{\"id\":{},\"time_ns\":{},\"interval_ns\":{},\"partial\":{},\"gpu_status\":{},\"gpu_spans\":{},\"gpu_dropped\":{},\"gpu_ns\":[{},{},{}],\"phase_ns\":[",f.id,f.time,f.interval,bool(f.partial),f.gpu_status,f.spans,f.dropped,f.gpu[0],f.gpu[1],f.gpu[2]);
        for(unsigned i=0;i<f.phases.size();++i) {if(i) file<<',';file<<f.phases[i];}
        file<<"],\"counters\":[";for(unsigned i=0;i<f.counters.size();++i) {if(i) file<<',';file<<f.counters[i];}file<<"]}";
    }
    file<<"],\"resources\":[";
    for(unsigned i=0;i<h.resources;++i) {
        const auto& r=data.resources[i];if(i) file<<',';
        file<<std::format("{{\"time_ns\":{},\"valid_flags\":{},\"working_set_bytes\":{},\"private_bytes\":{},\"process_cpu_ns\":{},\"main_thread_cpu_ns\":{},\"read_bytes\":{},\"write_bytes\":{},\"page_faults\":{},\"handles\":{},\"local_gpu_bytes\":{},\"local_gpu_budget\":{},\"shared_gpu_bytes\":{},\"texture_view_count\":{},\"target_count\":{},\"texture_source_bytes\":{},\"target_budget_bytes\":{}}}",r.time,r.valid,r.working_set,r.private_bytes,r.process_cpu_ns,r.main_cpu_ns,r.read_bytes,r.write_bytes,r.page_faults,r.handles,r.local,r.budget,r.shared,r.textures,r.targets,r.texture_bytes,r.target_bytes);
    }
    file<<"]}\n";if(!file || !text) throw std::runtime_error("Performance report write failed");
}
}
struct Performance::State {
    Data data;
    std::uint64_t started=0,last=0,frame_start=0,next_sample=0,next_resource=0;
    PerfPhase phase=PerfPhase::guest;std::uint32_t id=0;
    std::array<std::uint64_t,phase_names.size()> phases{};
    PerfCounters previous{};
    Cost* cost(PerfPhase kind,std::uint32_t identifier) {
        auto index=((identifier*2654435761U)^unsigned(kind))%cost_limit;
        for(unsigned n=0;n<cost_limit;++n,index=(index+1)%cost_limit) {
            auto& entry=data.costs[index];
            if(!entry.used) {entry.used=1;entry.phase=unsigned(kind);entry.id=identifier;return &entry;}
            if(entry.phase==unsigned(kind) && entry.id==identifier) return &entry;
        }
        ++data.header.costs_dropped;return nullptr;
    }
};
std::uint64_t performance_clock() noexcept {
    static const auto frequency=[] {LARGE_INTEGER value{};QueryPerformanceFrequency(&value);return std::uint64_t(value.QuadPart);}();
    LARGE_INTEGER value{};QueryPerformanceCounter(&value);
    const auto ticks=std::uint64_t(value.QuadPart);return (ticks/frequency)*1000000000ULL+(ticks%frequency)*1000000000ULL/frequency;
}
Performance::Performance()=default;
Performance::~Performance()=default;
void Performance::start(std::string_view gpu,unsigned refresh,unsigned scale,bool interpolation,bool vsync,const PerfCounters& counters) {
    state_=std::make_unique<State>();++generation_;auto& s=*state_;auto& h=s.data.header;
    h.refresh=refresh;h.scale=scale;h.interpolation=interpolation;h.vsync=vsync;h.processors=GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);h.recorder_bytes=sizeof(State);
    int info[4];__cpuid(info,0x80000000);
    if(unsigned(info[0])>=0x80000004) for(unsigned i=0;i<3;++i) {__cpuid(info,0x80000002+i);std::memcpy(h.cpu.data()+i*16,info,16);}
    else strcpy_s(h.cpu.data(),h.cpu.size(),"Unknown x64 CPU");
    for(unsigned i=48;i && h.cpu[i-1]==' ';--i) h.cpu[i-1]=0;
    const auto* module=reinterpret_cast<const std::byte*>(GetModuleHandleW(nullptr));
    const auto* dos=reinterpret_cast<const IMAGE_DOS_HEADER*>(module);
    const auto* image=reinterpret_cast<const IMAGE_NT_HEADERS64*>(module+dos->e_lfanew);
    strcpy_s(h.build.data(),h.build.size(),std::format("PE {:08X}, image {} bytes",image->FileHeader.TimeDateStamp,image->OptionalHeader.SizeOfImage).c_str());
    const auto length=std::min(gpu.size(),h.gpu.size()-1);std::memcpy(h.gpu.data(),gpu.data(),length);
    std::copy_n(target::sha256.begin(),std::min(target::sha256.size(),h.target_sha256.size()-1),h.target_sha256.begin());
    s.previous=counters;s.started=s.last=s.frame_start=performance_clock();s.next_sample=s.next_resource=s.started;active_=true;
}
void Performance::account(std::uint64_t now) {
    auto& s=*state_;const auto elapsed=now-s.last;s.phases[unsigned(s.phase)]+=elapsed;
    if(auto* cost=s.cost(s.phase,s.id)) cost->ns+=elapsed;s.last=now;
}
void Performance::stop() {if(active_) {const auto now=performance_clock();account(now);state_->data.header.duration=now-state_->started;active_=false;}}
void Performance::reset() {stop();state_.reset();++generation_;}
bool Performance::expired() const {return active_ && performance_clock()-state_->started>=60000000000ULL;}
bool Performance::resources_due() const {return active_ && performance_clock()>=state_->next_resource;}
void Performance::resources(std::uint64_t local,std::uint64_t budget,std::uint64_t shared,bool gpu_valid,std::uint64_t textures,std::uint64_t targets,std::uint64_t texture_bytes,std::uint64_t target_bytes) {
    if(!active_) return;auto& s=*state_;const auto now=performance_clock();s.next_resource=now+500000000;
    auto& h=s.data.header;if(h.resources==resource_limit) {++h.resources_dropped;return;}
    auto& r=s.data.resources[h.resources++];r.time=now-s.started;r.local=local;r.budget=budget;r.shared=shared;r.textures=textures;r.targets=targets;r.texture_bytes=texture_bytes;r.target_bytes=target_bytes;
    if(gpu_valid) r.valid|=8;
    PROCESS_MEMORY_COUNTERS_EX memory{};memory.cb=sizeof(memory);
    if(GetProcessMemoryInfo(GetCurrentProcess(),reinterpret_cast<PROCESS_MEMORY_COUNTERS*>(&memory),sizeof(memory))) {r.valid|=1;r.working_set=memory.WorkingSetSize;r.private_bytes=memory.PrivateUsage;r.page_faults=memory.PageFaultCount;}
    FILETIME created{},ended{},kernel{},user{};
    if(GetProcessTimes(GetCurrentProcess(),&created,&ended,&kernel,&user)) {r.valid|=2;r.process_cpu_ns=(filetime(kernel)+filetime(user))*100;}
    if(GetThreadTimes(GetCurrentThread(),&created,&ended,&kernel,&user)) {r.valid|=4;r.main_cpu_ns=(filetime(kernel)+filetime(user))*100;}
    IO_COUNTERS io{};if(GetProcessIoCounters(GetCurrentProcess(),&io)) {r.valid|=16;r.read_bytes=io.ReadTransferCount;r.write_bytes=io.WriteTransferCount;}
    DWORD handles=0;if(GetProcessHandleCount(GetCurrentProcess(),&handles)) {r.valid|=32;r.handles=handles;}
}
void Performance::guest_sample(std::uint32_t address,std::uint32_t thread) {
    if(!active_) return;auto& s=*state_;const auto now=performance_clock();if(now<s.next_sample) return;s.next_sample=now+1000000;
    auto& h=s.data.header;if(h.samples==sample_limit) {++h.samples_dropped;return;}s.data.samples[h.samples++]={now-s.started,address,thread};
}
void Performance::source_frame(std::uint64_t id,const PerfCounters& counters) {
    if(!active_) return;auto& s=*state_;const auto now=performance_clock();account(now);auto& h=s.data.header;
    if(h.frames==frame_limit) {++h.frames_dropped;return;}
    auto& frame=s.data.frames[h.frames++];frame.id=id;frame.time=now-s.started;frame.interval=now-s.frame_start;frame.phases=s.phases;frame.partial=h.frames==1;
    for(unsigned i=0;i<counters.size();++i) frame.counters[i]=counters[i]>=s.previous[i]?counters[i]-s.previous[i]:0;
    s.previous=counters;s.phases={};s.frame_start=now;h.duration=now-s.started;
}
void Performance::gpu_frame(std::uint64_t id,const std::array<std::uint64_t,3>& ns,unsigned spans,unsigned dropped,bool valid) {
    if(!state_) return;
    for(unsigned i=state_->data.header.frames;i;--i) if(auto& frame=state_->data.frames[i-1];frame.id==id) {
        frame.gpu=ns;frame.spans=spans;frame.dropped=dropped;frame.gpu_status=valid&&!dropped?1:2;return;
    }
}
bool Performance::has_gpu_samples() const {
    if(!state_) return false;
    for(const auto& frame:std::span(state_->data.frames).first(state_->data.header.frames)) if(frame.gpu_status==1 && frame.spans && (frame.gpu[0] || frame.gpu[1] || frame.gpu[2])) return true;
    return false;
}
PerfScope::PerfScope(Performance* profile,PerfPhase phase,std::uint32_t id) {
    if(!profile || !profile->active_) return;profile_=profile;generation_=profile->generation_;
    auto& s=*profile->state_;profile->account(performance_clock());previous_=s.phase;previous_id_=s.id;s.phase=phase;s.id=id;
    if(auto* cost=s.cost(phase,id)) ++cost->calls;
}
PerfScope::~PerfScope() {
    if(!profile_ || !profile_->active_ || generation_!=profile_->generation_) return;
    profile_->account(performance_clock());profile_->state_->phase=previous_;profile_->state_->id=previous_id_;
}
void Performance::save(const std::filesystem::path& directory) const {
    if(!state_) throw std::runtime_error("No performance recording");
    std::filesystem::create_directories(directory);const auto path=directory/"performance.bin";
    if(std::filesystem::exists(path)) throw std::runtime_error("Performance capture already exists");
    if(active_) state_->data.header.duration=performance_clock()-state_->started;
    state_->data.header.costs=cost_limit;auto header=state_->data.header;
    std::ofstream file(path,std::ios::binary);if(!file) throw std::runtime_error("Cannot create performance capture");
    write(file,std::span<const Header>(&header,1));write(file,std::span(state_->data.frames).first(header.frames));write(file,std::span(state_->data.samples).first(header.samples));
    write(file,std::span(state_->data.resources).first(header.resources));write(file,std::span(state_->data.costs));file.close();if(!file) throw std::runtime_error("Performance capture write failed");
    reports(state_->data,directory);
}
std::string inspect_performance(const std::filesystem::path& capture,const std::filesystem::path& output) {
    std::ifstream file(capture,std::ios::binary|std::ios::ate);if(!file) throw std::runtime_error("Cannot open performance capture");
    const auto position=file.tellg();if(position<0) throw std::runtime_error("Cannot measure performance capture length");
    const auto size=static_cast<std::uint64_t>(position);if(size<sizeof(Header) || size>sizeof(Data)) throw std::runtime_error("Performance capture exceeds its bounds");file.seekg(0);
    Header header;read(file,std::span<Header>(&header,1));const auto& h=header;
    if(h.magic!=Header{}.magic || h.frame_size!=sizeof(Frame) || h.sample_size!=sizeof(Sample) || h.resource_size!=sizeof(Resource) || h.cost_size!=sizeof(Cost) ||
       h.frames>frame_limit || h.samples>sample_limit || h.resources>resource_limit || h.costs!=cost_limit || h.cpu.back() || h.gpu.back() || h.build.back() || h.target_sha256.back() ||
       size!=sizeof(Header)+std::uint64_t(h.frames)*sizeof(Frame)+std::uint64_t(h.samples)*sizeof(Sample)+std::uint64_t(h.resources)*sizeof(Resource)+std::uint64_t(h.costs)*sizeof(Cost))
        throw std::runtime_error("Unsupported performance capture layout");
    auto data=std::make_unique<Data>();data->header=header;
    read(file,std::span(data->frames).first(h.frames));read(file,std::span(data->samples).first(h.samples));read(file,std::span(data->resources).first(h.resources));read(file,std::span(data->costs));
    for(const auto& cost:data->costs) if(cost.used && cost.phase>=phase_names.size()) throw std::runtime_error("Invalid performance phase");
    std::uint64_t previous=0;
    for(const auto& frame:std::span(data->frames).first(h.frames)) {
        if(frame.time<previous || frame.time>h.duration || frame.interval!=frame.time-previous || frame.gpu_status>2 || frame.spans>64) throw std::runtime_error("Invalid performance frame timeline");
        std::uint64_t remaining=frame.interval;for(auto ns:frame.phases) {if(ns>remaining) throw std::runtime_error("Overlapping performance phase times");remaining-=ns;}
        if(remaining) throw std::runtime_error("Incomplete performance phase times");previous=frame.time;
    }
    if(std::filesystem::exists(output)) throw std::runtime_error("Use a fresh performance report directory");
    reports(*data,output);return summary(*data);
}
std::string check_performance(const std::filesystem::path& output) {
    if(std::filesystem::exists(output)) throw std::runtime_error("Use a fresh performance check directory");
    Performance profile;PerfCounters counters{};profile.start("offline diagnostic",60,1,false,false,counters);
    {PerfScope outer(&profile,PerfPhase::kernel,7);{PerfScope nested(&profile,PerfPhase::render,3);Sleep(2);}Sleep(2);}
    profile.resources(1024,4096,0,true,2,1,256,768);profile.guest_sample(0x1000,4);profile.source_frame(1,counters);
    ++counters[unsigned(PerfMetric::draws)];Sleep(2);profile.source_frame(2,counters);profile.gpu_frame(2,{1000000,0,0},1,0,true);profile.stop();profile.save(output/"capture");
    const auto& data=profile.state_->data;
    if(data.header.frames!=2 || data.frames[1].counters[0]!=1 || !data.frames[0].phases[unsigned(PerfPhase::kernel)] || !data.frames[0].phases[unsigned(PerfPhase::render)] || data.frames[1].gpu_status!=1)
        throw std::runtime_error("Performance scopes, counters or GPU association failed");
    inspect_performance(output/"capture/performance.bin",output/"inspected");
    std::filesystem::copy_file(output/"capture/performance.bin",output/"invalid.bin");
    {std::ofstream file(output/"invalid.bin",std::ios::binary|std::ios::app);file.put(0);}
    bool rejected=false;try {inspect_performance(output/"invalid.bin",output/"invalid-report");} catch(const std::runtime_error&) {rejected=true;}
    if(!rejected) throw std::runtime_error("Malformed performance capture was accepted");
    return "{\"format\":\"b2-performance-check-v1\",\"passed\":true,\"game_booted\":false}";
}
}
