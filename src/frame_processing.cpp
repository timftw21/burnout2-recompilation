#include "frame_processing.h"
#include <climits>
#include "shaders.h"
#include <algorithm>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {void checked_flow(HRESULT value,const char* operation) {
    if(FAILED(value)) throw std::runtime_error(std::format("{}: 0x{:08X}",operation,unsigned(value)));
}}
FrameLayers::Images FrameLayers::history(ID3D11Device* device,unsigned width,unsigned height,bool render_targets) {
    Images images;
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=image.SampleDesc.Count=1;
    for(unsigned i=0;i<image_count;++i) {
        image.Format=!i?frame_color_format:i==1 || i==2?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM;
        image.BindFlags=D3D11_BIND_SHADER_RESOURCE|(render_targets && i?D3D11_BIND_RENDER_TARGET:0);
        checked_flow(device->CreateTexture2D(&image,nullptr,&images.textures[i]),"UI layer image");
        checked_flow(device->CreateShaderResourceView(images.textures[i].Get(),nullptr,&images.views[i]),"UI layer view");
        if(render_targets && i) checked_flow(device->CreateRenderTargetView(images.textures[i].Get(),nullptr,&images.targets[i-1]),"UI layer target");
    }
    return images;
}
FrameLayers::FrameLayers(ID3D11Device* device,ID3D11DeviceContext* context,unsigned width,unsigned height,unsigned samples,std::uint64_t& compilations,bool readable):context_(context) {
    const auto vertex=shader_binary(ShaderKind::display_vertex),pixel=shader_binary(samples>1?ShaderKind::layer_begin_ms:ShaderKind::layer_begin);
    compilations+=unsigned(vertex.compiled)+unsigned(pixel.compiled);
    checked_flow(device->CreateVertexShader(vertex.bytes.data(),vertex.bytes.size(),nullptr,&vertex_),"UI layer initialization vertex shader");
    checked_flow(device->CreatePixelShader(pixel.bytes.data(),pixel.bytes.size(),nullptr,&initialize_),"UI layer initialization shader");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=TRUE;raster.MultisampleEnable=samples>1;
    checked_flow(device->CreateRasterizerState(&raster,&raster_),"UI layer initialization rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked_flow(device->CreateDepthStencilState(&depth,&depth_),"UI layer initialization depth state");
    if(samples==1) {images_=history(device,width,height,true);bytes_=std::size_t(width)*height*(frame_color_bytes+24);return;}
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=1;
    image.SampleDesc.Count=1;image.Format=frame_color_format;image.BindFlags=D3D11_BIND_SHADER_RESOURCE;
    checked_flow(device->CreateTexture2D(&image,nullptr,&images_.textures[0]),"Scene before UI");
    checked_flow(device->CreateShaderResourceView(images_.textures[0].Get(),nullptr,&images_.views[0]),"Scene before UI view");
    bytes_=std::size_t(width)*height*frame_color_bytes;
    if(readable) {images_=history(device,width,height,false);bytes_+=std::size_t(width)*height*24;}
    image.BindFlags=D3D11_BIND_RENDER_TARGET;image.SampleDesc.Count=samples;
    for(unsigned i=0;i<basis_count;++i) {
        image.Format=i<2?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM;
        const auto pixel_bytes=i<2?8U:4U;
        checked_flow(device->CreateTexture2D(&image,nullptr,&multisample_[i]),"Multisample UI layer");
        bytes_+=std::size_t(width)*height*pixel_bytes*samples;
        checked_flow(device->CreateRenderTargetView(multisample_[i].Get(),nullptr,&images_.targets[i]),"UI layer target");
    }
}
const FrameLayers::Images& FrameLayers::resolved() {
    context_->OMSetRenderTargets(0,nullptr,nullptr);
    if(multisample_[0]) for(unsigned i=0;i<basis_count;++i)
        context_->ResolveSubresource(images_.textures[i+1].Get(),0,multisample_[i].Get(),0,i<2?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM);
    return images_;
}
void FrameLayers::begin(ID3D11Texture2D* scene,ID3D11ShaderResourceView* view,float uniform_alpha) {
    if(valid_) return;
    context_->OMSetRenderTargets(0,nullptr,nullptr);
    D3D11_TEXTURE2D_DESC desc{};scene->GetDesc(&desc);
    if(desc.SampleDesc.Count>1) context_->ResolveSubresource(images_.textures[0].Get(),0,scene,0,desc.Format);
    else context_->CopyResource(images_.textures[0].Get(),scene);
    // A full clear followed only by RGB writes leaves every alpha sample
    // identical. Native clears can initialize the bases without a fullscreen
    // sample-frequency shader. Unknown alpha still uses the exact sample path.
    if(uniform_alpha>=0) {
        for(unsigned i=0;i<basis_count;++i) {
            const float value=float(i&1),colour[]{value,value,value,uniform_alpha};
            context_->ClearRenderTargetView(images_.targets[i].Get(),colour);
        }
        valid_=true;return;
    }
    // Both basis images keep the original alpha, including individual MSAA
    // samples. Destination-alpha blends then have the same coefficients in
    // both images and remain separable without assuming an opaque scene.
    const D3D11_VIEWPORT viewport{0,0,float(desc.Width),float(desc.Height),0,1};context_->RSSetViewports(1,&viewport);
    context_->RSSetState(raster_.Get());context_->OMSetBlendState(nullptr,nullptr,UINT_MAX);context_->OMSetDepthStencilState(depth_.Get(),0);
    std::array<ID3D11RenderTargetView*,basis_count> targets{};
    for(unsigned i=0;i<basis_count;++i) targets[i]=images_.targets[i].Get();
    context_->OMSetRenderTargets(unsigned(targets.size()),targets.data(),nullptr);
    context_->IASetInputLayout(nullptr);context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->VSSetShader(vertex_.Get(),nullptr,0);context_->GSSetShader(nullptr,nullptr,0);context_->PSSetShader(initialize_.Get(),nullptr,0);
    context_->PSSetShaderResources(0,1,&view);context_->Draw(3,0);
    ID3D11ShaderResourceView* empty=nullptr;context_->PSSetShaderResources(0,1,&empty);context_->OMSetRenderTargets(0,nullptr,nullptr);
    valid_=true;
}
}
