#pragma once
#include "file.h"
#include "vertex_program.h"
#include <array>
#include <memory>

struct ID3D11Device;
struct ID3D11DeviceContext;

namespace b2 {
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
    std::array<bool, 4> linear_filter{}, alpha_kill{}, texture_opaque{};
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
    double gpu_ms = 0;
    bool gpu_timing_valid = false;
};

class Renderer {
public:
    explicit Renderer(std::uint32_t width, std::uint32_t height,
                      bool warp = false, bool debug = false);
    ~Renderer();
    Renderer(const Renderer&) = delete;
    Renderer& operator=(const Renderer&) = delete;
    std::uint32_t upload(const TextureData&);
    std::uint32_t create_target(std::uint32_t width,std::uint32_t height,
                                SurfaceColor=SurfaceColor::rgba8,SurfaceDepth=SurfaceDepth::z24s8);
    void grow_target(std::uint32_t target,std::uint32_t width,std::uint32_t height);
    void set_target(std::uint32_t target=0);
    void copy_target(std::uint32_t destination); // GPU copy for later sampling.
    std::uint32_t copy_cubemap(const std::array<std::uint32_t,6>& faces,std::uint32_t destination=0);
    void copy_to_main();
    void resize(std::uint32_t width, std::uint32_t height);
    void attach_window(void* native_window);
    bool display(); // Scale the main image to the window and bind its overlay target.
    void present(bool vsync);
    ID3D11Device* native_device() const;
    ID3D11DeviceContext* native_context() const;
    void prepare(const RenderState&); // Cached D3D11 states; shaders are built ahead of time.
    std::uint32_t prepare_program(const VertexProgram&, bool launch = false);
    std::uint32_t prepare_fixed(const FixedTransform& = {});
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
                      std::span<const std::uint32_t> indices={});
    void end();
    Image readback(); // Explicit diagnostic operation; never implicit in draw/present.
    Image read_target(std::uint32_t);
    Image read_display();
    RenderStats stats() const;
    std::string adapter() const;
    std::vector<std::string> errors() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

std::string check_render(const std::filesystem::path& directory, bool warp, bool debug);
std::string render_texture(const std::filesystem::path& input, TextureFormat format,
                           std::uint32_t width, std::uint32_t height,
                           const std::filesystem::path& output, bool warp);
std::string render_dictionary(const std::filesystem::path& input,
                              const std::filesystem::path& directory, bool warp);
}
