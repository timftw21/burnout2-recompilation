#include "texture_packs.h"
#include "mod_scripts.h"
#include "assets.h"
#include "crypto.h"
#include <d3d11.h>
#include <algorithm>
#include <array>
#include <bit>
#include <chrono>
#include <format>

namespace b2 {
ShaDigest texture_digest(const TextureData& texture) {
    // Versioned, little-endian metadata prevents different layouts sharing an id.
    const std::array fields{1U,texture.width,texture.height,std::uint32_t(texture.format),
        texture.mip_levels,texture.pitch,std::uint32_t(texture.cubemap),texture.depth};
    std::array<std::byte,32> header{};
    for(unsigned i=0;i<fields.size();++i) for(unsigned lane=0;lane<4;++lane) header[i*4+lane]=std::byte((fields[i]>>(lane*8))&255);
    Sha1 hash;hash.reset();hash.update(header);hash.update(texture.bytes);
    return hash.finish();
}
std::string texture_id(const TextureData& texture) {
    auto id=hex_bytes(texture_digest(texture));
    for(auto& ch:id) if(ch>='A' && ch<='F') ch+='a'-'A';
    return id;
}
PackImage load_pack_image(const std::filesystem::path& path) {
    File file(path);
    if(file.size()<33 || file.size()>64*1024*1024) throw std::runtime_error("Replacement PNG must be 33 bytes..64 MiB: "+path_text(path.filename()));
    constexpr std::array<std::byte,8> signature{std::byte{137},std::byte{80},std::byte{78},std::byte{71},std::byte{13},std::byte{10},std::byte{26},std::byte{10}};
    const auto header=file.read(0,33);
    if(!std::ranges::equal(Bytes(header).first(8),signature) || u32(header,8)!=0x0D000000 || u32(header,12)!=0x52444849)
        throw std::runtime_error("Texture replacements must be PNG images: "+path_text(path.filename()));
    const auto width=std::byteswap(u32(header,16)),height=std::byteswap(u32(header,20));
    if(!width || !height || width>4096 || height>4096) throw std::runtime_error("Replacement PNG dimensions must be 1..4096: "+path_text(path.filename()));
    auto image=load_image(path);
    PackImage result{image.width,image.height,unsigned(std::bit_width(std::max(image.width,image.height))),{}};
    std::size_t size=0;
    for(unsigned w=image.width,h=image.height;;w=std::max(1U,w/2),h=std::max(1U,h/2)) {
        size+=std::size_t(w)*h*4;if(w==1 && h==1) break;
    }
    image.rgba.reserve(size);
    // The native renderer accepts BGRA. Prepare every mip before starting play.
    for(std::size_t i=0;i<image.rgba.size();i+=4) std::swap(image.rgba[i],image.rgba[i+2]);
    result.bytes=std::move(image.rgba);result.bytes.resize(size);
    unsigned w=result.width,h=result.height;std::size_t previous=0,next=std::size_t(w)*h*4;
    while(w>1 || h>1) {
        const auto dw=std::max(1U,w/2),dh=std::max(1U,h/2);
        for(unsigned y=0;y<dh;++y) for(unsigned x=0;x<dw;++x) {
            unsigned alpha=0,count=0;std::array<unsigned,3> rgb{};
            for(unsigned sy=y*h/dh;sy<(y+1)*h/dh;++sy) for(unsigned sx=x*w/dw;sx<(x+1)*w/dw;++sx) {
                const auto* pixel=result.bytes.data()+previous+(std::size_t(sy)*w+sx)*4;
                const auto a=unsigned(pixel[3]);alpha+=a;++count;
                for(unsigned lane=0;lane<3;++lane) rgb[lane]+=unsigned(pixel[lane])*a;
            }
            auto* pixel=result.bytes.data()+next+(std::size_t(y)*dw+x)*4;
            for(unsigned lane=0;lane<3;++lane) pixel[lane]=std::byte(alpha?(rgb[lane]+alpha/2)/alpha:0);
            pixel[3]=std::byte((alpha+count/2)/count);
        }
        previous=next;next+=std::size_t(dw)*dh*4;w=dw;h=dh;
    }
    return result;
}
std::size_t validate_texture_packs(std::span<const ModFile> files) {
    if(files.size()>2048) throw std::runtime_error("Enabled texture packs exceed 2048 replacement images.");
    std::size_t bytes=0;
    for(const auto& file:files) {
        try {const auto image=load_pack_image(file.source);bytes+=image.bytes.size();}
        catch(const std::exception& error) {throw std::runtime_error("Texture pack "+file.mod+": "+error.what());}
        if(bytes>texture_pack_budget) throw std::runtime_error("Enabled texture packs exceed 512 MiB. Disable a pack or use smaller images.");
    }
    return bytes;
}
namespace {
void pack_directory(const std::filesystem::path& path) {
    if(!std::filesystem::is_directory(path) || std::filesystem::weakly_canonical(path)!=path)
        throw std::runtime_error("Texture pack storage must be a real folder without links or junctions.");
}
void remove_staging(const std::filesystem::path& path,const std::filesystem::path& root) {
    // Only our uniquely named, direct children of the verified mods folder.
    if(path.parent_path()!=root || !path_text(path.filename()).starts_with(".texture-pack-") ||
       std::filesystem::weakly_canonical(path)!=path) return;
    std::error_code ignored;std::filesystem::remove_all(path,ignored);
}
std::array<Vertex,4> pack_quad() {
    std::array<Vertex,4> vertices{};
    constexpr Float4 positions[]={{-1,1,0,1},{1,1,0,1},{1,-1,0,1},{-1,-1,0,1}};
    constexpr Float4 coordinates[]={{0,0,0,1},{1,0,0,1},{1,1,0,1},{0,1,0,1}};
    for(unsigned i=0;i<4;++i) {vertices[i].position=positions[i];vertices[i].uv[0]=coordinates[i];}
    return vertices;
}
Image pack_preview(Renderer& renderer,std::uint32_t texture,unsigned width,unsigned height,std::uint32_t control=0x0003FFC0) {
    RenderState state;state.primitive=8;state.textures[0]=texture;state.texture_modes[0]=1;state.final_inputs={8,0x1800};
    state.texture_control[0]=control;
    renderer.resize(width,height);renderer.prepare(state);renderer.begin(0);renderer.draw(state,pack_quad());renderer.end();
    return renderer.readback();
}
}
Mod install_texture_pack(const std::filesystem::path& source,const std::filesystem::path& mods_root) {
    const auto pack=read_texture_pack(source);
    if(pack.files.size()>2048) throw std::runtime_error("A texture pack can contain at most 2048 PNGs.");
    const auto root=std::filesystem::absolute(mods_root).lexically_normal();
    if(!std::filesystem::exists(root)) {pack_directory(root.parent_path());std::filesystem::create_directory(root);}
    pack_directory(root);
    const auto destination=root/pack.id;
    const bool replacing=std::filesystem::exists(std::filesystem::symlink_status(destination));
    if(replacing) {
        pack_directory(destination);
        if(read_texture_pack(destination,true).id!=pack.id) throw std::runtime_error("The destination belongs to a different texture pack.");
    }
    if(std::filesystem::absolute(source).lexically_normal()==destination)
        throw std::runtime_error("This pack is already installed. Enable it in the list below.");
    const auto token=std::chrono::steady_clock::now().time_since_epoch().count();
    const auto staging=root/std::format(".texture-pack-{}.tmp",token),backup=root/std::format(".texture-pack-{}.backup",token);
    if(!std::filesystem::create_directory(staging)) throw std::runtime_error("Cannot create texture pack staging folder.");
    bool moved=false;
    try {
        std::filesystem::create_directory(staging/"textures");
        File manifest(std::filesystem::absolute(source).lexically_normal()/"mod.cfg");
        if(manifest.size()>8192) throw std::runtime_error("Texture pack manifest changed during installation. Try again.");
        copy(manifest,0,manifest.size(),staging/"mod.cfg");
        if(pack.script) {
            File script(pack.script->source);
            if(script.size()!=pack.script->size) throw std::runtime_error("Texture pack script changed during installation. Try again.");
            copy(script,0,script.size(),staging/"startup.lua");
        }
        std::size_t bytes=0;
        for(const auto& file:pack.files) {
            File input(file.source);
            if(input.size()!=file.size) throw std::runtime_error("Texture pack image changed during installation. Try again.");
            const auto output=staging/"textures"/file.path;copy(input,0,input.size(),output);
            const auto image=load_pack_image(output);bytes+=image.bytes.size();
            if(bytes>texture_pack_budget) throw std::runtime_error("Texture pack exceeds 512 MiB after image preparation. Use smaller images or split the pack.");
        }
        // Recheck the copied manifest and filenames before publishing anything.
        const auto installed=read_texture_pack(staging);
        if(installed.id!=pack.id) throw std::runtime_error("Texture pack changed during installation. Try again.");
        if(installed.script) run_startup_scripts(std::span(&*installed.script,1));
        if(replacing) {std::filesystem::rename(destination,backup);moved=true;}
        try {
            std::filesystem::rename(staging,destination);
            try {discover_mods(root);}
            catch(...) {std::filesystem::rename(destination,staging);throw;}
        }
        catch(...) {if(moved) {std::filesystem::rename(backup,destination);moved=false;}throw;}
        if(moved) remove_staging(backup,root);
    } catch(...) {remove_staging(staging,root);throw;}
    return read_texture_pack(destination);
}
std::string export_texture_pack(const std::filesystem::path& input,const std::filesystem::path& directory,std::string_view name,bool warp) {
    TextureDictionary dictionary(input);Renderer renderer(64,64,warp);
    create_mod(directory,name,true);
    std::string report="{\"format\":\"b2-texture-pack-export-v1\",\"textures\":[";unsigned count=0;
    for(const auto& texture:dictionary.textures()) {
        if(texture.raster_format&0x6000) throw std::runtime_error("Paletted textures are not supported: "+texture.name);
        const auto bytes=dictionary.pixels(texture);
        const TextureData source{texture.width,texture.height,static_cast<TextureFormat>(texture.format),bytes,texture.mip_levels};
        const auto id=texture_id(source);const auto output=directory/"textures"/(id+".png");
        const auto resource=renderer.upload(source);
        const auto image=pack_preview(renderer,resource,source.width,source.height);renderer.release(resource);
        if(!std::filesystem::exists(output)) save_png(image,output);
        if(count++) report+=',';
        report+=std::format("{{\"name\":{},\"id\":{},\"width\":{},\"height\":{}}}",json(texture.name),json(id),source.width,source.height);
    }
    report+="],\"game_booted\":false}";write_text(directory/"index.json",report+'\n',false);return report;
}
std::string check_texture_packs(const std::filesystem::path& directory,bool warp) {
    if(std::filesystem::exists(directory)) throw std::runtime_error("Choose a fresh texture pack diagnostic directory.");
    std::filesystem::create_directories(directory/"sources");
    unsigned checks=0;
    const auto require=[&](bool passed,std::string_view name) {
        if(!passed) throw std::runtime_error("Texture pack check failed: "+std::string(name));++checks;
    };
    const auto rejects=[&](auto operation,std::string_view name) {
        bool failed=false;try {operation();} catch(const std::exception&) {failed=true;}require(failed,name);
    };
    const auto solid=[](unsigned w,unsigned h,std::array<unsigned,4> color) {
        Image image{w,h,std::vector<std::byte>(std::size_t(w)*h*4)};
        for(std::size_t i=0;i<image.rgba.size();++i) image.rgba[i]=std::byte(color[i%4]);return image;
    };
    // Original 4x4 BGRA green; replacement is 16x16 translucent red.
    const auto original=solid(4,4,{0,255,0,255});
    const TextureData source{4,4,TextureFormat::argb8_linear,original.rgba};const auto id=texture_id(source);
    auto changed=source;changed.width=2;changed.height=8;
    require(id.size()==40 && id!=texture_id(changed),"identity includes original layout");
    const auto author=directory/"sources/preview";create_mod(author,"Preview pack",true);
    const auto png=id+".png";save_png(solid(16,16,{255,0,0,128}),author/"textures"/png);
    const auto download=directory/"sources"/utf8_path("Texture Pack 1.0 - \xE6\x97\xA5\xE6\x9C\xAC");
    std::filesystem::rename(author,download);
    const auto root=directory/"mods";auto installed=install_texture_pack(download,root);
    require(installed.id=="preview" && installed.texture_pack && installed.files.size()==1,"folder picker accepts a renamed Unicode download folder");
    require(discover_mods(root).size()==1,"installed pack is visible in mods");
    const auto before=validate_texture_packs(installed.files);
    require(before==1364,"full mip chain has bounded, reported memory use");
    Renderer renderer(16,16,warp,render_debug_layer);renderer.clean_hud(true);
    const auto started=std::chrono::steady_clock::now();renderer.texture_packs(installed.files);
    const auto prepare_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-started).count();
    const auto resource=renderer.upload(source),alias=renderer.upload(source);
    require(resource==alias && renderer.resources().texture_bytes==before,"cached originals share one preloaded replacement");
    renderer.release(resource);renderer.release(alias);
    const auto held=root/"preview-held";std::filesystem::rename(root/"preview",held);
    const auto reused=renderer.upload(source);const auto image=pack_preview(renderer,reused,16,16,0x0003FFD0);
    const auto sampler_strength=[&](unsigned strength) {
        Microsoft::WRL::ComPtr<ID3D11SamplerState> sampler;renderer.native_context()->PSGetSamplers(0,1,sampler.GetAddressOf());
        D3D11_SAMPLER_DESC description{};if(sampler) sampler->GetDesc(&description);
        require(sampler && description.Filter==D3D11_FILTER_ANISOTROPIC && description.MaxAnisotropy==strength,
            "pack filtering respects original anisotropy and the user's override");
    };
    sampler_strength(2);renderer.enhancements(0,16);pack_preview(renderer,reused,16,16,0x0003FFD0);sampler_strength(16);
    renderer.enhancements(0,0);renderer.release(reused);
    require(unsigned(image.rgba[0])==255 && unsigned(image.rgba[1])==0 && unsigned(image.rgba[3])==128,
        "higher resolution and alpha survive source release without disk reads");
    save_png(image,directory/"replacement.png");std::filesystem::rename(held,root/"preview");
    const auto fallback=renderer.upload(changed);const auto fallback_image=pack_preview(renderer,fallback,16,16);renderer.release(fallback);
    require(unsigned(fallback_image.rgba[0])==0 && unsigned(fallback_image.rgba[1])==255,"unmatched texture keeps original pixels");
    save_png(fallback_image,directory/"original.png");
    renderer.reset(1);require(renderer.resources().texture_bytes==0,"restart releases pinned pack resources");
    const auto restored=renderer.upload(source);const auto restored_image=pack_preview(renderer,restored,16,16);renderer.release(restored);
    require(unsigned(restored_image.rgba[1])==255,"disabling a pack restores the original texture");
    const auto file_hash=[](const std::filesystem::path& path) {File file(path);return hash(file,0,file.size());};
    const auto saved_hash=file_hash(root/"preview/textures"/png);
    write_text(download/"textures"/png,"broken PNG",true);
    rejects([&]{install_texture_pack(download,root);},"invalid update rejected");
    require(file_hash(root/"preview/textures"/png)==saved_hash && discover_mods(root).size()==1,"failed update preserves the installed pack and cleans staging");
    save_png(solid(8,8,{0,0,255,255}),download/"textures"/png,true);
    installed=install_texture_pack(download,root);const auto updated=load_pack_image(installed.files[0].source);
    require(updated.width==8 && unsigned(updated.bytes[0])==255 && discover_mods(root).size()==1,"valid update atomically replaces the installed pack");
    const auto bad=directory/"bad.png";write_text(bad,"invalid",false);
    auto broken=installed.files[0];broken.path=std::string(40,'0')+".png";broken.source=bad;
    const std::array files{installed.files[0],broken};
    rejects([&]{renderer.texture_packs(files);},"failed preparation rejected");
    require(renderer.resources().texture_bytes==0,"failed preparation releases every previously loaded image");
    const auto alpha_path=directory/"alpha.png";
    auto alpha=solid(2,2,{255,0,0,0});alpha.rgba[0]=std::byte{};alpha.rgba[2]=std::byte{255};alpha.rgba[3]=std::byte{255};
    save_png(alpha,alpha_path);const auto filtered=load_pack_image(alpha_path);
    require(unsigned(filtered.bytes[16])==255 && unsigned(filtered.bytes[18])==0 && unsigned(filtered.bytes[19])==64,
        "transparent pixels do not bleed into smaller mip colors");
    File png_input(alpha_path);auto oversized=png_input.read(0,static_cast<std::size_t>(png_input.size()));
    oversized[16]=std::byte{0};oversized[17]=std::byte{0};oversized[18]=std::byte{0x20};oversized[19]=std::byte{0};
    write_text(bad,{reinterpret_cast<const char*>(oversized.data()),oversized.size()},true);
    rejects([&]{load_pack_image(bad);},"oversized image rejected before decoding");
    write_text(download/"textures/not-a-texture.dds","unsupported",false);
    rejects([&]{read_texture_pack(download);},"unsupported image names and formats rejected");
    std::filesystem::remove(download/"textures/not-a-texture.dds");
    std::string wrong;
    {File manifest(download/"mod.cfg");const auto text=manifest.read(0,static_cast<std::size_t>(manifest.size()));
     wrong.assign(reinterpret_cast<const char*>(text.data()),text.size());}
    wrong.replace(wrong.find("id=preview"),10,"id=../escape");
    write_text(download/"mod.cfg",wrong,true);
    rejects([&]{install_texture_pack(download,root);},"pack id cannot escape mods folder");
    require(renderer.errors().empty(),"native GPU validation");
    const auto report=std::format("{{\"format\":\"b2-texture-pack-check-v1\",\"passed\":true,\"game_booted\":false,\"checks\":{},\"adapter\":{},\"prepare_ms\":{:.3f},\"texture_id\":{},\"prepared_bytes\":{}}}",
        checks,json(renderer.adapter()),prepare_ms,json(id),before);
    write_text(directory/"texture-packs-check.json",report+'\n',false);return report;
}
}
