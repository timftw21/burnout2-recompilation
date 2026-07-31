#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>
#include <utility>
#include <vector>

namespace b2r::nv2a {

inline uint32_t nv2a_canonical_resource_address(uint32_t address) {
    return (address & 0xF0000000u) == 0x20000000u
        ? address & ~0x20000000u
        : address;
}

inline bool nv2a_texture_format_matches(
    std::string_view format,
    uint32_t format_raw
) {
    switch ((format_raw >> 8u) & 0xFFu) {
    case 0x05u: return format == "R5G6B5";
    case 0x06u: return format == "A8R8G8B8";
    case 0x07u: return format == "X8R8G8B8";
    case 0x0Cu: return format == "DXT1";
    case 0x0Eu: return format == "DXT3";
    case 0x0Fu: return format == "DXT5";
    case 0x12u: return format == "A8R8G8B8_LINEAR";
    case 0x1Eu: return format == "X8R8G8B8_LINEAR";
    default:
        constexpr char hexadecimal[] = "0123456789ABCDEF";
        const uint32_t color_format = (format_raw >> 8u) & 0xFFu;
        std::array<char, 10> name{
            'f', 'o', 'r', 'm', 'a', 't', '_', '0', '0', '\0'};
        name[7] = hexadecimal[(color_format >> 4u) & 0xFu];
        name[8] = hexadecimal[color_format & 0xFu];
        return format == name.data();
    }
}

inline bool nv2a_texture_format_is_linear(uint32_t format_raw) {
    const uint32_t color_format = (format_raw >> 8u) & 0xFFu;
    return color_format == 0x12u || color_format == 0x1Eu;
}

inline bool nv2a_texture_format_is_cubemap(uint32_t format_raw) {
    return (format_raw & (1u << 2u)) != 0u;
}

inline uint32_t nv2a_texture_uncompressed_bytes_per_pixel(
    uint32_t format_raw
) {
    switch ((format_raw >> 8u) & 0xFFu) {
    case 0x05u: return 2u;
    case 0x06u:
    case 0x07u:
    case 0x12u:
    case 0x1Eu:
        return 4u;
    default:
        return 0u;
    }
}

inline std::pair<uint32_t, uint32_t> nv2a_texture_extent(
    uint32_t format_raw,
    uint32_t image_rect_raw
) {
    if (nv2a_texture_format_is_linear(format_raw) && image_rect_raw != 0u) {
        return {
            (image_rect_raw >> 16u) & 0xFFFFu,
            image_rect_raw & 0xFFFFu,
        };
    }
    return {
        1u << ((format_raw >> 20u) & 0xFu),
        1u << ((format_raw >> 24u) & 0xFu),
    };
}

inline std::vector<uint8_t> nv2a_unswizzle_texture_2d(
    const std::vector<uint8_t>& payload,
    uint32_t width,
    uint32_t height,
    uint32_t bytes_per_pixel
) {
    std::vector<uint8_t> linear(payload.size(), 0u);
    if (bytes_per_pixel == 0u) {
        return linear;
    }
    uint32_t mask_x = 0u;
    uint32_t mask_y = 0u;
    uint32_t dimension_bit = 1u;
    uint32_t mask_bit = 1u;
    bool done = false;
    while (!done) {
        done = true;
        if (dimension_bit < width) {
            mask_x |= mask_bit;
            mask_bit <<= 1u;
            done = false;
        }
        if (dimension_bit < height) {
            mask_y |= mask_bit;
            mask_bit <<= 1u;
            done = false;
        }
        dimension_bit <<= 1u;
    }
    uint32_t offset_y = 0u;
    for (uint32_t y = 0u; y < height; ++y) {
        uint32_t offset_x = 0u;
        for (uint32_t x = 0u; x < width; ++x) {
            const size_t source = static_cast<size_t>(offset_x + offset_y)
                * bytes_per_pixel;
            const size_t destination = (static_cast<size_t>(y) * width + x)
                * bytes_per_pixel;
            if (source + bytes_per_pixel <= payload.size()
                && destination + bytes_per_pixel <= linear.size()) {
                std::copy_n(
                    payload.begin() + static_cast<std::ptrdiff_t>(source),
                    bytes_per_pixel,
                    linear.begin() + static_cast<std::ptrdiff_t>(destination));
            }
            offset_x = (offset_x - mask_x) & mask_x;
        }
        offset_y = (offset_y - mask_y) & mask_y;
    }
    return linear;
}

}  // namespace b2r::nv2a
