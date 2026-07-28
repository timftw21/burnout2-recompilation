#include "vulkan_presenter_runtime.h"
#include "live_transport_layout.h"

namespace b2r::host::vulkan_detail {

VulkanPresenter::VulkanPresenter(Options options) : options_(std::move(options)) {}

VulkanPresenter::~VulkanPresenter() {
    try {
        cleanup();
    } catch (...) {
        // Destructors must not mask the original presenter failure.
    }
}

int VulkanPresenter::run() {
    SetThreadDescription(GetCurrentThread(), L"b2-presenter-vulkan");
    log_.configure_live_mode(
        options_.live_render_stream && !options_.analyze_render_stream_only);
    log_.open(options_.debug_json);
    log_.emit(
        "startup",
        {
            {"target_platform", json_string("windows")},
            {"platform_backend", json_string("sdl3")},
            {"renderer_backend", json_string("vulkan")},
            {"width", std::to_string(options_.width)},
            {"height", std::to_string(options_.height)},
            {"max_frames", std::to_string(options_.max_frames)},
            {"vertex_program_backend", json_string(
                options_.cpu_vertex_programs ? "cpu" : "gpu")},
            {"vertex_attribute_backend", json_string(
                options_.cpu_vertex_attributes ? "cpu" : "gpu")},
            {"texture_conversion_backend", json_string(
                options_.cpu_texture_conversion ? "cpu" : "gpu")},
            {"presentation_pipeline_depth", std::to_string(
                options_.presentation_pipeline_depth)},
            {"guest_frame_rate_limit_hz", std::to_string(
                kTargetGuestFrameRateHz)},
        });

    if (options_.analyze_render_stream_only) {
        if (!options_.live_control_transport_name.empty()) {
            initialize_live_control_transport();
        }
        swapchain_extent_ = {options_.width, options_.height};
        if (!load_initial_recovered_render_work()) {
            throw std::runtime_error(
                "render manifest was incomplete during headless analysis");
        }
        const GpuRawAttributeValidation raw_attribute_validation =
            validate_gpu_raw_attribute_fetch(
                interpreted_stream_, recovered_source_.textures);
        log_.emit(
            "nv2a_gpu_raw_attribute_validation",
            {
                {"eligible_draws", std::to_string(
                    raw_attribute_validation.eligible_draw_count)},
                {"eligible_vertices", std::to_string(
                    raw_attribute_validation.eligible_vertex_count)},
                {"decoded_attributes", std::to_string(
                    raw_attribute_validation.decoded_attribute_count)},
                {"compared_vertices", std::to_string(
                    raw_attribute_validation.compared_vertex_count)},
                {"mismatch_vertices", std::to_string(
                    raw_attribute_validation.mismatch_vertex_count)},
                {"mapping_fallback_draws", std::to_string(
                    raw_attribute_validation.mapping_fallback_draw_count)},
                {"packed_resource_bytes", std::to_string(
                    raw_attribute_validation.packed_resource_bytes)},
                {"passed", json_bool(
                    raw_attribute_validation.mismatch_vertex_count == 0u)},
            });
        const GpuRawDirtyRangeValidation dirty_range_validation =
            validate_gpu_raw_dirty_range_uploads();
        log_.emit(
            "nv2a_gpu_raw_dirty_range_validation",
            {
                {"unchanged_generation_passed", json_bool(
                    dirty_range_validation.unchanged_generation_passed)},
                {"changed_generation_passed", json_bool(
                    dirty_range_validation.changed_generation_passed)},
                {"layout_change_passed", json_bool(
                    dirty_range_validation.layout_change_passed)},
                {"changed_ranges", std::to_string(
                    dirty_range_validation.changed_range_count)},
                {"changed_upload_bytes", std::to_string(
                    dirty_range_validation.changed_upload_bytes)},
                {"packed_resource_bytes", std::to_string(
                    dirty_range_validation.packed_resource_bytes)},
                {"passed", json_bool(dirty_range_validation.passed())},
            });
        const std::vector<NativeVertex> vertices =
            prepare_presented_vertices();
        emit_presented_vertex_transform_diagnostics(true);
        emit_presented_render_state_diagnostics();
        log_.emit(
            "render_stream_analysis_complete",
            {
                {"source_commands", std::to_string(
                    options_.live_render_stream
                        ? interpreted_source_command_count_
                        : recovered_source_.commands.size())},
                {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
                {"launch_transform_program_count", std::to_string(interpreted_stream_.launch_transform_program_count)},
                {"failed_launch_transform_program_count", std::to_string(interpreted_stream_.failed_launch_transform_program_count)},
                {"presented_draws", std::to_string(interpreted_stream_.presented_draw_count)},
                {"zero_count_method_words", std::to_string(interpreted_stream_.zero_count_method_word_count)},
                {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream_.zero_count_indexed_array_noop_packet_count)},
                {"ordered_push_buffer_appends", std::to_string(
                    interpreted_stream_.ordered_push_buffer_append_count)},
                {"indexed_word_push_buffer_appends", std::to_string(
                    interpreted_stream_.indexed_word_push_buffer_append_count)},
                {"reconstructed_push_buffer_appends", std::to_string(
                    interpreted_stream_.reconstructed_push_buffer_append_count)},
                {"surface_payload_scans_skipped", std::to_string(
                    interpreted_stream_.surface_payload_scan_skipped_count)},
                {"feedback_spec_cache_builds", std::to_string(
                    feedback_spec_cache_build_count_)},
                {"feedback_spec_cache_hits", std::to_string(
                    feedback_spec_cache_hit_count_)},
                {"uploaded_vertices", std::to_string(vertices.size())},
                {"presented_surface_color_offset", std::to_string(presented_surface_color_offset_)},
                {"continuation_bootstrapped", json_bool(continuation_analysis_bootstrap_)},
                {"state_history_complete", json_bool(!continuation_analysis_bootstrap_)},
            });
        log_.emit("shutdown", {{"status", json_string("analysis_ok")}});
        cleanup();
        return 0;
    }

    initialize_platform();
    create_instance();
    if (options_.list_adapters_only) {
        list_adapters();
        cleanup();
        return 0;
    }
    create_window();
    initialize_live_control_transport();
    create_surface();
    pick_physical_device();
    create_logical_device();
    create_pipeline_cache();
    create_swapchain();
    create_image_views();
    create_depth_resources();
    create_render_pass();
    create_framebuffers();
    create_command_pool();
    create_gpu_timing_resources();
    if (!load_initial_recovered_render_work()) {
        throw std::runtime_error("live render manifest was incomplete at startup");
    }
    create_native_graphics_pipeline();
    create_native_render_resources();
    create_readback_buffer();
    create_command_buffers();
    if (publication_event_) {
        ResetEvent(publication_event_);
    }
    acknowledge_current_presentation();
    create_sync_objects();
    main_loop();
    vkDeviceWaitIdle(device_);
    cleanup();
    log_.emit("shutdown", {{"status", json_string("ok")}});
    return 0;
}

std::optional<std::string> VulkanPresenter::read_live_render_manifest() {
    if (transport_.active() && options_.flip_audit_ack.empty()) {
        return transport_.read_manifest();
    }
    return transport_.read_manifest_file(
        options_.render_stream_json,
        !options_.flip_audit_ack.empty());
}

bool VulkanPresenter::load_initial_recovered_render_work() {
    const auto deadline = std::chrono::steady_clock::now()
        + std::chrono::seconds(2);
    uint32_t retry_count = 0;
    while (!load_recovered_render_work()) {
        if (!options_.live_render_stream
            || std::chrono::steady_clock::now() >= deadline) {
            return false;
        }
        ++retry_count;
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    if (retry_count != 0) {
        log_.emit(
            "live_render_startup_manifest_retried",
            {{"retries", std::to_string(retry_count)}});
    }
    return true;
}

bool VulkanPresenter::update_flip_audit_manifest_state(const std::string& manifest_text) {
    if (options_.flip_audit_ack.empty() || manifest_text.empty()) {
        return options_.flip_audit_ack.empty();
    }
    const bool enabled = manifest_text.find(
        "\"lossless_flip_audit\":true") != std::string::npos;
    const auto flip = json_object_field_text(manifest_text, "audit_flip_index");
    const auto guest_steps = json_object_field_text(
        manifest_text, "audit_guest_steps");
    const auto flip_value = json_object_field_text(
        manifest_text, "audit_flip_value");
    const auto command_count = json_object_field_text(
        manifest_text, "audit_command_record_count");
    const auto health_reason = json_object_field_text(
        manifest_text, "audit_health_reason");
    const auto generation = json_object_field_text(
        manifest_text, "command_stream_generation");
    if (!enabled || !flip.has_value()
        || !guest_steps.has_value() || !flip_value.has_value()
        || !command_count.has_value()
        || !generation.has_value()) {
        return false;
    }
    current_audit_flip_ = parse_json_u32_text(*flip, "audit flip index");
    current_audit_guest_steps_ = parse_json_u32_text(
        *guest_steps, "audit guest steps");
    current_audit_flip_value_ = parse_json_u32_text(
        *flip_value, "audit flip value");
    current_audit_command_count_ = parse_json_u32_text(
        *command_count, "audit command count");
    current_audit_generation_ = *generation;
    current_audit_health_reason_ = health_reason.value_or(std::string{});
    return true;
}

std::string VulkanPresenter::flip_audit_health_check_reason() const {
    if (options_.flip_audit_ack.empty() || current_audit_flip_ == 0u) {
        return {};
    }
    if (!current_audit_health_reason_.empty()) {
        return current_audit_health_reason_;
    }
    if (acknowledged_audit_flip_ == 0u) {
        return "first_flip";
    }
    if (live_resource_generation_ != last_audit_resource_generation_) {
        return "resource_changed";
    }
    const uint32_t command_delta = current_audit_command_count_
        - std::min(current_audit_command_count_, last_audit_command_count_);
    if (command_delta != last_audit_command_delta_) {
        return "command_shape_changed";
    }
    if (current_audit_flip_value_ != last_audit_flip_value_) {
        return "flip_value_changed";
    }
    if (options_.flip_audit_health_interval != 0u
        && current_audit_flip_ % options_.flip_audit_health_interval == 0u) {
        return "periodic_sample";
    }
    return {};
}

bool VulkanPresenter::read_live_command_stream_delta(
    const std::filesystem::path& path,
    uint64_t snapshot_base_record_count,
    uint64_t first_record_count,
    uint64_t required_record_count) {
    last_command_file_open_us_ = 0;
    last_command_file_read_us_ = 0;
    last_command_record_validation_us_ = 0;
    last_command_read_bytes_ = 0u;
    last_live_command_mmio_count_ = 0u;
    last_command_file_reused_ = path == live_command_file_path_
        && live_command_file_.is_open();
    if (first_record_count < snapshot_base_record_count
        || required_record_count < first_record_count) {
        throw std::runtime_error(
            "live command cursor is outside the published snapshot");
    }
    if (!last_command_file_reused_) {
        const auto open_begin = std::chrono::steady_clock::now();
        live_command_file_.close();
        live_command_file_.clear();
        live_command_file_.open(path, std::ios::binary);
        if (!live_command_file_) {
            return false;
        }
        std::array<char, 8> magic{};
        live_command_file_.read(
            magic.data(), static_cast<std::streamsize>(magic.size()));
        if (std::string(magic.data(), magic.size()) != "B2APPND1") {
            live_command_file_.close();
            return false;
        }
        live_command_file_path_ = path;
        last_command_file_open_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - open_begin).count();
    }
    constexpr uint64_t header_size = 8u;
    constexpr uint64_t record_size = kRecoveredD3DCommandRecordSize;
    // The manifest is the validated publication boundary and is written
    // only after the producer flushes this many records. Avoid a separate
    // filesystem metadata query and do not parse commands from a newer,
    // in-progress guest frame merely because they already exist on disk.
    const uint64_t local_first_record =
        first_record_count - snapshot_base_record_count;
    const uint64_t appended_count_u64 =
        required_record_count - first_record_count;
    if (local_first_record
            > (static_cast<uint64_t>(
                std::numeric_limits<std::streamoff>::max()) - header_size)
                / record_size
        || appended_count_u64
            > std::numeric_limits<size_t>::max() / record_size) {
        throw std::runtime_error("live command delta is too large");
    }
    live_command_delta_bytes_.clear();
    last_native_command_read_count_ = 0u;
    if (appended_count_u64 == 0u) {
        return true;
    }
    const size_t appended_count = static_cast<size_t>(appended_count_u64);
    live_command_delta_bytes_.resize(appended_count * record_size);
    const auto read_begin = std::chrono::steady_clock::now();
    live_command_file_.clear();
    live_command_file_.seekg(
        static_cast<std::streamoff>(
            header_size + local_first_record * record_size),
        std::ios::beg);
    live_command_file_.read(
        reinterpret_cast<char*>(live_command_delta_bytes_.data()),
        static_cast<std::streamsize>(live_command_delta_bytes_.size()));
    last_command_file_read_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - read_begin).count();
    if (!live_command_file_) {
        live_command_delta_bytes_.clear();
        return false;
    }
    last_command_read_bytes_ = live_command_delta_bytes_.size();
    const auto validation_begin = std::chrono::steady_clock::now();
    for (size_t record_index = 0; record_index < appended_count; ++record_index) {
        const uint8_t* record = live_command_delta_bytes_.data()
            + record_index * record_size;
        const uint8_t kind_raw = record[0];
        const uint8_t size = record[1];
        if (size == 0 || size > 8u || kind_raw > 1u) {
            live_command_delta_bytes_.clear();
            return false;
        }
        last_live_command_mmio_count_ += static_cast<uint32_t>(kind_raw == 0u);
    }
    last_command_record_validation_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - validation_begin).count();
    last_native_command_read_count_ = appended_count;
    native_command_read_count_ += appended_count;
    return true;
}

