#include "render.h"
#include "shaders.h"
#include "frame_processing.h"
#include "antialiasing.h"
#include "ambient_occlusion.h"
#ifdef _WIN32
#include <Windows.h>
#include <wincodec.h>
#else
#include <SDL3/SDL.h>
#include <png.h>
#include <jpeglib.h>
#include <csetjmp>
#include <cstdlib>
#ifdef __APPLE__
#include <SDL3/SDL_metal.h>
#else
#include <wsi/native_wsi.h>
#endif
#endif
#include <d3d11.h>
#include <d3d11_1.h>
#include <d3d11sdklayers.h>
#include <dxgi1_2.h>
#include <dxgi1_5.h>
#include "com_ptr.h"
#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>
#include <format>
#include <fstream>
#include <map>
#include <optional>
#include <chrono>
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
// The game's font atlases contain white RGB with varying alpha. Recognize that
// immutable pixel property once at upload, rather than guessing from draw order.
bool white_mask_mip(const TextureData& texture,std::span<const std::byte> bytes,unsigned pitch,unsigned width,unsigned height,
                    unsigned& first_alpha,bool& varying_alpha) {
    const auto alpha=[&](unsigned value) {
        if(first_alpha==UINT_MAX) first_alpha=value;
        else varying_alpha|=value!=first_alpha;
    };
    if(texture.format==TextureFormat::argb8 || texture.format==TextureFormat::argb8_linear) {
        for(unsigned y=0;y<height;++y) for(unsigned x=0;x<width;++x) {
            const auto* pixel=bytes.data()+std::size_t(y)*pitch+x*4;
            if(pixel[0]!=std::byte{255} || pixel[1]!=std::byte{255} || pixel[2]!=std::byte{255}) return false;
            if(!varying_alpha) alpha(unsigned(pixel[3])*35);
        }
    } else {
        for(std::size_t offset=0;offset<bytes.size();offset+=16) {
            const auto* block=bytes.data()+offset;std::uint16_t a,b;std::uint32_t colours;
            std::memcpy(&a,block+8,2);std::memcpy(&b,block+10,2);std::memcpy(&colours,block+12,4);
            if(!((a==0xFFFF && b==0xFFFF) || (a==0xFFFF && colours==0) || (b==0xFFFF && colours==0x55555555))) return false;
            if(varying_alpha) continue;
            if(texture.format==TextureFormat::dxt3) {
                std::uint64_t values;std::memcpy(&values,block,8);
                for(unsigned i=0;i<16;++i) alpha(unsigned((values>>(i*4))&15)*17*35);
            } else {
                const auto low=unsigned(block[0]),high=unsigned(block[1]);
                std::uint64_t values=0;std::memcpy(&values,block+2,6);
                for(unsigned i=0;i<16;++i) {
                    const auto index=unsigned((values>>(i*3))&7);
                    // A common denominator keeps BC3 interpolation comparisons exact.
                    alpha(index<2?(index?high:low)*35:low>high?((8-index)*low+(index-1)*high)*5:
                          index<6?((6-index)*low+(index-1)*high)*7:index==6?0:255*35);
                }
            }
        }
    }
    return true;
}
#ifdef _WIN32
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
#endif
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
using PipelineKey = std::array<std::uint32_t, 23>;
PipelineKey pipeline_key(const RenderState& state,bool multisample,bool responsive=false) {
    PipelineKey key{};
    std::size_t i = 0;
    for (auto value : {std::uint32_t(state.blend), std::uint32_t(state.depth_test), std::uint32_t(state.depth_write),
         std::uint32_t(state.cull && state.primitive!=1), std::uint32_t(state.front_ccw),
         state.depth_function, state.blend_source, state.blend_destination, state.blend_equation,
         state.cull_face, state.color_mask}) key[i++] = value;
    for(auto value : {std::uint32_t(state.stencil_test),state.stencil_function,state.stencil_read_mask,state.stencil_write_mask,
        state.stencil_fail,state.stencil_depth_fail,state.stencil_pass,std::uint32_t(state.polygon_offset),
        std::bit_cast<std::uint32_t>(state.polygon_offset_scale),std::bit_cast<std::uint32_t>(state.polygon_offset_bias),std::uint32_t(state.scissor_test)}) key[i++]=value;
    key[i]=std::uint32_t(multisample)|(std::uint32_t(responsive)<<1);return key;
}
using SamplerKey=std::array<std::uint32_t,6>;
SamplerKey sampler_key(const RenderState& state,unsigned stage,unsigned anisotropy) {
    const auto address=state.textures[stage]?state.texture_address[stage]:0x030303;
    const auto filter=state.textures[stage]?state.texture_filter[stage]:0x01030000;
    const auto control=state.textures[stage]?state.texture_control[stage]:0x0003FFC0;
    const auto min=(filter>>16)&63,mag=(filter>>24)&15;
    if(min<1 || min>6 || mag<1 || mag>2) throw std::runtime_error("Unsupported NV097 texture filter");
    const bool min_linear=(min&1)==0,mip_linear=min>=5,mip_enabled=min>=3;
    auto strength=anisotropy?anisotropy:1U<<((control>>4)&3);
    auto native_filter=(min_linear?0x10U:0U)|(mag==2?4U:0U)|(mip_linear?1U:0U);
    if(strength>1 && min_linear && mag==2) native_filter=D3D11_FILTER_ANISOTROPIC;
    else strength=1;
    auto bias=int(filter&0x1FFF);if(bias&0x1000) bias-=0x2000;
    // Ignore register fields that do not affect the native descriptor.
    return {address&0x0F0F0F,native_filter,mip_enabled?std::uint32_t(bias):0U,
            mip_enabled?(control>>18)&0xFFF:0U,mip_enabled?(control>>6)&0xFFF:0U,strength};
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
    std::array<Float4,2> point_parameters{};
};
bool same_uniforms(const ProgramUniforms& a,const ProgramUniforms& b,unsigned first,unsigned count) {
    static_assert(offsetof(ProgramUniforms,viewport)==sizeof(VertexConstants));
    return (!count || !std::memcmp(a.constants.data()+first,b.constants.data()+first,count*sizeof(Float4))) &&
        !std::memcmp(&a.viewport,&b.viewport,sizeof(ProgramUniforms)-sizeof(VertexConstants));
}
UINT uniform_bytes(unsigned count) {return count*sizeof(Float4)+sizeof(ProgramUniforms)-sizeof(VertexConstants);}
UINT uniform_capacity(unsigned count) {return (uniform_bytes(count)+255)&~255U;}
void write_uniforms(void* output,const ProgramUniforms& values,unsigned first,unsigned count) {
    auto* bytes=static_cast<std::byte*>(output);const auto constants=count*sizeof(Float4);
    if(count) std::memcpy(bytes,values.constants.data()+first,constants);
    std::memcpy(bytes+constants,&values.viewport,sizeof(ProgramUniforms)-sizeof(VertexConstants));
}
Constants fragment_constants(const RenderState& state) {
    Constants values{};
    for(unsigned i=0;i<8;++i) {
        values.factor0[i]=color(state.factor0[i]);values.factor1[i]=color(state.factor1[i]);
        values.combiners[i]={state.color_inputs[i],state.alpha_inputs[i],state.color_outputs[i],state.alpha_outputs[i]};
    }
    values.final0=color(state.final_factors[0]);values.final1=color(state.final_factors[1]);
    values.fog=color(state.fog_color);values.alpha[0]=float(state.alpha_reference&255);
    values.controls={state.combiner_control,state.final_inputs[0],state.final_inputs[1],state.alpha_test?state.alpha_function:0x207};
    for(unsigned i=0;i<4;++i) values.stages[i]={state.texture_modes[i],state.texture_sources[i],state.texture_clip[i],
        std::uint32_t(state.alpha_kill[i])|(std::uint32_t(state.texture_opaque[i])<<1)};
    return values;
}
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
#ifdef _WIN32
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
#else
    png_image png{};png.version=PNG_IMAGE_VERSION;png.width=image.width;png.height=image.height;png.format=PNG_FORMAT_RGBA;
    png_alloc_size_t size=0;
    if(!png_image_write_to_memory(&png,nullptr,&size,0,image.rgba.data(),0,nullptr) || size>128*1024*1024)
        throw std::runtime_error("Cannot size PNG output");
    std::string bytes(size,0);
    if(!png_image_write_to_memory(&png,bytes.data(),&size,0,image.rgba.data(),0,nullptr)) throw std::runtime_error(png.message);
    bytes.resize(size);write_text(path,bytes,replace);
#endif
}
Image load_image(const std::filesystem::path& path) {
#ifdef _WIN32
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
#else
    File input(path);const auto magic=input.read(0,std::min<std::uint64_t>(8,input.size()));
    if(magic.size()==8 && !png_sig_cmp(reinterpret_cast<const unsigned char*>(magic.data()),0,8)) {
        png_image png{};png.version=PNG_IMAGE_VERSION;
        if(!png_image_begin_read_from_file(&png,path.c_str())) throw std::runtime_error(png.message);
        try {
            extent(png.width,png.height);png.format=PNG_FORMAT_RGBA;
            Image result{png.width,png.height,std::vector<std::byte>(PNG_IMAGE_SIZE(png))};
            if(!png_image_finish_read(&png,nullptr,result.rgba.data(),0,nullptr)) throw std::runtime_error(png.message);
            png_image_free(&png);return result;
        } catch(...) {png_image_free(&png);throw;}
    }
    // libjpeg's error callback must not unwind through C. Keep C++ allocations
    // outside the setjmp scope and copy the bounded decoded pixels afterwards.
    struct JpegError {jpeg_error_mgr base;jmp_buf jump;char message[JMSG_LENGTH_MAX];};
    auto error=std::make_unique<JpegError>();auto decoder=std::make_unique<jpeg_decompress_struct>();
    decoder->err=jpeg_std_error(&error->base);
    error->base.error_exit=[](j_common_ptr value) {auto* e=reinterpret_cast<JpegError*>(value->err);value->err->format_message(value,e->message);longjmp(e->jump,1);};
    FILE* file=std::fopen(path.c_str(),"rb");if(!file) throw std::runtime_error("Cannot open image");
    unsigned char* volatile pixels=nullptr;
    if(setjmp(error->jump)) {std::free(pixels);jpeg_destroy_decompress(decoder.get());std::fclose(file);throw std::runtime_error(error->message);}
    jpeg_create_decompress(decoder.get());jpeg_stdio_src(decoder.get(),file);jpeg_read_header(decoder.get(),TRUE);
    if(!decoder->image_width || !decoder->image_height || decoder->image_width>4096 || decoder->image_height>4096) {
        jpeg_destroy_decompress(decoder.get());std::fclose(file);throw std::runtime_error("Image dimensions must be 1..4096");
    }
    decoder->out_color_space=JCS_EXT_RGBA;jpeg_start_decompress(decoder.get());
    const auto width=decoder->output_width,height=decoder->output_height;
    pixels=static_cast<unsigned char*>(std::malloc(std::size_t(width)*height*4));
    if(!pixels) {jpeg_destroy_decompress(decoder.get());std::fclose(file);throw std::bad_alloc();}
    while(decoder->output_scanline<height) {auto* row=pixels+std::size_t(decoder->output_scanline)*width*4;jpeg_read_scanlines(decoder.get(),&row,1);}
    jpeg_finish_decompress(decoder.get());jpeg_destroy_decompress(decoder.get());std::fclose(file);
    Image result{width,height,{}};
    try {result.rgba.assign(reinterpret_cast<std::byte*>(pixels),reinterpret_cast<std::byte*>(pixels)+std::size_t(width)*height*4);}
    catch(...) {std::free(pixels);throw;}
    std::free(pixels);return result;
#endif
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
                       double(sum) / (double(a.width) * a.height * 3), json(path_text(difference)));
}

namespace {
// Queries are allocated only for an explicit recording. Each frame sums
// disjoint command spans, so CPU/pacing idle between batches is not GPU work.
class GpuMeter {
    struct Span {ComPtr<ID3D11Query> first,last;unsigned kind=0;};
    struct Frame {ComPtr<ID3D11Query> timer;std::array<Span,64> spans;std::uint64_t id=0;unsigned used=0,dropped=0;bool pending=false;};
    std::array<Frame,8> frames_;
    ID3D11DeviceContext* context_;
    Performance& profile_;
    int current_=-1;
    unsigned depth_=0,open_=0,missing_=0;
    std::uint64_t id_=0;
public:
    GpuMeter(ID3D11Device* device,ID3D11DeviceContext* context,Performance& profile,std::uint64_t id):context_(context),profile_(profile) {
        for(auto& frame:frames_) {
            D3D11_QUERY_DESC query{D3D11_QUERY_TIMESTAMP_DISJOINT,0};checked(device->CreateQuery(&query,&frame.timer),"Performance GPU clock");query.Query=D3D11_QUERY_TIMESTAMP;
            for(auto& span:frame.spans) {checked(device->CreateQuery(&query,&span.first),"Performance GPU start");checked(device->CreateQuery(&query,&span.last),"Performance GPU end");}
        }
        start(id);
    }
    ~GpuMeter(){if(current_>=0) context_->End(frames_[current_].timer.Get());}
    void start(std::uint64_t id) {
        id_=id;current_=-1;missing_=0;
        for(unsigned i=0;i<frames_.size();++i) if(!frames_[i].pending) {
            current_=int(i);auto& frame=frames_[i];frame.id=id;frame.used=frame.dropped=0;context_->Begin(frame.timer.Get());break;
        }
    }
    void collect() {
        for(auto& frame:frames_) if(frame.pending) {
            D3D11_QUERY_DATA_TIMESTAMP_DISJOINT clock{};
            const auto status=context_->GetData(frame.timer.Get(),&clock,sizeof(clock),D3D11_ASYNC_GETDATA_DONOTFLUSH);checked(status,"Performance GPU clock read");if(status!=S_OK) continue;
            std::array<std::uint64_t,3> ns{};bool ready=true,valid=!clock.Disjoint && clock.Frequency;
            for(const auto& span:std::span(frame.spans).first(frame.used)) {
                UINT64 first=0,last=0;
                const auto a=context_->GetData(span.first.Get(),&first,sizeof(first),D3D11_ASYNC_GETDATA_DONOTFLUSH),b=context_->GetData(span.last.Get(),&last,sizeof(last),D3D11_ASYNC_GETDATA_DONOTFLUSH);
                checked(a,"Performance GPU start read");checked(b,"Performance GPU end read");
                if(a!=S_OK || b!=S_OK) {ready=false;break;}
                if(last<first) valid=false;
                if(valid) ns[span.kind]+=((last-first)/clock.Frequency)*1000000000ULL+((last-first)%clock.Frequency)*1000000000ULL/clock.Frequency;
            }
            if(!ready) continue;
            profile_.gpu_frame(frame.id,ns,frame.used,frame.dropped,valid);frame.pending=false;
        }
    }
    void boundary(std::uint64_t next) {
        if(depth_) throw std::runtime_error("Performance GPU span crossed a frame boundary");
        if(current_>=0) {auto& frame=frames_[current_];context_->End(frame.timer.Get());frame.pending=true;}
        else profile_.gpu_frame(id_,{},0,std::max(1U,missing_),false);
        {PerfScope overhead(&profile_,PerfPhase::diagnostics);collect();}
        start(next);
    }
    void begin(unsigned kind) {
        if(depth_++) return;open_=64;
        if(current_<0) {++missing_;return;}auto& frame=frames_[current_];
        if(frame.used==frame.spans.size()) {++frame.dropped;return;}
        open_=frame.used++;auto& span=frame.spans[open_];span.kind=kind;context_->End(span.first.Get());
    }
    void end() {if(--depth_ || current_<0 || open_==64) return;context_->End(frames_[current_].spans[open_].last.Get());}
};
struct GpuSpan {
    GpuMeter* meter;
    GpuSpan(GpuMeter* value,unsigned kind):meter(value) {if(meter) meter->begin(kind);}
    ~GpuSpan(){if(meter) meter->end();}
};
}
struct Renderer::Impl {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<ID3D11DeviceContext1> constant_context;
    ComPtr<IDXGIAdapter3> memory_adapter;
    Performance* profile=nullptr;
    std::unique_ptr<GpuMeter> gpu_meter;
    void count(PerfMetric metric,std::uint64_t amount=1) {if(profile) stats.counters[unsigned(metric)]+=amount;}
    std::array<ComPtr<ID3D11PixelShader>,4> simple_combiners;
    ComPtr<ID3D11Texture2D> target, depth, staging;
    ComPtr<ID3D11RenderTargetView> rtv;
    ComPtr<ID3D11DepthStencilView> dsv;
    ComPtr<ID3D11VertexShader> vs;
    ComPtr<ID3D11GeometryShader> point_geometry;
    ComPtr<ID3D11Buffer> point_constants;
    Float4 last_point_info{};
    bool point_info_valid=false;
    std::array<ComPtr<ID3D11PixelShader>,9> combiners;
    std::array<ComPtr<ID3D11VertexShader>,7> fixed_vertices;
    std::array<Bytes,7> fixed_signatures;
    std::array<ComPtr<ID3D11InputLayout>,7> fixed_layouts;
    ComPtr<ID3D11InputLayout> layout;
    ComPtr<ID3D11Buffer> vertices, index_buffer, factor_buffer, program_constants;
    UINT vertex_cursor=0,index_cursor=0;
    static constexpr UINT constant_capacity=65536;
    UINT factor_cursor=0,program_cursor=0,factor_offset=0,program_offset=0,program_count=16;
    Constants last_factors{};ProgramUniforms last_program{};
    std::uint32_t last_program_id=0;
    bool factors_valid=false,program_valid=false;
    struct DrawBindings {
        ID3D11InputLayout* layout=nullptr;
        D3D11_PRIMITIVE_TOPOLOGY topology=D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;
        ID3D11VertexShader* vertex=nullptr;ID3D11PixelShader* pixel=nullptr;
        ID3D11GeometryShader* geometry=nullptr;
        ID3D11BlendState* blend=nullptr;ID3D11DepthStencilState* depth=nullptr;ID3D11RasterizerState* raster=nullptr;
        std::array<ID3D11ShaderResourceView*,12> views{};
        std::array<ID3D11SamplerState*,4> samplers{};
        UINT stencil=0;D3D11_RECT scissor{};
        UINT factor_offset=0,program_offset=0,program_count=0;
        bool valid=false,scissor_enabled=false,layers=false,temporal=false;
    } bound;
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
        ComPtr<ID3D11Texture2D> color,depth,image,staging;
        ComPtr<ID3D11RenderTargetView> color_view;
        ComPtr<ID3D11DepthStencilView> depth_view;
        ComPtr<ID3D11ShaderResourceView> texture_view,multisample_view,depth_texture,stencil_texture;
        ComPtr<ID3D11Texture2D> responsive;
        ComPtr<ID3D11RenderTargetView> responsive_target;
        ComPtr<ID3D11ShaderResourceView> responsive_view;
        bool dirty=false;
        float uniform_alpha=-1; // Known across all pixels/samples; negative means unknown.
    };
    std::map<std::uint32_t,FrameTarget> targets;
    std::uint32_t current_target=0;
    std::uint32_t presentation_target=0;
    std::uint32_t feedback_target=0;
    const Visibility* visibility=nullptr;
    void visibility_scope(bool original) const {if(visibility) visibility->scope(visibility->context,original);}
    std::size_t target_bytes=0;
    HWND window=nullptr;
