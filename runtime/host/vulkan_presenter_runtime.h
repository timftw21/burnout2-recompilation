#pragma once

#include "vulkan_presenter_internal.h"

namespace b2r::host::vulkan_detail {

class VulkanPresenter {
public:
    explicit VulkanPresenter(Options options);
    ~VulkanPresenter();

    int run();

private:
    std::optional<std::string> read_live_render_manifest();

    bool load_initial_recovered_render_work();

    bool update_flip_audit_manifest_state(const std::string& manifest_text);

    std::string flip_audit_health_check_reason() const;

    bool read_live_command_stream_delta(
        const std::filesystem::path& path,
        uint64_t snapshot_base_record_count,
        uint64_t first_record_count,
        uint64_t required_record_count);

    bool read_live_command_span_stream_delta(
        const std::filesystem::path& path,
        uint64_t snapshot_base_byte_count,
        uint64_t first_byte_count,
        uint64_t required_byte_count,
        uint64_t expected_logical_write_count);

    bool read_live_command_span_transport_delta(
        uint64_t first_byte_count,
        uint64_t required_byte_count,
        uint64_t expected_logical_write_count);

    bool read_live_resource_transport(
        uint32_t slot,
        uint64_t expected_size,
        const std::string& expected_generation,
        std::vector<uint8_t>& payload) const;

    void create_instance();

    void initialize_platform();

    void list_adapters();

    void create_window();

    void create_surface();

    void pick_physical_device();

    std::optional<QueueFamilySelection> find_queue_family(VkPhysicalDevice device) const;

    bool device_supports_swapchain(VkPhysicalDevice device) const;

    SwapchainSupport query_swapchain_support(VkPhysicalDevice device) const;

    void create_logical_device();

    void create_pipeline_cache();

    void persist_pipeline_cache();

    void create_swapchain();

    VkSurfaceFormatKHR choose_surface_format(const std::vector<VkSurfaceFormatKHR>& formats) const;

    VkExtent2D choose_extent(const VkSurfaceCapabilitiesKHR& capabilities) const;

    void create_image_views();

    void destroy_depth_attachment(
        VkImage& image,
        VkDeviceMemory& memory,
        VkImageView& view);

    void create_depth_attachment(
        uint32_t width,
        uint32_t height,
        VkImage& image,
        VkDeviceMemory& memory,
        VkImageView& view);

    void create_depth_resources();

    void create_render_pass();

    void create_framebuffers();

    void create_command_pool();

    void create_gpu_timing_resources();

    uint32_t find_memory_type(uint32_t type_bits, VkMemoryPropertyFlags properties) const;

    void create_readback_buffer();

    bool load_recovered_render_work(
        const std::function<void()>& source_resident_callback = {});

    void destroy_native_render_resources(
        bool preserve_textures = false,
        bool preserve_vertex_buffer = false,
        bool preserve_offscreen_render_targets = false);

    void reload_live_render_work(bool publication_signaled = false);

    std::vector<uint32_t> read_spirv(const std::filesystem::path& path) const;

    VkShaderModule create_shader_module(const std::filesystem::path& path) const;

    bool ensure_texture_conversion_pipeline();

    void create_native_graphics_pipeline();

    void create_buffer(
        VkDeviceSize size,
        VkBufferUsageFlags usage,
        VkMemoryPropertyFlags properties,
        VkBuffer& buffer,
        VkDeviceMemory& memory);

    void refresh_raw_vertex_buffers();

    void refresh_fragment_states();

    void refresh_vertex_program_states();

    std::vector<uint8_t> decompress_dxt1(const RecoveredTextureResource& resource) const;

    std::vector<uint8_t> decompress_dxt5(const RecoveredTextureResource& resource) const;

    std::vector<std::vector<uint8_t>> decompress_dxt_mip_chain(
        const RecoveredTextureResource& resource) const;

    std::vector<std::vector<uint8_t>> build_cpu_dxt_conversion_mips(
        const RecoveredTextureResource& resource) const;

    std::vector<uint8_t> convert_bgra8_texture(
        const RecoveredTextureResource& resource,
        bool opaque_alpha,
        bool swizzled) const;