bool VulkanPresenter::read_live_command_span_stream_delta(
    const std::filesystem::path& path,
    uint64_t snapshot_base_byte_count,
    uint64_t first_byte_count,
    uint64_t required_byte_count,
    uint64_t expected_logical_write_count) {
    last_command_file_open_us_ = 0;
    last_command_file_read_us_ = 0;
    last_command_record_validation_us_ = 0;
    last_command_read_bytes_ = 0u;
    last_live_command_mmio_count_ = 0u;
    last_native_command_span_count_ = 0u;
    last_command_file_reused_ = path == live_command_file_path_
        && live_command_file_.is_open();
    if (first_byte_count < snapshot_base_byte_count
        || required_byte_count < first_byte_count) {
        throw std::runtime_error(
            "live command span cursor is outside the published snapshot");
    }
    if (!last_command_file_reused_) {
        const auto open_begin = std::chrono::steady_clock::now();
        live_command_file_.close();
        live_command_file_.clear();
        live_command_file_.open(path, std::ios::binary);
        if (!live_command_file_) {
            return false;
        }
        std::array<char, 8> magic{};
        live_command_file_.read(
            magic.data(), static_cast<std::streamsize>(magic.size()));
        if (std::string(magic.data(), magic.size()) != "B2SPAN01") {
            live_command_file_.close();
            return false;
        }
        live_command_file_path_ = path;
        last_command_file_open_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - open_begin).count();
    }
    constexpr uint64_t header_size = 8u;
    const uint64_t local_first_byte =
        first_byte_count - snapshot_base_byte_count;
    const uint64_t appended_byte_count =
        required_byte_count - first_byte_count;
    if (local_first_byte
            > static_cast<uint64_t>(
                std::numeric_limits<std::streamoff>::max()) - header_size
        || appended_byte_count > std::numeric_limits<size_t>::max()) {
        throw std::runtime_error("live command span delta is too large");
    }
    live_command_delta_bytes_.clear();
    last_native_command_read_count_ = 0u;
    if (appended_byte_count == 0u) {
        return expected_logical_write_count == 0u;
    }
    live_command_delta_bytes_.resize(
        static_cast<size_t>(appended_byte_count));
    const auto read_begin = std::chrono::steady_clock::now();
    live_command_file_.clear();
    live_command_file_.seekg(
        static_cast<std::streamoff>(header_size + local_first_byte),
        std::ios::beg);
    live_command_file_.read(
        reinterpret_cast<char*>(live_command_delta_bytes_.data()),
        static_cast<std::streamsize>(live_command_delta_bytes_.size()));
    last_command_file_read_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - read_begin).count();
    if (!live_command_file_) {
        live_command_delta_bytes_.clear();
        return false;
    }
    last_command_read_bytes_ = live_command_delta_bytes_.size();
    const auto validation_begin = std::chrono::steady_clock::now();
    uint64_t logical_write_count = 0u;
    size_t cursor = 0u;
    while (cursor < live_command_delta_bytes_.size()) {
        if (live_command_delta_bytes_.size() - cursor
            < kRecoveredD3DCommandSpanHeaderSize) {
            live_command_delta_bytes_.clear();
            return false;
        }
        const uint8_t* header = live_command_delta_bytes_.data() + cursor;
        const uint8_t kind = header[0];
        const uint8_t flags = header[1];
        uint32_t payload_size = 0u;
        uint32_t span_write_count = 0u;
        std::memcpy(&payload_size, header + 8u, sizeof(payload_size));
        std::memcpy(
            &span_write_count,
            header + 12u,
            sizeof(span_write_count));
        cursor += kRecoveredD3DCommandSpanHeaderSize;
        if (kind > 1u || (flags & ~1u) != 0u || payload_size == 0u
            || span_write_count == 0u
            || payload_size > live_command_delta_bytes_.size() - cursor) {
            live_command_delta_bytes_.clear();
            return false;
        }
        last_live_command_mmio_count_ += static_cast<uint32_t>(kind == 0u);
        logical_write_count += span_write_count;
        ++last_native_command_span_count_;
        cursor += payload_size;
    }
    last_command_record_validation_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - validation_begin).count();
    if (logical_write_count != expected_logical_write_count) {
        live_command_delta_bytes_.clear();
        return false;
    }
    last_native_command_read_count_ = static_cast<size_t>(logical_write_count);
    native_command_read_count_ += logical_write_count;
    native_command_span_read_count_ += last_native_command_span_count_;
    return true;
}

bool VulkanPresenter::read_live_command_span_transport_delta(
    uint64_t first_byte_count,
    uint64_t required_byte_count,
    uint64_t expected_logical_write_count) {
    last_command_file_open_us_ = 0;
    last_command_file_read_us_ = 0;
    last_command_record_validation_us_ = 0;
    last_command_read_bytes_ = 0u;
    last_live_command_mmio_count_ = 0u;
    last_native_command_span_count_ = 0u;
    last_command_file_reused_ = true;
    last_native_command_read_count_ = 0u;
    const auto read_begin = std::chrono::steady_clock::now();
    if (!transport_.read_command_bytes(
            first_byte_count,
            required_byte_count,
            live_command_delta_bytes_)) {
        return false;
    }
    last_command_file_read_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - read_begin).count();
    last_command_read_bytes_ = live_command_delta_bytes_.size();
    if (live_command_delta_bytes_.empty()) {
        return expected_logical_write_count == 0u;
    }
    const auto validation_begin = std::chrono::steady_clock::now();
    uint64_t logical_write_count = 0u;
    size_t cursor = 0u;
    while (cursor < live_command_delta_bytes_.size()) {
        if (live_command_delta_bytes_.size() - cursor
            < kRecoveredD3DCommandSpanHeaderSize) {
            live_command_delta_bytes_.clear();
            return false;
        }
        const uint8_t* header = live_command_delta_bytes_.data() + cursor;
        const uint8_t kind = header[0];
        const uint8_t flags = header[1];
        uint32_t payload_size = 0u;
        uint32_t span_write_count = 0u;
        std::memcpy(&payload_size, header + 8u, 4u);
        std::memcpy(&span_write_count, header + 12u, 4u);
        cursor += kRecoveredD3DCommandSpanHeaderSize;
        if (kind > 1u || (flags & ~1u) != 0u || payload_size == 0u
            || span_write_count == 0u
            || payload_size > live_command_delta_bytes_.size() - cursor) {
            live_command_delta_bytes_.clear();
            return false;
        }
        last_live_command_mmio_count_ += static_cast<uint32_t>(kind == 0u);
        logical_write_count += span_write_count;
        ++last_native_command_span_count_;
        cursor += payload_size;
    }
    last_command_record_validation_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - validation_begin).count();
    if (logical_write_count != expected_logical_write_count) {
        live_command_delta_bytes_.clear();
        return false;
    }
    const uint64_t capture_end = live_command_capture_base_byte_count_
        + live_command_capture_bytes_.size();
    if (first_byte_count != capture_end) {
        throw std::runtime_error(
            "shared command capture cursor lost synchronization");
    }
    live_command_capture_bytes_.insert(
        live_command_capture_bytes_.end(),
        live_command_delta_bytes_.begin(),
        live_command_delta_bytes_.end());
    transport_.commit_command_read_cursor(required_byte_count);
    last_native_command_read_count_ = static_cast<size_t>(logical_write_count);
    native_command_read_count_ += logical_write_count;
    native_command_span_read_count_ += last_native_command_span_count_;
    return true;
}

bool VulkanPresenter::read_live_resource_transport(
    uint32_t slot,
    uint64_t expected_size,
    const std::string& expected_generation,
    std::vector<uint8_t>& payload) const {
    return transport_.read_resource_slot(
        slot, expected_size, expected_generation, payload);
}

void VulkanPresenter::initialize_platform() {
    const bool enable_gamepads =
        !options_.controller_state_json.empty()
        || !options_.live_control_transport_name.empty();
    platform_.initialize(enable_gamepads);
    const auto& status = platform_.status();
    log_.emit(
        "platform_initialized",
        {
            {"backend", json_string("sdl3")},
            {"version", json_string(status.version)},
            {"revision", json_string(status.revision)},
            {"gamepad_initialized", json_bool(status.gamepad_initialized)},
            {"gamepad_error", status.gamepad_error.empty()
                ? "null" : json_string(status.gamepad_error)},
            {"custom_mapping_count", std::to_string(
                status.custom_mapping_count)},
            {"custom_mapping_path", status.custom_mapping_path.empty()
                ? "null" : json_string(status.custom_mapping_path)},
        });
}

void VulkanPresenter::list_adapters() {
    uint32_t device_count = 0;
    vk_check(vkEnumeratePhysicalDevices(instance_, &device_count, nullptr), "vkEnumeratePhysicalDevices");
    std::vector<VkPhysicalDevice> devices(device_count);
    if (device_count > 0) {
        vk_check(
            vkEnumeratePhysicalDevices(instance_, &device_count, devices.data()),
            "vkEnumeratePhysicalDevices");
    }
    for (uint32_t index = 0; index < device_count; ++index) {
        VkPhysicalDeviceProperties properties{};
        vkGetPhysicalDeviceProperties(devices[index], &properties);
        log_.emit(
            "vulkan_adapter",
            {
                {"index", std::to_string(index)},
                {"name", json_string(properties.deviceName)},
                {"vendor_id", std::to_string(properties.vendorID)},
                {"device_id", std::to_string(properties.deviceID)},
            });
    }
}

