#include "render.h"
#include "shaders.h"
#include <Windows.h>
#include <d3d11.h>
#include <d3d11sdklayers.h>
#include <dxgi1_2.h>
#include <wincodec.h>
#include <wrl/client.h>
#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>
#include <format>
#include <map>
#include <stdexcept>

using Microsoft::WRL::ComPtr;
namespace b2 {
namespace {
void checked(HRESULT result, std::string_view operation) {
    if (FAILED(result)) throw std::runtime_error(std::format("{} failed: 0x{:08X}", operation, static_cast<unsigned>(result)));
}
void extent(std::uint32_t width, std::uint32_t height) {
    if (!width || !height || width > 4096 || height > 4096)
        throw std::runtime_error("Image dimensions must be 1..4096");
}
Float4 color(std::uint32_t argb) {
    return {float((argb >> 16) & 255) / 255, float((argb >> 8) & 255) / 255,
            float(argb & 255) / 255, float(argb >> 24) / 255};
}
struct ComScope {
    HRESULT result = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    ComScope() { if (result != RPC_E_CHANGED_MODE) checked(result, "COM initialization"); }
    ~ComScope() { if (SUCCEEDED(result)) CoUninitialize(); }
};
ComPtr<IWICImagingFactory> image_factory() {
    ComPtr<IWICImagingFactory> factory;
    checked(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                            IID_PPV_ARGS(&factory)), "WIC factory");
    return factory;
}
void validate_fragment(const RenderState& state) {
    const auto count=state.combiner_control&255;
    if(count>8) throw std::runtime_error("Combiner stage count exceeds eight");
    if(state.alpha_test && (state.alpha_function<0x200 || state.alpha_function>0x207))
        throw std::runtime_error("Invalid alpha comparison function");
    const auto input=[](unsigned word,bool final) {
        const auto reg=word&15;
        if(!(reg<=5 || (reg>=8 && reg<=13) || (final && reg>=14)))
            throw std::runtime_error("Reserved combiner source register");
    };
    const auto output=[](unsigned word,unsigned allowed) {
        const auto flags=word>>12,mapping=flags&0x38;
        if(flags&~allowed || mapping==0x28 || mapping==0x38) throw std::runtime_error("Unsupported combiner output flags");
        for(unsigned shift:{0U,4U,8U}) {
            const auto reg=(word>>shift)&15;
            if(!(reg==0 || reg==4 || reg==5 || (reg>=8 && reg<=13)))
                throw std::runtime_error("Reserved combiner destination register");
        }
    };
    for(unsigned stage=0;stage<count;++stage) {
        for(unsigned shift:{0U,8U,16U,24U}) {input(state.color_inputs[stage]>>shift,false);input(state.alpha_inputs[stage]>>shift,false);}
        output(state.color_outputs[stage],0xFF);output(state.alpha_outputs[stage],0x3C);
    }
    if(state.final_inputs[0] || state.final_inputs[1]) {
        for(unsigned shift:{0U,8U,16U,24U}) input(state.final_inputs[0]>>shift,true);
        for(unsigned shift:{8U,16U,24U}) input(state.final_inputs[1]>>shift,true);
    }
    for(unsigned i=0;i<4;++i) {
        const auto mode=state.texture_modes[i];
        if(mode!=0 && mode!=1 && mode!=2 && mode!=3 && mode!=4 && mode!=5 && mode!=15 && mode!=16)
            throw std::runtime_error("Texture shader mode is not supported yet: "+hex32(mode));
        if(mode==5 && state.texture_clip[i]&~15U) throw std::runtime_error("Invalid texture clip-plane comparisons");
        if((mode==15 || mode==16) && state.texture_sources[i]>=i) throw std::runtime_error("Dependent texture must read an earlier stage");
    }
}
unsigned simple_combiner(const RenderState& s) {
    // These complete instruction patterns reduce exactly to vertex color or
    // color times texture. Other programs retain the general compiled shader.
    if(s.texture_modes[1] || s.texture_modes[2] || s.texture_modes[3]) return 4;
    const auto count=s.combiner_control&255;
    if(!count && !s.texture_modes[0] && s.final_inputs==std::array<std::uint32_t,2>{4,0x1400}) return 0;
    const bool direct=(!s.final_inputs[0] && !s.final_inputs[1]) ||
        (s.final_inputs[0]==12 && (s.final_inputs[1]&0xFFFFFF00U)==0x1C00);
    if(count!=1 || !direct || (s.color_outputs[0]!=0xC0 && s.color_outputs[0]!=0xC00) ||
       (s.alpha_outputs[0]!=0xC0 && s.alpha_outputs[0]!=0xC00)) return 4;
    if(!s.texture_modes[0] && s.color_inputs[0]==0x04200000 && s.alpha_inputs[0]==0x14200000) return 0;
    if((s.color_inputs[0]!=0x04080000 && s.color_inputs[0]!=0x08040000) ||
       (s.alpha_inputs[0]!=0x14180000 && s.alpha_inputs[0]!=0x18140000)) return 4;
    switch(s.texture_modes[0]) {case 1:return 1;case 3:return 2;case 2:return 3;default:return 4;}
}
D3D11_COMPARISON_FUNC comparison(std::uint32_t value) {
    if (value < 0x200 || value > 0x207) throw std::runtime_error("Invalid depth comparison function");
    return static_cast<D3D11_COMPARISON_FUNC>(value - 0x200 + 1);
}
D3D11_BLEND blend_factor(std::uint32_t value) {
    switch (value) {
    case 0: return D3D11_BLEND_ZERO; case 1: return D3D11_BLEND_ONE;
    case 0x300: return D3D11_BLEND_SRC_COLOR; case 0x301: return D3D11_BLEND_INV_SRC_COLOR;
    case 0x302: return D3D11_BLEND_SRC_ALPHA; case 0x303: return D3D11_BLEND_INV_SRC_ALPHA;
    case 0x304: return D3D11_BLEND_DEST_ALPHA; case 0x305: return D3D11_BLEND_INV_DEST_ALPHA;
    case 0x306: return D3D11_BLEND_DEST_COLOR; case 0x307: return D3D11_BLEND_INV_DEST_COLOR;
    case 0x308: return D3D11_BLEND_SRC_ALPHA_SAT;
    default: throw std::runtime_error("Unsupported blend factor: " + hex32(value));
    }
}
D3D11_BLEND alpha_blend_factor(D3D11_BLEND factor) {
    switch (factor) {
    case D3D11_BLEND_SRC_COLOR: return D3D11_BLEND_SRC_ALPHA;
    case D3D11_BLEND_INV_SRC_COLOR: return D3D11_BLEND_INV_SRC_ALPHA;
    case D3D11_BLEND_DEST_COLOR: return D3D11_BLEND_DEST_ALPHA;
    case D3D11_BLEND_INV_DEST_COLOR: return D3D11_BLEND_INV_DEST_ALPHA;
    // The source-alpha-saturate factor has alpha component one.
    case D3D11_BLEND_SRC_ALPHA_SAT: return D3D11_BLEND_ONE;
    default: return factor;
    }
}
D3D11_BLEND_OP blend_op(std::uint32_t value) {
    switch (value) {
    case 0x8006: return D3D11_BLEND_OP_ADD; case 0x8007: return D3D11_BLEND_OP_MIN;
    case 0x8008: return D3D11_BLEND_OP_MAX; case 0x800A: return D3D11_BLEND_OP_SUBTRACT;
    case 0x800B: return D3D11_BLEND_OP_REV_SUBTRACT;
    default: throw std::runtime_error("Unsupported blend equation");
    }
}
D3D11_TEXTURE_ADDRESS_MODE address_mode(unsigned value) {
    switch (value) {
    case 1: return D3D11_TEXTURE_ADDRESS_WRAP; case 2: return D3D11_TEXTURE_ADDRESS_MIRROR;
    case 3: return D3D11_TEXTURE_ADDRESS_CLAMP; case 4: return D3D11_TEXTURE_ADDRESS_BORDER;
    default: throw std::runtime_error("Unsupported texture address mode");
    }
}
D3D11_STENCIL_OP stencil_operation(std::uint32_t value) {
    switch(value) {
    case 0x1E00:return D3D11_STENCIL_OP_KEEP;
    case 0:return D3D11_STENCIL_OP_ZERO;
    case 0x1E01:return D3D11_STENCIL_OP_REPLACE;
    case 0x1E02:return D3D11_STENCIL_OP_INCR_SAT;
    case 0x1E03:return D3D11_STENCIL_OP_DECR_SAT;
    case 0x150A:return D3D11_STENCIL_OP_INVERT;
    case 0x8507:return D3D11_STENCIL_OP_INCR;
    case 0x8508:return D3D11_STENCIL_OP_DECR;
    default:throw std::runtime_error("Unsupported stencil operation");
    }
}
using PipelineKey = std::array<std::uint32_t, 32>;
PipelineKey pipeline_key(const RenderState& state) {
    PipelineKey key{};
    std::size_t i = 0;
    for (auto value : {std::uint32_t(state.blend), std::uint32_t(state.depth_test), std::uint32_t(state.depth_write),
         std::uint32_t(state.cull), std::uint32_t(state.front_ccw),
         state.depth_function, state.blend_source, state.blend_destination, state.blend_equation,
         state.cull_face, state.color_mask}) key[i++] = value;
    for (unsigned n = 0; n < 4; ++n) for (auto value : {state.texture_address[n], std::uint32_t(state.linear_filter[n])}) key[i++] = value;
    for(auto value : {std::uint32_t(state.stencil_test),state.stencil_function,state.stencil_read_mask,state.stencil_write_mask,
        state.stencil_fail,state.stencil_depth_fail,state.stencil_pass,std::uint32_t(state.polygon_offset),
        std::bit_cast<std::uint32_t>(state.polygon_offset_scale),std::bit_cast<std::uint32_t>(state.polygon_offset_bias),std::uint32_t(state.scissor_test)}) key[i++]=value;
    return key;
}
struct alignas(16) Constants {
    std::array<Float4, 8> factor0, factor1;
    Float4 final0, final1, fog, alpha;
    std::array<std::array<std::uint32_t,4>,8> combiners;
    std::array<std::array<std::uint32_t,4>,4> stages;
    std::array<std::uint32_t,4> controls;
};
struct alignas(16) ProgramUniforms {
    VertexConstants constants;
    Float4 viewport,fog;
    std::array<Float4,4> texture_scales;
    std::array<std::uint32_t,16> input_formats{};
    std::array<std::uint32_t,4> fixed_flags{};
    std::array<std::array<std::uint32_t,4>,4> fixed_texgen{};
    std::array<std::uint32_t,4> fixed_matrices{};
};
struct AttributeFormat {DXGI_FORMAT native;unsigned bytes,fetched;};
AttributeFormat attribute_format(std::uint32_t format) {
    const unsigned type=format&15,size=(format>>4)&15;
    if(format==0x16) return {DXGI_FORMAT_R16G16_UNORM,4,4};
    if(format==0x72) return {DXGI_FORMAT_R32G32B32_FLOAT,12,12};
    if(format==0x40) return {DXGI_FORMAT_R8G8B8A8_UNORM,4,4};
    if(size<1 || size>4 || format&~255U) throw std::runtime_error("Invalid NV2A vertex format");
    constexpr DXGI_FORMAT floats[]={DXGI_FORMAT_R32_FLOAT,DXGI_FORMAT_R32G32_FLOAT,DXGI_FORMAT_R32G32B32_FLOAT,DXGI_FORMAT_R32G32B32A32_FLOAT};
    constexpr DXGI_FORMAT shorts[]={DXGI_FORMAT_R16_SNORM,DXGI_FORMAT_R16G16_SNORM,DXGI_FORMAT_R16G16B16A16_SNORM,DXGI_FORMAT_R16G16B16A16_SNORM};
    constexpr DXGI_FORMAT raw_shorts[]={DXGI_FORMAT_R16_UNORM,DXGI_FORMAT_R16G16_UNORM,DXGI_FORMAT_R16G16B16A16_UNORM,DXGI_FORMAT_R16G16B16A16_UNORM};
    constexpr DXGI_FORMAT bytes[]={DXGI_FORMAT_R8_UNORM,DXGI_FORMAT_R8G8_UNORM,DXGI_FORMAT_R8G8B8A8_UNORM,DXGI_FORMAT_R8G8B8A8_UNORM};
    switch(type) {
    case 1:return {shorts[size-1],size*2,(size==3?4:size)*2};
    case 2:return {floats[size-1],size*4,size*4};
    case 4:return {bytes[size-1],size,size==3?4:size};
    case 5:return {raw_shorts[size-1],size*2,(size==3?4:size)*2};
    default:throw std::runtime_error("Unsupported NV2A vertex format: "+hex32(format));
    }
}
}

