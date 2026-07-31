#pragma once

#include <cstddef>
#include <cstdint>

namespace b2r::host {

inline bool native_pipeline_primitive_supported(uint32_t primitive) {
    return primitive == 2u
        || primitive == 5u
        || primitive == 6u;
}

struct NativePipelineState {
    uint32_t primitive = 6u;
    uint32_t blend_enable = 0u;
    uint32_t blend_source_factor = 1u;
    uint32_t blend_destination_factor = 0u;
    uint32_t blend_equation = 0x8006u;
    uint32_t color_mask = 0x01010101u;
    uint32_t depth_test_enable = 0u;
    uint32_t depth_function = 0x0203u;
    uint32_t depth_write_enable = 1u;
    uint32_t cull_face_enable = 0u;
    uint32_t cull_face = 0x0405u;
    uint32_t front_face = 0x0900u;
    bool raw_attribute_fetch = false;

    bool operator==(const NativePipelineState& other) const {
        return primitive == other.primitive
            && blend_enable == other.blend_enable
            && blend_source_factor == other.blend_source_factor
            && blend_destination_factor == other.blend_destination_factor
            && blend_equation == other.blend_equation
            && color_mask == other.color_mask
            && depth_test_enable == other.depth_test_enable
            && depth_function == other.depth_function
            && depth_write_enable == other.depth_write_enable
            && cull_face_enable == other.cull_face_enable
            && cull_face == other.cull_face
            && front_face == other.front_face
            && raw_attribute_fetch == other.raw_attribute_fetch;
    }

    bool operator!=(const NativePipelineState& other) const {
        return !(*this == other);
    }
};

struct NativePipelineStateHash {
    size_t operator()(const NativePipelineState& state) const noexcept {
        size_t value = 1469598103934665603ull;
        const auto mix = [&](uint32_t field) {
            value ^= static_cast<size_t>(field);
            value *= 1099511628211ull;
        };
        mix(state.primitive);
        mix(state.blend_enable);
        mix(state.blend_source_factor);
        mix(state.blend_destination_factor);
        mix(state.blend_equation);
        mix(state.color_mask);
        mix(state.depth_test_enable);
        mix(state.depth_function);
        mix(state.depth_write_enable);
        mix(state.cull_face_enable);
        mix(state.cull_face);
        mix(state.front_face);
        mix(state.raw_attribute_fetch ? 1u : 0u);
        return value;
    }
};

}  // namespace b2r::host