    std::vector<uint8_t> convert_r5g6b5_texture(
        const RecoveredTextureResource& resource) const;

    void submit_immediate(const std::function<void(VkCommandBuffer)>& record);

    HostTexture create_host_texture(
        uint32_t guest_address,
        uint32_t width,
        uint32_t height,
        std::string format,
        std::string content_hash,
        const std::vector<uint8_t>& rgba,
        VkFormat image_format = VK_FORMAT_R8G8B8A8_UNORM,
        bool render_target_feedback = false,
        const std::vector<std::vector<uint8_t>>& recovered_mips = {},
        bool cubemap = false);

    bool build_gpu_texture_conversion_job(
        const RecoveredTextureResource& resource,
        size_t host_texture_index,
        GpuTextureConversionJob& job) const;

    bool execute_gpu_texture_conversion_batch(
        std::vector<GpuTextureConversionJob>& jobs);

    std::optional<HostTexture> create_cpu_converted_host_texture(
        const RecoveredTextureResource& resource,
        const std::string& content_identity,
        bool cubemap);

    static void clear_moved_texture_handles(HostTexture& texture);

    void destroy_host_texture(HostTexture& texture);

    static std::string texture_content_identity(
        const RecoveredTextureResource& resource);

    std::pair<uint32_t, uint32_t> draw_surface_extent(
        const NativeDraw& draw) const;

    uint32_t select_presented_surface_color_offset() const;

    bool draw_surface_clip_is_subsurface_viewport(
        const NativeDraw& draw) const;

    bool draw_targets_presented_surface(const NativeDraw& draw) const;

    VkRect2D draw_scissor(
        const NativeDraw& draw,
        VkExtent2D target_extent) const;

    static bool draw_has_supported_host_transform(const NativeDraw& draw);

    const std::vector<RenderTargetFeedbackSpec>&
    presented_render_target_feedback_specs() const;

    static bool render_target_feedback_texture_matches_spec(
        const HostTexture& texture,
        const RenderTargetFeedbackSpec& spec);

    void cache_render_target_feedback_texture(HostTexture& texture);

    void trim_render_target_feedback_image_cache();

    bool render_target_feedback_refresh_required() const;

    HostTexture create_render_target_feedback_texture(
        const RenderTargetFeedbackSpec& spec);

    void destroy_offscreen_render_targets();

    bool offscreen_render_targets_match_presented_specs() const;

    void create_offscreen_render_targets();

    void destroy_host_texture_bindings();

    size_t host_texture_index_for_draw(const NativeDraw& draw) const;

    size_t host_texture_index_for_stage(
        const NativeDraw& draw,
        uint32_t stage) const;

    std::vector<HostTextureBindingSpec>
    required_host_texture_bindings() const;

    void refresh_host_texture_bindings();

    void refresh_host_textures(bool retain_unlisted_resources = false);

    std::vector<NativeVertex> prepare_presented_vertices(
        bool collect_diagnostics = true);

    void emit_presented_vertex_transform_diagnostics(bool emit_draw_details);

    void emit_presented_render_state_diagnostics();

    void create_native_render_resources(
        bool create_textures = true,
        bool reuse_offscreen_render_targets = false);

    VkDescriptorSet descriptor_for_draw(const NativeDraw& draw) const;

    const HostTexture* presented_render_target_feedback_texture() const;

    uint32_t presented_surface_color_clear_count() const;

    bool record_render_target_feedback(
        VkCommandBuffer command_buffer,
        VkImage swapchain_image) const;

    void record_native_draws_for_target(
        VkCommandBuffer command_buffer,
        VkExtent2D target_extent,
        std::optional<uint32_t> producer_address) const;

    void record_native_draws(VkCommandBuffer command_buffer) const;

    uint32_t record_offscreen_render_targets(
        VkCommandBuffer command_buffer) const;

    struct FrontendTextRaster {
        uint32_t width = 0u;
        uint32_t height = 0u;
        std::vector<uint8_t> alpha;
    };

    FrontendTextRaster rasterize_frontend_text(
        const std::string& text,
        float layout_scale) const;