#ifdef __APPLE__
    SDL_MetalView metal_view=nullptr;
#endif
    ComPtr<IDXGISwapChain2> swapchain;
    HANDLE frame_ready=nullptr;
    bool frame_acquired=false,allow_tearing=false;
    ComPtr<ID3D11RenderTargetView> window_view;
    ComPtr<ID3D11VertexShader> display_vs;
    ComPtr<ID3D11PixelShader> display_ps;
    ComPtr<ID3D11SamplerState> display_sampler;
    ComPtr<ID3D11RasterizerState> display_raster;
    ComPtr<ID3D11DepthStencilState> display_depth;
    std::unique_ptr<FrameLayers> layers;
    std::array<ComPtr<ID3D11PixelShader>,9> layer_combiners;
    std::array<ComPtr<ID3D11PixelShader>,4> layer_simple;
    bool layer_scene=false;
    std::uint32_t layer_target=UINT32_MAX;
    std::unique_ptr<FrameAntialiasing> antialiasing;
    std::unique_ptr<FrameAmbientOcclusion> ambient_occlusion;
    unsigned ao_quality=0;
    float ao_strength=0.35f,camera_aspect=1;
    bool ao_scene_only=false,ao_scene_depth=false;
    std::unique_ptr<FrameUpscaling> upscaler;
    UpscaleMethod upscale_method=UpscaleMethod::off;
    unsigned upscale_output_scale=0;
    UpscaleFrame upscale_frame;
    std::uint32_t camera_identifier=0;
    MotionCamera motion_camera;
    bool upscaled=false,camera_valid=false,camera_reset=true;
    std::chrono::steady_clock::time_point upscale_time{};
    std::array<float,2> jitter{};
    std::array<float,2> applied_jitter{};
    std::array<ComPtr<ID3D11PixelShader>,9> responsive_combiners;
    std::array<ComPtr<ID3D11PixelShader>,4> responsive_simple;
    ComPtr<ID3D11VertexShader> jitter_vertex;
    ComPtr<ID3D11Buffer> temporal_constants;
    std::array<Float4,2> last_temporal{};
    bool temporal_valid=false;
    std::unique_ptr<MultisampleCopy> multisample_copy;
    unsigned antialiasing_quality=0,anisotropy=0,samples=1,msaa_supported=0;
    ComPtr<ID3D11VertexShader> clear_vs;
    ComPtr<ID3D11PixelShader> clear_ps;
    ComPtr<ID3D11Buffer> clear_constants;
    ComPtr<ID3D11RasterizerState> clear_raster;
    std::array<ComPtr<ID3D11BlendState>,16> clear_blends;
    std::array<ComPtr<ID3D11DepthStencilState>,4> clear_depths;
    std::uint32_t window_width=0,window_height=0;
    bool displaying=false;
    float display_aspect=0;
    struct Pipeline {
        ComPtr<ID3D11BlendState> blend,layer_blend;
        ComPtr<ID3D11DepthStencilState> depth;
        ComPtr<ID3D11RasterizerState> raster;
    };
    std::map<PipelineKey, Pipeline> pipelines;
    ComPtr<ID3D11SamplerState> default_sampler;
    std::map<SamplerKey,ComPtr<ID3D11SamplerState>> sampler_states;
    // Leave room under D3D11's device limit for 256 queued four-stage draws
    // retaining evicted samplers, plus display and postprocessing samplers.
    std::array<SamplerKey,1024> sampler_order{};
    unsigned sampler_next=0;
    PipelineKey prepared_pipeline_key{};
    const Pipeline* prepared_pipeline=nullptr;
    std::array<SamplerKey,4> prepared_sampler_keys{};
    std::array<ID3D11SamplerState*,4> prepared_samplers{};
    struct Program {
        VertexProgram key;
        bool launch;
        ComPtr<ID3D11VertexShader> vertex;
        ComPtr<ID3D11VertexShader> jitter_vertex;
        ComPtr<ID3D11ComputeShader> compute;
        ComPtr<ID3D11InputLayout> layout;
        Bytes signature;
        std::uint32_t input_mask=0;
        bool fixed=false,screen_space=false;
        FixedTransform fixed_key{};
        std::uint32_t constant_first=0,constant_count=192;
        std::uint32_t upload_first=0,upload_count=192;
    };
    std::vector<Program> programs;
    struct StreamLayout {
        std::uint32_t program;VertexLayout attributes;ComPtr<ID3D11InputLayout> native;
        std::array<unsigned,16> required{},fetched{};
    };
    std::vector<StreamLayout> stream_layouts;
    struct StreamUpload {
        std::array<UINT,17> offsets{},strides{};
        std::array<UINT,16> sizes{};
        UINT bytes=sizeof(ProgramVertex);
    };
    struct IndexPlan {D3D11_PRIMITIVE_TOPOLOGY topology=D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;UINT count=0,offset=0;};
    struct StreamBatchDraw {
        StreamUpload vertices;
        IndexPlan indices;
        Constants factors;
        ProgramUniforms uniforms;
        UINT factor_offset=0,program_offset=0;
        bool factor_changed=false,program_changed=false;
        UINT reused_streams=0,reused_bytes=0;
        std::uint32_t program_id=0;
    };
    std::array<StreamBatchDraw,256> stream_uploads{};
    struct StreamDraw {
        const StreamLayout& layout;std::span<const VertexStream> streams;const ProgramVertex& current;
        const StreamUpload* uploaded=nullptr;
        const StreamBatchDraw* batch=nullptr;
    };
    StreamUpload stream_storage(std::uint32_t layout,std::span<const VertexStream> streams,std::uint32_t count) const {
        if(!layout || layout>stream_layouts.size() || streams.size()>16 || !count || count>max_vertices)
            throw std::runtime_error("Native vertex streams exceed their bounds");
        const auto& required=stream_layouts[layout-1].required;
        const auto& fetched=stream_layouts[layout-1].fetched;
        StreamUpload upload;
        for(unsigned i=0;i<required.size();++i) if(required[i]) {
            if(i>=streams.size()) throw std::runtime_error("Missing vertex attribute stream");
            const auto& source=streams[i];
            if(source.stride>2048 || (source.stride && required[i]>source.stride) ||
                source.bytes.size()<std::size_t(count-1)*source.stride+required[i])
                throw std::runtime_error("Vertex stream stride or storage is too small");
            upload.bytes=(upload.bytes+15)&~15U;upload.offsets[i]=upload.bytes;
            upload.strides[i]=source.stride?std::max(source.stride,fetched[i]):0;
            upload.sizes[i]=source.stride?count*upload.strides[i]:fetched[i];upload.bytes+=upload.sizes[i];
        }
        if(upload.bytes>max_vertices*sizeof(ProgramVertex)) throw std::runtime_error("Native vertex streams exceed upload storage");
        return upload;
    }
    static void fill_streams(std::byte* destination,const StreamUpload& upload,std::span<const VertexStream> streams,
                             const ProgramVertex& current,std::uint32_t count,UINT reused=0) {
        if(!(reused&(1U<<16))) std::memcpy(destination+upload.offsets[16],&current,sizeof(current));
        for(unsigned i=0;i<streams.size();++i) if(upload.sizes[i] && !(reused&(1U<<i))) {
            const auto& source=streams[i];auto* output=destination+upload.offsets[i];
            const auto stride=upload.strides[i],size=upload.sizes[i];
            const auto copied=std::min<std::size_t>(size,source.bytes.size());
            if(!source.stride || stride==source.stride) {
                std::memcpy(output,source.bytes.data(),copied);if(copied<size) std::memset(output+copied,0,size-copied);
            } else for(std::size_t n=0;n<count;++n) {
                const auto bytes=std::min<std::size_t>(source.stride,source.bytes.size()-n*source.stride);
                std::memcpy(output+n*stride,source.bytes.data()+n*source.stride,bytes);
                std::memset(output+n*stride+bytes,0,stride-bytes);
            }
        }
    }
    struct Texture {ComPtr<ID3D11ShaderResourceView> view;unsigned dimension=0;std::size_t bytes=0;bool white_mask=false;};
    std::vector<Texture> textures{1};
    std::vector<std::uint32_t> free_textures;
    bool texture_capacity() const {return !free_textures.empty() || textures.size()<4096;}
    std::uint32_t retain_texture(ComPtr<ID3D11ShaderResourceView> view,unsigned dimension,std::size_t bytes) {
        if(!texture_capacity()) throw std::runtime_error("Texture resource limit reached");
        std::uint32_t id;
        if(free_textures.empty()) {id=static_cast<std::uint32_t>(textures.size());textures.emplace_back();}
        else {id=free_textures.back();free_textures.pop_back();}
        textures[id]={std::move(view),dimension,bytes};texture_bytes+=bytes;return id;
    }
    std::vector<std::uint32_t> converted;
    std::string adapter;
    std::uint32_t width, height, resolution_scale;
    std::size_t target_limit=1024ULL*1024*1024;
    std::size_t target_size(unsigned w,unsigned h) const {return std::size_t(w)*h*resolution_scale*resolution_scale*(samples>1?8*samples+8:12);}
    std::size_t target_usage() const {
        auto bytes=target_bytes+(antialiasing?antialiasing->bytes():0)+(layers?layers->bytes():0)+(ambient_occlusion?ambient_occlusion->bytes():0);
        for(const auto& [id,target]:targets) if(target.responsive) bytes+=std::size_t(target.width)*target.height*resolution_scale*resolution_scale;
        return bytes;
    }
    void reset_layers() {
        unbind_textures();layers.reset();layer_scene=false;layer_target=UINT32_MAX;motion_camera={};ao_scene_only=ao_scene_depth=false;
    }
    std::unique_ptr<FrameLayers> make_layers(bool readable=false) {
        const auto& main=targets.at(0);
        auto result=std::make_unique<FrameLayers>(device.Get(),context.Get(),main.width*resolution_scale,main.height*resolution_scale,samples,stats.shader_compilations,readable);
        if(result->bytes()>target_limit-target_usage()) throw std::runtime_error("Presentation layers exceed the render-target budget.");
        for(unsigned i=0;i<layer_combiners.size();++i) if(!layer_combiners[i]) {
            const auto pixel=shader(ShaderKind(unsigned(ShaderKind::layer_combiner0)+i));
            checked(device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&layer_combiners[i]),"UI layer combiner shader");
        }
        for(unsigned i=0;i<layer_simple.size();++i) if(!layer_simple[i]) {
            const auto pixel=shader(ShaderKind(unsigned(ShaderKind::layer_color)+i));
            checked(device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&layer_simple[i]),"UI layer simple shader");
        }
        return result;
    }
    Float4 program_viewport(const ProgramView& view,bool edge_aligned=false) const {
        // Guest screen coordinates already include a half-pixel origin. Scale
        // geometry around that origin, then add the host's half physical pixel.
        const auto bias=view.subpixel_bias+(edge_aligned?0:0.5f*(1.0f-1.0f/resolution_scale));
        return {2.0f/width,2.0f/height,1/view.depth_scale,bias};
    }
    bool edge_aligned(const Program& program,const VertexConstants& constants,std::span<const Float4> positions) const {
        if(resolution_scale==1 || !program.screen_space || positions.size()!=4 || constants[0][0]!=1 || constants[0][1]!=1 ||
           constants[1][0]!=0.53125f || constants[1][1]!=0.53125f) return false;
        // D3D8 rectangles with the half-pixel adjustment already in their
        // vertices describe pixel edges. Applying the center conversion again
        // moves framebuffer copies and leaves the last row/column uncovered.
        float left=positions[0][0],right=left,top=positions[0][1],bottom=top;
        for(const auto& p:positions) {
            for(unsigned lane=0;lane<2;++lane) if(!std::isfinite(p[lane]) || std::abs(p[lane])>16777215 ||
                std::trunc(p[lane]+0.5f)!=p[lane]+0.5f) return false;
            left=std::min(left,p[0]);right=std::max(right,p[0]);top=std::min(top,p[1]);bottom=std::max(bottom,p[1]);
        }
        if(left==right || top==bottom) return false;
        unsigned corners=0;
        for(const auto& p:positions) {
            if((p[0]!=left && p[0]!=right) || (p[1]!=top && p[1]!=bottom)) return false;
            corners|=1U<<((p[0]==right?1:0)+(p[1]==bottom?2:0));
        }
        return corners==15;
    }
    RenderStats stats;
    std::size_t texture_bytes = 0;
    bool recording = false, ended = false;
    static constexpr UINT max_vertices = 32768, max_indices = max_vertices * 6;
    static IndexPlan plan_indices(unsigned primitive,UINT vertices,std::span<const std::uint32_t> input) {
        if(!vertices || vertices>max_vertices || input.size()>max_vertices) throw std::runtime_error("Draw exceeds bounded vertex/index buffers");
        for(const auto index:input) if(index>=vertices) throw std::runtime_error("Vertex index outside draw");
        const auto count=input.empty()?vertices:static_cast<UINT>(input.size());
        IndexPlan plan;plan.count=static_cast<UINT>(input.size());
        switch(primitive) {
        case 1:plan.topology=D3D11_PRIMITIVE_TOPOLOGY_POINTLIST;break;
        case 2:if(count%2) throw std::runtime_error("Incomplete line list");plan.topology=D3D11_PRIMITIVE_TOPOLOGY_LINELIST;break;
        case 3:if(count<2) throw std::runtime_error("Incomplete line loop");plan.topology=D3D11_PRIMITIVE_TOPOLOGY_LINESTRIP;plan.count=count+1;break;
        case 4:plan.topology=D3D11_PRIMITIVE_TOPOLOGY_LINESTRIP;break;
        case 5:if(count%3) throw std::runtime_error("Incomplete triangle list");plan.topology=D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST;break;
        case 6:plan.topology=D3D11_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP;break;
        case 7:case 10:
            if(count<3) throw std::runtime_error("Triangle fan size outside limit");
            plan.topology=D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST;plan.count=(count-2)*3;break;
        case 8:
            if(count%4) throw std::runtime_error("Incomplete quad list");
            plan.topology=D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST;plan.count=count/4*6;break;
        case 9:
            if(count%2 || count<4) throw std::runtime_error("Incomplete quad strip");
            plan.topology=D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST;plan.count=(count/2-1)*6;break;
        default:throw std::runtime_error("Unsupported NV097 primitive: "+hex32(primitive));
        }
        return plan;
    }
    static void fill_indices(std::byte* destination,unsigned primitive,UINT vertices,std::span<const std::uint32_t> input) {
        const auto count=input.empty()?vertices:static_cast<UINT>(input.size());
        if(primitive!=3 && primitive!=7 && primitive!=8 && primitive!=9 && primitive!=10) {
            std::memcpy(destination,input.data(),input.size_bytes());return;
        }
        const auto index=[&](unsigned n){return input.empty()?n:input[n];};
        const auto append=[&](unsigned n){const auto value=index(n);std::memcpy(destination,&value,4);destination+=4;};
        const auto triangle=[&](unsigned a,unsigned b,unsigned c){append(a);append(b);append(c);};
        switch(primitive) {
        case 3:for(unsigned n=0;n<count;++n) append(n);append(0);break;
        case 7:case 10:for(unsigned n=2;n<count;++n) triangle(0,n-1,n);break;
        case 8:for(unsigned n=0;n<count;n+=4){triangle(n,n+1,n+2);triangle(n,n+2,n+3);}break;
        case 9:for(unsigned n=2;n<count;n+=2){triangle(n-2,n-1,n+1);triangle(n-2,n+1,n);}break;
        }
    }
    std::byte* append_buffer(ID3D11Buffer* buffer,UINT capacity,UINT& cursor,std::size_t size,UINT& offset,UINT alignment=16) {
        if(!size || size>capacity) throw std::runtime_error("Upload exceeds dynamic buffer storage");
        offset=(cursor+alignment-1)&~(alignment-1);
        if(size>capacity-offset) offset=0;
        D3D11_MAPPED_SUBRESOURCE mapped{};
        // Append only to bytes no earlier draw can read. DISCARD on wrapping
        // lets D3D11 retain the previous allocation until the GPU finishes it.
        checked(context->Map(buffer,0,offset?D3D11_MAP_WRITE_NO_OVERWRITE:D3D11_MAP_WRITE_DISCARD,0,&mapped),"Dynamic buffer append");
        cursor=offset+static_cast<UINT>(size);
        count(PerfMetric::buffer_maps);count(PerfMetric::buffer_bytes,size);
        return static_cast<std::byte*>(mapped.pData)+offset;
    }
    Bytes shader(ShaderKind kind,const VertexProgram* program=nullptr,ShaderBinary* metadata=nullptr) {
        const auto result=shader_binary(kind,program);stats.shader_compilations+=result.compiled;count(PerfMetric::shader_compiles,result.compiled);
        if(result.constant_first>192 || result.constant_count>192-result.constant_first) throw std::runtime_error("Invalid static shader constant range");
        if(metadata) *metadata=result;return result.bytes;
    }
    Impl(std::uint32_t w, std::uint32_t h, bool warp, bool debug,unsigned scale) : width(w), height(h),resolution_scale(scale) {
#if defined(__APPLE__)
        if(warp) throw std::runtime_error("WARP is Windows-only; macOS diagnostics use a Metal device.");
        if(debug) throw std::runtime_error("The D3D11 debug layer is Windows-only; use Metal validation on macOS.");
        setenv("DXMT_LOG_PATH","none",0);setenv("DXMT_LOG_LEVEL","error",0);
#elif !defined(_WIN32)
        if(warp) throw std::runtime_error("WARP is Windows-only; select a Vulkan software ICD for Linux offscreen diagnostics.");
        if(debug) throw std::runtime_error("The D3D11 debug layer is Windows-only; use Vulkan validation on Linux.");
        setenv("DXVK_WSI_DRIVER","SDL3",1);
#endif
        if(!scale || scale>4) throw std::runtime_error("Internal resolution scale must be between 1 and 4");
        extent(w, h);
        const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_0};
        D3D_FEATURE_LEVEL level;
        const UINT flags = D3D11_CREATE_DEVICE_SINGLETHREADED | (debug ? D3D11_CREATE_DEVICE_DEBUG : 0);
        checked(D3D11CreateDevice(nullptr, warp ? D3D_DRIVER_TYPE_WARP : D3D_DRIVER_TYPE_HARDWARE,
                                  nullptr, flags, levels, 1, D3D11_SDK_VERSION, &device, &level, &context), "D3D11 device");
        D3D11_SAMPLER_DESC sampler{};sampler.Filter=D3D11_FILTER_MIN_MAG_MIP_POINT;
        sampler.AddressU=sampler.AddressV=sampler.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;
        sampler.MaxAnisotropy=1;sampler.MaxLOD=4095;sampler.ComparisonFunc=D3D11_COMPARISON_NEVER;
        checked(device->CreateSamplerState(&sampler,&default_sampler),"Default texture sampler");
        for(unsigned count:{2U,4U,8U}) {
            bool supported=true;
            for(auto format:{DXGI_FORMAT_R8G8B8A8_UNORM,DXGI_FORMAT_B5G6R5_UNORM,DXGI_FORMAT_D16_UNORM,DXGI_FORMAT_D24_UNORM_S8_UINT}) {
                UINT quality=0;supported&=SUCCEEDED(device->CheckMultisampleQualityLevels(format,count,&quality)) && quality!=0;
            }
            for(auto format:{DXGI_FORMAT_R8G8B8A8_UNORM,DXGI_FORMAT_B5G6R5_UNORM,DXGI_FORMAT_R16_UNORM,DXGI_FORMAT_R24_UNORM_X8_TYPELESS,DXGI_FORMAT_X24_TYPELESS_G8_UINT}) {
                UINT flags=0;supported&=SUCCEEDED(device->CheckFormatSupport(format,&flags)) && (flags&D3D11_FORMAT_SUPPORT_MULTISAMPLE_LOAD)!=0;
                if(format==DXGI_FORMAT_R8G8B8A8_UNORM || format==DXGI_FORMAT_B5G6R5_UNORM) supported&=(flags&D3D11_FORMAT_SUPPORT_MULTISAMPLE_RESOLVE)!=0;
            }
            if(supported) msaa_supported|=count;
        }
        D3D11_FEATURE_DATA_D3D11_OPTIONS options{};
        if(SUCCEEDED(device->CheckFeatureSupport(D3D11_FEATURE_D3D11_OPTIONS,&options,sizeof(options))) &&
            options.ConstantBufferOffsetting && options.MapNoOverwriteOnDynamicConstantBuffer) context.As(&constant_context);
        ComPtr<IDXGIDevice> dxgi;
        checked(device.As(&dxgi), "DXGI device");
        ComPtr<IDXGIAdapter> gpu;
        checked(dxgi->GetAdapter(&gpu), "DXGI adapter");
        gpu.As(&memory_adapter);
        DXGI_ADAPTER_DESC description{};
        checked(gpu->GetDesc(&description), "Adapter description");
        if(memory_adapter) {
            // Native-resolution motion and layer histories grow with the image.
            // Bound them by the adapter's actual budget, with headroom for
            // textures and driver storage, rather than rejecting a viable scale.
            DXGI_QUERY_VIDEO_MEMORY_INFO memory{};
            if(SUCCEEDED(memory_adapter->QueryVideoMemoryInfo(0,DXGI_MEMORY_SEGMENT_GROUP_LOCAL,&memory)))
                target_limit=std::clamp<std::size_t>(memory.Budget/4*3,1024ULL*1024*1024,2048ULL*1024*1024);
        }
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
        const auto points=shader(ShaderKind::point_geometry);
        checked(device->CreateGeometryShader(points.data(),points.size(),nullptr,&point_geometry),"Point geometry shader");
        const auto buffer = [&](UINT bytes, UINT bind, ComPtr<ID3D11Buffer>& output) {
            D3D11_BUFFER_DESC desc{};
            desc.ByteWidth = bytes; desc.Usage = D3D11_USAGE_DYNAMIC;
            desc.BindFlags = bind; desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
            checked(device->CreateBuffer(&desc, nullptr, &output), "Dynamic buffer");
        };
        buffer(max_vertices * sizeof(ProgramVertex), D3D11_BIND_VERTEX_BUFFER, vertices);
        buffer(max_indices * sizeof(std::uint32_t), D3D11_BIND_INDEX_BUFFER, index_buffer);
        buffer(constant_context?constant_capacity:sizeof(Constants),D3D11_BIND_CONSTANT_BUFFER,factor_buffer);
        buffer(constant_context?constant_capacity:sizeof(ProgramUniforms),D3D11_BIND_CONSTANT_BUFFER,program_constants);
        buffer(32,D3D11_BIND_CONSTANT_BUFFER,clear_constants);
        buffer(16,D3D11_BIND_CONSTANT_BUFFER,point_constants);
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
        clear_desc.DepthClipEnable=clear_desc.ScissorEnable=clear_desc.MultisampleEnable=TRUE;
        checked(device->CreateRasterizerState(&clear_desc,&clear_raster),"Clear rectangle state");
    }
    ~Impl() {
#ifdef _WIN32
        if(frame_ready) CloseHandle(frame_ready);
#elif defined(__APPLE__)
        // Release the swapchain's retained layer before its SDL view.
        swapchain.Reset();if(metal_view) SDL_Metal_DestroyView(metal_view);
#endif
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
              const Program* = nullptr, const ProgramUniforms* = nullptr,const StreamDraw* = nullptr,const PreparedState* = nullptr);
    void draw_streams(const RenderState&,std::uint32_t,const VertexConstants&,const ProgramView&,
                      std::span<const VertexStream>,const ProgramVertex&,std::uint32_t,
                      std::span<const std::uint32_t>,const PreparedState*,const StreamBatchDraw* = nullptr);
    void stream_uniforms(ProgramUniforms&,const RenderState&,std::uint32_t,const VertexConstants&,const ProgramView&,
                         std::span<const VertexStream>,std::uint32_t,bool same_program=false) const;
    void make_responsive(FrameTarget& output) {
        D3D11_TEXTURE2D_DESC image{};image.Width=output.width*resolution_scale;image.Height=output.height*resolution_scale;
        image.MipLevels=image.ArraySize=image.SampleDesc.Count=1;image.Format=DXGI_FORMAT_R8_UNORM;
        image.BindFlags=D3D11_BIND_RENDER_TARGET|D3D11_BIND_SHADER_RESOURCE;
        checked(device->CreateTexture2D(&image,nullptr,&output.responsive),"Temporal responsive image");
        checked(device->CreateRenderTargetView(output.responsive.Get(),nullptr,&output.responsive_target),"Temporal responsive target");
        checked(device->CreateShaderResourceView(output.responsive.Get(),nullptr,&output.responsive_view),"Temporal responsive view");
        const float zero[4]={};context->ClearRenderTargetView(output.responsive_target.Get(),zero);
    }
    FrameTarget make_target(std::uint32_t w,std::uint32_t h,SurfaceColor color_format=SurfaceColor::rgba8,SurfaceDepth depth_format=SurfaceDepth::z24s8) {
        extent(w,h);
        FrameTarget output{w,h,color_format,depth_format};
        const auto native_color=color_format==SurfaceColor::rgb565?DXGI_FORMAT_B5G6R5_UNORM:DXGI_FORMAT_R8G8B8A8_UNORM;
        D3D11_TEXTURE2D_DESC texture{};
        texture.Width=w*resolution_scale; texture.Height=h*resolution_scale; texture.MipLevels=texture.ArraySize=1;
        extent(texture.Width,texture.Height);
        texture.Format=native_color; texture.SampleDesc.Count=samples;
        texture.BindFlags=D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        checked(device->CreateTexture2D(&texture,nullptr,&output.color), "Offscreen target");
        checked(device->CreateRenderTargetView(output.color.Get(),nullptr,&output.color_view), "Offscreen view");
        if(samples>1) {
            checked(device->CreateShaderResourceView(output.color.Get(),nullptr,&output.multisample_view),"Multisample colour view");
            texture.SampleDesc.Count=1;checked(device->CreateTexture2D(&texture,nullptr,&output.image),"Resolved target");texture.SampleDesc.Count=samples;
            output.dirty=true;
        } else output.image=output.color;
        checked(device->CreateShaderResourceView(output.image.Get(),nullptr,&output.texture_view),"Target texture view");
        const auto native_depth=depth_format==SurfaceDepth::z16?DXGI_FORMAT_D16_UNORM:DXGI_FORMAT_D24_UNORM_S8_UINT;
        texture.Format=depth_format==SurfaceDepth::z16?DXGI_FORMAT_R16_TYPELESS:DXGI_FORMAT_R24G8_TYPELESS;
        texture.BindFlags=D3D11_BIND_DEPTH_STENCIL|D3D11_BIND_SHADER_RESOURCE;
        checked(device->CreateTexture2D(&texture,nullptr,&output.depth), "Depth target");
        D3D11_DEPTH_STENCIL_VIEW_DESC depth_view{};depth_view.Format=native_depth;
        depth_view.ViewDimension=samples>1?D3D11_DSV_DIMENSION_TEXTURE2DMS:D3D11_DSV_DIMENSION_TEXTURE2D;
        checked(device->CreateDepthStencilView(output.depth.Get(),&depth_view,&output.depth_view), "Depth view");
        {
            D3D11_SHADER_RESOURCE_VIEW_DESC view{};view.ViewDimension=D3D11_SRV_DIMENSION_TEXTURE2DMS;
            if(samples==1) {view.ViewDimension=D3D11_SRV_DIMENSION_TEXTURE2D;view.Texture2D.MipLevels=1;}
            view.Format=depth_format==SurfaceDepth::z16?DXGI_FORMAT_R16_UNORM:DXGI_FORMAT_R24_UNORM_X8_TYPELESS;
            checked(device->CreateShaderResourceView(output.depth.Get(),&view,&output.depth_texture),"Multisample depth view");
            if(samples>1 && depth_format!=SurfaceDepth::z16) {view.Format=DXGI_FORMAT_X24_TYPELESS_G8_UINT;checked(device->CreateShaderResourceView(output.depth.Get(),&view,&output.stencil_texture),"Multisample stencil view");}
        }
        // Readback storage is created only for explicit diagnostics.
        return output;
    }
    void resolve(FrameTarget& output) {
        if(samples==1 || !output.dirty) return;
        unbind_textures();context->OMSetRenderTargets(0,nullptr,nullptr);
        context->ResolveSubresource(output.image.Get(),0,output.color.Get(),0,output.color_format==SurfaceColor::rgb565?DXGI_FORMAT_B5G6R5_UNORM:DXGI_FORMAT_R8G8B8A8_UNORM);
        output.dirty=false;auto color=rtv.Get();context->OMSetRenderTargets(1,&color,dsv.Get());
    }
    void unbind_textures() {
        bound.valid=false;
        std::array<ID3D11ShaderResourceView*,20> empty{};
        context->PSSetShaderResources(0,static_cast<UINT>(empty.size()),empty.data());
    }
    void swap_main_color(std::uint32_t id) {
        unbind_textures();context->OMSetRenderTargets(0,nullptr,nullptr);
        auto& scene=targets.at(id);auto& main=targets.at(0);
        std::swap(scene.color,main.color);std::swap(scene.image,main.image);
        std::swap(scene.staging,main.staging);std::swap(scene.color_view,main.color_view);
        std::swap(scene.texture_view,main.texture_view);std::swap(scene.multisample_view,main.multisample_view);
        std::swap(scene.dirty,main.dirty);std::swap(scene.uniform_alpha,main.uniform_alpha);
        textures[id].view=scene.texture_view;
    }
    void select_target(std::uint32_t id) {
        const auto found=targets.find(id);
        if(found==targets.end()) throw std::runtime_error("Render target was not created");
        unbind_textures();context->OMSetRenderTargets(0,nullptr,nullptr);
        const auto& output=found->second;
        width=output.width;height=output.height;current_target=id;
        target=output.color;depth=output.depth;staging=output.staging;rtv=output.color_view;dsv=output.depth_view;
        const std::array<ID3D11RenderTargetView*,2> colors{rtv.Get(),output.responsive_target.Get()};
        context->OMSetRenderTargets(output.responsive_target?2:1,colors.data(),dsv.Get());
        const D3D11_VIEWPORT viewport{0,0,float(width*resolution_scale),float(height*resolution_scale),0,1};
        context->RSSetViewports(1,&viewport);
    }
    void resize(std::uint32_t w, std::uint32_t h) {
        extent(w,h);
        if (recording || current_target) throw std::runtime_error("Resize the main target outside frame submission");
        if (target && width==w && height==h) return;
        const auto required=target_size(w,h);
        const auto prior=target?target_size(width,height):0;
        const auto aa=antialiasing_quality && antialiasing_quality<=3?required/3:0;
        if(required+aa>target_limit-(target_bytes-prior)) throw std::runtime_error("Render targets exceed the adapter budget");
        auto output=make_target(w,h);
        feedback_target=0;
        targets.insert_or_assign(0,std::move(output));target_bytes=target_bytes-prior+required;
        select_target(0);ended=false;
    }
};
Renderer::Renderer(std::uint32_t width, std::uint32_t height, bool warp, bool debug,unsigned scale)
    : impl_(std::make_unique<Impl>(width, height, warp, debug,scale)) {
    for(const auto& shader:static_vertex_shaders()) prepare_program(shader.program,shader.launch);
}
Renderer::~Renderer() = default;
void Renderer::reset(unsigned scale) {
    auto& r=*impl_;
    if(r.recording || !scale || scale>4) throw std::runtime_error("Reset graphics between game sessions with a scale from 1 to 4");
    const auto width=r.targets.at(0).width,height=r.targets.at(0).height,previous=r.resolution_scale;
    r.resolution_scale=scale;
    Impl::FrameTarget main;
    try {
        if(r.target_size(width,height)>r.target_limit) throw std::runtime_error("Render targets exceed the adapter budget; reduce resolution or MSAA.");
        main=r.make_target(width,height);
    } catch(...) {r.resolution_scale=previous;throw;}
    r.reset_layers();r.antialiasing.reset();r.ambient_occlusion.reset();r.upscaler.reset();r.upscale_method=UpscaleMethod::off;
    r.upscaled=r.camera_valid=false;r.camera_reset=true;r.camera_identifier=0;r.upscale_time={};
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    r.targets.clear();r.textures.resize(1);r.free_textures.clear();r.texture_bytes=0;
    r.presentation_target=r.feedback_target=0;
    r.targets.emplace(0,std::move(main));r.target_bytes=r.target_size(width,height);r.select_target(0);r.ended=false;
    const auto compilations=r.stats.shader_compilations;r.stats={};r.stats.shader_compilations=compilations;
    r.vertex_cursor=r.index_cursor=0;r.factors_valid=r.program_valid=false;
    r.factor_cursor=r.program_cursor=r.factor_offset=r.program_offset=0;
    enhancements(r.antialiasing_quality,r.anisotropy);
    if(r.ao_quality) ambient_occlusion(r.ao_quality,r.ao_strength);
}
void Renderer::resize(std::uint32_t width, std::uint32_t height) {
    auto& r=*impl_;const auto& main=r.targets.at(0);
    const bool changed=main.width!=width || main.height!=height;
    r.resize(width,height);
    if(changed) {
        if(r.upscale_method!=UpscaleMethod::off) configure_upscaling(r.upscale_method);
        else if(r.antialiasing_quality) enhancements(r.antialiasing_quality,r.anisotropy);
        if(r.ao_quality) ambient_occlusion(r.ao_quality,r.ao_strength);
    }
}
std::uint32_t Renderer::create_target(std::uint32_t width,std::uint32_t height,SurfaceColor color,SurfaceDepth depth) {
    auto& r=*impl_;extent(width,height);
    PerfScope timing(r.profile,PerfPhase::resources);r.count(PerfMetric::target_creations);
    if(r.recording) throw std::runtime_error("Create render targets before frame submission");
    const auto required=r.target_size(width,height);
    const bool temporal=r.upscaler && width==r.targets.at(0).width && height==r.targets.at(0).height;
    const auto mask_bytes=temporal?std::size_t(width)*height*r.resolution_scale*r.resolution_scale:0;
    if(!r.texture_capacity() || required+mask_bytes>r.target_limit-r.target_usage())
        throw std::runtime_error("Render target resource limit reached");
    auto output=r.make_target(width,height,color,depth);
    if(temporal) r.make_responsive(output);
    const auto id=r.retain_texture(output.texture_view,1,0);
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
    PerfScope timing(r.profile,PerfPhase::resources);GpuSpan gpu(r.gpu_meter.get(),1);
    const auto found=r.targets.find(target);
    if(!target || found==r.targets.end() || r.recording) throw std::runtime_error("Grow an existing auxiliary target outside submission");
    const auto& prior=found->second;
    if(width<prior.width || height<prior.height) throw std::runtime_error("Target growth cannot shrink either dimension");
    if(width==prior.width && height==prior.height) return;
    const auto previous=r.target_size(prior.width,prior.height),required=r.target_size(width,height);
    if(required>r.target_limit-(r.target_usage()-previous)) throw std::runtime_error("Render targets exceed the adapter budget");
    auto output=r.make_target(width,height,prior.color_format,prior.depth_format);
    if(r.upscaler && width==r.targets.at(0).width && height==r.targets.at(0).height) {r.make_responsive(output);r.camera_reset=true;}
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    constexpr float black[4]{};
    r.context->ClearRenderTargetView(output.color_view.Get(),black);
    r.context->ClearDepthStencilView(output.depth_view.Get(),D3D11_CLEAR_DEPTH|(prior.depth_format==SurfaceDepth::z16?0:D3D11_CLEAR_STENCIL),1,0);
    if(r.samples>1) {
        r.multisample_copy->grow(prior.multisample_view.Get(),prior.depth_texture.Get(),prior.stencil_texture.Get(),
            output.color_view.Get(),output.depth_view.Get(),prior.width*r.resolution_scale,prior.height*r.resolution_scale);
    } else {
    r.context->CopySubresourceRegion(output.color.Get(),0,0,0,0,prior.color.Get(),0,nullptr);
    // Depth/stencil resources require whole copies with matching dimensions.
    // Migrate their packed bits through ordinary typeless textures, then copy
    // the complete enlarged image back into its depth/stencil resource.
    D3D11_TEXTURE2D_DESC packed{};packed.Width=prior.width*r.resolution_scale;packed.Height=prior.height*r.resolution_scale;
    packed.MipLevels=packed.ArraySize=1;packed.Format=prior.depth_format==SurfaceDepth::z16?DXGI_FORMAT_R16_TYPELESS:DXGI_FORMAT_R24G8_TYPELESS;packed.SampleDesc.Count=1;
    ComPtr<ID3D11Texture2D> previous_depth,grown_depth;
    checked(r.device->CreateTexture2D(&packed,nullptr,&previous_depth),"Previous depth transfer");
    packed.Width=width*r.resolution_scale;packed.Height=height*r.resolution_scale;
    checked(r.device->CreateTexture2D(&packed,nullptr,&grown_depth),"Grown depth transfer");
    r.context->CopyResource(previous_depth.Get(),prior.depth.Get());
    r.context->CopyResource(grown_depth.Get(),output.depth.Get());
    r.context->CopySubresourceRegion(grown_depth.Get(),0,0,0,0,previous_depth.Get(),0,nullptr);
    r.context->CopyResource(output.depth.Get(),grown_depth.Get());
    }
    found->second=std::move(output);r.textures[target].view=found->second.texture_view;
    r.target_bytes=r.target_bytes-previous+required;r.select_target(r.current_target);
}
void Renderer::copy_target(std::uint32_t destination) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::resources);GpuSpan gpu(r.gpu_meter.get(),1);r.count(PerfMetric::target_copies);
    const auto found=r.targets.find(destination);
    if(found==r.targets.end() || destination==r.current_target || found->second.width!=r.width || found->second.height!=r.height ||
        found->second.color_format!=r.targets.at(r.current_target).color_format)
        throw std::runtime_error("Target copy requires a distinct target with matching dimensions");
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    r.context->CopyResource(found->second.color.Get(),r.target.Get());
    found->second.uniform_alpha=r.targets.at(r.current_target).uniform_alpha;
    found->second.dirty=true;
    if(!destination && r.antialiasing) r.antialiasing->invalidate();
    auto view=r.rtv.Get();r.context->OMSetRenderTargets(1,&view,r.dsv.Get());
}
std::uint32_t Renderer::copy_cubemap(const std::array<std::uint32_t,6>& faces,std::uint32_t destination) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::resources);GpuSpan gpu(r.gpu_meter.get(),1);r.count(PerfMetric::target_copies,6);
    if(r.recording) throw std::runtime_error("Copy cubemap faces outside frame submission");
    std::array<ID3D11Texture2D*,6> sources{};
    D3D11_TEXTURE2D_DESC description{};
    for(unsigned face=0;face<faces.size();++face) {
        const auto found=r.targets.find(faces[face]);
        if(found==r.targets.end()) throw std::runtime_error("Cubemap face is not a render target");
        r.resolve(found->second);sources[face]=found->second.image.Get();
        D3D11_TEXTURE2D_DESC source{};sources[face]->GetDesc(&source);
        if(!face) description=source;
        if(source.Width!=source.Height || source.Width!=description.Width || source.Format!=description.Format)
            throw std::runtime_error("Cubemap faces require matching square images and formats");
    }
    description.ArraySize=6;description.BindFlags=D3D11_BIND_SHADER_RESOURCE;
    description.MiscFlags=D3D11_RESOURCE_MISC_TEXTURECUBE;
    ComPtr<ID3D11Texture2D> cube;
    if(!destination) {
        const auto required=std::size_t(description.Width)*description.Height*6*(description.Format==DXGI_FORMAT_B5G6R5_UNORM?2:4);
        if(!r.texture_capacity() || required>256*1024*1024-r.texture_bytes)
            throw std::runtime_error("Cubemap texture resource limit reached");
        checked(r.device->CreateTexture2D(&description,nullptr,&cube),"GPU cubemap");
        ComPtr<ID3D11ShaderResourceView> view;
        checked(r.device->CreateShaderResourceView(cube.Get(),nullptr,&view),"GPU cubemap view");
        destination=r.retain_texture(std::move(view),3,required);
    } else {
        if(destination>=r.textures.size() || r.textures[destination].dimension!=3)
            throw std::runtime_error("Cubemap destination was not created");
        ComPtr<ID3D11Resource> resource;r.textures[destination].view->GetResource(&resource);
        checked(resource.As(&cube),"GPU cubemap resource");
        D3D11_TEXTURE2D_DESC existing{};cube->GetDesc(&existing);
        if(existing.Width!=description.Width || existing.Height!=description.Height || existing.Format!=description.Format ||
            existing.MipLevels!=1 || existing.ArraySize!=6 || existing.Usage!=D3D11_USAGE_DEFAULT)
            throw std::runtime_error("Cubemap destination differs from its faces");
    }
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    for(unsigned face=0;face<faces.size();++face)
        r.context->CopySubresourceRegion(cube.Get(),face,0,0,0,sources[face],0,nullptr);
    auto view=r.rtv.Get();r.context->OMSetRenderTargets(1,&view,r.dsv.Get());
    return destination;
}
void Renderer::copy_to_main() {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::resources);GpuSpan gpu(r.gpu_meter.get(),1);r.count(PerfMetric::target_copies);
    if(r.recording || !r.current_target) throw std::runtime_error("Copy an ended auxiliary target to the main image");
    const auto source=r.target;
    const auto identifier=r.current_target;
    const auto format=r.targets.at(identifier).color_format;
    const auto width=r.width,height=r.height;
    const auto& main=r.targets.at(0);
    if(main.width!=width || main.height!=height) throw std::runtime_error("Presentation target dimensions differ");
    r.feedback_target=0;
    r.presentation_target=identifier;
    if(r.layers && r.layer_target!=identifier) {r.layers->invalidate();r.layer_scene=false;r.ao_scene_depth=false;}
    r.select_target(0);
    if(format==SurfaceColor::rgb565) {
        RenderState state;state.texture_modes[0]=1;state.textures[0]=identifier;state.final_inputs={0x00000008,0x00001800};
        prepare(state);begin(0,1,false);
        std::array<Vertex,4> vertices{};
        constexpr std::array<Float4,4> positions{{{-1,1,0,1},{1,1,0,1},{1,-1,0,1},{-1,-1,0,1}}};
        for(unsigned i=0;i<4;++i) {vertices[i].position=positions[i];vertices[i].uv[0]={(i==1 || i==2)?1.0f:0.0f,i>=2?1.0f:0.0f,0,1};}
        constexpr std::array<std::uint32_t,6> indices{0,1,2,0,2,3};draw(state,vertices,indices);end();r.presentation_target=identifier;return;
    }
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    r.context->CopyResource(r.target.Get(),source.Get());
    r.targets.at(0).uniform_alpha=r.targets.at(identifier).uniform_alpha;
    r.targets.at(0).dirty=true;
    if(r.antialiasing) r.antialiasing->invalidate();
    r.ended=true;
}
void Renderer::begin_overlay() {
    auto& r=*impl_;const auto id=r.current_target;
    if(r.recording || !id || r.targets.at(id).color_format!=SurfaceColor::rgba8)
        throw std::runtime_error("Begin overlays on an ended RGBA scene target");
    // Make the existing presentation copy before the HUD. Draw the HUD into
    // that copy, retaining the scene's depth, stencil and responsive mask.
    copy_to_main();r.swap_main_color(id);r.select_target(id);
}
void Renderer::end_overlay() {
    auto& r=*impl_;const auto id=r.current_target;
    if(r.recording || !id || id!=r.presentation_target)
        throw std::runtime_error("End overlays on their original scene target");
    // The original surface becomes clean history; main receives the complete
    // frame. Swapping handles costs no GPU pass, copy or allocation.
    r.swap_main_color(id);r.select_target(0);r.feedback_target=id;r.ended=true;
}
void Renderer::restore_feedback() {
    auto& r=*impl_;
    if(!r.feedback_target) return;
    if(r.recording) throw std::runtime_error("Restore displayed feedback outside frame submission");
    r.swap_main_color(r.feedback_target);r.feedback_target=0;r.select_target(r.current_target);
}
ID3D11Device* Renderer::native_device() const {return impl_->device.Get();}
ID3D11DeviceContext* Renderer::native_context() const {return impl_->context.Get();}
ID3D11Texture2D* Renderer::reconstruction_input(unsigned channel) const {
    if(!impl_->upscaler) throw std::runtime_error("Reconstruction is disabled");
    return impl_->upscaler->input(channel);
}
void Renderer::attach_window(void* native_window) {
    auto& r=*impl_;
    if(r.window || !native_window
#ifdef _WIN32
       || !IsWindow(static_cast<HWND>(native_window))
#else
       || !SDL_GetWindowID(static_cast<SDL_Window*>(native_window))
#endif
    )
        throw std::runtime_error("Attach one valid native window to the renderer");
#ifdef __APPLE__
    r.metal_view=SDL_Metal_CreateView(static_cast<SDL_Window*>(native_window));
    if(!r.metal_view) throw std::runtime_error(SDL_GetError());
    const auto window=static_cast<HWND>(SDL_Metal_GetLayer(r.metal_view));
    if(!window) throw std::runtime_error("SDL Metal layer is missing");
#else
    const auto window=static_cast<HWND>(native_window);
#endif
    ComPtr<IDXGIDevice> device;ComPtr<IDXGIAdapter> adapter;ComPtr<IDXGIFactory2> factory;
    checked(r.device.As(&device),"Presentation device");checked(device->GetAdapter(&adapter),"Presentation adapter");
    checked(adapter->GetParent(IID_PPV_ARGS(&factory)),"Presentation factory");
    ComPtr<IDXGIFactory5> modern;BOOL tearing=FALSE;
    r.allow_tearing=SUCCEEDED(factory.As(&modern)) &&
        SUCCEEDED(modern->CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING,&tearing,sizeof(tearing))) && tearing;
    DXGI_SWAP_CHAIN_DESC1 description{};
    description.Width=r.width;description.Height=r.height;description.Format=DXGI_FORMAT_R8G8B8A8_UNORM;
    description.SampleDesc.Count=1;description.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT;
    description.BufferCount=2;description.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;description.Scaling=DXGI_SCALING_STRETCH;
    description.Flags=(r.allow_tearing?DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING:0);
