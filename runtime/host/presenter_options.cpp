#define NOMINMAX
#define WIN32_LEAN_AND_MEAN

#include "presenter_options.h"

#include <windows.h>
#include <shellapi.h>

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>

namespace b2r::host {
namespace {

std::string narrow(const std::wstring& value) {
    if (value.empty()) {
        return {};
    }
    const int size = WideCharToMultiByte(
        CP_UTF8,
        0,
        value.data(),
        static_cast<int>(value.size()),
        nullptr,
        0,
        nullptr,
        nullptr);
    if (size <= 0) {
        return {};
    }
    std::string result(static_cast<size_t>(size), '\0');
    WideCharToMultiByte(
        CP_UTF8,
        0,
        value.data(),
        static_cast<int>(value.size()),
        result.data(),
        size,
        nullptr,
        nullptr);
    return result;
}

uint32_t parse_u32(const std::wstring& value, const wchar_t* label) {
    wchar_t* end = nullptr;
    const unsigned long parsed = wcstoul(value.c_str(), &end, 10);
    if (!end || *end != L'\0' || parsed > UINT32_MAX) {
        throw std::runtime_error("invalid integer for " + narrow(label));
    }
    return static_cast<uint32_t>(parsed);
}

[[noreturn]] void print_help_and_exit() {
    std::wcout
        << L"Usage: b2_first_frame.exe [--width N] [--height N] [--max-frames N]\n"
        << L"  --max-frames 0 keeps the window open until it is closed or Escape is pressed.\n"
        << L"                          [--presented-draw-begin N] [--presented-draw-end N]\n"
        << L"                          [--presentation-pipeline-depth 1|2]\n"
        << L"                          [--debug-json PATH] [--render-stream-json PATH]\n"
        << L"                          [--live-render-stream-json PATH] [--controller-state-json PATH]\n"
        << L"                          [--live-control-transport NAME]\n"
        << L"                          [--strict-render-validation]\n"
        << L"                          [--analyze-render-stream] [--cpu-vertex-programs]\n"
        << L"                          [--cpu-vertex-attributes] [--cpu-texture-conversion]\n"
        << L"                          [--flip-audit-ack PATH] [--flip-audit-frame-directory PATH]\n"
        << L"                          [--flip-audit-max-flips N]\n"
        << L"                          [--flip-audit-health-interval N]\n"
        << L"                          [--screenshot PATH] [--hotkey-screenshot-directory PATH]\n"
        << L"                          [--window-icon PATH]\n"
        << L"                          [--metrics-report-directory PATH]\n"
        << L"                          [--command-work-cache-trace PATH]\n"
        << L"  Press F8 to trigger an armed replay-capsule capture.\n"
        << L"  Press F9 to toggle the completed-guest-frame FPS counter.\n"
        << L"  Press F10 to start or stop an armed native hot-path capture.\n"
        << L"  Press F11 to write a metrics snapshot in the metrics report directory.\n"
        << L"  Press F12 to save the current frame in the hotkey screenshot directory.\n"
        << L"                          [--vertex-shader PATH] [--fragment-shader PATH]\n"
        << L"                          [--texture-convert-shader PATH]\n"
        << L"                          [--pipeline-cache PATH]\n"
        << L"                          [--inject-input]\n"
        << L"                          [--list-adapters]\n";
    ExitProcess(0);
}

}  // namespace

std::vector<std::wstring> command_line_args() {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) {
        throw std::runtime_error("CommandLineToArgvW failed");
    }
    std::vector<std::wstring> result;
    result.reserve(static_cast<size_t>(argc));
    for (int index = 0; index < argc; ++index) {
        result.emplace_back(argv[index]);
    }
    LocalFree(argv);
    return result;
}

