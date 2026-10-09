#include "render.h"
#include "assets.h"
#include "gpu.h"
#include "cpu.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <format>
#include <functional>
#include <stdexcept>

namespace b2 {
namespace {
std::array<Vertex, 4> quad(Float4 color = {1, 1, 1, 1}, float depth = 0.5f) {
    std::array<Vertex, 4> vertices{};
    constexpr Float4 positions[] = {{-1,1,0,1}, {1,1,0,1}, {1,-1,0,1}, {-1,-1,0,1}};
    constexpr Float4 coordinates[] = {{0,0,0,1}, {1,0,0,1}, {1,1,0,1}, {0,1,0,1}};
    for (unsigned i = 0; i < 4; ++i) {
        vertices[i].position = positions[i]; vertices[i].position[2] = depth;
        vertices[i].color = color;
        for (auto& uv : vertices[i].uv) uv = coordinates[i];
    }
    return vertices;
}
RenderState colored() {
    RenderState state;
    state.primitive = 8; state.final_inputs = {0x00000004, 0x00001400};
    return state;
}
RenderState textured(std::uint32_t texture) {
    auto state = colored(); state.final_inputs = {};
    state.textures[0] = texture; state.texture_modes[0] = 1; state.combiner_control = 1;
    state.color_inputs[0] = 0x04080000; state.alpha_inputs[0] = 0x14180000;
    state.color_outputs[0] = state.alpha_outputs[0] = 0xC0;
    return state;
}
std::array<unsigned, 4> pixel(const Image& image, unsigned x, unsigned y) {
    const auto offset = (std::size_t(y) * image.width + x) * 4;
    return {unsigned(image.rgba[offset]), unsigned(image.rgba[offset + 1]),
            unsigned(image.rgba[offset + 2]), unsigned(image.rgba[offset + 3])};
}
void expect(const Image& image, unsigned x, unsigned y, std::array<unsigned, 4> expected, unsigned tolerance = 1) {
    const auto actual = pixel(image, x, y);
    for (unsigned lane = 0; lane < 4; ++lane)
        if (std::abs(int(actual[lane]) - int(expected[lane])) > int(tolerance))
            throw std::runtime_error(std::format("Pixel ({},{}), channel {}: {} expected {}", x, y, lane, actual[lane], expected[lane]));
}
std::vector<std::byte> solid(unsigned width, unsigned height, std::uint32_t argb) {
    std::vector<std::byte> bytes(std::size_t(width) * height * 4);
    for (std::size_t i = 0; i < bytes.size(); i += 4)
        for (unsigned lane = 0; lane < 4; ++lane) bytes[i + lane] = std::byte((argb >> (lane * 8)) & 255);
    return bytes;
}
}

std::string check_render(const std::filesystem::path& directory, bool warp, bool debug) {
    if (std::filesystem::exists(directory)) throw std::runtime_error("Render output directory already exists; choose a fresh directory");
    Renderer renderer(64, 64, warp, debug);
    std::filesystem::create_directories(directory);
    std::string report = "{\"format\":\"b2-render-check-v1\",\"adapter\":" + json(renderer.adapter()) +
        ",\"warp\":" + (warp ? "true" : "false") + ",\"debug_layer\":" + (debug ? "true" : "false") +
        ",\"game_booted\":false,\"visual_parity_established\":false,\"cases\":[";
    unsigned total = 0, passed = 0;
    const auto run = [&](std::string_view name, RenderState state, std::span<const Vertex> vertices,
                         const std::function<void(const Image&)>& validate,
                         const std::function<void()>& after = {}, std::span<const std::uint32_t> indices = {},
                         const std::function<void()>& submit = {}) {
        if (total++) report += ',';
        const auto started = std::chrono::steady_clock::now();
        std::string error;
        bool begun = false;
        const auto image_path = directory / (std::string(name) + ".png");
        try {
            renderer.prepare(state); renderer.begin(); begun = true;
            if(submit) submit(); else renderer.draw(state, vertices, indices);
            if (after) after();
            renderer.end(); begun = false;
            const auto image = renderer.readback(); save_png(image, image_path);
            validate(image);
            ++passed;
        } catch (const std::exception& failure) {
            error = failure.what();
            if (begun) renderer.end();
        }
        const auto timing = renderer.stats();
        report += std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{},\"draws\":{},\"vertices\":{},\"gpu_ms\":{},\"elapsed_ms\":{}}}",
            json(name), error.empty(), json(error), json(utf8(image_path.wstring())), timing.draws, timing.vertices,
            timing.gpu_timing_valid ? std::format("{}", timing.gpu_ms) : "null",
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count());
    };
    const auto red = quad({1,0,0,1}), blue = quad({0,0,1,1}), white = quad();
    const auto red_pixel = [](const Image& image) { expect(image, 32, 32, {255,0,0,255}); expect(image, 3, 3, {255,0,0,255}); };
    auto state = colored();
    run("colored-quads", state, red, red_pixel);
    constexpr std::uint32_t triangles[] = {0,1,2,0,2,3};
    state.primitive = 5;
    run("indexed-triangles", state, red, red_pixel, {}, triangles);
    state.primitive = 7;
    run("triangle-fan", state, red, red_pixel);
    state.primitive = 10;
    run("polygon", state, red, red_pixel);
    auto strip = std::array<Vertex, 4>{red[0], red[1], red[3], red[2]};
    state.primitive = 6;
    run("triangle-strip", state, strip, red_pixel);
    state.primitive = 9;
    run("quad-strip", state, strip, red_pixel);

    for (auto format : {TextureFormat::argb8, TextureFormat::xrgb8, TextureFormat::argb8_linear, TextureFormat::xrgb8_linear}) {
        const bool opaque = format == TextureFormat::xrgb8 || format == TextureFormat::xrgb8_linear;
        const auto bytes = solid(4,4,0x40FF0000);
        const auto texture = renderer.upload({4,4,format,bytes});
        run(std::format("texture-{:02X}", static_cast<unsigned>(format)), textured(texture), white,
            [opaque](const Image& image) { expect(image,32,32,{255,0,0,opaque?255U:64U}); });
    }
    std::vector<std::byte> rgb565(32);
    for (unsigned i = 1; i < rgb565.size(); i += 2) rgb565[i] = std::byte{0xF8};
    run("texture-565", textured(renderer.upload({4,4,TextureFormat::rgb565,rgb565})), white, red_pixel);
    for (auto format : {TextureFormat::dxt1, TextureFormat::dxt3, TextureFormat::dxt5}) {
        const unsigned header = format == TextureFormat::dxt1 ? 0 : 8;
        std::vector<std::byte> bytes(8 + header);
        bytes[header + 1] = std::byte{0xF8};
        if (format == TextureFormat::dxt3) std::fill_n(bytes.begin(),8,std::byte{255});
        if (format == TextureFormat::dxt5) bytes[0] = std::byte{255};
        run(std::format("texture-{:02X}", static_cast<unsigned>(format)),
            textured(renderer.upload({4,4,format,bytes})), white, red_pixel);
    }
    // Rectangular Morton order is specified independently by enumerating coordinate bits.
    std::vector<std::byte> rectangular(8 * 4 * 4);
    for (unsigned y = 0; y < 4; ++y) for (unsigned x = 0; x < 8; ++x) {
        const auto address = (x & 1) | ((y & 1) << 1) | ((x & 2) << 1) | ((y & 2) << 2) | ((x & 4) << 2);
        rectangular[address * 4] = std::byte(x * 30);
        rectangular[address * 4 + 1] = std::byte(y * 60);
        rectangular[address * 4 + 2] = std::byte(255 - x * 30);
        rectangular[address * 4 + 3] = std::byte{255};
    }
    run("rectangular-swizzle", textured(renderer.upload({8,4,TextureFormat::argb8,rectangular})), white,
        [](const Image& image) {
            for (unsigned y=0;y<4;++y) for(unsigned x=0;x<8;++x)
                expect(image,x*8+4,y*16+8,{255-x*30,y*60,x*30,255});
        });
    auto mip_bytes = solid(4,4,0xFFFF0000);
    const auto mip1 = solid(2,2,0xFF00FF00), mip2 = solid(1,1,0xFF0000FF);
    mip_bytes.insert(mip_bytes.end(),mip1.begin(),mip1.end()); mip_bytes.insert(mip_bytes.end(),mip2.begin(),mip2.end());
    auto minified = white;
    for (auto& vertex : minified) { vertex.uv[0][0] *= 64; vertex.uv[0][1] *= 64; }
    run("mip-chain", textured(renderer.upload({4,4,TextureFormat::argb8,mip_bytes,3})), minified,
        [](const Image& image) { expect(image,32,32,{0,0,255,255}); });
    std::vector<std::byte> pitched(24 * 4);
    const auto row = solid(4,1,0xFF00FF00);
    for (unsigned y=0;y<4;++y) std::copy(row.begin(),row.end(),pitched.begin()+y*24);
    run("linear-pitch", textured(renderer.upload({4,4,TextureFormat::argb8_linear,pitched,1,24})), white,
        [](const Image& image) { expect(image,32,32,{0,255,0,255}); });

