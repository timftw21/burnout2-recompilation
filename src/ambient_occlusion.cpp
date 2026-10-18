#include "ambient_occlusion.h"
#include "shaders.h"
#include <cstring>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {
void checked_ao(HRESULT value,const char* operation) {
    if(FAILED(value)) throw std::runtime_error(std::format("{}: 0x{:08X}",operation,unsigned(value)));
}
struct AoConstants {float near_plane,far_plane,tan_x,tan_y,radius,strength;unsigned width,height,layered,padding[3]{};};
}
FrameAmbientOcclusion::FrameAmbientOcclusion(ID3D11Device* device,ID3D11DeviceContext* context,unsigned width,unsigned height,unsigned quality,unsigned samples,std::uint64_t& compilations)
    :device_(device),context_(context),width_(width),height_(height) {
    if(!width || !height || quality<1 || quality>3) throw std::runtime_error("Invalid ambient occlusion configuration");
    const auto shader=[&](ShaderKind kind){auto binary=shader_binary(kind);compilations+=binary.compiled;return binary.bytes;};
    auto code=shader(samples>1?ShaderKind::flow_depth_ms:ShaderKind::flow_depth);
    checked_ao(device->CreateComputeShader(code.data(),code.size(),nullptr,&depth_shader_),"AO depth shader");
    code=shader(ShaderKind(unsigned(ShaderKind::ao_low)+quality-1));
    checked_ao(device->CreateComputeShader(code.data(),code.size(),nullptr,&main_shader_),"GTAO shader");
    code=shader(ShaderKind::display_vertex);checked_ao(device->CreateVertexShader(code.data(),code.size(),nullptr,&vertex_),"AO composite vertex shader");
    code=shader(ShaderKind::ao_composite);checked_ao(device->CreatePixelShader(code.data(),code.size(),nullptr,&composite_),"AO composite shader");
    D3D11_TEXTURE2D_DESC image{};image.Width=width;image.Height=height;image.MipLevels=image.ArraySize=image.SampleDesc.Count=1;
    image.Format=DXGI_FORMAT_R32_FLOAT;image.BindFlags=D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_UNORDERED_ACCESS;
    checked_ao(device->CreateTexture2D(&image,nullptr,&depth_),"AO depth snapshot");
    checked_ao(device->CreateShaderResourceView(depth_.Get(),nullptr,&depth_view_),"AO depth view");
    checked_ao(device->CreateUnorderedAccessView(depth_.Get(),nullptr,&depth_output_),"AO depth output");
    image.Format=DXGI_FORMAT_R32G32_FLOAT;
    checked_ao(device->CreateTexture2D(&image,nullptr,&visibility_),"AO visibility/depth");
    checked_ao(device->CreateShaderResourceView(visibility_.Get(),nullptr,&visibility_view_),"AO visibility view");
    checked_ao(device->CreateUnorderedAccessView(visibility_.Get(),nullptr,&visibility_output_),"AO visibility output");
    image.Format=DXGI_FORMAT_R8G8B8A8_UNORM;image.BindFlags=D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_RENDER_TARGET;
    for(unsigned i=0;i<2;++i) {
        checked_ao(device->CreateTexture2D(&image,nullptr,&image_[i]),"AO presentation image");
        checked_ao(device->CreateShaderResourceView(image_[i].Get(),nullptr,&view_[i]),"AO presentation view");
        checked_ao(device->CreateRenderTargetView(image_[i].Get(),nullptr,&target_[i]),"AO presentation target");
    }
    D3D11_BUFFER_DESC buffer{sizeof(AoConstants),D3D11_USAGE_DYNAMIC,D3D11_BIND_CONSTANT_BUFFER,D3D11_CPU_ACCESS_WRITE,0,0};
    checked_ao(device->CreateBuffer(&buffer,nullptr,&constants_),"AO constants");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=TRUE;
    checked_ao(device->CreateRasterizerState(&raster,&raster_),"AO rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    checked_ao(device->CreateDepthStencilState(&depth,&depth_state_),"AO depth state");
}
void FrameAmbientOcclusion::unbind() {
    std::array<ID3D11ShaderResourceView*,6> empty{};ID3D11UnorderedAccessView* output=nullptr;
    context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);context_->CSSetShaderResources(0,1,empty.data());
    context_->PSSetShaderResources(0,unsigned(empty.size()),empty.data());context_->OMSetRenderTargets(0,nullptr,nullptr);
}
void FrameAmbientOcclusion::capture_depth(ID3D11ShaderResourceView* source) {
    unbind();auto output=depth_output_.Get();context_->CSSetShader(depth_shader_.Get(),nullptr,0);
    context_->CSSetShaderResources(0,1,&source);context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);
    context_->Dispatch((width_+7)/8,(height_+7)/8,1);unbind();context_->CSSetShader(nullptr,nullptr,0);
}
void FrameAmbientOcclusion::publish(ID3D11ShaderResourceView* scene,const FrameLayers::Images* layers,float near_plane,float far_plane,float tan_x,float tan_y,float strength) {
    unbind();const AoConstants value{near_plane,far_plane,tan_x,tan_y,1.5f,strength,width_,height_,unsigned(layers!=nullptr)};
    D3D11_MAPPED_SUBRESOURCE mapped{};checked_ao(context_->Map(constants_.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"AO constants map");
    std::memcpy(mapped.pData,&value,sizeof(value));context_->Unmap(constants_.Get(),0);
    auto constants=constants_.Get();context_->CSSetConstantBuffers(0,1,&constants);context_->PSSetConstantBuffers(0,1,&constants);
    auto depth=depth_view_.Get();auto output=visibility_output_.Get();context_->CSSetShader(main_shader_.Get(),nullptr,0);
    context_->CSSetShaderResources(0,1,&depth);context_->CSSetUnorderedAccessViews(0,1,&output,nullptr);
    context_->Dispatch((width_+7)/8,(height_+7)/8,1);unbind();context_->CSSetShader(nullptr,nullptr,0);
    std::array<ID3D11RenderTargetView*,2> targets{target_[0].Get(),target_[1].Get()};context_->OMSetRenderTargets(2,targets.data(),nullptr);
    const D3D11_VIEWPORT viewport{0,0,float(width_),float(height_),0,1};context_->RSSetViewports(1,&viewport);context_->RSSetState(raster_.Get());
    context_->OMSetBlendState(nullptr,nullptr,UINT_MAX);context_->OMSetDepthStencilState(depth_state_.Get(),0);
    context_->IASetInputLayout(nullptr);context_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context_->VSSetShader(vertex_.Get(),nullptr,0);context_->GSSetShader(nullptr,nullptr,0);context_->PSSetShader(composite_.Get(),nullptr,0);
    const std::array<ID3D11ShaderResourceView*,6> inputs{scene,visibility_view_.Get(),layers?layers->views[1].Get():nullptr,layers?layers->views[2].Get():nullptr,layers?layers->views[3].Get():nullptr,layers?layers->views[4].Get():nullptr};
    context_->PSSetShaderResources(0,unsigned(inputs.size()),inputs.data());context_->Draw(3,0);unbind();ready_=true;
}
Image FrameAmbientOcclusion::readback() {
    if(!ready_) throw std::runtime_error("Publish ambient occlusion before reading its image");
    D3D11_TEXTURE2D_DESC desc{};image_[1]->GetDesc(&desc);desc.BindFlags=0;desc.Usage=D3D11_USAGE_STAGING;desc.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    Ptr<ID3D11Texture2D> staging;checked_ao(device_->CreateTexture2D(&desc,nullptr,&staging),"AO readback storage");
    context_->CopyResource(staging.Get(),image_[1].Get());D3D11_MAPPED_SUBRESOURCE mapped{};
    checked_ao(context_->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped),"AO readback");
    Image result{width_,height_,std::vector<std::byte>(std::size_t(width_)*height_*4)};
    for(unsigned y=0;y<height_;++y)std::memcpy(result.rgba.data()+std::size_t(y)*width_*4,static_cast<const std::byte*>(mapped.pData)+std::size_t(y)*mapped.RowPitch,width_*4);
    context_->Unmap(staging.Get(),0);return result;
}
std::string ambient_occlusion_shader(unsigned quality,bool composite) {
    const std::string constants=R"(
cbuffer C:register(b0){float nearPlane,farPlane,tanX,tanY,radius,strength;uint width,height,layered;float3 padding;}
)";
    if(composite) return constants+R"(