void save_png(const Image& image, const std::filesystem::path& path, bool replace) {
    extent(image.width, image.height);
    if (image.rgba.size() != std::size_t(image.width) * image.height * 4) throw std::runtime_error("Invalid RGBA image size");
    ComScope scope;
    auto factory = image_factory();
    ComPtr<IStream> stream;
    checked(CreateStreamOnHGlobal(nullptr, TRUE, &stream), "PNG stream");
    ComPtr<IWICBitmapEncoder> encoder;
    checked(factory->CreateEncoder(GUID_ContainerFormatPng, nullptr, &encoder), "PNG encoder");
    checked(encoder->Initialize(stream.Get(), WICBitmapEncoderNoCache), "PNG initialization");
    ComPtr<IWICBitmapFrameEncode> frame;
    ComPtr<IPropertyBag2> properties;
    checked(encoder->CreateNewFrame(&frame, &properties), "PNG frame");
    checked(frame->Initialize(properties.Get()), "PNG frame initialization");
    checked(frame->SetSize(image.width, image.height), "PNG dimensions");
    auto format = GUID_WICPixelFormat32bppBGRA;
    checked(frame->SetPixelFormat(&format), "PNG format");
    if (format != GUID_WICPixelFormat32bppBGRA && format != GUID_WICPixelFormat32bppRGBA)
        throw std::runtime_error("PNG encoder cannot preserve RGBA channels");
    ComPtr<IWICBitmap> bitmap;
    checked(factory->CreateBitmapFromMemory(image.width,image.height,GUID_WICPixelFormat32bppRGBA,
        image.width*4,static_cast<UINT>(image.rgba.size()),
        reinterpret_cast<BYTE*>(const_cast<std::byte*>(image.rgba.data())),&bitmap), "PNG source bitmap");
    ComPtr<IWICFormatConverter> converter;
    checked(factory->CreateFormatConverter(&converter), "PNG format converter");
    checked(converter->Initialize(bitmap.Get(),format,WICBitmapDitherTypeNone,nullptr,0,
                                  WICBitmapPaletteTypeCustom), "PNG channel conversion");
    checked(frame->WriteSource(converter.Get(),nullptr), "PNG pixels");
    checked(frame->Commit(), "PNG frame commit");
    checked(encoder->Commit(), "PNG commit");
    STATSTG info{};
    checked(stream->Stat(&info, STATFLAG_NONAME), "PNG stream size");
    if (info.cbSize.QuadPart > 128 * 1024 * 1024) throw std::runtime_error("Encoded PNG exceeds limit");
    HGLOBAL handle;
    checked(GetHGlobalFromStream(stream.Get(), &handle), "PNG stream memory");
    const auto bytes = static_cast<const char*>(GlobalLock(handle));
    if (!bytes) throw std::runtime_error("Cannot lock PNG stream");
    try { write_text(path, {bytes, static_cast<std::size_t>(info.cbSize.QuadPart)}, replace); }
    catch (...) { GlobalUnlock(handle); throw; }
    GlobalUnlock(handle);
}
Image load_image(const std::filesystem::path& path) {
    ComScope scope;
    auto factory = image_factory();
    ComPtr<IWICBitmapDecoder> decoder;
    checked(factory->CreateDecoderFromFilename(path.c_str(), nullptr, GENERIC_READ, WICDecodeMetadataCacheOnDemand, &decoder), "Image decoder");
    ComPtr<IWICBitmapFrameDecode> frame;
    checked(decoder->GetFrame(0, &frame), "Image frame");
    Image image{};
    checked(frame->GetSize(&image.width, &image.height), "Image size");
    extent(image.width, image.height);
    image.rgba.resize(std::size_t(image.width) * image.height * 4);
    ComPtr<IWICFormatConverter> converter;
    checked(factory->CreateFormatConverter(&converter), "Image converter");
    checked(converter->Initialize(frame.Get(), GUID_WICPixelFormat32bppRGBA, WICBitmapDitherTypeNone,
                                  nullptr, 0, WICBitmapPaletteTypeCustom), "RGBA conversion");
    checked(converter->CopyPixels(nullptr, image.width * 4, static_cast<UINT>(image.rgba.size()),
                                  reinterpret_cast<BYTE*>(image.rgba.data())), "Image pixels");
    return image;
}
std::string compare_images(const std::filesystem::path& actual, const std::filesystem::path& reference,
                           const std::filesystem::path& difference, unsigned tolerance) {
    if (tolerance > 255) throw std::runtime_error("Image tolerance must be 0..255");
    const auto a = load_image(actual), b = load_image(reference);
    if (a.width != b.width || a.height != b.height) throw std::runtime_error("Image dimensions differ; align reference viewpoint and size first");
    Image diff{a.width, a.height, std::vector<std::byte>(a.rgba.size())};
    std::uint64_t sum = 0, changed = 0;
    unsigned maximum = 0;
    for (std::size_t i = 0; i < a.rgba.size(); i += 4) {
        bool over = false;
        for (unsigned lane = 0; lane < 3; ++lane) {
            const auto delta = static_cast<unsigned>(std::abs(int(a.rgba[i + lane]) - int(b.rgba[i + lane])));
            sum += delta; maximum = std::max(maximum, delta); over |= delta > tolerance;
            diff.rgba[i + lane] = std::byte(delta);
        }
        diff.rgba[i + 3] = std::byte{255};
        changed += over;
    }
    save_png(diff, difference);
    return std::format("{{\"format\":\"b2-image-compare-v1\",\"passed\":{},\"width\":{},\"height\":{},\"tolerance\":{},\"pixels_over_tolerance\":{},\"maximum_error\":{},\"mean_absolute_error\":{},\"difference\":{}}}",
                       changed == 0, a.width, a.height, tolerance, changed, maximum,
                       double(sum) / (double(a.width) * a.height * 3), json(utf8(difference.wstring())));
}

