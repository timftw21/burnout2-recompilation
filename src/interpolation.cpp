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
FrameLayers::Images FrameLayers::history(ID3D11Device* device,unsigned width,unsigned height,bool render_targets) {
    Images images;
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=image.SampleDesc.Count=1;
    for(unsigned i=0;i<image_count;++i) {
        image.Format=i==1 || i==2?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM;
        image.BindFlags=D3D11_BIND_SHADER_RESOURCE|(render_targets && i?D3D11_BIND_RENDER_TARGET:0);
        checked_flow(device->CreateTexture2D(&image,nullptr,&images.textures[i]),"UI layer image");
        checked_flow(device->CreateShaderResourceView(images.textures[i].Get(),nullptr,&images.views[i]),"UI layer view");
        if(render_targets && i) checked_flow(device->CreateRenderTargetView(images.textures[i].Get(),nullptr,&images.targets[i-1]),"UI layer target");
    }
    return images;
}
FrameLayers::FrameLayers(ID3D11Device* device,ID3D11DeviceContext* context,unsigned width,unsigned height,unsigned samples,std::uint64_t& compilations):context_(context) {
    const auto vertex=shader_binary(ShaderKind::display_vertex),pixel=shader_binary(samples>1?ShaderKind::layer_begin_ms:ShaderKind::layer_begin);
    compilations+=unsigned(vertex.compiled)+unsigned(pixel.compiled);
    checked_flow(device->CreateVertexShader(vertex.bytes.data(),vertex.bytes.size(),nullptr,&vertex_),"UI layer initialization vertex shader");
    checked_flow(device->CreatePixelShader(pixel.bytes.data(),pixel.bytes.size(),nullptr,&initialize_),"UI layer initialization shader");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=TRUE;raster.MultisampleEnable=samples>1;
    checked_flow(device->CreateRasterizerState(&raster,&raster_),"UI layer initialization rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked_flow(device->CreateDepthStencilState(&depth,&depth_),"UI layer initialization depth state");
    if(samples==1) {images_=history(device,width,height,true);bytes_=std::size_t(width)*height*28;return;}
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=1;
    image.SampleDesc.Count=1;image.Format=DXGI_FORMAT_R8G8B8A8_UNORM;image.BindFlags=D3D11_BIND_SHADER_RESOURCE;
    checked_flow(device->CreateTexture2D(&image,nullptr,&images_.textures[0]),"Scene before UI");
    checked_flow(device->CreateShaderResourceView(images_.textures[0].Get(),nullptr,&images_.views[0]),"Scene before UI view");
    bytes_=std::size_t(width)*height*4;
    image.BindFlags=D3D11_BIND_RENDER_TARGET;image.SampleDesc.Count=samples;
    for(unsigned i=0;i<basis_count;++i) {
        image.Format=i<2?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM;
        const auto pixel_bytes=i<2?8U:4U;
        checked_flow(device->CreateTexture2D(&image,nullptr,&multisample_[i]),"Multisample UI layer");
        bytes_+=std::size_t(width)*height*pixel_bytes*samples;
        checked_flow(device->CreateRenderTargetView(multisample_[i].Get(),nullptr,&images_.targets[i]),"UI layer target");
    }
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
void FrameLayers::publish(Images& history) {
    // Recycle the oldest history as the next capture. The immediate context
    // orders reuse after earlier reads, without copies or CPU/GPU waits.
    if(!multisample_[0]) {std::swap(images_,history);return;}
    images_.textures[0].Swap(history.textures[0]);images_.views[0].Swap(history.views[0]);
    for(unsigned i=0;i<basis_count;++i)
        context_->ResolveSubresource(history.textures[i+1].Get(),0,multisample_[i].Get(),0,i<2?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM);
}
FrameInterpolation::FrameInterpolation(ID3D11Device* device,ID3D11DeviceContext* context,unsigned width,unsigned height,std::uint64_t& compilations,bool interpolation_support,bool separate_layers,unsigned samples,bool capture_depth):context_(context),
    width_(std::clamp(width,16U,640U)),height_(std::clamp(height,16U,480U)),interpolation_support_(interpolation_support) {
    bytes_=std::size_t(width)*height*(8+(separate_layers?56:0))+4+32+levels*32;
    for(unsigned level=0;level<levels;++level) {
        const auto w=width_>>level,h=height_>>level;
        bytes_+=std::size_t(w)*h*32+std::size_t(w+2)*(h+2)*64;
    }
    if(interpolation_support_) bytes_+=std::size_t(width_)*height_*(16+(capture_depth?8:0));
    const auto shader=[&](ShaderKind kind) {const auto binary=shader_binary(kind);compilations+=binary.compiled;return binary.bytes;};
    const auto vs=shader(ShaderKind::display_vertex),colour=shader(ShaderKind::flow_colour);
    const auto estimate=shader(ShaderKind::flow_estimate),cut=shader(ShaderKind::flow_cut),gradient=shader(ShaderKind::flow_gradient);
    checked_flow(device->CreateVertexShader(vs.data(),vs.size(),nullptr,&vertex_),"Interpolation vertex shader");
    checked_flow(device->CreatePixelShader(colour.data(),colour.size(),nullptr,&downsample_),"Colour shader");
    checked_flow(device->CreateComputeShader(estimate.data(),estimate.size(),nullptr,&estimate_),"Motion shader");
    checked_flow(device->CreateComputeShader(cut.data(),cut.size(),nullptr,&detect_cut_),"Scene-cut shader");
    checked_flow(device->CreateComputeShader(gradient.data(),gradient.size(),nullptr,&gradient_),"Motion gradient shader");
    if(interpolation_support_) {
        const auto pixel=shader(ShaderKind::flow_interpolate),resolve=shader(ShaderKind::flow_resolve);
        checked_flow(device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&interpolate_),"Interpolation shader");
        checked_flow(device->CreateComputeShader(resolve.data(),resolve.size(),nullptr,&resolve_),"Motion support shader");
        if(capture_depth) {
            const auto depth=shader(samples>1?ShaderKind::flow_depth_ms:ShaderKind::flow_depth);
            checked_flow(device->CreateComputeShader(depth.data(),depth.size(),nullptr,&depth_),"Motion depth shader");
        }
    }
    for(unsigned level=0;level<levels;++level) {
        const Constants value{1,level,unsigned(level==levels-1),0,width_,height_,{}};const D3D11_SUBRESOURCE_DATA data{&value,0,0};
        const D3D11_BUFFER_DESC buffer{sizeof(Constants),D3D11_USAGE_IMMUTABLE,D3D11_BIND_CONSTANT_BUFFER,0,0,0};
        checked_flow(device->CreateBuffer(&buffer,&data,&compute_constants_[level]),"Fixed motion constants");
    }
    const D3D11_BUFFER_DESC buffer{sizeof(Constants),D3D11_USAGE_DYNAMIC,D3D11_BIND_CONSTANT_BUFFER,D3D11_CPU_ACCESS_WRITE,0,0};
    checked_flow(device->CreateBuffer(&buffer,nullptr,&constants_),"Interpolation constants");
    D3D11_SAMPLER_DESC sampler{};sampler.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;sampler.AddressU=sampler.AddressV=sampler.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler.MaxLOD=D3D11_FLOAT32_MAX;sampler.ComparisonFunc=D3D11_COMPARISON_ALWAYS;
    checked_flow(device->CreateSamplerState(&sampler,&sampler_),"Motion sampler");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=true;
    checked_flow(device->CreateRasterizerState(&raster,&raster_),"Luminance rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked_flow(device->CreateDepthStencilState(&depth,&depth_state_),"Luminance depth state");
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=1;
    image.Format=DXGI_FORMAT_R8G8B8A8_UNORM;image.SampleDesc.Count=1;image.BindFlags=D3D11_BIND_SHADER_RESOURCE;
    for(auto& frame:frame_) {
        checked_flow(device->CreateTexture2D(&image,nullptr,&frame.image),"Interpolation history");
        checked_flow(device->CreateShaderResourceView(frame.image.Get(),nullptr,&frame.image_view),"History view");
        if(separate_layers) frame.layers=FrameLayers::history(device,width,height,samples==1);
        auto pyramid=image;pyramid.Width=width_;pyramid.Height=height_;pyramid.MipLevels=levels;pyramid.Format=DXGI_FORMAT_R16G16B16A16_FLOAT;
        pyramid.BindFlags|=D3D11_BIND_RENDER_TARGET;pyramid.MiscFlags=D3D11_RESOURCE_MISC_GENERATE_MIPS;
        checked_flow(device->CreateTexture2D(&pyramid,nullptr,&frame.pyramid),"Colour pyramid");
        checked_flow(device->CreateShaderResourceView(frame.pyramid.Get(),nullptr,&frame.pyramid_view),"Pyramid view");
        D3D11_RENDER_TARGET_VIEW_DESC view{};view.Format=pyramid.Format;view.ViewDimension=D3D11_RTV_DIMENSION_TEXTURE2D;
        checked_flow(device->CreateRenderTargetView(frame.pyramid.Get(),&view,&frame.pyramid_target),"Pyramid target");
    }
    const auto output=[&](Flow& flow,unsigned w,unsigned h,DXGI_FORMAT format) {
        auto description=image;description.Width=w;description.Height=h;description.Format=format;
        description.BindFlags|=D3D11_BIND_UNORDERED_ACCESS;Ptr<ID3D11Texture2D> texture;
        checked_flow(device->CreateTexture2D(&description,nullptr,&texture),"Motion storage");
        checked_flow(device->CreateShaderResourceView(texture.Get(),nullptr,&flow.view),"Motion view");
        checked_flow(device->CreateUnorderedAccessView(texture.Get(),nullptr,&flow.output),"Motion output");
    };
    for(auto& direction:flow_) for(unsigned level=0;level<levels;++level) output(direction[level],width_>>level,height_>>level,DXGI_FORMAT_R16G16B16A16_FLOAT);
    if(interpolation_support_) for(auto& direction:resolved_) output(direction,width_,height_,DXGI_FORMAT_R16G16B16A16_FLOAT);
    if(depth_) for(auto& frame:frame_) output(frame.depth,width_,height_,DXGI_FORMAT_R32_FLOAT);
    for(auto& frame:frame_) for(unsigned level=0;level<levels;++level) for(auto& gradient:frame.gradients[level])
        output(gradient,(width_>>level)+2,(height_>>level)+2,DXGI_FORMAT_R32G32B32A32_FLOAT);
    output(cut_,1,1,DXGI_FORMAT_R32_FLOAT);
}
void FrameInterpolation::compute_constants(unsigned level) {
    auto buffer=compute_constants_[level].Get();context_->CSSetConstantBuffers(0,1,&buffer);context_->PSSetConstantBuffers(0,1,&buffer);
}
void FrameInterpolation::constants(float phase) {
    const Constants value{phase,0,0,unsigned(frame_[0].layered)|(unsigned(frame_[1].layered)<<1),width_,height_,{}};
    D3D11_MAPPED_SUBRESOURCE mapped{};checked_flow(context_->Map(constants_.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Motion constants map");
    std::memcpy(mapped.pData,&value,sizeof(value));context_->Unmap(constants_.Get(),0);
    auto buffer=constants_.Get();context_->CSSetConstantBuffers(0,1,&buffer);context_->PSSetConstantBuffers(0,1,&buffer);
}
void FrameInterpolation::unbind(bool target) {
    std::array<ID3D11ShaderResourceView*,19> empty{};std::array<ID3D11UnorderedAccessView*,2> outputs{};
    context_->CSSetUnorderedAccessViews(0,unsigned(outputs.size()),outputs.data(),nullptr);context_->CSSetShaderResources(0,unsigned(empty.size()),empty.data());
    context_->PSSetShaderResources(0,unsigned(empty.size()),empty.data());if(target) context_->OMSetRenderTargets(0,nullptr,nullptr);
}
void FrameInterpolation::publish(ID3D11Texture2D* image,bool reset,FrameLayers* layers,ID3D11ShaderResourceView* depth,const MotionCamera* camera) {
    unbind();if(reset) frames_=0;std::swap(frame_[0],frame_[1]);context_->CopyResource(frame_[1].image.Get(),image);
    auto& latest=frame_[1];latest.layered=layers && layers->valid();latest.camera=camera?*camera:MotionCamera{};
    if(latest.layered) layers->publish(latest.layers);
    if(layers) layers->invalidate();
    latest.depth_valid=depth && depth_;
    if(latest.depth_valid) {
        auto output=latest.depth.output.Get();context_->CSSetShader(depth_.Get(),nullptr,0);context_->CSSetShaderResources(0,1,&depth);
        context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);context_->Dispatch((width_+7)/8,(height_+7)/8,1);unbind();
    }
    const D3D11_VIEWPORT viewport{0,0,float(width_),float(height_),0,1};context_->RSSetViewports(1,&viewport);context_->RSSetState(raster_.Get());
    context_->OMSetDepthStencilState(depth_state_.Get(),0);context_->OMSetBlendState(nullptr,nullptr,UINT_MAX);
    auto target=latest.pyramid_target.Get();context_->OMSetRenderTargets(1,&target,nullptr);
    context_->IASetInputLayout(nullptr);context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->VSSetShader(vertex_.Get(),nullptr,0);context_->PSSetShader(downsample_.Get(),nullptr,0);
    auto view=latest.scene();auto sampler=sampler_.Get();context_->PSSetShaderResources(0,1,&view);context_->PSSetSamplers(0,1,&sampler);
    compute_constants();context_->Draw(3,0);unbind();context_->GenerateMips(latest.pyramid_view.Get());
    context_->CSSetShader(gradient_.Get(),nullptr,0);
    for(unsigned level=0;level<levels;++level) {
        auto source=latest.pyramid_view.Get();compute_constants(level);
        std::array<ID3D11UnorderedAccessView*,2> outputs{latest.gradients[level][0].output.Get(),latest.gradients[level][1].output.Get()};
        context_->CSSetShaderResources(0,1,&source);context_->CSSetUnorderedAccessViews(0,2,outputs.data(),nullptr);
        context_->Dispatch(((width_>>level)+9)/8,((height_>>level)+9)/8,1);unbind();
    }
    if(++frames_<2) {context_->CSSetShader(nullptr,nullptr,0);return;}
    frames_=2;context_->CSSetSamplers(0,1,&sampler);context_->CSSetShader(estimate_.Get(),nullptr,0);
    for(unsigned direction=0;direction<2;++direction) for(int level=levels-1;level>=0;--level) {
        const auto coarse=level==levels-1?0:level+1;
        std::array<ID3D11ShaderResourceView*,7> inputs{frame_[direction].pyramid_view.Get(),frame_[1-direction].pyramid_view.Get(),flow_[direction][coarse].view.Get(),
            frame_[direction].scene(),frame_[1-direction].scene(),frame_[1-direction].gradients[level][0].view.Get(),frame_[1-direction].gradients[level][1].view.Get()};
        auto output=flow_[direction][level].output.Get();compute_constants(unsigned(level));
        context_->CSSetShaderResources(0,unsigned(inputs.size()),inputs.data());context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);
        context_->Dispatch(((width_>>level)+7)/8,((height_>>level)+7)/8,1);unbind();
    }
    if(interpolation_support_) {
        context_->CSSetShader(resolve_.Get(),nullptr,0);
        for(unsigned direction=0;direction<2;++direction) {
            std::array<ID3D11ShaderResourceView*,4> inputs{frame_[direction].scene(),frame_[1-direction].scene(),flow_[direction][0].view.Get(),flow_[1-direction][0].view.Get()};
            auto output=resolved_[direction].output.Get();context_->CSSetShaderResources(0,unsigned(inputs.size()),inputs.data());context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);
            context_->Dispatch((width_+7)/8,(height_+7)/8,1);unbind();
        }
    }
    context_->CSSetShader(detect_cut_.Get(),nullptr,0);
    std::array<ID3D11ShaderResourceView*,3> inputs{frame_[0].scene(),frame_[1].scene(),interpolation_support_?resolved_[0].view.Get():flow_[0][0].view.Get()};
    auto output=cut_.output.Get();context_->CSSetShaderResources(0,3,inputs.data());context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);
    context_->Dispatch(1,1,1);unbind();context_->CSSetShader(nullptr,nullptr,0);
}
void FrameInterpolation::bind(float phase) {
    if(!interpolation_support_) throw std::runtime_error("Intermediate images require interpolation support");
    unbind(false);constants(phase);
    std::array<ID3D11ShaderResourceView*,19> inputs{frame_[0].scene(),frame_[1].scene(),resolved_[0].view.Get(),resolved_[1].view.Get(),cut_.view.Get(),
        frame_[0].layers.views[1].Get(),frame_[0].layers.views[2].Get(),frame_[1].layers.views[1].Get(),frame_[1].layers.views[2].Get(),nullptr,nullptr,nullptr,nullptr,nullptr,nullptr,
        frame_[0].layers.views[3].Get(),frame_[0].layers.views[4].Get(),frame_[1].layers.views[3].Get(),frame_[1].layers.views[4].Get()};
    context_->PSSetShaderResources(0,unsigned(inputs.size()),inputs.data());context_->PSSetShader(interpolate_.Get(),nullptr,0);
}
}