    auto blend = colored(); blend.blend = true; blend.blend_source = 0x302; blend.blend_destination = 0x303;
    renderer.prepare(blend);
    run("alpha-blend", colored(), blue,
        [](const Image& image) { expect(image,32,32,{128,0,128,191}); },
        [&] { renderer.draw(blend,quad({1,0,0,0.5f})); });
    auto masked = colored(); masked.color_mask = 0x10000;
    renderer.prepare(masked);
    run("color-mask", colored(), blue,
        [](const Image& image) { expect(image,32,32,{255,0,255,255}); },
        [&] { renderer.draw(masked,red); });
    auto depth = colored(); depth.depth_test = depth.depth_write = true;
    run("depth-occlusion", depth, quad({1,0,0,1},0.25f), red_pixel,
        [&] { renderer.draw(depth,quad({0,0,1,1},0.75f)); });
    for (unsigned function = 0x200; function <= 0x207; ++function) {
        auto alpha = colored(); alpha.alpha_test = true; alpha.alpha_function = function; alpha.alpha_reference = 128;
        // Alpha 0.5 rounds to 128. Equal/LEqual/GEqual/Always pass.
        const bool expected = function == 0x202 || function == 0x203 || function == 0x206 || function == 0x207;
        run(std::format("alpha-compare-{:03X}",function), alpha, quad({1,0,0,0.5f}),
            [expected](const Image& image) { expect(image,32,32,expected ? std::array<unsigned,4>{255,0,0,128} : std::array<unsigned,4>{0,0,0,255}); });
    }
    auto factors = colored(); factors.combiner_control = 1; factors.final_inputs = {};
    factors.color_inputs[0] = 0x01200000; factors.alpha_inputs[0] = 0x11200000;
    factors.color_outputs[0] = factors.alpha_outputs[0] = 0xC0; factors.factor0[0] = 0xFF0080FF;
    run("combiner-factors", factors, white,
        [](const Image& image) { expect(image,32,32,{0,128,255,255}); });
    factors.factor0[0] = 0xFFFF8000;
    run("combiner-updated-constants", factors, white,
        [](const Image& image) { expect(image,32,32,{255,128,0,255}); });
    auto passthrough=colored();passthrough.combiner_control=1;
    passthrough.color_inputs[0]=0x04200000;passthrough.alpha_inputs[0]=0x14200000;
    passthrough.color_outputs[0]=passthrough.alpha_outputs[0]=0xC00;passthrough.final_inputs={12,0x1C80};
    run("combiner-pass-through-sum",passthrough,red,red_pixel);
    auto blue_alpha=factors;blue_alpha.factor0[0]=0xFF4080C0;
    blue_alpha.color_outputs[0]=0xB00C0;blue_alpha.alpha_outputs[0]=0;
    run("combiner-blue-to-alpha-mapping",blue_alpha,white,
        [](const Image& image){expect(image,32,32,{32,64,96,96});});

    auto scissor=colored(); scissor.scissor_test=true; scissor.scissor={16,16,48,48};
    run("scissor",scissor,red,[](const Image& image) {
        expect(image,32,32,{255,0,0,255}); expect(image,3,3,{0,0,0,255});
    });
    auto write_stencil=colored(); write_stencil.stencil_test=true; write_stencil.stencil_reference=7;
    write_stencil.stencil_pass=0x1E01; write_stencil.scissor_test=true; write_stencil.scissor={16,16,48,48};
    auto read_stencil=colored(); read_stencil.stencil_test=true; read_stencil.stencil_reference=7;
    read_stencil.stencil_function=0x202; read_stencil.stencil_write_mask=0;
    renderer.prepare(read_stencil);
    run("stencil-replace-equal",write_stencil,red,[](const Image& image) {
        expect(image,32,32,{0,0,255,255}); expect(image,3,3,{0,0,0,255});
    },[&] {renderer.draw(read_stencil,blue);});
    constexpr std::array<std::int32_t,4> clear_rectangle{16,16,48,48};
    for(unsigned mask:{1U,2U,4U,8U,9U}) {
        run(std::format("clear-channel-mask-{}",mask),colored(),blue,[mask](const Image& image) {
            const std::array<unsigned,4> initial{0,0,255,255},replacement{96,128,160,64};
            auto expected=initial;
            for(unsigned lane=0;lane<4;++lane) if(mask&(1U<<lane)) expected[lane]=replacement[lane];
            expect(image,32,32,expected);expect(image,3,3,expected);
        },[&] {renderer.clear_buffers(mask<<4,0x406080A0,1,0);});
    }
    run("clear-color-rectangle",colored(),blue,[](const Image& image) {
        expect(image,32,32,{255,0,0,255});expect(image,3,3,{0,0,255,255});
    },[&] {renderer.clear_buffers(0xF0,0xFFFF0000,1,0,clear_rectangle);});
    run("clear-depth-rectangle",depth,quad({1,0,0,1},0.25f),[](const Image& image) {
        expect(image,32,32,{0,255,0,255});expect(image,3,3,{255,0,0,255});
    },[&] {
        renderer.clear_buffers(1,0,0.75f,0,clear_rectangle);
        renderer.draw(depth,quad({0,1,0,1},0.5f));
    });
    run("clear-stencil-rectangle",colored(),blue,[](const Image& image) {
        expect(image,32,32,{255,0,0,255});expect(image,3,3,{0,0,255,255});
    },[&] {
        renderer.clear_buffers(2,0,1,7,clear_rectangle);
        renderer.draw(read_stencil,red);
    });
    run("clear-clipped-rectangle",colored(),blue,[](const Image& image) {
        expect(image,3,3,{255,0,0,255});expect(image,32,32,{0,0,255,255});
    },[&] {
        constexpr std::array<std::int32_t,4> clipped{-8,-8,16,16};
        renderer.clear_buffers(0xF0,0xFFFF0000,1,0,clipped);
    });
    run("clear-empty-rectangle",colored(),blue,[](const Image& image) {expect(image,32,32,{0,0,255,255});},[&] {
        constexpr std::array<std::int32_t,4> empty{40,40,20,20};
        renderer.clear_buffers(0xF3,0xFFFF0000,0,7,empty);
    });
    std::vector<std::byte> cube;
    constexpr std::uint32_t face_colors[]={0xFFFF0000,0xFF00FF00,0xFF0000FF,0xFFFFFF00,0xFFFF00FF,0xFF00FFFF};
    for(const auto value:face_colors) {const auto face=solid(4,4,value);cube.insert(cube.end(),face.begin(),face.end());cube.resize(cube.size()+64);}
    auto cube_state=textured(renderer.upload({4,4,TextureFormat::argb8,cube,1,0,true})); cube_state.texture_modes[0]=3;
    auto cube_vertices=white; for(auto& vertex:cube_vertices) vertex.uv[0]={1,0,0,1};
    run("cubemap-positive-x",cube_state,cube_vertices,red_pixel);
    for(auto& vertex:cube_vertices) vertex.uv[0]={0,0,-1,1};
    run("cubemap-negative-z",cube_state,cube_vertices,[](const Image& image) {expect(image,32,32,{0,255,255,255});});