void VulkanPresenter::create_window() {
    platform_.create_window(
        narrow(options_.title), options_.width, options_.height);
    log_.emit(
        "window_created",
        {
            {"title", json_string(narrow(options_.title))},
            {"width", std::to_string(options_.width)},
            {"height", std::to_string(options_.height)},
        });
}

void VulkanPresenter::create_surface() {
    surface_ = platform_.create_vulkan_surface(instance_);
    log_.emit("vulkan_surface_created", {{"platform", json_string("sdl3")}});
}

bool VulkanPresenter::load_recovered_render_work(
    const std::function<void()>& source_resident_callback) {
    const auto command_load_begin = std::chrono::steady_clock::now();
    last_resource_snapshot_reused_count_ = 0u;
    last_resource_snapshot_reused_bytes_ = 0u;
    auto after_manifest_read = command_load_begin;
    bool resources_unchanged = false;
    bool resource_generation_changed = false;
    bool resource_generation_declared = false;
    bool shared_resource_transport = false;
    uint32_t shared_resource_slot = 0u;
    uint64_t shared_resource_size = 0u;
    std::filesystem::path resource_source;
    std::filesystem::file_time_type resource_write_time{};
    std::string next_resource_generation = live_resource_generation_;
    std::string manifest_text;
    if (options_.live_render_stream && !options_.render_stream_json.empty()) {
        manifest_text = read_live_render_manifest().value_or(
            std::string{});
        after_manifest_read = std::chrono::steady_clock::now();
        const size_t final_non_space = manifest_text.find_last_not_of(" \t\r\n");
        if (final_non_space == std::string::npos
            || manifest_text[final_non_space] != '}') {
            return false;
        }
        if (!options_.flip_audit_ack.empty()) {
            const auto deadline = std::chrono::steady_clock::now()
                + std::chrono::seconds(2);
            while (!update_flip_audit_manifest_state(manifest_text)) {
                if (std::chrono::steady_clock::now() >= deadline) {
                    throw std::runtime_error(
                        "lossless flip audit manifest is missing required fields");
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
                manifest_text = read_live_render_manifest().value_or(
                    std::string{});
            }
        }
        const auto guest_flip_field = json_object_field_text(
            manifest_text, "guest_flip_count");
        const auto guest_steps_field = json_object_field_text(
            manifest_text, "guest_steps");
        const auto guest_compiled_blocks_field = json_object_field_text(
            manifest_text, "guest_compiled_blocks");
        const auto guest_invalidations_field = json_object_field_text(
            manifest_text, "guest_invalidations");
        const auto presentable_command_field = json_object_field_text(
            manifest_text, "presentable_command_record_count");
        const auto published_command_field = json_object_field_text(
            manifest_text, "published_command_record_count");
        const auto command_base_field = json_object_field_text(
            manifest_text, "command_snapshot_base_record_count");
        const auto command_transport_field = json_object_field_text(
            manifest_text, "command_transport_format");
        const auto presentable_command_byte_field = json_object_field_text(
            manifest_text, "presentable_command_byte_count");
        const auto command_base_byte_field = json_object_field_text(
            manifest_text, "command_snapshot_base_byte_count");
        if (guest_flip_field.has_value()) {
            current_manifest_guest_flip_count_ = parse_json_u64_text(
                *guest_flip_field, "guest flip count");
        }
        if (guest_steps_field.has_value()) {
            current_manifest_guest_steps_ = parse_json_u64_text(
                *guest_steps_field, "guest steps");
        }
        if (guest_compiled_blocks_field.has_value()) {
            current_manifest_guest_compiled_blocks_ = parse_json_u64_text(
                *guest_compiled_blocks_field, "guest compiled blocks");
        }
        if (guest_invalidations_field.has_value()) {
            current_manifest_guest_invalidations_ = parse_json_u64_text(
                *guest_invalidations_field, "guest invalidations");
        }
        if (presentable_command_field.has_value()) {
            current_manifest_presentable_command_count_ = parse_json_u64_text(
                *presentable_command_field, "presentable command record count");
        } else if (published_command_field.has_value()) {
            current_manifest_presentable_command_count_ = parse_json_u64_text(
                *published_command_field, "published command record count");
        }
        current_manifest_command_base_count_ = command_base_field.has_value()
            ? parse_json_u64_text(
                *command_base_field, "command snapshot base record count")
            : 0u;
        current_manifest_shared_command_transport_ =
            command_transport_field.has_value()
            && *command_transport_field == "shared_memory_span_v1";
        current_manifest_bulk_span_commands_ =
            command_transport_field.has_value()
            && (*command_transport_field == "bulk_span_v1"
                || current_manifest_shared_command_transport_);
        current_manifest_presentable_command_byte_count_ =
            presentable_command_byte_field.has_value()
            ? parse_json_u64_text(
                *presentable_command_byte_field,
                "presentable command byte count")
            : 0u;
        current_manifest_command_base_byte_count_ =
            command_base_byte_field.has_value()
            ? parse_json_u64_text(
                *command_base_byte_field,
                "command snapshot base byte count")
            : 0u;
        if (current_manifest_bulk_span_commands_
            && (!presentable_command_byte_field.has_value()
                || !command_base_byte_field.has_value()
                || current_manifest_command_base_byte_count_
                    > current_manifest_presentable_command_byte_count_)) {
            throw std::runtime_error(
                "bulk-span manifest has an invalid command byte boundary");
        }
        if (current_manifest_command_base_count_
            > current_manifest_presentable_command_count_) {
            throw std::runtime_error(
                "live render command snapshot begins after its presentable boundary");
        }
        if (current_manifest_shared_command_transport_
            && current_manifest_command_base_byte_count_
                > live_command_capture_base_byte_count_) {
            const uint64_t trim = current_manifest_command_base_byte_count_
                - live_command_capture_base_byte_count_;
            if (trim > live_command_capture_bytes_.size()) {
                throw std::runtime_error(
                    "shared command epoch advanced beyond retained capture bytes");
            }
            live_command_capture_bytes_.erase(
                live_command_capture_bytes_.begin(),
                live_command_capture_bytes_.begin()
                    + static_cast<std::ptrdiff_t>(trim));
            live_command_capture_base_byte_count_ =
                current_manifest_command_base_byte_count_;
        }
        const auto resource_field = json_object_field_text(
            manifest_text, "resource_snapshot_path");
        const auto resource_generation_field = json_object_field_text(
            manifest_text, "resource_stream_generation");
        const auto resource_transport_field = json_object_field_text(
            manifest_text, "resource_transport_format");
        const auto resource_slot_field = json_object_field_text(
            manifest_text, "resource_transport_slot");
        const auto resource_size_field = json_object_field_text(
            manifest_text, "resource_transport_size");
        shared_resource_transport = resource_transport_field.has_value()
            && *resource_transport_field == "shared_memory_slot_v1";
        if (shared_resource_transport) {
            if (!transport_.active()
                || !resource_slot_field.has_value()
                || !resource_size_field.has_value()) {
                throw std::runtime_error(
                    "shared resource manifest is incomplete");
            }
            shared_resource_slot = parse_json_u32_text(
                *resource_slot_field, "resource transport slot");
            shared_resource_size = parse_json_u64_text(
                *resource_size_field, "resource transport size");
            if (shared_resource_slot > 1u
                || shared_resource_size
                    > transport_.resource_slot_capacity()) {
                throw std::runtime_error(
                    "shared resource manifest exceeds its transport slot");
            }
        }
        resource_generation_declared = resource_generation_field.has_value();
        resource_source = resource_field.has_value()
            ? std::filesystem::path(*resource_field)
            : options_.render_stream_json;
        if (resource_generation_field.has_value()) {
            next_resource_generation = *resource_generation_field;
            resources_unchanged = !live_resource_generation_.empty()
                && next_resource_generation == live_resource_generation_
                && resource_source == live_resource_source_;
            if (!live_resource_generation_.empty()
                && next_resource_generation == live_resource_generation_
                && !live_resource_source_.empty()
                && resource_source != live_resource_source_) {
                throw std::runtime_error(
                    "live resource generation changed snapshot paths");
            }
            resource_generation_changed = !resources_unchanged;
        } else {
            std::error_code resource_error;
            resource_write_time = std::filesystem::last_write_time(
                resource_source, resource_error);
            if (!resource_error
                && resource_source == recovered_source_.resource_source
                && resource_write_time == live_resource_write_time_) {
                resources_unchanged = true;
            } else if (!resource_error) {
                resource_generation_changed = true;
            }
        }
    }
    const auto command_field = manifest_text.empty()
        ? std::optional<std::string>{}
        : json_object_field_text(manifest_text, "command_snapshot_path");
    const auto generation_field = manifest_text.empty()
        ? std::optional<std::string>{}
        : json_object_field_text(manifest_text, "command_stream_generation");
    const auto presentation_ack_field = manifest_text.empty()
        ? std::optional<std::string>{}
        : json_object_field_text(manifest_text, "presentation_ack_path");
    const auto presentation_event_field = manifest_text.empty()
        ? std::optional<std::string>{}
        : json_object_field_text(manifest_text, "presentation_event_name");
    const auto publication_event_field = manifest_text.empty()
        ? std::optional<std::string>{}
        : json_object_field_text(manifest_text, "publication_event_name");
    if (!manifest_text.empty()) {
        current_live_manifest_text_ = manifest_text;
        current_live_command_snapshot_path_ = command_field.has_value()
            ? std::filesystem::path(*command_field)
            : std::filesystem::path{};
        current_live_resource_snapshot_path_ = resource_source;
    } else {
        current_live_manifest_text_.clear();
        current_live_command_snapshot_path_.clear();
        current_live_resource_snapshot_path_.clear();
    }
    if (presentation_ack_field.has_value()) {
        const std::filesystem::path next_ack_path(*presentation_ack_field);
        if (next_ack_path != presentation_ack_path_) {
            if (presentation_ack_file_ != INVALID_HANDLE_VALUE) {
                CloseHandle(presentation_ack_file_);
                presentation_ack_file_ = INVALID_HANDLE_VALUE;
            }
            presentation_ack_path_ = next_ack_path;
            acknowledged_presentation_flip_ = 0u;
        }
    }
    if (presentation_event_field.has_value()
        && *presentation_event_field != presentation_event_name_) {
        if (presentation_ack_event_) {
            CloseHandle(presentation_ack_event_);
            presentation_ack_event_ = nullptr;
        }
        presentation_event_name_ = *presentation_event_field;
    }
    if (publication_event_field.has_value()
        && *publication_event_field != publication_event_name_) {
        publication_retry_pending_ = false;
        if (publication_event_) {
            CloseHandle(publication_event_);
            publication_event_ = nullptr;
        }
        publication_event_name_ = *publication_event_field;
        publication_event_ = OpenEventW(
            SYNCHRONIZE,
            FALSE,
            widen(publication_event_name_).c_str());
    }
    const bool command_generation_changed = options_.live_render_stream
        && generation_field.has_value()
        && *generation_field != live_command_generation_;
    const bool native_incremental_commands = options_.live_render_stream
        && command_field.has_value()
        && generation_field.has_value();
    if (options_.live_render_stream && !native_incremental_commands) {
        throw std::runtime_error(
            "live render manifest is missing its command snapshot identity");
    }
    uint64_t target_command_count =
        current_manifest_presentable_command_count_;
    const uint64_t target_command_byte_count =
        current_manifest_presentable_command_byte_count_;
    if (!options_.flip_audit_ack.empty()) {
        if (current_audit_command_count_ > target_command_count) {
            throw std::runtime_error(
                "lossless flip audit exceeds the presentable command boundary");
        }
        target_command_count = current_audit_command_count_;
    }
    bool reset_interpreter = false;
    uint64_t command_read_begin = 0u;
    uint64_t command_read_begin_byte = 0u;
    if (native_incremental_commands) {
        reset_interpreter = command_generation_changed
            || interpreted_source_command_count_ > target_command_count
            || (current_manifest_bulk_span_commands_
                && interpreted_source_command_byte_count_
                    > target_command_byte_count);
        if (reset_interpreter) {
            if (current_manifest_command_base_count_ != 0u
                && !options_.analyze_render_stream_only) {
                throw std::runtime_error(
                    "live render continuation snapshot cannot bootstrap a new presenter");
            }
            command_read_begin = current_manifest_command_base_count_;
            command_read_begin_byte =
                current_manifest_command_base_byte_count_;
        } else if (interpreted_source_command_count_
            < current_manifest_command_base_count_) {
            if (!options_.analyze_render_stream_only) {
                throw std::runtime_error(
                    "live render continuation snapshot skipped uninterpreted commands");
            }
            reset_interpreter = true;
            command_read_begin = current_manifest_command_base_count_;
            command_read_begin_byte =
                current_manifest_command_base_byte_count_;
        } else {
            command_read_begin = interpreted_source_command_count_;
            command_read_begin_byte = interpreted_source_command_byte_count_;
        }
        if (command_read_begin > target_command_count) {
            throw std::runtime_error(
                "live command cursor passed the presentable boundary");
        }
        if (current_manifest_bulk_span_commands_
            && command_read_begin_byte > target_command_byte_count) {
            throw std::runtime_error(
                "live command byte cursor passed the presentable boundary");
        }
    }
    const auto before_source_load = std::chrono::steady_clock::now();
    if (native_incremental_commands) {
        const bool loaded_commands =
            current_manifest_shared_command_transport_
            ? read_live_command_span_transport_delta(
                command_read_begin_byte,
                target_command_byte_count,
                target_command_count - command_read_begin)
            : current_manifest_bulk_span_commands_
            ? read_live_command_span_stream_delta(
                std::filesystem::path(*command_field),
                current_manifest_command_base_byte_count_,
                command_read_begin_byte,
                target_command_byte_count,
                target_command_count - command_read_begin)
            : read_live_command_stream_delta(
                std::filesystem::path(*command_field),
                current_manifest_command_base_count_,
                command_read_begin,
                target_command_count);
        if (!loaded_commands) {
            return false;
        }
    }
    // Validate and retain the command delta before moving hash-identical
    // payloads out of the active resource snapshot. An incomplete sidecar
    // retry must leave the currently presented resources intact.
    std::vector<uint8_t> shared_resource_bytes;
    if (shared_resource_transport && !resources_unchanged) {
        if (!read_live_resource_transport(
                shared_resource_slot,
                shared_resource_size,
                next_resource_generation,
                shared_resource_bytes)) {
            return false;
        }
    }
    RecoveredD3DStreamSource next_source = load_or_build_recovered_d3d_command_stream(
        options_.render_stream_json,
        !resources_unchanged && !shared_resource_transport,
        manifest_text.empty() ? nullptr : &manifest_text,
        !native_incremental_commands,
        options_.live_render_stream && !resources_unchanged,
        options_.live_render_stream && !resources_unchanged
            ? &recovered_source_.textures : nullptr,
        &last_resource_snapshot_reused_count_,
        &last_resource_snapshot_reused_bytes_);
    if (shared_resource_transport && !resources_unchanged) {
        next_source.textures = load_recovered_texture_resources_bytes(
            shared_resource_bytes,
            &recovered_source_.textures,
            &last_resource_snapshot_reused_count_,
            &last_resource_snapshot_reused_bytes_);
        current_live_resource_snapshot_bytes_ =
            std::move(shared_resource_bytes);
    } else if (!shared_resource_transport && !resources_unchanged) {
        current_live_resource_snapshot_bytes_.clear();
    }
    if (resources_unchanged) {
        next_source.textures = std::move(recovered_source_.textures);
    }
    const auto after_source_load = std::chrono::steady_clock::now();
    if (options_.live_render_stream) {
        live_command_generation_ = *generation_field;
        live_resource_generation_ = next_resource_generation;
        live_resource_source_ = resource_source;
        if (!resource_generation_declared) {
            live_resource_write_time_ = resource_write_time;
        }
    }
    next_source.resources_unchanged = resources_unchanged;
    recovered_source_ = std::move(next_source);
    last_resource_generation_changed_ = resource_generation_changed;
    last_command_cursor_reset_ = reset_interpreter;
    const auto after_command_load = std::chrono::steady_clock::now();
    if (source_resident_callback) {
        // The packed command delta and resource payloads are private to the
        // presenter now. A depth-two reload may release the producer before
        // interpreting either buffer; subsequent epoch rotation cannot
        // invalidate these resident copies.
        source_resident_callback();
    }
    const auto before_interpret = std::chrono::steady_clock::now();
    if (options_.live_render_stream) {
        if (reset_interpreter) {
            interpreted_stream_ = InterpretedD3DStream{};
            interpreted_stream_.state_seed = 0xB200D3D8u;
            interpreted_source_command_count_ = static_cast<size_t>(
                current_manifest_command_base_count_);
            interpreted_source_command_byte_count_ =
                current_manifest_command_base_byte_count_;
            continuation_analysis_bootstrap_ =
                current_manifest_command_base_count_ != 0u;
            if (continuation_analysis_bootstrap_) {
                log_.emit(
                    "render_stream_continuation_bootstrapped",
                    {
                        {"analysis_only", json_bool(true)},
                        {"state_history_complete", json_bool(false)},
                        {"command_snapshot_base_record_count", std::to_string(current_manifest_command_base_count_)},
                        {"presentable_command_record_count", std::to_string(target_command_count)},
                        {"resident_command_record_count", std::to_string(
                            target_command_count - current_manifest_command_base_count_)},
                    });
            }
        }
        const uint64_t expected_delta = target_command_count - command_read_begin;
        if (current_manifest_bulk_span_commands_) {
            const uint64_t interpreted_delta =
                interpret_recovered_d3d_span_append(
                    live_command_delta_bytes_,
                    interpreted_stream_);
            if (interpreted_delta != expected_delta) {
                throw std::runtime_error(
                    "bulk live command interpreter returned an incomplete delta");
            }
        } else {
            if (expected_delta
                != live_command_delta_bytes_.size()
                    / kRecoveredD3DCommandRecordSize) {
                throw std::runtime_error(
                    "native live command reader returned an incomplete delta");
            }
            interpret_recovered_d3d_packed_append(
                live_command_delta_bytes_,
                interpreted_stream_);
        }
        last_interpreted_command_delta_ = static_cast<size_t>(expected_delta);
        interpreted_source_command_count_ = static_cast<size_t>(
            target_command_count);
        interpreted_source_command_byte_count_ = target_command_byte_count;
    } else {
        interpreted_stream_ = interpret_recovered_d3d_stream(
            recovered_source_.commands,
            recovered_source_.interpreter_bootstrap_source);
        interpreted_source_command_count_ = recovered_source_.commands.size();
        last_interpreted_command_delta_ = interpreted_source_command_count_;
    }
    const auto after_method_interpret = std::chrono::steady_clock::now();
    materialize_indexed_draws(
        interpreted_stream_,
        recovered_source_.textures,
        gpu_raw_vertex_resource_cache_,
        !options_.cpu_vertex_programs
            && !options_.cpu_vertex_attributes
            && !options_.analyze_render_stream_only,
        recovered_source_.resources_unchanged);
    if (last_interpreted_command_delta_ != 0u
        || last_resource_generation_changed_) {
        ++render_work_generation_;
        presented_diagnostics_valid_ = false;
    }
    presented_surface_color_offset_ =
        select_presented_surface_color_offset();
    const auto after_interpret = std::chrono::steady_clock::now();
    last_command_load_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
        after_command_load - command_load_begin).count();
    last_manifest_read_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
        after_manifest_read - command_load_begin).count();
    last_manifest_parse_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
        before_source_load - after_manifest_read).count();
    last_source_load_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
        after_source_load - before_source_load).count();
    last_interpret_us_ = std::chrono::duration_cast<std::chrono::microseconds>(
        after_interpret - before_interpret).count();
    last_method_interpret_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            after_method_interpret - before_interpret).count();
    last_indexed_materialize_us_ = std::chrono::duration_cast<
        std::chrono::microseconds>(
            after_interpret - after_method_interpret).count();
    recovered_frontend_text_ = recovered_source_.frontend_text;
    if (options_.live_render_stream && !publication_event_) {
        std::error_code error;
        live_render_write_time_ = std::filesystem::last_write_time(
            options_.render_stream_json, error);
    }
    return true;
}

