#pragma once
#include "file.h"

namespace b2 {
struct NativeTexture {
    std::string name, mask;
    std::uint32_t width, height, mip_levels, format, raster_format, filter_flags;
    std::uint64_t data_offset;
    std::uint32_t data_size;
};
class TextureDictionary {
public:
    explicit TextureDictionary(const std::filesystem::path&);
    std::span<const NativeTexture> textures() const { return textures_; }
    std::vector<std::byte> pixels(const NativeTexture& texture);
    std::string report() const;
private:
    File file_;
    std::vector<NativeTexture> textures_;
};
}
