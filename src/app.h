#pragma once
#include <filesystem>
#include <cstdint>
#include <span>
#include <string>

namespace b2 {
struct MemorySlice {std::uint32_t address,size;};
int run_app(const std::filesystem::path& xbe={},const std::filesystem::path& disc={},unsigned seconds=0,
            const std::filesystem::path& frame={},const std::filesystem::path& controls={},std::span<const MemorySlice> memory={},bool trace_io=false);
std::string check_window(const std::filesystem::path& image,bool warp);
}