#ifdef _WIN32
    description.Flags|=DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
#endif
    ComPtr<IDXGISwapChain1> swapchain;
    checked(factory->CreateSwapChainForHwnd(r.device.Get(),window,&description,nullptr,nullptr,&swapchain),"Window swapchain");
    checked(swapchain.As(&r.swapchain),"Window frame latency interface");
#ifdef _WIN32
    checked(r.swapchain->SetMaximumFrameLatency(1),"Window frame latency");
    r.frame_ready=r.swapchain->GetFrameLatencyWaitableObject();
    if(!r.frame_ready) throw std::runtime_error("Window frame availability handle is missing");
#else
    // Native backends pace the queue in Present. Bound it through the device.
    ComPtr<IDXGIDevice1> latency_device;checked(device.As(&latency_device),"Presentation latency device");
    checked(latency_device->SetMaximumFrameLatency(1),"Presentation queue latency");
#endif
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
    r.window=static_cast<HWND>(native_window);
}
void Renderer::aspect_ratio(float aspect) {
    if(!std::isfinite(aspect) || aspect<=0) throw std::runtime_error("Invalid display aspect ratio");
    impl_->display_aspect=aspect;
}
unsigned Renderer::raster_samples_per_pixel() const {return impl_->samples*impl_->resolution_scale*impl_->resolution_scale;}
bool Renderer::supports_msaa(unsigned samples) const {return samples>=2 && samples<=8 && std::has_single_bit(samples) && (impl_->msaa_supported&samples)!=0;}
bool Renderer::supports_upscaling(UpscaleMethod method) const {
    if(method==UpscaleMethod::off) return true;
#ifndef _WIN32
    return false;
#else
    if(method<UpscaleMethod::dlss || method>UpscaleMethod::xess) return false;
    ComPtr<IDXGIDevice> device;ComPtr<IDXGIAdapter> adapter;DXGI_ADAPTER_DESC desc{};
    if(FAILED(impl_->device.As(&device)) || FAILED(device->GetAdapter(&adapter)) || FAILED(adapter->GetDesc(&desc))) return false;
    if(desc.VendorId==0x1414) return false;
    return method!=UpscaleMethod::dlss || desc.VendorId==0x10DE;
#endif
}
void Renderer::upscaling(UpscaleMethod method,unsigned output_scale) {
    auto& r=*impl_;
    if(r.recording || r.targets.size()!=1 || r.texture_bytes) throw std::runtime_error("Change upscaling between game sessions.");
    if(output_scale>8 || (output_scale && output_scale<r.resolution_scale)) throw std::runtime_error("Invalid upscaling output resolution");
    r.upscale_output_scale=output_scale?output_scale:r.resolution_scale*2;
    configure_upscaling(method);
}
void Renderer::configure_upscaling(UpscaleMethod method) {
    auto& r=*impl_;
    if(r.recording) throw std::runtime_error("Configure upscaling outside frame submission.");
    if(!supports_upscaling(method)) throw std::runtime_error("This upscaling technique is unavailable on your graphics device.");
    r.reset_layers();
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    r.upscaler.reset();r.upscale_method=UpscaleMethod::off;r.upscaled=false;
    auto& main=r.targets.at(0);
    for(auto& [id,target]:r.targets) {target.responsive.Reset();target.responsive_target.Reset();target.responsive_view.Reset();}
    r.camera_valid=false;r.camera_identifier=0;r.camera_reset=true;r.upscale_time={};r.applied_jitter={};r.temporal_valid=false;
    try { if(method!=UpscaleMethod::off) {
        if(r.samples!=1 || r.antialiasing) throw std::runtime_error("Upscaling supplies antialiasing; disable separate antialiasing before enabling it.");
        const auto width=main.width*r.resolution_scale,height=main.height*r.resolution_scale;
        auto next=std::make_unique<FrameUpscaling>(r.device.Get(),r.context.Get(),method,width,height,
            main.width*r.upscale_output_scale,main.height*r.upscale_output_scale);
        next->prepare_frames(r.stats.shader_compilations);
        for(auto& [id,target]:r.targets) if(target.width==main.width && target.height==main.height) r.make_responsive(target);
        if(r.target_usage()>r.target_limit) throw std::runtime_error("Temporal masks exceed the render-target budget.");
        if(!r.temporal_constants) {
            D3D11_BUFFER_DESC buffer{32,D3D11_USAGE_DYNAMIC,D3D11_BIND_CONSTANT_BUFFER,D3D11_CPU_ACCESS_WRITE,0,0};
            checked(r.device->CreateBuffer(&buffer,nullptr,&r.temporal_constants),"Temporal draw constants");
            const auto vs=r.shader(ShaderKind::passthrough_jitter);
            checked(r.device->CreateVertexShader(vs.data(),vs.size(),nullptr,&r.jitter_vertex),"Jittered vertex shader");
            for(unsigned i=0;i<r.responsive_combiners.size();++i) {
                const auto pixel=r.shader(ShaderKind(unsigned(ShaderKind::responsive_combiner0)+i));
                checked(r.device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&r.responsive_combiners[i]),"Responsive combiner shader");
            }
            for(unsigned i=0;i<r.responsive_simple.size();++i) {
                const auto pixel=r.shader(ShaderKind(unsigned(ShaderKind::responsive_color)+i));
                checked(r.device->CreatePixelShader(pixel.data(),pixel.size(),nullptr,&r.responsive_simple[i]),"Responsive simple shader");
            }
        }
        for(auto& program:r.programs) if(!program.launch && !program.screen_space && !program.jitter_vertex) {
            const auto binary=program.fixed?shader_binary(ShaderKind(unsigned(ShaderKind::fixed_jitter0)+program.fixed_key.skin)):
                shader_binary(ShaderKind::vertex_jitter,&program.key);
            r.stats.shader_compilations+=binary.compiled;
            checked(r.device->CreateVertexShader(binary.bytes.data(),binary.bytes.size(),nullptr,&program.jitter_vertex),"Jittered program shader");
        }
        r.upscaler=std::move(next);r.upscale_method=method;r.jitter={0,-1.0f/6};
    } } catch(...) {
        for(auto& [id,target]:r.targets) {target.responsive.Reset();target.responsive_target.Reset();target.responsive_view.Reset();}
        r.temporal_constants.Reset();r.jitter_vertex.Reset();
        for(auto& shader:r.responsive_combiners) shader.Reset();
        for(auto& shader:r.responsive_simple) shader.Reset();
        r.select_target(0);throw;
    }
    r.select_target(0);if(r.ambient_occlusion) r.layers=r.make_layers(true);
}
void Renderer::camera(std::uint32_t identifier,float near_plane,float far_plane,float vertical_fov,const MotionCamera* motion,float aspect) {
    auto& r=*impl_;if(!r.upscaler && !r.ambient_occlusion) return;
    r.motion_camera={};r.camera_valid=false;
    if(!(near_plane>0) || !(far_plane>near_plane) || !std::isfinite(far_plane) || !(vertical_fov>0 && vertical_fov<3.141593f)) return;
    r.camera_reset|=r.camera_identifier!=identifier;
    r.camera_identifier=identifier;r.camera_valid=true;
    r.motion_camera=motion?*motion:MotionCamera{};
    if(!motion) {r.motion_camera.identifier=identifier;r.motion_camera.near_plane=near_plane;r.motion_camera.far_plane=far_plane;r.motion_camera.projection=1;}
    const auto& main=r.targets.at(0);
    r.camera_aspect=aspect>0 && std::isfinite(aspect)?aspect:r.display_aspect>0?r.display_aspect:float(main.width)/main.height;
    r.upscale_frame.camera_near=near_plane;r.upscale_frame.camera_far=far_plane;r.upscale_frame.vertical_fov=vertical_fov;
}
void Renderer::enhancements(unsigned quality,unsigned anisotropy) {
    auto& r=*impl_;
    if(r.recording || quality>6 || anisotropy>16 || (anisotropy && !std::has_single_bit(anisotropy)))
        throw std::runtime_error("Configure valid graphics enhancements outside submission.");
    const auto samples=quality>=4?1U<<(quality-3):1U;
    if(samples>1 && !supports_msaa(samples)) throw std::runtime_error(std::format("MSAA {}x is unavailable on this graphics device.",samples));
    if(samples>1 && !r.multisample_copy) r.multisample_copy=std::make_unique<MultisampleCopy>(r.device.Get(),r.context.Get(),r.stats.shader_compilations);
    if(samples!=r.samples) {
        if(r.targets.size()!=1 || r.texture_bytes) throw std::runtime_error("Change MSAA between game sessions.");
        const auto previous_samples=r.samples,previous_quality=r.antialiasing_quality,previous_anisotropy=r.anisotropy;
        const auto previous_target=r.targets.at(0).color.Get();

        r.samples=samples;r.antialiasing_quality=quality;r.anisotropy=anisotropy;
        try {reset(r.resolution_scale);} catch(...) {
            if(r.targets.at(0).color.Get()==previous_target) {
                r.samples=previous_samples;r.antialiasing_quality=previous_quality;r.anisotropy=previous_anisotropy;
                if(previous_samples==1) r.multisample_copy.reset();
            }
            throw;
        }
        return;
    }
    std::unique_ptr<FrameAntialiasing> next;
    if(quality && quality<=3) {
        const auto& main=r.targets.at(0);
        const auto bytes=std::size_t(main.width)*main.height*r.resolution_scale*r.resolution_scale*4;
        if(bytes>r.target_limit-r.target_bytes) throw std::runtime_error("Antialiasing exceeds the render-target budget.");
        next=std::make_unique<FrameAntialiasing>(r.device.Get(),r.context.Get(),main.width*r.resolution_scale,main.height*r.resolution_scale,quality,r.stats.shader_compilations);
        r.count(PerfMetric::target_creations);
    }
    if(samples==1) r.multisample_copy.reset();
    r.unbind_textures();r.antialiasing=std::move(next);r.antialiasing_quality=quality;r.anisotropy=anisotropy;

    r.reset_layers();if(r.ambient_occlusion) r.layers=r.make_layers(true);
}
void Renderer::ambient_occlusion(unsigned quality,float strength) {
    auto& r=*impl_;
    if(r.recording || quality>3 || !std::isfinite(strength) || strength<0 || strength>1)
        throw std::runtime_error("Configure valid ambient occlusion outside submission");
    r.reset_layers();r.ambient_occlusion.reset();
    r.ao_quality=quality;r.ao_strength=strength;
    if(quality) {
        const auto& main=r.targets.at(0);
        const auto bytes=std::size_t(main.width)*main.height*r.resolution_scale*r.resolution_scale*20;
        if(bytes>r.target_limit-r.target_usage()) throw std::runtime_error("Ambient occlusion exceeds the render-target budget");
        r.ambient_occlusion=std::make_unique<FrameAmbientOcclusion>(r.device.Get(),r.context.Get(),main.width*r.resolution_scale,main.height*r.resolution_scale,quality,r.samples,r.stats.shader_compilations);
    }
    if(r.ambient_occlusion) r.layers=r.make_layers(true);
}
void Renderer::visibility(const Visibility* visibility) noexcept {impl_->visibility=visibility;}
bool Renderer::needs_motion_camera() const {return bool(impl_->ambient_occlusion);}
void Renderer::publish_frame(bool reset) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::publish);
    if(r.recording || !r.ended) throw std::runtime_error("Publish a completed game frame");
    {GpuSpan gpu(r.gpu_meter.get(),2);
        auto& main=r.targets.at(0);r.resolve(main);
        ID3D11Texture2D* image=main.image.Get();ID3D11ShaderResourceView* view=main.texture_view.Get();
        if(r.ambient_occlusion) {
            auto& ao=*r.ambient_occlusion;ao.invalidate();
            const bool layered=r.layers && r.layers->valid();
            if(r.ao_scene_depth && r.camera_valid && r.motion_camera.projection==1 && r.layer_target==r.presentation_target && (layered || r.ao_scene_only)) {
                r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
                const auto& source=r.targets.at(r.presentation_target);
                if(!layered) ao.capture_depth(source.depth_texture.Get());
                const auto* images=layered?&r.layers->resolved():nullptr;
                const auto tan_y=std::tan(r.upscale_frame.vertical_fov*0.5f);
                ao.publish(images?images->views[0].Get():view,images,r.upscale_frame.camera_near,r.upscale_frame.camera_far,tan_y*r.camera_aspect,tan_y,r.ao_strength);
                image=ao.image();view=ao.view();r.select_target(r.current_target);
            }
        }
        if(r.upscaler && r.camera_valid) {
            const auto now=std::chrono::steady_clock::now();
            r.upscale_frame.elapsed_ms=r.upscale_time.time_since_epoch().count()?std::chrono::duration<float,std::milli>(now-r.upscale_time).count():1000.0f/60;
            r.upscale_frame.reset=reset || r.camera_reset || r.upscale_frame.elapsed_ms>250;
            r.upscale_frame.jitter_x=r.applied_jitter[0];r.upscale_frame.jitter_y=r.applied_jitter[1];
            r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
            const auto found=r.targets.find(r.presentation_target);
            const auto& source=found==r.targets.end()?main:found->second;
            const auto responsive=source.responsive_view.Get();
            r.upscaler->publish_frame(image,source.depth_texture.Get(),responsive,r.upscale_frame);
            r.upscaled=true;r.camera_reset=false;r.upscale_time=now;
            const auto halton=[](unsigned index,unsigned base) {float result=0,factor=1;for(;index;index/=base){factor/=base;result+=factor*(index%base);}return result-0.5f;};
            const auto phase=unsigned((r.stats.published_frames+1)%32)+1;
            r.jitter={halton(phase,2),halton(phase,3)};r.applied_jitter={};
            const float zero[4]={};r.context->ClearRenderTargetView(source.responsive_target.Get(),zero);r.select_target(r.current_target);
        }
        if(r.antialiasing) {r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);r.antialiasing->publish(view);r.select_target(r.current_target);}
        if(r.layers) r.layers->invalidate();r.layer_scene=false;r.layer_target=UINT32_MAX;r.ao_scene_only=r.ao_scene_depth=false;
    }
    ++r.stats.published_frames;
    if(r.gpu_meter) r.gpu_meter->boundary(r.stats.published_frames+1);
}
bool Renderer::wait_frame(unsigned timeout_ms) {
    auto& r=*impl_;if(r.frame_acquired) return true;
#ifdef _WIN32
    if(!r.frame_ready) throw std::runtime_error("A window is required for presentation pacing");
    PerfScope availability(r.profile,PerfPhase::present);
    const auto result=WaitForSingleObject(r.frame_ready,timeout_ms);
    if(result==WAIT_TIMEOUT) return false;
    if(result!=WAIT_OBJECT_0) throw std::runtime_error("Window frame availability wait failed");
#else
    (void)timeout_ms;
    if(!r.window) throw std::runtime_error("A window is required for presentation pacing");
#endif
    r.frame_acquired=true;return true;
}
bool Renderer::display(bool wait) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::display);
    if(!r.window || !r.ended || r.recording)
        throw std::runtime_error("Display a completed main image");