PresenterOptions parse_presenter_options(const std::vector<std::wstring>& args) {
    PresenterOptions options;
    for (size_t index = 1; index < args.size(); ++index) {
        const std::wstring& arg = args[index];
        auto require_value = [&](const wchar_t* name) -> const std::wstring& {
            if (index + 1 >= args.size()) {
                throw std::runtime_error("missing value for " + narrow(name));
            }
            return args[++index];
        };

        if (arg == L"--width") {
            options.width = parse_u32(require_value(L"--width"), L"--width");
        } else if (arg == L"--height") {
            options.height = parse_u32(require_value(L"--height"), L"--height");
        } else if (arg == L"--max-frames") {
            options.max_frames = parse_u32(require_value(L"--max-frames"), L"--max-frames");
        } else if (arg == L"--presented-draw-begin") {
            options.presented_draw_begin = parse_u32(
                require_value(L"--presented-draw-begin"), L"--presented-draw-begin");
        } else if (arg == L"--presented-draw-end") {
            options.presented_draw_end = parse_u32(
                require_value(L"--presented-draw-end"), L"--presented-draw-end");
        } else if (arg == L"--presentation-pipeline-depth") {
            options.presentation_pipeline_depth = parse_u32(
                require_value(L"--presentation-pipeline-depth"),
                L"--presentation-pipeline-depth");
        } else if (arg == L"--debug-json") {
            options.debug_json = std::filesystem::path(require_value(L"--debug-json"));
        } else if (arg == L"--render-stream-json") {
            options.render_stream_json = std::filesystem::path(
                require_value(L"--render-stream-json"));
        } else if (arg == L"--live-render-stream-json") {
            options.render_stream_json = std::filesystem::path(
                require_value(L"--live-render-stream-json"));
            options.live_render_stream = true;
        } else if (arg == L"--controller-state-json") {
            options.controller_state_json = std::filesystem::path(
                require_value(L"--controller-state-json"));
        } else if (arg == L"--live-control-transport") {
            options.live_control_transport_name = require_value(L"--live-control-transport");
        } else if (arg == L"--strict-render-validation") {
            options.strict_render_validation = true;
        } else if (arg == L"--analyze-render-stream") {
            options.analyze_render_stream_only = true;
        } else if (arg == L"--cpu-vertex-programs") {
            options.cpu_vertex_programs = true;
        } else if (arg == L"--cpu-vertex-attributes") {
            options.cpu_vertex_attributes = true;
        } else if (arg == L"--cpu-texture-conversion") {
            options.cpu_texture_conversion = true;
        } else if (arg == L"--flip-audit-ack") {
            options.flip_audit_ack = std::filesystem::path(
                require_value(L"--flip-audit-ack"));
        } else if (arg == L"--flip-audit-frame-directory") {
            options.flip_audit_frame_directory = std::filesystem::path(
                require_value(L"--flip-audit-frame-directory"));
        } else if (arg == L"--flip-audit-max-flips") {
            options.flip_audit_max_flips = parse_u32(
                require_value(L"--flip-audit-max-flips"), L"--flip-audit-max-flips");
        } else if (arg == L"--flip-audit-health-interval") {
            options.flip_audit_health_interval = parse_u32(
                require_value(L"--flip-audit-health-interval"),
                L"--flip-audit-health-interval");
        } else if (arg == L"--screenshot") {
            options.screenshot = std::filesystem::path(require_value(L"--screenshot"));
        } else if (arg == L"--hotkey-screenshot-directory") {
            options.hotkey_screenshot_directory = std::filesystem::path(
                require_value(L"--hotkey-screenshot-directory"));
        } else if (arg == L"--window-icon") {
            options.window_icon = std::filesystem::path(
                require_value(L"--window-icon"));
        } else if (arg == L"--metrics-report-directory") {
            options.metrics_report_directory = std::filesystem::path(
                require_value(L"--metrics-report-directory"));
        } else if (arg == L"--command-work-cache-trace") {
            options.command_work_cache_trace = std::filesystem::path(
                require_value(L"--command-work-cache-trace"));
        } else if (arg == L"--vertex-shader") {
            options.vertex_shader = std::filesystem::path(require_value(L"--vertex-shader"));
        } else if (arg == L"--fragment-shader") {
            options.fragment_shader = std::filesystem::path(
                require_value(L"--fragment-shader"));
        } else if (arg == L"--texture-convert-shader") {
            options.texture_convert_shader = std::filesystem::path(
                require_value(L"--texture-convert-shader"));
        } else if (arg == L"--pipeline-cache") {
            options.pipeline_cache = std::filesystem::path(
                require_value(L"--pipeline-cache"));
        } else if (arg == L"--title") {
            options.title = require_value(L"--title");
        } else if (arg == L"--inject-input") {
            options.inject_input = true;
        } else if (arg == L"--list-adapters") {
            options.list_adapters_only = true;
        } else if (arg == L"--help" || arg == L"-h") {
            print_help_and_exit();
        } else {
            throw std::runtime_error("unknown argument: " + narrow(arg));
        }
    }

    if (!options.analyze_render_stream_only
        && (options.vertex_shader.empty() || options.fragment_shader.empty())) {
        throw std::runtime_error("--vertex-shader and --fragment-shader are required");
    }
    if (!options.analyze_render_stream_only
        && !options.cpu_texture_conversion
        && options.texture_convert_shader.empty()) {
        throw std::runtime_error(
            "--texture-convert-shader is required for GPU texture conversion");
    }
    if (options.presented_draw_begin > options.presented_draw_end) {
        throw std::runtime_error(
            "--presented-draw-begin must not exceed --presented-draw-end");
    }
    if (options.presentation_pipeline_depth < 1u
        || options.presentation_pipeline_depth > 2u) {
        throw std::runtime_error("--presentation-pipeline-depth must be 1 or 2");
    }
    if (options.presentation_pipeline_depth > 1u && !options.live_render_stream) {
        throw std::runtime_error(
            "--presentation-pipeline-depth 2 requires a live render stream");
    }
    if (options.flip_audit_ack.empty() != options.flip_audit_frame_directory.empty()) {
        throw std::runtime_error(
            "--flip-audit-ack and --flip-audit-frame-directory must be used together");
    }
    if (!options.flip_audit_ack.empty() && !options.live_render_stream) {
        throw std::runtime_error(
            "lossless flip audit requires --live-render-stream-json");
    }
    if (options.flip_audit_max_flips != 0u && options.flip_audit_ack.empty()) {
        throw std::runtime_error(
            "--flip-audit-max-flips requires lossless flip audit paths");
    }
    if (options.flip_audit_health_interval != 30u && options.flip_audit_ack.empty()) {
        throw std::runtime_error(
            "--flip-audit-health-interval requires lossless flip audit paths");
    }
    return options;
}

}  // namespace b2r::host
