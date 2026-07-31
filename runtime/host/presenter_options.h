#pragma once

#include <cstdint>
#include <filesystem>
#include <limits>
#include <string>
#include <vector>

namespace b2r::host {

struct PresenterOptions {
    uint32_t width = 640u;
    uint32_t height = 480u;
    uint32_t max_frames = 120u;
    uint32_t presented_draw_begin = 0u;
    uint32_t presented_draw_end = std::numeric_limits<uint32_t>::max();
    uint32_t presentation_pipeline_depth = 1u;
    uint32_t flip_audit_max_flips = 0u;
    uint32_t flip_audit_health_interval = 30u;
    bool inject_input = false;
    bool list_adapters_only = false;
    std::wstring title = L"B2 Recomp";
    std::filesystem::path debug_json;
    std::filesystem::path render_stream_json;
    std::filesystem::path screenshot;
    std::filesystem::path hotkey_screenshot_directory;
    std::filesystem::path metrics_report_directory;
    std::filesystem::path command_work_cache_trace;
    std::filesystem::path flip_audit_ack;
    std::filesystem::path flip_audit_frame_directory;
    std::filesystem::path vertex_shader;
    std::filesystem::path fragment_shader;
    std::filesystem::path texture_convert_shader;
    std::filesystem::path pipeline_cache;
    bool live_render_stream = false;
    bool strict_render_validation = false;
    bool analyze_render_stream_only = false;
    bool cpu_vertex_programs = false;
    bool cpu_vertex_attributes = false;
    bool cpu_texture_conversion = false;
    std::filesystem::path controller_state_json;
    std::wstring live_control_transport_name;
};

std::vector<std::wstring> command_line_args();
PresenterOptions parse_presenter_options(const std::vector<std::wstring>& args);

}  // namespace b2r::host
