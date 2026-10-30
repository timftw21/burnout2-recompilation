#include "render.h"
#include "assets.h"
#include "gpu.h"
#include "cpu.h"
#include <d3d11.h>
#include "com_ptr.h"
#include <algorithm>
#include <bit>
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
                         const std::function<void()>& submit = {},bool publish=false) {
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
            if(publish) renderer.publish_frame();
            const auto image = publish?renderer.read_frame():renderer.readback(); save_png(image, image_path);
            validate(image);
            ++passed;
        } catch (const std::exception& failure) {
            error = failure.what();
            if (begun) renderer.end();
        }
        const auto timing = renderer.stats();
        report += std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{},\"draws\":{},\"vertices\":{},\"gpu_ms\":{},\"elapsed_ms\":{}}}",
            json(name), error.empty(), json(error), json(path_text(image_path)), timing.draws, timing.vertices,
            timing.gpu_timing_valid ? std::format("{}", timing.gpu_ms) : "null",
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count());
    };
    const auto red = quad({1,0,0,1}), blue = quad({0,0,1,1}), white = quad();
    const auto red_pixel = [](const Image& image) { expect(image, 32, 32, {255,0,0,255}); expect(image, 3, 3, {255,0,0,255}); };
    auto state = colored();
    run("colored-quads", state, red, red_pixel);
    for(unsigned samples:{1U,4U}) for(unsigned displays:{1U,6U}) {
        if(samples>1 && !renderer.supports_msaa(samples)) continue;
        run(std::format("boost-hud-history-{}x-{}hz",samples,displays*60),state,white,[&](const Image&) {
            Performance counters;
            Renderer native(64,64,warp,debug);
            if(samples>1) native.enhancements(5,0);
            const std::array targets{native.create_target(64,64),native.create_target(64,64)};
            auto world=colored();world.depth_test=world.depth_write=true;
            auto hud=colored();hud.blend=true;hud.blend_source=0x302;hud.blend_destination=0x303;
            hud.color_mask=0x00010101;hud.scissor_test=true;hud.scissor={8,8,24,24};
            auto text=colored();text.scissor_test=true;text.scissor={40,8,56,24};
            auto hidden=world;hidden.depth_write=false;
            native.prepare(world);native.prepare(hud);native.prepare(text);native.prepare(hidden);
            for(auto target:targets) {native.set_target(target);native.begin(0xFFFFFFFF);native.end();}
            native.performance(&counters);
            const auto resources=native.resources();
            const auto frames=displays*4;
            for(unsigned frame=0;frame<frames;++frame) {
                const auto target=targets[frame&1];
                auto feedback=textured(targets[(frame+1)&1]);feedback.blend=true;
                feedback.blend_source=0x302;feedback.blend_destination=0x303;feedback.color_mask=0x00010101;
                native.prepare(feedback);native.set_target(target);native.begin();
                native.draw(world,frame<displays*3?white:blue);
                native.draw(feedback,quad({1,1,1,float(std::pow(0.7,1.0/displays))}));native.end();
                if(frame) expect(native.read_target(0),48,16,{255,0,0,255}); // The completed HUD must survive until replacement.
                native.begin_overlay();native.begin(0,1,false);
                native.draw(hud,quad({0,0,0,0.3f}));native.draw(text,red);
                // The presentation copy must still use the scene's depth.
                native.draw(hidden,quad({0,1,0,1},0.75f));native.end();native.end_overlay();
                native.publish_frame();
                const auto image=native.readback();
                expect(image,48,16,{255,0,0,255});
                const auto scene=pixel(image,32,32);
                expect(image,16,16,{unsigned(std::lround(scene[0]*0.7)),unsigned(std::lround(scene[1]*0.7)),179,255},2);
                if(frame<displays*3) expect(image,32,32,{255,255,255,255});
                if(frame+1==frames) {
                    expect(image,32,32,{179,179,255,255},3);
                    save_png(image,directory/std::format("boost-hud-{}x-{}hz.png",samples,displays*60));
                    native.set_target(target);const auto history=native.readback();
                    expect(history,16,16,scene);expect(history,48,16,scene);native.set_target();
                    native.restore_feedback();const auto transition=native.read_target(target);
                    if(transition.rgba!=image.rgba) throw std::runtime_error("Loading transition lost the completed HUD");
                }
            }
            const auto after=native.resources();const auto counts=native.stats().counters;
            if(after.targets!=resources.targets || after.target_bytes!=resources.target_bytes ||
               counts[unsigned(PerfMetric::target_creations)] || counts[unsigned(PerfMetric::target_copies)]!=frames*2 ||
               counts[unsigned(PerfMetric::draws)]!=frames*5)
                throw std::runtime_error("Boost HUD feedback exceeded its existing buffers, two copies or five draws per frame");
            if(!native.errors().empty()) throw std::runtime_error("Boost HUD feedback produced graphics validation errors");
        });
    }
    for(unsigned samples:{1U,4U}) for(unsigned refresh:{60U,144U,360U}) {
        if(samples>1 && !renderer.supports_msaa(samples)) continue;
        run(std::format("reward-text-history-{}x-{}hz",samples,refresh),state,white,[&](const Image&) {
            Renderer native(64,64,warp,debug);
            if(samples>1) native.enhancements(5,0);
            const std::array targets{native.create_target(64,64),native.create_target(64,64)};
            const auto original_budget=3ULL*64*64*(samples>1?8*samples+8:12);
            if(native.resources().target_bytes>original_budget+64ULL*64*samples*4)
                throw std::runtime_error("Framebuffer feedback exceeded one RGBA8 colour-only scratch image");
            if(refresh==144) native.ambient_occlusion(1,0);
            auto text=colored();text.scissor_test=true;
            auto dark=text;dark.scissor={0,0,64,16};
            auto light=text;light.scissor={0,32,64,64};
            dark.depth_test=dark.depth_write=light.depth_test=light.depth_write=true;
            text.blend=true;text.blend_source=0x302;text.blend_destination=0x303;text.color_mask=0x00010101;
            native.prepare(dark);
            const auto background=[&] {
                native.camera(1,0.1f,100,1.047197551f);
                native.begin(0xFF807060);native.draw(dark,quad({0,0,0,1}));native.draw(light,white);
            };
            for(auto target:targets) {native.set_target(target);background();native.end();}
            const auto alpha=float(std::pow(0.7,60.0/refresh));
            const auto first=(refresh+59)/60;
            const auto program=native.prepare_program(screen_vertex_programs()[0]);
            VertexLayout attributes{};attributes[0]={0,offsetof(Vertex,position),0x42};
            attributes[3]={0,offsetof(Vertex,color),0x42};attributes[9]={0,offsetof(Vertex,uv),0x42};
            const auto layout=native.prepare_layout(program,attributes);
            VertexConstants constants{};constants[0]={1,1,16777215,1};constants[1]={0.53125f,0.53125f,0,0};
            ProgramView view;view.depth_scale=16777215;ProgramVertex current;
            auto history_vertices=quad({1,1,1,alpha}),text_vertices=red;
            for(auto* vertices:{&history_vertices,&text_vertices}) for(auto& vertex:*vertices) {
                vertex.position[0]=(vertex.position[0]+1)*32;vertex.position[1]=(1-vertex.position[1])*32;
            }
            const std::array history_stream{VertexStream{std::as_bytes(std::span(history_vertices)),sizeof(Vertex)}};
            const std::array text_stream{VertexStream{std::as_bytes(std::span(text_vertices)),sizeof(Vertex)}};
            const auto resources=native.resources();
            // Include the full fade on black, midtone and white backgrounds.
            // One tick misses faint history trapped by RGB8 rounding.
            for(unsigned frame=0;frame<=refresh/2;++frame) {
                auto feedback=textured(targets[(frame+1)&1]);feedback.blend=true;
                feedback.blend_source=0x302;feedback.blend_destination=0x303;feedback.color_mask=0x00010101;
                text.scissor=frame?std::array<std::int32_t,4>{8,8,24,56}:std::array<std::int32_t,4>{40,8,56,56};
                const auto feedback_bindings=native.prepare(feedback),text_bindings=native.prepare(text);
                native.set_target(targets[frame&1]);background();
                const std::array batch{
                    Renderer::StreamDraw{&feedback,&constants,&view,history_stream,&current,{},&feedback_bindings,layout,4},
                    Renderer::StreamDraw{&text,&constants,&view,text_stream,&current,{},&text_bindings,layout,4}};
                native.draw_streams(batch);native.end();native.copy_to_main();native.publish_frame();
                if(frame==first) {
                    const auto image=native.readback();const auto fade=std::pow(0.7,60.0*frame/refresh);
                    const auto blend=[&](unsigned background,unsigned text) {return unsigned(std::lround(background+(double(text)-background)*fade));};
                    expect(image,16,12,{255,0,0,255});
                    expect(image,48,12,{blend(0,255),0,0,255},3);
                    expect(image,48,24,{blend(128,255),blend(112,0),blend(96,0),255},3);
                    expect(image,48,48,{255,blend(255,0),blend(255,0),255},3);
                    save_png(image,directory/std::format("reward-text-{}x-{}hz.png",samples,refresh));
                }
            }
            const auto settled=native.readback();
            save_png(settled,directory/std::format("reward-text-settled-{}x-{}hz.png",samples,refresh));
            for(unsigned y=8;y<56;++y) for(unsigned x=32;x<56;++x)
                expect(settled,x,y,y<16?std::array{0U,0U,0U,255U}:y<32?std::array{128U,112U,96U,255U}:std::array{255U,255U,255U,255U},1);
            if(refresh==144) {
                const auto composed=native.read_frame();
                expect(composed,16,12,{255,0,0,255});expect(composed,48,12,{0,0,0,255},1);
                expect(composed,48,24,{128,112,96,255},1);expect(composed,48,48,{255,255,255,255},1);
            }
            if(refresh==360) {
                // Even the weakest non-opaque byte alpha must erase a one-byte
                // disturbance on both black and white within one second.
                for(auto target:targets) {
                    native.set_target(target);background();
                    native.draw(dark,quad({1.0f/255,1.0f/255,1.0f/255,1},0.25f));
                    native.draw(light,quad({254.0f/255,254.0f/255,254.0f/255,1},0.25f));native.end();
                }
                for(unsigned frame=0;frame<refresh;++frame) {
                    auto feedback=textured(targets[(frame+1)&1]);feedback.blend=true;
                    feedback.blend_source=0x302;feedback.blend_destination=0x303;feedback.color_mask=0x00010101;
                    native.prepare(feedback);native.set_target(targets[frame&1]);background();
                    native.draw(feedback,quad({1,1,1,254.0f/255}));native.end();native.copy_to_main();native.publish_frame();
                }
                const auto faint=native.readback();
                save_png(faint,directory/std::format("reward-last-byte-{}x.png",samples));
                for(unsigned y=0;y<64;++y) for(unsigned x=0;x<64;++x)
                    expect(faint,x,y,y<16?std::array{0U,0U,0U,255U}:y<32?std::array{128U,112U,96U,255U}:std::array{255U,255U,255U,255U},0);
            }
            if(native.resources().target_bytes!=resources.target_bytes || native.resources().targets!=resources.targets)
                throw std::runtime_error("Framebuffer feedback allocated additional images");
            if(!native.errors().empty()) throw std::runtime_error("Reward text feedback produced graphics validation errors");
        });
    }
    run("gpu-boost-overlay-flip-and-snapshot",state,white,[&](const Image&) {
        Renderer native(64,64,warp,debug);
        std::vector<std::byte> ram(65536);Memory memory(0,ram);Gpu gpu(native,memory,60000);
        std::vector<std::uint32_t> words;
        const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values) {
            words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18));words.insert(words.end(),values);
        };
        const auto submit=[&] {
            const auto bytes=std::as_bytes(std::span(words));std::memcpy(memory.access(0,bytes.size()),bytes.data(),bytes.size());
            gpu.submit(0,static_cast<std::uint32_t>(bytes.size()),false);words.clear();
        };
        packet(0,{0xD});packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x20000,0x30000});
        for(bool flip:{true,false}) {
            packet(0x1D98,{63U<<16,63U<<16});packet(0x1D90,{0xFFFFFFFF});packet(0x1D94,{0xF0});submit();
            gpu.begin_overlay();
            packet(0x1D98,{(23U<<16)|8U,(23U<<16)|8U});packet(0x1D90,{0xFF000000});packet(0x1D94,{0xF0});
            if(flip) packet(0x12C,{0});submit();gpu.snapshot();
            const auto image=native.readback();expect(image,16,16,{0,0,0,255});expect(image,32,32,{255,255,255,255});
        }
        if(gpu.stats().flips!=1 || !native.errors().empty()) throw std::runtime_error("GPU overlay completion lost its frame or produced graphics errors");
    });
    auto diagonal=white;diagonal[1].position={1,-1,0.5f,1};diagonal[2].position={-1,-1,0.5f,1};
    auto edge_state=colored();edge_state.primitive=5;
    Image original_edge;
    run("antialiasing-off",edge_state,std::span(diagonal).first(3),[&](const Image& image) {original_edge=image;},{},{},{},true);
    const auto target_budget=renderer.resources().target_bytes;
    for(unsigned quality=1;quality<=3;++quality) {
        renderer.enhancements(quality,0);
        run(std::format("fxaa-quality-{}",quality),edge_state,std::span(diagonal).first(3),[&](const Image& image) {
            if(renderer.readback().rgba!=original_edge.rgba) throw std::runtime_error("Antialiasing changed original game feedback");
            unsigned changed=0;
            for(std::size_t i=0;i<image.rgba.size();i+=4) {
                changed+=image.rgba[i]!=original_edge.rgba[i];
                if(image.rgba[i+3]!=original_edge.rgba[i+3]) throw std::runtime_error("Antialiasing changed alpha");
            }
            if(changed<32) throw std::runtime_error("Antialiasing did not smooth the diagonal");
            expect(image,3,60,{255,255,255,255});expect(image,60,3,{0,0,0,255});
        },{},{},{},true);
    }
    renderer.resize(48,32);
    run("fxaa-resize",colored(),red,[](const Image& image) {
        if(image.width!=48 || image.height!=32) throw std::runtime_error("Antialiasing retained old dimensions");
        expect(image,24,16,{255,0,0,255});
    },{},{},{},true);
    renderer.reset(2);
    run("fxaa-scaled-session-reset",colored(),red,[](const Image& image) {
        if(image.width!=96 || image.height!=64) throw std::runtime_error("Antialiasing ignored internal resolution");
        expect(image,48,32,{255,0,0,255});
    },{},{},{},true);
    renderer.enhancements(0,0);renderer.reset(1);renderer.resize(64,64);
    if(renderer.resources().target_bytes!=target_budget) throw std::runtime_error("Disabled antialiasing retained render storage");
    auto msaa_diagonal=white;msaa_diagonal[2].position={-1,-1,0.5f,1};
    for(unsigned samples:{2U,4U,8U}) {
        if(!renderer.supports_msaa(samples)) continue;

        const auto mode=3U+unsigned(std::countr_zero(samples));renderer.enhancements(mode,0);
        run(std::format("msaa-{}x-geometry",samples),edge_state,std::span(msaa_diagonal).first(3),[&](const Image& image) {
            unsigned coverage=0;
            for(unsigned y=0;y<64;++y) for(unsigned x=0;x<64;++x) {const auto value=pixel(image,x,y)[0];coverage+=value>0 && value<255;}
            if(coverage<32) throw std::runtime_error("MSAA did not smooth geometry coverage");
            expect(image,3,3,{255,255,255,255});expect(image,60,60,{0,0,0,255});
            Microsoft::WRL::ComPtr<ID3D11RenderTargetView> target;renderer.native_context()->OMGetRenderTargets(1,&target,nullptr);
            Microsoft::WRL::ComPtr<ID3D11Resource> resource;target->GetResource(&resource);Microsoft::WRL::ComPtr<ID3D11Texture2D> texture;resource.As(&texture);
            D3D11_TEXTURE2D_DESC description{};texture->GetDesc(&description);
            if(description.SampleDesc.Count!=samples) throw std::runtime_error("MSAA sample count differs from the selected mode");
        },{},{},{},true);
        renderer.resize(48,32);
        run(std::format("msaa-{}x-resize",samples),colored(),red,[](const Image& image) {
            if(image.width!=48 || image.height!=32) throw std::runtime_error("MSAA history retained old dimensions");expect(image,24,16,{255,0,0,255});
        },{},{},{},true);
        renderer.enhancements(0,0);renderer.resize(64,64);
    }

    if(renderer.resources().target_bytes!=target_budget) throw std::runtime_error("Disabled MSAA retained render storage");
    if(renderer.supports_msaa(4)) {
        renderer.enhancements(5,0);
        std::vector<std::byte> checker(64*64*4);
        for(unsigned y=0;y<64;++y) for(unsigned x=0;x<64;++x) {
            const auto value=std::byte(((x^y)&1)?255:0);const auto offset=(y*64+x)*4;
            checker[offset]=checker[offset+1]=checker[offset+2]=value;checker[offset+3]=std::byte{255};
        }
        const auto texture=renderer.upload({64,64,TextureFormat::argb8_linear,checker});
        run("msaa-preserves-fine-texture-detail",textured(texture),white,[](const Image& image) {
            for(unsigned y=4;y<60;++y) for(unsigned x=4;x<60;++x) {
                const auto value=((x^y)&1)?255U:0U;expect(image,x,y,{value,value,value,255},0);
            }
        },{},{},{},true);
        renderer.reset(1);
        const auto source=renderer.create_target(64,64),copy=renderer.create_target(64,64);
        renderer.set_target(source);renderer.prepare(edge_state);renderer.begin();renderer.draw(edge_state,std::span(diagonal).first(3));renderer.end();
        const auto scene=renderer.readback();renderer.copy_target(copy);renderer.copy_to_main();renderer.publish_frame();
        if(renderer.read_frame().rgba!=scene.rgba) throw std::runtime_error("MSAA presentation lost sample coverage");
        auto feedback=textured(copy);renderer.prepare(feedback);
        run("msaa-render-target-feedback",feedback,white,[&](const Image& image) {
            if(image.rgba!=scene.rgba) throw std::runtime_error("MSAA render-target sampling lost coverage");
        },{},{},{},true);
        const std::array<std::uint32_t,6> faces{copy,copy,copy,copy,copy,copy};const auto cube=renderer.copy_cubemap(faces);
        auto cubed=textured(cube);cubed.texture_modes[0]=3;auto face=white;for(auto& vertex:face) vertex.uv[0]={-0.75f,-0.75f,1,1};
        run("msaa-cubemap-resolve",cubed,face,[](const Image& image) {expect(image,32,32,{255,255,255,255});});
        renderer.reset(1);renderer.enhancements(0,0);
    }
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

    {
        const std::array<std::uint32_t,4> corners{0xFFFF0000,0xFF00FF00,0xFF0000FF,0xFFFFFFFF};
        auto point=colored();point.primitive=1;point.point_size=16;point.point_sprite=true;point.cull=true;
        point.textures[3]=renderer.upload({2,2,TextureFormat::argb8,std::as_bytes(std::span(corners))});
        point.texture_modes[3]=1;point.texture_filter[3]=0x01010000;point.final_inputs={11,0x1B00};
        const std::array<Vertex,1> center{};
        auto lines=colored();lines.primitive=2;renderer.prepare(lines);
        std::array<Vertex,2> rain{};
        rain[0].position={-0.75f,-0.75f,0.5f,1};rain[1].position={0.75f,-0.75f,0.5f,1};
        for(auto& vertex:rain) vertex.color={0,0,1,1};
        run("point-sprite-texture-and-lines",point,center,[](const Image& image) {
            expect(image,26,26,{255,0,0,255});expect(image,37,26,{0,255,0,255});
            expect(image,26,37,{0,0,255,255});expect(image,37,37,{255,255,255,255});
            expect(image,20,32,{0,0,0,255});
            if(pixel(image,32,55)[2]!=255 && pixel(image,32,56)[2]!=255)
                throw std::runtime_error("Point expansion leaked into subsequent line rendering");
        },{},{},[&] {renderer.draw(point,center);renderer.draw(lines,rain);});
        auto occluder=colored();occluder.depth_test=occluder.depth_write=true;renderer.prepare(occluder);
        point.depth_test=true;
        auto behind=center;behind[0].position[2]=0.75f;
        run("point-sprite-depth",point,behind,[](const Image& image) {expect(image,32,32,{0,0,255,255});},
            {},{},[&] {renderer.draw(occluder,blue);renderer.draw(point,behind);});

        const auto fixed=renderer.prepare_fixed({});
        VertexConstants constants{};
        constants[0]={1,0,0,0};constants[1]={0,1,0,0};constants[2]={0,0,0,0.5f};constants[3]={0,0,0,1};
        for(unsigned i=0;i<4;++i) constants[8+i][i]=1;
        constants[59]={32.53125f,32.53125f,0,0};
        auto scaled=colored();scaled.primitive=1;scaled.point_scale=true;
        scaled.point_parameters={0,0,1,16,16,16,0,0};
        for(unsigned distance:{2U,4U}) {
            std::array<ProgramVertex,1> vertex{};
            vertex[0].attributes[0]={0,0,std::sqrt(float(distance*distance-1)),1};
            vertex[0].attributes[3]={1,0,0,1};
            run(std::format("point-attenuation-{}",distance),scaled,center,[&](const Image& image) {
                unsigned covered=0;
                for(std::size_t offset=0;offset<image.rgba.size();offset+=4) covered+=image.rgba[offset]==std::byte{255};
                const auto side=16/distance;
                if(covered!=side*side) throw std::runtime_error("Point distance attenuation has incorrect coverage");
            },{},{},[&] {renderer.draw_program(scaled,fixed,constants,{},vertex);},true);
        }
    }

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
    const auto mip_texture=renderer.upload({4,4,TextureFormat::argb8,mip_bytes,3});
    run("mip-chain", textured(mip_texture), minified,
        [](const Image& image) { expect(image,32,32,{0,0,255,255}); });
    auto sampling=textured(mip_texture);sampling.texture_filter[0]=0x02061E00; // -2 levels, as on the captured road.
    run("mip-negative-detail-bias",sampling,minified,red_pixel);
    sampling.texture_filter[0]=0x02061F80;
    run("mip-fractional-detail-bias",sampling,minified,
        [](const Image& image){expect(image,32,32,{0,128,128,255},3);});
    sampling.texture_filter[0]=0x02060100;
    auto unbiased=white;for(auto& vertex:unbiased) {vertex.uv[0][0]*=16;vertex.uv[0][1]*=16;}
    run("mip-positive-detail-bias",sampling,unbiased,[](const Image& image){expect(image,32,32,{0,255,0,255});});
    sampling.texture_filter[0]=0x02020000;
    run("mip-disabled-by-minification-filter",sampling,minified,red_pixel);
    sampling.texture_filter[0]=0x02060000;sampling.texture_control[0]=1U<<6;
    run("mip-maximum-level",sampling,minified,[](const Image& image){expect(image,32,32,{0,255,0,255});});
    sampling.texture_control[0]=(1U<<18)|0x0003FFC0;
    run("mip-minimum-level",sampling,white,[](const Image& image){expect(image,32,32,{0,255,0,255});});
    sampling.texture_control[0]=0x0003FFF0;
    auto grazing=white;for(auto& vertex:grazing) vertex.uv[0][0]*=64;
    run("mip-anisotropic-detail",sampling,grazing,[](const Image& image){
        const auto actual=pixel(image,32,32);
        if(actual[0]<200 || actual[2]>20) throw std::runtime_error("Grazing texture lost its base-level detail");
    });
    sampling.texture_filter[0]=0x02063DBB;sampling.texture_control[0]=0x4003FFD0;
    for(unsigned anisotropy:{0U,1U,2U,4U,8U,16U}) {
        renderer.enhancements(0,anisotropy);
        run(std::format("anisotropic-override-{}",anisotropy),sampling,grazing,[](const Image&){},[&] {
            Microsoft::WRL::ComPtr<ID3D11SamplerState> native;renderer.native_context()->PSGetSamplers(0,1,&native);
            D3D11_SAMPLER_DESC description{};native->GetDesc(&description);
            const auto expected=anisotropy?anisotropy:2;
            // MaxAnisotropy is inactive for linear filtering; the driver can
            // canonicalize that unused descriptor field to zero.
            if((expected>1 && description.MaxAnisotropy!=expected) ||
               description.Filter!=(expected>1?D3D11_FILTER_ANISOTROPIC:D3D11_FILTER_MIN_MAG_MIP_LINEAR) ||
               description.MipLODBias!=-581.0f/256 || description.MinLOD!=0 || description.MaxLOD!=4095)
                throw std::runtime_error(std::format("Anisotropic sampler: filter 0x{:X}, strength {}, bias {}, mip range {}..{}; expected strength {}",
                    unsigned(description.Filter),description.MaxAnisotropy,description.MipLODBias,description.MinLOD,description.MaxLOD,expected));
        });
    }
    sampling.texture_filter[0]=0x01010000;
    run("anisotropic-preserves-point-sampling",sampling,grazing,red_pixel,[&] {
        Microsoft::WRL::ComPtr<ID3D11SamplerState> native;renderer.native_context()->PSGetSamplers(0,1,&native);
        D3D11_SAMPLER_DESC description{};native->GetDesc(&description);
        if(description.Filter!=D3D11_FILTER_MIN_MAG_MIP_POINT) throw std::runtime_error("Anisotropic override changed point sampling");
    });
    renderer.enhancements(0,0);
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

    const auto growth_check=[&](bool stencil,bool rgb565=false,unsigned scale=1,unsigned msaa=1,bool per_sample=false) {
        std::string name=rgb565?"render-target-growth-rgb565-z16":stencil?"render-target-growth-stencil":"render-target-growth-depth-color";
        if(scale>1) name+=std::format("-{}x",scale);
        if(msaa>1) name+=std::format("-msaa-{}x",msaa);
        if(per_sample) name+="-individual-samples";
        if(total++) report+=',';
        std::string error;const auto image_path=directory/(name+".png");
        try {
            Renderer native(64,64,warp,debug,scale);
            if(msaa>1) native.enhancements(3U+unsigned(std::countr_zero(msaa)),0);
            const auto target=native.create_target(32,32,
                rgb565?SurfaceColor::rgb565:SurfaceColor::rgba8,rgb565?SurfaceDepth::z16:SurfaceDepth::z24s8);
            native.set_target(target);
            auto state=colored();state.depth_test=!stencil;
            state.stencil_test=stencil;state.stencil_function=0x202;state.stencil_reference=0xA5;
            Image reference;
            if(per_sample) {
                auto source=colored();source.primitive=5;source.depth_test=true;
                source.stencil_test=stencil;source.stencil_function=0x207;source.stencil_reference=0xA5;source.stencil_pass=0x1E01;
                auto shape=msaa_diagonal;for(auto& vertex:shape) vertex.position[2]=0.25f;
                const auto initialize=[&] {native.prepare(source);native.begin();native.draw(source,std::span(shape).first(3));native.end();};
                initialize();native.prepare(state);native.begin(0,1,false);native.draw(state,red);native.end();reference=native.readback();
                initialize();
            } else {native.begin(0xFF0000FF,0.25f);native.clear_stencil(0xA5);native.end();}
            native.grow_target(target,64,64);
            native.prepare(state);native.begin(0,1,false);native.draw(state,red);native.end();native.copy_to_main();
            const auto image=native.readback();save_png(image,image_path);
            if(image.width!=64*scale || image.height!=64*scale) throw std::runtime_error("Scaled target dimensions differ");
            if(per_sample) {
                for(unsigned y=0;y<32;++y) for(unsigned x=0;x<32;++x) expect(image,x,y,pixel(reference,x,y),0);
            } else expect(image,16*scale,16*scale,stencil?std::array<unsigned,4>{255,0,0,255}:std::array<unsigned,4>{0,0,255,255});
            expect(image,48*scale,48*scale,stencil?std::array<unsigned,4>{0,0,0,0}:std::array<unsigned,4>{255,0,0,255});
            if(const auto messages=native.errors();!messages.empty()) throw std::runtime_error("Target growth: "+messages.front());
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{}}}",json(name),error.empty(),json(error),json(path_text(image_path)));
    };
    growth_check(false);growth_check(true);growth_check(false,true);
    for(unsigned samples:{2U,4U,8U}) if(renderer.supports_msaa(samples)) {
        growth_check(false,false,1,samples);growth_check(true,false,1,samples);growth_check(false,true,1,samples);
        growth_check(false,false,1,samples,true);growth_check(true,false,1,samples,true);
    }
    for(unsigned scale=2;scale<=4;++scale) {growth_check(false,false,scale);growth_check(true,false,scale);growth_check(false,true,scale);}

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
    for(unsigned scale=2;scale<=4;++scale) {
        if(total++) report+=',';
        const auto name=std::format("internal-resolution-{}x",scale);
        const auto image_path=directory/(name+".png");
        std::string error;
        try {
            Renderer native(64,64,warp,debug,scale);
            const auto target=native.create_target(64,64),copy=native.create_target(64,64);
            const auto program=native.prepare_program(vertex_program);
            auto clipped=colored();clipped.scissor_test=true;clipped.scissor={16,8,48,56};native.prepare(clipped);
            native.set_target(target);native.begin();native.draw_program(clipped,program,context,view,program_vertices);
            constexpr std::array<std::int32_t,4> rectangle{8,8,24,24};
            native.clear_buffers(0xF0,0xFF00FF00,1,0,rectangle);native.end();native.copy_target(copy);
            const auto feedback=textured(copy);native.prepare(feedback);native.set_target();native.begin();native.draw(feedback,white);native.end();
            const auto image=native.readback();save_png(image,image_path);
            expect(image,12*scale,12*scale,{0,255,0,255});expect(image,32*scale,32*scale,{255,0,0,255});expect(image,2*scale,2*scale,{0,0,0,255});
            const auto messages=native.errors();if(!messages.empty()) throw std::runtime_error("Scaled feedback: "+messages.front());
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{}}}",json(name),error.empty(),json(error),json(path_text(image_path)));
    }
    // SDK passthrough at XBE 00223108, identical to UI draw 546 in the user's
    // frame-51886055200 capture. Its constants include the NV2A pixel origin.
    const auto& ui_program=screen_vertex_programs()[0];
    VertexConstants ui_constants{};ui_constants[0]={1,1,16777215,1};ui_constants[1]={0.53125f,0.53125f,0,0};
    std::array<ProgramVertex,4> ui_vertices{};
    struct UiVertex {Float4 position;std::array<float,2> uv;std::uint32_t color;};
    static_assert(sizeof(UiVertex)==28);
    std::array<UiVertex,4> ui_stream{};
    constexpr Float4 ui_positions[]={{16,16,0.5f,1},{48,16,0.5f,1},{48,48,0.5f,1},{16,48,0.5f,1}};
    for(unsigned i=0;i<4;++i) {ui_vertices[i].attributes[0]=ui_positions[i];ui_vertices[i].attributes[3]={1,0,0,1};ui_stream[i]={ui_positions[i],{},0xFF00FF00};}
    ProgramView ui_view;ui_view.depth_scale=16777215;
    run("sdk-textured-zero-rhw",colored(),red,[&](const Image&) {
        Renderer native(64,64,warp,debug);
        auto pixels=solid(8,8,0xFFFF0000);
        for(unsigned y=0;y<8;++y) for(unsigned x=0;x<8;++x) {
            const auto color=y<4?(x<4?0xFFFF0000U:0xFF00FF00U):(x<4?0xFF0000FFU:0xFFFFFFFFU);
            std::memcpy(pixels.data()+(y*8+x)*4,&color,4);
        }
        auto state=textured(native.upload({8,8,TextureFormat::argb8_linear,pixels}));
        state.final_inputs={0x130C0300,0x00001C80};state.color_outputs[0]=state.alpha_outputs[0]=0xC00;
        state.fog_enable=true;state.fog_color=0xFF424B5B;
        state.fog_parameters={std::bit_cast<float>(0x4002FCE5U),std::bit_cast<float>(0xB9F4C401U)};
        const auto program=native.prepare_program(ui_program);
        VertexLayout attributes{};attributes[0]={0,0,0x42};attributes[9]={0,16,0x22};attributes[3]={0,24,0x40};
        const auto layout=native.prepare_layout(program,attributes);
        auto vertices=ui_stream;
        constexpr std::array<float,2> coordinates[]={{0,0},{1,0},{1,1},{0,1}};
        for(unsigned i=0;i<4;++i) {vertices[i].uv=coordinates[i];vertices[i].color=0xFFFFFFFF;}
        const std::array<VertexStream,1> streams{{{std::as_bytes(std::span(vertices)),sizeof(UiVertex)}}};
        for(float rhw:{1.0f,0.0f}) {
            for(auto& vertex:vertices) vertex.position[3]=rhw;
            native.prepare(state);native.begin();native.draw_streams(state,layout,ui_constants,ui_view,streams,{},4);native.end();
            const auto image=native.readback();
            save_png(image,directory/(rhw?"sdk-textured-rhw-one.png":"sdk-textured-rhw-zero.png"));
            expect(image,24,24,{255,0,0,255});expect(image,40,24,{0,255,0,255});
            expect(image,24,40,{0,0,255,255});expect(image,40,40,{255,255,255,255});
        }
        if(!native.errors().empty()) throw std::runtime_error("Textured zero RHW produced graphics validation errors");
    });
    run("sampler-cache-lifetime",colored(),red,[&](const Image&) {
        Renderer native(64,64,warp,debug);
        auto state=textured(native.upload({4,4,TextureFormat::argb8,mip_bytes,3}));
        state.texture_filter[0]=0x02060000;state.texture_control[0]=0;
        const auto queued=native.prepare(state);
        // More distinct native descriptors than D3D11 permits simultaneously.
        // An older queued draw must retain its original level-zero sampler.
        for(unsigned i=0;i<8192;++i) {
            auto varying=state;varying.texture_control[0]=(i&4095)<<6;
            varying.texture_filter[0]|=(i>>12)<<8;
            native.prepare(varying);
        }
        auto vertices=ui_stream;
        constexpr std::array<float,2> coordinates[]={{0,0},{64,0},{64,64},{0,64}};
        for(unsigned i=0;i<4;++i) {vertices[i].uv=coordinates[i];vertices[i].color=0xFFFFFFFF;}
        const auto program=native.prepare_program(ui_program);
        VertexLayout attributes{};attributes[0]={0,0,0x42};attributes[9]={0,16,0x22};attributes[3]={0,24,0x40};
        const auto layout=native.prepare_layout(program,attributes);
        const std::array<VertexStream,1> streams{{{std::as_bytes(std::span(vertices)),sizeof(UiVertex)}}};
        native.begin();native.draw_streams(state,layout,ui_constants,ui_view,streams,{},4,{},&queued);native.end();
        const auto image=native.readback();expect(image,32,32,{255,0,0,255});save_png(image,directory/"sampler-cache-lifetime-native.png");
        Microsoft::WRL::ComPtr<ID3D11SamplerState> sampler;native.native_context()->PSGetSamplers(0,1,&sampler);
        D3D11_SAMPLER_DESC description{};sampler->GetDesc(&description);
        if(description.MaxLOD!=0 || description.MipLODBias!=0 || !native.errors().empty())
            throw std::runtime_error("Sampler eviction changed a queued draw or produced graphics validation errors");
    });
    for(unsigned scale=1;scale<=4;++scale) {
        if(total++) report+=',';
        const auto name=std::format("ui-pixel-origin-{}x",scale);const auto image_path=directory/(name+".png");std::string error;
        try {
            Renderer native(64,64,warp,debug,scale);const auto program=native.prepare_program(ui_program);
            native.prepare(colored());
            const auto aligned=[&](const Image& image,std::array<unsigned,4> color) {
                for(auto x:{16*scale,48*scale-1}) for(auto y:{16*scale,48*scale-1}) expect(image,x,y,color);
                for(auto edge:{16*scale-1,48*scale}) {expect(image,edge,32*scale,{0,0,0,255});expect(image,32*scale,edge,{0,0,0,255});}
            };
            native.begin();native.draw_program(colored(),program,ui_constants,ui_view,ui_vertices);native.end();
            aligned(native.readback(),{255,0,0,255});
            VertexLayout attributes{};attributes[0]={0,0,0x42};attributes[9]={0,16,0x22};attributes[3]={0,24,0x40};
            const auto layout=native.prepare_layout(program,attributes);
            const std::array<VertexStream,1> streams{{{std::as_bytes(std::span(ui_stream)),sizeof(UiVertex)}}};
            native.begin();native.draw_streams(colored(),layout,ui_constants,ui_view,streams,{},4);native.end();
            const auto image=native.readback();aligned(image,{0,255,0,255});save_png(image,image_path);
            if(const auto messages=native.errors();!messages.empty()) throw std::runtime_error("UI pixel origin: "+messages.front());
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{}}}",json(name),error.empty(),json(error),json(path_text(image_path)));
    }
    // frame-377419886700: copy the previous frame with a half-pixel-adjusted
    // rectangle, then darken it using reverse subtraction. Repeated copies
    // must preserve every physical pixel, including all four framebuffer edges.
    for(unsigned scale=1;scale<=4;++scale) {
        if(total++) report+=',';
        const auto name=std::format("transition-framebuffer-copy-{}x",scale);const auto image_path=directory/(name+".png");std::string error;
        try {
            Renderer native(64,64,warp,debug,scale);const auto shader=native.prepare_program(ui_program);
            std::array<std::uint32_t,2> targets{native.create_target(64,64),native.create_target(64,64)};
            std::vector<std::byte> pattern(64*scale*64*scale*4);
            for(unsigned y=0;y<64*scale;++y) for(unsigned x=0;x<64*scale;++x) {
                const auto at=(y*64*scale+x)*4;const auto value=std::byte((x*37+y*13)&255);
                pattern[at]=pattern[at+1]=pattern[at+2]=value;pattern[at+3]=std::byte{255};
            }
            const auto input=native.upload({64*scale,64*scale,TextureFormat::argb8_linear,pattern});
            auto initial=textured(input);initial.texture_filter[0]=0x01013D9B;initial.texture_address[0]=0x00010303;
            native.prepare(initial);native.set_target(targets[0]);native.begin();native.draw(initial,white);native.end();
            struct TransitionVertex {Float4 position;std::uint32_t color;std::array<float,2> uv;};
            std::array<TransitionVertex,4> vertices{{
                {{-0.5f,-0.5f,0.1f,0.1f},0xFFFFFFFF,{0,0}},{{-0.5f,63.5f,0.1f,0.1f},0xFFFFFFFF,{0,64}},
                {{63.5f,63.5f,0.1f,0.1f},0xFFFFFFFF,{64,64}},{{63.5f,-0.5f,0.1f,0.1f},0xFFFFFFFF,{64,0}}
            }};
            VertexLayout attributes{};attributes[0]={0,0,0x42};attributes[3]={0,16,0x40};attributes[9]={0,20,0x22};
            const auto layout=native.prepare_layout(shader,attributes);ProgramView transition_view=ui_view;transition_view.texture_scales[0]={1.0f/64,1.0f/64,1,1};
            RenderState copy;copy.primitive=8;copy.color_mask=0x00010101;copy.combiner_control=1;copy.color_inputs[0]=0x00002008;
            copy.alpha_inputs[0]=0x14200000;copy.color_outputs[0]=copy.alpha_outputs[0]=0xC00;copy.final_inputs={0x0000000C,0x00001C80};
            copy.texture_modes[0]=1;copy.texture_filter[0]=0x01013D9B;copy.texture_address[0]=0x00010303;
            auto fade=copy;fade.texture_modes[0]=0;fade.color_inputs[0]=0x04200000;fade.blend=true;fade.blend_source=fade.blend_destination=1;fade.blend_equation=0x800B;
            std::array<ProgramVertex,4> fade_vertices{};
            for(unsigned i=0;i<4;++i) {fade_vertices[i].attributes[0]=vertices[i].position;fade_vertices[i].attributes[3]={8.0f/255,8.0f/255,8.0f/255,1};}
            native.prepare(fade);
            for(unsigned frame=0;frame<12;++frame) {
                copy.textures[0]=targets[frame&1];native.prepare(copy);native.set_target(targets[(frame+1)&1]);
                for(auto& vertex:vertices) vertex.color=0xFFFFFFFF;
                const std::array<VertexStream,1> streams{{{std::as_bytes(std::span(vertices)),sizeof(TransitionVertex)}}};
                native.begin(0xFFFF00FF);native.draw_streams(copy,layout,ui_constants,transition_view,streams,{},4);
                if(frame>=6) native.draw_program(fade,shader,ui_constants,ui_view,fade_vertices);
                native.end();const auto image=native.readback();
                for(std::size_t at=0;at<pattern.size();at+=4) for(unsigned lane=0;lane<3;++lane) {
                    const auto expected=std::max(0,int(unsigned(pattern[at]))-int(frame>=6?(frame-5)*8:0));
                    if(std::abs(int(unsigned(image.rgba[at+lane]))-expected)>1) throw std::runtime_error(std::format("Frame {} moved or failed to fade pixel ({},{}), channel {}",frame,(at/4)%image.width,(at/4)/image.width,lane));
                }
                if(frame==11) save_png(image,image_path);
            }
            if(const auto messages=native.errors();!messages.empty()) throw std::runtime_error("Transition: "+messages.front());
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{}}}",json(name),error.empty(),json(error),json(path_text(image_path)));
    }
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
    // Wrap vertex, index and constant buffers without an intervening readback.
    // Distinct positions and colours expose overwritten queued draw data.
    if(total++) report+=',';
    std::string upload_error;double upload_submit_ms=0,upload_complete_ms=0;PerfCounters upload_counters{};
    const auto upload_image=directory/"stream-upload-order.png";
    try {
        Renderer native(8,8,warp,debug);
        auto queued_program=vertex_program;queued_program.tokens[1]=token(1,0,0,96,3,false,true,3);
        const auto program=native.prepare_program(queued_program);
        VertexLayout attributes{};attributes[0]={0,0,0x42};
        const std::array layouts{native.prepare_layout(program,attributes),native.prepare_layout(native.prepare_program(vertex_program),attributes)};
        auto queued_current=current;queued_current.attributes[3]={0,0,1,1};
        std::array<RenderState,2> states{factors,factors};
        for(auto& state:states) {state.color_inputs[0]=0x01040000;state.alpha_inputs[0]=0x11140000;native.prepare(state);}
        states[0].factor0[0]=0xFFFF0000;states[1].factor0[0]=0xFFFFFFFF;
        std::array<VertexConstants,2> uniforms{context,context};
        uniforms[0][96]={1,1,1,1};uniforms[1][96]={0,0,1,1};
        const std::array<std::array<Float4,4>,2> positions{{
            {{{0,0,0.5f,1},{4,0,0.5f,1},{4,8,0.5f,1},{0,8,0.5f,1}}},
            {{{4,0,0.5f,1},{8,0,0.5f,1},{8,8,0.5f,1},{4,8,0.5f,1}}}}};
        const std::array<std::array<VertexStream,1>,2> streams{{
            {{{std::as_bytes(std::span(positions[0])),sizeof(Float4)}}},
            {{{std::as_bytes(std::span(positions[1])),sizeof(Float4)}}}}};
        std::array<Renderer::StreamDraw,32> group{};
        Performance profile;profile.start(native.adapter(),60,1,false,{});native.performance(&profile);
        native.begin();const auto started=std::chrono::steady_clock::now();
        constexpr unsigned count=32770;
        for(unsigned first=0;first<count;first+=static_cast<unsigned>(group.size())) {
            const auto size=std::min<unsigned>(static_cast<unsigned>(group.size()),count-first);
            for(unsigned i=0;i<size;++i) {
                const auto which=((first+i)/2)&1;
                const auto layout=layouts[((first+i)&1)?which:0];
                group[i]={&states[which],&uniforms[which],&view,streams[which],&queued_current,{},nullptr,layout,4};
            }
            native.draw_streams(std::span(group).first(size));
        }
        native.end();upload_submit_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
        const auto image=native.readback();upload_complete_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
        upload_counters=native.stats().counters;native.performance(nullptr);profile.stop();
        if(native.stats().reused_vertex_bytes<=(count-(count+31)/32)*sizeof(ProgramVertex))
            throw std::runtime_error("Queued shared geometry was uploaded repeatedly");
        expect(image,1,4,{255,0,0,255});expect(image,6,4,{0,0,255,255});save_png(image,upload_image);
        if(const auto messages=native.errors();!messages.empty()) throw std::runtime_error("Queued uploads: "+messages.front());
        ++passed;
    } catch(const std::exception& failure) {upload_error=failure.what();}
    report+=std::format("{{\"name\":\"stream-upload-order\",\"passed\":{},\"error\":{},\"image\":{},\"draws\":32770,\"submission_ms\":{},\"completion_ms\":{},\"buffer_maps\":{},\"buffer_bytes\":{}}}",
        upload_error.empty(),json(upload_error),json(path_text(upload_image)),upload_submit_ms,upload_complete_ms,
        upload_counters[unsigned(PerfMetric::buffer_maps)],upload_counters[unsigned(PerfMetric::buffer_bytes)]);
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
    FixedTransform explicit_fog{};explicit_fog.fog=true;explicit_fog.fog_source=6;
    const auto explicit_shader=renderer.prepare_fixed(explicit_fog);
    for(unsigned mode:{0x2601U,0x0804U,0x0800U,0x0802U,0x0801U,0x0803U}) {
        auto state=fogged;state.fog_mode=mode;
        const auto expected=mode==0x2601 || mode==0x0804 || mode==0x0800 ?
            std::array<unsigned,4>{255,0,0,255}:std::array<unsigned,4>{0,0,255,255};
        for(auto bits:{0x7F800000U,0xFF800000U,0x7FC00000U}) {
            auto vertices=fixed_vertices;for(auto& vertex:vertices) vertex.attributes[5][0]=std::bit_cast<float>(bits);
            run(std::format("fog-nonfinite-{:04x}-{:08x}",mode,bits),state,white,[&](const Image& image){expect(image,32,32,expected);},{},{},[&] {
                renderer.draw_program(state,explicit_shader,fixed_context,view,vertices);
            });
        }
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

    const auto gpu_check=[&](std::string_view name,unsigned indexed,bool shifted,bool programmable,bool inlined=false,bool skinned=false,bool generated_texture=false,bool surface_clip=false,
                         bool target_sample=false,bool rgb565=false,bool unfinished=false,bool linear_sample=false,
                         TextureFormat feedback_format=TextureFormat::argb8_linear,bool layout_alias=false,bool upload_context=false,unsigned semaphore_burst=0) {
        if(total++) report+=',';
        std::string error;GpuStats statistics{};double submission_ms=0;
        const auto image_path=directory/(std::string(name)+".png");
        try {
            Renderer native(64,64,warp,debug);
            std::vector<std::byte> ram(semaphore_burst?256*1024:65536);Memory memory(0,ram);Gpu gpu(native,memory,60000);
            const bool alpha_feedback=feedback_format==TextureFormat::argb8 || feedback_format==TextureFormat::xrgb8;
            std::vector<std::uint32_t> words;
            const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values,bool nonincreasing=false) {
                words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18)|(nonincreasing?0x40000000U:0));
                words.insert(words.end(),values);
            };
            packet(0,{0xD});packet(0x1A4,{8});packet(0x1D6C,{0});
            packet(0x200,{64U<<16,64U<<16,rgb565?0x06060213U:layout_alias?0x06060224U:alpha_feedback?0x06060228U:0x128U,
                rgb565?0x00800080U:0x01000100U,0x20000,layout_alias?0U:0x30000U});
            packet(0x1D98,{63U<<16,63U<<16});packet(0x1D8C,{0xFFFFFF00,layout_alias?0xFF00FF00U:alpha_feedback?0x000000FFU:0xFF0000FFU});packet(0x1D94,{0xF3});
            if(layout_alias) {
                // The game clears an old shadow through its packed linear view,
                // then renders/samples the same allocation in Morton order.
                packet(0x208,{0x06060124});packet(0x1D90,{0});packet(0x1D94,{0xF0});packet(0x208,{0x06060224});
            }
            if(surface_clip || layout_alias) packet(0x200,{(32U<<16)|16U,(32U<<16)|16U});
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
                auto uploaded=vertex_program;if(upload_context) uploaded.tokens[1]=token(1,0,0,96,3,false,true,3);
                words.push_back(0xB00|(8U<<18));
                for(unsigned instruction=0;instruction<2;++instruction) words.insert(words.end(),uploaded.tokens[instruction].begin(),uploaded.tokens[instruction].end());
                packet(0x1E9C,{0});
                packet(0xB04,{uploaded.tokens[0][1],uploaded.tokens[0][2],uploaded.tokens[0][3]});
                packet(0xB00,{uploaded.tokens[1][0],uploaded.tokens[1][1],uploaded.tokens[1][2],uploaded.tokens[1][3]});
                packet(0x1EA0,{0});
                if(upload_context) {
                    packet(0x1EA4,{94});
                    packet(0xB80,{0,0,0x3F800000,0x3F800000,0,0x3F800000,0,0x3F800000,
                        0x3F800000,0x3F800000,0,0x3F800000,0x3F800000,0x3F800000,0x3F800000,0x3F800000});
                    packet(0x1EA4,{94});packet(0xB84,{0,0x3F800000,0x3F800000,0,0x3F800000,0,0x3F800000});
                    packet(0xB80,{0x3F800000,0x3F800000,0,0x3F800000});
                    // A partial upload must preserve its lane and load index.
                    packet(0x1EA4,{96});packet(0xB84,{0});
                }
            }
            packet(0x17FC,{8});
            if(inlined) {
                const auto data=std::span(positions).subspan(1);
                words.push_back(0x40001818|(16U<<18));
                for(const auto& position:data) for(const auto component:position) words.push_back(std::bit_cast<std::uint32_t>(component));
            } else if(indexed==32) packet(0x1808,{0,1,2,3},true);
            else if(indexed) packet(0x1800,{0x00010000,0x00030002},true);
            else packet(0x1810,{0x03000000|first});
            packet(0x17FC,{0});
            packet(0x110,{0});
            if(layout_alias) {
                // Repeat after a nonuniform shadow draw, as on the next frame.
                packet(0x200,{64U<<16,64U<<16,0x06060124});packet(0x1D94,{0xF0});
                packet(0x200,{(32U<<16)|16U,(32U<<16)|16U,0x06060224});
                packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
                constexpr std::array<Float4,4> coordinates{{{0,0,0,1},{1,0,0,1},{1,1,0,1},{0,1,0,1}}};
                std::memcpy(memory.access(8192,sizeof(coordinates)),coordinates.data(),sizeof(coordinates));
                packet(0x1744,{8192});packet(0x1784,{0x1042});
            }
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
            for(unsigned i=0;i<semaphore_burst;++i) {
                if(i==semaphore_burst/2) {
                    // New GPU work must have a distinct completion point.
                    packet(0x1D90,{0xFF0000FF});packet(0x1D94,{0xF0});
                    packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});
                }
                packet(0x1D6C,{(i%24)*4});packet(0x1D70,{i+1});
            }
            packet(0x1D6C,{0});packet(0x1D70,{123});
            // A flip presents the bound surface even without intervening draws.
            packet(0x12C,{0});packet(0x12C,{0});
            if(unfinished) {
                // A smaller unpresented pass must not replace the final frame.
                packet(0x200,{32U<<16,32U<<16,0x05050213,0x00400040,0x60000,0x70000});
                packet(0x1D98,{31U<<16,31U<<16});packet(0x1D90,{0xFF00FF00});packet(0x1D94,{0xF0});
            }
            const auto bytes=std::as_bytes(std::span(words));const auto command_begin=semaphore_burst?65536U:0U;
            std::memcpy(memory.access(command_begin,bytes.size()),bytes.data(),bytes.size());
            const auto started=std::chrono::steady_clock::now();gpu.submit(command_begin,command_begin+static_cast<std::uint32_t>(bytes.size()));gpu.wait();gpu.snapshot();
            submission_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
            if(memory.load<std::uint32_t>(60000)!=123) throw std::runtime_error("GPU semaphore completed without publishing its value");
            statistics=gpu.stats();
            if(statistics.draws!=(target_sample?2U:1U)+(layout_alias?1U:0U)+(semaphore_burst?1U:0U) || statistics.vertices!=(target_sample?8U:4U)+(layout_alias?4U:0U)+(semaphore_burst?4U:0U) ||
                statistics.clears!=(target_sample?2U:1U)+(unfinished?1U:0U)+(linear_sample?1U:0U)+(layout_alias?2U:0U)+(semaphore_burst?1U:0U))
                throw std::runtime_error("GPU packet draw/clear counts differ");
            if(semaphore_burst) {
                for(unsigned slot=1;slot<24;++slot) {
                    const auto last=semaphore_burst-1-(semaphore_burst-1-slot)%24;
                    if(memory.load<std::uint32_t>(60000+slot*4)!=last+1) throw std::runtime_error("GPU semaphore slot lost its final value");
                }
                if(statistics.fence_queries>4 || statistics.fence_queue_waits) throw std::runtime_error("Duplicate semaphore queries exhausted the completion pool");
            }
            if(statistics.flips!=2+(linear_sample?1U:0U)) throw std::runtime_error("Repeated GPU flips were not presented");
            const auto image=native.readback();save_png(image,image_path);
            if(layout_alias) {expect(image,32,32,{255,0,0,255});expect(image,3,3,{0,0,0,255});expect(image,52,52,{0,0,0,255});}
            else if(surface_clip) {expect(image,32,32,{255,0,0,255});expect(image,3,3,{0,0,255,255});expect(image,52,52,{0,0,255,255});}
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
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{},\"draws\":{},\"vertices\":{},\"submission_ms\":{},\"fences\":{},\"fence_queries\":{},\"fence_queue_waits\":{}}}",
            json(name),error.empty(),json(error),json(path_text(image_path)),statistics.draws,statistics.vertices,submission_ms,statistics.fences,statistics.fence_queries,statistics.fence_queue_waits);
    };
    gpu_check("gpu-packets-array-quad",false,false,false);
    gpu_check("gpu-capture-last-presented-frame",false,false,false,false,false,false,false,false,false,true);
    gpu_check("gpu-packets-element16-quad",16,false,false);
    gpu_check("gpu-packets-element32-quad",32,false,false);
    gpu_check("gpu-packets-first-vertex",false,true,false);
    gpu_check("gpu-packets-skin-matrix-aliases",false,false,false,false,true);
    gpu_check("gpu-packets-texgen-matrix-aliases",false,false,false,false,false,true);
    gpu_check("gpu-packets-program-upload",false,false,true);
    gpu_check("gpu-packets-context-upload",false,false,true,false,false,false,false,false,false,false,false,TextureFormat::argb8_linear,false,true);
    gpu_check("gpu-packets-semaphore-burst",false,false,false,false,false,false,false,false,false,false,false,TextureFormat::argb8_linear,false,false,4096);
    gpu_check("gpu-packets-inline-array",false,false,false,true);
    gpu_check("gpu-packets-surface-clip",false,false,false,false,false,false,true);
    gpu_check("gpu-packets-rgb565-z16",false,false,false,false,false,false,false,false,true);
    gpu_check("gpu-packets-render-target-sampling",false,false,false,false,false,false,false,true);
    gpu_check("gpu-packets-rgb565-target-sampling",false,false,false,false,false,false,false,true,true);
    gpu_check("gpu-packets-linear-target-coordinates",false,false,false,false,false,false,false,true,false,false,true);
    gpu_check("gpu-packets-xrgb-feedback-alpha-kill",false,false,false,false,false,false,false,true,false,false,false,TextureFormat::xrgb8);
    gpu_check("gpu-packets-argb-feedback-alpha-kill",false,false,false,false,false,false,false,true,false,false,false,TextureFormat::argb8);
    gpu_check("gpu-packets-shadow-clear-layout-alias",false,false,false,false,false,false,false,true,false,false,false,TextureFormat::xrgb8,true);

    if(total++) report+=',';
    std::string barrier_error;GpuStats barrier_stats{};double barrier_ms=0;
    const auto barrier_image=directory/"gpu-command-barriers.png";
    try {
        Renderer native(64,64,warp,debug);
        std::vector<std::byte> ram(65536);Memory memory(0,ram);Gpu gpu(native,memory,60000);
        std::vector<std::uint32_t> words;
        const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values) {
            words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18));words.insert(words.end(),values);
        };
        packet(0,{0xD});packet(0x1A4,{8});packet(0x1D6C,{0});
        packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x40000,0x50000});
        for(unsigned i=0;i<1024;++i) {
            const auto scanline=i%64;
            packet(0x1D98,{63U<<16,(scanline<<16)|scanline});
            packet(0x1D90,{scanline&1?0xFF0000FFU:0xFFFF0000U});packet(0x1D94,{0xF0});
            packet(0x110,{0});
        }
        packet(0x1D70,{1});
        const auto commands=std::as_bytes(std::span(words));std::memcpy(memory.access(0,commands.size()),commands.data(),commands.size());
        const auto started=std::chrono::steady_clock::now();
        gpu.submit(0,static_cast<std::uint32_t>(commands.size()));
        barrier_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
        barrier_stats=gpu.stats();
        if(barrier_stats.fence_queries!=1 || barrier_stats.fences!=1 || barrier_stats.fence_queue_waits)
            throw std::runtime_error("GPU command barriers created CPU completion queries or exhausted their pool");
        gpu.wait_fence(1,2);
        if(memory.load<std::uint32_t>(60000)!=1) throw std::runtime_error("Command barriers lost the CPU-visible semaphore");
        gpu.wait();
        if(gpu.stats().fence_queries!=2) throw std::runtime_error("Explicit CPU-idle wait lost its completion query");
        gpu.snapshot();const auto image=native.readback();save_png(image,barrier_image);
        for(unsigned scanline=0;scanline<64;++scanline) expect(image,32,scanline,scanline&1?std::array<unsigned,4>{0,0,255,255}:std::array<unsigned,4>{255,0,0,255});
        if(!native.errors().empty()) throw std::runtime_error("GPU command barriers produced D3D11 validation messages");
        ++passed;
    } catch(const std::exception& failure) {barrier_error=failure.what();}
    report+=std::format("{{\"name\":\"gpu-command-barriers\",\"passed\":{},\"error\":{},\"image\":{},\"barriers\":1024,\"submission_ms\":{},\"fence_queries\":{},\"fence_queue_waits\":{}}}",
        barrier_error.empty(),json(barrier_error),json(path_text(barrier_image)),barrier_ms,barrier_stats.fence_queries,barrier_stats.fence_queue_waits);

    if(total++) report+=',';
    std::string fence_error;GpuStats fence_stats{};
    const auto fence_image=directory/"gpu-requested-fence-wrap.png";
    try {
        Renderer native(64,64,warp,debug);
        std::vector<std::byte> ram(65536);Memory memory(0,ram);Gpu gpu(native,memory,60000);
        std::vector<std::uint32_t> words;
        const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values) {
            words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18));words.insert(words.end(),values);
        };
        packet(0,{0xD});packet(0x1A4,{8});packet(0x1D6C,{0});packet(0x1D98,{63U<<16,63U<<16});
        packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x40000,0x50000});
        memory.store<std::uint32_t>(60000,0xFFFFFFFA);
        for(unsigned i=0;i<130;++i) {
            packet(0x1D90,{i&1?0xFF0000FFU:0xFFFF0000U});packet(0x1D94,{0xF0});
            packet(0x110,{0});
            packet(0x1D70,{0xFFFFFFFCU+i*2});
        }
        const auto commands=std::as_bytes(std::span(words));std::memcpy(memory.access(0,commands.size()),commands.data(),commands.size());
        gpu.submit(0,static_cast<std::uint32_t>(commands.size()));
        const auto queries=gpu.stats().fence_queries;
        gpu.wait_fence(2,256);gpu.wait_fence(254,256);gpu.wait_fence(0xFFFFFFF8,256);
        fence_stats=gpu.stats();
        if(queries!=130 || fence_stats.fence_queries!=queries || memory.load<std::uint32_t>(60000)!=254)
            throw std::runtime_error("Requested/completed fences changed completion points or failed counter wraparound");
        gpu.snapshot();const auto image=native.readback();save_png(image,fence_image);expect(image,32,32,{0,0,255,255});
        if(!native.errors().empty()) throw std::runtime_error("Requested fence completion produced D3D11 validation messages");
        ++passed;
    } catch(const std::exception& failure) {fence_error=failure.what();}
    report+=std::format("{{\"name\":\"gpu-requested-fence-wrap\",\"passed\":{},\"error\":{},\"image\":{},\"fence_queries\":{},\"fence_queue_waits\":{}}}",
        fence_error.empty(),json(fence_error),json(path_text(fence_image)),fence_stats.fence_queries,fence_stats.fence_queue_waits);

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
            packet(0x110,{0});
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
        gpu.invalidate_memory(0x8002A000,8192);
        if(native.resources().textures!=6) throw std::runtime_error("Released cubemap face retained its snapshot or surface");
        packet(0x200,{64U<<16,64U<<16,0x06060213,0x00800080,0x2A000,0});
        packet(0x1D90,{0xFFFF00FF});packet(0x1D94,{0xF0});
        packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x40000,0x50000});
        sample_face(5);packet(0x200,{64U<<16,64U<<16});packet(0x12C,{0});submit();
        expect(native.readback(),55,32,{255,0,255,255});
        if(!native.errors().empty()) throw std::runtime_error("Cubemap feedback produced D3D11 validation messages");
        ++passed;
    } catch(const std::exception& failure) {cubemap_error=failure.what();}
    report+=std::format("{{\"name\":\"gpu-packets-reflection-cubemap\",\"passed\":{},\"error\":{},\"image\":{}}}",
        cubemap_error.empty(),json(cubemap_error),json(path_text(cubemap_image)));

    for(const bool recycling:{false,true}) {
        if(total++) report+=',';
        const auto name=recycling?"graphics-resource-recycling-5000":"gpu-storage-reuse";
        const auto image_path=directory/(std::string(name)+".png");
        std::string error;
        try {
            Renderer native(64,64,warp,debug);
            const auto baseline=native.resources();
            if(recycling) {
                const auto pixels=solid(4,4,0xFF00FF00);
                const auto retained=native.upload({4,4,TextureFormat::argb8,pixels});
                for(unsigned cycle=0;cycle<5000;++cycle) {
                    const auto texture=native.upload({4,4,TextureFormat::argb8,pixels});
                    const auto target=native.create_target(4,4,SurfaceColor::rgb565,SurfaceDepth::z16);
                    native.set_target(target);native.clear(0xFFFF0000);
                    const std::array<std::uint32_t,6> faces{target,target,target,target,target,target};
                    const auto cube=native.copy_cubemap(faces);
                    native.release(cube);native.release(target);native.release(texture);
                    const auto resources=native.resources();
                    if(resources.textures!=1 || resources.targets!=baseline.targets ||
                       resources.texture_bytes!=pixels.size() || resources.target_bytes!=baseline.target_bytes)
                        throw std::runtime_error("Freed resources retained their views or memory budget");
                }
                const auto state=textured(retained);native.prepare(state);native.begin();native.draw(state,white);native.end();
                const auto image=native.readback();expect(image,32,32,{0,255,0,255});save_png(image,image_path);
                native.release(retained);
            } else {
                std::vector<std::byte> ram(512*1024);Memory memory(0,ram);Gpu gpu(native,memory,60000);
                std::vector<std::uint32_t> words;
                const auto packet=[&](std::uint32_t method,std::initializer_list<std::uint32_t> values) {
                    words.push_back(method|(static_cast<std::uint32_t>(values.size())<<18));words.insert(words.end(),values);
                };
                const auto submit=[&] {
                    const auto bytes=std::as_bytes(std::span(words));std::memcpy(memory.access(0,bytes.size()),bytes.data(),bytes.size());
                    gpu.submit(0,static_cast<std::uint32_t>(bytes.size()));words.clear();
                };
                const auto paint=[&] {packet(0x17FC,{8});packet(0x1810,{0x03000000});packet(0x17FC,{0});};
                const auto fill=[&](std::uint32_t address,std::uint32_t argb) {
                    const auto pixels=solid(4,4,argb);std::memcpy(memory.access(address,pixels.size()),pixels.data(),pixels.size());
                };
                packet(0,{0xD});packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x20000,0x30000});
                packet(0x1D98,{63U<<16,63U<<16});packet(0x1D8C,{0xFFFFFF00,0xFF000000});packet(0x1D94,{0xF3});
                packet(0x33C,{0x207,0,1,0});packet(0x350,{0x8006,0x203,0x01010101,0,255,0x207,0,255,0x1E00,0x1E00,0x1E00});
                packet(0x288,{8,0x1800});packet(0x1E70,{1});
                for(unsigned stage=0;stage<4;++stage) packet(0x1B08+stage*0x40,{0x030303});
                packet(0x1B00,{0x8000,0x02210620,0x030303,0x40000000});packet(0x1B14,{0x01010000});
                packet(0xAF0,{0x3F800000,0x3F800000,0x3F800000,0});packet(0xA20,{0x42000000,0x42000000,0,0});
                packet(0x680,{0x42000000,0,0,0,0,0xC2000000,0,0,0,0,0x3F800000,0,0,0,0,0x3F800000});
                constexpr std::array<Float4,4> positions{{{-1,1,0.25f,1},{1,1,0.25f,1},{1,-1,0.25f,1},{-1,-1,0.25f,1}}};
                constexpr std::array<Float4,4> coordinates{{{0,0,0,1},{1,0,0,1},{1,1,0,1},{0,1,0,1}}};
                std::memcpy(memory.access(4096,sizeof(positions)),positions.data(),sizeof(positions));
                std::memcpy(memory.access(8192,sizeof(coordinates)),coordinates.data(),sizeof(coordinates));
                packet(0x1720,{4096});packet(0x1760,{0x1042});packet(0x1744,{8192});packet(0x1784,{0x1042});
                fill(0x8000,0xFFFF0000);fill(0x9000,0xFF0000FF);
                // Keep an unrelated texture alive while replacing a subrange of
                // the first allocation through its high-address alias.
                packet(0x200,{(16U<<16)|48U,64U<<16});packet(0x1B00,{0x9000});paint();
                packet(0x200,{32U<<16,64U<<16});packet(0x1B00,{0x8000});paint();
                packet(0x1B10,{0x77770000});packet(0x1B1C,{0x12345678});packet(0x1B20,{0x76543210});paint();submit();
                if(native.resources().textures!=3) throw std::runtime_error("Unused texture registers created duplicate cache entries");
                gpu.invalidate_memory(0xF0008010,4);fill(0x8000,0xFF00FF00);
                if(gpu.stats().draws!=3 || native.resources().textures!=2) throw std::runtime_error("Storage release dropped draws or unrelated resources");
                packet(0x200,{(16U<<16)|32U,64U<<16});paint();
                packet(0x200,{64U<<16,64U<<16});packet(0x12C,{0});submit();gpu.wait();
                const auto image=native.readback();expect(image,8,32,{255,0,0,255});expect(image,40,32,{0,255,0,255});expect(image,56,32,{0,0,255,255});
                save_png(image,image_path);
                // A former GPU surface at this address must no longer override
                // an ordinary texture loaded into the recycled guest allocation.
                packet(0x200,{4U<<16,4U<<16,0x02020228,0x00100010,0xA000,0});
                packet(0x1D98,{3U<<16,3U<<16});packet(0x1D90,{0xFFFF0000});packet(0x1D94,{0xF0});submit();
                gpu.invalidate_memory(0x8000A000,64);fill(0xA000,0xFF0000FF);
                packet(0x200,{64U<<16,64U<<16,0x128,0x01000100,0x20000,0x30000});packet(0x1B00,{0xA000});paint();
                packet(0x12C,{0});submit();gpu.wait();expect(native.readback(),32,32,{0,0,255,255});
                // Releasing only a depth allocation also retires its stale
                // composite surface, including when it is the active target.
                gpu.invalidate_memory(0x80030080,4);
                if(native.resources().targets!=1) throw std::runtime_error("Released depth storage retained its cached surface");
                gpu.invalidate_memory(0x8000,0x3000);
            }
            const auto resources=native.resources();
            if(resources.textures || resources.targets!=baseline.targets || resources.texture_bytes || resources.target_bytes!=baseline.target_bytes)
                throw std::runtime_error("Graphics storage did not return to its initial resource budget");
            if(!native.errors().empty()) throw std::runtime_error("Resource recycling produced D3D11 validation messages");
            ++passed;
        } catch(const std::exception& failure) {error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{}}}",json(name),error.empty(),json(error),json(path_text(image_path)));
    }
    for(unsigned quality=1;quality<=3;++quality) for(const auto sample_count:{1U,4U}) {
        if(sample_count>1 && !renderer.supports_msaa(sample_count)) continue;
        if(total++) report+=',';
        const auto name=std::format("gtao-{}-{}x",quality,sample_count);const auto image_path=directory/(name+".png");
        std::string error;const auto started=std::chrono::steady_clock::now();
        try {
            Renderer native(128,96,warp,debug);native.enhancements(sample_count==4?5:0,0);
            const auto baseline_bytes=native.resources().target_bytes;
            auto surface=colored();surface.depth_test=surface.depth_write=true;native.prepare(surface);
            const auto ui=native.prepare_program(screen_vertex_programs()[0]);
            VertexConstants constants{};constants[0]={1,1,16777215,1};constants[1]={0.53125f,0.53125f,0,0};
            ProgramView view;view.depth_scale=16777215;
            auto hud=colored();hud.blend=true;hud.blend_source=0x302;hud.blend_destination=0x303;native.prepare(hud);
            std::array<ProgramVertex,4> text{};
            constexpr std::array<Float4,4> positions{{{48,36,0,1},{80,36,0,1},{80,60,0,1},{48,60,0,1}}};
            for(unsigned i=0;i<4;++i){text[i].attributes[0]=positions[i];text[i].attributes[3]={1,0,0,1};}
            auto alpha=colored();alpha.color_mask=0x01000000;native.prepare(alpha);
            auto shadow=hud;shadow.color_mask=0x10101;shadow.blend_source=0x304;shadow.blend_destination=1;shadow.blend_equation=0x800B;native.prepare(shadow);
            auto unsupported=shadow;unsupported.blend_source=1;unsupported.blend_equation=0x800A;native.prepare(unsupported);
            std::array<ProgramVertex,4> panel{};
            constexpr std::array<Float4,4> full{{{0,0,0,1},{128,0,0,1},{128,96,0,1},{0,96,0,1}}};
            for(unsigned i=0;i<4;++i){panel[i].attributes[0]=full[i];panel[i].attributes[3]={0.25f,0.25f,0.25f,0.5f};}
            const auto depth_at=[](float z){return (100.0f/(100.0f-0.1f))*(1-0.1f/z);};
            const auto draw=[&](bool corner,bool overlay,bool clear_depth=false,bool isolate=false,unsigned menu_shadow=0) {
                native.prepare(surface);native.prepare(hud);
                native.camera(1,0.1f,100,1.047197551f);
                native.begin(0xFF808080);
                auto plane=quad({0.8f,0.8f,0.8f,1},depth_at(4));
                if(corner) {
                    auto wall=plane;wall[1].position[0]=wall[2].position[0]=0;
                    wall[0].position[2]=wall[3].position[2]=depth_at(4/(1+0.577350269f*(128.0f/96)));
                    native.draw(surface,wall);plane[0].position[0]=plane[3].position[0]=0;
                }
                native.draw(surface,plane);
                if(isolate) {native.end();native.begin_overlay();native.begin(0,1,false);}
                if(overlay) native.draw_program(hud,ui,constants,view,text);
                if(menu_shadow) {
                    native.draw_program(alpha,ui,constants,view,panel);
                    native.draw_program(menu_shadow==1?shadow:unsupported,ui,constants,view,panel);
                    auto transparent=text;for(auto& vertex:transparent) vertex.attributes[3][3]=0;
                    native.draw_program(hud,ui,constants,view,transparent);
                }
                if(clear_depth) native.clear_buffers(3,0,1,0);
                native.end();if(isolate) native.end_overlay();native.publish_frame();return native.read_frame();
            };
            const auto original=draw(true,false);
            native.ambient_occlusion(quality,1);
            const auto flat=draw(false,false);expect(flat,64,48,{204,204,204,255},2);
            const auto shaded=draw(true,false);
            const auto dark=pixel(shaded,65,48)[0];
            if(dark>=198 || dark<20) throw std::runtime_error(std::format("GTAO corner shading is missing or excessive: {}",dark));
            expect(native.readback(),65,48,pixel(original,65,48),0);
            const auto overlaid=draw(true,true,true);expect(overlaid,64,48,{255,0,0,255},1);
            if(pixel(overlaid,65,28)[0]>=198) throw std::runtime_error("HUD depth clear lost the scene depth snapshot");
            save_png(overlaid,image_path);
            const auto menu=draw(true,true,false,false,1);
            expect(menu,64,48,{223,0,0,128},2);
            expect(menu,65,28,{unsigned(std::max(0,int(pixel(shaded,65,28)[0])-32)),unsigned(std::max(0,int(pixel(shaded,65,28)[1])-32)),unsigned(std::max(0,int(pixel(shaded,65,28)[2])-32)),128},2);
            const auto unseparated=draw(true,true,false,false,2);
            if(unseparated.rgba!=native.readback().rgba) throw std::runtime_error("AO reused scene depth after an unsupported menu blend");
            // A later 2D screen can reuse the surface without clearing depth.
            auto background=colored();native.prepare(background);native.begin(0,1,false);
            native.draw_program(background,ui,constants,view,panel);native.draw_program(hud,ui,constants,view,text);
            native.end();native.publish_frame();
            if(native.read_frame().rgba!=native.readback().rgba) throw std::runtime_error("AO shaded a new menu with leftover scene depth");
            native.ambient_occlusion(quality,0);const auto zero=draw(true,false);
            if(zero.rgba!=original.rgba) throw std::runtime_error("Zero-strength AO changed the original image");
            native.ambient_occlusion(quality,0.35f);
            draw(true,true);const auto repeated=draw(true,true);expect(repeated,64,48,{255,0,0,255},1);
            const auto scene=native.create_target(128,96);
            native.set_target(scene);const auto isolated=draw(true,true,true,true);
            expect(isolated,64,48,{255,0,0,255},1);expect(native.read_target(scene),64,48,{204,204,204,255},1);
            expect(isolated,65,28,pixel(repeated,65,28),1);
            native.release(scene);

            if(sample_count==1) {native.enhancements(2,0);expect(draw(true,true),64,48,{255,0,0,255},1);native.enhancements(0,0);}
            native.ambient_occlusion(0);const auto disabled=draw(true,false);
            if(disabled.rgba!=original.rgba || native.resources().target_bytes!=baseline_bytes)
                throw std::runtime_error("Disabling AO did not restore original pixels/resource usage");
            if(!native.errors().empty()) throw std::runtime_error(native.errors().front());
            ++passed;
        } catch(const std::exception& failure){error=failure.what();}
        report+=std::format("{{\"name\":{},\"passed\":{},\"error\":{},\"image\":{},\"elapsed_ms\":{:.3f}}}",json(name),error.empty(),json(error),json(path_text(image_path)),std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count());
    }
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
        ",\"image\":"+json(path_text(output))+",\"source_bytes\":"+std::to_string(bytes.size())+'}';
}
std::string render_dictionary(const std::filesystem::path& input,
                              const std::filesystem::path& directory, bool warp) {
    if (std::filesystem::exists(directory)) throw std::runtime_error("Dictionary output directory already exists");
    TextureDictionary dictionary(input);
    Renderer renderer(64,64,warp);
    std::filesystem::create_directories(directory);
    std::string report="{\"format\":\"b2-dictionary-render-v1\",\"input\":"+json(path_text(input))+
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
            json(texture.name),texture.width,texture.height,texture.format,error.empty(),json(error),json(path_text(output)));
    }
    report+=std::format("],\"passed\":{},\"total\":{},\"failures\":{},\"shader_compilations\":{}}}",
                        failures==0,count,failures,renderer.stats().shader_compilations);
    write_text(directory/"report.json",report+'\n',false);
    return report;
}
}
