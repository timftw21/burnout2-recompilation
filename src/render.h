#pragma once
#include "file.h"
#include "vertex_program.h"
#include "performance.h"
#include "upscaling.h"
#include <array>
#include <memory>
#include <wrl/client.h>

struct ID3D11Device;
struct ID3D11DeviceContext;
struct ID3D11SamplerState;

namespace b2 {
struct MotionCamera;
struct Image {
    std::uint32_t width, height;
    std::vector<std::byte> rgba;
};
void save_png(const Image&, const std::filesystem::path&, bool replace = false);
Image load_image(const std::filesystem::path&);
std::string compare_images(const std::filesystem::path& actual,
                           const std::filesystem::path& reference,
                           const std::filesystem::path& difference, unsigned tolerance);

// NV097 color-format codes. DXT blocks are already in row-major block order.
enum class TextureFormat : std::uint32_t {
    rgb565 = 0x05, argb8 = 0x06, xrgb8 = 0x07,
    dxt1 = 0x0C, dxt3 = 0x0E, dxt5 = 0x0F,
    argb8_linear = 0x12, xrgb8_linear = 0x1E
};
struct TextureData {
    std::uint32_t width, height;
    TextureFormat format;
    Bytes bytes;
    std::uint32_t mip_levels = 1, pitch = 0;
    bool cubemap = false;
    std::uint32_t depth = 1;
};
enum class SurfaceColor {rgba8,rgb565};
enum class SurfaceDepth {z24s8,z16};

struct Vertex {
    Float4 position{0, 0, 0, 1}; // Homogeneous D3D clip coordinates.
    Float4 color{1, 1, 1, 1}, secondary{};
    std::array<Float4, 4> uv{};
    float fog = 1;
};
// Numeric primitive, comparison, blending and combiner fields retain NV097 values.
struct RenderState {
    std::uint32_t primitive = 5;
    float point_size = 1;
    bool point_sprite = false, point_scale = false;
    std::array<float,8> point_parameters{};
    bool blend = false, depth_test = false, depth_write = false;
    bool alpha_test = false, cull = false, front_ccw = false;
    std::uint32_t depth_function = 0x0203, alpha_function = 0x0207, alpha_reference = 0;
    std::uint32_t blend_source = 1, blend_destination = 0, blend_equation = 0x8006;
    std::uint32_t cull_face = 0x0405, color_mask = 0x01010101;
    bool stencil_test = false, polygon_offset = false, scissor_test = false;
    std::uint32_t stencil_function = 0x0207, stencil_reference = 0, stencil_read_mask = 255, stencil_write_mask = 255;
    std::uint32_t stencil_fail = 0x1E00, stencil_depth_fail = 0x1E00, stencil_pass = 0x1E00;
    float polygon_offset_scale = 0, polygon_offset_bias = 0;
    std::array<std::int32_t,4> scissor{};
    std::array<std::uint32_t, 4> textures{}, texture_modes{}; // NV097 texture shader modes.
    std::array<std::uint32_t, 4> texture_sources{}, texture_clip{};
    std::array<std::uint32_t, 4> texture_address{0x030303, 0x030303, 0x030303, 0x030303};
    std::array<std::uint32_t,4> texture_filter{0x01030000,0x01030000,0x01030000,0x01030000};
    std::array<std::uint32_t,4> texture_control{0x0003FFC0,0x0003FFC0,0x0003FFC0,0x0003FFC0};
    std::array<bool, 4> alpha_kill{}, texture_opaque{};
    std::uint32_t combiner_control = 0;
    std::array<std::uint32_t, 8> color_inputs{}, alpha_inputs{}, color_outputs{}, alpha_outputs{};
    std::array<std::uint32_t, 8> factor0{}, factor1{};
    std::array<std::uint32_t, 2> final_inputs{}, final_factors{};
    std::uint32_t fog_color = 0;
    bool fog_enable = false;
    std::uint32_t fog_mode = 0x2601;
    std::array<float,2> fog_parameters{};
};

struct ProgramView {
    float depth_scale = 1, subpixel_bias = 0.03125f;
    std::array<Float4,4> texture_scales{{{1,1,1,1},{1,1,1,1},{1,1,1,1},{1,1,1,1}}};
};

struct VertexAttribute {
    std::uint32_t stream=16,offset=0,format=0x42; // Stream 16 supplies current values.
    auto operator<=>(const VertexAttribute&) const = default;
};
using VertexLayout=std::array<VertexAttribute,16>;
struct VertexStream { Bytes bytes;std::uint32_t stride; };

struct RenderStats {
    std::uint64_t draws = 0, vertices = 0, shader_compilations = 0, vertex_bytes = 0;
    std::uint64_t published_frames = 0;
    std::uint64_t reused_vertex_bytes = 0;
    double gpu_ms = 0;
    bool gpu_timing_valid = false;
    PerfCounters counters{};
};
struct RenderResources {
    std::uint64_t local=0,budget=0,shared=0,textures=0,targets=0,texture_bytes=0,target_bytes=0;
    bool gpu_valid=false;
};

class Renderer {
    struct Impl;
public:
    struct Visibility {void* context;void (*scope)(void*,bool original_draw);};
    class PreparedState {
        friend class Renderer;
        friend struct Impl;
        const void* owner_=nullptr;
        const void* pipeline_=nullptr;
        // Keep queued draws valid when their samplers leave the cache.
        std::array<Microsoft::WRL::ComPtr<ID3D11SamplerState>,4> samplers_{};
    };
    struct StreamDraw {
        const RenderState* state=nullptr;
        const VertexConstants* constants=nullptr;
        const ProgramView* view=nullptr;
        std::span<const VertexStream> streams;
        const ProgramVertex* current=nullptr;
        std::span<const std::uint32_t> indices;
        const PreparedState* bindings=nullptr;
        std::uint32_t layout=0,count=0;
    };
    explicit Renderer(std::uint32_t width, std::uint32_t height,
                      bool warp = false, bool debug = false,unsigned resolution_scale = 1);
    ~Renderer();
    Renderer(const Renderer&) = delete;
    Renderer& operator=(const Renderer&) = delete;
    std::uint32_t upload(const TextureData&);
    void release(std::uint32_t resource); // After queued draws have been submitted; native GPU work retains its references.
    std::uint32_t create_target(std::uint32_t width,std::uint32_t height,
                                SurfaceColor=SurfaceColor::rgba8,SurfaceDepth=SurfaceDepth::z24s8);
    void grow_target(std::uint32_t target,std::uint32_t width,std::uint32_t height);
    void set_target(std::uint32_t target=0);
    void copy_target(std::uint32_t destination); // GPU copy for later sampling.
    std::uint32_t copy_cubemap(const std::array<std::uint32_t,6>& faces,std::uint32_t destination=0);
    void copy_to_main();
    void begin_overlay(); // Reuse the presentation copy to keep UI out of scene feedback.
    void end_overlay();
    void restore_feedback(); // Other original framebuffer users need the complete displayed image.
    void resize(std::uint32_t width, std::uint32_t height);
    void reset(unsigned resolution_scale); // New session: discard game targets/textures, retain native pipelines.
    void attach_window(void* native_window);
    void widescreen(bool enabled); // Present the game's native anamorphic image at 16:9.
    void enhancements(unsigned antialiasing,unsigned anisotropy); // AA 0=off, 1..3=FXAA, 4..6=MSAA 2/4/8x; AF 0=original or 1/2/4/8/16.
    void ambient_occlusion(unsigned quality,float strength=0.35f); // 0=off; 1..3=GTAO low/medium/high.
    bool supports_msaa(unsigned samples) const;
    unsigned raster_samples_per_pixel() const; // Host samples representing one original Xbox pixel.
    void upscaling(UpscaleMethod,unsigned output_scale=0);
    void camera(std::uint32_t identifier,float near_plane,float far_plane,float vertical_fov,const MotionCamera* motion=nullptr,float aspect=0);
    bool supports_upscaling(UpscaleMethod) const;
    bool needs_motion_camera() const;
    void visibility(const Visibility*) noexcept; // Exclude presentation-only draws from original sample counts.
    void publish_frame(bool reset = false); // Snapshot a completed game frame.
    bool display(bool wait = true); // Scale the main image when a window buffer is available.
    bool wait_frame(unsigned timeout_ms); // Acquire one presentation slot, with a bounded wait.
    bool present(bool vsync,bool wait = true);
    void performance(Performance*); // Opt-in, bounded queries; never waits for results.
    RenderResources resources() const;
    ID3D11Device* native_device() const;
    ID3D11DeviceContext* native_context() const;
    ID3D11Texture2D* reconstruction_input(unsigned channel) const; // Explicit diagnostic readback only.
    PreparedState prepare(const RenderState&); // Borrowed bindings, valid for this renderer's lifetime.
    std::uint32_t prepare_program(const VertexProgram&, bool launch = false);
    std::uint32_t prepare_fixed(const FixedTransform& = {});
    void snapshot_constants(std::uint32_t program,const VertexConstants& source,VertexConstants& destination,std::uint32_t& previous);
    std::uint32_t prepare_layout(std::uint32_t program,const VertexLayout&);
    void launch_program(std::uint32_t program, VertexConstants&, const ProgramVertex&);
    void begin(std::uint32_t argb = 0xFF000000, float depth = 1, bool clear_target = true);
    void clear(std::uint32_t argb, float depth = 1);
    void clear_buffers(std::uint32_t flags,std::uint32_t argb,float depth,std::uint8_t stencil,
                       std::span<const std::int32_t> rectangle={});
    void clear_stencil(std::uint8_t value = 0);
    void draw(const RenderState&, std::span<const Vertex>, std::span<const std::uint32_t> indices = {});
    void draw_program(const RenderState&, std::uint32_t program, const VertexConstants&,
                      const ProgramView&, std::span<const ProgramVertex>, std::span<const std::uint32_t> indices = {});
    void draw_streams(const RenderState&,std::uint32_t layout,const VertexConstants&,const ProgramView&,
                      std::span<const VertexStream>,const ProgramVertex& current,std::uint32_t count,
                      std::span<const std::uint32_t> indices={},const PreparedState* bindings=nullptr);
    // Stream bytes and current values stay unchanged until the bounded group returns.
    void draw_streams(std::span<const StreamDraw>);
    void end();
    Image readback(); // Explicit diagnostic operation; never implicit in draw/present.
    Image read_frame(); // Published presentation image, including optional antialiasing.
    Image read_target(std::uint32_t);
    Image read_display();
    RenderStats stats() const;
    std::string adapter() const;
    std::vector<std::string> errors() const;
private:
    void configure_upscaling(UpscaleMethod);
    std::unique_ptr<Impl> impl_;
};

std::string check_render(const std::filesystem::path& directory, bool warp, bool debug);
std::string render_texture(const std::filesystem::path& input, TextureFormat format,
                           std::uint32_t width, std::uint32_t height,
                           const std::filesystem::path& output, bool warp);
std::string render_dictionary(const std::filesystem::path& input,
                              const std::filesystem::path& directory, bool warp);
}
