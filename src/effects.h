#pragma once
#include "file.h"
#include <array>

namespace b2 {
struct EffectProgram {
    std::uint32_t file_offset=0;
    std::array<std::uint32_t,8> descriptor{};
    std::vector<std::byte> code;
};
struct EffectsImage {
    std::uint32_t program_words=0;
    std::uint32_t data_file_offset=0,descriptor_flags=0;
    std::vector<std::uint32_t> data;
    std::vector<EffectProgram> effects;
};
EffectsImage decode_effects(Bytes image);
}
