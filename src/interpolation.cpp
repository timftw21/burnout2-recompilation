#include "interpolation.h"
#include "shaders.h"
#include <algorithm>
#include <cstring>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {void checked_flow(HRESULT value,const char* operation) {
    if(FAILED(value)) throw std::runtime_error(std::format("{}: 0x{:08X}",operation,unsigned(value)));
}}
FrameInterpolation::FrameInterpolation(ID3D11Device* device,ID3D11DeviceContext* context,unsigned width,unsigned height,std::uint64_t& compilations):context_(context) {
    const auto shader=[&](ShaderKind kind) {const auto binary=shader_binary(kind);compilations+=binary.compiled;return binary.bytes;};
    const auto vs=shader(ShaderKind::display_vertex),luma=shader(ShaderKind::flow_luma),pixel=shader(ShaderKind::flow_interpolate);
    const auto estimate=shader(ShaderKind::flow_estimate),cut=shader(ShaderKind::flow_cut);
    checked_flow(device->CreateVertexShader(vs.data(),vs.size(),nullptr,&vertex_),"Interpolation vertex shader");
    checked_flow(device->CreatePixelShader(luma.data(),luma.size(),nullptr,&luma_),"Luminance shader");
    checked_flow(device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&interpolate_),"Interpolation shader");
    checked_flow(device->CreateComputeShader(estimate.data(),estimate.size(),nullptr,&estimate_),"Motion shader");
    checked_flow(device->CreateComputeShader(cut.data(),cut.size(),nullptr,&detect_cut_),"Scene-cut shader");
    D3D11_BUFFER_DESC buffer{16,D3D11_USAGE_DYNAMIC,D3D11_BIND_CONSTANT_BUFFER,D3D11_CPU_ACCESS_WRITE,0,0};
    checked_flow(device->CreateBuffer(&buffer,nullptr,&constants_),"Interpolation constants");
    D3D11_SAMPLER_DESC sampler{};sampler.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;sampler.AddressU=sampler.AddressV=sampler.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD=D3D11_FLOAT32_MAX;sampler.ComparisonFunc=D3D11_COMPARISON_ALWAYS;
    checked_flow(device->CreateSamplerState(&sampler,&sampler_),"Motion sampler");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=true;
    checked_flow(device->CreateRasterizerState(&raster,&raster_),"Luminance rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked_flow(device->CreateDepthStencilState(&depth,&depth_),"Luminance depth state");
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=1;
    image.Format=DXGI_FORMAT_R8G8B8A8_UNORM;image.SampleDesc.Count=1;image.BindFlags=D3D11_BIND_SHADER_RESOURCE;
    for(auto& frame:frame_) {
        checked_flow(device->CreateTexture2D(&image,nullptr,&frame.image),"Interpolation history");
        checked_flow(device->CreateShaderResourceView(frame.image.Get(),nullptr,&frame.image_view),"History view");
        auto pyramid=image;pyramid.Width=160;pyramid.Height=120;pyramid.MipLevels=4;pyramid.Format=DXGI_FORMAT_R16_FLOAT;
        pyramid.BindFlags|=D3D11_BIND_RENDER_TARGET;pyramid.MiscFlags=D3D11_RESOURCE_MISC_GENERATE_MIPS;
        checked_flow(device->CreateTexture2D(&pyramid,nullptr,&frame.luma),"Luminance pyramid");
        checked_flow(device->CreateShaderResourceView(frame.luma.Get(),nullptr,&frame.luma_view),"Pyramid view");
        D3D11_RENDER_TARGET_VIEW_DESC view{};view.Format=pyramid.Format;view.ViewDimension=D3D11_RTV_DIMENSION_TEXTURE2D;
        checked_flow(device->CreateRenderTargetView(frame.luma.Get(),&view,&frame.luma_target),"Pyramid target");
    }
    const auto output=[&](Flow& flow,unsigned w,unsigned h,DXGI_FORMAT format) {
        auto description=image;description.Width=w;description.Height=h;description.Format=format;
        description.BindFlags|=D3D11_BIND_UNORDERED_ACCESS;Ptr<ID3D11Texture2D> texture;
        checked_flow(device->CreateTexture2D(&description,nullptr,&texture),"Motion storage");
        checked_flow(device->CreateShaderResourceView(texture.Get(),nullptr,&flow.view),"Motion view");
        checked_flow(device->CreateUnorderedAccessView(texture.Get(),nullptr,&flow.output),"Motion output");
    };
    for(auto& direction:flow_) for(unsigned level=0;level<4;++level) output(direction[level],160>>level,120>>level,DXGI_FORMAT_R16G16B16A16_FLOAT);
    output(cut_,1,1,DXGI_FORMAT_R32_FLOAT);
}
void FrameInterpolation::constants(float phase,unsigned level,bool first) {
    struct Constants {float phase;unsigned level,first,spare;};const Constants value{phase,level,unsigned(first),0};
    D3D11_MAPPED_SUBRESOURCE mapped{};checked_flow(context_->Map(constants_.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Motion constants map");
    std::memcpy(mapped.pData,&value,sizeof(value));context_->Unmap(constants_.Get(),0);
    auto buffer=constants_.Get();context_->CSSetConstantBuffers(0,1,&buffer);context_->PSSetConstantBuffers(0,1,&buffer);
}
void FrameInterpolation::unbind() {
    std::array<ID3D11ShaderResourceView*,5> empty{};ID3D11UnorderedAccessView* output=nullptr;
    context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);context_->CSSetShaderResources(0,5,empty.data());
    context_->PSSetShaderResources(0,5,empty.data());context_->OMSetRenderTargets(0,nullptr,nullptr);
}
void FrameInterpolation::publish(ID3D11Texture2D* image,bool reset) {
    unbind();if(reset) frames_=0;std::swap(frame_[0],frame_[1]);context_->CopyResource(frame_[1].image.Get(),image);
    const D3D11_VIEWPORT viewport{0,0,160,120,0,1};context_->RSSetViewports(1,&viewport);context_->RSSetState(raster_.Get());
    context_->OMSetDepthStencilState(depth_.Get(),0);context_->OMSetBlendState(nullptr,nullptr,UINT_MAX);
    auto target=frame_[1].luma_target.Get();context_->OMSetRenderTargets(1,&target,nullptr);
    context_->IASetInputLayout(nullptr);context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->VSSetShader(vertex_.Get(),nullptr,0);context_->PSSetShader(luma_.Get(),nullptr,0);
    auto view=frame_[1].image_view.Get();auto sampler=sampler_.Get();context_->PSSetShaderResources(0,1,&view);context_->PSSetSamplers(0,1,&sampler);
    context_->Draw(3,0);unbind();context_->GenerateMips(frame_[1].luma_view.Get());
    if(++frames_<2) return;
    frames_=2;
    context_->CSSetSamplers(0,1,&sampler);context_->CSSetShader(estimate_.Get(),nullptr,0);
    for(unsigned direction=0;direction<2;++direction) for(int level=3;level>=0;--level) {
        const auto coarse=level==3?0:level+1;
        std::array<ID3D11ShaderResourceView*,3> inputs{frame_[direction].luma_view.Get(),frame_[1-direction].luma_view.Get(),flow_[direction][coarse].view.Get()};
        auto output=flow_[direction][level].output.Get();constants(1,unsigned(level),level==3);
        context_->CSSetShaderResources(0,3,inputs.data());context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);
        context_->Dispatch(((160>>level)+7)/8,((120>>level)+7)/8,1);unbind();
    }
    context_->CSSetShader(detect_cut_.Get(),nullptr,0);
    std::array<ID3D11ShaderResourceView*,3> inputs{frame_[0].luma_view.Get(),frame_[1].luma_view.Get(),flow_[0][0].view.Get()};
    auto output=cut_.output.Get();context_->CSSetShaderResources(0,3,inputs.data());context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);
    context_->Dispatch(1,1,1);unbind();context_->CSSetShader(nullptr,nullptr,0);
}
void FrameInterpolation::bind(float phase) {
    constants(phase);
    std::array<ID3D11ShaderResourceView*,5> inputs{frame_[0].image_view.Get(),frame_[1].image_view.Get(),flow_[0][0].view.Get(),flow_[1][0].view.Get(),cut_.view.Get()};
    context_->PSSetShaderResources(0,5,inputs.data());context_->PSSetShader(interpolate_.Get(),nullptr,0);
}
}