Texture2D<float4> scene:register(t0);Texture2D<float2> visibility:register(t1);
Texture2D<float4> black:register(t2),white:register(t3),lower:register(t4),upper:register(t5);
struct Output{float4 scene:SV_Target0;float4 image:SV_Target1;};
Output main(float4 position:SV_Position) {
 int2 p=int2(position.xy);float2 center=visibility.Load(int3(p,0));float sum=0,weightSum=0;
 [unroll] for(int y=-2;y<=2;++y) [unroll] for(int x=-2;x<=2;++x) {
  int2 q=clamp(p+int2(x,y),int2(0,0),int2(width-1,height-1));float2 tap=visibility.Load(int3(q,0));
  float weight=exp2(-0.5*(x*x+y*y)-abs(tap.y-center.y)/max(0.02,center.y*0.005));sum+=tap.x*weight;weightSum+=weight;
 }
 float ao=lerp(1,saturate(sum/max(weightSum,1e-6)),strength);
 Output o;float4 color=scene.Load(int3(p,0));o.scene=float4(color.rgb*ao,color.a);o.image=o.scene;
 if(layered) o.image=clamp(black.Load(int3(p,0))+o.scene*(white.Load(int3(p,0))-black.Load(int3(p,0))),lower.Load(int3(p,0)),upper.Load(int3(p,0)));
 return o;
})";
    // GTAO horizon integration and normal projection adapted from XeGTAO.
    // Copyright (C) 2016-2021 Intel Corporation, MIT; see THIRD_PARTY_NOTICES.txt.
    return std::format("#define SLICES {}\n#define STEPS {}\n",quality==1?1:quality==2?2:3,quality==1?4:quality==2?3:4)+constants+R"(
