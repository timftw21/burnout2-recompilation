#include "antialiasing.h"
#include "shaders.h"
#include <cstring>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {
void checked_aa(HRESULT status,const char* operation) {
    if(FAILED(status)) throw std::runtime_error(std::format("{} failed (0x{:08X})",operation,static_cast<unsigned>(status)));
}
}
FrameAntialiasing::FrameAntialiasing(ID3D11Device* device,ID3D11DeviceContext* context,unsigned width,unsigned height,unsigned quality,std::uint64_t& compilations)
    :device_(device),context_(context),width_(width),height_(height) {
    if(!width || !height || quality<1 || quality>3) throw std::runtime_error("Invalid antialiasing dimensions or quality.");
    const auto vertex=shader_binary(ShaderKind::display_vertex),pixel=shader_binary(ShaderKind::fxaa);
    compilations+=unsigned(vertex.compiled)+unsigned(pixel.compiled);
    checked_aa(device->CreateVertexShader(vertex.bytes.data(),vertex.bytes.size(),nullptr,&vertex_),"AA vertex shader");
    checked_aa(device->CreatePixelShader(pixel.bytes.data(),pixel.bytes.size(),nullptr,&pixel_),"FXAA shader");
    D3D11_TEXTURE2D_DESC texture{};texture.Width=width;texture.Height=height;texture.MipLevels=texture.ArraySize=texture.SampleDesc.Count=1;
    texture.Format=DXGI_FORMAT_R8G8B8A8_UNORM;texture.BindFlags=D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_RENDER_TARGET;
    checked_aa(device->CreateTexture2D(&texture,nullptr,&image_),"AA image");
    checked_aa(device->CreateShaderResourceView(image_.Get(),nullptr,&view_),"AA texture view");
    checked_aa(device->CreateRenderTargetView(image_.Get(),nullptr,&target_),"AA target view");
    const Float4 parameters{1.0f/width,1.0f/height,float(quality),0};
    D3D11_BUFFER_DESC buffer{};buffer.ByteWidth=sizeof(parameters);buffer.Usage=D3D11_USAGE_IMMUTABLE;buffer.BindFlags=D3D11_BIND_CONSTANT_BUFFER;
    D3D11_SUBRESOURCE_DATA initial{parameters.data()};
    checked_aa(device->CreateBuffer(&buffer,&initial,&constants_),"AA parameters");
    D3D11_SAMPLER_DESC sampler{};sampler.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU=sampler.AddressV=sampler.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;sampler.MaxLOD=D3D11_FLOAT32_MAX;sampler.ComparisonFunc=D3D11_COMPARISON_ALWAYS;
    checked_aa(device->CreateSamplerState(&sampler,&sampler_),"AA sampler");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=TRUE;
    checked_aa(device->CreateRasterizerState(&raster,&raster_),"AA rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked_aa(device->CreateDepthStencilState(&depth,&depth_),"AA depth state");
}
void FrameAntialiasing::publish(ID3D11ShaderResourceView* source) {
    auto target=target_.Get();context_->OMSetRenderTargets(1,&target,nullptr);
    const D3D11_VIEWPORT viewport{0,0,float(width_),float(height_),0,1};context_->RSSetViewports(1,&viewport);
    context_->RSSetState(raster_.Get());context_->OMSetDepthStencilState(depth_.Get(),0);context_->OMSetBlendState(nullptr,nullptr,UINT_MAX);
    context_->IASetInputLayout(nullptr);context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->VSSetShader(vertex_.Get(),nullptr,0);context_->PSSetShader(pixel_.Get(),nullptr,0);
    auto sampler=sampler_.Get();auto constants=constants_.Get();context_->PSSetSamplers(0,1,&sampler);context_->PSSetConstantBuffers(0,1,&constants);
    context_->PSSetShaderResources(0,1,&source);context_->Draw(3,0);
    ID3D11ShaderResourceView* empty=nullptr;context_->PSSetShaderResources(0,1,&empty);context_->OMSetRenderTargets(0,nullptr,nullptr);
    ready_=true;
}
Image FrameAntialiasing::readback() {
    if(!ready_) throw std::runtime_error("Publish a frame before reading its antialiasing result.");
    D3D11_TEXTURE2D_DESC description{};image_->GetDesc(&description);
    description.BindFlags=0;description.Usage=D3D11_USAGE_STAGING;description.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    Ptr<ID3D11Texture2D> staging;checked_aa(device_->CreateTexture2D(&description,nullptr,&staging),"AA diagnostic storage");
    context_->CopyResource(staging.Get(),image_.Get());D3D11_MAPPED_SUBRESOURCE mapped{};
    checked_aa(context_->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped),"AA diagnostic read");
    Image result{width_,height_,std::vector<std::byte>(bytes())};
    for(unsigned row=0;row<height_;++row) std::memcpy(result.rgba.data()+std::size_t(row)*width_*4,static_cast<const std::byte*>(mapped.pData)+std::size_t(row)*mapped.RowPitch,width_*4);
    context_->Unmap(staging.Get(),0);return result;
}
MultisampleCopy::MultisampleCopy(ID3D11Device* device,ID3D11DeviceContext* context,std::uint64_t& compilations):context_(context) {
    const auto vertex=shader_binary(ShaderKind::display_vertex),pixel=shader_binary(ShaderKind::copy_multisample),stencil=shader_binary(ShaderKind::copy_multisample_stencil);
    compilations+=unsigned(vertex.compiled)+unsigned(pixel.compiled)+unsigned(stencil.compiled);
    checked_aa(device->CreateVertexShader(vertex.bytes.data(),vertex.bytes.size(),nullptr,&vertex_),"MSAA transfer vertex shader");
    checked_aa(device->CreatePixelShader(pixel.bytes.data(),pixel.bytes.size(),nullptr,&pixel_),"MSAA colour/depth transfer shader");
    checked_aa(device->CreatePixelShader(stencil.bytes.data(),stencil.bytes.size(),nullptr,&stencil_),"MSAA stencil transfer shader");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=raster.MultisampleEnable=TRUE;
    checked_aa(device->CreateRasterizerState(&raster,&raster_),"MSAA transfer rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthEnable=TRUE;depth.DepthWriteMask=D3D11_DEPTH_WRITE_MASK_ALL;depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked_aa(device->CreateDepthStencilState(&depth,&depth_),"MSAA depth transfer state");
    depth.DepthEnable=FALSE;depth.DepthWriteMask=D3D11_DEPTH_WRITE_MASK_ZERO;depth.StencilEnable=TRUE;depth.StencilReadMask=255;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_REPLACE,D3D11_STENCIL_OP_REPLACE,D3D11_STENCIL_OP_REPLACE,D3D11_COMPARISON_ALWAYS};
    for(unsigned bit=0;bit<8;++bit) {
        depth.StencilWriteMask=static_cast<UINT8>(1U<<bit);
        checked_aa(device->CreateDepthStencilState(&depth,&stencil_states_[bit]),"MSAA stencil transfer state");
        const std::array<unsigned,4> mask{1U<<bit,0,0,0};D3D11_BUFFER_DESC buffer{};buffer.ByteWidth=sizeof(mask);
        buffer.Usage=D3D11_USAGE_IMMUTABLE;buffer.BindFlags=D3D11_BIND_CONSTANT_BUFFER;const D3D11_SUBRESOURCE_DATA data{mask.data()};
        checked_aa(device->CreateBuffer(&buffer,&data,&stencil_masks_[bit]),"MSAA stencil transfer mask");
    }
}
void MultisampleCopy::grow(ID3D11ShaderResourceView* color,ID3D11ShaderResourceView* depth,ID3D11ShaderResourceView* stencil,
                           ID3D11RenderTargetView* target,ID3D11DepthStencilView* destination,unsigned width,unsigned height) {
    const D3D11_VIEWPORT viewport{0,0,float(width),float(height),0,1};context_->RSSetViewports(1,&viewport);
    context_->RSSetState(raster_.Get());context_->OMSetBlendState(nullptr,nullptr,UINT_MAX);
    context_->IASetInputLayout(nullptr);context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->VSSetShader(vertex_.Get(),nullptr,0);context_->PSSetShader(pixel_.Get(),nullptr,0);
    context_->OMSetRenderTargets(1,&target,destination);context_->OMSetDepthStencilState(depth_.Get(),0);
    ID3D11ShaderResourceView* sources[]={color,depth,stencil};context_->PSSetShaderResources(0,3,sources);context_->Draw(3,0);
    if(stencil) {
        context_->OMSetRenderTargets(0,nullptr,destination);context_->PSSetShader(stencil_.Get(),nullptr,0);
        for(unsigned bit=0;bit<8;++bit) {
            context_->OMSetDepthStencilState(stencil_states_[bit].Get(),1U<<bit);
            auto buffer=stencil_masks_[bit].Get();context_->PSSetConstantBuffers(0,1,&buffer);context_->Draw(3,0);
        }
    }
    ID3D11ShaderResourceView* empty[3]{};context_->PSSetShaderResources(0,3,empty);context_->OMSetRenderTargets(0,nullptr,nullptr);
}
}
