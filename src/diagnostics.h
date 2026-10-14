#pragma once
#include <filesystem>
#include <cstdint>
#include <string>
#include <string_view>

namespace b2 {
struct Cpu;
void initialize_diagnostics() noexcept;
void diagnostic_record(std::string_view json_record) noexcept;
void diagnostic_guest(const Cpu*) noexcept;
const std::filesystem::path& diagnostic_log() noexcept;
void diagnostic_watch(unsigned slot,void* address);
std::string check_memory_watch(bool crash=false);
}