struct Renderer::Impl {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    std::array<ComPtr<ID3D11PixelShader>,4> simple_combiners;
    ComPtr<ID3D11Texture2D> target, depth, staging;
    ComPtr<ID3D11RenderTargetView> rtv;
    ComPtr<ID3D11DepthStencilView> dsv;
    ComPtr<ID3D11VertexShader> vs;
    std::array<ComPtr<ID3D11PixelShader>,9> combiners;
    std::array<ComPtr<ID3D11VertexShader>,7> fixed_vertices;
    std::array<Bytes,7> fixed_signatures;
    std::array<ComPtr<ID3D11InputLayout>,7> fixed_layouts;
    ComPtr<ID3D11InputLayout> layout;
    ComPtr<ID3D11Buffer> vertices, index_buffer, factor_buffer, program_constants;
    ComPtr<ID3D11Buffer> launch_constants, launch_inputs, launch_readback;
    ComPtr<ID3D11UnorderedAccessView> launch_output;
    ComPtr<ID3D11ShaderResourceView> launch_input;
    struct Timing {ComPtr<ID3D11Query> timer,start,finish;bool pending=false;std::uint64_t frame=0;};
    std::array<Timing,8> timings;
    unsigned timing_index=8;
    std::uint64_t frame_id=0,latest_timing=0;
    ComPtr<ID3D11InfoQueue> info;
    struct FrameTarget {
        std::uint32_t width,height;
        SurfaceColor color_format=SurfaceColor::rgba8;
        SurfaceDepth depth_format=SurfaceDepth::z24s8;
        ComPtr<ID3D11Texture2D> color,depth,staging;
        ComPtr<ID3D11RenderTargetView> color_view;
        ComPtr<ID3D11DepthStencilView> depth_view;
        ComPtr<ID3D11ShaderResourceView> texture_view;
    };
    std::map<std::uint32_t,FrameTarget> targets;
    std::uint32_t current_target=0;
    std::size_t target_bytes=0;
    HWND window=nullptr;
    ComPtr<IDXGISwapChain1> swapchain;
    ComPtr<ID3D11RenderTargetView> window_view;
    ComPtr<ID3D11VertexShader> display_vs;
    ComPtr<ID3D11PixelShader> display_ps;
    ComPtr<ID3D11SamplerState> display_sampler;
    ComPtr<ID3D11RasterizerState> display_raster;
    ComPtr<ID3D11DepthStencilState> display_depth;
    ComPtr<ID3D11VertexShader> clear_vs;
    ComPtr<ID3D11PixelShader> clear_ps;
    ComPtr<ID3D11Buffer> clear_constants;
    ComPtr<ID3D11RasterizerState> clear_raster;
    std::array<ComPtr<ID3D11BlendState>,16> clear_blends;
    std::array<ComPtr<ID3D11DepthStencilState>,4> clear_depths;
    std::uint32_t window_width=0,window_height=0;
    bool displaying=false;
    struct Pipeline {
        ComPtr<ID3D11BlendState> blend;
        ComPtr<ID3D11DepthStencilState> depth;
        ComPtr<ID3D11RasterizerState> raster;
        std::array<ComPtr<ID3D11SamplerState>, 4> samplers;
    };
    std::map<PipelineKey, Pipeline> pipelines;
    struct Program {
        VertexProgram key;
        bool launch;
        ComPtr<ID3D11VertexShader> vertex;
        ComPtr<ID3D11ComputeShader> compute;
        ComPtr<ID3D11InputLayout> layout;
        Bytes signature;
        bool fixed=false;
        FixedTransform fixed_key{};
    };
    std::vector<Program> programs;
    struct StreamLayout {std::uint32_t program;VertexLayout attributes;ComPtr<ID3D11InputLayout> native;};
    std::vector<StreamLayout> stream_layouts;
    struct StreamDraw {const StreamLayout& layout;std::span<const VertexStream> streams;const ProgramVertex& current;};
    std::vector<ComPtr<ID3D11ShaderResourceView>> textures{1};
    std::vector<unsigned> texture_dimensions{0};
    std::vector<std::uint32_t> converted;
    std::string adapter;
    std::uint32_t width, height;
    RenderStats stats;
    std::size_t texture_bytes = 0;
    bool recording = false, ended = false;
    static constexpr UINT max_vertices = 32768, max_indices = max_vertices * 6;
    Bytes shader(ShaderKind kind,const VertexProgram* program=nullptr) {
        const auto result=shader_binary(kind,program);stats.shader_compilations+=result.compiled;return result.bytes;
    }
    Impl(std::uint32_t w, std::uint32_t h, bool warp, bool debug) : width(w), height(h) {
        extent(w, h);
        const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_0};
        D3D_FEATURE_LEVEL level;
        const UINT flags = D3D11_CREATE_DEVICE_SINGLETHREADED | (debug ? D3D11_CREATE_DEVICE_DEBUG : 0);
        checked(D3D11CreateDevice(nullptr, warp ? D3D_DRIVER_TYPE_WARP : D3D_DRIVER_TYPE_HARDWARE,
                                  nullptr, flags, levels, 1, D3D11_SDK_VERSION, &device, &level, &context), "D3D11 device");
        ComPtr<IDXGIDevice> dxgi;
        checked(device.As(&dxgi), "DXGI device");
        ComPtr<IDXGIAdapter> gpu;
        checked(dxgi->GetAdapter(&gpu), "DXGI adapter");
        DXGI_ADAPTER_DESC description{};
        checked(gpu->GetDesc(&description), "Adapter description");
        adapter = utf8(description.Description);
        if (debug) checked(device.As(&info), "D3D11 debug queue");
        resize(w,h);
        const auto code = shader(ShaderKind::passthrough);
        checked(device->CreateVertexShader(code.data(),code.size(),nullptr,&vs), "Vertex shader");
        const D3D11_INPUT_ELEMENT_DESC inputs[] = {
            {"POSITION",0,DXGI_FORMAT_R32G32B32A32_FLOAT,0,0,D3D11_INPUT_PER_VERTEX_DATA,0},
            {"COLOR",0,DXGI_FORMAT_R32G32B32A32_FLOAT,0,16,D3D11_INPUT_PER_VERTEX_DATA,0},
            {"COLOR",1,DXGI_FORMAT_R32G32B32A32_FLOAT,0,32,D3D11_INPUT_PER_VERTEX_DATA,0},
            {"TEXCOORD",0,DXGI_FORMAT_R32G32B32A32_FLOAT,0,48,D3D11_INPUT_PER_VERTEX_DATA,0},
            {"TEXCOORD",1,DXGI_FORMAT_R32G32B32A32_FLOAT,0,64,D3D11_INPUT_PER_VERTEX_DATA,0},
            {"TEXCOORD",2,DXGI_FORMAT_R32G32B32A32_FLOAT,0,80,D3D11_INPUT_PER_VERTEX_DATA,0},
            {"TEXCOORD",3,DXGI_FORMAT_R32G32B32A32_FLOAT,0,96,D3D11_INPUT_PER_VERTEX_DATA,0},
            {"TEXCOORD",4,DXGI_FORMAT_R32_FLOAT,0,112,D3D11_INPUT_PER_VERTEX_DATA,0}};
        static_assert(sizeof(Vertex) == 116);
        checked(device->CreateInputLayout(inputs, static_cast<UINT>(std::size(inputs)), code.data(),code.size(), &layout), "Vertex layout");
        for(unsigned i=0;i<combiners.size();++i) {
            const auto pixel=shader(ShaderKind(unsigned(ShaderKind::combiner0)+i));
            checked(device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&combiners[i]),"Combiner shader");
        }
        for(unsigned i=0;i<simple_combiners.size();++i) {
            const auto pixel=shader(ShaderKind(unsigned(ShaderKind::color)+i));
            checked(device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&simple_combiners[i]),"Simple combiner shader");
        }
        std::array<D3D11_INPUT_ELEMENT_DESC,16> program_inputs{};
        for(unsigned i=0;i<program_inputs.size();++i)
            program_inputs[i]={"TEXCOORD",i,DXGI_FORMAT_R32G32B32A32_FLOAT,0,i*16,D3D11_INPUT_PER_VERTEX_DATA,0};
        for(unsigned i=0;i<fixed_vertices.size();++i) {
            const auto fixed=shader(ShaderKind(unsigned(ShaderKind::fixed0)+i));fixed_signatures[i]=fixed;
            checked(device->CreateVertexShader(fixed.data(),fixed.size(),nullptr,&fixed_vertices[i]),"Fixed transform shader");
            checked(device->CreateInputLayout(program_inputs.data(),16,fixed.data(),fixed.size(),&fixed_layouts[i]),"Fixed transform layout");
        }
        const auto buffer = [&](UINT bytes, UINT bind, ComPtr<ID3D11Buffer>& output) {
            D3D11_BUFFER_DESC desc{};
            desc.ByteWidth = bytes; desc.Usage = D3D11_USAGE_DYNAMIC;
            desc.BindFlags = bind; desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
            checked(device->CreateBuffer(&desc, nullptr, &output), "Dynamic buffer");
        };
        buffer(max_vertices * sizeof(ProgramVertex), D3D11_BIND_VERTEX_BUFFER, vertices);
        buffer(max_indices * sizeof(std::uint32_t), D3D11_BIND_INDEX_BUFFER, index_buffer);
        buffer(sizeof(Constants), D3D11_BIND_CONSTANT_BUFFER, factor_buffer);
        buffer(sizeof(ProgramUniforms),D3D11_BIND_CONSTANT_BUFFER,program_constants);
        buffer(32,D3D11_BIND_CONSTANT_BUFFER,clear_constants);
        converted.reserve(max_indices);
        for(auto& timing:timings) {
            D3D11_QUERY_DESC query{D3D11_QUERY_TIMESTAMP_DISJOINT,0};
            checked(device->CreateQuery(&query,&timing.timer),"GPU timer");query.Query=D3D11_QUERY_TIMESTAMP;
            checked(device->CreateQuery(&query,&timing.start),"GPU start timestamp");
            checked(device->CreateQuery(&query,&timing.finish),"GPU end timestamp");
        }
        const auto clear_vertex=shader(ShaderKind::clear_vertex),clear_pixel=shader(ShaderKind::clear_pixel);
        checked(device->CreateVertexShader(clear_vertex.data(),clear_vertex.size(),nullptr,&clear_vs),"Clear vertex shader");
        checked(device->CreatePixelShader(clear_pixel.data(),clear_pixel.size(),nullptr,&clear_ps),"Clear pixel shader");
        for(unsigned mask=0;mask<clear_blends.size();++mask) {
            D3D11_BLEND_DESC desc{};auto& output=desc.RenderTarget[0];
            output.RenderTargetWriteMask=static_cast<UINT8>(mask);
            output.SrcBlend=output.SrcBlendAlpha=D3D11_BLEND_ONE;
            output.DestBlend=output.DestBlendAlpha=D3D11_BLEND_ZERO;
            output.BlendOp=output.BlendOpAlpha=D3D11_BLEND_OP_ADD;
            checked(device->CreateBlendState(&desc,&clear_blends[mask]),"Clear channel mask");
        }
        for(unsigned mask=0;mask<clear_depths.size();++mask) {
            D3D11_DEPTH_STENCIL_DESC desc{};
            desc.DepthEnable=(mask&1)!=0;desc.DepthWriteMask=mask&1?D3D11_DEPTH_WRITE_MASK_ALL:D3D11_DEPTH_WRITE_MASK_ZERO;
            desc.DepthFunc=D3D11_COMPARISON_ALWAYS;desc.StencilEnable=(mask&2)!=0;
            desc.StencilReadMask=desc.StencilWriteMask=255;
            desc.FrontFace=desc.BackFace={D3D11_STENCIL_OP_REPLACE,D3D11_STENCIL_OP_REPLACE,D3D11_STENCIL_OP_REPLACE,D3D11_COMPARISON_ALWAYS};
            checked(device->CreateDepthStencilState(&desc,&clear_depths[mask]),"Clear depth/stencil mask");
        }
        D3D11_RASTERIZER_DESC clear_desc{};clear_desc.FillMode=D3D11_FILL_SOLID;clear_desc.CullMode=D3D11_CULL_NONE;
        clear_desc.DepthClipEnable=clear_desc.ScissorEnable=TRUE;
        checked(device->CreateRasterizerState(&clear_desc,&clear_raster),"Clear rectangle state");
    }
    void collect_timings() {
        for(auto& timing:timings) if(timing.pending) {
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT result{};UINT64 start=0,finish=0;
            const auto status=context->GetData(timing.timer.Get(),&result,sizeof(result),D3D11_ASYNC_GETDATA_DONOTFLUSH);
            checked(status,"GPU timing query");
            if(status!=S_OK) continue;
            const auto first=context->GetData(timing.start.Get(),&start,sizeof(start),D3D11_ASYNC_GETDATA_DONOTFLUSH);
            const auto last=context->GetData(timing.finish.Get(),&finish,sizeof(finish),D3D11_ASYNC_GETDATA_DONOTFLUSH);
            checked(first,"GPU start query");checked(last,"GPU finish query");
            if(first!=S_OK || last!=S_OK) continue;
            timing.pending=false;
            if(!result.Disjoint && result.Frequency && timing.frame>=latest_timing) {
                stats.gpu_ms=double(finish-start)*1000/double(result.Frequency);stats.gpu_timing_valid=true;latest_timing=timing.frame;
            }
        }
    }
    void draw(const RenderState&, Bytes, std::size_t, std::span<const std::uint32_t>,
              const Program* = nullptr, const ProgramUniforms* = nullptr,const StreamDraw* = nullptr);
    FrameTarget make_target(std::uint32_t w,std::uint32_t h,SurfaceColor color_format=SurfaceColor::rgba8,SurfaceDepth depth_format=SurfaceDepth::z24s8) {
        extent(w,h);
        FrameTarget output{w,h,color_format,depth_format};
        const auto native_color=color_format==SurfaceColor::rgb565?DXGI_FORMAT_B5G6R5_UNORM:DXGI_FORMAT_R8G8B8A8_UNORM;
        D3D11_TEXTURE2D_DESC texture{};
        texture.Width=w; texture.Height=h; texture.MipLevels=texture.ArraySize=1;
        texture.Format=native_color; texture.SampleDesc.Count=1;
        texture.BindFlags=D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        checked(device->CreateTexture2D(&texture,nullptr,&output.color), "Offscreen target");
        checked(device->CreateRenderTargetView(output.color.Get(),nullptr,&output.color_view), "Offscreen view");
        checked(device->CreateShaderResourceView(output.color.Get(),nullptr,&output.texture_view),"Target texture view");
        texture.Format=depth_format==SurfaceDepth::z16?DXGI_FORMAT_D16_UNORM:DXGI_FORMAT_D24_UNORM_S8_UINT; texture.BindFlags=D3D11_BIND_DEPTH_STENCIL;
        checked(device->CreateTexture2D(&texture,nullptr,&output.depth), "Depth target");
        checked(device->CreateDepthStencilView(output.depth.Get(),nullptr,&output.depth_view), "Depth view");
        texture.Format=native_color; texture.BindFlags=0;
        texture.Usage=D3D11_USAGE_STAGING; texture.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        checked(device->CreateTexture2D(&texture,nullptr,&output.staging), "Readback target");
        return output;
    }
    void unbind_textures() {
        std::array<ID3D11ShaderResourceView*,12> empty{};
        context->PSSetShaderResources(0,static_cast<UINT>(empty.size()),empty.data());
    }
    void select_target(std::uint32_t id) {
        const auto found=targets.find(id);
        if(found==targets.end()) throw std::runtime_error("Render target was not created");
        unbind_textures();context->OMSetRenderTargets(0,nullptr,nullptr);
        const auto& output=found->second;
        width=output.width;height=output.height;current_target=id;
        target=output.color;depth=output.depth;staging=output.staging;rtv=output.color_view;dsv=output.depth_view;
        auto color=rtv.Get();context->OMSetRenderTargets(1,&color,dsv.Get());
        const D3D11_VIEWPORT viewport{0,0,float(width),float(height),0,1};
        context->RSSetViewports(1,&viewport);
    }
    void resize(std::uint32_t w, std::uint32_t h) {
        extent(w,h);
        if (recording || current_target) throw std::runtime_error("Resize the main target outside frame submission");
        if (target && width==w && height==h) return;
        const auto required=std::size_t(w)*h*12;
        const auto prior=target?std::size_t(width)*height*12:0;
        if(required>256*1024*1024-(target_bytes-prior)) throw std::runtime_error("Render targets exceed 256 MiB");
        auto output=make_target(w,h);
        targets.insert_or_assign(0,std::move(output));target_bytes=target_bytes-prior+required;
        select_target(0);ended=false;
    }
};
Renderer::Renderer(std::uint32_t width, std::uint32_t height, bool warp, bool debug)
    : impl_(std::make_unique<Impl>(width, height, warp, debug)) {
    for(const auto& shader:static_vertex_shaders()) prepare_program(shader.program,shader.launch);
}
Renderer::~Renderer() = default;
void Renderer::resize(std::uint32_t width, std::uint32_t height) { impl_->resize(width,height); }
std::uint32_t Renderer::create_target(std::uint32_t width,std::uint32_t height,SurfaceColor color,SurfaceDepth depth) {
    auto& r=*impl_;extent(width,height);
    if(r.recording) throw std::runtime_error("Create render targets before frame submission");
    const auto required=std::size_t(width)*height*12;
    if(r.textures.size()>=4096 || required>256*1024*1024-r.target_bytes)
        throw std::runtime_error("Render target resource limit reached");
    auto output=r.make_target(width,height,color,depth);
    const auto id=static_cast<std::uint32_t>(r.textures.size());
    r.textures.push_back(output.texture_view);r.texture_dimensions.push_back(1);
    r.targets.emplace(id,std::move(output));r.target_bytes+=required;
    if(color==SurfaceColor::rgb565) {
        RenderState conversion;conversion.texture_modes[0]=1;conversion.textures[0]=id;conversion.final_inputs={0x00000008,0x00001800};
        prepare(conversion);
    }
    return id;
}
void Renderer::set_target(std::uint32_t target) {impl_->select_target(target);}
void Renderer::grow_target(std::uint32_t target,std::uint32_t width,std::uint32_t height) {
    auto& r=*impl_;extent(width,height);
    const auto found=r.targets.find(target);
    if(!target || found==r.targets.end() || r.recording) throw std::runtime_error("Grow an existing auxiliary target outside submission");
    const auto& prior=found->second;
    if(width<prior.width || height<prior.height) throw std::runtime_error("Target growth cannot shrink either dimension");
    if(width==prior.width && height==prior.height) return;
    const auto previous=std::size_t(prior.width)*prior.height*12,required=std::size_t(width)*height*12;
    if(required>256*1024*1024-(r.target_bytes-previous)) throw std::runtime_error("Render targets exceed 256 MiB");
    auto output=r.make_target(width,height,prior.color_format,prior.depth_format);
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    constexpr float black[4]{};
    r.context->ClearRenderTargetView(output.color_view.Get(),black);
    r.context->ClearDepthStencilView(output.depth_view.Get(),D3D11_CLEAR_DEPTH|(prior.depth_format==SurfaceDepth::z16?0:D3D11_CLEAR_STENCIL),1,0);
    r.context->CopySubresourceRegion(output.color.Get(),0,0,0,0,prior.color.Get(),0,nullptr);
    // Depth/stencil resources require whole copies with matching dimensions.
    // Migrate their packed bits through ordinary typeless textures, then copy
    // the complete enlarged image back into its depth/stencil resource.
    D3D11_TEXTURE2D_DESC packed{};packed.Width=prior.width;packed.Height=prior.height;
    packed.MipLevels=packed.ArraySize=1;packed.Format=prior.depth_format==SurfaceDepth::z16?DXGI_FORMAT_R16_TYPELESS:DXGI_FORMAT_R24G8_TYPELESS;packed.SampleDesc.Count=1;
    ComPtr<ID3D11Texture2D> previous_depth,grown_depth;
    checked(r.device->CreateTexture2D(&packed,nullptr,&previous_depth),"Previous depth transfer");
    packed.Width=width;packed.Height=height;
    checked(r.device->CreateTexture2D(&packed,nullptr,&grown_depth),"Grown depth transfer");
    r.context->CopyResource(previous_depth.Get(),prior.depth.Get());
    r.context->CopyResource(grown_depth.Get(),output.depth.Get());
    r.context->CopySubresourceRegion(grown_depth.Get(),0,0,0,0,previous_depth.Get(),0,nullptr);
    r.context->CopyResource(output.depth.Get(),grown_depth.Get());
    found->second=std::move(output);r.textures[target]=found->second.texture_view;
    r.target_bytes=r.target_bytes-previous+required;r.select_target(r.current_target);
}
void Renderer::copy_target(std::uint32_t destination) {
    auto& r=*impl_;
    const auto found=r.targets.find(destination);
    if(found==r.targets.end() || destination==r.current_target || found->second.width!=r.width || found->second.height!=r.height ||
        found->second.color_format!=r.targets.at(r.current_target).color_format)
        throw std::runtime_error("Target copy requires a distinct target with matching dimensions");
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    r.context->CopyResource(found->second.color.Get(),r.target.Get());
    auto view=r.rtv.Get();r.context->OMSetRenderTargets(1,&view,r.dsv.Get());
}
void Renderer::copy_to_main() {
    auto& r=*impl_;
    if(r.recording || !r.current_target) throw std::runtime_error("Copy an ended auxiliary target to the main image");
    const auto source=r.target;
    const auto identifier=r.current_target;
    const auto format=r.targets.at(identifier).color_format;
    const auto width=r.width,height=r.height;
    const auto& main=r.targets.at(0);
    if(main.width!=width || main.height!=height) throw std::runtime_error("Presentation target dimensions differ");
    r.select_target(0);
    if(format==SurfaceColor::rgb565) {
        RenderState state;state.texture_modes[0]=1;state.textures[0]=identifier;state.final_inputs={0x00000008,0x00001800};
        prepare(state);begin(0,1,false);
        std::array<Vertex,4> vertices{};
        constexpr std::array<Float4,4> positions{{{-1,1,0,1},{1,1,0,1},{1,-1,0,1},{-1,-1,0,1}}};
        for(unsigned i=0;i<4;++i) {vertices[i].position=positions[i];vertices[i].uv[0]={(i==1 || i==2)?1.0f:0.0f,i>=2?1.0f:0.0f,0,1};}
        constexpr std::array<std::uint32_t,6> indices{0,1,2,0,2,3};draw(state,vertices,indices);end();return;
    }
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    r.context->CopyResource(r.target.Get(),source.Get());
    r.ended=true;
}
ID3D11Device* Renderer::native_device() const {return impl_->device.Get();}
ID3D11DeviceContext* Renderer::native_context() const {return impl_->context.Get();}
void Renderer::attach_window(void* native_window) {
    auto& r=*impl_;
    if(r.window || !native_window || !IsWindow(static_cast<HWND>(native_window)))
        throw std::runtime_error("Attach one valid native window to the renderer");
    const auto window=static_cast<HWND>(native_window);
    ComPtr<IDXGIDevice> device;ComPtr<IDXGIAdapter> adapter;ComPtr<IDXGIFactory2> factory;
    checked(r.device.As(&device),"Presentation device");checked(device->GetAdapter(&adapter),"Presentation adapter");
    checked(adapter->GetParent(IID_PPV_ARGS(&factory)),"Presentation factory");
    DXGI_SWAP_CHAIN_DESC1 description{};
    description.Width=r.width;description.Height=r.height;description.Format=DXGI_FORMAT_R8G8B8A8_UNORM;
    description.SampleDesc.Count=1;description.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT;
    description.BufferCount=2;description.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;description.Scaling=DXGI_SCALING_STRETCH;
    checked(factory->CreateSwapChainForHwnd(r.device.Get(),window,&description,nullptr,nullptr,&r.swapchain),"Window swapchain");
    checked(factory->MakeWindowAssociation(window,DXGI_MWA_NO_ALT_ENTER),"Window association");
    const auto vs=r.shader(ShaderKind::display_vertex),ps=r.shader(ShaderKind::display_pixel);
    checked(r.device->CreateVertexShader(vs.data(),vs.size(),nullptr,&r.display_vs),"Display vertex shader");
    checked(r.device->CreatePixelShader(ps.data(),ps.size(),nullptr,&r.display_ps),"Display pixel shader");
    D3D11_SAMPLER_DESC sampler{};sampler.Filter=D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    sampler.AddressU=sampler.AddressV=sampler.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;sampler.MaxLOD=D3D11_FLOAT32_MAX;
    sampler.ComparisonFunc=D3D11_COMPARISON_ALWAYS;
    checked(r.device->CreateSamplerState(&sampler,&r.display_sampler),"Display sampler");
    D3D11_RASTERIZER_DESC raster{};raster.FillMode=D3D11_FILL_SOLID;raster.CullMode=D3D11_CULL_NONE;raster.DepthClipEnable=true;
    checked(r.device->CreateRasterizerState(&raster,&r.display_raster),"Display rasterizer");
    D3D11_DEPTH_STENCIL_DESC depth{};
    depth.DepthFunc=D3D11_COMPARISON_ALWAYS;
    depth.FrontFace=depth.BackFace={D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_STENCIL_OP_KEEP,D3D11_COMPARISON_ALWAYS};
    checked(r.device->CreateDepthStencilState(&depth,&r.display_depth),"Display depth state");
    r.window=window;
}
bool Renderer::display() {
    auto& r=*impl_;
    if(!r.window || !r.ended || r.recording || r.current_target) throw std::runtime_error("Display the completed main target on its attached window");
    RECT rectangle{};if(!GetClientRect(r.window,&rectangle)) throw std::runtime_error("Cannot read window dimensions");
    const auto width=static_cast<std::uint32_t>(rectangle.right),height=static_cast<std::uint32_t>(rectangle.bottom);
    if(!width || !height) return false;
    extent(width,height);
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    if(!r.window_view || r.window_width!=width || r.window_height!=height) {
        r.window_view.Reset();checked(r.swapchain->ResizeBuffers(0,width,height,DXGI_FORMAT_UNKNOWN,0),"Window resize");
        ComPtr<ID3D11Texture2D> buffer;checked(r.swapchain->GetBuffer(0,IID_PPV_ARGS(&buffer)),"Window buffer");
        checked(r.device->CreateRenderTargetView(buffer.Get(),nullptr,&r.window_view),"Window render target");
        r.window_width=width;r.window_height=height;
    }
    auto output=r.window_view.Get();r.context->OMSetRenderTargets(1,&output,nullptr);
    const float black[4]={0,0,0,1};r.context->ClearRenderTargetView(output,black);
    const auto scale=std::min(float(width)/r.width,float(height)/r.height);
    const auto image_width=r.width*scale,image_height=r.height*scale;
    const D3D11_VIEWPORT viewport{(width-image_width)/2,(height-image_height)/2,image_width,image_height,0,1};r.context->RSSetViewports(1,&viewport);
    r.context->RSSetState(r.display_raster.Get());r.context->OMSetDepthStencilState(r.display_depth.Get(),0);
    r.context->OMSetBlendState(nullptr,nullptr,UINT_MAX);
    r.context->IASetInputLayout(nullptr);r.context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    r.context->VSSetShader(r.display_vs.Get(),nullptr,0);r.context->PSSetShader(r.display_ps.Get(),nullptr,0);
    auto texture=r.targets.at(0).texture_view.Get();auto sampler=r.display_sampler.Get();
    r.context->PSSetShaderResources(0,1,&texture);r.context->PSSetSamplers(0,1,&sampler);r.context->Draw(3,0);
    r.unbind_textures();r.displaying=true;return true;
}
void Renderer::present(bool vsync) {
    auto& r=*impl_;
    if(!r.displaying) throw std::runtime_error("Display a frame before presenting");
    checked(r.swapchain->Present(vsync?1:0,0),"Window presentation");r.displaying=false;
}