Texture2D<float> depth:register(t0);RWTexture2D<float2> visibility:register(u0);
float linearDepth(int2 p){float d=depth.Load(int3(clamp(p,int2(0,0),int2(width-1,height-1)),0));return nearPlane*farPlane/max(farPlane-d*(farPlane-nearPlane),1e-6);}
float3 viewPosition(int2 p,float z){return float3(((float2(p)+0.5)/float2(width,height)*float2(2,-2)+float2(-1,1))*float2(tanX,tanY)*z,z);}
[numthreads(8,8,1)] void main(uint3 id:SV_DispatchThreadID) {
 if(id.x>=width || id.y>=height)return;int2 p=int2(id.xy);float z=linearDepth(p);
 if(depth.Load(int3(p,0))>=0.999999){visibility[p]=float2(1,z);return;}
 float pixelRadius=radius*float(height)/(2*tanY*z);
 if(pixelRadius<1){visibility[p]=float2(1,z);return;}
 float3 center=viewPosition(p,z),view=normalize(-center);
 float zl=linearDepth(p-int2(1,0)),zr=linearDepth(p+int2(1,0)),zt=linearDepth(p-int2(0,1)),zb=linearDepth(p+int2(0,1));
 float3 dx=abs(zl-z)<abs(zr-z)?center-viewPosition(p-int2(1,0),zl):viewPosition(p+int2(1,0),zr)-center;
 float3 dy=abs(zt-z)<abs(zb-z)?viewPosition(p-int2(0,1),zt)-center:center-viewPosition(p+int2(0,1),zb);
 float3 normal=cross(dy,dx);normal=dot(normal,normal)>1e-12?normalize(normal):view;
 if(dot(normal,view)<0)normal=-normal;
 center*=0.9995;float result=0,unoccluded=0;
 // Fixed spatial noise avoids shimmer when temporal antialiasing is disabled.
 float noise=frac(52.9829189*frac(dot(float2(p),float2(0.06711056,0.00583715))));
 [unroll] for(int slice=0;slice<SLICES;++slice) {
  float phi=(slice+noise)*3.141592654/SLICES;float2 direction=float2(cos(phi),sin(phi));
  float3 sliceDirection=float3(direction,0),ortho=sliceDirection-dot(sliceDirection,view)*view;
  float3 axis=normalize(cross(ortho,view)),projected=normal-axis*dot(normal,axis);
  float projectedLength=length(projected),cosNormal=saturate(dot(projected,view)/max(projectedLength,1e-6));
  float n=sign(dot(ortho,projected))*acos(cosNormal);
  float2 low=cos(n+float2(1.570796327,-1.570796327)),horizon=low;
  float2 maximumOffset=direction*float2(float(width)/(2*tanX),-float(height)/(2*tanY))*radius/z;
  [unroll] for(int step=0;step<STEPS;++step) {
   float s=(step+0.5)/STEPS;float2 offset=round(maximumOffset*s*s);
   [unroll] for(int side=0;side<2;++side) {
    int2 q=p+int2(offset)*(side==0?1:-1);
    if(any(q<0)||q.x>=int(width)||q.y>=int(height)||all(q==p)||depth.Load(int3(q,0))>=0.999999)continue;
    float3 delta=viewPosition(q,linearDepth(q))-center;float distance=length(delta);
    float weight=saturate((radius-distance)/(radius*0.5));
    float h=dot(delta,view)/max(distance,1e-6);
    if(side==0)horizon.x=max(horizon.x,lerp(low.x,h,weight));else horizon.y=max(horizon.y,lerp(low.y,h,weight));
   }
  }
  float2 h=float2(-acos(clamp(horizon.y,-1,1)),acos(clamp(horizon.x,-1,1)));
  float2 integral=(cosNormal+2*h*sin(n)-cos(2*h-n))*0.25;
  result+=projectedLength*(integral.x+integral.y);
  // Normalize by the same unoccluded slices so finite angular sampling
  // cannot darken an unobstructed plane, including near screen edges.
  unoccluded+=projectedLength*(cosNormal+n*sin(n));
 }
 visibility[p]=float2(lerp(1,saturate(result/max(unoccluded,1e-6)),saturate(pixelRadius-1)),z);
})";
}
}