void VulkanPresenter::destroy_native_render_resources(
    bool preserve_textures,
    bool preserve_vertex_buffer,
    bool preserve_offscreen_render_targets) {
    if (!preserve_offscreen_render_targets) {
        destroy_offscreen_render_targets();
    }
    if (!command_buffers_.empty()) {
        vkFreeCommandBuffers(
            device_, command_pool_, static_cast<uint32_t>(command_buffers_.size()),
            command_buffers_.data());
        command_buffers_.clear();
        command_buffer_draw_counts_.clear();
        command_buffer_triangle_counts_.clear();
        command_buffer_barrier_counts_.clear();
    }
    if (!preserve_vertex_buffer) {
        destroy_host_texture_bindings();
        if (vertex_mapped_) {
            vkUnmapMemory(device_, vertex_memory_);
            vertex_mapped_ = nullptr;
        }
        if (vertex_buffer_) {
            vkDestroyBuffer(device_, vertex_buffer_, nullptr);
            vertex_buffer_ = VK_NULL_HANDLE;
        }
        if (vertex_memory_) {
            vkFreeMemory(device_, vertex_memory_, nullptr);
            vertex_memory_ = VK_NULL_HANDLE;
        }
        vertex_buffer_size_ = 0;
    }
    if (!preserve_textures) {
        destroy_host_texture_bindings();
        for (const HostTexture& texture : host_textures_) {
            if (texture.view) vkDestroyImageView(device_, texture.view, nullptr);
            if (texture.image) vkDestroyImage(device_, texture.image, nullptr);
            if (texture.memory) vkFreeMemory(device_, texture.memory, nullptr);
        }
        host_textures_.clear();
        for (HostTexture& texture :
             render_target_feedback_image_cache_) {
            destroy_host_texture(texture);
        }
        render_target_feedback_image_cache_.clear();
    }
    presented_half_quad_recovered_ = false;
    presented_overscan_height_recovered_ = false;
    presented_vertex_program_transformed_count_ = 0;
    presented_fixed_function_transformed_count_ = 0;
    presented_linear_texture_normalized_vertex_count_ = 0;
    offscreen_render_target_draw_count_ = 0;
    offscreen_render_target_transformed_vertex_count_ = 0;
    frontend_text_rectangle_count_ = 0;
    frontend_text_first_vertex_ = 0;
    frontend_text_vertex_count_ = 0;
    frontend_text_fragment_state_index_ =
        std::numeric_limits<uint32_t>::max();
}