    std::vector<std::byte> halves(4*4*4);
    for(unsigned y=0;y<4;++y) for(unsigned x=0;x<4;++x) {
        const auto offset=(y*4+x)*4;
        halves[offset+1]=std::byte(x>=2?255:0);halves[offset+2]=std::byte(x<2?255:0);halves[offset+3]=std::byte{255};
    }
    const auto half_texture=renderer.upload({4,4,TextureFormat::argb8_linear,halves});
    auto projected=white;for(auto& vertex:projected) vertex.uv[0][3]=2;
    run("projective-2d-divide",textured(half_texture),projected,red_pixel);
    run("projective-2d-unit",textured(half_texture),white,[](const Image& image) {expect(image,32,32,{0,255,0,255});});
    std::array<std::byte,2*2*4*4> volume{};
    static constexpr std::uint32_t slice_colors[]={0xFFFF0000,0xFF00FF00,0xFF0000FF,0xFFFFFFFF};
    for(unsigned z=0;z<4;++z) for(unsigned y=0;y<2;++y) for(unsigned x=0;x<2;++x) {
        const auto index=x|(y<<1)|((z&1)<<2)|((z&2)<<2);
        for(unsigned lane=0;lane<4;++lane) volume[index*4+lane]=std::byte((slice_colors[z]>>(lane*8))&255);
    }
    auto volume_state=textured(renderer.upload({2,2,TextureFormat::argb8,volume,1,0,false,4}));volume_state.texture_modes[0]=2;
    for(unsigned z=0;z<4;++z) {
        auto vertices=white;for(auto& vertex:vertices) vertex.uv[0]={0.5f,0.5f,(float(z)+0.5f)/4,1};
        run(std::format("volume-swizzle-slice-{}",z),volume_state,vertices,[z](const Image& image) {
            const auto value=slice_colors[z];expect(image,32,32,{(value>>16)&255,(value>>8)&255,value&255,255});
        });
    }
    auto volume_projected=white;for(auto& vertex:volume_projected) vertex.uv[0]={1,1,1.25f,2};
    run("projective-volume-divide",volume_state,volume_projected,[](const Image& image) {expect(image,32,32,{0,0,255,255});});
    auto pass=colored();pass.texture_modes[0]=4;pass.alpha_kill[0]=true;pass.final_inputs={0x00000008,0x00001800};
    auto pass_vertices=white;for(auto& vertex:pass_vertices) vertex.uv[0]={0.25f,0.5f,1.5f,-1};
    run("texture-coordinate-passthrough",pass,pass_vertices,[](const Image& image) {expect(image,32,32,{64,128,255,0});});
    auto clip=colored();clip.texture_modes[1]=5;clip.alpha_kill[1]=true;
    auto clip_vertices=red;for(unsigned i=0;i<4;++i) clip_vertices[i].uv[1]={i==0 || i==3?-1.f:1.f,1,1,1};
    run("texture-clip-negative",clip,clip_vertices,[](const Image& image) {
        expect(image,16,32,{0,0,0,255});expect(image,48,32,{255,0,0,255});
    });
    clip.texture_clip[1]=1;
    run("texture-clip-positive",clip,clip_vertices,[](const Image& image) {
        expect(image,16,32,{255,0,0,255});expect(image,48,32,{0,0,0,255});
    });
    auto dependent=colored();dependent.texture_modes[0]=4;dependent.textures[1]=half_texture;
    dependent.final_inputs={0x00000009,0x00001900};
    auto dependent_vertices=white;for(auto& vertex:dependent_vertices) vertex.uv[0]={0.75f,0.75f,0.25f,0.25f};
    dependent.texture_modes[1]=15;
    run("dependent-alpha-red",dependent,dependent_vertices,red_pixel);
    dependent.texture_modes[1]=16;
    run("dependent-green-blue",dependent,dependent_vertices,[](const Image& image) {expect(image,32,32,{0,255,0,255});});
    dependent.texture_modes[1]=4;dependent.texture_modes[3]=15;dependent.textures[3]=half_texture;
    dependent.texture_sources[3]=1;dependent.final_inputs={0x0000000B,0x00001B00};
    for(auto& vertex:dependent_vertices) vertex.uv[1]={0.75f,0,0,0.25f};
    run("dependent-selected-stage",dependent,dependent_vertices,red_pixel);

    const auto source_target=renderer.create_target(64,64),copied_target=renderer.create_target(64,64);
    const auto copied_state=textured(copied_target);renderer.prepare(colored());
    run("render-target-gpu-copy",copied_state,white,red_pixel,{}, {},[&] {
        renderer.set_target(source_target);renderer.clear(0xFF000000);renderer.draw(colored(),red);
        renderer.copy_target(copied_target);renderer.set_target();renderer.draw(copied_state,white);
    });
    run("render-target-sampling",textured(source_target),white,red_pixel);
    run("render-target-copy-independence",copied_state,white,red_pixel,{}, {},[&] {
        renderer.set_target(source_target);renderer.clear(0xFF00FF00);renderer.set_target();renderer.draw(copied_state,white);
    });
    run("render-target-content-update",textured(source_target),white,[](const Image& image) {expect(image,32,32,{0,255,0,255});});
    auto opaque_feedback=colored();opaque_feedback.textures[0]=source_target;opaque_feedback.texture_modes[0]=1;
    opaque_feedback.texture_opaque[0]=opaque_feedback.alpha_kill[0]=true;opaque_feedback.final_inputs={8,0x1800};
    run("xrgb-feedback-alpha-kill-combiner",opaque_feedback,white,red_pixel,{}, {},[&] {
        renderer.set_target(source_target);renderer.clear(0x00FF0000);
        renderer.set_target();renderer.draw(opaque_feedback,white);
    });