#ifdef _WIN32
    RECT rectangle{};if(!GetClientRect(r.window,&rectangle)) throw std::runtime_error("Cannot read window dimensions");
    const auto width=static_cast<std::uint32_t>(rectangle.right),height=static_cast<std::uint32_t>(rectangle.bottom);
#else
    int pixel_width=0,pixel_height=0;
    if(!SDL_GetWindowSizeInPixels(static_cast<SDL_Window*>(r.window),&pixel_width,&pixel_height)) throw std::runtime_error(SDL_GetError());
    const auto width=static_cast<std::uint32_t>(pixel_width),height=static_cast<std::uint32_t>(pixel_height);
#endif
    if(!width || !height) return false;
    extent(width,height);
    if(!wait_frame(wait?UINT32_MAX:0)) return false;
    GpuSpan gpu(r.gpu_meter.get(),2);
    r.unbind_textures();r.context->OMSetRenderTargets(0,nullptr,nullptr);
    if(!r.window_view || r.window_width!=width || r.window_height!=height) {
        UINT flags=r.allow_tearing?DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING:0;
#ifdef _WIN32
        flags|=DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
#endif
        r.window_view.Reset();checked(r.swapchain->ResizeBuffers(0,width,height,DXGI_FORMAT_UNKNOWN,flags),"Window resize");
        ComPtr<ID3D11Texture2D> buffer;checked(r.swapchain->GetBuffer(0,IID_PPV_ARGS(&buffer)),"Window buffer");
        checked(r.device->CreateRenderTargetView(buffer.Get(),nullptr,&r.window_view),"Window render target");
        r.window_width=width;r.window_height=height;
    }
    auto& main=r.targets.at(0);r.resolve(main);
    auto output=r.window_view.Get();r.context->OMSetRenderTargets(1,&output,nullptr);
    const float black[4]={0,0,0,1};r.context->ClearRenderTargetView(output,black);
    const auto display_width=r.display_aspect>0?main.height*r.display_aspect:float(main.width);
    const auto scale=std::min(float(width)/display_width,float(height)/main.height);
    const auto image_width=display_width*scale,image_height=main.height*scale;
    const D3D11_VIEWPORT viewport{(width-image_width)/2,(height-image_height)/2,image_width,image_height,0,1};r.context->RSSetViewports(1,&viewport);
    r.context->RSSetState(r.display_raster.Get());r.context->OMSetDepthStencilState(r.display_depth.Get(),0);
    r.context->OMSetBlendState(nullptr,nullptr,UINT_MAX);
    r.context->IASetInputLayout(nullptr);r.context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    r.context->VSSetShader(r.display_vs.Get(),nullptr,0);r.context->PSSetShader(r.display_ps.Get(),nullptr,0);
    auto texture=r.upscaled?r.upscaler->view():r.antialiasing && r.antialiasing->ready()?r.antialiasing->view():r.ambient_occlusion && r.ambient_occlusion->ready()?r.ambient_occlusion->view():main.texture_view.Get();
    auto sampler=r.display_sampler.Get();
    r.context->PSSetShaderResources(0,1,&texture);
    r.context->PSSetSamplers(0,1,&sampler);r.context->Draw(3,0);
    r.unbind_textures();r.displaying=true;return true;
}
bool Renderer::present(bool vsync,bool wait) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::present);
    if(!r.displaying) throw std::runtime_error("Display a frame before presenting");
    // The swapchain stays windowed (SDL handles borderless fullscreen). Sync
    // interval zero also needs this flag to avoid compositor refresh throttling.
    const auto flags=(wait?0U:DXGI_PRESENT_DO_NOT_WAIT)|(!vsync && r.allow_tearing?DXGI_PRESENT_ALLOW_TEARING:0U);
    const auto result=r.swapchain->Present(vsync?1:0,flags);
    r.displaying=false;
    if(!wait && result==DXGI_ERROR_WAS_STILL_DRAWING) return false;
    checked(result,"Window presentation");r.frame_acquired=false;return true;
}
void Renderer::performance(Performance* profile) {
    auto& r=*impl_;if(r.gpu_meter) r.gpu_meter->collect();r.gpu_meter.reset();r.profile=profile;
    if(profile) {r.stats.counters={};r.gpu_meter=std::make_unique<GpuMeter>(r.device.Get(),r.context.Get(),*profile,r.stats.published_frames+1);}
}
RenderResources Renderer::resources() const {
    const auto& r=*impl_;RenderResources result;
    result.textures=r.textures.size()-1-r.free_textures.size();result.targets=r.targets.size()+unsigned(bool(r.antialiasing));result.texture_bytes=r.texture_bytes;
    result.target_bytes=r.target_usage();
    if(r.memory_adapter) {
        DXGI_QUERY_VIDEO_MEMORY_INFO local{},shared{};
        result.gpu_valid=SUCCEEDED(r.memory_adapter->QueryVideoMemoryInfo(0,DXGI_MEMORY_SEGMENT_GROUP_LOCAL,&local)) && SUCCEEDED(r.memory_adapter->QueryVideoMemoryInfo(0,DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL,&shared));
        if(result.gpu_valid) {result.local=local.CurrentUsage;result.budget=local.Budget;result.shared=shared.CurrentUsage;}
    }
    return result;
}

