#pragma once
#include "render.h"
#include <functional>

namespace b2 {
class Memory;
struct GpuStats {
    std::uint64_t submissions=0,packets=0,methods=0,draws=0,vertices=0,clears=0,flips=0,fences=0;
    std::uint64_t fence_queries=0,fence_queue_waits=0;
};
// The original SDK produces NV2A packets. This bridge shares the native renderer
// with offline diagnostics; it does not decode or execute CPU instructions.
class Gpu {
public:
    Gpu(Renderer&,Memory&,std::uint32_t semaphore_base);
    ~Gpu();
    void submit(std::uint32_t begin,std::uint32_t end,bool flush=true);
    void begin_overlay();
    void restore_feedback();
    void coverage(std::uint64_t); // Applies to subsequently submitted packets.
    bool busy();
    void wait();
    void wait_fence(std::uint32_t value,std::uint32_t issued);
    void snapshot();
    void invalidate_memory(std::uint32_t address,std::uint32_t size); // Guest storage is being replaced or released.
    void capture_frame(const std::filesystem::path& directory,bool automated=false);
    void performance(Performance*);
    GpuStats stats() const;
    std::function<void()> flip;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
