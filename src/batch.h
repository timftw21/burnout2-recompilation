#pragma once
#include "recompiler.h"

namespace b2 {
struct Batch {
    std::string source, assembly, report, header;
    std::vector<std::string> shards;
    bool complete = false;
    std::size_t discovered = 0, compiled = 0, instructions = 0;
};
Batch recompile_batch(Xbe& image, std::span<const std::uint32_t> seeds,
                      std::uint32_t function_limit, std::uint32_t instruction_limit,
                      std::uint32_t shard_count = 0, std::string_view header_name = {});
}