    uint32_t append_frontend_text_vertices(
        std::vector<NativeVertex>& vertices,
        const std::string& text);

    void record_frontend_text(VkCommandBuffer command_buffer) const;

    void create_command_buffers();

    void create_sync_objects();

    bool readback_enabled() const;

    std::filesystem::path current_flip_audit_frame_path() const;

    bool acknowledge_current_presentation();

    void acknowledge_current_flip_audit(
        const FrameReadback* readback,
        const std::filesystem::path& frame_path,
        const std::string& health_check_reason);

    void main_loop();

    void set_fps_counter_title(std::optional<double> fps);

    void toggle_fps_counter();

    void update_guest_fps_sample(
        std::chrono::steady_clock::time_point now);

    void sleep_until_frame_deadline(
        std::chrono::steady_clock::time_point deadline);

    void wait_for_frame_deadline(
        std::chrono::steady_clock::time_point deadline);

    void queue_hotkey_screenshot();

    void emit_controller_event(
        const char* name,
        const ControllerMetadata& controller);

    uint32_t handle_platform_result(PlatformPollResult result);

    uint32_t pump_window_messages();

    uint64_t wait_for_in_flight_fence(const char* operation);

    void update_gpu_frame_time();

    void draw_frame();

    static void write_u16(std::ofstream& output, uint16_t value);

    static void write_u32(std::ofstream& output, uint32_t value);

    std::filesystem::path retain_hotkey_render_capture(
        const std::filesystem::path& screenshot_path);

    PresenterMetricsSnapshot collect_metrics_snapshot() const;

    void write_metrics_report();
    std::filesystem::path next_hotkey_screenshot_path();

    FrameReadback read_frame();

    void write_screenshot(
        const std::filesystem::path& screenshot_path,
        const FrameReadback& readback,
        const char* trigger);

    void capture_screenshot(
        const std::filesystem::path& screenshot_path,
        const char* trigger);

    void initialize_live_control_transport();
    void publish_live_controller_state();
    void consume_live_audio();

    void write_controller_state();

    void write_command_work_cache_trace();

    void cleanup();

