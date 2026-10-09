#pragma once
#include "cpu.h"
#include "xbe.h"
#include "gpu.h"
#include <memory>
#include <string>

namespace b2 {
class Input;
struct XboxHost {
    Renderer* renderer=nullptr;
    Input* input=nullptr;
    std::function<bool()> poll;
    std::function<void()> flip;
    bool trace_io=false;
    std::filesystem::path storage_root;
};
struct BootIo {
    std::uint32_t ordinal=0,status=0,information=0,callsite=0;
    std::string path;
    std::uint32_t options=0,requested=0;
    std::uint64_t offset=0;
    std::uint32_t buffer=0;
};
struct BootAllocation {
    std::uint32_t address=0,size=0,committed=0,protect=0;
};
struct BootThread {
    std::uint32_t id=0,routine=0,system=0,eip=0,stack=0,wait_object=0;
    std::string state;
    bool suspended=false;
};
struct BootResult {
    bool entry_returned=false,main_reached=false;
    bool graphics_device_created=false,graphics_initialized=false;
    bool audio_device_created=false;
    std::uint32_t audio_buffers=0;
    std::uint32_t audio_effects=0;
    std::string audio_error;
    std::string graphics_adapter;
    GpuStats graphics;
    std::vector<BootIo> io;
    std::vector<BootAllocation> allocations;
    std::vector<BootThread> threads;
    std::array<std::uint32_t,5> last_failed_allocation{};
    std::uint32_t active_thread=0,threads_created=0;
    std::uint64_t vblank_callbacks=0;
    std::string boundary;
    Cpu cpu;
    ExecutionDiagnostic diagnostic;
    std::array<std::uint32_t,16> stack_words{};
    std::uint32_t stack_word_count=0;
    std::array<std::uint64_t,512> service_calls{};
};
class Xbox {
public:
    Xbox(Xbe&,Memory&,std::span<std::byte> ram,const std::filesystem::path& disc = {},XboxHost host = {});
    ~Xbox();
    Xbox(const Xbox&)=delete;
    Xbox& operator=(const Xbox&)=delete;
    BootResult run(std::uint32_t visit_budget,std::uint32_t break_address=0,std::uint32_t break_hit=1);
    bool save_frame(const std::filesystem::path&);
    void output_gain(float);
    void capture_frame(const std::filesystem::path& directory);
private:
    friend void native_platform(Cpu&,Memory&,std::uint32_t);
    friend std::string check_storage(const std::filesystem::path&,const std::filesystem::path&);
    friend std::string check_replays(const std::filesystem::path&,const std::filesystem::path&,const std::filesystem::path&);
    friend std::string check_audio_listener(const std::filesystem::path&,const std::filesystem::path&);
    struct State;
    std::unique_ptr<State> state_;
};
std::string check_storage(const std::filesystem::path& xbe,const std::filesystem::path& output);
std::string check_replays(const std::filesystem::path& xbe,const std::filesystem::path& disc,const std::filesystem::path& output);
std::string check_audio_listener(const std::filesystem::path& xbe,const std::filesystem::path& output);
}
