#pragma once
#include "file.h"
#include <array>

namespace b2 {
using Float4 = std::array<float,4>;
using VertexTokens = std::array<std::array<std::uint32_t,4>,136>;
using VertexConstants = std::array<Float4,192>;
struct ProgramVertex { std::array<Float4,16> attributes{}; };
struct VertexProgram {
    VertexTokens tokens{};
    std::uint32_t start=0;
    auto operator<=>(const VertexProgram&) const = default;
};
struct FixedTransform {
    std::uint32_t skin=0;
    bool normalize=false,fog=false;
    std::uint32_t fog_source=6;
    std::array<std::array<std::uint32_t,4>,4> texgen{};
    std::array<bool,4> texture_matrix{};
    auto operator<=>(const FixedTransform&) const = default;
};
struct VertexTranslation {
    std::string source;
    std::array<std::uint32_t,13> output_masks{};
    std::uint32_t instructions=0;
    bool writes_context=false;
};
// Translation belongs to the native build tool. Gameplay selects bytecode.
VertexTranslation translate_vertex_program(const VertexProgram&,bool launch=false);
VertexProgram normalize_vertex_program(const VertexProgram&);
std::string fixed_transform_source(std::uint32_t skin);
void validate_fixed_transform(const FixedTransform&);
}
