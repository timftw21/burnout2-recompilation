#pragma once
#include <filesystem>
#include <string_view>

namespace b2 {
struct Cpu;
void initialize_diagnostics() noexcept;
void diagnostic_record(std::string_view json_record) noexcept;
void diagnostic_guest(const Cpu*) noexcept;
const std::filesystem::path& diagnostic_log() noexcept;
}
