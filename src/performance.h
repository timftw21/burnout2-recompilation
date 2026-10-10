#pragma once
#include <array>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <string_view>

namespace b2 {
enum class PerfPhase : unsigned {
    guest, kernel, platform, gpu_commands, render, texture_upload, resources,
    buffer_upload, gpu_wait, publish, display, present, ui, pacing, scheduler, diagnostics, capture, count
};
enum class PerfMetric : unsigned {
    draws, vertices, buffer_maps, buffer_bytes, texture_uploads, texture_bytes,
    target_creations, target_copies, state_misses, layout_misses, sampler_misses, program_creations, shader_compiles,
    submissions, packets, methods, fences, displayed, generated, audio_batches,
    audio_mix_ns, audio_overruns, count
};
enum class PerfGpuWait : std::uint32_t { poll=1, queue_space, semaphore, idle };
using PerfCounters=std::array<std::uint64_t,unsigned(PerfMetric::count)>;
std::uint64_t performance_clock() noexcept;
class PerfScope;
class Performance {
public:
    Performance();
    ~Performance();
    void start(std::string_view gpu,unsigned refresh,unsigned scale,bool interpolation,bool vsync,const PerfCounters&);
    void stop();
    void reset();
    bool active() const {return active_;}
    bool expired() const;
    bool resources_due() const;
    void resources(std::uint64_t local,std::uint64_t budget,std::uint64_t shared,bool gpu_valid,
                   std::uint64_t textures,std::uint64_t targets,std::uint64_t texture_bytes,std::uint64_t target_bytes);
    void guest_sample(std::uint32_t address,std::uint32_t thread);
    void source_frame(std::uint64_t id,const PerfCounters&);
    void gpu_frame(std::uint64_t id,const std::array<std::uint64_t,3>& ns,unsigned spans,unsigned dropped,bool valid);
    void save(const std::filesystem::path&) const;
    bool has_gpu_samples() const;
private:
    friend class PerfScope;
    friend std::string check_performance(const std::filesystem::path&);
    struct State;
    std::unique_ptr<State> state_;
    bool active_=false;
    std::uint64_t generation_=0;
    void account(std::uint64_t);
};
// Main-thread wall time is exclusive across nested scopes. Fiber switches must
// enter a scheduler scope; audio uses independent atomic counters.
class PerfScope {
public:
    PerfScope(Performance*,PerfPhase,std::uint32_t id=0);
    ~PerfScope();
    PerfScope(const PerfScope&)=delete;
    PerfScope& operator=(const PerfScope&)=delete;
private:
    Performance* profile_=nullptr;
    PerfPhase previous_=PerfPhase::guest;
    std::uint32_t previous_id_=0;
    std::uint64_t generation_=0;
};
std::string inspect_performance(const std::filesystem::path& capture,const std::filesystem::path& output);
std::string check_performance(const std::filesystem::path& output);
}
