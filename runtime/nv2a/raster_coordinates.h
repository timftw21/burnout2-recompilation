#pragma once

#include <cstdint>

namespace b2r::nv2a {

constexpr float kNv2aViewportSubpixelBias = 0.53125f;
constexpr float kVulkanPixelCenter = 0.5f;
constexpr float kNv2aToVulkanPixelCenterBias =
    kNv2aViewportSubpixelBias - kVulkanPixelCenter;

constexpr float nv2a_screen_coordinate_to_vulkan_ndc(
    float coordinate,
    uint32_t target_extent) {
    return (coordinate - kNv2aToVulkanPixelCenterBias) * 2.0f
        / static_cast<float>(target_extent) - 1.0f;
}

}  // namespace b2r::nv2a