void VulkanPresenter::reload_live_render_work(bool publication_signaled) {
    if (!options_.live_render_stream) {
        return;
    }
    const auto probe_begin = std::chrono::steady_clock::now();
    if (publication_event_ && !publication_signaled) {
        if (publication_retry_pending_) {
            publication_signaled = true;
        } else {
            const DWORD result = WaitForSingleObject(publication_event_, 0u);
            if (result == WAIT_TIMEOUT) {
                last_reload_probe_us_ += static_cast<uint64_t>(
                    std::chrono::duration_cast<std::chrono::microseconds>(
                        std::chrono::steady_clock::now() - probe_begin).count());
                return;
            }
            if (result != WAIT_OBJECT_0) {
                throw std::runtime_error(
                    "publication event probe failed");
            }
            publication_signaled = true;
        }
    }
    // The validated manifest is the publication boundary. Command/resource
    // sidecars are written first and may grow while a guest frame is still
    // under construction; consuming them directly would replay partial
    // work under a stale flip identity.
    if (!publication_signaled) {
        std::error_code error;
        const auto write_time = std::filesystem::last_write_time(
            options_.render_stream_json, error);
        if (error || write_time == live_render_write_time_) {
            last_reload_probe_us_ += static_cast<uint64_t>(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::steady_clock::now() - probe_begin).count());
            return;
        }
    }
    last_reload_probe_us_ += static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now() - probe_begin).count());
    const auto reload_begin = std::chrono::steady_clock::now();
    // Only one frame can be in flight. Waiting for its fence is sufficient
    // before replacing command buffers/resources and avoids draining the
    // entire device on every guest publication.
    wait_for_in_flight_fence("vkWaitForFences(live reload)");
    const auto after_wait = std::chrono::steady_clock::now();
    auto source_resident = after_wait;
    auto presentation_ack_begin = after_wait;
    auto presentation_ack_end = after_wait;
    bool presentation_ack_published = false;
    const auto source_resident_callback = [&]() {
        source_resident = std::chrono::steady_clock::now();
        if (options_.presentation_pipeline_depth == 2u) {
            // Both sidecars are fully loaded into presenter-owned memory.
            // Releasing the guest here overlaps method interpretation,
            // indexed materialization, and all Vulkan preparation with its
            // next flip while preserving one-future-publication ordering.
            presentation_ack_begin = source_resident;
            presentation_ack_published = acknowledge_current_presentation();
            presentation_ack_end = std::chrono::steady_clock::now();
        }
    };
    if (!load_recovered_render_work(source_resident_callback)) {
        publication_retry_pending_ = publication_event_ != nullptr;
        return;
    }
    publication_retry_pending_ = false;
    const auto after_load = std::chrono::steady_clock::now();
    if (last_interpreted_command_delta_ == 0u
        && recovered_source_.resources_unchanged
        && current_manifest_guest_flip_count_ != 0u
        && current_manifest_guest_flip_count_
            <= acknowledged_presentation_flip_) {
        ++live_render_skipped_reload_count_;
        log_.emit(
            "live_render_stream_reload_skipped",
            {
                {"skipped_reload", std::to_string(live_render_skipped_reload_count_)},
                {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
                {"presentable_command_record_count", std::to_string(current_manifest_presentable_command_count_)},
                {"reason", json_string("already_presented_without_command_or_resource_delta")},
                {"load_interpret_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_load - after_wait).count())},
            });
        return;
    }
    // Keep existing images alive across a resource generation change so
    // refresh_host_textures can retain payload-identical textures and
    // upload only additions/replacements.
    const bool preserve_textures = !host_textures_.empty();
    const auto [presented_vertex_begin, presented_vertex_end] =
        presented_vertex_span(interpreted_stream_);
    const VkDeviceSize required_vertex_bytes =
        std::max<VkDeviceSize>(
            (presented_vertex_end - presented_vertex_begin)
                * sizeof(NativeVertex),
            sizeof(NativeVertex));
    const bool preserve_vertex_buffer = vertex_buffer_ != VK_NULL_HANDLE
        && required_vertex_bytes <= vertex_buffer_size_;
    // Resource payload generations do not own GPU-produced feedback
    // attachments. Keep an offscreen framebuffer when the newly loaded
    // command stream requests the same target and its exact backing view
    // is still resident; refresh_host_textures verifies the view again
    // after applying the new generation.
    const bool preserve_offscreen_render_targets =
        preserve_textures
        && !offscreen_render_targets_.empty()
        && offscreen_render_targets_match_presented_specs();
    const auto resource_destroy_begin =
        std::chrono::steady_clock::now();
    destroy_native_render_resources(
        preserve_textures,
        preserve_vertex_buffer,
        preserve_offscreen_render_targets);
    const auto after_resource_destroy =
        std::chrono::steady_clock::now();
    create_native_render_resources(
        !recovered_source_.resources_unchanged,
        preserve_offscreen_render_targets);
    const auto after_native_resources =
        std::chrono::steady_clock::now();
    create_native_graphics_pipeline();
    const auto after_resources = std::chrono::steady_clock::now();
    const int64_t resource_preflight_us =
        std::chrono::duration_cast<std::chrono::microseconds>(
            resource_destroy_begin - after_load).count();
    const int64_t resource_destroy_us =
        std::chrono::duration_cast<std::chrono::microseconds>(
            after_resource_destroy - resource_destroy_begin).count();
    const int64_t native_resource_create_us =
        std::chrono::duration_cast<std::chrono::microseconds>(
            after_native_resources - after_resource_destroy).count();
    const int64_t pipeline_prepare_us =
        std::chrono::duration_cast<std::chrono::microseconds>(
            after_resources - after_native_resources).count();
    const int64_t resource_update_us =
        std::chrono::duration_cast<std::chrono::microseconds>(
            after_resources - after_load).count();
    const int64_t resource_prepare_us = std::max<int64_t>(
        resource_update_us - last_render_validation_us_,
        0);
    create_command_buffers();
    const auto after_commands = std::chrono::steady_clock::now();
    if (options_.presentation_pipeline_depth == 1u) {
        presentation_ack_begin = std::chrono::steady_clock::now();
        presentation_ack_published = acknowledge_current_presentation();
        presentation_ack_end = std::chrono::steady_clock::now();
    }
    ++live_render_reload_count_;
    log_.emit(
        "live_render_stream_reloaded",
        {
            {"reload", std::to_string(live_render_reload_count_)},
            {"writes", std::to_string(interpreted_source_command_count_)},
            {"guest_flips", std::to_string(interpreted_stream_.flip_count)},
            {"manifest_guest_flip_count", std::to_string(current_manifest_guest_flip_count_)},
            {"manifest_guest_steps", std::to_string(current_manifest_guest_steps_)},
            {"launch_transform_program_count", std::to_string(interpreted_stream_.launch_transform_program_count)},
            {"failed_launch_transform_program_count", std::to_string(interpreted_stream_.failed_launch_transform_program_count)},
            {"presentable_command_record_count", std::to_string(current_manifest_presentable_command_count_)},
            {"command_snapshot_base_record_count", std::to_string(current_manifest_command_base_count_)},
            {"resident_command_record_count", std::to_string(
                current_manifest_presentable_command_count_
                    - current_manifest_command_base_count_)},
            {"native_command_records_read", std::to_string(
                last_native_command_read_count_)},
            {"native_command_records_read_total", std::to_string(
                native_command_read_count_)},
            {"native_command_spans_read", std::to_string(
                last_native_command_span_count_)},
            {"native_command_spans_read_total", std::to_string(
                native_command_span_read_count_)},
            {"command_transport", json_string(
                current_manifest_shared_command_transport_
                    ? "shared_memory_span_v1"
                    : current_manifest_bulk_span_commands_
                    ? "bulk_span_v1" : "packed_direct")},
            {"resource_transport", json_string(
                current_live_resource_snapshot_bytes_.empty()
                    ? "file" : "shared_memory_slot_v1")},
            {"command_file_reused", json_bool(last_command_file_reused_)},
            {"command_read_bytes", std::to_string(last_command_read_bytes_)},
            {"command_file_open_us", std::to_string(
                last_command_file_open_us_)},
            {"command_file_read_us", std::to_string(
                last_command_file_read_us_)},
            {"command_record_validation_us", std::to_string(
                last_command_record_validation_us_)},
            {"interpreted_source_commands", std::to_string(interpreted_source_command_count_)},
            {"interpreted_command_delta", std::to_string(last_interpreted_command_delta_)},
            {"pending_method_packet", json_bool(interpreted_stream_.pending_method_packet)},
            {"truncated_packets", std::to_string(interpreted_stream_.truncated_packet_count)},
            {"control_flow_packets", std::to_string(interpreted_stream_.control_flow_packet_count)},
            {"unknown_packets", std::to_string(interpreted_stream_.unknown_packet_count)},
            {"zero_count_indexed_array_noop_packets", std::to_string(interpreted_stream_.zero_count_indexed_array_noop_packet_count)},
            {"ordered_push_buffer_appends", std::to_string(
                interpreted_stream_.ordered_push_buffer_append_count)},
            {"indexed_word_push_buffer_appends", std::to_string(
                interpreted_stream_.indexed_word_push_buffer_append_count)},
            {"reconstructed_push_buffer_appends", std::to_string(
                interpreted_stream_.reconstructed_push_buffer_append_count)},
            {"surface_payload_scans_skipped", std::to_string(
                interpreted_stream_.surface_payload_scan_skipped_count)},
            {"exact_completed_flip", json_bool(
                interpreted_source_command_count_
                    == current_manifest_presentable_command_count_
                && interpreted_stream_.flip_count
                    == current_manifest_guest_flip_count_)},
            {"resources_unchanged", json_bool(recovered_source_.resources_unchanged)},
            {"resource_generation_changed", json_bool(
                last_resource_generation_changed_)},
            {"resource_snapshot_reused_resources", std::to_string(
                last_resource_snapshot_reused_count_)},
            {"resource_snapshot_reused_bytes", std::to_string(
                last_resource_snapshot_reused_bytes_)},
            {"render_work_generation", std::to_string(
                render_work_generation_)},
            {"presented_diagnostics_valid", json_bool(
                presented_diagnostics_valid_
                && presented_diagnostics_generation_
                    == render_work_generation_)},
            {"presented_diagnostics_sampled", json_bool(
                last_presented_diagnostics_sampled_)},
            {"offscreen_render_targets_reused", json_bool(
                last_offscreen_render_targets_reused_)},
            {"offscreen_render_target_count", std::to_string(
                offscreen_render_targets_.size())},
            {"presentation_pipeline_depth", std::to_string(
                options_.presentation_pipeline_depth)},
            {"presentation_ack_phase", json_string(
                options_.presentation_pipeline_depth == 2u
                    ? "after_source_load"
                    : "after_command_record")},
            {"presentation_ack_published", json_bool(
                presentation_ack_published)},
            {"presentation_ack_us", std::to_string(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    presentation_ack_end - presentation_ack_begin).count())},
            {"pre_ack_us", std::to_string(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    presentation_ack_end - reload_begin).count())},
            {"post_ack_us", std::to_string(
                options_.presentation_pipeline_depth == 2u
                    ? std::chrono::duration_cast<std::chrono::microseconds>(
                        after_commands - presentation_ack_end).count()
                    : 0)},
            {"wait_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_wait - reload_begin).count())},
            {"source_residency_us", std::to_string(
                std::chrono::duration_cast<std::chrono::microseconds>(
                    source_resident - after_wait).count())},
            {"load_interpret_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_load - after_wait).count())},
            {"command_load_us", std::to_string(last_command_load_us_)},
            {"manifest_read_us", std::to_string(last_manifest_read_us_)},
            {"manifest_parse_us", std::to_string(last_manifest_parse_us_)},
            {"source_load_us", std::to_string(last_source_load_us_)},
            {"interpret_us", std::to_string(last_interpret_us_)},
            {"method_interpret_us", std::to_string(last_method_interpret_us_)},
            {"push_buffer_collect_us", std::to_string(
                interpreted_stream_.last_push_buffer_collect_us)},
            {"method_apply_us", std::to_string(
                interpreted_stream_.last_method_apply_us)},
            {"method_finalize_us", std::to_string(
                interpreted_stream_.last_method_finalize_us)},
            {"interpreted_method_delta", std::to_string(
                interpreted_stream_.last_interpreted_method_count)},
            {"bulk_indexed_method_delta", std::to_string(
                interpreted_stream_.last_bulk_indexed_method_count)},
            {"bulk_inline_method_delta", std::to_string(
                interpreted_stream_.last_bulk_inline_method_count)},
            {"state_seed_updates_required", json_bool(
                interpreted_stream_.state_seed_updates_required)},
            {"indexed_materialize_us", std::to_string(
                last_indexed_materialize_us_)},
            {"resource_update_us", std::to_string(resource_update_us)},
            {"resource_prepare_us", std::to_string(resource_prepare_us)},
            {"resource_preflight_us", std::to_string(
                resource_preflight_us)},
            {"resource_destroy_us", std::to_string(resource_destroy_us)},
            {"native_resource_create_us", std::to_string(
                native_resource_create_us)},
            {"vertex_resource_prepare_us", std::to_string(
                last_vertex_resource_prepare_us_)},
            {"state_resource_prepare_us", std::to_string(
                last_state_resource_prepare_us_)},
            {"texture_resource_prepare_us", std::to_string(
                last_texture_resource_prepare_us_)},
            {"texture_refresh_us", std::to_string(
                last_texture_refresh_us_)},
            {"texture_indexed_lookup_count", std::to_string(
                last_texture_indexed_lookup_count_)},
            {"texture_indexed_lookup_candidates", std::to_string(
                last_texture_indexed_lookup_candidate_count_)},
            {"texture_constant_lookup_count", std::to_string(
                last_texture_constant_lookup_count_)},
            {"render_target_feedback_image_cache_hits", std::to_string(
                last_render_target_feedback_image_cache_hit_count_)},
            {"render_target_feedback_image_cache_misses", std::to_string(
                last_render_target_feedback_image_cache_miss_count_)},
            {"render_target_feedback_image_cache_stores", std::to_string(
                last_render_target_feedback_image_cache_store_count_)},
            {"render_target_feedback_image_cache_evictions", std::to_string(
                last_render_target_feedback_image_cache_eviction_count_)},
            {"render_target_feedback_image_cache_resident", std::to_string(
                render_target_feedback_image_cache_.size())},
            {"render_target_feedback_image_cache_capacity", std::to_string(
                kRenderTargetFeedbackImageCacheCapacity)},
            {"offscreen_resource_prepare_us", std::to_string(
                last_offscreen_resource_prepare_us_)},
            {"resource_bookkeeping_us", std::to_string(
                last_resource_bookkeeping_us_)},
            {"pipeline_prepare_us", std::to_string(
                pipeline_prepare_us)},
            {"pipeline_state_discovery_us", std::to_string(
                last_pipeline_state_discovery_us_)},
            {"pipeline_candidate_draws", std::to_string(
                last_pipeline_candidate_draw_count_)},
            {"pipeline_unique_states", std::to_string(
                last_pipeline_unique_state_count_)},
            {"pipeline_missing_states", std::to_string(
                last_pipeline_missing_state_count_)},
            {"feedback_spec_build_us", std::to_string(
                last_feedback_spec_build_us_)},
            {"feedback_spec_cache_builds", std::to_string(
                feedback_spec_cache_build_count_)},
            {"feedback_spec_cache_hits", std::to_string(
                feedback_spec_cache_hit_count_)},
            {"texture_binding_update_us", std::to_string(
                last_texture_binding_update_us_)},
            {"texture_binding_set_reused", json_bool(
                last_texture_binding_set_reused_)},
            {"texture_binding_image_descriptor_updates", std::to_string(
                last_texture_binding_image_descriptor_update_count_)},
            {"texture_binding_descriptor_sets_allocated", std::to_string(
                last_texture_binding_descriptor_set_allocation_count_)},
            {"texture_binding_set_reuses", std::to_string(
                texture_binding_set_reuse_count_)},
            {"texture_binding_set_rebuilds", std::to_string(
                texture_binding_set_rebuild_count_)},
            {"texture_binding_image_descriptor_updates_total", std::to_string(
                texture_binding_image_descriptor_update_count_)},
            {"texture_binding_descriptor_sets_allocated_total", std::to_string(
                texture_binding_descriptor_set_allocation_count_)},
            {"render_validation_us", std::to_string(
                last_render_validation_us_)},
            {"vertex_transform_us", std::to_string(last_vertex_transform_us_)},
            {"vertex_state_upload_us", std::to_string(
                last_vertex_state_upload_us_)},
            {"raw_vertex_upload_us", std::to_string(
                last_raw_vertex_upload_us_)},
            {"gpu_raw_attribute_draws", std::to_string(
                gpu_raw_attribute_draw_count_)},
            {"gpu_raw_attribute_vertices", std::to_string(
                gpu_raw_attribute_vertex_count_)},
            {"expanded_vertex_bytes_avoided", std::to_string(
                static_cast<uint64_t>(
                    gpu_raw_attribute_vertex_count_)
                    * sizeof(NativeVertex))},
            {"gpu_vertex_program_draws", std::to_string(
                gpu_vertex_program_draw_count_)},
            {"gpu_vertex_program_vertices", std::to_string(
                gpu_vertex_program_vertex_count_)},
            {"cpu_vertex_program_fallback_draws", std::to_string(
                cpu_vertex_program_fallback_draw_count_)},
            {"cpu_vertex_program_fallback_vertices", std::to_string(
                cpu_vertex_program_fallback_vertex_count_)},
            {"vertex_map_us", std::to_string(last_vertex_map_us_)},
            {"vertex_copy_us", std::to_string(last_vertex_copy_us_)},
            {"command_record_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_commands - after_resources).count())},
            {"total_us", std::to_string(std::chrono::duration_cast<std::chrono::microseconds>(after_commands - reload_begin).count())},
        });
}

bool VulkanPresenter::acknowledge_current_presentation() {
    if ((presentation_ack_path_.empty() && !transport_.active())
        || current_manifest_guest_flip_count_ == 0u
        || current_manifest_guest_flip_count_ <= acknowledged_presentation_flip_) {
        return false;
    }
    const uint64_t generation = std::stoull(live_command_generation_);
    if (transport_.active()) {
        transport_.publish_presentation(
            generation, current_manifest_guest_flip_count_);
    } else {
        if (presentation_ack_path_.has_parent_path()) {
            std::filesystem::create_directories(
                presentation_ack_path_.parent_path());
        }
        if (presentation_ack_file_ == INVALID_HANDLE_VALUE) {
            presentation_ack_file_ = CreateFileW(
                presentation_ack_path_.c_str(),
                GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                nullptr,
                OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                nullptr);
        }
        if (presentation_ack_file_ == INVALID_HANDLE_VALUE) {
            throw std::runtime_error(
                "cannot open live presentation acknowledgement");
        }
        std::array<uint8_t, 24> ack{};
        std::memcpy(ack.data(), "B2PRS001", 8);
        std::memcpy(ack.data() + 8, &generation, sizeof(generation));
        std::memcpy(
            ack.data() + 16,
            &current_manifest_guest_flip_count_,
            sizeof(current_manifest_guest_flip_count_));
        LARGE_INTEGER start{};
        DWORD written = 0;
        const BOOL write_ok = SetFilePointerEx(
                presentation_ack_file_, start, nullptr, FILE_BEGIN)
            && WriteFile(
                presentation_ack_file_,
                ack.data(),
                static_cast<DWORD>(ack.size()),
                &written,
                nullptr)
            && SetEndOfFile(presentation_ack_file_);
        if (!write_ok || written != ack.size()) {
            throw std::runtime_error(
                "cannot publish live presentation acknowledgement");
        }
    }
    acknowledged_presentation_flip_ = current_manifest_guest_flip_count_;
    if (!presentation_ack_event_ && !presentation_event_name_.empty()) {
        presentation_ack_event_ = OpenEventW(
            EVENT_MODIFY_STATE,
            FALSE,
            widen(presentation_event_name_).c_str());
    }
    if (presentation_ack_event_) {
        SetEvent(presentation_ack_event_);
    }
    return true;
}

void VulkanPresenter::acknowledge_current_flip_audit(
    const FrameReadback* readback,
    const std::filesystem::path& frame_path,
    const std::string& health_check_reason) {
    if (options_.flip_audit_ack.empty()
        || current_audit_flip_ == 0u
        || current_audit_flip_ <= acknowledged_audit_flip_) {
        return;
    }
    if (options_.flip_audit_ack.has_parent_path()) {
        std::filesystem::create_directories(
            options_.flip_audit_ack.parent_path());
    }
    const HANDLE ack_file = CreateFileW(
        options_.flip_audit_ack.c_str(),
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr,
        CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (ack_file == INVALID_HANDLE_VALUE) {
        throw std::runtime_error(
            "cannot open lossless flip audit acknowledgement");
    }
    std::array<uint8_t, 20> ack{};
    std::memcpy(ack.data(), "B2ACK001", 8);
    const uint64_t generation = std::stoull(current_audit_generation_);
    std::memcpy(ack.data() + 8, &generation, sizeof(generation));
    std::memcpy(
        ack.data() + 16, &current_audit_flip_, sizeof(current_audit_flip_));
    DWORD written = 0;
    const BOOL write_ok = WriteFile(
        ack_file,
        ack.data(),
        static_cast<DWORD>(ack.size()),
        &written,
        nullptr);
    CloseHandle(ack_file);
    if (!write_ok || written != ack.size()) {
        throw std::runtime_error(
            "cannot publish lossless flip audit acknowledgement");
    }
    acknowledged_audit_flip_ = current_audit_flip_;
    const uint32_t command_delta = current_audit_command_count_
        - std::min(current_audit_command_count_, last_audit_command_count_);
    last_audit_command_count_ = current_audit_command_count_;
    last_audit_command_delta_ = command_delta;
    last_audit_flip_value_ = current_audit_flip_value_;
    last_audit_resource_generation_ = live_resource_generation_;
    std::ostringstream fingerprint;
    if (readback != nullptr) {
        fingerprint << std::hex << std::setfill('0') << std::setw(16)
                    << readback->pixel_fingerprint;
    }
    const bool health_checked = readback != nullptr;
    log_.emit(
        "lossless_flip_audited",
        {
            {"flip_index", std::to_string(current_audit_flip_)},
            {"guest_steps", std::to_string(current_audit_guest_steps_)},
            {"command_record_count", std::to_string(current_audit_command_count_)},
            {"host_frame", std::to_string(frame_count_ + 1u)},
            {"health_checked", json_bool(health_checked)},
            {"health_check_reason", json_string(
                health_checked ? health_check_reason : "not_selected")},
            {"readback_captured", json_bool(health_checked)},
            {"pixel_fingerprint", health_checked
                ? json_string(fingerprint.str()) : "null"},
            {"pixel_count", std::to_string(health_checked ? readback->pixel_count : 0u)},
            {"unique_colors", std::to_string(health_checked ? readback->unique_colors : 0u)},
            {"dominant_rgba", std::to_string(health_checked ? readback->dominant_rgba : 0u)},
            {"dominant_count", std::to_string(health_checked ? readback->dominant_count : 0u)},
            {"bright_count", std::to_string(health_checked ? readback->bright_count : 0u)},
            {"dark_count", std::to_string(health_checked ? readback->dark_count : 0u)},
            {"near_solid_frame", json_bool(health_checked && readback->near_solid)},
            {"whiteout_frame", json_bool(health_checked && readback->whiteout)},
            {"low_information_frame", json_bool(health_checked && readback->low_information)},
            {"visual_issue", json_bool(health_checked && readback->visual_issue())},
            {"frame_saved", json_bool(!frame_path.empty())},
            {"frame_path", json_string(frame_path.string())},
            {"ack_transport", json_string("binary")},
        });
    if (options_.flip_audit_max_flips != 0u
        && acknowledged_audit_flip_ >= options_.flip_audit_max_flips) {
        running_ = false;
    }
}

void VulkanPresenter::main_loop() {
    log_.emit("main_loop_enter");
    pump_window_messages();
    write_controller_state();
    auto last_frame = std::chrono::steady_clock::now();
    auto next_frame_time = last_frame + kTargetFrameInterval;
    guest_fps_sampler_.reset(last_frame, current_manifest_guest_flip_count_);
    while (running_ && (options_.max_frames == 0u || frame_count_ < options_.max_frames)) {
        const auto frame_start = std::chrono::steady_clock::now();
        active_frame_fence_wait_us_ = 0u;
        last_window_message_pump_us_ = 0u;
        last_controller_poll_us_ = 0u;
        last_keyboard_latch_us_ = 0u;
        last_reload_probe_us_ = 0u;
        last_reload_dispatch_us_ = 0u;
        last_pre_render_unattributed_us_ = 0u;
        last_draw_fence_wait_us_ = 0u;
        last_reload_fence_wait_us_ = 0u;
        last_gpu_query_us_ = 0u;
        last_fence_reset_us_ = 0u;
        last_acquire_us_ = 0u;
        last_submit_us_ = 0u;
        last_readback_wait_us_ = 0u;
        last_readback_process_us_ = 0u;
        last_present_us_ = 0u;
        last_audit_ack_us_ = 0u;
        last_draw_unattributed_us_ = 0u;

        auto stage_begin = std::chrono::steady_clock::now();
        pump_window_messages();
        last_window_message_pump_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now() - stage_begin).count());
        stage_begin = std::chrono::steady_clock::now();
        if (!running_) {
            break;
        }
        const auto reload_dispatch_begin = std::chrono::steady_clock::now();
        reload_live_render_work();
        last_reload_dispatch_us_ = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                std::chrono::steady_clock::now()
                - reload_dispatch_begin).count());
        if (options_.inject_input && !input_injected_) {
            const uint32_t injected_events = handle_platform_result(
                platform_.inject_confirm(current_manifest_guest_flip_count_));
            input_injected_ = true;
            log_.emit("input_injected", {{"key", json_string("space")}});
            log_.emit(
                "input_injection_processed",
                {
                    {"messages", std::to_string(injected_events)},
                    {"input_events", std::to_string(input_events_)},
                    {"before_frame", std::to_string(frame_count_ + 1u)},
                });
            if (!running_) {
                break;
            }
        }
        const auto cpu_render_begin = std::chrono::steady_clock::now();
        const uint64_t pre_render_us = static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::microseconds>(
                cpu_render_begin - frame_start).count());
        const uint64_t attributed_pre_render_us =
            last_window_message_pump_us_
            + last_controller_poll_us_
            + last_keyboard_latch_us_
            + last_reload_dispatch_us_;
        last_pre_render_unattributed_us_ = pre_render_us
            > attributed_pre_render_us
            ? pre_render_us - attributed_pre_render_us
            : 0u;
        draw_frame();
        last_cpu_render_us_ = std::chrono::duration_cast<
            std::chrono::microseconds>(
                std::chrono::steady_clock::now() - cpu_render_begin).count();
        const uint64_t attributed_draw_us =
            last_draw_fence_wait_us_
            + last_gpu_query_us_
            + last_fence_reset_us_
            + last_acquire_us_
            + last_submit_us_
            + last_readback_wait_us_
            + last_readback_process_us_
            + last_present_us_
            + last_audit_ack_us_;
        last_draw_unattributed_us_ = last_cpu_render_us_
            > attributed_draw_us
            ? last_cpu_render_us_ - attributed_draw_us
            : 0u;
        const uint64_t draw_frame_fence_wait_us =
            last_draw_fence_wait_us_ + last_readback_wait_us_;
        last_reload_fence_wait_us_ = active_frame_fence_wait_us_
            > draw_frame_fence_wait_us
            ? active_frame_fence_wait_us_ - draw_frame_fence_wait_us
            : 0u;
        ++frame_count_;
        const auto after_draw = std::chrono::steady_clock::now();
        const auto draw_us =
            std::chrono::duration_cast<std::chrono::microseconds>(after_draw - frame_start).count();
        const auto sleep_us = std::max<int64_t>(
            0,
            std::chrono::duration_cast<std::chrono::microseconds>(next_frame_time - after_draw).count());
        if (sleep_us > 0) {
            wait_for_frame_deadline(next_frame_time);
        }
        const auto now = std::chrono::steady_clock::now();
        const auto elapsed_us =
            std::chrono::duration_cast<std::chrono::microseconds>(now - last_frame).count();
        last_frame_elapsed_us_ = elapsed_us;
        last_fence_wait_us_ = active_frame_fence_wait_us_;
        last_frame = now;
        next_frame_time += kTargetFrameInterval;
        if (next_frame_time <= now) {
            next_frame_time = now + kTargetFrameInterval;
        }
        update_guest_fps_sample(now);
        log_.emit(
            "frame_presented",
            {
                {"frame", std::to_string(frame_count_)},
                {"elapsed_us", std::to_string(elapsed_us)},
                {"draw_us", std::to_string(draw_us)},
                {"cpu_render_us", std::to_string(last_cpu_render_us_)},
                {"window_message_pump_us", std::to_string(
                    last_window_message_pump_us_)},
                {"controller_poll_us", std::to_string(
                    last_controller_poll_us_)},
                {"keyboard_latch_us", std::to_string(
                    last_keyboard_latch_us_)},
                {"reload_probe_us", std::to_string(
                    last_reload_probe_us_)},
                {"reload_dispatch_us", std::to_string(
                    last_reload_dispatch_us_)},
                {"pre_render_unattributed_us", std::to_string(
                    last_pre_render_unattributed_us_)},
                {"reload_fence_wait_us", std::to_string(
                    last_reload_fence_wait_us_)},
                {"draw_fence_wait_us", std::to_string(
                    last_draw_fence_wait_us_)},
                {"gpu_query_us", std::to_string(last_gpu_query_us_)},
                {"fence_reset_us", std::to_string(last_fence_reset_us_)},
                {"acquire_us", std::to_string(last_acquire_us_)},
                {"submit_us", std::to_string(last_submit_us_)},
                {"readback_wait_us", std::to_string(
                    last_readback_wait_us_)},
                {"readback_process_us", std::to_string(
                    last_readback_process_us_)},
                {"present_us", std::to_string(last_present_us_)},
                {"audit_ack_us", std::to_string(last_audit_ack_us_)},
                {"draw_unattributed_us", std::to_string(
                    last_draw_unattributed_us_)},
                {"target_frame_us", std::to_string(kTargetFrameUs)},
                {"pacing_sleep_us", std::to_string(sleep_us)},
                {"elapsed_ms", std::to_string(elapsed_us / 1000)},
                {"draw_ms", std::to_string(draw_us / 1000)},
                {"target_frame_ms", "16.666667"},
                {"pacing_sleep_ms", std::to_string(sleep_us / 1000)},
                {"translated_commands", std::to_string(translated_command_count_)},
                {"source_d3d_commands", std::to_string(recovered_d3d_command_count_)},
                {"push_buffer_words", std::to_string(interpreted_push_buffer_word_count_)},
                {"method_packets", std::to_string(interpreted_method_packet_count_)},
                {"interpreted_methods", std::to_string(interpreted_method_count_)},
                {"zero_count_method_words", std::to_string(interpreted_zero_count_method_word_count_)},
    });
}

    log_.emit(
        "main_loop_exit",
        {
            {"frames", std::to_string(frame_count_)},
            {"input_events", std::to_string(input_events_)},
            {"closed_by_user", json_bool(closed_by_user_)},
        });
}

