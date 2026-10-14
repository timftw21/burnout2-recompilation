#include "upscaling.h"
#include "render.h"
#include <d3d11.h>
#include <wrl/client.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <format>
#include <sstream>
#include <stdexcept>

namespace b2 {
namespace {
using Microsoft::WRL::ComPtr;
void checked(HRESULT result,const char* operation) {
    if(FAILED(result)) throw std::runtime_error(std::format("{} failed (0x{:08X})",operation,unsigned(result)));
}
float half(unsigned value) {
    const auto sign=(value>>15)?-1.0f:1.0f;
    const auto exponent=(value>>10)&31,mantissa=value&1023;
    if(exponent==31) return mantissa?NAN:sign*INFINITY;
    return sign*std::ldexp(exponent?1.0f+mantissa/1024.0f:mantissa/1024.0f,int(exponent?exponent:1)-15);
}
}
std::string check_upscaling(const std::filesystem::path& output) {
    if(std::filesystem::exists(output)) throw std::runtime_error("Choose a new upscaling diagnostic directory");
    std::filesystem::create_directories(output);
    constexpr unsigned width=128,height=96;
    Renderer renderer(width,height,false,true);
    auto* device=renderer.native_device();auto* context=renderer.native_context();
    std::array<ComPtr<ID3D11Texture2D>,4> inputs;
    constexpr DXGI_FORMAT formats[]={DXGI_FORMAT_R8G8B8A8_UNORM,DXGI_FORMAT_R32_FLOAT,DXGI_FORMAT_R16G16_FLOAT,DXGI_FORMAT_R8_UNORM};
    for(unsigned i=0;i<inputs.size();++i) {
        D3D11_TEXTURE2D_DESC desc{};desc.Width=width;desc.Height=height;desc.MipLevels=desc.ArraySize=1;
        desc.Format=formats[i];desc.SampleDesc.Count=1;desc.BindFlags=D3D11_BIND_SHADER_RESOURCE;
        checked(device->CreateTexture2D(&desc,nullptr,&inputs[i]),"Upscaling diagnostic input");
    }
    unsigned passed=0;std::ostringstream report;report<<"{\"format\":\"b2-upscaling-check-v1\",\"adapter\":"<<json(renderer.adapter())<<",\"cases\":[";
    for(unsigned kind=1;kind<=3;++kind) {
        const auto* name=kind==1?"dlss":kind==2?"fsr":"xess";
        if(kind>1) report<<',';
        report<<"{\"technique\":"<<json(name);
        try {
            FrameUpscaling upscale(device,context,UpscaleMethod(kind),width,height,width*2,height*2);
            std::vector<std::uint32_t> colour(width*height);
            std::vector<float> depth(width*height);
            std::vector<std::array<std::uint16_t,2>> motion(width*height);
            std::vector<std::uint8_t> reactive(width*height);
            for(unsigned frame=0;frame<12;++frame) {
                // Known geometry moves one input pixel each frame. Vectors
                // point to the previous frame; depth separates its two planes.
                for(unsigned y=0;y<height;++y) for(unsigned x=0;x<width;++x) {
                    const auto i=y*width+x;const bool foreground=x>=24+frame && x<72+frame && y>=24 && y<72;
                    colour[i]=foreground?0xFF2020E0U:0xFFE04020U;depth[i]=foreground?0.2f:0.8f;
                    motion[i]={std::uint16_t(foreground && frame?0xBC00:0),0};reactive[i]=0;
                }
                context->UpdateSubresource(inputs[0].Get(),0,nullptr,colour.data(),width*4,0);
                context->UpdateSubresource(inputs[1].Get(),0,nullptr,depth.data(),width*4,0);
                context->UpdateSubresource(inputs[2].Get(),0,nullptr,motion.data(),width*4,0);
                context->UpdateSubresource(inputs[3].Get(),0,nullptr,reactive.data(),width,0);
                upscale.publish(inputs[0].Get(),inputs[1].Get(),inputs[2].Get(),inputs[3].Get(),{0,0,16.666667f,0.1f,100,1.047198f,frame==0});
            }
            D3D11_TEXTURE2D_DESC desc{};upscale.image()->GetDesc(&desc);
            desc.BindFlags=desc.MiscFlags=0;desc.Usage=D3D11_USAGE_STAGING;desc.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
            ComPtr<ID3D11Texture2D> readback;checked(device->CreateTexture2D(&desc,nullptr,&readback),"Upscaling diagnostic readback");
            context->CopyResource(readback.Get(),upscale.image());
            D3D11_MAPPED_SUBRESOURCE mapped{};checked(context->Map(readback.Get(),0,D3D11_MAP_READ,0,&mapped),"Upscaling diagnostic map");
            Image image{width*2,height*2,std::vector<std::byte>(width*height*16)};bool valid=true;
            for(unsigned y=0;y<image.height;++y) {
                const auto* row=reinterpret_cast<const std::uint16_t*>(static_cast<const std::byte*>(mapped.pData)+y*mapped.RowPitch);
                for(unsigned x=0;x<image.width;++x) for(unsigned lane=0;lane<4;++lane) {
                    const auto value=half(row[x*4+lane]);valid&=std::isfinite(value);
                    image.rgba[(y*image.width+x)*4+lane]=std::byte(std::isfinite(value)?unsigned(std::clamp(value,0.0f,1.0f)*255+0.5f):0);
                }
            }
            context->Unmap(readback.Get(),0);
            const auto centre=(96*image.width+112)*4,background=(8*image.width+8)*4;
            valid&=unsigned(image.rgba[centre])>100 && unsigned(image.rgba[centre+2])<100 &&
                   unsigned(image.rgba[background+2])>100 && unsigned(image.rgba[background])<100;
            save_png(image,output/(std::string(name)+".png"));
            if(!valid) throw std::runtime_error("Upscaling output is non-finite or does not preserve foreground/background colour");
            renderer.upscaling(UpscaleMethod(kind));
            renderer.camera(1,0.1f,100,1.047198f);
            RenderState solid;solid.primitive=8;solid.final_inputs={0x00000004,0x00001400};
            solid.depth_test=solid.depth_write=true;
            auto hud=solid;hud.depth_test=hud.depth_write=false;
            renderer.prepare(solid);renderer.prepare(hud);
            for(unsigned frame=0;frame<12;++frame) {
                const auto x=-0.6f+float(frame)/64;
                std::array<Vertex,4> vertices{};
                constexpr std::array<Float4,4> positions{{{-0.5f,0.5f,0.2f,1},{0.5f,0.5f,0.2f,1},{0.5f,-0.5f,0.2f,1},{-0.5f,-0.5f,0.2f,1}}};
                for(unsigned i=0;i<4;++i) {vertices[i].position=positions[i];vertices[i].position[0]+=x+0.5f;vertices[i].color={0.9f,0.1f,0.1f,1};}
                renderer.begin(0xFF2040E0,1,true);renderer.draw(solid,vertices);
                for(unsigned i=0;i<4;++i) {
                    vertices[i].position=positions[i];vertices[i].position[0]=positions[i][0]*0.25f;
                    vertices[i].position[1]=positions[i][1]*0.25f-0.7f;vertices[i].color={1,1,1,1};
                }
                renderer.draw(hud,vertices);renderer.end();renderer.publish_frame(frame==0);
            }
            const auto game_image=renderer.read_frame();
            save_png(game_image,output/(std::string(name)+"-renderer.png"));
            if(game_image.width!=width*2 || game_image.height!=height*2)
                throw std::runtime_error("Renderer did not publish the reconstructed resolution");
            renderer.interpolation(true);renderer.resize(width+16,height+16);
            renderer.camera(2,0.1f,100,1.047198f);
            renderer.begin(0xFF2040E0,1,true);renderer.end();renderer.publish_frame(true);
            const auto resized=renderer.read_frame();
            if(resized.width!=(width+16)*2 || resized.height!=(height+16)*2)
                throw std::runtime_error("Upscaling retained the old resolution or interpolation history after resize");
            renderer.interpolation(false);
            renderer.upscaling(UpscaleMethod::off);
            renderer.resize(width,height);
            // Native AA and a 3x output reconstructed from a 2x render must
            // use the requested dimensions, rather than doubling every input.
            for(unsigned scale:{1U,3U}) {
                renderer.reset(scale==3?2:1);renderer.upscaling(UpscaleMethod(kind),scale);
                renderer.camera(3,0.1f,100,1.047198f);
                for(unsigned frame=0;frame<4;++frame) {
                    renderer.begin(0xFF2040E0,1,true);renderer.end();renderer.publish_frame(frame==0);
                }
                const auto reconstructed=renderer.read_frame();
                if(reconstructed.width!=width*scale || reconstructed.height!=height*scale)
                    throw std::runtime_error("Upscaling ignored the selected output resolution");
                const auto pixel=(reconstructed.height/2*reconstructed.width+reconstructed.width/2)*4;
                if(unsigned(reconstructed.rgba[pixel])>80 || unsigned(reconstructed.rgba[pixel+2])<160)
                    throw std::runtime_error("Native AA or custom-ratio reconstruction changed the source colour");
                save_png(reconstructed,output/std::format("{}-{}x.png",name,scale));
                renderer.upscaling(UpscaleMethod::off);
            }
            renderer.reset(1);
            ++passed;report<<",\"passed\":true";
        } catch(const std::exception& error) {report<<",\"passed\":false,\"error\":"<<json(error.what());}
        report<<'}';
    }
    const auto errors=renderer.errors();
    report<<"],\"passed\":"<<passed<<",\"failed\":"<<(3-passed)<<",\"debug_messages\":"<<errors.size()<<'}';return report.str();
}
}
