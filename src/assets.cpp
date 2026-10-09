#include "assets.h"
#include <algorithm>
#include <format>
#include <stdexcept>

namespace b2 {
namespace {
struct Chunk { std::uint64_t data, end; };
Chunk chunk(File& file, std::uint64_t offset, std::uint64_t end, std::uint32_t expected) {
    if (offset > end || end - offset < 12) throw std::runtime_error("Truncated RenderWare chunk header");
    const auto header = file.read(offset,12);
    const auto length = u32(header,4);
    if (u32(header,0) != expected) throw std::runtime_error("Unexpected RenderWare chunk at " + std::to_string(offset));
    if (length > end - offset - 12) throw std::runtime_error("RenderWare chunk exceeds parent");
    return {offset+12,offset+12+length};
}
std::string name(Bytes bytes, unsigned offset) {
    const auto data = bytes.subspan(offset,32);
    const auto zero = std::ranges::find(data,std::byte{});
    if (zero == data.end()) throw std::runtime_error("Texture name is not terminated");
    return {reinterpret_cast<const char*>(data.data()),static_cast<std::size_t>(zero-data.begin())};
}
}
TextureDictionary::TextureDictionary(const std::filesystem::path& path) : file_(path) {
    // Standard RW Xbox layout, checked against Burnout 2's native dictionary headers.
    // https://github.com/aap/rwtools/blob/master/src/txdread.cpp (readXbox)
    if (file_.size() < 28 || file_.size() > 256 * 1024 * 1024)
        throw std::runtime_error("Dictionary size outside 28 bytes..256 MiB");
    const auto prefix = file_.read(0,16);
    const bool wrapped = u32(prefix,0) != 0x16 && u32(prefix,4) == 0x16;
    const auto root = chunk(file_,wrapped?4:0,file_.size(),0x16);
    if (root.end != file_.size()) throw std::runtime_error("Trailing data after texture dictionary");
    const auto structure = chunk(file_,root.data,root.end,1);
    if (structure.end-structure.data != 4) throw std::runtime_error("Unexpected dictionary count structure");
    const auto counts = file_.read(structure.data,4);
    const auto count = u16(counts,0);
    if (count > 4096 || u16(counts,2) != 0 || (wrapped && u32(prefix,0) != count))
        throw std::runtime_error("Unsupported dictionary count/device wrapper");
    auto offset = structure.end;
    textures_.reserve(count);
    for (unsigned i=0;i<count;++i) {
        const auto native = chunk(file_,offset,root.end,0x15);
        const auto header_chunk = chunk(file_,native.data,native.end,1);
        if (header_chunk.end-header_chunk.data < 92) throw std::runtime_error("Truncated Xbox texture header");
        const auto header = file_.read(header_chunk.data,92);
        if (u32(header,0) != 5) throw std::runtime_error("Texture platform is not original Xbox");
        NativeTexture texture{name(header,8),name(header,40),u16(header,80),u16(header,82),
            unsigned(header[85]),unsigned(header[87]),u32(header,72),u32(header,4),
            header_chunk.data+92,u32(header,88)};
        if (texture.data_size > 64 * 1024 * 1024 || texture.data_size != header_chunk.end-texture.data_offset)
            throw std::runtime_error("Texture size does not match its native chunk: " + texture.name);
        if (!texture.width || !texture.height || texture.width > 4096 || texture.height > 4096 || !texture.mip_levels || texture.mip_levels > 13)
            throw std::runtime_error("Invalid texture dimensions/mip count: " + texture.name);
        // Uncompressed RW textures encode the color layout in rasterFormat.
        if (!texture.format) {
            switch (texture.raster_format & 0xF00) {
            case 0x200: texture.format=0x05; break;
            case 0x500: texture.format=0x06; break;
            case 0x600: texture.format=0x07; break;
            default: break; // Explicit unsupported format remains in the inventory.
            }
        }
        const auto extension = chunk(file_,header_chunk.end,native.end,3);
        if (extension.end != native.end) throw std::runtime_error("Trailing native texture data");
        textures_.push_back(std::move(texture));
        offset = native.end;
    }
    const auto extension = chunk(file_,offset,root.end,3);
    if (extension.end != root.end) throw std::runtime_error("Trailing texture dictionary chunks");
}
std::vector<std::byte> TextureDictionary::pixels(const NativeTexture& texture) {
    return file_.read(texture.data_offset,texture.data_size);
}
std::string TextureDictionary::report() const {
    std::string output = "{\"format\":\"b2-texture-dictionary-v1\",\"textures\":[";
    for (std::size_t i=0;i<textures_.size();++i) {
        if (i) output+=',';
        const auto& t=textures_[i];
        output+=std::format("{{\"name\":{},\"width\":{},\"height\":{},\"mip_levels\":{},\"nv097_format\":{},\"raster_format\":{},\"filter_flags\":{},\"data_offset\":{},\"data_bytes\":{}}}",
            json(t.name),t.width,t.height,t.mip_levels,t.format,t.raster_format,t.filter_flags,t.data_offset,t.data_size);
    }
    return output+"]}";
}
}