void VulkanPresenter::set_fps_counter_title(std::optional<double> fps) {
    std::ostringstream title;
    title << narrow(options_.title) << " | Game FPS: ";
    if (fps.has_value()) {
        title << std::fixed << std::setprecision(1) << *fps;
    } else {
        title << "--";
    }
    platform_.set_window_title(title.str());
}

void VulkanPresenter::toggle_fps_counter() {
    fps_counter_enabled_ = !fps_counter_enabled_;
    if (fps_counter_enabled_) {
        set_fps_counter_title(
            guest_fps_sample_valid_
                ? std::optional<double>(last_guest_fps_)
                : std::nullopt);
    } else {
        platform_.set_window_title(narrow(options_.title));
    }
    log_.emit(
        "fps_counter_toggled",
        {
            {"enabled", json_bool(fps_counter_enabled_)},
            {"source", json_string("completed_guest_flips")},
            {"manifest_guest_flip_count", std::to_string(
                current_manifest_guest_flip_count_)},
            {"sample_valid", json_bool(guest_fps_sample_valid_)},
        });
}

void VulkanPresenter::update_guest_fps_sample(
    std::chrono::steady_clock::time_point now) {
    const auto sample = guest_fps_sampler_.observe(
        now,
        current_manifest_guest_flip_count_);
    if (!sample.has_value()) {
        return;
    }
    last_guest_fps_ = sample->fps;
    last_guest_fps_sample_completed_flips_ = sample->completed_flips;
    last_guest_fps_sample_seconds_ = sample->seconds;
    guest_fps_sample_valid_ = true;
    if (fps_counter_enabled_) {
        set_fps_counter_title(last_guest_fps_);
    }
}