    Options options_;
    DebugLog log_;
    std::ofstream command_work_cache_trace_;
    uint64_t command_work_cache_trace_reload_count_ = 0u;
    PresenterMetricsReporter metrics_reporter_;
    LivePresenterTransport transport_;
    SdlPlatform platform_;
    std::vector<uint8_t> live_audio_payload_;
    uint64_t live_audio_buffer_count_ = 0u;
    uint64_t live_audio_byte_count_ = 0u;
    bool running_ = true;
    bool closed_by_user_ = false;
    bool input_injected_ = false;
    uint32_t frame_count_ = 0;
    uint32_t input_events_ = 0;
    HANDLE frame_pacing_timer_ = nullptr;
    uint32_t controller_publish_count_ = 0;
    uint32_t live_render_reload_count_ = 0;
    uint32_t live_render_skipped_reload_count_ = 0;
    int64_t last_command_load_us_ = 0;
    int64_t last_manifest_read_us_ = 0;
    int64_t last_manifest_parse_us_ = 0;
    int64_t last_source_load_us_ = 0;
    int64_t last_command_file_open_us_ = 0;
    int64_t last_command_file_read_us_ = 0;
    int64_t last_command_record_validation_us_ = 0;
    uint64_t last_command_transport_provenance_us_ = 0u;
    uint64_t last_command_transport_resize_us_ = 0u;
    uint64_t last_command_transport_copy_us_ = 0u;
    bool last_command_file_reused_ = false;
    int64_t last_interpret_us_ = 0;
    int64_t last_method_interpret_us_ = 0;
    int64_t last_indexed_materialize_us_ = 0;
    uint64_t last_command_work_cache_us_ = 0u;
    int64_t last_render_validation_us_ = 0;
    uint64_t last_resource_snapshot_reused_count_ = 0u;
    uint64_t last_resource_snapshot_reused_bytes_ = 0u;
    bool last_presented_diagnostics_sampled_ = false;
    bool last_offscreen_render_targets_reused_ = false;
    size_t last_interpreted_command_delta_ = 0;
    size_t last_native_command_read_count_ = 0;
    size_t last_native_command_span_count_ = 0;
    size_t last_command_read_bytes_ = 0;
    uint64_t native_command_read_count_ = 0;
    uint64_t native_command_span_read_count_ = 0;
    std::vector<CommandSpanDescriptor> live_command_span_descriptors_;
    CommandWorkCache command_work_cache_;
    uint32_t last_live_command_mmio_count_ = 0;
    bool last_resource_generation_changed_ = false;
    bool last_command_cursor_reset_ = false;
    uint64_t render_work_generation_ = 0;
    mutable uint64_t feedback_spec_cache_generation_ =
        std::numeric_limits<uint64_t>::max();
    mutable std::vector<RenderTargetFeedbackSpec> feedback_spec_cache_;
    mutable uint64_t feedback_spec_cache_hit_count_ = 0;
    mutable uint64_t feedback_spec_cache_build_count_ = 0;
    mutable int64_t last_feedback_spec_build_us_ = 0;
    uint64_t counted_command_render_work_generation_ =
        std::numeric_limits<uint64_t>::max();
    uint64_t presented_diagnostics_generation_ = 0;
    bool presented_diagnostics_valid_ = false;
    uint64_t current_manifest_guest_flip_count_ = 0;
    uint64_t current_manifest_guest_steps_ = 0;
    uint64_t current_manifest_guest_compiled_blocks_ = 0;
    uint64_t current_manifest_guest_invalidations_ = 0;
    uint64_t current_manifest_presentable_command_count_ = 0;
    uint64_t current_manifest_command_base_count_ = 0;
    bool current_manifest_bulk_span_commands_ = false;
    bool current_manifest_shared_command_transport_ = false;
    uint64_t current_manifest_presentable_command_byte_count_ = 0;
    uint64_t current_manifest_command_base_byte_count_ = 0;
    std::filesystem::file_time_type live_render_write_time_{};
    std::filesystem::file_time_type live_resource_write_time_{};
    std::string live_resource_generation_;
    std::filesystem::path live_resource_source_;
    std::string live_command_generation_;
    std::string current_live_manifest_text_;
    std::ifstream live_command_file_;
    std::filesystem::path live_command_file_path_;
    std::vector<uint8_t> live_command_delta_bytes_;
    std::vector<uint8_t> live_command_capture_bytes_;
    uint64_t live_command_capture_base_byte_count_ = 0u;
    std::vector<uint8_t> current_live_resource_snapshot_bytes_;
    std::filesystem::path current_live_command_snapshot_path_;
    std::filesystem::path current_live_resource_snapshot_path_;
    std::filesystem::path presentation_ack_path_;
    HANDLE presentation_ack_file_ = INVALID_HANDLE_VALUE;
    std::string presentation_event_name_;
    HANDLE presentation_ack_event_ = nullptr;
    std::string publication_event_name_;
    HANDLE publication_event_ = nullptr;
    bool publication_retry_pending_ = false;
    uint64_t acknowledged_presentation_flip_ = 0;
    uint32_t recovered_d3d_command_count_ = 0;
    size_t counted_recovered_command_count_ = 0;
    size_t interpreted_source_command_count_ = 0;
    uint64_t interpreted_source_command_byte_count_ = 0;
    bool continuation_analysis_bootstrap_ = false;
    uint32_t recovered_d3d_mmio_count_ = 0;
    uint32_t recovered_d3d_push_buffer_count_ = 0;
    uint32_t interpreted_push_buffer_word_count_ = 0;
    uint32_t interpreted_method_packet_count_ = 0;
    uint32_t interpreted_method_count_ = 0;
    uint32_t interpreted_zero_count_method_word_count_ = 0;
    uint32_t translated_command_count_ = 0;
    uint32_t frontend_text_rectangle_count_ = 0;
    uint32_t frontend_text_first_vertex_ = 0;
    uint32_t frontend_text_vertex_count_ = 0;
    uint32_t frontend_text_fragment_state_index_ =
        std::numeric_limits<uint32_t>::max();
    std::string recovered_frontend_text_;
    RecoveredD3DStreamSource recovered_source_;
    InterpretedD3DStream interpreted_stream_;
    GpuRawVertexResourceCache gpu_raw_vertex_resource_cache_;
    bool presented_half_quad_recovered_ = false;
    bool presented_overscan_height_recovered_ = false;
    uint32_t presented_vertex_program_transformed_count_ = 0;
    uint32_t presented_linear_texture_normalized_vertex_count_ = 0;
    PresentedVertexTransformDiagnostics presented_vertex_transform_diagnostics_{};
    std::vector<PresentedDrawTransformDiagnostics>
        presented_draw_transform_diagnostics_;
    bool screenshot_captured_ = false;
    bool hotkey_screenshot_pending_ = false;
    uint32_t hotkey_screenshot_count_ = 0;
    bool fps_counter_enabled_ = false;
    bool guest_fps_sample_valid_ = false;
    uint64_t last_guest_fps_sample_completed_flips_ = 0u;
    double last_guest_fps_sample_seconds_ = 0.0;
    double last_guest_fps_ = 0.0;
    CompletedFlipFpsSampler guest_fps_sampler_{};
    std::filesystem::path pending_hotkey_screenshot_path_;
    std::filesystem::path pending_hotkey_render_capture_manifest_;
    uint32_t unsupported_texture_resource_count_ = 0;
    uint32_t current_audit_flip_ = 0;
    uint32_t current_audit_guest_steps_ = 0;
    uint32_t current_audit_flip_value_ = 0;
    uint32_t current_audit_command_count_ = 0;
    uint32_t acknowledged_audit_flip_ = 0;
    uint32_t last_audit_command_count_ = 0;
    uint32_t last_audit_command_delta_ = 0;
    uint32_t last_audit_flip_value_ = 0;
    bool current_work_has_readback_ = false;
    std::string current_audit_generation_;
    std::string current_audit_health_reason_;
    std::string last_audit_resource_generation_;

