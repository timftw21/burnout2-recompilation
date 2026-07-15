#pragma once

// NV2A vertex-program token decoding follows the public-domain
// xemu-project/nv2a_vsh_cpu field layout and execution semantics.

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>

struct Nv2aVertexProgramResult {
    std::array<std::array<float, 4>, 13> outputs{};
    std::array<uint32_t, 13> output_masks{};
    bool valid = false;
};

namespace nv2a_vsh {

using Vec4 = std::array<float, 4>;

inline uint32_t field(uint32_t value, uint32_t start, uint32_t length) {
    return (value >> start) & ((1u << length) - 1u);
}

inline Vec4 swizzle(
    const Vec4& value,
    uint32_t packed,
    bool negate) {
    Vec4 result{};
    for (uint32_t lane = 0; lane < 4; ++lane) {
        const uint32_t shift = 6u - lane * 2u;
        result[lane] = value[(packed >> shift) & 3u] * (negate ? -1.0f : 1.0f);
    }
    return result;
}

inline Vec4 execute_operation(
    uint32_t opcode,
    const Vec4& a,
    const Vec4& b,
    const Vec4& c) {
    Vec4 result{};
    switch (opcode) {
    case 1: result = a; break;
    case 2:
        for (uint32_t i = 0; i < 4; ++i) result[i] = a[i] * b[i];
        break;
    case 3:
        for (uint32_t i = 0; i < 4; ++i) result[i] = a[i] + b[i];
        break;
    case 4:
        for (uint32_t i = 0; i < 4; ++i) result[i] = a[i] * b[i] + c[i];
        break;
    case 5: {
        const float value = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
        result.fill(value);
        break;
    }
    case 6: {
        const float value = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + b[3];
        result.fill(value);
        break;
    }
    case 7: {
        const float value = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
        result.fill(value);
        break;
    }
    case 8: result = {1.0f, a[1] * b[1], a[2], b[3]}; break;
    case 9:
        for (uint32_t i = 0; i < 4; ++i) result[i] = std::min(a[i], b[i]);
        break;
    case 10:
        for (uint32_t i = 0; i < 4; ++i) result[i] = std::max(a[i], b[i]);
        break;
    case 11:
        for (uint32_t i = 0; i < 4; ++i) result[i] = a[i] < b[i] ? 1.0f : 0.0f;
        break;
    case 12:
        for (uint32_t i = 0; i < 4; ++i) result[i] = a[i] >= b[i] ? 1.0f : 0.0f;
        break;
    case 13: {
        const float value = std::floor(a[0] + 0.001f);
        result.fill(value);
        break;
    }
    case 14: {
        const float value = 1.0f / a[0];
        result.fill(value);
        break;
    }
    case 15: {
        const float reciprocal = 1.0f / a[0];
        const float value = std::clamp(reciprocal, -1.884467e19f, 1.884467e19f);
        result.fill(value);
        break;
    }
    case 16: {
        const float value = 1.0f / std::sqrt(std::abs(a[0]));
        result.fill(value);
        break;
    }
    case 17: {
        const float base = std::floor(a[0]);
        result = {std::exp2(base), a[0] - base, std::exp2(a[0]), 1.0f};
        break;
    }
    case 18: {
        const float value = std::abs(a[0]);
        const float exponent = std::floor(std::log2(value));
        result = {exponent, value / std::exp2(exponent), std::log2(value), 1.0f};
        break;
    }
    case 19:
        result = {
            1.0f,
            std::max(a[0], 0.0f),
            a[0] > 0.0f ? std::pow(std::max(a[1], 0.0f), a[3]) : 0.0f,
            1.0f,
        };
        break;
    default: break;
    }
    return result;
}

inline void write_mask(Vec4& destination, const Vec4& value, uint32_t mask) {
    if (mask & 8u) destination[0] = value[0];
    if (mask & 4u) destination[1] = value[1];
    if (mask & 2u) destination[2] = value[2];
    if (mask & 1u) destination[3] = value[3];
}

} // namespace nv2a_vsh