std::uint32_t Renderer::upload(const TextureData& texture) {
    auto& r = *impl_;
    if (r.recording) throw std::runtime_error("Upload textures before frame submission");
    extent(texture.width, texture.height);
    if (!texture.depth || texture.depth>2048 || (texture.depth>1 && (texture.width>2048 || texture.height>2048)) ||
        texture.bytes.size() > 64 * 1024 * 1024 || !texture.mip_levels ||
        texture.mip_levels > static_cast<unsigned>(std::bit_width(std::max({texture.width, texture.height,texture.depth}))))
        throw std::runtime_error("Invalid texture size or mip count");
    DXGI_FORMAT format;
    unsigned bpp = 0, block = 0;
    bool swizzled = false;
    switch (texture.format) {
    case TextureFormat::rgb565: format = DXGI_FORMAT_B5G6R5_UNORM; bpp = 2; swizzled = true; break;
    case TextureFormat::argb8: format = DXGI_FORMAT_B8G8R8A8_UNORM; bpp = 4; swizzled = true; break;
    case TextureFormat::xrgb8: format = DXGI_FORMAT_B8G8R8X8_UNORM; bpp = 4; swizzled = true; break;
    case TextureFormat::argb8_linear: format = DXGI_FORMAT_B8G8R8A8_UNORM; bpp = 4; break;
    case TextureFormat::xrgb8_linear: format = DXGI_FORMAT_B8G8R8X8_UNORM; bpp = 4; break;
    case TextureFormat::dxt1: format = DXGI_FORMAT_BC1_UNORM; block = 8; break;
    case TextureFormat::dxt3: format = DXGI_FORMAT_BC2_UNORM; block = 16; break;
    case TextureFormat::dxt5: format = DXGI_FORMAT_BC3_UNORM; block = 16; break;
    default: throw std::runtime_error("Unsupported NV097 texture format: " + hex32(static_cast<std::uint32_t>(texture.format)));
    }
    if (swizzled && (!std::has_single_bit(texture.width) || !std::has_single_bit(texture.height) || !std::has_single_bit(texture.depth)))
        throw std::runtime_error("Swizzled texture dimensions must be powers of two");
    if (block && ((texture.width % 4) || (texture.height % 4)))
        throw std::runtime_error("BC top-level dimensions must be multiples of four");
    if (texture.pitch && (swizzled || block || texture.mip_levels != 1))
        throw std::runtime_error("An explicit pitch requires a single linear mip");
    if (r.textures.size() >= 4096) throw std::runtime_error("Texture resource limit reached");
    if (texture.bytes.size() > 256 * 1024 * 1024 - r.texture_bytes)
        throw std::runtime_error("Texture resources exceed 256 MiB");
    if(texture.cubemap && (texture.width!=texture.height || texture.depth!=1)) throw std::runtime_error("Cubemap faces must be square and two-dimensional");
    const auto faces=texture.cubemap?6U:1U;
    std::vector<D3D11_SUBRESOURCE_DATA> data(texture.mip_levels*faces);
    std::vector<std::vector<std::byte>> linear(texture.mip_levels*faces);
    std::size_t offset = 0;
    for(unsigned face=0;face<faces;++face) for (unsigned mip = 0; mip < texture.mip_levels; ++mip) {
        const auto subresource=face*texture.mip_levels+mip;
        const auto w = std::max(1U, texture.width >> mip), h = std::max(1U, texture.height >> mip), d=std::max(1U,texture.depth>>mip);
        const auto pitch = texture.pitch ? texture.pitch : block ? ((w + 3) / 4) * block : w * bpp;
        if (pitch < w * bpp) throw std::runtime_error("Texture pitch is shorter than a row");
        const auto slice = std::size_t(pitch) * (block ? (h + 3) / 4 : h),size=slice*d;
        if (offset > texture.bytes.size() || size > texture.bytes.size() - offset) throw std::runtime_error("Truncated texture mip payload");
        const auto input_bytes = texture.bytes.subspan(offset, size);
        const void* pixels = input_bytes.data();
        if (swizzled) {
            auto& output = linear[subresource]; output.resize(size);
            std::uint32_t mx = 0, my = 0, mz=0,bit = 1;
            for (unsigned dimension = 1; dimension < std::max({w,h,d}); dimension <<= 1) {
                if (dimension < w) { mx |= bit; bit <<= 1; }
                if (dimension < h) { my |= bit; bit <<= 1; }
                if (dimension < d) { mz |= bit; bit <<= 1; }
            }
            std::uint32_t sz=0;
            for(unsigned z=0;z<d;++z) {
                std::uint32_t sy = 0;
                for (unsigned y = 0; y < h; ++y) {
                    std::uint32_t sx = 0;
                    for (unsigned x = 0; x < w; ++x) {
                        std::memcpy(output.data() + ((std::size_t(z)*h+y) * w + x) * bpp,
                                    input_bytes.data() + (sx + sy+sz) * bpp, bpp);
                        sx = (sx - mx) & mx;
                    }
                    sy = (sy - my) & my;
                }
                sz=(sz-mz)&mz;
            }
            pixels = output.data();
        }
        data[subresource] = {pixels, pitch, static_cast<UINT>(slice)};
        offset += size;
    }
    if (offset != texture.bytes.size()) throw std::runtime_error("Texture payload has trailing bytes; specify its full mip count");
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = texture.width; desc.Height = texture.height; desc.MipLevels = texture.mip_levels;
    desc.ArraySize = faces; desc.Format = format; desc.SampleDesc.Count = 1;
    desc.MiscFlags=texture.cubemap?D3D11_RESOURCE_MISC_TEXTURECUBE:0;
    desc.Usage = D3D11_USAGE_IMMUTABLE; desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    ComPtr<ID3D11ShaderResourceView> view;
    if(texture.depth>1) {
        D3D11_TEXTURE3D_DESC volume{};
        volume.Width=texture.width;volume.Height=texture.height;volume.Depth=texture.depth;
        volume.MipLevels=texture.mip_levels;volume.Format=format;
        volume.Usage=D3D11_USAGE_IMMUTABLE;volume.BindFlags=D3D11_BIND_SHADER_RESOURCE;
        ComPtr<ID3D11Texture3D> resource;
        checked(r.device->CreateTexture3D(&volume,data.data(),&resource),"Volume texture upload");
        checked(r.device->CreateShaderResourceView(resource.Get(),nullptr,&view),"Volume texture view");
    } else {
        ComPtr<ID3D11Texture2D> resource;
        checked(r.device->CreateTexture2D(&desc, data.data(), &resource), "Texture upload");
        checked(r.device->CreateShaderResourceView(resource.Get(), nullptr, &view), "Texture view");
    }
    r.textures.push_back(std::move(view));
    r.texture_dimensions.push_back(texture.cubemap?3U:texture.depth>1?2U:1U);
    r.texture_bytes += texture.bytes.size();
    return static_cast<std::uint32_t>(r.textures.size() - 1);
}
void Renderer::prepare(const RenderState& state) {
    auto& r = *impl_;
    if (r.recording) throw std::runtime_error("Prepare pipelines before frame submission");
    validate_fragment(state);
    const auto key = pipeline_key(state);
    if (r.pipelines.contains(key)) return;
    if (r.pipelines.size() >= 1024) throw std::runtime_error("Pipeline cache limit reached");
    Impl::Pipeline pipeline;
    D3D11_BLEND_DESC blend{};
    auto& output = blend.RenderTarget[0];
    output.BlendEnable = state.blend;
    output.SrcBlend = blend_factor(state.blend_source); output.DestBlend = blend_factor(state.blend_destination);
    output.SrcBlendAlpha = alpha_blend_factor(output.SrcBlend); output.DestBlendAlpha = alpha_blend_factor(output.DestBlend);
    output.BlendOp = output.BlendOpAlpha = blend_op(state.blend_equation);
    if (state.color_mask & ~0x01010101U) throw std::runtime_error("Invalid NV097 color write mask");
    output.RenderTargetWriteMask = ((state.color_mask & 0x10000) ? D3D11_COLOR_WRITE_ENABLE_RED : 0) |
        ((state.color_mask & 0x100) ? D3D11_COLOR_WRITE_ENABLE_GREEN : 0) |
        ((state.color_mask & 1) ? D3D11_COLOR_WRITE_ENABLE_BLUE : 0) |
        ((state.color_mask & 0x1000000) ? D3D11_COLOR_WRITE_ENABLE_ALPHA : 0);
    checked(r.device->CreateBlendState(&blend, &pipeline.blend), "Blend state");
    D3D11_DEPTH_STENCIL_DESC depth{};
    depth.DepthEnable = state.depth_test; depth.DepthWriteMask = state.depth_write ? D3D11_DEPTH_WRITE_MASK_ALL : D3D11_DEPTH_WRITE_MASK_ZERO;
    depth.DepthFunc = comparison(state.depth_function);
    depth.StencilEnable=state.stencil_test;
    if(state.stencil_read_mask>255 || state.stencil_write_mask>255) throw std::runtime_error("Stencil masks must fit eight bits");
    depth.StencilReadMask=static_cast<UINT8>(state.stencil_read_mask); depth.StencilWriteMask=static_cast<UINT8>(state.stencil_write_mask);
    depth.FrontFace = depth.BackFace = {stencil_operation(state.stencil_fail),stencil_operation(state.stencil_depth_fail),
        stencil_operation(state.stencil_pass),comparison(state.stencil_function)};
    checked(r.device->CreateDepthStencilState(&depth, &pipeline.depth), "Depth state");
    D3D11_RASTERIZER_DESC raster{};
    raster.FillMode = D3D11_FILL_SOLID; raster.DepthClipEnable = TRUE;
    raster.CullMode = D3D11_CULL_NONE;
    if (state.cull) {
        if (state.cull_face == 0x404) raster.CullMode = D3D11_CULL_FRONT;
        else if (state.cull_face == 0x405) raster.CullMode = D3D11_CULL_BACK;
        else throw std::runtime_error("Unsupported cull face");
    }
    raster.FrontCounterClockwise = state.front_ccw;
    raster.ScissorEnable=state.scissor_test;
    if(state.polygon_offset) {
        if(!std::isfinite(state.polygon_offset_scale) || !std::isfinite(state.polygon_offset_bias) ||
            std::abs(state.polygon_offset_bias)>=2147483648.0f) throw std::runtime_error("Invalid polygon offset");
        raster.SlopeScaledDepthBias=state.polygon_offset_scale; raster.DepthBias=static_cast<INT>(state.polygon_offset_bias);
    }
    checked(r.device->CreateRasterizerState(&raster, &pipeline.raster), "Rasterizer state");
    for (unsigned i = 0; i < 4; ++i) {
        D3D11_SAMPLER_DESC sampler{};
        sampler.Filter = state.linear_filter[i] ? D3D11_FILTER_MIN_MAG_MIP_LINEAR : D3D11_FILTER_MIN_MAG_MIP_POINT;
        sampler.AddressU = address_mode(state.texture_address[i] & 15);
        sampler.AddressV = address_mode((state.texture_address[i] >> 8) & 15);
        sampler.AddressW = address_mode((state.texture_address[i] >> 16) & 15);
        sampler.MaxAnisotropy = 1; sampler.ComparisonFunc = D3D11_COMPARISON_NEVER;
        sampler.MaxLOD = D3D11_FLOAT32_MAX;
        checked(r.device->CreateSamplerState(&sampler, &pipeline.samplers[i]), "Sampler state");
    }
    r.pipelines.emplace(key, std::move(pipeline));
}
std::uint32_t Renderer::prepare_program(const VertexProgram& program,bool launch) {
    auto& r=*impl_;
    if(r.recording) throw std::runtime_error("Prepare vertex programs before frame submission");
    const auto key=normalize_vertex_program(program);
    for(std::size_t i=0;i<r.programs.size();++i)
        if(!r.programs[i].fixed && r.programs[i].launch==launch && r.programs[i].key==key) return static_cast<std::uint32_t>(i+1);
    if(r.programs.size()>=1024) throw std::runtime_error("Vertex program cache limit reached");
    const auto code=r.shader(launch?ShaderKind::launch:ShaderKind::vertex,&key);
    Impl::Program prepared{key,launch};
    if(launch) {
        checked(r.device->CreateComputeShader(code.data(),code.size(),nullptr,&prepared.compute),"Transform launch shader");
        if(!r.launch_constants) {
            D3D11_BUFFER_DESC description{};
            description.ByteWidth=sizeof(VertexConstants); description.StructureByteStride=sizeof(Float4);
            description.Usage=D3D11_USAGE_DEFAULT; description.BindFlags=D3D11_BIND_UNORDERED_ACCESS;
            description.MiscFlags=D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
            checked(r.device->CreateBuffer(&description,nullptr,&r.launch_constants),"Transform launch context");
            D3D11_UNORDERED_ACCESS_VIEW_DESC view{};
            view.ViewDimension=D3D11_UAV_DIMENSION_BUFFER; view.Buffer.NumElements=192;
            checked(r.device->CreateUnorderedAccessView(r.launch_constants.Get(),&view,&r.launch_output),"Transform context view");
            description.ByteWidth=sizeof(ProgramVertex); description.BindFlags=D3D11_BIND_SHADER_RESOURCE;
            checked(r.device->CreateBuffer(&description,nullptr,&r.launch_inputs),"Transform launch inputs");
            D3D11_SHADER_RESOURCE_VIEW_DESC input{};
            input.ViewDimension=D3D11_SRV_DIMENSION_BUFFER; input.Buffer.NumElements=16;
            checked(r.device->CreateShaderResourceView(r.launch_inputs.Get(),&input,&r.launch_input),"Transform input view");
            description.ByteWidth=sizeof(VertexConstants); description.BindFlags=0;
            description.MiscFlags=0; description.StructureByteStride=0;
            description.Usage=D3D11_USAGE_STAGING; description.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
            checked(r.device->CreateBuffer(&description,nullptr,&r.launch_readback),"Transform launch readback");
        }
    } else {
        prepared.signature=code;
        checked(r.device->CreateVertexShader(code.data(),code.size(),nullptr,&prepared.vertex),"Translated vertex shader");
        std::array<D3D11_INPUT_ELEMENT_DESC,16> inputs{};
        for(unsigned i=0;i<inputs.size();++i)
            inputs[i]={"TEXCOORD",i,DXGI_FORMAT_R32G32B32A32_FLOAT,0,i*static_cast<UINT>(sizeof(Float4)),D3D11_INPUT_PER_VERTEX_DATA,0};
        checked(r.device->CreateInputLayout(inputs.data(),static_cast<UINT>(inputs.size()),code.data(),code.size(),&prepared.layout),"Program vertex layout");
    }
    r.programs.push_back(std::move(prepared));
    return static_cast<std::uint32_t>(r.programs.size());
}
std::uint32_t Renderer::prepare_fixed(const FixedTransform& transform) {
    auto& r=*impl_;
    if(r.recording) throw std::runtime_error("Prepare fixed transforms before frame submission");
    validate_fixed_transform(transform);
    for(unsigned i=0;i<r.programs.size();++i) if(r.programs[i].fixed && r.programs[i].fixed_key==transform) return i+1;
    if(r.programs.size()>=1024) throw std::runtime_error("Vertex program cache limit reached");
    Impl::Program prepared{};prepared.fixed=true;prepared.fixed_key=transform;
    prepared.signature=r.fixed_signatures[transform.skin];prepared.vertex=r.fixed_vertices[transform.skin];prepared.layout=r.fixed_layouts[transform.skin];
    r.programs.push_back(std::move(prepared));return static_cast<std::uint32_t>(r.programs.size());
}
std::uint32_t Renderer::prepare_layout(std::uint32_t program,const VertexLayout& attributes) {
    auto& r=*impl_;
    if(r.recording || !program || program>r.programs.size() || r.programs[program-1].launch)
        throw std::runtime_error("Prepare a draw program before its vertex layout");
    for(unsigned i=0;i<r.stream_layouts.size();++i)
        if(r.stream_layouts[i].program==program && r.stream_layouts[i].attributes==attributes) return i+1;
    if(r.stream_layouts.size()>=1024) throw std::runtime_error("Vertex layout cache limit reached");
    std::array<D3D11_INPUT_ELEMENT_DESC,16> inputs{};
    for(unsigned i=0;i<inputs.size();++i) {
        const auto& attribute=attributes[i];
        if(attribute.stream>16 || attribute.offset>2048) throw std::runtime_error("Invalid vertex attribute stream or offset");
        const auto format=attribute.stream==16?AttributeFormat{DXGI_FORMAT_R32G32B32A32_FLOAT,16,16}:attribute_format(attribute.format);
        inputs[i]={"TEXCOORD",i,format.native,attribute.stream,attribute.stream==16?i*16:attribute.offset,D3D11_INPUT_PER_VERTEX_DATA,0};
    }
    Impl::StreamLayout layout{program,attributes};
    const auto& code=r.programs[program-1].signature;
    checked(r.device->CreateInputLayout(inputs.data(),static_cast<UINT>(inputs.size()),code.data(),code.size(),&layout.native),"Native vertex stream layout");
    r.stream_layouts.push_back(std::move(layout));return static_cast<std::uint32_t>(r.stream_layouts.size());
}
void Renderer::launch_program(std::uint32_t program,VertexConstants& constants,const ProgramVertex& input) {
    auto& r=*impl_;
    if(r.recording || !program || program>r.programs.size() || !r.programs[program-1].launch)
        throw std::runtime_error("Launch a prepared transform outside frame submission");
    r.context->UpdateSubresource(r.launch_constants.Get(),0,nullptr,constants.data(),0,0);
    r.context->UpdateSubresource(r.launch_inputs.Get(),0,nullptr,input.attributes.data(),0,0);
    auto output=r.launch_output.Get(); auto inputs=r.launch_input.Get();
    r.context->CSSetUnorderedAccessViews(0,1,&output,nullptr);
    r.context->CSSetShaderResources(0,1,&inputs);
    r.context->CSSetShader(r.programs[program-1].compute.Get(),nullptr,0);
    r.context->Dispatch(1,1,1);
    ID3D11UnorderedAccessView* no_output=nullptr; ID3D11ShaderResourceView* no_input=nullptr;
    r.context->CSSetUnorderedAccessViews(0,1,&no_output,nullptr); r.context->CSSetShaderResources(0,1,&no_input);
    r.context->CSSetShader(nullptr,nullptr,0);
    r.context->CopyResource(r.launch_readback.Get(),r.launch_constants.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    checked(r.context->Map(r.launch_readback.Get(),0,D3D11_MAP_READ,0,&mapped),"Transform context readback");
    std::memcpy(constants.data(),mapped.pData,sizeof(constants)); r.context->Unmap(r.launch_readback.Get(),0);
}
void Renderer::clear(std::uint32_t argb, float depth) {
    auto& r = *impl_;
    if (!std::isfinite(depth) || depth < 0 || depth > 1) throw std::runtime_error("Invalid clear depth");
    const auto rgba = color(argb);
    r.context->ClearRenderTargetView(r.rtv.Get(), rgba.data());
    r.context->ClearDepthStencilView(r.dsv.Get(),D3D11_CLEAR_DEPTH|
        (r.targets.at(r.current_target).depth_format==SurfaceDepth::z16?0:D3D11_CLEAR_STENCIL),depth,0);
}
void Renderer::clear_buffers(std::uint32_t flags,std::uint32_t argb,float depth,std::uint8_t stencil,
                             std::span<const std::int32_t> rectangle) {
    auto& r=*impl_;
    if(flags&~0xF3U) throw std::runtime_error("Invalid clear channel flags");
    if(r.targets.at(r.current_target).depth_format==SurfaceDepth::z16) flags&=~2U;
    if(!std::isfinite(depth) || depth<0 || depth>1) throw std::runtime_error("Invalid clear depth");
    if(!rectangle.empty() && rectangle.size()!=4) throw std::runtime_error("A clear rectangle requires four coordinates");
    const D3D11_RECT bounds=rectangle.empty()?D3D11_RECT{0,0,LONG(r.width),LONG(r.height)}:
        D3D11_RECT{std::clamp<LONG>(rectangle[0],0,r.width),std::clamp<LONG>(rectangle[1],0,r.height),
                   std::clamp<LONG>(rectangle[2],0,r.width),std::clamp<LONG>(rectangle[3],0,r.height)};
    if(!flags || bounds.left>=bounds.right || bounds.top>=bounds.bottom) return;
    if(bounds.left || bounds.top || bounds.right!=LONG(r.width) || bounds.bottom!=LONG(r.height) ||
        ((flags&0xF0U)!=0 && (flags&0xF0U)!=0xF0U)) {
        // One precompiled pass replaces only the requested channels and pixels.
        // Its state is independent of the preceding game's blend/depth/scissor.
        const std::array<Float4,2> constants{color(argb),Float4{depth,0,0,0}};
        D3D11_MAPPED_SUBRESOURCE mapped{};
        checked(r.context->Map(r.clear_constants.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Clear constants");
        std::memcpy(mapped.pData,constants.data(),sizeof(constants));r.context->Unmap(r.clear_constants.Get(),0);
        r.unbind_textures();auto target=r.rtv.Get();r.context->OMSetRenderTargets(1,&target,r.dsv.Get());
        const D3D11_VIEWPORT viewport{0,0,float(r.width),float(r.height),0,1};
        r.context->RSSetViewports(1,&viewport);r.context->RSSetScissorRects(1,&bounds);r.context->RSSetState(r.clear_raster.Get());
        r.context->OMSetBlendState(r.clear_blends[(flags>>4)&15].Get(),nullptr,UINT_MAX);
        r.context->OMSetDepthStencilState(r.clear_depths[flags&3].Get(),stencil);
        r.context->IASetInputLayout(nullptr);r.context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        r.context->VSSetShader(r.clear_vs.Get(),nullptr,0);r.context->PSSetShader(r.clear_ps.Get(),nullptr,0);
        auto buffer=r.clear_constants.Get();r.context->VSSetConstantBuffers(0,1,&buffer);r.context->PSSetConstantBuffers(0,1,&buffer);
        r.context->Draw(3,0);return;
    }
    if(flags&0xF0U) {const auto rgba=color(argb);r.context->ClearRenderTargetView(r.rtv.Get(),rgba.data());}
    const auto mask=((flags&1)?D3D11_CLEAR_DEPTH:0)|((flags&2)?D3D11_CLEAR_STENCIL:0);
    if(mask) r.context->ClearDepthStencilView(r.dsv.Get(),mask,depth,stencil);
}
void Renderer::begin(std::uint32_t argb, float depth, bool clear_target) {
    auto& r = *impl_;
    if (r.recording) throw std::runtime_error("Frame already begun");
    if(clear_target) clear(argb, depth);
    r.stats.draws = r.stats.vertices = r.stats.vertex_bytes = 0; r.stats.gpu_ms = 0; r.stats.gpu_timing_valid = false;
    r.collect_timings();r.timing_index=static_cast<unsigned>(r.timings.size());++r.frame_id;
    for(unsigned index=0;index<r.timings.size();++index) if(!r.timings[index].pending) {
        r.timing_index=index;auto& timing=r.timings[index];timing.frame=r.frame_id;
        r.context->Begin(timing.timer.Get());r.context->End(timing.start.Get());break;
    }
    r.recording = true; r.ended = false;
    auto target = r.rtv.Get();
    r.context->OMSetRenderTargets(1, &target, r.dsv.Get());
    const D3D11_VIEWPORT viewport{0, 0, float(r.width), float(r.height), 0, 1};
    r.context->RSSetViewports(1, &viewport);
}
void Renderer::clear_stencil(std::uint8_t value) {
    if(impl_->targets.at(impl_->current_target).depth_format!=SurfaceDepth::z16)
        impl_->context->ClearDepthStencilView(impl_->dsv.Get(),D3D11_CLEAR_STENCIL,1,value);
}
void Renderer::draw(const RenderState& state,std::span<const Vertex> vertices,std::span<const std::uint32_t> indices) {
    impl_->draw(state,std::as_bytes(vertices),vertices.size(),indices);
}
void Renderer::draw_program(const RenderState& state,std::uint32_t program,const VertexConstants& constants,
                            const ProgramView& view,std::span<const ProgramVertex> vertices,std::span<const std::uint32_t> indices) {
    auto& r=*impl_;
    if(!program || program>r.programs.size() || r.programs[program-1].launch) throw std::runtime_error("Vertex program was not prepared");
    if(!std::isfinite(view.depth_scale) || view.depth_scale<=0) throw std::runtime_error("Invalid vertex depth scale");
    unsigned mode=0;
    if(state.fog_enable) switch(state.fog_mode) {
    case 0x2601:case 0x0804:mode=0;break;
    case 0x0800:case 0x0802:mode=1;break;
    case 0x0801:case 0x0803:mode=2;break;
    default:throw std::runtime_error(std::format("Unsupported NV2A fog mode 0x{:X}",state.fog_mode));
    }
    const bool absolute=state.fog_mode==0x0802 || state.fog_mode==0x0803 || state.fog_mode==0x0804;
    ProgramUniforms uniforms{constants,{2.0f/r.width,2.0f/r.height,1/view.depth_scale,view.subpixel_bias},
        {state.fog_parameters[0],state.fog_parameters[1],float(mode),state.fog_enable?(absolute?2.0f:1.0f):0.0f},view.texture_scales};
    r.draw(state,std::as_bytes(vertices),vertices.size(),indices,&r.programs[program-1],&uniforms);
}
void Renderer::draw_streams(const RenderState& state,std::uint32_t layout,const VertexConstants& constants,const ProgramView& view,
                            std::span<const VertexStream> streams,const ProgramVertex& current,std::uint32_t count,
                            std::span<const std::uint32_t> indices) {
    auto& r=*impl_;
    if(!layout || layout>r.stream_layouts.size() || streams.size()>16) throw std::runtime_error("Native vertex streams were not prepared");
    if(!std::isfinite(view.depth_scale) || view.depth_scale<=0) throw std::runtime_error("Invalid vertex depth scale");
    const auto& prepared=r.stream_layouts[layout-1];
    unsigned mode=0;
    if(state.fog_enable) switch(state.fog_mode) {
    case 0x2601:case 0x0804:mode=0;break;case 0x0800:case 0x0802:mode=1;break;case 0x0801:case 0x0803:mode=2;break;
    default:throw std::runtime_error(std::format("Unsupported NV2A fog mode 0x{:X}",state.fog_mode));
    }
    const bool absolute=state.fog_mode==0x0802 || state.fog_mode==0x0803 || state.fog_mode==0x0804;
    ProgramUniforms uniforms{constants,{2.0f/r.width,2.0f/r.height,1/view.depth_scale,view.subpixel_bias},
        {state.fog_parameters[0],state.fog_parameters[1],float(mode),state.fog_enable?(absolute?2.0f:1.0f):0.0f},view.texture_scales};
    for(unsigned i=0;i<16;++i) uniforms.input_formats[i]=prepared.attributes[i].stream==16?0:prepared.attributes[i].format;
    const Impl::StreamDraw source{prepared,streams,current};
    r.draw(state,{},count,indices,&r.programs[prepared.program-1],&uniforms,&source);
}
void Renderer::Impl::draw(const RenderState& state,Bytes vertex_bytes,std::size_t vertex_count,
                          std::span<const std::uint32_t> input_indices,const Program* program,const ProgramUniforms* uniforms,const StreamDraw* streams) {
    auto& r = *this;
    if (!r.recording) throw std::runtime_error("Begin a frame before drawing");
    if (!vertex_count || vertex_count > Impl::max_vertices || input_indices.size() > Impl::max_vertices)
        throw std::runtime_error("Draw exceeds bounded vertex/index buffers");
    const auto found = r.pipelines.find(pipeline_key(state));
    if (found == r.pipelines.end()) throw std::runtime_error("Pipeline was not prepared before submission");
    const auto& pipeline = found->second;
    for (const auto index : input_indices) if (index >= vertex_count) throw std::runtime_error("Vertex index outside draw");
    const auto count = input_indices.empty() ? vertex_count : input_indices.size();
    D3D11_PRIMITIVE_TOPOLOGY topology;
    auto indices = input_indices;
    r.converted.clear();
    const auto index = [&](std::size_t n) { return input_indices.empty() ? static_cast<std::uint32_t>(n) : input_indices[n]; };
    const auto triangle = [&](std::size_t a, std::size_t b, std::size_t c) {
        r.converted.push_back(index(a)); r.converted.push_back(index(b)); r.converted.push_back(index(c));
    };
    switch (state.primitive) {
    case 1: topology = D3D11_PRIMITIVE_TOPOLOGY_POINTLIST; break;
    case 2: if (count % 2) throw std::runtime_error("Incomplete line list"); topology = D3D11_PRIMITIVE_TOPOLOGY_LINELIST; break;
    case 3:
        if (count < 2) throw std::runtime_error("Incomplete line loop");
        topology = D3D11_PRIMITIVE_TOPOLOGY_LINESTRIP;
        for (std::size_t n = 0; n < count; ++n) r.converted.push_back(index(n));
        r.converted.push_back(index(0)); indices = r.converted; break;
    case 4: topology = D3D11_PRIMITIVE_TOPOLOGY_LINESTRIP; break;
    case 5: if (count % 3) throw std::runtime_error("Incomplete triangle list"); topology = D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST; break;
    case 6: topology = D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP; break;
    case 7: case 10:
        topology = D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST;
        if (count > Impl::max_vertices || count < 3) throw std::runtime_error("Triangle fan size outside limit");
        for (std::size_t n = 2; n < count; ++n) triangle(0, n - 1, n);
        indices = r.converted; break;
    case 8:
        if (count % 4) throw std::runtime_error("Incomplete quad list");
        topology = D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST;
        for (std::size_t n = 0; n < count; n += 4) { triangle(n, n + 1, n + 2); triangle(n, n + 2, n + 3); }
        indices = r.converted; break;
    case 9:
        if (count % 2 || count < 4) throw std::runtime_error("Incomplete quad strip");
        topology = D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST;
        for (std::size_t n = 2; n < count; n += 2) { triangle(n - 2, n - 1, n + 1); triangle(n - 2, n + 1, n); }
        indices = r.converted; break;
    default: throw std::runtime_error("Unsupported NV097 primitive: " + hex32(state.primitive));
    }
    if (indices.size() > Impl::max_indices) throw std::runtime_error("Converted indices exceed buffer limit");
    const auto upload = [&](ID3D11Buffer* buffer, const void* data, std::size_t size) {
        D3D11_MAPPED_SUBRESOURCE mapped{};
        checked(r.context->Map(buffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped), "Buffer map");
        std::memcpy(mapped.pData, data, size); r.context->Unmap(buffer, 0);
    };
    Constants constants{};
    for (unsigned i = 0; i < 8; ++i) {
        constants.factor0[i] = color(state.factor0[i]); constants.factor1[i] = color(state.factor1[i]);
        constants.combiners[i]={state.color_inputs[i],state.alpha_inputs[i],state.color_outputs[i],state.alpha_outputs[i]};
    }
    constants.final0 = color(state.final_factors[0]); constants.final1 = color(state.final_factors[1]);
    constants.fog = color(state.fog_color); constants.alpha[0] = float(state.alpha_reference & 255);
    constants.controls={state.combiner_control,state.final_inputs[0],state.final_inputs[1],state.alpha_test?state.alpha_function:0x207};
    std::array<ID3D11ShaderResourceView*, 12> views{};
    std::array<ID3D11SamplerState*, 4> samplers{};
    for (unsigned i = 0; i < 4; ++i) {
        const auto mode=state.texture_modes[i];
        const bool sampled=mode && mode!=4 && mode!=5;
        if (sampled && (!state.textures[i] || state.textures[i] >= r.textures.size()))
            throw std::runtime_error("Enabled texture is not uploaded");
        if(sampled && state.textures[i]==r.current_target)
            throw std::runtime_error("Copy the active render target before sampling its contents");
        if(sampled && r.texture_dimensions[state.textures[i]]!=(mode==3?3U:mode==2?2U:1U))
            throw std::runtime_error("Texture resource dimension does not match its shader mode");
        if(sampled) views[i+(mode==3?4:mode==2?8:0)] = r.textures[state.textures[i]].Get();
        constants.stages[i]={mode,state.texture_sources[i],state.texture_clip[i],std::uint32_t(state.alpha_kill[i])};
        samplers[i] = pipeline.samplers[i].Get();
    }
    std::array<UINT,17> stream_offsets{},stream_strides{};
    if(streams) {
        std::array<unsigned,16> required{},fetched{};
        for(const auto& attribute:streams->layout.attributes) if(attribute.stream<16) {
            if(attribute.stream>=streams->streams.size()) throw std::runtime_error("Missing vertex attribute stream");
            const auto format=attribute_format(attribute.format);
            required[attribute.stream]=std::max(required[attribute.stream],attribute.offset+format.bytes);
            fetched[attribute.stream]=std::max(fetched[attribute.stream],attribute.offset+format.fetched);
        }
        std::size_t total=sizeof(ProgramVertex);
        for(unsigned i=0;i<streams->streams.size();++i) if(required[i]) {
            const auto& source=streams->streams[i];
            if(source.stride>2048 || (source.stride && required[i]>source.stride) ||
                source.bytes.size()<(vertex_count-1)*source.stride+required[i]) throw std::runtime_error("Vertex stream stride or storage is too small");
            total=(total+15)&~std::size_t(15);stream_offsets[i]=static_cast<UINT>(total);
            stream_strides[i]=source.stride?std::max(source.stride,fetched[i]):0;
            total+=source.stride?vertex_count*stream_strides[i]:fetched[i];
        }
        if(total>std::size_t(max_vertices)*sizeof(ProgramVertex)) throw std::runtime_error("Native vertex streams exceed upload storage");
        r.stats.vertex_bytes+=total;
        D3D11_MAPPED_SUBRESOURCE mapped{};
        checked(r.context->Map(r.vertices.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Vertex stream map");
        auto* destination=static_cast<std::byte*>(mapped.pData);
        std::memcpy(destination,&streams->current,sizeof(ProgramVertex));
        for(unsigned i=0;i<streams->streams.size();++i) if(required[i]) {
            const auto& source=streams->streams[i];auto* output=destination+stream_offsets[i];
            const auto stride=stream_strides[i];
            const auto size=source.stride?vertex_count*stride:fetched[i];
            const auto copied=std::min(size,source.bytes.size());
            if(!source.stride || stride==source.stride) {
                std::memcpy(output,source.bytes.data(),copied);if(copied<size) std::memset(output+copied,0,size-copied);
            } else for(std::size_t n=0;n<vertex_count;++n) {
                const auto bytes=std::min<std::size_t>(source.stride,source.bytes.size()-n*source.stride);
                std::memcpy(output+n*stride,source.bytes.data()+n*source.stride,bytes);
                std::memset(output+n*stride+bytes,0,stride-bytes);
            }
        }
        r.context->Unmap(r.vertices.Get(),0);
    } else {upload(r.vertices.Get(), vertex_bytes.data(), vertex_bytes.size());r.stats.vertex_bytes+=vertex_bytes.size();}
    upload(r.factor_buffer.Get(), &constants, sizeof(constants));
    if(uniforms) {
        auto values=*uniforms;
        if(program->fixed) {
            const auto& fixed=program->fixed_key;
            values.fixed_flags={std::uint32_t(fixed.normalize),std::uint32_t(fixed.fog),fixed.fog_source,0};values.fixed_texgen=fixed.texgen;
            for(unsigned i=0;i<4;++i) values.fixed_matrices[i]=fixed.texture_matrix[i];
        }
        upload(r.program_constants.Get(),&values,sizeof(values));
    }
    const UINT stride = static_cast<UINT>(program?sizeof(ProgramVertex):sizeof(Vertex)), offset = 0;
    auto vertex_buffer = r.vertices.Get(); auto constant_buffer = r.factor_buffer.Get();
    if(streams) {
        std::array<ID3D11Buffer*,17> buffers{};buffers.fill(vertex_buffer);
        r.context->IASetVertexBuffers(0,17,buffers.data(),stream_strides.data(),stream_offsets.data());
    } else r.context->IASetVertexBuffers(0, 1, &vertex_buffer, &stride, &offset);
    r.context->IASetInputLayout(streams?streams->layout.native.Get():program?program->layout.Get():r.layout.Get()); r.context->IASetPrimitiveTopology(topology);
    const auto simple=simple_combiner(state);
    r.context->VSSetShader(program?program->vertex.Get():r.vs.Get(), nullptr, 0);
    r.context->PSSetShader(simple<r.simple_combiners.size()?r.simple_combiners[simple].Get():r.combiners[state.combiner_control&255].Get(),nullptr,0);
    auto program_buffer=r.program_constants.Get(); r.context->VSSetConstantBuffers(1,1,&program_buffer);
    r.context->PSSetConstantBuffers(0, 1, &constant_buffer);
    r.context->PSSetShaderResources(0, static_cast<UINT>(views.size()), views.data()); r.context->PSSetSamplers(0, 4, samplers.data());
    r.context->OMSetBlendState(pipeline.blend.Get(), nullptr, ~0U);
    r.context->OMSetDepthStencilState(pipeline.depth.Get(), state.stencil_reference); r.context->RSSetState(pipeline.raster.Get());
    if(state.scissor_test) {
        const D3D11_RECT rect{state.scissor[0],state.scissor[1],state.scissor[2],state.scissor[3]};
        if(rect.right<rect.left || rect.bottom<rect.top) throw std::runtime_error("Invalid scissor rectangle");
        r.context->RSSetScissorRects(1,&rect);
    }
    if (indices.empty()) r.context->Draw(static_cast<UINT>(vertex_count), 0);
    else {
        upload(r.index_buffer.Get(), indices.data(), indices.size_bytes());
        r.context->IASetIndexBuffer(r.index_buffer.Get(), DXGI_FORMAT_R32_UINT, 0);
        r.context->DrawIndexed(static_cast<UINT>(indices.size()), 0, 0);
    }
    ++r.stats.draws; r.stats.vertices += count;
}
void Renderer::end() {
    auto& r = *impl_;
    if (!r.recording) throw std::runtime_error("No frame to end");
    if(r.timing_index<r.timings.size()) {
        auto& timing=r.timings[r.timing_index];r.context->End(timing.finish.Get());r.context->End(timing.timer.Get());timing.pending=true;
    }
    r.recording = false; r.ended = true;
}
Image Renderer::readback() {
    auto& r = *impl_;
    if (!r.ended || r.recording) throw std::runtime_error("End a frame before reading back");
    Image image{r.width, r.height, std::vector<std::byte>(std::size_t(r.width) * r.height * 4)};
    r.context->CopyResource(r.staging.Get(), r.target.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    checked(r.context->Map(r.staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Frame readback");
    const bool rgb565=r.targets.at(r.current_target).color_format==SurfaceColor::rgb565;
    for(unsigned y=0;y<r.height;++y) {
        const auto* row=static_cast<const std::byte*>(mapped.pData)+std::size_t(y)*mapped.RowPitch;
        auto* pixels=image.rgba.data()+std::size_t(y)*r.width*4;
        if(!rgb565) std::memcpy(pixels,row,std::size_t(r.width)*4);
        else for(unsigned x=0;x<r.width;++x) {
            std::uint16_t value;std::memcpy(&value,row+x*2,2);
            pixels[x*4]=std::byte((((value>>11)&31)*255+15)/31);
            pixels[x*4+1]=std::byte((((value>>5)&63)*255+31)/63);
            pixels[x*4+2]=std::byte(((value&31)*255+15)/31);pixels[x*4+3]=std::byte{255};
        }
    }
    r.context->Unmap(r.staging.Get(), 0);
    r.collect_timings();
    return image;
}
Image Renderer::read_display() {
    auto& r=*impl_;
    if(!r.displaying || !r.swapchain) throw std::runtime_error("Display a frame before reading the window image");
    ComPtr<ID3D11Texture2D> buffer,staging;
    checked(r.swapchain->GetBuffer(0,IID_PPV_ARGS(&buffer)),"Window image");
    D3D11_TEXTURE2D_DESC description{};buffer->GetDesc(&description);
    description.Usage=D3D11_USAGE_STAGING;description.BindFlags=0;description.MiscFlags=0;description.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    checked(r.device->CreateTexture2D(&description,nullptr,&staging),"Window readback storage");
    r.context->CopyResource(staging.Get(),buffer.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};checked(r.context->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped),"Window readback");
    Image image{description.Width,description.Height,std::vector<std::byte>(std::size_t(description.Width)*description.Height*4)};
    for(unsigned y=0;y<image.height;++y) std::memcpy(image.rgba.data()+std::size_t(y)*image.width*4,
        static_cast<const std::byte*>(mapped.pData)+std::size_t(y)*mapped.RowPitch,std::size_t(image.width)*4);
    r.context->Unmap(staging.Get(),0);return image;
}
RenderStats Renderer::stats() const { return impl_->stats; }
std::string Renderer::adapter() const { return impl_->adapter; }
std::vector<std::string> Renderer::errors() const {
    std::vector<std::string> messages;
    const auto& queue = impl_->info;
    if (!queue) return messages;
    for (UINT64 i = 0; i < queue->GetNumStoredMessagesAllowedByRetrievalFilter(); ++i) {
        SIZE_T size = 0;
        checked(queue->GetMessage(i, nullptr, &size), "Debug message size");
        std::vector<std::byte> storage(size);
        const auto message = reinterpret_cast<D3D11_MESSAGE*>(storage.data());
        checked(queue->GetMessage(i, message, &size), "Debug message");
        if (message->Severity <= D3D11_MESSAGE_SEVERITY_WARNING)
            messages.emplace_back(message->pDescription, message->DescriptionByteLength - 1);
    }
    return messages;
}
}