void VulkanPresenter::sleep_until_frame_deadline(
    std::chrono::steady_clock::time_point deadline) {
    const auto now = std::chrono::steady_clock::now();
    if (deadline <= now) {
        return;
    }
    if (!frame_pacing_timer_) {
        frame_pacing_timer_ = CreateWaitableTimerExW(
            nullptr,
            nullptr,
            kHighResolutionWaitableTimerFlag,
            TIMER_ALL_ACCESS);
    }
    if (frame_pacing_timer_) {
        const int64_t remaining_ns = std::chrono::duration_cast<
            std::chrono::nanoseconds>(deadline - now).count();
        LARGE_INTEGER due_time{};
        due_time.QuadPart = -std::max<int64_t>(
            1,
            (remaining_ns + 99) / 100);
        if (SetWaitableTimerEx(
                frame_pacing_timer_,
                &due_time,
                0,
                nullptr,
                nullptr,
                nullptr,
                0)) {
            WaitForSingleObject(frame_pacing_timer_, INFINITE);
            return;
        }
    }
    std::this_thread::sleep_until(deadline);
}

void VulkanPresenter::wait_for_frame_deadline(
    std::chrono::steady_clock::time_point deadline) {
    const auto now = std::chrono::steady_clock::now();
    if (deadline <= now
        || !publication_event_
        || options_.presentation_pipeline_depth != 2u) {
        sleep_until_frame_deadline(deadline);
        return;
    }
    if (!frame_pacing_timer_) {
        frame_pacing_timer_ = CreateWaitableTimerExW(
            nullptr,
            nullptr,
            kHighResolutionWaitableTimerFlag,
            TIMER_ALL_ACCESS);
    }
    if (!frame_pacing_timer_) {
        sleep_until_frame_deadline(deadline);
        return;
    }
    const int64_t remaining_ns = std::chrono::duration_cast<
        std::chrono::nanoseconds>(deadline - now).count();
    LARGE_INTEGER due_time{};
    due_time.QuadPart = -std::max<int64_t>(
        1,
        (remaining_ns + 99) / 100);
    if (!SetWaitableTimerEx(
            frame_pacing_timer_,
            &due_time,
            0,
            nullptr,
            nullptr,
            nullptr,
            0)) {
        sleep_until_frame_deadline(deadline);
        return;
    }
    const std::array<HANDLE, 2> waits{
        publication_event_,
        frame_pacing_timer_,
    };
    const DWORD result = WaitForMultipleObjects(
        static_cast<DWORD>(waits.size()),
        waits.data(),
        FALSE,
        INFINITE);
    if (result == WAIT_OBJECT_0) {
        // The native producer has its own 60 Hz deadline pacer. Consuming one
        // completed publication while this frame is idle removes the
        // one-missed-probe penalty without allowing a second guest flip in
        // the same displayed-frame interval.
        reload_live_render_work(true);
        sleep_until_frame_deadline(deadline);
    } else if (result != WAIT_OBJECT_0 + 1u) {
        sleep_until_frame_deadline(deadline);
    }
}

