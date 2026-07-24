#include "presenter_metrics.h"

#include <chrono>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace b2r::host {
namespace {

std::tm local_time(std::time_t value) {
    std::tm result{};
#ifdef _WIN32
    localtime_s(&result, &value);
#else
    localtime_r(&value, &result);
#endif
    return result;
}

}  // namespace

std::filesystem::path PresenterMetricsReporter::write(
    const std::filesystem::path& directory,
    const PresenterMetricsSnapshot& snapshot) {
    const auto now = std::chrono::system_clock::now();
    const auto now_time = std::chrono::system_clock::to_time_t(now);
    const std::tm local = local_time(now_time);
    const auto milliseconds = std::chrono::duration_cast<std::chrono::milliseconds>(
        now.time_since_epoch()).count() % 1000;
    ++report_count_;
    std::ostringstream name;
    name << "b2-recomp-metrics-" << std::setfill('0')
         << std::setw(4) << local.tm_year + 1900
         << std::setw(2) << local.tm_mon + 1
         << std::setw(2) << local.tm_mday << '-'
         << std::setw(2) << local.tm_hour
         << std::setw(2) << local.tm_min
         << std::setw(2) << local.tm_sec << '-'
         << std::setw(3) << milliseconds
         << "-frame-" << snapshot.frame
         << '-' << report_count_ << ".txt";
    const std::filesystem::path output_path = directory / name.str();
    std::filesystem::create_directories(output_path.parent_path());
    std::ofstream output(output_path, std::ios::binary | std::ios::trunc);
    if (!output) {
        throw std::runtime_error(
            "unable to open metrics report: " + output_path.string());
    }

    constexpr double bytes_per_mb = 1024.0 * 1024.0;
    output << "B2 Recomp metrics snapshot\n"
           << "frame: " << snapshot.frame << '\n'
           << "scope: counters are cumulative since presenter startup; "
              "guest FPS is the latest complete one-second completed-flip "
              "sample; presenter FPS and timing values are from the latest "
              "completed host frame\n"
           << std::fixed << std::setprecision(3);
    output << "guest FPS: ";
    if (snapshot.guest_fps_valid) {
        output << snapshot.guest_fps;
    } else {
        output << "n/a";
    }
    output << '\n' << "guest FPS sample flips: ";
    if (snapshot.guest_fps_valid) {
        output << snapshot.guest_fps_sample_flips;
    } else {
        output << "n/a";
    }
    output << '\n' << "guest FPS sample seconds: ";
    if (snapshot.guest_fps_valid) {
        output << snapshot.guest_fps_sample_seconds;
    } else {
        output << "n/a";
    }
    output << '\n'
           << "guest completed flips: " << snapshot.guest_completed_flips << '\n'
           << "presenter FPS: ";
    if (snapshot.presenter_frame_us > 0) {
        output << 1'000'000.0 / static_cast<double>(snapshot.presenter_frame_us);
    } else {
        output << "n/a";
    }
    output << '\n'
           << "guest instructions: " << snapshot.guest_instructions << '\n'
           << "compiled blocks / invalidations: " << snapshot.compiled_blocks
           << " / " << snapshot.invalidations << '\n'
           << "push-buffer commands: " << snapshot.push_buffer_commands << '\n'
           << "draws: " << snapshot.draws << '\n'
           << "triangles: " << snapshot.triangles << '\n'
           << "pipeline creations: " << snapshot.pipeline_creations << '\n'
           << "pipeline cache misses: " << snapshot.pipeline_cache_misses << '\n'
           << "descriptor allocations: " << snapshot.descriptor_allocations << '\n'
           << "command buffers: " << snapshot.command_buffer_allocations << '\n'
           << "queue submissions: " << snapshot.queue_submissions << '\n'
           << "barriers: " << snapshot.barriers << '\n'
           << "uploads MB: " << static_cast<double>(snapshot.upload_bytes) / bytes_per_mb << '\n'
           << "readbacks MB: " << static_cast<double>(snapshot.readback_bytes) / bytes_per_mb << '\n'
           << "CPU render ms: " << static_cast<double>(snapshot.cpu_render_us) / 1000.0 << '\n'
           << "window message pump ms: "
           << static_cast<double>(snapshot.platform_poll_us) / 1000.0 << '\n'
           << "controller poll ms: "
           << static_cast<double>(snapshot.controller_poll_us) / 1000.0 << '\n'
           << "keyboard latch ms: "
           << static_cast<double>(snapshot.keyboard_latch_us) / 1000.0 << '\n'
           << "reload probe ms: "
           << static_cast<double>(snapshot.reload_probe_us) / 1000.0 << '\n'
           << "pre-render unattributed ms: "
           << static_cast<double>(snapshot.pre_render_unattributed_us) / 1000.0 << '\n'
           << "GPU frame ms: ";
    if (snapshot.gpu_frame_time_valid) {
        output << snapshot.gpu_frame_ms;
    } else {
        output << "n/a";
    }
    output << '\n'
           << "fence-wait ms: " << static_cast<double>(snapshot.fence_wait_us) / 1000.0 << '\n'
           << "draw fence-wait ms: "
           << static_cast<double>(snapshot.draw_fence_wait_us) / 1000.0 << '\n'
           << "image acquire ms: "
           << static_cast<double>(snapshot.image_acquire_us) / 1000.0 << '\n'
           << "queue submit ms: "
           << static_cast<double>(snapshot.queue_submit_us) / 1000.0 << '\n'
           << "readback wait ms: "
           << static_cast<double>(snapshot.readback_wait_us) / 1000.0 << '\n'
           << "queue present ms: "
           << static_cast<double>(snapshot.queue_present_us) / 1000.0 << '\n'
           << "draw unattributed ms: "
           << static_cast<double>(snapshot.draw_unattributed_us) / 1000.0 << '\n';
    if (!output) {
        throw std::runtime_error(
            "failed while writing metrics report: " + output_path.string());
    }
    return output_path;
}

}  // namespace b2r::host
