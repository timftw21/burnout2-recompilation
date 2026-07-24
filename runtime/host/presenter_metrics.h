#pragma once

#include <cstdint>
#include <filesystem>

namespace b2r::host {

struct PresenterMetricsSnapshot {
    uint32_t frame = 0u;
    bool guest_fps_valid = false;
    double guest_fps = 0.0;
    uint64_t guest_fps_sample_flips = 0u;
    double guest_fps_sample_seconds = 0.0;
    uint64_t guest_completed_flips = 0u;
    int64_t presenter_frame_us = 0;
    uint64_t guest_instructions = 0u;
    uint64_t compiled_blocks = 0u;
    uint64_t invalidations = 0u;
    uint64_t push_buffer_commands = 0u;
    uint64_t draws = 0u;
    uint64_t triangles = 0u;
    uint64_t pipeline_creations = 0u;
    uint64_t pipeline_cache_misses = 0u;
    uint64_t descriptor_allocations = 0u;
    uint64_t command_buffer_allocations = 0u;
    uint64_t queue_submissions = 0u;
    uint64_t barriers = 0u;
    uint64_t upload_bytes = 0u;
    uint64_t readback_bytes = 0u;
    uint64_t cpu_render_us = 0u;
    uint64_t platform_poll_us = 0u;
    uint64_t controller_poll_us = 0u;
    uint64_t keyboard_latch_us = 0u;
    uint64_t reload_probe_us = 0u;
    uint64_t pre_render_unattributed_us = 0u;
    bool gpu_frame_time_valid = false;
    double gpu_frame_ms = 0.0;
    uint64_t fence_wait_us = 0u;
    uint64_t draw_fence_wait_us = 0u;
    uint64_t image_acquire_us = 0u;
    uint64_t queue_submit_us = 0u;
    uint64_t readback_wait_us = 0u;
    uint64_t queue_present_us = 0u;
    uint64_t draw_unattributed_us = 0u;
};

class PresenterMetricsReporter {
public:
    std::filesystem::path write(
        const std::filesystem::path& directory,
        const PresenterMetricsSnapshot& snapshot);

private:
    uint32_t report_count_ = 0u;
};

}  // namespace b2r::host