inline Nv2aVertexProgramResult execute_nv2a_vertex_program(
    const std::array<std::array<uint32_t, 4>, 136>& program,
    const std::array<std::array<uint32_t, 4>, 192>& constant_bits,
    uint32_t start,
    const std::array<std::array<float, 4>, 16>& inputs) {
    using namespace nv2a_vsh;
    Nv2aVertexProgramResult execution{};
    std::array<Vec4, 12> temporary{};
    std::array<Vec4, 192> constants{};
    for (uint32_t row = 0; row < constants.size(); ++row) {
        for (uint32_t lane = 0; lane < 4; ++lane) {
            std::memcpy(&constants[row][lane], &constant_bits[row][lane], sizeof(float));
        }
    }
    float address = 0.0f;

    auto fetch = [&](uint32_t type, uint32_t temporary_index, uint32_t input_index,
                     uint32_t constant_index, uint32_t packed_swizzle,
                     bool negate, bool relative) -> Vec4 {
        Vec4 value{};
        if (type == 1u) {
            value = temporary_index == 12u ? execution.outputs[0] : temporary[temporary_index];
        } else if (type == 2u) {
            value = inputs[input_index];
        } else if (type == 3u) {
            const int64_t index = static_cast<int64_t>(constant_index)
                + (relative ? static_cast<int64_t>(address) : 0);
            if (index < 0 || index >= static_cast<int64_t>(constants.size())) return {};
            value = constants[static_cast<size_t>(index)];
        }
        return swizzle(value, packed_swizzle, negate);
    };

    for (uint32_t instruction = start; instruction < program.size(); ++instruction) {
        const auto& token = program[instruction];
        const uint32_t mac_opcode = field(token[1], 21, 4);
        const uint32_t ilu_opcode = field(token[1], 25, 3);
        if (mac_opcode > 13u || ilu_opcode > 7u) return execution;
        const uint32_t input_index = field(token[1], 9, 4);
        const uint32_t constant_index = field(token[1], 13, 8);
        const bool relative = field(token[3], 1, 1) != 0;
        const Vec4 a = fetch(
            field(token[2], 26, 2), field(token[2], 28, 4), input_index,
            constant_index, field(token[1], 0, 8), field(token[1], 8, 1), relative);
        const Vec4 b = fetch(
            field(token[2], 11, 2), field(token[2], 13, 4), input_index,
            constant_index, field(token[2], 17, 8), field(token[2], 25, 1), relative);
        const uint32_t c_register = field(token[2], 0, 2) * 4u + field(token[3], 30, 2);
        const Vec4 c = fetch(
            field(token[3], 28, 2), c_register, input_index, constant_index,
            field(token[2], 2, 8), field(token[2], 10, 1), relative);

        Vec4 mac{};
        if (mac_opcode == 3u) mac = execute_operation(mac_opcode, a, c, {});
        else mac = execute_operation(mac_opcode, a, b, c);
        static constexpr std::array<uint32_t, 8> kIluOperations = {
            0u, 1u, 14u, 15u, 16u, 17u, 18u, 19u,
        };
        const Vec4 ilu = execute_operation(kIluOperations[ilu_opcode], c, {}, {});

        const uint32_t temporary_index = field(token[3], 20, 4);
        const uint32_t mac_mask = field(token[3], 24, 4);
        const uint32_t ilu_mask = field(token[3], 16, 4);
        if (mac_opcode == 13u) {
            address = mac[0];
        } else if (mac_mask && temporary_index < temporary.size()) {
            write_mask(temporary[temporary_index], mac, mac_mask);
        }
        if (ilu_mask) {
            const uint32_t ilu_temporary = mac_opcode ? 1u : temporary_index;
            if (ilu_temporary < temporary.size()) write_mask(temporary[ilu_temporary], ilu, ilu_mask);
        }

        const uint32_t output_mask = field(token[3], 12, 4);
        if (output_mask) {
            const Vec4& value = field(token[3], 2, 1) ? ilu : mac;
            const uint32_t output_index = field(token[3], 3, 8);
            if (field(token[3], 11, 1)) {
                if (output_index >= execution.outputs.size()) return execution;
                write_mask(execution.outputs[output_index], value, output_mask);
                execution.output_masks[output_index] |= output_mask;
            } else {
                if (output_index >= constants.size()) return execution;
                write_mask(constants[output_index], value, output_mask);
            }
        }
        if (field(token[3], 0, 1)) {
            execution.valid = true;
            return execution;
        }
    }
    return execution;
}
