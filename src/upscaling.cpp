#include "upscaling.h"
#include "interpolation.h"
#include "shaders.h"
#include <d3d11_4.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <nvsdk_ngx_helpers_d3d.h>
#include <xess/xess_d3d12.h>
#include <ffx_api_loader.h>
#include <dx12/ffx_api_dx12.h>
#include <ffx_upscale.h>
#include <array>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {
using Microsoft::WRL::ComPtr;
void checked(HRESULT result,const char* operation) {
    if(FAILED(result)) throw std::runtime_error(std::format("{} failed (0x{:08X})",operation,unsigned(result)));
}
void checked(NVSDK_NGX_Result result,const char* operation) {
    if(NVSDK_NGX_FAILED(result)) throw std::runtime_error(std::format("{} failed (0x{:08X})",operation,unsigned(result)));
}
void checked(xess_result_t result,const char* operation) {
    if(result<0) throw std::runtime_error(std::format("{} failed ({})",operation,int(result)));
}
void checked(ffxReturnCode_t result,const char* operation) {
    if(result!=FFX_API_RETURN_OK) throw std::runtime_error(std::format("{} failed ({})",operation,result));
}
std::filesystem::path executable_folder() {
    std::array<wchar_t,32768> path{};
    const auto size=GetModuleFileNameW(nullptr,path.data(),DWORD(path.size()));
    if(!size || size==path.size()) throw std::runtime_error("Cannot locate the upscaling libraries");
    return std::filesystem::path(std::wstring_view(path.data(),size)).parent_path();
}
struct SharedTexture {
    ComPtr<ID3D12Resource> resource;
    ComPtr<ID3D11Texture2D> texture;
};
}
struct FrameUpscaling::Impl {
    UpscaleMethod method=UpscaleMethod::off;
    unsigned width=0,height=0,output_width=0,output_height=0;
    ComPtr<ID3D11Device5> device11;
    ComPtr<ID3D11DeviceContext4> context11;
    ComPtr<ID3D12Device> device12;
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12GraphicsCommandList> commands;
    struct Slot {ComPtr<ID3D12CommandAllocator> allocator;std::uint64_t completed=0;};
    std::array<Slot,3> slots;
    unsigned slot=0;
    ComPtr<ID3D12Fence> fence12;
    ComPtr<ID3D11Fence> fence11;
    HANDLE event=nullptr;
    std::uint64_t serial=0,submitted=0;
    std::array<SharedTexture,5> images;
    ComPtr<ID3D11ShaderResourceView> output_view;
    NVSDK_NGX_Parameter* ngx_parameters=nullptr;
    NVSDK_NGX_Handle* ngx_feature=nullptr;
    bool ngx_initialized=false;
    xess_context_handle_t xess=nullptr;
    HMODULE fsr_module=nullptr;
    ffxFunctions fsr{};
    ffxContext fsr_context=nullptr;
    std::unique_ptr<FrameInterpolation> motion;
    ComPtr<ID3D11VertexShader> vertex;
    ComPtr<ID3D11PixelShader> convert,present;
    ComPtr<ID3D11Buffer> input_constants;
    ComPtr<ID3D11SamplerState> sampler;
    ComPtr<ID3D11RasterizerState> raster;
    ComPtr<ID3D11DepthStencilState> depth_state;
    std::array<ComPtr<ID3D11RenderTargetView>,3> input_targets;
    ComPtr<ID3D11Texture2D> frame_image;
    ComPtr<ID3D11ShaderResourceView> frame_view;
    ComPtr<ID3D11RenderTargetView> frame_target;
    float previous_jitter_x=0,previous_jitter_y=0;
    ~Impl() {
        // Complete both queues before releasing vendor-owned history or shared
        // resources. Normal frame submission uses GPU fences, not CPU waits.
        if(context11 && queue && fence12) {
            const auto value=++serial;
            if(SUCCEEDED(context11->Signal(fence11.Get(),value))) {
                context11->Flush();
                if(SUCCEEDED(queue->Wait(fence12.Get(),value))) {
                    const auto completed=++serial;
                    if(SUCCEEDED(queue->Signal(fence12.Get(),completed))) {
                        if(fence12->GetCompletedValue()<completed && event &&
                           SUCCEEDED(fence12->SetEventOnCompletion(completed,event))) WaitForSingleObject(event,30000);
                    }
                }
            }
        }
        if(ngx_feature) NVSDK_NGX_D3D12_ReleaseFeature(ngx_feature);
        if(ngx_parameters) NVSDK_NGX_D3D12_DestroyParameters(ngx_parameters);
        if(ngx_initialized) NVSDK_NGX_D3D12_Shutdown1(device12.Get());
        if(xess) xessDestroyContext(xess);
        if(fsr_context) fsr.DestroyContext(&fsr_context,nullptr);
        if(fsr_module) FreeLibrary(fsr_module);
        if(event) CloseHandle(event);
    }
    void await(std::uint64_t value) {
        if(fence12->GetCompletedValue()>=value) return;
        checked(fence12->SetEventOnCompletion(value,event),"Upscaling fence event");
        if(WaitForSingleObject(event,30000)!=WAIT_OBJECT_0) throw std::runtime_error("Upscaling GPU did not complete its work");
    }
    SharedTexture share(unsigned w,unsigned h,DXGI_FORMAT format,bool output=false) {
        SharedTexture result;
        D3D12_HEAP_PROPERTIES heap{};heap.Type=D3D12_HEAP_TYPE_DEFAULT;
        heap.CreationNodeMask=heap.VisibleNodeMask=1;
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension=D3D12_RESOURCE_DIMENSION_TEXTURE2D;desc.Width=w;desc.Height=h;
        desc.DepthOrArraySize=desc.MipLevels=1;desc.Format=format;desc.SampleDesc.Count=1;
        desc.Flags=D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS|D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
        if(output) desc.Flags|=D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        checked(device12->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_SHARED,&desc,D3D12_RESOURCE_STATE_COMMON,
                                                 nullptr,IID_PPV_ARGS(&result.resource)),"Shared upscaling image");
        HANDLE handle=nullptr;
        checked(device12->CreateSharedHandle(result.resource.Get(),nullptr,GENERIC_ALL,nullptr,&handle),"Upscaling image handle");
        const auto opened=device11->OpenSharedResource1(handle,IID_PPV_ARGS(&result.texture));CloseHandle(handle);
        checked(opened,"D3D11 upscaling image");return result;
    }
    void initialize(ID3D11Device* device,ID3D11DeviceContext* context,UpscaleMethod technique,
                    unsigned w,unsigned h,unsigned ow,unsigned oh) {
        if(technique==UpscaleMethod::off || !w || !h || ow<w || oh<h || ow>8192 || oh>8192)
            throw std::runtime_error("Invalid upscaling dimensions or technique");
        method=technique;width=w;height=h;output_width=ow;output_height=oh;
        checked(device->QueryInterface(IID_PPV_ARGS(&device11)),"D3D11 shared-fence device");
        checked(context->QueryInterface(IID_PPV_ARGS(&context11)),"D3D11 shared-fence context");
        ComPtr<IDXGIDevice> dxgi;checked(device->QueryInterface(IID_PPV_ARGS(&dxgi)),"Upscaling graphics device");
        ComPtr<IDXGIAdapter> adapter;checked(dxgi->GetAdapter(&adapter),"Upscaling adapter");
        checked(D3D12CreateDevice(adapter.Get(),D3D_FEATURE_LEVEL_11_0,IID_PPV_ARGS(&device12)),"D3D12 upscaling device");
        D3D12_COMMAND_QUEUE_DESC desc{};desc.Type=D3D12_COMMAND_LIST_TYPE_DIRECT;
        checked(device12->CreateCommandQueue(&desc,IID_PPV_ARGS(&queue)),"Upscaling command queue");
        for(auto& entry:slots) checked(device12->CreateCommandAllocator(desc.Type,IID_PPV_ARGS(&entry.allocator)),"Upscaling command allocator");
        checked(device12->CreateCommandList(0,desc.Type,slots[0].allocator.Get(),nullptr,IID_PPV_ARGS(&commands)),"Upscaling command list");
        checked(device12->CreateFence(0,D3D12_FENCE_FLAG_SHARED,IID_PPV_ARGS(&fence12)),"Upscaling shared fence");
        HANDLE handle=nullptr;
        checked(device12->CreateSharedHandle(fence12.Get(),nullptr,GENERIC_ALL,nullptr,&handle),"Upscaling fence handle");
        const auto opened=device11->OpenSharedFence(handle,IID_PPV_ARGS(&fence11));CloseHandle(handle);
        checked(opened,"D3D11 upscaling fence");event=CreateEventW(nullptr,FALSE,FALSE,nullptr);
        if(!event) throw std::runtime_error("Cannot create the upscaling fence event");
        images[0]=share(w,h,DXGI_FORMAT_R8G8B8A8_UNORM);
        images[1]=share(w,h,DXGI_FORMAT_R32_FLOAT);
        images[2]=share(w,h,DXGI_FORMAT_R16G16_FLOAT);
        images[3]=share(w,h,DXGI_FORMAT_R8_UNORM);
        images[4]=share(ow,oh,DXGI_FORMAT_R16G16B16A16_FLOAT,true);
        checked(device11->CreateShaderResourceView(images[4].texture.Get(),nullptr,&output_view),"Upscaling output view");
        const auto folder=executable_folder();
        if(method==UpscaleMethod::dlss) {
            const auto path=folder.wstring();const wchar_t* search=path.c_str();
            const auto cache=folder/L"data/local/upscaling";std::filesystem::create_directories(cache);
            NVSDK_NGX_FeatureCommonInfo info{};info.PathListInfo={&search,1};
            checked(NVSDK_NGX_D3D12_Init_with_ProjectID("95483253-e66a-4b4d-a0ed-d994b84f76be",NVSDK_NGX_ENGINE_TYPE_CUSTOM,
                                                      "1.4",cache.c_str(),device12.Get(),&info),"DLSS initialization");
            ngx_initialized=true;
            checked(NVSDK_NGX_D3D12_GetCapabilityParameters(&ngx_parameters),"DLSS capabilities");
            int available=0;ngx_parameters->Get(NVSDK_NGX_Parameter_SuperSampling_Available,&available);
            if(!available) throw std::runtime_error("DLSS is unavailable on this graphics device or driver");
            NVSDK_NGX_DLSS_Create_Params params{};
            const auto quality=ow==w && oh==h?NVSDK_NGX_PerfQuality_Value_DLAA:
                double(ow)/w<1.75?NVSDK_NGX_PerfQuality_Value_MaxQuality:NVSDK_NGX_PerfQuality_Value_MaxPerf;
            params.Feature={w,h,ow,oh,quality};
            params.InFeatureCreateFlags=NVSDK_NGX_DLSS_Feature_Flags_MVLowRes;
            checked(NGX_D3D12_CREATE_DLSS_EXT(commands.Get(),1,1,&ngx_feature,ngx_parameters,&params),"DLSS feature creation");
        } else if(method==UpscaleMethod::xess) {
            checked(xessD3D12CreateContext(device12.Get(),&xess),"XeSS context creation");
            xess_d3d12_init_params_t params{};params.outputResolution={ow,oh};
            params.qualitySetting=ow==w && oh==h?XESS_QUALITY_SETTING_AA:
                double(ow)/w<1.75?XESS_QUALITY_SETTING_QUALITY:XESS_QUALITY_SETTING_PERFORMANCE;
            params.initFlags=XESS_INIT_FLAG_LDR_INPUT_COLOR|XESS_INIT_FLAG_RESPONSIVE_PIXEL_MASK;
            checked(xessD3D12Init(xess,&params),"XeSS initialization");
            checked(xessSetVelocityScale(xess,1,1),"XeSS motion scale");
        } else if(method==UpscaleMethod::fsr) {
            fsr_module=LoadLibraryExW((folder/L"amd_fidelityfx_loader_dx12.dll").c_str(),nullptr,
                                     LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
            if(!fsr_module) throw std::runtime_error("Cannot load the FSR library");
            ffxLoadFunctions(&fsr,fsr_module);
            if(!fsr.CreateContext || !fsr.DestroyContext || !fsr.Dispatch) throw std::runtime_error("Incomplete FSR library");
            ffxCreateBackendDX12Desc backend{};backend.header.type=FFX_API_CREATE_CONTEXT_DESC_TYPE_BACKEND_DX12;
            backend.device=device12.Get();
            ffxCreateContextDescUpscale params{};params.header={FFX_API_CREATE_CONTEXT_DESC_TYPE_UPSCALE,&backend.header};
            params.maxRenderSize={w,h};params.maxUpscaleSize={ow,oh};params.flags=FFX_UPSCALE_ENABLE_NON_LINEAR_COLORSPACE;
            checked(fsr.CreateContext(&fsr_context,&params.header,nullptr),"FSR context creation");
        } else throw std::runtime_error("Unknown upscaling technique");
        checked(commands->Close(),"Upscaling initialization close");
        ID3D12CommandList* lists[]={commands.Get()};queue->ExecuteCommandLists(1,lists);
        submitted=++serial;checked(queue->Signal(fence12.Get(),submitted),"Upscaling initialization signal");
        await(submitted);slots[0].completed=submitted;
    }
    void publish(std::array<ID3D11Texture2D*,4> inputs,const UpscaleFrame& frame) {
        if(!std::isfinite(frame.jitter_x) || !std::isfinite(frame.jitter_y) || std::abs(frame.jitter_x)>0.5f ||
           std::abs(frame.jitter_y)>0.5f || !(frame.elapsed_ms>0) || !std::isfinite(frame.elapsed_ms) || !(frame.camera_near>0) ||
           !(frame.camera_far>frame.camera_near) || !std::isfinite(frame.camera_far) || !(frame.vertical_fov>0 && frame.vertical_fov<3.141593f))
            throw std::runtime_error("Upscaling requires valid frame timing, jitter and camera data");
        for(unsigned i=0;i<inputs.size();++i) {
            if(!inputs[i]) throw std::runtime_error("Missing upscaling frame input");
            D3D11_TEXTURE2D_DESC source{},destination{};inputs[i]->GetDesc(&source);images[i].texture->GetDesc(&destination);
            if(source.Width!=width || source.Height!=height || source.Format!=destination.Format || source.SampleDesc.Count!=1)
                throw std::runtime_error("Upscaling input has incompatible dimensions, format or multisampling");
            if(images[i].texture.Get()!=inputs[i]) context11->CopyResource(images[i].texture.Get(),inputs[i]);
        }
        const auto ready=++serial;
        checked(context11->Signal(fence11.Get(),ready),"Upscaling input signal");context11->Flush();
        checked(queue->Wait(fence12.Get(),ready),"Upscaling input wait");
        auto& entry=slots[slot];await(entry.completed);
        checked(entry.allocator->Reset(),"Upscaling allocator reset");
        checked(commands->Reset(entry.allocator.Get(),nullptr),"Upscaling command reset");
        std::array<D3D12_RESOURCE_BARRIER,5> barriers{};
        for(unsigned i=0;i<barriers.size();++i) {
            auto& barrier=barriers[i];barrier.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            barrier.Transition.pResource=images[i].resource.Get();barrier.Transition.Subresource=D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
            barrier.Transition.StateBefore=D3D12_RESOURCE_STATE_COMMON;
            barrier.Transition.StateAfter=i==4?D3D12_RESOURCE_STATE_UNORDERED_ACCESS:D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
        }
        commands->ResourceBarrier(UINT(barriers.size()),barriers.data());
        if(method==UpscaleMethod::dlss) {
            NVSDK_NGX_D3D12_DLSS_Eval_Params params{};
            params.Feature.pInColor=images[0].resource.Get();params.Feature.pInOutput=images[4].resource.Get();
            params.pInDepth=images[1].resource.Get();params.pInMotionVectors=images[2].resource.Get();
            params.pInBiasCurrentColorMask=images[3].resource.Get();
            params.InJitterOffsetX=frame.jitter_x;params.InJitterOffsetY=frame.jitter_y;
            params.InRenderSubrectDimensions={width,height};params.InReset=frame.reset;
            params.InMVScaleX=params.InMVScaleY=params.InPreExposure=params.InExposureScale=1;
            params.InFrameTimeDeltaInMsec=frame.elapsed_ms;
            checked(NGX_D3D12_EVALUATE_DLSS_EXT(commands.Get(),ngx_feature,ngx_parameters,&params),"DLSS frame evaluation");
        } else if(method==UpscaleMethod::xess) {
            xess_d3d12_execute_params_t params{};
            params.pColorTexture=images[0].resource.Get();params.pDepthTexture=images[1].resource.Get();
            params.pVelocityTexture=images[2].resource.Get();params.pResponsivePixelMaskTexture=images[3].resource.Get();
            params.pOutputTexture=images[4].resource.Get();params.jitterOffsetX=frame.jitter_x;params.jitterOffsetY=frame.jitter_y;
            params.exposureScale=1;params.resetHistory=frame.reset;params.inputWidth=width;params.inputHeight=height;
            checked(xessD3D12Execute(xess,commands.Get(),&params),"XeSS frame evaluation");
        } else {
            ffxDispatchDescUpscale params{};params.header.type=FFX_API_DISPATCH_DESC_TYPE_UPSCALE;
            params.commandList=commands.Get();params.color=ffxApiGetResourceDX12(images[0].resource.Get());
            params.depth=ffxApiGetResourceDX12(images[1].resource.Get());params.motionVectors=ffxApiGetResourceDX12(images[2].resource.Get());
            params.reactive=ffxApiGetResourceDX12(images[3].resource.Get());
            params.output=ffxApiGetResourceDX12(images[4].resource.Get(),FFX_API_RESOURCE_STATE_UNORDERED_ACCESS);
            params.jitterOffset={frame.jitter_x,frame.jitter_y};params.motionVectorScale={1,1};
            params.renderSize={width,height};params.upscaleSize={output_width,output_height};
            params.frameTimeDelta=frame.elapsed_ms;params.preExposure=1;params.reset=frame.reset;
            params.cameraNear=frame.camera_near;params.cameraFar=frame.camera_far;
            params.cameraFovAngleVertical=frame.vertical_fov;params.viewSpaceToMetersFactor=1;
            checked(fsr.Dispatch(&fsr_context,&params.header),"FSR frame evaluation");
        }
        for(auto& barrier:barriers) std::swap(barrier.Transition.StateBefore,barrier.Transition.StateAfter);
        commands->ResourceBarrier(UINT(barriers.size()),barriers.data());
        checked(commands->Close(),"Upscaling frame close");
        ID3D12CommandList* lists[]={commands.Get()};queue->ExecuteCommandLists(1,lists);
        submitted=++serial;checked(queue->Signal(fence12.Get(),submitted),"Upscaling output signal");
        checked(context11->Wait(fence11.Get(),submitted),"Upscaling output wait");
        entry.completed=submitted;slot=(slot+1)%unsigned(slots.size());
    }
    void prepare_frames(std::uint64_t& compilations) {
        if(motion) return;
        motion=std::make_unique<FrameInterpolation>(device11.Get(),context11.Get(),width,height,compilations,false);
        const auto shader=[&](ShaderKind kind) {const auto result=shader_binary(kind);compilations+=result.compiled;return result.bytes;};
        const auto vs=shader(ShaderKind::display_vertex),ps=shader(ShaderKind::upscale_inputs),display=shader(ShaderKind::display_pixel);
        checked(device11->CreateVertexShader(vs.data(),vs.size(),nullptr,&vertex),"Upscaling input vertex shader");
        checked(device11->CreatePixelShader(ps.data(),ps.size(),nullptr,&convert),"Upscaling input shader");
        checked(device11->CreatePixelShader(display.data(),display.size(),nullptr,&present),"Upscaling colour conversion shader");
        D3D11_BUFFER_DESC buffer{32,D3D11_USAGE_DYNAMIC,D3D11_BIND_CONSTANT_BUFFER,D3D11_CPU_ACCESS_WRITE,0,0};
        checked(device11->CreateBuffer(&buffer,nullptr,&input_constants),"Upscaling input constants");
        D3D11_SAMPLER_DESC sample{};sample.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;
        sample.AddressU=sample.AddressV=sample.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;sample.MaxLOD=D3D11_FLOAT32_MAX;
        sample.ComparisonFunc=D3D11_COMPARISON_NEVER;
        checked(device11->CreateSamplerState(&sample,&sampler),"Upscaling input sampler");
        D3D11_RASTERIZER_DESC rs{};rs.FillMode=D3D11_FILL_SOLID;rs.CullMode=D3D11_CULL_NONE;rs.DepthClipEnable=TRUE;
        checked(device11->CreateRasterizerState(&rs,&raster),"Upscaling input rasterizer");
        D3D11_DEPTH_STENCIL_DESC ds{};ds.DepthFunc=D3D11_COMPARISON_ALWAYS;
        ds.FrontFace=ds.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
        checked(device11->CreateDepthStencilState(&ds,&depth_state),"Upscaling input depth state");
        for(unsigned i=0;i<input_targets.size();++i)
            checked(device11->CreateRenderTargetView(images[i+1].texture.Get(),nullptr,&input_targets[i]),"Upscaling input target");
        D3D11_TEXTURE2D_DESC image{};image.Width=output_width;image.Height=output_height;image.MipLevels=image.ArraySize=1;
        image.Format=DXGI_FORMAT_R8G8B8A8_UNORM;image.SampleDesc.Count=1;image.BindFlags=D3D11_BIND_RENDER_TARGET|D3D11_BIND_SHADER_RESOURCE;
        checked(device11->CreateTexture2D(&image,nullptr,&frame_image),"Upscaling presentation image");
        checked(device11->CreateShaderResourceView(frame_image.Get(),nullptr,&frame_view),"Upscaling presentation view");
        checked(device11->CreateRenderTargetView(frame_image.Get(),nullptr,&frame_target),"Upscaling presentation target");
    }
    void publish_frame(ID3D11Texture2D* colour,ID3D11ShaderResourceView* depth,ID3D11ShaderResourceView* responsive,const UpscaleFrame& frame) {
        if(!motion || !depth || !responsive) throw std::runtime_error("Prepare all upscaling frame inputs before playing");
        context11->OMSetRenderTargets(0,nullptr,nullptr);
        motion->publish(colour,frame.reset);
        struct Constants {float jitter[4],info[4];};
        const Constants values{{frame.jitter_x-previous_jitter_x,frame.jitter_y-previous_jitter_y,0,0},
                               {float(width),float(height),float(motion->ready()),0}};
        D3D11_MAPPED_SUBRESOURCE mapped{};
        checked(context11->Map(input_constants.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Upscaling input map");
        std::memcpy(mapped.pData,&values,sizeof(values));context11->Unmap(input_constants.Get(),0);
        auto constants=input_constants.Get();auto filter=sampler.Get();
        std::array<ID3D11RenderTargetView*,3> targets{};for(unsigned i=0;i<targets.size();++i) targets[i]=input_targets[i].Get();
        std::array<ID3D11ShaderResourceView*,4> views{depth,motion->backward_field(),responsive,motion->scene_cut()};
        context11->OMSetRenderTargets(UINT(targets.size()),targets.data(),nullptr);
        D3D11_VIEWPORT viewport{0,0,float(width),float(height),0,1};context11->RSSetViewports(1,&viewport);
        context11->RSSetState(raster.Get());context11->OMSetDepthStencilState(depth_state.Get(),0);context11->OMSetBlendState(nullptr,nullptr,UINT_MAX);
        context11->IASetInputLayout(nullptr);context11->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        context11->VSSetShader(vertex.Get(),nullptr,0);context11->PSSetShader(convert.Get(),nullptr,0);
        context11->PSSetConstantBuffers(0,1,&constants);context11->PSSetSamplers(0,1,&filter);
        context11->PSSetShaderResources(0,UINT(views.size()),views.data());context11->Draw(3,0);
        views.fill(nullptr);context11->PSSetShaderResources(0,UINT(views.size()),views.data());context11->OMSetRenderTargets(0,nullptr,nullptr);
        publish({colour,images[1].texture.Get(),images[2].texture.Get(),images[3].texture.Get()},frame);
        auto target=frame_target.Get();auto source=output_view.Get();
        context11->OMSetRenderTargets(1,&target,nullptr);
        viewport.Width=float(output_width);viewport.Height=float(output_height);context11->RSSetViewports(1,&viewport);
        context11->PSSetShader(present.Get(),nullptr,0);context11->PSSetShaderResources(0,1,&source);
        context11->Draw(3,0);source=nullptr;context11->PSSetShaderResources(0,1,&source);context11->OMSetRenderTargets(0,nullptr,nullptr);
        previous_jitter_x=frame.jitter_x;previous_jitter_y=frame.jitter_y;
    }
};
FrameUpscaling::FrameUpscaling(ID3D11Device* device,ID3D11DeviceContext* context,UpscaleMethod method,
                             unsigned width,unsigned height,unsigned output_width,unsigned output_height):impl_(std::make_unique<Impl>()) {
    impl_->initialize(device,context,method,width,height,output_width,output_height);
}
FrameUpscaling::~FrameUpscaling()=default;
void FrameUpscaling::publish(ID3D11Texture2D* colour,ID3D11Texture2D* depth,ID3D11Texture2D* motion,
                            ID3D11Texture2D* responsive,const UpscaleFrame& frame) {
    impl_->publish({colour,depth,motion,responsive},frame);
}
void FrameUpscaling::prepare_frames(std::uint64_t& compilations) {impl_->prepare_frames(compilations);}
void FrameUpscaling::publish_frame(ID3D11Texture2D* colour,ID3D11ShaderResourceView* depth,ID3D11ShaderResourceView* responsive,const UpscaleFrame& frame) {
    impl_->publish_frame(colour,depth,responsive,frame);
}
ID3D11Texture2D* FrameUpscaling::image() const {return impl_->frame_image?impl_->frame_image.Get():impl_->images[4].texture.Get();}
ID3D11ShaderResourceView* FrameUpscaling::view() const {return impl_->frame_view?impl_->frame_view.Get():impl_->output_view.Get();}
}
