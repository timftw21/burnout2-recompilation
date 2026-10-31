#include "hdr.h"
#include "shaders.h"
#include <algorithm>
#include <cmath>
#include <format>
#include <limits>
#include <stdexcept>

namespace b2 {
namespace {
void checked_hdr(HRESULT value,const char* operation) {
    if(FAILED(value)) throw std::runtime_error(std::format("{}: 0x{:08X}",operation,unsigned(value)));
}
float half_float(std::uint16_t bits) {
    const auto exponent=(bits>>10)&31,mantissa=bits&1023;
    const float value=exponent==31?(mantissa?std::numeric_limits<float>::quiet_NaN():std::numeric_limits<float>::infinity()):
        exponent?std::ldexp(1+mantissa/1024.0f,int(exponent)-15):std::ldexp(float(mantissa),-24);
    return bits&0x8000?-value:value;
}
}
FrameHdr::FrameHdr(ID3D11Device* device,ID3D11DeviceContext* context,unsigned width,unsigned height,std::uint64_t& compilations)
    :device_(device),context_(context),width_(width),height_(height) {
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=image.SampleDesc.Count=1;
    image.Format=DXGI_FORMAT_R16G16B16A16_FLOAT;image.BindFlags=D3D11_BIND_RENDER_TARGET|D3D11_BIND_SHADER_RESOURCE;
    checked_hdr(device->CreateTexture2D(&image,nullptr,&image_),"HDR image");
    checked_hdr(device->CreateShaderResourceView(image_.Get(),nullptr,&view_),"HDR image view");
    checked_hdr(device->CreateRenderTargetView(image_.Get(),nullptr,&target_),"HDR image target");
    const auto vertex=shader_binary(ShaderKind::display_vertex),pixel=shader_binary(ShaderKind::hdr_expand);
    compilations+=unsigned(vertex.compiled)+unsigned(pixel.compiled);
    checked_hdr(device->CreateVertexShader(vertex.bytes.data(),vertex.bytes.size(),nullptr,&vertex_),"HDR vertex shader");
    checked_hdr(device->CreatePixelShader(pixel.bytes.data(),pixel.bytes.size(),nullptr,&pixel_),"HDR expansion shader");
    D3D11_BUFFER_DESC buffer{};buffer.ByteWidth=16;buffer.BindFlags=D3D11_BIND_CONSTANT_BUFFER;
    checked_hdr(device->CreateBuffer(&buffer,nullptr,&constants_),"HDR constants");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=TRUE;
    checked_hdr(device->CreateRasterizerState(&raster,&raster_),"HDR rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked_hdr(device->CreateDepthStencilState(&depth,&depth_),"HDR depth state");
}
void FrameHdr::publish(ID3D11ShaderResourceView* image,ID3D11ShaderResourceView* scene,const FrameLayers::Images* layers,float paper_white,float peak) {
    const Float4 values{paper_white/80,peak/80,layers?1.0f:0.0f,0};
    context_->UpdateSubresource(constants_.Get(),0,nullptr,values.data(),0,0);
    auto constants=constants_.Get();context_->PSSetConstantBuffers(0,1,&constants);
    const D3D11_VIEWPORT viewport{0,0,float(width_),float(height_),0,1};context_->RSSetViewports(1,&viewport);
    context_->RSSetState(raster_.Get());context_->OMSetDepthStencilState(depth_.Get(),0);context_->OMSetBlendState(nullptr,nullptr,UINT_MAX);
    context_->IASetInputLayout(nullptr);context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->VSSetShader(vertex_.Get(),nullptr,0);context_->GSSetShader(nullptr,nullptr,0);context_->PSSetShader(pixel_.Get(),nullptr,0);
    auto target=target_.Get();context_->OMSetRenderTargets(1,&target,nullptr);
    std::array<ID3D11ShaderResourceView*,6> images{image,scene};
    if(layers) for(unsigned i=1;i<FrameLayers::image_count;++i) images[i+1]=layers->views[i].Get();
    context_->PSSetShaderResources(0,unsigned(images.size()),images.data());context_->Draw(3,0);
    images.fill(nullptr);context_->PSSetShaderResources(0,unsigned(images.size()),images.data());
    context_->OMSetRenderTargets(0,nullptr,nullptr);ready_=true;
}
HdrImage read_hdr_image(ID3D11Device* device,ID3D11DeviceContext* context,ID3D11Texture2D* source) {
    D3D11_TEXTURE2D_DESC description{};source->GetDesc(&description);
    if(description.Format!=DXGI_FORMAT_R16G16B16A16_FLOAT) throw std::runtime_error("HDR readback requires a floating-point image");
    description.BindFlags=description.MiscFlags=0;description.Usage=D3D11_USAGE_STAGING;description.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    Microsoft::WRL::ComPtr<ID3D11Texture2D> staging;
    checked_hdr(device->CreateTexture2D(&description,nullptr,&staging),"HDR explicit readback image");
    HdrImage image{description.Width,description.Height,std::vector<Float4>(std::size_t(description.Width)*description.Height)};
    context->CopyResource(staging.Get(),source);
    D3D11_MAPPED_SUBRESOURCE mapped{};checked_hdr(context->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped),"HDR explicit readback");
    for(unsigned y=0;y<image.height;++y) {
        const auto* row=reinterpret_cast<const std::uint16_t*>(static_cast<const std::byte*>(mapped.pData)+std::size_t(y)*mapped.RowPitch);
        for(unsigned x=0;x<image.width;++x) for(unsigned c=0;c<4;++c) image.pixels[std::size_t(y)*image.width+x][c]=half_float(row[x*4+c]);
    }
    context->Unmap(staging.Get(),0);return image;
}
HdrImage FrameHdr::readback() {
    if(!ready_) throw std::runtime_error("Publish an HDR frame before reading it");
    return read_hdr_image(device_,context_,image_.Get());
}
Image hdr_sdr_image(const HdrImage& source,float paper_white) {
    Image image{source.width,source.height,std::vector<std::byte>(source.pixels.size()*4)};
    for(std::size_t i=0;i<source.pixels.size();++i) for(unsigned c=0;c<4;++c) {
        auto value=std::clamp(source.pixels[i][c]*(c==3?1:80/paper_white),0.0f,1.0f);
        if(c!=3) value=value<=0.0031308f?value*12.92f:1.055f*std::pow(value,1/2.4f)-0.055f;
        image.rgba[i*4+c]=std::byte(static_cast<unsigned>(std::lround(value*255)));
    }
    return image;
}
std::string hdr_color_source() {return R"(
cbuffer DisplayColor:register(b1){float hdr_display;float hdr_white;float hdr_source;float hdr_reserved;}
float3 sdrLinear(float3 color) {return color<=0.04045?color/12.92:pow((max(color,0)+0.055)/1.055,2.4);}
)";}
std::string hdr_shader_source() {return hdr_color_source()+R"(
cbuffer Hdr:register(b0){float paperWhite;float peakWhite;float layered;float reserved;}
Texture2D<float4> image:register(t0);Texture2D<float4> scene:register(t1);
Texture2D<float4> black:register(t2);Texture2D<float4> white:register(t3);
Texture2D<float4> lower:register(t4);Texture2D<float4> upper:register(t5);
float4 main(float4 position:SV_Position):SV_Target {
 int3 p=int3(position.xy,0);float4 color=image.Load(p);float3 base=scene.Load(p).rgb;
 float3 weight=1;
 if(layered) {
  float3 b=black.Load(p).rgb,a=white.Load(p).rgb-b,composed=b+base*a;
  // The slope of the retained, clipped UI transform measures how much scene
  // remains visible. Opaque HUD pixels have zero slope and stay at paper white.
  weight=saturate(a)*step(lower.Load(p).rgb-0.00001,composed)*step(composed,upper.Load(p).rgb+0.00001);
 }
 float3 sceneLinear=sdrLinear(base);float maximum=max(sceneLinear.r,max(sceneLinear.g,sceneLinear.b));
 float shoulder=saturate((maximum-0.5)*2);
 float3 result=sdrLinear(color.rgb)*paperWhite+sceneLinear*(peakWhite-paperWhite)*shoulder*shoulder*weight;
 result*=min(1,peakWhite/max(max(result.r,max(result.g,result.b)),0.00001));
 return float4(result,color.a);
})";}
}