    VkInstance instance_ = VK_NULL_HANDLE;
    VkSurfaceKHR surface_ = VK_NULL_HANDLE;
    VkPhysicalDevice physical_device_ = VK_NULL_HANDLE;
    QueueFamilySelection queue_family_{};
    VkDevice device_ = VK_NULL_HANDLE;
    VkQueue graphics_queue_ = VK_NULL_HANDLE;
    VkPipelineCache pipeline_cache_ = VK_NULL_HANDLE;
    size_t pipeline_cache_loaded_bytes_ = 0u;
    size_t pipeline_cache_saved_bytes_ = 0u;
    bool pipeline_cache_rejected_ = false;
    VkSwapchainKHR swapchain_ = VK_NULL_HANDLE;
    VkFormat swapchain_format_ = VK_FORMAT_UNDEFINED;
    VkExtent2D swapchain_extent_{};
    std::vector<VkImage> swapchain_images_;
    std::vector<VkImageView> swapchain_image_views_;
    VkFormat depth_format_ = VK_FORMAT_UNDEFINED;
    VkImage depth_image_ = VK_NULL_HANDLE;
    VkDeviceMemory depth_memory_ = VK_NULL_HANDLE;
    VkImageView depth_image_view_ = VK_NULL_HANDLE;
    VkRenderPass render_pass_ = VK_NULL_HANDLE;
    VkDescriptorSetLayout texture_descriptor_layout_ = VK_NULL_HANDLE;
    VkDescriptorPool texture_descriptor_pool_ = VK_NULL_HANDLE;
    VkPipelineLayout pipeline_layout_ = VK_NULL_HANDLE;
    std::vector<HostPipeline> graphics_pipelines_;
    VkDescriptorSetLayout texture_convert_descriptor_layout_ = VK_NULL_HANDLE;
    VkPipelineLayout texture_convert_pipeline_layout_ = VK_NULL_HANDLE;
    VkPipeline texture_convert_pipeline_ = VK_NULL_HANDLE;
    VkBuffer vertex_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory vertex_memory_ = VK_NULL_HANDLE;
    VkDeviceSize vertex_buffer_size_ = 0;
    void* vertex_mapped_ = nullptr;
    VkBuffer fragment_state_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory fragment_state_memory_ = VK_NULL_HANDLE;
    VkDeviceSize fragment_state_buffer_size_ = 0;
    void* fragment_state_mapped_ = nullptr;
    uint32_t fragment_state_count_ = 0;
    VkBuffer vertex_program_state_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory vertex_program_state_memory_ = VK_NULL_HANDLE;
    VkDeviceSize vertex_program_state_buffer_size_ = 0;
    void* vertex_program_state_mapped_ = nullptr;
    uint32_t vertex_program_state_count_ = 0;
    VkBuffer raw_vertex_resource_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory raw_vertex_resource_memory_ = VK_NULL_HANDLE;
    VkDeviceSize raw_vertex_resource_buffer_size_ = 0;
    void* raw_vertex_resource_mapped_ = nullptr;
    uint32_t gpu_vertex_program_draw_count_ = 0;
    uint32_t gpu_vertex_program_vertex_count_ = 0;
    uint32_t gpu_raw_attribute_draw_count_ = 0;
    uint32_t gpu_raw_attribute_vertex_count_ = 0;
    uint32_t cpu_vertex_program_fallback_draw_count_ = 0;
    uint32_t cpu_vertex_program_fallback_vertex_count_ = 0;
    uint64_t last_vertex_state_upload_us_ = 0;
    uint64_t last_raw_vertex_upload_us_ = 0;
    uint64_t raw_vertex_resource_upload_bytes_ = 0;
    uint64_t raw_vertex_index_upload_bytes_ = 0;
    uint64_t last_gpu_texture_conversion_us_ = 0;
    uint64_t last_texture_refresh_us_ = 0u;
    uint64_t last_texture_indexed_lookup_count_ = 0u;
    uint64_t last_texture_indexed_lookup_candidate_count_ = 0u;
    uint64_t last_texture_constant_lookup_count_ = 0u;
    uint64_t gpu_texture_conversion_batch_count_ = 0;
    uint64_t gpu_texture_conversion_texture_count_ = 0;
    uint64_t gpu_texture_conversion_mip_count_ = 0;
    uint64_t gpu_texture_conversion_input_bytes_ = 0;
    uint64_t gpu_texture_conversion_output_bytes_ = 0;
    uint64_t gpu_texture_conversion_rejected_batch_count_ = 0;
    GpuTextureValidationCoverage gpu_texture_validation_coverage_{};
    uint64_t last_vertex_transform_us_ = 0;
    uint64_t last_vertex_map_us_ = 0;
    uint64_t last_vertex_copy_us_ = 0;
    uint64_t last_vertex_resource_prepare_us_ = 0u;
    uint64_t last_state_resource_prepare_us_ = 0u;
    uint64_t last_texture_resource_prepare_us_ = 0u;
    uint64_t last_offscreen_resource_prepare_us_ = 0u;
    uint64_t last_resource_bookkeeping_us_ = 0u;
    uint32_t uploaded_vertex_base_ = 0;
    uint32_t uploaded_vertex_count_ = 0;
    uint32_t presented_surface_color_offset_ = 0;
    uint32_t presented_fixed_function_transformed_count_ = 0;
    uint32_t offscreen_render_target_draw_count_ = 0;
    uint32_t offscreen_render_target_transformed_vertex_count_ = 0;
    uint32_t last_render_target_feedback_pruned_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_hit_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_miss_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_store_count_ = 0;
    uint32_t last_render_target_feedback_image_cache_eviction_count_ = 0;
    uint64_t render_target_feedback_image_cache_hit_count_ = 0;
    uint64_t render_target_feedback_image_cache_miss_count_ = 0;
    uint64_t render_target_feedback_image_cache_store_count_ = 0;
    uint64_t render_target_feedback_image_cache_eviction_count_ = 0;
    std::vector<HostTexture> host_textures_;
    std::vector<HostTexture> render_target_feedback_image_cache_;
    std::vector<HostTextureBinding> host_texture_bindings_;
    uint64_t texture_binding_set_reuse_count_ = 0u;
    uint64_t texture_binding_set_rebuild_count_ = 0u;
    uint64_t texture_binding_image_descriptor_update_count_ = 0u;
    uint64_t texture_binding_descriptor_set_allocation_count_ = 0u;
    uint64_t last_texture_binding_update_us_ = 0u;
    uint32_t last_texture_binding_image_descriptor_update_count_ = 0u;
    uint32_t last_texture_binding_descriptor_set_allocation_count_ = 0u;
    bool last_texture_binding_set_reused_ = false;
    std::vector<OffscreenRenderTarget> offscreen_render_targets_;
    std::vector<VkFramebuffer> framebuffers_;
    VkCommandPool command_pool_ = VK_NULL_HANDLE;
    VkQueryPool gpu_timing_query_pool_ = VK_NULL_HANDLE;
    float gpu_timestamp_period_ns_ = 0.0f;
    uint32_t gpu_timestamp_valid_bits_ = 0u;
    uint32_t last_submitted_image_index_ =
        std::numeric_limits<uint32_t>::max();
    bool gpu_frame_time_valid_ = false;
    double last_gpu_frame_ms_ = 0.0;
    VkBuffer readback_buffer_ = VK_NULL_HANDLE;
    VkDeviceMemory readback_memory_ = VK_NULL_HANDLE;
    VkDeviceSize readback_size_ = 0;
    std::vector<VkCommandBuffer> command_buffers_;
    std::vector<uint64_t> command_buffer_draw_counts_;
    std::vector<uint64_t> command_buffer_triangle_counts_;
    std::vector<uint64_t> command_buffer_barrier_counts_;
    mutable uint64_t recording_draw_count_ = 0u;
    mutable uint64_t recording_triangle_count_ = 0u;
    mutable uint64_t recording_barrier_count_ = 0u;
    uint64_t draw_count_ = 0u;
    uint64_t triangle_count_ = 0u;
    uint64_t pipeline_creation_count_ = 0u;
    uint64_t pipeline_cache_miss_count_ = 0u;
    uint64_t last_pipeline_state_discovery_us_ = 0u;
    uint32_t last_pipeline_candidate_draw_count_ = 0u;
    uint32_t last_pipeline_unique_state_count_ = 0u;
    uint32_t last_pipeline_missing_state_count_ = 0u;
    uint64_t descriptor_allocation_count_ = 0u;
    uint64_t command_buffer_allocation_count_ = 0u;
    uint64_t queue_submission_count_ = 0u;
    uint64_t barrier_count_ = 0u;
    uint64_t upload_bytes_ = 0u;
    uint64_t readback_bytes_ = 0u;
    uint64_t active_frame_fence_wait_us_ = 0u;
    uint64_t last_fence_wait_us_ = 0u;
    uint64_t last_window_message_pump_us_ = 0u;
    uint64_t last_controller_poll_us_ = 0u;
    uint64_t last_keyboard_latch_us_ = 0u;
    uint64_t last_reload_probe_us_ = 0u;
    uint64_t last_reload_dispatch_us_ = 0u;
    uint64_t last_pre_render_unattributed_us_ = 0u;
    uint64_t last_reload_fence_wait_us_ = 0u;
    uint64_t last_draw_fence_wait_us_ = 0u;
    uint64_t last_gpu_query_us_ = 0u;
    uint64_t last_fence_reset_us_ = 0u;
    uint64_t last_acquire_us_ = 0u;
    uint64_t last_submit_us_ = 0u;
    uint64_t last_readback_wait_us_ = 0u;
    uint64_t last_readback_process_us_ = 0u;
    uint64_t last_present_us_ = 0u;
    uint64_t last_audit_ack_us_ = 0u;
    uint64_t last_draw_unattributed_us_ = 0u;
    int64_t last_frame_elapsed_us_ = 0;
    uint64_t last_cpu_render_us_ = 0u;
    VkSemaphore image_available_ = VK_NULL_HANDLE;
    VkSemaphore render_finished_ = VK_NULL_HANDLE;
    VkFence in_flight_ = VK_NULL_HANDLE;
};

}  // namespace b2r::host::vulkan_detail
