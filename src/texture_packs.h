#pragma once
#include "mods.h"
#include "render.h"
#include "crypto.h"

namespace b2 {
inline constexpr std::size_t texture_pack_budget=512ULL*1024*1024;
struct PackImage {
    unsigned width,height,mip_levels;
    std::vector<std::byte> bytes;
    TextureData texture() const {return {width,height,TextureFormat::argb8_linear,bytes,mip_levels};}
};
std::string texture_id(const TextureData&);
ShaDigest texture_digest(const TextureData&);
PackImage load_pack_image(const std::filesystem::path&);
std::size_t validate_texture_packs(std::span<const ModFile>);
Mod install_texture_pack(const std::filesystem::path& source,const std::filesystem::path& mods_root);
std::string export_texture_pack(const std::filesystem::path& dictionary,const std::filesystem::path& directory,
                              std::string_view name,bool warp);
std::string check_texture_packs(const std::filesystem::path& directory,bool warp);
}