void VulkanPresenter::queue_hotkey_screenshot() {
    if (options_.hotkey_screenshot_directory.empty()) {
        return;
    }
    hotkey_screenshot_pending_ = true;
    pending_hotkey_screenshot_path_ = next_hotkey_screenshot_path();
    pending_hotkey_render_capture_manifest_ = retain_hotkey_render_capture(
        pending_hotkey_screenshot_path_);
    log_.emit(
        "hotkey_screenshot_queued",
        {
            {"frame", std::to_string(frame_count_ + 1u)},
            {"directory", json_string(
                options_.hotkey_screenshot_directory.string())},
            {"output", json_string(
                pending_hotkey_screenshot_path_.string())},
            {"render_capture_manifest",
                pending_hotkey_render_capture_manifest_.empty()
                ? "null"
                : json_string(
                    pending_hotkey_render_capture_manifest_.string())},
        });
}

void VulkanPresenter::emit_controller_event(
    const char* name,
    const ControllerMetadata& controller) {
    log_.emit(
        name,
        {
            {"backend", json_string("sdl3")},
            {"name", json_string(controller.name)},
            {"type", json_string(controller.type)},
            {"guid", json_string(controller.guid)},
            {"vendor_id", std::to_string(controller.vendor_id)},
            {"product_id", std::to_string(controller.product_id)},
            {"product_version", std::to_string(
                controller.product_version)},
            {"mapping", json_string(controller.mapping)},
        });
}

uint32_t VulkanPresenter::handle_platform_result(PlatformPollResult result) {
    for (const auto& event : result.events) {
        if (event.type == PlatformEventType::Key) {
            ++input_events_;
            log_.emit(
                "input_event",
                {
                    {"message", json_string(
                        event.key_down ? "keydown" : "keyup")},
                    {"keycode", std::to_string(event.keycode)},
                    {"frame", std::to_string(frame_count_ + 1u)},
                    {"backend", json_string("sdl3")},
                });
        } else if (event.type == PlatformEventType::ControllerConnected) {
            emit_controller_event("host_controller_connected", event.controller);
        } else if (event.type == PlatformEventType::ControllerDisconnected) {
            emit_controller_event("host_controller_disconnected", event.controller);
        } else if (event.type == PlatformEventType::ControllerDiscoveryFailed) {
            log_.emit(
                "controller_discovery_failed",
                {
                    {"backend", json_string("sdl3")},
                    {"error", json_string(event.error)},
                });
        }
    }
    if (result.close_requested) {
        closed_by_user_ = true;
        running_ = false;
    }
    if (result.toggle_fps_counter) {
        toggle_fps_counter();
    }
    if (result.toggle_hot_path_profile) {
        const uint32_t state = transport_.toggle_hot_path_profile();
        const char* state_name = "disabled";
        if (state == b2r::live_transport::kLiveHotPathProfileActive) {
            state_name = "active";
            platform_.set_window_title(
                narrow(options_.title) + " | Hot-path capture ACTIVE (F10 to stop)");
        } else if (state == b2r::live_transport::kLiveHotPathProfileComplete) {
            state_name = "complete";
            platform_.set_window_title(
                narrow(options_.title) + " | Hot-path capture COMPLETE");
        }
        log_.emit(
            "hot_path_profile_capture",
            {
                {"state", json_string(state_name)},
                {"frame", std::to_string(frame_count_ + 1u)},
                {"guest_flip_count", std::to_string(
                    current_manifest_guest_flip_count_)},
            });
    }
    if (result.write_metrics_report
        && !options_.metrics_report_directory.empty()) {
        write_metrics_report();
    }
    if (result.capture_screenshot) {
        queue_hotkey_screenshot();
    }
    if (result.controller_state_changed) {
        write_controller_state();
    }
    return static_cast<uint32_t>(result.events.size());
}

uint32_t VulkanPresenter::pump_window_messages() {
    return handle_platform_result(
        platform_.poll(current_manifest_guest_flip_count_));
}

void VulkanPresenter::initialize_live_control_transport() {
    if (options_.live_control_transport_name.empty()) {
        return;
    }
    const auto info = transport_.open(options_.live_control_transport_name);
    if (transport_.hot_path_profile_state()
        == b2r::live_transport::kLiveHotPathProfileArmed) {
        platform_.set_window_title(
            narrow(options_.title) + " | Hot-path capture ARMED (F10 to start)");
        log_.emit(
            "hot_path_profile_capture",
            {
                {"state", json_string("armed")},
                {"frame", std::to_string(frame_count_)},
                {"guest_flip_count", std::to_string(
                    current_manifest_guest_flip_count_)},
            });
    }
    log_.emit(
        "live_control_transport_opened",
        {
            {"schema_version", std::to_string(info.schema_version)},
            {"mapping_size", std::to_string(info.control_mapping_size)},
            {"command_capacity", std::to_string(info.command_capacity)},
            {"resource_slot_capacity", std::to_string(
                info.resource_slot_capacity)},
        });
}

void VulkanPresenter::publish_live_controller_state() {
    transport_.publish_controller(platform_.controller_state());
}

void VulkanPresenter::write_controller_state() {
    if (options_.controller_state_json.empty() && !transport_.active()) {
        return;
    }
    const ControllerState& controller = platform_.controller_state();
    const ControllerMetadata& metadata = platform_.controller_metadata();
    const auto publish_begin = std::chrono::steady_clock::now();
    uint32_t replace_retry_count = 0u;
    if (transport_.active()) {
        publish_live_controller_state();
    } else {
        if (options_.controller_state_json.has_parent_path()) {
            std::filesystem::create_directories(options_.controller_state_json.parent_path());
        }
        std::filesystem::path temporary = options_.controller_state_json;
        temporary += L".tmp";
        {
            std::ofstream output(temporary, std::ios::trunc);
            if (!output) {
                throw std::runtime_error("cannot write controller state JSON: " + temporary.string());
            }
            output << "{\"ports\":{\"0\":{\"connected\":true,\"buttons\":"
                   << controller.buttons
                   << ",\"left_trigger\":"
                   << static_cast<uint32_t>(controller.left_trigger)
                   << ",\"right_trigger\":"
                   << static_cast<uint32_t>(controller.right_trigger)
                   << ",\"thumb_lx\":" << controller.thumb_lx
                   << ",\"thumb_ly\":" << controller.thumb_ly
                   << ",\"thumb_rx\":" << controller.thumb_rx
                   << ",\"thumb_ry\":" << controller.thumb_ry
                   << "}},\"host_controller\":{\"connected\":"
                   << json_bool(controller.connected)
                   << ",\"backend\":\"sdl3\",\"name\":"
                   << json_string(metadata.name)
                   << ",\"type\":" << json_string(metadata.type)
                   << ",\"guid\":" << json_string(metadata.guid)
                   << ",\"vendor_id\":" << metadata.vendor_id
                   << ",\"product_id\":" << metadata.product_id
                   << "}}\n";
        }
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::seconds(10);
        while (!MoveFileExW(
            temporary.c_str(), options_.controller_state_json.c_str(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            if (std::chrono::steady_clock::now() >= deadline) {
                throw std::runtime_error("cannot publish controller state JSON");
            }
            ++replace_retry_count;
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
    }
    const int64_t publish_us = std::chrono::duration_cast<
        std::chrono::microseconds>(
            std::chrono::steady_clock::now() - publish_begin).count();
    ++controller_publish_count_;
    log_.emit(
        "controller_state_published",
        {
            {"publish", std::to_string(controller_publish_count_)},
            {"buttons", std::to_string(controller.buttons)},
            {"physical_buttons", std::to_string(
                controller.keyboard_buttons | controller.gamepad_buttons)},
            {"keyboard_buttons", std::to_string(
                controller.keyboard_buttons)},
            {"gamepad_buttons", std::to_string(controller.gamepad_buttons)},
            {"latched_buttons", std::to_string(controller.latched_buttons)},
            {"controller_backend", json_string("sdl3")},
            {"host_controller_connected", json_bool(
                controller.connected)},
            {"left_trigger", std::to_string(
                controller.left_trigger)},
            {"right_trigger", std::to_string(
                controller.right_trigger)},
            {"thumb_lx", std::to_string(controller.thumb_lx)},
            {"thumb_ly", std::to_string(controller.thumb_ly)},
            {"thumb_rx", std::to_string(controller.thumb_rx)},
            {"thumb_ry", std::to_string(controller.thumb_ry)},
            {"publish_us", std::to_string(publish_us)},
            {"replace_retry_count", std::to_string(replace_retry_count)},
            {"transport", json_string(
                transport_.active() ? "shared_memory_v1" : "json_file")},
            {
                "manifest_guest_flip_count",
                std::to_string(current_manifest_guest_flip_count_),
            },
        });
}

}  // namespace b2r::host::vulkan_detail

namespace {

using namespace b2r::host::vulkan_detail;

int run_presenter(const std::vector<std::wstring>& args) {
    try {
        VulkanPresenter app(b2r::host::parse_presenter_options(args));
        return app.run();
    } catch (const std::exception& exc) {
        std::cerr << "{\"event\":\"fatal\",\"error\":" << json_string(exc.what()) << "}\n";
        return 1;
    }
}

}  // namespace

int b2r::host::run_vulkan_presenter(
    const std::vector<std::wstring>& args) {
    return run_presenter(args);
}