    const auto growth_check=[&](bool stencil,bool rgb565=false) {
        const std::string name=rgb565?"render-target-growth-rgb565-z16":stencil?"render-target-growth-stencil":"render-target-growth-depth-color";
        if(total++) report+=',';
        std::string error;const auto image_path=directory/(name+".png");
        try {
            Renderer native(64,64,warp,debug);const auto target=native.create_target(32,32,
                rgb565?SurfaceColor::rgb565:SurfaceColor::rgba8,rgb565?SurfaceDepth::z16:SurfaceDepth::z24s8);
            native.set_target(target);native.begin(0xFF0000FF,0.25f);native.clear_stencil(7);native.end();
            native.grow_target(target,64,64);
            auto state=colored();state.depth_test=!stencil;
            state.stencil_test=stencil;state.stencil_function=0x202;state.stencil_reference=7;
            native.prepare(state);native.begin(0,1,false);native.draw(state,red);native.end();native.copy_to_main();
            const auto image=native.readback();save_png(image,image_path);
            expect(image,16,16,stencil?std::array<unsigned,4>{255,0,0,255}:std::array<unsigned,4>{0,0,255,255});
            expect(image,48,48,stencil?std::array<unsigned,4>{0,0,0,0}:std::array<unsigned,4>{255,0,0,255});
            if(const auto messages=native.errors();!messages.empty()) throw std::runtime_error("Target growth: "+messages.front());
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{}}}",json(name),error.empty(),json(error),json(utf8(image_path.wstring())));
    };
    growth_check(false);growth_check(true);growth_check(false,true);

    const auto token=[](unsigned mac,unsigned ilu,unsigned input,unsigned constant,unsigned out,bool context,bool final,
                        unsigned at=2,unsigned bt=3,unsigned ct=3,unsigned temporary=0,unsigned mm=0,unsigned im=0) {
        return std::array<std::uint32_t,4>{0,(mac<<21)|(ilu<<25)|(input<<9)|(constant<<13)|0x1B,
            (at<<26)|(bt<<11)|(0x1B<<17)|(0x1B<<2),
            (ct<<28)|(temporary<<20)|(mm<<24)|(im<<16)|(15<<12)|(context?0:1<<11)|(out<<3)|(ilu?4:0)|(final?1:0)};
    };
    VertexProgram vertex_program;
    vertex_program.tokens[0]=token(1,0,0,0,0,false,false);
    vertex_program.tokens[1]=token(1,0,3,0,3,false,true);
    const auto gpu_program=renderer.prepare_program(vertex_program);
    std::array<ProgramVertex,4> program_vertices{};
    constexpr Float4 screen_positions[]={{0,0,0.5f,1},{64,0,0.5f,1},{64,64,0.5f,1},{0,64,0.5f,1}};
    for(unsigned i=0;i<4;++i) {program_vertices[i].attributes[0]=screen_positions[i];program_vertices[i].attributes[3]={1,0,0,1};}
    VertexConstants context{};
    ProgramView view; view.subpixel_bias=0;
    run("translated-vertex-program",colored(),white,red_pixel,{}, {},[&] {
        renderer.draw_program(colored(),gpu_program,context,view,program_vertices);
    });
    auto stale_program=vertex_program;stale_program.tokens.back()={0xFFFFFFFF,0xFFFFFFFF,0xFFFFFFFF,0xFFFFFFFF};
    auto relocated_program=stale_program;relocated_program.start=5;
    relocated_program.tokens[5]=vertex_program.tokens[0];relocated_program.tokens[6]=vertex_program.tokens[1];
    const auto stale_handle=renderer.prepare_program(stale_program),relocated_handle=renderer.prepare_program(relocated_program);
    run("vertex-program-inactive-slots",colored(),white,[&](const Image& image) {
        red_pixel(image);if(stale_handle!=gpu_program) throw std::runtime_error("Inactive slots changed the compiled program");
    },{},{},[&]{renderer.draw_program(colored(),stale_handle,context,view,program_vertices);});
    run("vertex-program-relocated-start",colored(),white,[&](const Image& image) {
        red_pixel(image);if(relocated_handle!=gpu_program) throw std::runtime_error("Relocation changed the compiled program");
    },{},{},[&]{renderer.draw_program(colored(),relocated_handle,context,view,program_vertices);});
    ProgramVertex current{};for(auto& attribute:current.attributes) attribute={0,0,0,1};
    VertexLayout raw_layout{};raw_layout[0]={0,0,0x32};raw_layout[3]={1,0,0x40};
    const auto raw_case=[&](std::string_view name,std::uint32_t format,Bytes payload,std::array<unsigned,4> expected,bool repeat=false) {
        raw_layout[3].format=format;
        const auto layout=renderer.prepare_layout(gpu_program,raw_layout);
        std::vector<std::byte> repeated;
        if(repeat) {for(unsigned i=0;i<4;++i) repeated.insert(repeated.end(),payload.begin(),payload.end());}
        const std::array streams{VertexStream{std::as_bytes(std::span(screen_positions)),sizeof(Float4)},
            VertexStream{repeat?Bytes(repeated):payload,repeat?static_cast<std::uint32_t>(payload.size()):0U}};
        run(name,colored(),white,[&](const Image& image){expect(image,32,32,expected);},{},{},[&] {
            renderer.draw_streams(colored(),layout,context,view,streams,current,4);
        });
    };
    const std::array<float,4> float_color{0.25f,0.5f,0.75f,0.5f};
    const auto floats=std::as_bytes(std::span(float_color));
    raw_case("vertex-stream-float1",0x12,floats.first(4),{64,0,0,255});
    raw_case("vertex-stream-float2",0x22,floats.first(8),{64,128,0,255});
    raw_case("vertex-stream-float3",0x32,floats.first(12),{64,128,191,255});
    raw_case("vertex-stream-float4",0x42,floats,{64,128,191,128});
    raw_case("vertex-stream-float2h",0x72,floats.first(12),{64,128,0,191});
    const std::uint32_t packed_color=0x80402010;
    raw_case("vertex-stream-argb",0x40,std::as_bytes(std::span(&packed_color,1)),{64,32,16,128});
    const std::array<std::int16_t,4> normalized{32767,-16384,16384,16384};
    const auto norm=std::as_bytes(std::span(normalized));
    raw_case("vertex-stream-normal1",0x11,norm.first(2),{255,0,0,255});
    raw_case("vertex-stream-normal2",0x21,norm.first(4),{255,0,0,255});
    raw_case("vertex-stream-normal3",0x31,norm.first(6),{255,0,128,255},true);
    raw_case("vertex-stream-normal4",0x41,norm,{255,0,128,128});
    const std::array<std::int16_t,4> integers{1,-32768,1,1};
    const auto shorts=std::as_bytes(std::span(integers));
    raw_case("vertex-stream-short1",0x15,shorts.first(2),{255,0,0,255});
    raw_case("vertex-stream-short2",0x25,shorts.first(4),{255,0,0,255});
    raw_case("vertex-stream-short3",0x35,shorts.first(6),{255,0,255,255},true);
    raw_case("vertex-stream-short4",0x45,shorts,{255,0,255,255});
    const std::array<std::byte,4> byte_color{std::byte{20},std::byte{40},std::byte{60},std::byte{80}};
    raw_case("vertex-stream-byte1",0x14,Bytes(byte_color).first(1),{20,0,0,255});
    raw_case("vertex-stream-byte2",0x24,Bytes(byte_color).first(2),{20,40,0,255});
    raw_case("vertex-stream-byte3",0x34,Bytes(byte_color).first(3),{20,40,60,255},true);
    raw_case("vertex-stream-byte4",0x44,byte_color,{20,40,60,80});
    const std::uint32_t normal_packed=1023U|(1024U<<11)|(511U<<22);
    raw_case("vertex-stream-packed3",0x16,std::as_bytes(std::span(&normal_packed,1)),{255,0,255,255});
    struct RawVertex {std::array<float,3> position;std::uint32_t color;};
    std::array<RawVertex,4> raw_vertices{};
    for(unsigned i=0;i<4;++i) {std::copy_n(screen_positions[i].begin(),3,raw_vertices[i].position.begin());raw_vertices[i].color=0xFFFF0000;}
    raw_layout[3]={0,12,0x40};
    const auto interleaved=renderer.prepare_layout(gpu_program,raw_layout);
    const std::array raw_stream{VertexStream{std::as_bytes(std::span(raw_vertices)),sizeof(RawVertex)}};
    auto indexed_raw=colored();indexed_raw.primitive=5;
    const std::array<std::uint32_t,6> raw_indices{0,1,2,0,2,3};
    run("vertex-stream-interleaved-indexed",indexed_raw,white,[&](const Image& image) {
        red_pixel(image);if(renderer.stats().vertex_bytes!=320) throw std::runtime_error("Interleaved vertices were expanded during upload");
    },{},{},[&]{renderer.draw_streams(indexed_raw,interleaved,context,view,raw_stream,current,4,raw_indices);});
    raw_layout[3]={};current.attributes[3]={0,0,1,1};
    const auto default_color=renderer.prepare_layout(gpu_program,raw_layout);
    run("vertex-stream-current-color",colored(),white,[](const Image& image){expect(image,32,32,{0,0,255,255});},{},{},[&] {
        renderer.draw_streams(colored(),default_color,context,view,raw_stream,current,4);
    });
    const auto fixed_program=renderer.prepare_fixed();
    VertexConstants fixed_constants{};
    fixed_constants[0]={32,0,0,0};fixed_constants[1]={0,-32,0,0};
    fixed_constants[2]={0,0,1,0};fixed_constants[3]={0,0,0,1};fixed_constants[59]={32,32,0,0};
    std::array<ProgramVertex,4> fixed_vertices{};
    constexpr Float4 fixed_positions[]={{-1,1,0.5f,1},{1,1,0.5f,1},{1,-1,0.5f,1},{-1,-1,0.5f,1}};
    for(unsigned i=0;i<4;++i) {fixed_vertices[i].attributes[0]=fixed_positions[i];fixed_vertices[i].attributes[3]={1,0,0,1};}
    run("fixed-composite-transform",colored(),white,red_pixel,{},{},[&] {
        renderer.draw_program(colored(),fixed_program,fixed_constants,view,fixed_vertices);
    });
    auto shifted_fixed=fixed_constants;shifted_fixed[0][3]=-16;
    run("fixed-transform-translation",colored(),white,[](const Image& image) {
        expect(image,32,32,{255,0,0,255});expect(image,60,32,{0,0,0,255});
    },{},{},[&]{renderer.draw_program(colored(),fixed_program,shifted_fixed,view,fixed_vertices);});
    for(auto& vertex:fixed_vertices) for(auto& lane:vertex.attributes[0]) lane*=2;
    run("fixed-transform-perspective",colored(),white,red_pixel,{},{},[&] {
        renderer.draw_program(colored(),fixed_program,fixed_constants,view,fixed_vertices);
    });
    VertexLayout fixed_layout{};fixed_layout[0]={0,0,0x42};
    const auto fixed_stream_layout=renderer.prepare_layout(fixed_program,fixed_layout);
    const std::array fixed_stream{VertexStream{std::as_bytes(std::span(fixed_positions)),sizeof(Float4)}};
    current.attributes[3]={0,1,0,1};
    run("fixed-transform-native-stream",colored(),white,[](const Image& image){expect(image,32,32,{0,255,0,255});},{},{},[&] {
        renderer.draw_streams(colored(),fixed_stream_layout,fixed_constants,view,fixed_stream,current,4);
    });
    auto fixed_context=fixed_constants;
    for(unsigned bone=0;bone<4;++bone) for(unsigned lane=0;lane<4;++lane) {
        fixed_context[8+bone*8+lane]={};fixed_context[8+bone*8+lane][lane]=1;
        fixed_context[12+bone*8+lane]=fixed_context[8+bone*8+lane];
    }
    for(unsigned i=0;i<4;++i) {
        fixed_vertices[i].attributes[0]=fixed_positions[i];fixed_vertices[i].attributes[1]={0.2f,0.3f,0.1f,0.4f};
        fixed_vertices[i].attributes[2]={0,0,1,0};fixed_vertices[i].attributes[4]={0,0,0,0.5f};
        fixed_vertices[i].attributes[5]={0.5f,0,0,1};fixed_vertices[i].attributes[9]={0.25f,0.5f,0.75f,1};
    }
    for(unsigned mode=1;mode<=6;++mode) {
        FixedTransform transform{};transform.skin=mode;
        const auto shader=renderer.prepare_fixed(transform);auto constants=fixed_context;
        const auto count=(mode+3)/2;
        float final_weight=fixed_vertices[0].attributes[1][count-1];
        if(mode&1) {final_weight=1;for(unsigned i=0;i+1<count;++i) final_weight-=fixed_vertices[0].attributes[1][i];}
        float total_weight=1;
        if(!(mode&1)) {total_weight=0;for(unsigned i=0;i<count;++i) total_weight+=fixed_vertices[0].attributes[1][i];}
        constants[8+(count-1)*8][3]=-total_weight/final_weight;
        run(std::format("fixed-skin-mode-{}",mode),colored(),white,[](const Image& image) {
            expect(image,16,32,{255,0,0,255});expect(image,48,32,{0,0,0,255});
        },{},{},[&]{renderer.draw_program(colored(),shader,constants,view,fixed_vertices);});
    }
    for(unsigned mode:{0x2400U,0x2401U,0x8511U,0x8512U,0x2402U}) {
        FixedTransform transform{};auto constants=fixed_context;
        const auto lanes=mode==0x2402?2U:3U;
        for(unsigned lane=0;lane<lanes;++lane) transform.texgen[0][lane]=mode;
        for(unsigned lane=0;lane<3;++lane) {constants[64+lane]={0,0,0,float(lane+1)*0.25f};constants[8+lane]={0,0,0,lane==2?-1.0f:0};}
        const auto shader=renderer.prepare_fixed(transform);
        const auto expected=(mode==0x8511 || mode==0x8512)?std::array<unsigned,4>{0,0,255,255}:
            mode==0x2402?std::array<unsigned,4>{128,128,191,255}:std::array<unsigned,4>{64,128,191,255};
        run(std::format("fixed-texgen-{:04X}",mode),pass,white,[&](const Image& image){expect(image,32,32,expected);},{},{},[&] {
            renderer.draw_program(pass,shader,constants,view,fixed_vertices);
        });
    }
    FixedTransform normal_transform{};normal_transform.normalize=true;normal_transform.texgen[0]={0x8511,0x8511,0x8511,0};
    const auto normal_shader=renderer.prepare_fixed(normal_transform);
    for(auto& vertex:fixed_vertices) vertex.attributes[2]={3,4,0,0};
    auto normal_context=fixed_context;normal_context[12][0]=0.5f;normal_context[13][1]=0.25f;
    run("fixed-normal-normalization",pass,white,[](const Image& image){expect(image,32,32,{212,141,0,255});},{},{},[&] {
        renderer.draw_program(pass,normal_shader,normal_context,view,fixed_vertices);
    });
    FixedTransform texture_transform{};texture_transform.texture_matrix[0]=true;
    const auto texture_shader=renderer.prepare_fixed(texture_transform);auto texture_context=fixed_context;
    texture_context[68]={2,0,0,0};texture_context[69]={0,1,0,0};texture_context[70]={0,0,1,0};texture_context[71]={0,0,0,1};
    run("fixed-texture-matrix",pass,white,[](const Image& image){expect(image,32,32,{128,128,191,255});},{},{},[&] {
        renderer.draw_program(pass,texture_shader,texture_context,view,fixed_vertices);
    });
    // Fog enters the final combiner through its alpha interpolation factor.
    auto fogged=colored();fogged.fog_enable=true;fogged.fog_color=0x000000FF;fogged.fog_parameters={2,-1};
    fogged.final_inputs={0x13040300,0x00001400};
    for(unsigned mode:{0U,1U,2U,3U,6U}) {
        FixedTransform transform{};transform.fog=true;transform.fog_source=mode;
        auto constants=fixed_context;constants[8]={};constants[9]={};constants[10]={0,0,0,0.5f};
        constants[57]={0,0,mode==3?-1.0f:1.0f,0};
        const auto shader=renderer.prepare_fixed(transform);
        run(std::format("fixed-fog-source-{}",mode),fogged,white,[](const Image& image){expect(image,32,32,{128,0,128,255});},{},{},[&] {
            renderer.draw_program(fogged,shader,constants,view,fixed_vertices);
        });
    }
    // Transform launches run the same decoded operations on a single GPU thread.
    // Results below are independent arithmetic expectations, not another decoder.
    constexpr Float4 vector_results[]={
        {2,3,4,5},{1,3,8,15},{2.5f,4,6,8},{1.5f,4,10,18},{12,12,12,12},
        {15,15,15,15},{27,27,27,27},{1,3,4,3},{0.5f,1,2,3},{2,3,4,5},{0,0,0,0},{1,1,1,1}};
    const Float4 scalar_results[]={{0.5f,1,2,3},{2,2,2,2},{2,2,2,2},
        {std::sqrt(2.0f),std::sqrt(2.0f),std::sqrt(2.0f),std::sqrt(2.0f)},
        {1,0.5f,std::sqrt(2.0f),1},{-1,1,-1,1},{1,0.5f,1,1}};
    const auto launch_check=[&](std::string_view name,VertexProgram program,const Float4& expected) {
        if(total++) report+=',';
        std::string error;
        try {
            VertexConstants constants{}; constants[96]={0.5f,1,2,3}; constants[98]={4,5,6,7};
            ProgramVertex input; input.attributes[0]={2,3,4,5};
            const auto compiled=renderer.prepare_program(program,true);
            renderer.launch_program(compiled,constants,input);
            for(unsigned lane=0;lane<4;++lane)
                if(std::abs(constants[100][lane]-expected[lane])>0.00001f)
                    throw std::runtime_error(std::format("Transform lane {}: {} expected {}",lane,constants[100][lane],expected[lane]));
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{}}}",json(name),error.empty(),json(error));
    };
    for(unsigned mac=1;mac<=12;++mac) {
        VertexProgram program; program.tokens[0]=token(mac,0,0,96,100,true,true);
        launch_check(std::format("vertex-mac-{}",mac),program,vector_results[mac-1]);
    }
    for(unsigned ilu=1;ilu<=7;++ilu) {
        VertexProgram program; program.tokens[0]=token(0,ilu,0,96,100,true,true);
        launch_check(std::format("vertex-ilu-{}",ilu),program,scalar_results[ilu-1]);
    }
    VertexProgram relative;
    relative.tokens[0]=token(13,0,0,0,0,false,false); relative.tokens[0][3]&=~(15U<<12);
    relative.tokens[1]=token(1,0,0,96,100,true,true,3); relative.tokens[1][3]|=2;
    launch_check("vertex-relative-constant",relative,{4,5,6,7});
    VertexProgram paired;
    paired.tokens[0]=token(1,1,0,96,0,false,false,2,3,3,1,15,15); paired.tokens[0][3]&=~(15U<<12);
    paired.tokens[1]=token(1,0,0,0,100,true,true,1); paired.tokens[1][2]|=1U<<28;
    launch_check("vertex-paired-register-ownership",paired,{0.5f,1,2,3});

    const auto gpu_check=[&](std::string_view name,bool indexed,bool shifted,bool programmable,bool inlined=false,bool skinned=false,bool generated_texture=false,bool surface_clip=false,
                         bool target_sample=false,bool rgb565=false,bool unfinished=false,bool linear_sample=false,
                         TextureFormat feedback_format=TextureFormat::argb8_linear) {
        if(total++) report+=',';
        std::string error;GpuStats statistics{};
        const auto image_path=directory/(std::string(name)+".png");
        try {
            Renderer native(64,64,warp,debug);
            std::vector<std::byte> ram(65536);Memory memory(0,ram);Gpu gpu(native,memory,60000);
            const bool alpha_feedback=feedback_format==TextureFormat::argb8 || feedback_format==TextureFormat::xrgb8;
            std::vector<std::uint32_t> words;
            const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values,bool nonincreasing=false) {
                words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18)|(nonincreasing?0x40000000U:0));
                words.insert(words.end(),values);
            };
            packet(0,{0xD});packet(0x1A4,{8});packet(0x1D6C,{0});
            packet(0x200,{64U<<16,64U<<16,rgb565?0x06060213U:alpha_feedback?0x06060228U:0x128U,
                rgb565?0x00800080U:0x01000100U,0x20000,0x30000});
            packet(0x1D98,{63U<<16,63U<<16});packet(0x1D8C,{0xFFFFFF00,alpha_feedback?0x000000FFU:0xFF0000FFU});packet(0x1D94,{0xF3});
            if(surface_clip) packet(0x200,{(32U<<16)|16U,(32U<<16)|16U});
            packet(0x29C,{0x0804});packet(0x33C,{0x207,0,1,0});packet(0x350,{0x8006,0x203,0x01010101,0,255,0x207,0,255,0x1E00,0x1E00,0x1E00});
            packet(0x39C,{0x405,0x901});packet(0x288,{0x00000004,0x00001400});
            for(unsigned stage=0;stage<4;++stage) packet(0x1B08+stage*0x40,{0x030303});
            packet(0xAF0,{0x3F800000,0x3F800000,0x3F800000,0});
            packet(0xA20,{0x42000000,0x42000000,0,0});
            packet(0x680,{0x42000000,0,0,0,0,0xC2000000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
            if(skinned) {
                packet(0x328,{1});packet(0x1A10,{0x3F000000,0,0,0});
                packet(0x480,{0x3F800000,0,0,0xBF000000,0,0x3F800000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000,
                    0x3F800000,0,0,0x3F000000,0,0x3F800000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
            }
            if(generated_texture) {
                packet(0x288,{0x00000008,0x00001800});packet(0x1E70,{4});packet(0x3C0,{0x2401,0x2401,0x2401,0x2401});
                packet(0x840,{0,0,0,0x3F000000,0,0,0,0,0,0,0,0,0,0,0,0x3F800000});packet(0x420,{1});
                packet(0x6C0,{0x40000000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
            }
            constexpr std::array<Float4,5> positions{{{99,99,99,1},{-1,1,0.5f,1},{1,1,0.5f,1},{1,-1,0.5f,1},{-1,-1,0.5f,1}}};
            const auto first=shifted?1U:0U;
            const auto payload=programmable?Bytes(std::as_bytes(std::span(screen_positions))):Bytes(std::as_bytes(std::span(positions).subspan(shifted?0:1)));
            std::memcpy(memory.access(4096,payload.size()),payload.data(),payload.size());
            packet(0x1720,{4096});packet(0x1760,{0x1042});
            packet(0x1A30,{0x3F800000,0,0,alpha_feedback?0U:0x3F800000U});
            if(programmable) {
                packet(0x1E94,{2});packet(0x1E9C,{0});
                words.push_back(0xB00|(8U<<18));
                for(unsigned instruction=0;instruction<2;++instruction) words.insert(words.end(),vertex_program.tokens[instruction].begin(),vertex_program.tokens[instruction].end());
                packet(0x1EA0,{0});
            }
            packet(0x17FC,{8});
            if(inlined) {
                const auto data=std::span(positions).subspan(1);
                words.push_back(0x40001818|(16U<<18));
                for(const auto& position:data) for(const auto component:position) words.push_back(std::bit_cast<std::uint32_t>(component));
            } else if(indexed) packet(0x1800,{0x00010000,0x00030002},true);
            else packet(0x1810,{0x03000000|first});
            packet(0x17FC,{0});
            if(linear_sample) {
                // A nonuniform GPU image sampled with pixel coordinates catches
                // missing linear-texture normalization; a solid image cannot.
                packet(0x1D98,{31U<<16,63U<<16});packet(0x1D90,{0xFF00FF00});packet(0x1D94,{0xF0});
                packet(0x1D98,{63U<<16,63U<<16});
                constexpr std::array<Float4,4> coordinates{{{0,0,0,1},{64,0,0,1},{64,64,0,1},{0,64,0,1}}};
                std::memcpy(memory.access(8192,sizeof(coordinates)),coordinates.data(),sizeof(coordinates));
                packet(0x1744,{8192});packet(0x1784,{0x1042});
                gpu.capture_frame(directory/(std::string(name)+"-capture"));packet(0x12C,{0});
            }
            if(target_sample) {
                packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x40000,0x50000});
                packet(0x1D94,{0xF0});
                const auto format=rgb565?0x06610520U:alpha_feedback?0x06610020U|(static_cast<unsigned>(feedback_format)<<8):0x00011220U;
                packet(0x1B00,{0x20000,format,0x030303,alpha_feedback?0x40000004U:0x40000000U});
                packet(0x1B10,{(rgb565?128U:256U)<<16,0x01010000});packet(0x1B1C,{(64U<<16)|64U});
                packet(0x1E70,{1});packet(0x288,{0x00000008,0x00001800});
                if(alpha_feedback) {
                    // RGB-only target writes leave stored alpha zero. XRGB
                    // sampling must supply one before the game's alpha kill.
                    packet(0x1A30,{0x3F800000,0x3F800000,0x3F800000,0x3F800000});
                    packet(0x1E60,{1});packet(0xAC0,{0x04080000});packet(0x260,{0x14180000});
                    packet(0x1E40,{0xC00});packet(0xAA0,{0xC00});packet(0x288,{0xC,0x1C80});
                }
                packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
            }
            packet(0x1D70,{123});
            // A flip presents the bound surface even without intervening draws.
            packet(0x12C,{0});packet(0x12C,{0});
            if(unfinished) {
                // A smaller unpresented pass must not replace the final frame.
                packet(0x200,{32U<<16,32U<<16,0x05050213,0x00400040,0x60000,0x70000});
                packet(0x1D98,{31U<<16,31U<<16});packet(0x1D90,{0xFF00FF00});packet(0x1D94,{0xF0});
            }
            const auto bytes=std::as_bytes(std::span(words));std::memcpy(memory.access(0,bytes.size()),bytes.data(),bytes.size());
            gpu.submit(0,static_cast<std::uint32_t>(bytes.size()));gpu.wait();gpu.snapshot();
            if(memory.load<std::uint32_t>(60000)!=123) throw std::runtime_error("GPU semaphore completed without publishing its value");
            statistics=gpu.stats();
            if(statistics.draws!=(target_sample?2U:1U) || statistics.vertices!=(target_sample?8U:4U) || statistics.clears!=(target_sample?2U:1U)+(unfinished?1U:0U)+(linear_sample?1U:0U))
                throw std::runtime_error("GPU packet draw/clear counts differ");
            if(statistics.flips!=2+(linear_sample?1U:0U)) throw std::runtime_error("Repeated GPU flips were not presented");
            const auto image=native.readback();save_png(image,image_path);
            if(surface_clip) {expect(image,32,32,{255,0,0,255});expect(image,3,3,{0,0,255,255});expect(image,52,52,{0,0,255,255});}
            else if(linear_sample) {
                expect(image,8,32,{0,255,0,255});expect(image,55,32,{255,0,0,255});
                const auto capture=directory/(std::string(name)+"-capture");File trace(capture/"draws.jsonl");
                if(trace.size()>1024*1024) throw std::runtime_error("Small GPU capture exceeded its bound");
                const auto trace_bytes=trace.read(0,static_cast<std::size_t>(trace.size()));
                const std::string_view text(reinterpret_cast<const char*>(trace_bytes.data()),trace_bytes.size());
                if(text.find("\"type\":\"draw\"")==text.npos || text.find("\"truncated\":false")==text.npos || !std::filesystem::exists(capture/"frame.png"))
                    throw std::runtime_error("GPU frame capture omitted its draw or image");
            } else if(feedback_format==TextureFormat::argb8) expect(image,32,32,{0,0,255,0});
            else red_pixel(image);
            if(!native.errors().empty()) throw std::runtime_error("GPU packet replay produced D3D11 validation messages");
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{},\"draws\":{},\"vertices\":{}}}",
            json(name),error.empty(),json(error),json(utf8(image_path.wstring())),statistics.draws,statistics.vertices);
    };
    gpu_check("gpu-packets-array-quad",false,false,false);
    gpu_check("gpu-capture-last-presented-frame",false,false,false,false,false,false,false,false,false,true);
    gpu_check("gpu-packets-element16-quad",true,false,false);
    gpu_check("gpu-packets-first-vertex",false,true,false);
    gpu_check("gpu-packets-skin-matrix-aliases",false,false,false,false,true);
    gpu_check("gpu-packets-texgen-matrix-aliases",false,false,false,false,false,true);
    gpu_check("gpu-packets-program-upload",false,false,true);
    gpu_check("gpu-packets-inline-array",false,false,false,true);
    gpu_check("gpu-packets-surface-clip",false,false,false,false,false,false,true);
    gpu_check("gpu-packets-rgb565-z16",false,false,false,false,false,false,false,false,true);
    gpu_check("gpu-packets-render-target-sampling",false,false,false,false,false,false,false,true);
    gpu_check("gpu-packets-rgb565-target-sampling",false,false,false,false,false,false,false,true,true);
    gpu_check("gpu-packets-linear-target-coordinates",false,false,false,false,false,false,false,true,false,false,true);
    gpu_check("gpu-packets-xrgb-feedback-alpha-kill",false,false,false,false,false,false,false,true,false,false,false,TextureFormat::xrgb8);
    gpu_check("gpu-packets-argb-feedback-alpha-kill",false,false,false,false,false,false,false,true,false,false,false,TextureFormat::argb8);

    if(total++) report+=',';
    std::string cubemap_error;
    const auto cubemap_image=directory/"gpu-packets-reflection-cubemap.png";
    try {
        Renderer native(64,64,warp,debug);
        std::vector<std::byte> ram(65536);Memory memory(0,ram);Gpu gpu(native,memory,60000);
        std::vector<std::uint32_t> words;
        const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values) {
            words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18));words.insert(words.end(),values);
        };
        const auto submit=[&] {
            const auto bytes=std::as_bytes(std::span(words));std::memcpy(memory.access(0,bytes.size()),bytes.data(),bytes.size());
            gpu.submit(0,static_cast<std::uint32_t>(bytes.size()));gpu.wait();words.clear();
        };
        packet(0,{0xD});packet(0x1D98,{63U<<16,63U<<16});
        for(unsigned face=0;face<6;++face) {
            packet(0x200,{64U<<16,64U<<16,0x06060213,0x00800080,0x20000+face*8192,0});
            packet(0x1D90,{face_colors[face]});packet(0x1D94,{0xF0});
        }
        packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x40000,0x50000});
        packet(0x1D90,{0xFF000000});packet(0x1D94,{0xF0});
        packet(0x33C,{0x207,0,1,0});
        packet(0x350,{0x8006,0x203,0x01010101,0,255,0x207,0,255,0x1E00,0x1E00,0x1E00});
        packet(0x288,{8,0x1800});packet(0x1E70,{3});
        for(unsigned stage=0;stage<4;++stage) packet(0x1B08+stage*0x40,{0x030303});
        packet(0x1B00,{0x20000,0x06610524,0x030303,0x40000000});packet(0x1B14,{0x01010000});
        packet(0xAF0,{0x3F800000,0x3F800000,0x3F800000,0});packet(0xA20,{0x42000000,0x42000000,0,0});
        packet(0x680,{0x42000000,0,0,0,0,0xC2000000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
        constexpr std::array<Float4,4> positions{{{-1,1,0.5f,1},{1,1,0.5f,1},{1,-1,0.5f,1},{-1,-1,0.5f,1}}};
        std::memcpy(memory.access(4096,sizeof(positions)),positions.data(),sizeof(positions));
        packet(0x1720,{4096});packet(0x1760,{0x1042});
        // A queued scene draw must survive the flush required by the first
        // cubemap copy, just as the game's road draws precede its car.
        packet(0x288,{4,0x1400});packet(0x1E70,{0});
        packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
        packet(0x288,{8,0x1800});packet(0x1E70,{3});
        constexpr std::array<Float4,6> directions{{{1,0,0,1},{-1,0,0,1},{0,1,0,1},{0,-1,0,1},{0,0,1,1},{0,0,-1,1}}};
        const auto sample_face=[&](unsigned face) {
            packet(0x200,{(10U<<16)|face*10U,64U<<16});
            const auto& face_direction=directions[face];
            packet(0x1A90,{std::bit_cast<std::uint32_t>(face_direction[0]),std::bit_cast<std::uint32_t>(face_direction[1]),
                std::bit_cast<std::uint32_t>(face_direction[2]),std::bit_cast<std::uint32_t>(face_direction[3])});
            packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
        };
        for(unsigned face=0;face<6;++face) sample_face(face);
        packet(0x200,{64U<<16,64U<<16});packet(0x12C,{0});submit();
        const auto initial=native.readback();
        expect(initial,62,32,{255,255,255,255});
        if(gpu.stats().draws!=7) throw std::runtime_error("Cubemap resolution dropped a queued draw");
        for(unsigned face=0;face<6;++face) {
            const auto value=face_colors[face];expect(initial,face*10+5,32,{(value>>16)&255,(value>>8)&255,value&255,255});
        }
        // Rewrite one existing face, then resample the same cached cubemap.
        packet(0x200,{64U<<16,64U<<16,0x06060213,0x00800080,0x20000+5*8192,0});
        packet(0x1D90,{0xFFFFFFFF});packet(0x1D94,{0xF0});
        packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x40000,0x50000});
        sample_face(5);packet(0x200,{64U<<16,64U<<16});packet(0x12C,{0});submit();
        const auto updated=native.readback();save_png(updated,cubemap_image);
        expect(updated,55,32,{255,255,255,255});expect(updated,5,32,{255,0,0,255});
        if(!native.errors().empty()) throw std::runtime_error("Cubemap feedback produced D3D11 validation messages");
        ++passed;
    } catch(const std::exception& failure) {cubemap_error=failure.what();}
    report+=std::format("{{\"name\":\"gpu-packets-reflection-cubemap\",\"passed\":{},\"error\":{},\"image\":{}}}",
        cubemap_error.empty(),json(cubemap_error),json(utf8(cubemap_image.wstring())));

    const auto messages = renderer.errors();
    report += std::format("],\"passed\":{},\"total\":{},\"shader_compilations\":{},\"debug_messages\":[",
                          passed == total && messages.empty(),total,renderer.stats().shader_compilations);
    for (std::size_t i=0;i<messages.size();++i) { if(i)report+=',';report+=json(messages[i]); }
    report += "]}";
    write_text(directory / "report.json",report+'\n',false);
    return report;
}
std::string render_texture(const std::filesystem::path& input, TextureFormat format,
                           std::uint32_t width, std::uint32_t height,
                           const std::filesystem::path& output, bool warp) {
    File file(input);
    if (file.size()>64*1024*1024) throw std::runtime_error("Texture file exceeds 64 MiB");
    const auto bytes = file.read(0,static_cast<std::size_t>(file.size()));
    Renderer renderer(width,height,warp);
    const auto state = textured(renderer.upload({width,height,format,bytes}));
    renderer.prepare(state); renderer.begin(); renderer.draw(state,quad()); renderer.end();
    save_png(renderer.readback(),output);
    return "{\"format\":\"b2-texture-render-v1\",\"adapter\":"+json(renderer.adapter())+
        ",\"image\":"+json(utf8(output.wstring()))+",\"source_bytes\":"+std::to_string(bytes.size())+'}';
}
std::string render_dictionary(const std::filesystem::path& input,
                              const std::filesystem::path& directory, bool warp) {
    if (std::filesystem::exists(directory)) throw std::runtime_error("Dictionary output directory already exists");
    TextureDictionary dictionary(input);
    Renderer renderer(64,64,warp);
    std::filesystem::create_directories(directory);
    std::string report="{\"format\":\"b2-dictionary-render-v1\",\"input\":"+json(utf8(input.wstring()))+
        ",\"adapter\":"+json(renderer.adapter())+",\"game_booted\":false,\"textures\":[";
    unsigned count=0, failures=0;
    for (const auto& texture : dictionary.textures()) {
        if(count)report+=',';
        // Indices supply safe filenames even when native names are duplicated or contain paths.
        const auto output=directory/std::format("{:04}.png",count++);
        std::string error;
        bool begun=false;
        try {
            if (texture.raster_format & 0x6000) throw std::runtime_error("Paletted texture support is pending");
            const auto bytes=dictionary.pixels(texture);
            const auto state=textured(renderer.upload({texture.width,texture.height,
                static_cast<TextureFormat>(texture.format),bytes,texture.mip_levels}));
            renderer.resize(texture.width,texture.height); renderer.prepare(state);
            renderer.begin(); begun=true;
            renderer.draw(state,quad()); renderer.end(); begun=false;
            save_png(renderer.readback(),output);
        } catch(const std::exception& failure) {
            error=failure.what(); ++failures;
            if(begun)renderer.end();
        }
        report+=std::format("{{\"name\":{},\"width\":{},\"height\":{},\"nv097_format\":{},\"passed\":{},\"error\":{},\"image\":{}}}",
            json(texture.name),texture.width,texture.height,texture.format,error.empty(),json(error),json(utf8(output.wstring())));
    }
    report+=std::format("],\"passed\":{},\"total\":{},\"failures\":{},\"shader_compilations\":{}}}",
                        failures==0,count,failures,renderer.stats().shader_compilations);
    write_text(directory/"report.json",report+'\n',false);
    return report;
}
}
