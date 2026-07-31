#include "host_core_test_cases.h"
#include "nv2a/raster_coordinates.h"
#include "nv2a/texture_layout.h"

#include <cmath>
#include <cstdint>
#include <utility>
#include <vector>

namespace b2r::test {

void test_texture_layout_and_formats(TestContext& context) {
    using namespace b2r::nv2a;
    context.expect(
        nv2a_canonical_resource_address(0x21234567u) == 0x01234567u,
        "DMA alias canonicalization"
    );
    context.expect(
        nv2a_canonical_resource_address(0x11234567u) == 0x11234567u,
        "unaliased address preservation"
    );
    context.expect(nv2a_texture_format_matches("DXT1", 0x0C00u), "DXT1 format");
    context.expect(
        nv2a_texture_format_matches("format_2A", 0x2A00u),
        "unknown format fallback name"
    );
    context.expect(nv2a_texture_format_is_linear(0x1200u), "linear format flag");
    context.expect(!nv2a_texture_format_is_linear(0x0C00u), "swizzled format flag");
    context.expect(nv2a_texture_format_is_cubemap(0x0C04u), "cubemap format flag");
    context.expect(!nv2a_texture_format_is_cubemap(0x0C00u), "2D format flag");
    context.expect(
        nv2a_texture_uncompressed_bytes_per_pixel(0x0500u) == 2u,
        "RGB565 texel size"
    );
    context.expect(
        nv2a_texture_uncompressed_bytes_per_pixel(0x0600u) == 4u,
        "ARGB8888 texel size"
    );
    context.expect(
        nv2a_texture_uncompressed_bytes_per_pixel(0x0C00u) == 0u,
        "compressed texture has no uncompressed texel size"
    );
    context.expect(
        nv2a_texture_extent(0x1200u, (320u << 16u) | 240u)
            == std::pair<uint32_t, uint32_t>{320u, 240u},
        "linear image rectangle extent"
    );
    context.expect(
        nv2a_texture_extent((6u << 20u) | (5u << 24u), 0u)
            == std::pair<uint32_t, uint32_t>{64u, 32u},
        "logarithmic swizzled extent"
    );
}

void test_nv2a_raster_coordinates(TestContext& context) {
    using namespace b2r::nv2a;
    constexpr float kWidth = 640.0f;
    constexpr float kHeight = 480.0f;
    constexpr float kTolerance = 0.000001f;
    context.expect(
        std::abs(kNv2aToVulkanPixelCenterBias - 0.03125f) < kTolerance,
        "NV2A to Vulkan pixel-center bias"
    );
    context.expect(
        std::abs(nv2a_screen_coordinate_to_vulkan_ndc(
            kNv2aViewportSubpixelBias, static_cast<uint32_t>(kWidth))
            - (-1.0f + 1.0f / kWidth)) < kTolerance,
        "left NV2A viewport edge reaches the first Vulkan pixel center"
    );
    context.expect(
        std::abs(nv2a_screen_coordinate_to_vulkan_ndc(
            kNv2aViewportSubpixelBias + kHeight, static_cast<uint32_t>(kHeight))
            - (1.0f + 1.0f / kHeight)) < kTolerance,
        "bottom NV2A viewport edge remains beyond the last Vulkan pixel center"
    );
}

void test_texture_unswizzle(TestContext& context) {
    const std::vector<uint8_t> swizzled = {0u, 1u, 2u, 3u, 4u, 5u, 6u, 7u};
    const std::vector<uint8_t> linear = b2r::nv2a::nv2a_unswizzle_texture_2d(
        swizzled, 4u, 2u, 1u);
    context.expect(
        linear == std::vector<uint8_t>({0u, 1u, 4u, 5u, 2u, 3u, 6u, 7u}),
        "rectangular Morton unswizzle"
    );
    context.expect(
        b2r::nv2a::nv2a_unswizzle_texture_2d(swizzled, 4u, 2u, 0u)
            == std::vector<uint8_t>(swizzled.size(), 0u),
        "zero-sized pixel format is bounded"
    );
}

}  // namespace b2r::test