std::uint32_t Renderer::upload(const TextureData& texture) {
    auto& r = *impl_;
    PerfScope timing(r.profile,PerfPhase::texture_upload);GpuSpan gpu(r.gpu_meter.get(),1);
    r.count(PerfMetric::texture_uploads);r.count(PerfMetric::texture_bytes,texture.bytes.size());
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
    if (!r.texture_capacity()) throw std::runtime_error("Texture resource limit reached");
    if (texture.bytes.size() > 256 * 1024 * 1024 - r.texture_bytes)
        throw std::runtime_error("Texture resources exceed 256 MiB");
    if(texture.cubemap && (texture.width!=texture.height || texture.depth!=1)) throw std::runtime_error("Cubemap faces must be square and two-dimensional");
    const auto faces=texture.cubemap?6U:1U;
    std::vector<D3D11_SUBRESOURCE_DATA> data(texture.mip_levels*faces);
    std::vector<std::vector<std::byte>> linear(texture.mip_levels*faces);
    bool white_mask=!texture.cubemap && texture.depth==1 && (texture.format==TextureFormat::argb8 ||
        texture.format==TextureFormat::argb8_linear || texture.format==TextureFormat::dxt3 || texture.format==TextureFormat::dxt5);
    unsigned first_alpha=UINT_MAX;bool varying_alpha=false;
    std::size_t offset = 0;
    for(unsigned face=0;face<faces;++face) for (unsigned mip = 0; mip < texture.mip_levels; ++mip) {
        const auto subresource=face*texture.mip_levels+mip;
        const auto w = std::max(1U, texture.width >> mip), h = std::max(1U, texture.height >> mip), d=std::max(1U,texture.depth>>mip);
        const auto pitch = texture.pitch ? texture.pitch : block ? ((w + 3) / 4) * block : w * bpp;
        if (pitch < w * bpp) throw std::runtime_error("Texture pitch is shorter than a row");
        const auto slice = std::size_t(pitch) * (block ? (h + 3) / 4 : h),size=slice*d;
        if (offset > texture.bytes.size() || size > texture.bytes.size() - offset) throw std::runtime_error("Truncated texture mip payload");
        const auto input_bytes = texture.bytes.subspan(offset, size);
        if(white_mask) white_mask=white_mask_mip(texture,input_bytes,pitch,w,h,first_alpha,varying_alpha);
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
        // NV097 aligns each complete cubemap face, including its mip chain.
        if(texture.cubemap && mip+1==texture.mip_levels) offset=(offset+127)&~std::size_t(127);
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
    const auto id=r.retain_texture(std::move(view),texture.cubemap?3U:texture.depth>1?2U:1U,texture.bytes.size());
    r.textures[id].white_mask=white_mask && varying_alpha;return id;
}
void Renderer::release(std::uint32_t resource) {
    auto& r=*impl_;
    if(r.recording || !resource || resource>=r.textures.size() || !r.textures[resource].view)
        throw std::runtime_error("Release an existing game resource outside frame submission");
    // Unbind context references as well as cache references. D3D11 keeps work
    // already submitted alive without a CPU wait for the GPU.
    r.unbind_textures();
    if(const auto found=r.targets.find(resource);found!=r.targets.end()) {
        if(r.feedback_target==resource) r.feedback_target=0;
        if(r.current_target==resource) r.select_target(0);
        r.target_bytes-=r.target_size(found->second.width,found->second.height);r.targets.erase(found);
    }
    r.texture_bytes-=r.textures[resource].bytes;r.textures[resource]={};r.free_textures.push_back(resource);
}
Renderer::PreparedState Renderer::prepare(const RenderState& state) {
    auto& r = *impl_;
    PerfScope timing(r.profile,PerfPhase::resources);
    if (r.recording) throw std::runtime_error("Prepare pipelines before frame submission");
    validate_fragment(state);
    PreparedState prepared;prepared.owner_=&r;
    // Sampling varies per texture, independently of blend/depth/raster state.
    // Sharing these objects avoids rebuilding whole pipelines for LOD changes.
    for(unsigned i=0;i<4;++i) {
        const auto mode=state.texture_modes[i];
        if(!mode || mode==4 || mode==5) continue;
        const auto key=sampler_key(state,i,r.anisotropy);
        if(r.prepared_samplers[i] && !std::memcmp(&key,&r.prepared_sampler_keys[i],sizeof(key))) {
            prepared.samplers_[i]=r.prepared_samplers[i];continue;
        }
        if(const auto found=r.sampler_states.find(key);found!=r.sampler_states.end()) {
            prepared.samplers_[i]=found->second.Get();
            r.prepared_sampler_keys[i]=key;r.prepared_samplers[i]=prepared.samplers_[i].Get();continue;
        }
        if(r.sampler_states.size()==r.sampler_order.size()) {
            const auto oldest=r.sampler_states.find(r.sampler_order[r.sampler_next]);
            for(auto& cached:r.prepared_samplers) if(cached==oldest->second.Get()) cached=nullptr;
            r.sampler_states.erase(oldest);
        }
        D3D11_SAMPLER_DESC sampler{};
        sampler.Filter=D3D11_FILTER(key[1]);
        sampler.AddressU=address_mode(key[0]&15);
        sampler.AddressV=address_mode((key[0]>>8)&15);
        sampler.AddressW=address_mode((key[0]>>16)&15);
        sampler.MaxAnisotropy=key[5];
        sampler.ComparisonFunc=D3D11_COMPARISON_NEVER;
        sampler.MipLODBias=float(std::bit_cast<std::int32_t>(key[2]))/256;
        sampler.MinLOD=float(key[3]);sampler.MaxLOD=float(key[4]);
        ComPtr<ID3D11SamplerState> native;
        checked(r.device->CreateSamplerState(&sampler,&native),"Sampler state");
        prepared.samplers_[i]=r.sampler_states.emplace(key,std::move(native)).first->second.Get();
        r.sampler_order[r.sampler_next]=key;r.sampler_next=(r.sampler_next+1)%r.sampler_order.size();
        r.prepared_sampler_keys[i]=key;r.prepared_samplers[i]=prepared.samplers_[i].Get();
        r.count(PerfMetric::sampler_misses);
    }
    const auto key = pipeline_key(state,r.samples>1,bool(r.upscaler));
    if(r.prepared_pipeline && !std::memcmp(&key,&r.prepared_pipeline_key,sizeof(key))) {
        prepared.pipeline_=r.prepared_pipeline;return prepared;
    }
    if(const auto found=r.pipelines.find(key);found!=r.pipelines.end()) {
        prepared.pipeline_=&found->second;
        r.prepared_pipeline_key=key;r.prepared_pipeline=&found->second;return prepared;
    }
    if (r.pipelines.size() >= 1024) throw std::runtime_error("Pipeline cache limit reached");
    Impl::Pipeline pipeline;
    D3D11_BLEND_DESC blend{};
    if(r.upscaler) {blend.IndependentBlendEnable=TRUE;blend.RenderTarget[1].RenderTargetWriteMask=state.color_mask?D3D11_COLOR_WRITE_ENABLE_RED:0;}
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
    if(r.upscaler) {
        // HUD bases all need the game's colour blend; slot 1 of the ordinary
        // upscaling pipeline instead writes a single-channel responsive mask.
        blend.IndependentBlendEnable=FALSE;
        checked(r.device->CreateBlendState(&blend,&pipeline.layer_blend),"UI layer blend state");
    }
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
    if (state.cull && state.primitive!=1) {
        if (state.cull_face == 0x404) raster.CullMode = D3D11_CULL_FRONT;
        else if (state.cull_face == 0x405) raster.CullMode = D3D11_CULL_BACK;
        else throw std::runtime_error("Unsupported cull face");
    }
    raster.FrontCounterClockwise = state.front_ccw;
    raster.ScissorEnable=state.scissor_test;
    raster.MultisampleEnable=r.samples>1;
    if(state.polygon_offset) {
        if(!std::isfinite(state.polygon_offset_scale) || !std::isfinite(state.polygon_offset_bias) ||
            std::abs(state.polygon_offset_bias)>=2147483648.0f) throw std::runtime_error("Invalid polygon offset");
        raster.SlopeScaledDepthBias=state.polygon_offset_scale; raster.DepthBias=static_cast<INT>(state.polygon_offset_bias);
    }
    checked(r.device->CreateRasterizerState(&raster, &pipeline.raster), "Rasterizer state");
    prepared.pipeline_=&r.pipelines.emplace(key,std::move(pipeline)).first->second;
    r.prepared_pipeline_key=key;r.prepared_pipeline=static_cast<const Impl::Pipeline*>(prepared.pipeline_);
    r.count(PerfMetric::state_misses);
    return prepared;
}
std::uint32_t Renderer::prepare_program(const VertexProgram& program,bool launch) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::resources);
    if(r.recording) throw std::runtime_error("Prepare vertex programs before frame submission");
    const auto key=normalize_vertex_program(program);
    static_assert(sizeof(VertexTokens)==136*4*sizeof(std::uint32_t));
    for(std::size_t i=0;i<r.programs.size();++i)
        if(!r.programs[i].fixed && r.programs[i].launch==launch && r.programs[i].key.start==key.start &&
            !std::memcmp(r.programs[i].key.tokens.data(),key.tokens.data(),sizeof(VertexTokens))) return static_cast<std::uint32_t>(i+1);
    if(r.programs.size()>=1024) throw std::runtime_error("Vertex program cache limit reached");
    ShaderBinary metadata;
    const auto code=r.shader(launch?ShaderKind::launch:ShaderKind::vertex,&key,&metadata);
    Impl::Program prepared{key,launch};
    prepared.input_mask=metadata.input_mask;prepared.constant_first=metadata.constant_first;prepared.constant_count=metadata.constant_count;
    prepared.upload_first=metadata.constant_first;prepared.upload_count=metadata.constant_count;
    const auto screen_programs=screen_vertex_programs();
    prepared.screen_space=!launch && std::ranges::find(screen_programs,key)!=screen_programs.end();
    if(prepared.screen_space) {prepared.constant_count=prepared.constant_count?std::max(2U,prepared.constant_first+prepared.constant_count):2;prepared.constant_first=0;}
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
    if(r.upscaler && !prepared.launch && !prepared.screen_space) {
        const auto jitter=shader_binary(ShaderKind::vertex_jitter,&key);r.stats.shader_compilations+=jitter.compiled;
        checked(r.device->CreateVertexShader(jitter.bytes.data(),jitter.bytes.size(),nullptr,&prepared.jitter_vertex),"Jittered program shader");
    }
    r.programs.push_back(std::move(prepared));
    r.count(PerfMetric::program_creations);
    return static_cast<std::uint32_t>(r.programs.size());
}
std::uint32_t Renderer::prepare_fixed(const FixedTransform& transform) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::resources);
    if(r.recording) throw std::runtime_error("Prepare fixed transforms before frame submission");
    validate_fixed_transform(transform);
    for(unsigned i=0;i<r.programs.size();++i) if(r.programs[i].fixed && r.programs[i].fixed_key==transform) return i+1;
    if(r.programs.size()>=1024) throw std::runtime_error("Vertex program cache limit reached");
    Impl::Program prepared{};prepared.fixed=true;prepared.fixed_key=transform;
    const auto metadata=shader_binary(ShaderKind(unsigned(ShaderKind::fixed0)+transform.skin));
    prepared.input_mask=metadata.input_mask;prepared.constant_first=metadata.constant_first;prepared.constant_count=metadata.constant_count;
    prepared.upload_first=metadata.constant_first;prepared.upload_count=metadata.constant_count;
    prepared.signature=r.fixed_signatures[transform.skin];prepared.vertex=r.fixed_vertices[transform.skin];prepared.layout=r.fixed_layouts[transform.skin];
    if(r.upscaler) {
        const auto jitter=shader_binary(ShaderKind(unsigned(ShaderKind::fixed_jitter0)+transform.skin));r.stats.shader_compilations+=jitter.compiled;
        checked(r.device->CreateVertexShader(jitter.bytes.data(),jitter.bytes.size(),nullptr,&prepared.jitter_vertex),"Jittered fixed shader");
    }
    r.programs.push_back(std::move(prepared));r.count(PerfMetric::program_creations);return static_cast<std::uint32_t>(r.programs.size());
}
void Renderer::snapshot_constants(std::uint32_t program,const VertexConstants& source,VertexConstants& destination,std::uint32_t& previous) {
    auto& r=*impl_;
    if(!program || program>r.programs.size() || r.programs[program-1].launch) throw std::runtime_error("Snapshot requires a prepared draw program");
    const auto& used=r.programs[program-1];
    if(previous!=program) {destination={};previous=program;}
    if(used.constant_count) std::memcpy(destination.data()+used.constant_first,source.data()+used.constant_first,used.constant_count*sizeof(Float4));
}
std::uint32_t Renderer::prepare_layout(std::uint32_t program,const VertexLayout& attributes) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::resources);
    if(r.recording || !program || program>r.programs.size() || r.programs[program-1].launch)
        throw std::runtime_error("Prepare a draw program before its vertex layout");
    static_assert(sizeof(VertexLayout)==16*3*sizeof(std::uint32_t));
    for(unsigned i=0;i<r.stream_layouts.size();++i)
        if(r.stream_layouts[i].program==program &&
            !std::memcmp(r.stream_layouts[i].attributes.data(),attributes.data(),sizeof(VertexLayout))) return i+1;
    if(r.stream_layouts.size()>=1024) throw std::runtime_error("Vertex layout cache limit reached");
    std::array<D3D11_INPUT_ELEMENT_DESC,16> inputs{};
    for(unsigned i=0;i<inputs.size();++i) {
        const auto& attribute=attributes[i];
        if(attribute.stream>16 || attribute.offset>2048) throw std::runtime_error("Invalid vertex attribute stream or offset");
        const auto format=attribute.stream==16?AttributeFormat{DXGI_FORMAT_R32G32B32A32_FLOAT,16,16}:attribute_format(attribute.format);
        inputs[i]={"TEXCOORD",i,format.native,attribute.stream,attribute.stream==16?i*16:attribute.offset,D3D11_INPUT_PER_VERTEX_DATA,0};
    }
    Impl::StreamLayout layout{program,attributes};
    for(const auto& attribute:attributes) if(attribute.stream<16) {
        const auto format=attribute_format(attribute.format);
        layout.required[attribute.stream]=std::max(layout.required[attribute.stream],attribute.offset+format.bytes);
        layout.fetched[attribute.stream]=std::max(layout.fetched[attribute.stream],attribute.offset+format.fetched);
    }
    const auto& code=r.programs[program-1].signature;
    checked(r.device->CreateInputLayout(inputs.data(),static_cast<UINT>(inputs.size()),code.data(),code.size(),&layout.native),"Native vertex stream layout");
    r.stream_layouts.push_back(std::move(layout));r.count(PerfMetric::layout_misses);return static_cast<std::uint32_t>(r.stream_layouts.size());
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
    PerfScope timing(r.profile,PerfPhase::render);GpuSpan gpu(r.gpu_meter.get(),0);
    if (!std::isfinite(depth) || depth < 0 || depth > 1) throw std::runtime_error("Invalid clear depth");
    if(!r.current_target) r.feedback_target=0;
    if(!r.current_target && r.antialiasing) r.antialiasing->invalidate();
    if(r.layers && r.layer_target==r.current_target) {r.layers->invalidate();r.layer_scene=false;r.ao_scene_depth=false;}
    const auto rgba = color(argb);
    r.targets.at(r.current_target).uniform_alpha=rgba[3];
    if(r.samples>1) r.targets.at(r.current_target).dirty=true;
    r.context->ClearRenderTargetView(r.rtv.Get(), rgba.data());
    if(auto* mask=r.targets.at(r.current_target).responsive_target.Get()) {const float zero[4]={};r.context->ClearRenderTargetView(mask,zero);}
    r.context->ClearDepthStencilView(r.dsv.Get(),D3D11_CLEAR_DEPTH|
        (r.targets.at(r.current_target).depth_format==SurfaceDepth::z16?0:D3D11_CLEAR_STENCIL),depth,0);
}
void Renderer::clear_buffers(std::uint32_t flags,std::uint32_t argb,float depth,std::uint8_t stencil,
                             std::span<const std::int32_t> rectangle) {
    auto& r=*impl_;
    PerfScope timing(r.profile,PerfPhase::render);GpuSpan gpu(r.gpu_meter.get(),0);
    if(flags&~0xF3U) throw std::runtime_error("Invalid clear channel flags");
    if((flags&0xF0U) && !r.current_target) r.feedback_target=0;
    if((flags&0xF0U) && !r.current_target && r.antialiasing) r.antialiasing->invalidate();
    if((flags&0xF0U) && r.layers && r.layer_target==r.current_target) {r.layers->invalidate();r.layer_scene=false;r.ao_scene_depth=false;}
    if(r.samples>1 && (flags&0xF0U)) r.targets.at(r.current_target).dirty=true;
    if(r.targets.at(r.current_target).depth_format==SurfaceDepth::z16) flags&=~2U;
    if(!std::isfinite(depth) || depth<0 || depth>1) throw std::runtime_error("Invalid clear depth");
    if(!rectangle.empty() && rectangle.size()!=4) throw std::runtime_error("A clear rectangle requires four coordinates");
    const D3D11_RECT bounds=rectangle.empty()?D3D11_RECT{0,0,LONG(r.width),LONG(r.height)}:
        D3D11_RECT{std::clamp<LONG>(rectangle[0],0,r.width),std::clamp<LONG>(rectangle[1],0,r.height),
                   std::clamp<LONG>(rectangle[2],0,r.width),std::clamp<LONG>(rectangle[3],0,r.height)};
    if(!flags || bounds.left>=bounds.right || bounds.top>=bounds.bottom) return;
    if(flags&0x80U) {
        auto& alpha=r.targets.at(r.current_target).uniform_alpha;const auto value=color(argb)[3];
        if(!bounds.left && !bounds.top && bounds.right==LONG(r.width) && bounds.bottom==LONG(r.height)) alpha=value;
        else if(alpha!=value) alpha=-1;
    }
    if(bounds.left || bounds.top || bounds.right!=LONG(r.width) || bounds.bottom!=LONG(r.height) ||
        ((flags&0xF0U)!=0 && (flags&0xF0U)!=0xF0U)) {
        // One precompiled pass replaces only the requested channels and pixels.
        // Its state is independent of the preceding game's blend/depth/scissor.
        const std::array<Float4,2> constants{color(argb),Float4{depth,0,0,0}};
        D3D11_MAPPED_SUBRESOURCE mapped{};
        checked(r.context->Map(r.clear_constants.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Clear constants");
        std::memcpy(mapped.pData,constants.data(),sizeof(constants));r.context->Unmap(r.clear_constants.Get(),0);
        r.unbind_textures();auto target=r.rtv.Get();r.context->OMSetRenderTargets(1,&target,r.dsv.Get());
        const D3D11_VIEWPORT viewport{0,0,float(r.width*r.resolution_scale),float(r.height*r.resolution_scale),0,1};
        const auto scale=LONG(r.resolution_scale);const D3D11_RECT scaled{bounds.left*scale,bounds.top*scale,bounds.right*scale,bounds.bottom*scale};
        r.context->RSSetViewports(1,&viewport);r.context->RSSetScissorRects(1,&scaled);r.context->RSSetState(r.clear_raster.Get());
        r.context->OMSetBlendState(r.clear_blends[(flags>>4)&15].Get(),nullptr,UINT_MAX);
        r.context->OMSetDepthStencilState(r.clear_depths[flags&3].Get(),stencil);
        r.context->IASetInputLayout(nullptr);r.context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        r.context->GSSetShader(nullptr,nullptr,0);
        r.context->VSSetShader(r.clear_vs.Get(),nullptr,0);r.context->PSSetShader(r.clear_ps.Get(),nullptr,0);
        auto buffer=r.clear_constants.Get();r.context->VSSetConstantBuffers(0,1,&buffer);r.context->PSSetConstantBuffers(0,1,&buffer);
        r.context->Draw(3,0);return;
    }
    if(flags&0xF0U) {
        const auto rgba=color(argb);r.context->ClearRenderTargetView(r.rtv.Get(),rgba.data());
    }
    const auto mask=((flags&1)?D3D11_CLEAR_DEPTH:0)|((flags&2)?D3D11_CLEAR_STENCIL:0);
    if(mask) r.context->ClearDepthStencilView(r.dsv.Get(),mask,depth,stencil);
}
void Renderer::begin(std::uint32_t argb, float depth, bool clear_target) {
    auto& r = *impl_;
    if (r.recording) throw std::runtime_error("Frame already begun");
    if(!r.current_target) r.presentation_target=r.feedback_target=0;
    if(!r.current_target && r.antialiasing) r.antialiasing->invalidate();
    if(clear_target) clear(argb, depth);
    // Clear, presentation and native-context users may change bindings between
    // batches. Within a batch only changed state needs another driver call.
    r.bound.valid=false;
    r.stats.draws = r.stats.vertices = r.stats.vertex_bytes = 0; r.stats.gpu_ms = 0; r.stats.gpu_timing_valid = false;
    r.collect_timings();r.timing_index=static_cast<unsigned>(r.timings.size());++r.frame_id;
    for(unsigned index=0;index<r.timings.size();++index) if(!r.timings[index].pending) {
        r.timing_index=index;auto& timing=r.timings[index];timing.frame=r.frame_id;
        r.context->Begin(timing.timer.Get());r.context->End(timing.start.Get());break;
    }
    if(r.gpu_meter) r.gpu_meter->begin(0);
    r.recording = true; r.ended = false;
    const auto& output=r.targets.at(r.current_target);
    const std::array<ID3D11RenderTargetView*,2> targets{r.rtv.Get(),output.responsive_target.Get()};
    r.context->OMSetRenderTargets(output.responsive_target?2:1,targets.data(),r.dsv.Get());
    const D3D11_VIEWPORT viewport{0, 0, float(r.width*r.resolution_scale), float(r.height*r.resolution_scale), 0, 1};
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
    std::array<Float4,4> positions{};
    if(vertices.size()==4 && r.programs[program-1].screen_space) for(unsigned i=0;i<4;++i) positions[i]=vertices[i].attributes[0];
    const auto edge_aligned=vertices.size()==4 && r.edge_aligned(r.programs[program-1],constants,positions);
    ProgramUniforms uniforms{constants,r.program_viewport(view,edge_aligned),
        {state.fog_parameters[0],state.fog_parameters[1],float(mode),state.fog_enable?(absolute?2.0f:1.0f):0.0f},view.texture_scales};
    if(state.primitive==1 && state.point_scale) std::memcpy(uniforms.point_parameters.data(),state.point_parameters.data(),sizeof(uniforms.point_parameters));
    r.draw(state,std::as_bytes(vertices),vertices.size(),indices,&r.programs[program-1],&uniforms);
}
void Renderer::draw_streams(const RenderState& state,std::uint32_t layout,const VertexConstants& constants,const ProgramView& view,
                            std::span<const VertexStream> streams,const ProgramVertex& current,std::uint32_t count,
                            std::span<const std::uint32_t> indices,const PreparedState* bindings) {
    impl_->draw_streams(state,layout,constants,view,streams,current,count,indices,bindings);
}
void Renderer::draw_streams(std::span<const StreamDraw> draws) {
    auto& r=*impl_;
    if(!r.recording || draws.size()>r.stream_uploads.size()) throw std::runtime_error("Queued draws exceed their bound");
    constexpr UINT factor_size=(sizeof(Constants)+255)&~255U;
    for(std::size_t first=0;first<draws.size();) {
        std::size_t last=first;UINT bytes=0,factor_bytes=0,program_bytes=0,index_bytes=0;
        for(;last<draws.size();++last) {
            const auto& draw=draws[last];
            if(!draw.state || !draw.constants || !draw.view || !draw.current) throw std::runtime_error("Incomplete queued draw");
            auto upload=r.stream_storage(draw.layout,draw.streams,draw.count);
            UINT next_bytes=bytes,reused=0,reused_bytes=0;
            const auto* previous=last>first?&draws[last-1]:nullptr;
            const auto* previous_upload=previous?&r.stream_uploads[last-1].vertices:nullptr;
            if(previous && !std::memcmp(draw.current,previous->current,sizeof(ProgramVertex))) {
                upload.offsets[16]=previous_upload->offsets[16];reused|=1U<<16;reused_bytes+=sizeof(ProgramVertex);
            } else {next_bytes=(next_bytes+15)&~15U;upload.offsets[16]=next_bytes;next_bytes+=sizeof(ProgramVertex);}
            for(unsigned stream=0;stream<draw.streams.size();++stream) if(upload.sizes[stream]) {
                const auto& source=draw.streams[stream];
                if(previous && stream<previous->streams.size() &&
                   source.bytes.data()==previous->streams[stream].bytes.data() && source.bytes.size()==previous->streams[stream].bytes.size() &&
                   source.stride==previous->streams[stream].stride && upload.strides[stream]==previous_upload->strides[stream] &&
                   upload.sizes[stream]==previous_upload->sizes[stream]) {
                    upload.offsets[stream]=previous_upload->offsets[stream];reused|=1U<<stream;reused_bytes+=upload.sizes[stream];
                } else {next_bytes=(next_bytes+15)&~15U;upload.offsets[stream]=next_bytes;next_bytes+=upload.sizes[stream];}
            }
            if(next_bytes>Impl::max_vertices*sizeof(ProgramVertex)) break;
            auto indices=Impl::plan_indices(draw.state->primitive,draw.count,draw.indices);
            const auto index_size=indices.count*4;
            if(index_size>Impl::max_indices*4-index_bytes) break;
            auto& staged=r.stream_uploads[last];
            if(r.constant_context) {
                staged.factors=fragment_constants(*draw.state);
                const auto program_id=r.stream_layouts[draw.layout-1].program;
                const auto& program=r.programs[program_id-1];
                const auto program_size=uniform_capacity(program.upload_count);
                r.stream_uniforms(staged.uniforms,*draw.state,draw.layout,*draw.constants,*draw.view,draw.streams,draw.count,staged.program_id==program_id);
                staged.program_id=program_id;
                staged.factor_changed=last==first || std::memcmp(&staged.factors,&r.stream_uploads[last-1].factors,sizeof(Constants));
                staged.program_changed=last==first || staged.program_id!=r.stream_uploads[last-1].program_id ||
                    !same_uniforms(staged.uniforms,r.stream_uploads[last-1].uniforms,program.constant_first,program.constant_count);
                if((staged.factor_changed && factor_size>Impl::constant_capacity-factor_bytes) ||
                   (staged.program_changed && program_size>Impl::constant_capacity-program_bytes)) break;
                staged.factor_offset=staged.factor_changed?factor_bytes:r.stream_uploads[last-1].factor_offset;
                staged.program_offset=staged.program_changed?program_bytes:r.stream_uploads[last-1].program_offset;
                if(staged.factor_changed) factor_bytes+=factor_size;
                if(staged.program_changed) program_bytes+=program_size;
            }
            upload.bytes=next_bytes-bytes;bytes=next_bytes;staged.vertices=upload;
            staged.reused_streams=reused;staged.reused_bytes=reused_bytes;
            indices.offset=index_bytes;staged.indices=indices;index_bytes+=index_size;
        }
        UINT base=0;
        {
            PerfScope timing(r.profile,PerfPhase::buffer_upload);
            if(r.constant_context) {
                // A failed submission must not reuse values from storage that
                // this group has already replaced.
                r.factors_valid=r.program_valid=false;
                UINT factor_base=0,program_base=0;
                auto* factors=r.append_buffer(r.factor_buffer.Get(),Impl::constant_capacity,r.factor_cursor,factor_bytes,factor_base,256);
                for(auto i=first;i<last;++i) {
                    auto& staged=r.stream_uploads[i];
                    if(staged.factor_changed) {
                        std::memcpy(factors+staged.factor_offset,&staged.factors,sizeof(Constants));
                        std::memset(factors+staged.factor_offset+sizeof(Constants),0,factor_size-sizeof(Constants));
                    }
                    staged.factor_offset+=factor_base;
                }
                r.context->Unmap(r.factor_buffer.Get(),0);
                auto* programs=r.append_buffer(r.program_constants.Get(),Impl::constant_capacity,r.program_cursor,program_bytes,program_base,256);
                for(auto i=first;i<last;++i) {
                    auto& staged=r.stream_uploads[i];
                    if(staged.program_changed) {
                        const auto& program=r.programs[staged.program_id-1];
                        const auto size=uniform_bytes(program.upload_count),capacity=uniform_capacity(program.upload_count);
                        write_uniforms(programs+staged.program_offset,staged.uniforms,program.upload_first,program.upload_count);
                        if(capacity>size) std::memset(programs+staged.program_offset+size,0,capacity-size);
                    }
                    staged.program_offset+=program_base;
                }
                r.context->Unmap(r.program_constants.Get(),0);
            }
            auto* destination=r.append_buffer(r.vertices.Get(),Impl::max_vertices*sizeof(ProgramVertex),r.vertex_cursor,bytes,base);
            for(auto i=first;i<last;++i) {
                const auto& draw=draws[i];auto& upload=r.stream_uploads[i].vertices;
                Impl::fill_streams(destination,upload,draw.streams,*draw.current,draw.count,r.stream_uploads[i].reused_streams);
                r.stats.reused_vertex_bytes+=r.stream_uploads[i].reused_bytes;
                for(auto& offset:upload.offsets) offset+=base;
            }
            r.context->Unmap(r.vertices.Get(),0);
            if(index_bytes) {
                UINT index_base=0;
                auto* output=r.append_buffer(r.index_buffer.Get(),Impl::max_indices*4,r.index_cursor,index_bytes,index_base);
                for(auto i=first;i<last;++i) {
                    const auto& draw=draws[i];auto& indices=r.stream_uploads[i].indices;
                    if(indices.count) Impl::fill_indices(output+indices.offset,draw.state->primitive,draw.count,draw.indices);
                    indices.offset+=index_base;
                }
                r.context->Unmap(r.index_buffer.Get(),0);
            }
        }
        for(auto i=first;i<last;++i) {
            const auto& draw=draws[i];
            r.draw_streams(*draw.state,draw.layout,*draw.constants,*draw.view,draw.streams,*draw.current,
                draw.count,draw.indices,draw.bindings,&r.stream_uploads[i]);
        }
        if(r.constant_context) {
            r.last_factors=r.stream_uploads[last-1].factors;r.factors_valid=true;
            r.last_program=r.stream_uploads[last-1].uniforms;r.program_valid=true;
            r.last_program_id=r.stream_uploads[last-1].program_id;
        }
        first=last;
    }
}
void Renderer::Impl::draw_streams(const RenderState& state,std::uint32_t layout,const VertexConstants& constants,const ProgramView& view,
                                 std::span<const VertexStream> streams,const ProgramVertex& current,std::uint32_t count,
                                 std::span<const std::uint32_t> indices,const PreparedState* bindings,const StreamBatchDraw* uploaded) {
    std::optional<ProgramUniforms> local;
    const ProgramUniforms* uniforms;
    if(uploaded && constant_context) uniforms=&uploaded->uniforms;
    else {stream_uniforms(local.emplace(),state,layout,constants,view,streams,count);uniforms=&*local;}
    const Impl::StreamDraw source{stream_layouts[layout-1],streams,current,uploaded?&uploaded->vertices:nullptr,uploaded};
    draw(state,{},count,indices,&programs[source.layout.program-1],uniforms,&source,bindings);
}
void Renderer::Impl::stream_uniforms(ProgramUniforms& uniforms,const RenderState& state,std::uint32_t layout,
                                    const VertexConstants& constants,const ProgramView& view,
                                    std::span<const VertexStream> streams,std::uint32_t count,bool same_program) const {
    auto& r=*this;
    if(!layout || layout>r.stream_layouts.size() || streams.size()>16) throw std::runtime_error("Native vertex streams were not prepared");
    if(!std::isfinite(view.depth_scale) || view.depth_scale<=0) throw std::runtime_error("Invalid vertex depth scale");
    const auto& prepared=r.stream_layouts[layout-1];
    unsigned mode=0;
    if(state.fog_enable) switch(state.fog_mode) {
    case 0x2601:case 0x0804:mode=0;break;case 0x0800:case 0x0802:mode=1;break;case 0x0801:case 0x0803:mode=2;break;
    default:throw std::runtime_error(std::format("Unsupported NV2A fog mode 0x{:X}",state.fog_mode));
    }
    const bool absolute=state.fog_mode==0x0802 || state.fog_mode==0x0803 || state.fog_mode==0x0804;
    bool edge_aligned=false;
    const auto& position=prepared.attributes[0];const auto& program=r.programs[prepared.program-1];
    if(count==4 && program.screen_space && position.stream<streams.size() && (position.format==0x42 || position.format==0x32)) {
        const auto& source=streams[position.stream];const auto bytes=position.format==0x42?16U:12U;
        if(source.bytes.size()>=std::size_t(source.stride)*3+position.offset+bytes) {
            std::array<Float4,4> positions{};
            for(unsigned i=0;i<4;++i) {positions[i][3]=1;std::memcpy(positions[i].data(),source.bytes.data()+i*source.stride+position.offset,bytes);}
            edge_aligned=r.edge_aligned(program,constants,positions);
        }
    }
    if(!same_program) uniforms.constants={};
    if(program.constant_count) std::memcpy(uniforms.constants.data()+program.constant_first,constants.data()+program.constant_first,program.constant_count*sizeof(Float4));
    uniforms.viewport=r.program_viewport(view,edge_aligned);
    uniforms.fog={state.fog_parameters[0],state.fog_parameters[1],float(mode),state.fog_enable?(absolute?2.0f:1.0f):0.0f};
    uniforms.texture_scales=view.texture_scales;uniforms.fixed_flags={};uniforms.fixed_texgen={};uniforms.fixed_matrices={};
    uniforms.point_parameters={};
    if(state.primitive==1 && state.point_scale) std::memcpy(uniforms.point_parameters.data(),state.point_parameters.data(),sizeof(uniforms.point_parameters));
    for(unsigned i=0;i<16;++i) uniforms.input_formats[i]=prepared.attributes[i].stream==16?0:prepared.attributes[i].format;
    if(program.fixed) {
        const auto& fixed=program.fixed_key;
        uniforms.fixed_flags={std::uint32_t(fixed.normalize),std::uint32_t(fixed.fog),fixed.fog_source,std::uint32_t(state.primitive==1 && state.point_scale)};uniforms.fixed_texgen=fixed.texgen;
        for(unsigned i=0;i<4;++i) uniforms.fixed_matrices[i]=fixed.texture_matrix[i];
    }
}
void Renderer::Impl::draw(const RenderState& state,Bytes vertex_bytes,std::size_t vertex_count,
                          std::span<const std::uint32_t> input_indices,const Program* program,const ProgramUniforms* uniforms,const StreamDraw* streams,const PreparedState* bindings) {
    auto& r = *this;
    PerfScope timing(r.profile,PerfPhase::render,program?unsigned(program-r.programs.data())+1:0);
    if (!r.recording) throw std::runtime_error("Begin a frame before drawing");
    if (!vertex_count || vertex_count > Impl::max_vertices || input_indices.size() > Impl::max_vertices)
        throw std::runtime_error("Draw exceeds bounded vertex/index buffers");
    const Pipeline* selected;
    if(bindings) {
        if(bindings->owner_!=&r || !bindings->pipeline_) throw std::runtime_error("Draw bindings belong to another renderer");
        selected=static_cast<const Pipeline*>(bindings->pipeline_);
    } else {
            const auto found=r.pipelines.find(pipeline_key(state,r.samples>1,bool(r.upscaler)));
        if(found==r.pipelines.end()) throw std::runtime_error("Pipeline was not prepared before submission");
        selected=&found->second;
    }
    const auto& pipeline=*selected;
    const auto* batch=streams?streams->batch:nullptr;
    const auto* constant_batch=r.constant_context?batch:nullptr;
    const auto plan=batch?batch->indices:plan_indices(state.primitive,static_cast<UINT>(vertex_count),input_indices);
    const auto count = input_indices.empty() ? vertex_count : input_indices.size();
    const auto topology=plan.topology;
    auto indices = input_indices;
    if(!batch && plan.count && (state.primitive==3 || state.primitive==7 || state.primitive==8 || state.primitive==9 || state.primitive==10)) {
        r.converted.resize(plan.count);
        fill_indices(reinterpret_cast<std::byte*>(r.converted.data()),state.primitive,static_cast<UINT>(vertex_count),input_indices);
        indices=r.converted;
    }
    const auto upload = [&](ID3D11Buffer* buffer,const void* data,std::size_t size,UINT& cursor,UINT& offset,const Program* packed=nullptr) {
        PerfScope timing(r.profile,PerfPhase::buffer_upload);
        const auto write=[&](void* output) {
            if(packed) write_uniforms(output,*static_cast<const ProgramUniforms*>(data),packed->upload_first,packed->upload_count);
            else std::memcpy(output,data,size);
        };
        if(r.constant_context) {
            const auto padded=(size+255)&~std::size_t(255);
            auto* output=r.append_buffer(buffer,constant_capacity,cursor,padded,offset,256);
            write(output);if(padded>size) std::memset(output+size,0,padded-size);
            r.context->Unmap(buffer,0);return;
        }
        offset=0;
        r.count(PerfMetric::buffer_maps);r.count(PerfMetric::buffer_bytes,size);
        D3D11_MAPPED_SUBRESOURCE mapped{};
        checked(r.context->Map(buffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped), "Buffer map");
        write(mapped.pData); r.context->Unmap(buffer, 0);
    };
    std::optional<Constants> local_factors;
    const auto* constants=constant_batch?&constant_batch->factors:&local_factors.emplace(fragment_constants(state));
    std::array<ID3D11ShaderResourceView*, 12> views{};
    std::array<ID3D11SamplerState*, 4> samplers{};
    samplers.fill(r.default_sampler.Get()); // Static shaders declare all four slots.
    for (unsigned i = 0; i < 4; ++i) {
        const auto mode=state.texture_modes[i];
        const bool sampled=mode && mode!=4 && mode!=5;
        if (sampled && (!state.textures[i] || state.textures[i] >= r.textures.size() || !r.textures[state.textures[i]].view))
            throw std::runtime_error("Enabled texture is not uploaded");
        if(sampled && state.textures[i]==r.current_target)
            throw std::runtime_error("Copy the active render target before sampling its contents");
        if(sampled && r.textures[state.textures[i]].dimension!=(mode==3?3U:mode==2?2U:1U))
            throw std::runtime_error("Texture resource dimension does not match its shader mode");
        if(sampled) {
            if(r.samples>1) if(const auto target=r.targets.find(state.textures[i]);target!=r.targets.end()) r.resolve(target->second);
            views[i+(mode==3?4:mode==2?8:0)] = r.textures[state.textures[i]].view.Get();
        }
        if(bindings && sampled) samplers[i]=bindings->samplers_[i].Get();
        else if(sampled) {
            const auto sampler=r.sampler_states.find(sampler_key(state,i,r.anisotropy));
            if(sampler==r.sampler_states.end()) throw std::runtime_error("Sampler was not prepared before submission");
            samplers[i]=sampler->second.Get();
        }
    }
    UINT vertex_offset=0;
    std::array<UINT,17> stream_offsets{},stream_strides{};
    if(streams) {
        std::optional<StreamUpload> local;
        const auto* uploaded=streams->uploaded;
        if(!uploaded) {
            auto& upload=local.emplace(r.stream_storage(static_cast<std::uint32_t>(&streams->layout-r.stream_layouts.data())+1,
                                    streams->streams,static_cast<std::uint32_t>(vertex_count)));
            PerfScope timing(r.profile,PerfPhase::buffer_upload);
            auto* destination=r.append_buffer(r.vertices.Get(),max_vertices*sizeof(ProgramVertex),r.vertex_cursor,upload.bytes,vertex_offset);
            fill_streams(destination,upload,streams->streams,streams->current,static_cast<std::uint32_t>(vertex_count));
            r.context->Unmap(r.vertices.Get(),0);
            for(auto& offset:upload.offsets) offset+=vertex_offset;
            uploaded=&upload;
        }
        r.stats.vertex_bytes+=uploaded->bytes;stream_offsets=uploaded->offsets;stream_strides=uploaded->strides;
    } else {
        PerfScope vertex_upload(r.profile,PerfPhase::buffer_upload);
        auto* destination=r.append_buffer(r.vertices.Get(),max_vertices*sizeof(ProgramVertex),r.vertex_cursor,vertex_bytes.size(),vertex_offset);
        std::memcpy(destination,vertex_bytes.data(),vertex_bytes.size());r.context->Unmap(r.vertices.Get(),0);
        r.stats.vertex_bytes+=vertex_bytes.size();
    }
    if(constant_batch) {r.factor_offset=constant_batch->factor_offset;r.program_offset=constant_batch->program_offset;}
    else if(!r.factors_valid || std::memcmp(&r.last_factors,constants,sizeof(*constants))) {
        upload(r.factor_buffer.Get(),constants,sizeof(*constants),r.factor_cursor,r.factor_offset);r.last_factors=*constants;r.factors_valid=true;
    }
    if(uniforms && !constant_batch) {
        const auto* values=uniforms;
        std::optional<ProgramUniforms> adjusted;
        if(program->fixed && !streams) {
            auto& fixed_values=adjusted.emplace(*uniforms);
            const auto& fixed=program->fixed_key;
            fixed_values.fixed_flags={std::uint32_t(fixed.normalize),std::uint32_t(fixed.fog),fixed.fog_source,std::uint32_t(state.primitive==1 && state.point_scale)};fixed_values.fixed_texgen=fixed.texgen;
            for(unsigned i=0;i<4;++i) fixed_values.fixed_matrices[i]=fixed.texture_matrix[i];
            values=&fixed_values;
        }
        const auto id=static_cast<std::uint32_t>(program-r.programs.data())+1;
        if(!r.program_valid || r.last_program_id!=id || !same_uniforms(r.last_program,*values,program->constant_first,program->constant_count)) {
            upload(r.program_constants.Get(),values,uniform_bytes(program->upload_count),r.program_cursor,r.program_offset,program);r.last_program=*values;r.program_valid=true;r.last_program_id=id;
        }
    }
    const UINT stride = static_cast<UINT>(program?sizeof(ProgramVertex):sizeof(Vertex));
    auto vertex_buffer = r.vertices.Get(); auto constant_buffer = r.factor_buffer.Get();
    auto& bound=r.bound;
    const auto input_layout=streams?streams->layout.native.Get():program?program->layout.Get():r.layout.Get();
    const auto simple=simple_combiner(state);
    bool layered=false;
    const auto& main=r.targets.at(0);
    if(r.layers && r.width==main.width && r.height==main.height &&
       r.targets.at(r.current_target).color_format==SurfaceColor::rgba8 && state.color_mask) {
        if(r.layer_target!=r.current_target) {r.layers->invalidate();r.layer_scene=false;r.ao_scene_depth=false;r.layer_target=r.current_target;}
        const bool screen=program && program->screen_space;
        r.ao_scene_only=!screen && state.depth_test;
        if(r.ao_scene_only && state.depth_write) r.ao_scene_depth=true;
        // Menu/video backgrounds can be opaque screen-space copies without
        // depth. Keep those copies in the scene before the following UI suffix.
        const bool scene=(!screen && state.depth_test) || (screen && !state.blend && !state.alpha_test &&
            (state.color_mask&0x10101U)==0x10101U);
        if(scene) r.layer_scene=true;
        if(screen && !state.depth_test && simple==1 && state.textures[0] && r.textures[state.textures[0]].white_mask)
            r.layer_scene=true;
        // Positive affine blends remain separable through RGB clamping when
        // both the transform and its clamped endpoint bounds are retained.
        // Add/subtract with destination alpha remains affine while alpha is
        // unchanged. Menus use reverse subtraction for their shadow overlay.
        const bool offset=!(state.color_mask&0x01000000U) && state.blend_destination==1 &&
            (state.blend_source==1 || (state.blend_source>=0x302 && state.blend_source<=0x305));
        const bool affine=!state.blend || (state.blend_equation==0x8006 &&
            ((state.blend_source==0x302 && state.blend_destination==0x303) ||
             (state.blend_source==0x304 && state.blend_destination==0x305) ||
             (state.blend_source==0x305 && state.blend_destination==0x304) ||
             (state.blend_source==1 && state.blend_destination==0) ||
             offset ||
             (state.blend_source==0 && (state.blend_destination<=1 ||
                 (state.blend_destination>=0x300 && state.blend_destination<=0x303))))) ||
            (state.blend_equation==0x800B && offset);
        layered=screen && !scene && affine && r.layer_scene && !(state.depth_test && state.depth_write) && !state.stencil_test;
        if(layered) {if(!r.layers->valid()) {
            const auto& target=r.targets.at(r.current_target);
            r.visibility_scope(false);
            auto view=r.samples>1?target.multisample_view.Get():target.texture_view.Get();
            r.layers->begin(r.target.Get(),view,target.uniform_alpha);bound.valid=false;
            if(r.ambient_occlusion && r.ao_scene_depth) r.ambient_occlusion->capture_depth(target.depth_texture.Get());
        }}
        else {
            r.layers->invalidate();
            // Once an unseparated overlay changes colour, its image no longer
            // matches the scene depth. A later UI suffix cannot restore that.
            if(!r.ao_scene_only) r.ao_scene_depth=false;
        }
    }
    if(r.layers && (!bound.valid || bound.layers!=layered)) {
        const auto& output=r.targets.at(r.current_target);
        const std::array<ID3D11RenderTargetView*,2> targets{r.rtv.Get(),output.responsive_target.Get()};
        r.context->OMSetRenderTargets(output.responsive_target?2:1,targets.data(),r.dsv.Get());
    }
    bound.layers=layered;
    if(streams) {
        std::array<ID3D11Buffer*,17> buffers{};buffers.fill(vertex_buffer);
        r.context->IASetVertexBuffers(0,17,buffers.data(),stream_strides.data(),stream_offsets.data());
    } else r.context->IASetVertexBuffers(0,1,&vertex_buffer,&stride,&vertex_offset);
    if(!bound.valid || bound.layout!=input_layout) r.context->IASetInputLayout(input_layout);
    if(!bound.valid || bound.topology!=topology) r.context->IASetPrimitiveTopology(topology);
    const bool responsive=bool(r.upscaler) && bool(r.targets.at(r.current_target).responsive_target);
    const bool jittered=responsive && r.camera_valid && state.depth_test && (!program || !program->screen_space);
    auto vertex=program?program->vertex.Get():r.vs.Get();
    auto pixel=simple<r.simple_combiners.size()?r.simple_combiners[simple].Get():r.combiners[state.combiner_control&255].Get();
    if(responsive) {
        const Float4 offset{jittered?r.jitter[0]*2/(r.width*r.resolution_scale):0,jittered?-r.jitter[1]*2/(r.height*r.resolution_scale):0,0,0};
        const std::array<Float4,2> values{offset,Float4{float(state.blend || !jittered),0,0,0}};
        if(!r.temporal_valid || values!=r.last_temporal) {
            D3D11_MAPPED_SUBRESOURCE mapped{};checked(r.context->Map(r.temporal_constants.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Temporal draw map");
            std::memcpy(mapped.pData,values.data(),sizeof(values));r.context->Unmap(r.temporal_constants.Get(),0);
            r.last_temporal=values;r.temporal_valid=true;
        }
        if(!bound.valid || !bound.temporal) {
            auto buffer=r.temporal_constants.Get();r.context->VSSetConstantBuffers(3,1,&buffer);r.context->PSSetConstantBuffers(3,1,&buffer);
        }
        if(jittered) {vertex=program?program->jitter_vertex.Get():r.jitter_vertex.Get();r.applied_jitter=r.jitter;}
        pixel=simple<r.responsive_simple.size()?r.responsive_simple[simple].Get():r.responsive_combiners[state.combiner_control&255].Get();
    }
    bound.temporal=responsive;
    if(!bound.valid || bound.vertex!=vertex) r.context->VSSetShader(vertex,nullptr,0);
    if(!bound.valid || bound.pixel!=pixel) r.context->PSSetShader(pixel,nullptr,0);
    const auto geometry=state.primitive==1?r.point_geometry.Get():nullptr;
    if(state.primitive==1) {
        if(state.point_scale && !program) throw std::runtime_error("Point attenuation requires an original vertex transform");
        const Float4 info{1.0f/r.width,1.0f/r.height,float(state.point_sprite),state.point_scale?-1.0f:std::max(1.0f,state.point_size)};
        if(!r.point_info_valid || info!=r.last_point_info) {
            D3D11_MAPPED_SUBRESOURCE mapped{};
            checked(r.context->Map(r.point_constants.Get(),0,D3D11_MAP_WRITE_DISCARD,0,&mapped),"Point constants map");
            std::memcpy(mapped.pData,info.data(),sizeof(info));r.context->Unmap(r.point_constants.Get(),0);
            r.last_point_info=info;r.point_info_valid=true;
        }
        auto buffer=r.point_constants.Get();r.context->GSSetConstantBuffers(4,1,&buffer);
    }
    if(!bound.valid || bound.geometry!=geometry) r.context->GSSetShader(geometry,nullptr,0);
    auto program_buffer=r.program_constants.Get();
    if(program) r.program_count=uniform_capacity(program->upload_count)/16;
    if(r.constant_context) {
        if(!bound.valid || bound.program_offset!=r.program_offset || bound.program_count!=r.program_count) {
            const UINT first=r.program_offset/16,count=r.program_count;
            r.constant_context->VSSetConstantBuffers1(1,1,&program_buffer,&first,&count);
        }
        if(!bound.valid || bound.factor_offset!=r.factor_offset) {
            const UINT first=r.factor_offset/16,count=((sizeof(Constants)+255)&~255U)/16;
            r.constant_context->PSSetConstantBuffers1(0,1,&constant_buffer,&first,&count);
        }
    } else if(!bound.valid) {
        r.context->VSSetConstantBuffers(1,1,&program_buffer);r.context->PSSetConstantBuffers(0,1,&constant_buffer);
    }
    bound.program_offset=r.program_offset;bound.factor_offset=r.factor_offset;bound.program_count=r.program_count;
    if(!bound.valid || bound.views!=views) r.context->PSSetShaderResources(0,static_cast<UINT>(views.size()),views.data());
    if(!bound.valid || bound.samplers!=samplers) r.context->PSSetSamplers(0,4,samplers.data());
    if(!bound.valid || bound.blend!=pipeline.blend.Get()) r.context->OMSetBlendState(pipeline.blend.Get(),nullptr,~0U);
    if(!bound.valid || bound.depth!=pipeline.depth.Get() || bound.stencil!=state.stencil_reference)
        r.context->OMSetDepthStencilState(pipeline.depth.Get(),state.stencil_reference);
    if(!bound.valid || bound.raster!=pipeline.raster.Get()) r.context->RSSetState(pipeline.raster.Get());
    if(state.scissor_test) {
        const auto scale=LONG(r.resolution_scale);
        const D3D11_RECT rect{state.scissor[0]*scale,state.scissor[1]*scale,state.scissor[2]*scale,state.scissor[3]*scale};
        if(rect.right<rect.left || rect.bottom<rect.top) throw std::runtime_error("Invalid scissor rectangle");
        if(!bound.valid || !bound.scissor_enabled || std::memcmp(&bound.scissor,&rect,sizeof(rect))) r.context->RSSetScissorRects(1,&rect);
        bound.scissor=rect;
    }
    bound.layout=input_layout;bound.topology=topology;bound.vertex=vertex;bound.pixel=pixel;bound.geometry=geometry;
    bound.views=views;bound.samplers=samplers;bound.blend=pipeline.blend.Get();bound.depth=pipeline.depth.Get();bound.raster=pipeline.raster.Get();
    bound.stencil=state.stencil_reference;bound.scissor_enabled=state.scissor_test;bound.valid=true;
    if(plan.count) {
        UINT index_offset=plan.offset;
        if(!batch) {
            PerfScope index_upload(r.profile,PerfPhase::buffer_upload);
            auto* destination=r.append_buffer(r.index_buffer.Get(),max_indices*sizeof(std::uint32_t),r.index_cursor,indices.size_bytes(),index_offset);
            std::memcpy(destination,indices.data(),indices.size_bytes());r.context->Unmap(r.index_buffer.Get(),0);
        }
        r.context->IASetIndexBuffer(r.index_buffer.Get(), DXGI_FORMAT_R32_UINT, index_offset);
    }
    const auto submit=[&] {if(plan.count) r.context->DrawIndexed(plan.count,0,0);else r.context->Draw(static_cast<UINT>(vertex_count),0);};
    r.visibility_scope(true);
    submit();
    if(state.color_mask&0x01000000U) r.targets.at(r.current_target).uniform_alpha=-1;
    if(layered) {
        // Additional MRT outputs can change point-sampled MSAA evaluation at
        // triangle boundaries. Keep the original draw's shader and attachments
        // identical; capture only its colour operation in an immediate second draw.
        r.visibility_scope(false);
        std::array<ID3D11RenderTargetView*,FrameLayers::basis_count> targets{};
        for(unsigned i=0;i<FrameLayers::basis_count;++i) targets[i]=r.layers->target(i);
        const auto capture=simple<r.layer_simple.size()?r.layer_simple[simple].Get():r.layer_combiners[state.combiner_control&255].Get();
        r.context->OMSetRenderTargets(unsigned(targets.size()),targets.data(),r.dsv.Get());r.context->PSSetShader(capture,nullptr,0);
        if(pipeline.layer_blend) r.context->OMSetBlendState(pipeline.layer_blend.Get(),nullptr,UINT_MAX);
        submit();const auto& output=r.targets.at(r.current_target);
        const std::array<ID3D11RenderTargetView*,2> original_targets{r.rtv.Get(),output.responsive_target.Get()};
        r.context->OMSetRenderTargets(output.responsive_target?2:1,original_targets.data(),r.dsv.Get());r.context->PSSetShader(pixel,nullptr,0);
        if(pipeline.layer_blend) r.context->OMSetBlendState(pipeline.blend.Get(),nullptr,UINT_MAX);
    }
    ++r.stats.draws; r.stats.vertices += count;r.count(PerfMetric::draws);r.count(PerfMetric::vertices,count);
    if(r.samples>1 && state.color_mask) r.targets.at(r.current_target).dirty=true;
}
void Renderer::end() {
    auto& r = *impl_;
    if (!r.recording) throw std::runtime_error("No frame to end");
    if(r.bound.geometry) {r.context->GSSetShader(nullptr,nullptr,0);r.bound.geometry=nullptr;}
    if(r.timing_index<r.timings.size()) {
        auto& timing=r.timings[r.timing_index];r.context->End(timing.finish.Get());r.context->End(timing.timer.Get());timing.pending=true;
    }
    r.recording = false; r.ended = true;
    if(r.gpu_meter) r.gpu_meter->end();
}
Image Renderer::read_target(std::uint32_t target) {
    const auto previous=impl_->current_target;
    set_target(target);
    try {auto image=readback();set_target(previous);return image;}
    catch(...) {set_target(previous);throw;}
}
Image Renderer::read_frame() {
    if(impl_->upscaled) {
        auto& r=*impl_;ComPtr<ID3D11Resource> source;
        source=r.upscaler->image();
        ComPtr<ID3D11Texture2D> texture;checked(source.As(&texture),"Published frame image");D3D11_TEXTURE2D_DESC desc{};texture->GetDesc(&desc);
        desc.Usage=D3D11_USAGE_STAGING;desc.BindFlags=desc.MiscFlags=0;desc.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        ComPtr<ID3D11Texture2D> staging;checked(r.device->CreateTexture2D(&desc,nullptr,&staging),"Upscaled frame readback");
        r.context->CopyResource(staging.Get(),texture.Get());
        D3D11_MAPPED_SUBRESOURCE mapped{};checked(r.context->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped),"Upscaled frame map");
        Image image{desc.Width,desc.Height,std::vector<std::byte>(std::size_t(desc.Width)*desc.Height*4)};
        for(unsigned y=0;y<desc.Height;++y) std::memcpy(image.rgba.data()+std::size_t(y)*desc.Width*4,
            static_cast<const std::byte*>(mapped.pData)+std::size_t(y)*mapped.RowPitch,std::size_t(desc.Width)*4);
        r.context->Unmap(staging.Get(),0);return image;
    }
    if(impl_->antialiasing && impl_->antialiasing->ready()) return impl_->antialiasing->readback();
    if(impl_->ambient_occlusion && impl_->ambient_occlusion->ready()) return impl_->ambient_occlusion->readback();
    return read_target(0);
}
Image Renderer::readback() {
    auto& r = *impl_;
    if (!r.ended || r.recording) throw std::runtime_error("End a frame before reading back");
    auto& output=r.targets.at(r.current_target);r.resolve(output);
    D3D11_TEXTURE2D_DESC description{};output.image->GetDesc(&description);
    auto& stored=r.targets.at(r.current_target).staging;
    if(!stored) {
        description.BindFlags=0;description.Usage=D3D11_USAGE_STAGING;description.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        checked(r.device->CreateTexture2D(&description,nullptr,&stored),"Explicit readback storage");
    }
    r.staging=stored;
    Image image{description.Width,description.Height,std::vector<std::byte>(std::size_t(description.Width)*description.Height*4)};
    r.context->CopyResource(r.staging.Get(),output.image.Get());
    D3D11_MAPPED_SUBRESOURCE mapped{};
    checked(r.context->Map(r.staging.Get(), 0, D3D11_MAP_READ, 0, &mapped), "Frame readback");
    const bool rgb565=r.targets.at(r.current_target).color_format==SurfaceColor::rgb565;
    for(unsigned y=0;y<image.height;++y) {
        const auto* row=static_cast<const std::byte*>(mapped.pData)+std::size_t(y)*mapped.RowPitch;
        auto* pixels=image.rgba.data()+std::size_t(y)*image.width*4;
        if(!rgb565) std::memcpy(pixels,row,std::size_t(image.width)*4);
        else for(unsigned x=0;x<image.width;++x) {
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
