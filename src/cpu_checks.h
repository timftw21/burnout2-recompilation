#pragma once
#include <cstdint>
#include <filesystem>

namespace b2 {
struct CpuChecks { unsigned instruction_cases = 0, game_cases = 0, memory_cases = 0, floating_cases = 0, control_cases = 0; };
CpuChecks check_cpu_batch();
unsigned check_cpu_image(const std::filesystem::path&);
struct CpuTiming { double ns_per_call; std::uint64_t checksum; };
CpuTiming benchmark_call_chain(std::uint32_t iterations);
struct MatrixTiming {CpuTiming fused,unfused;};
MatrixTiming benchmark_matrix(std::uint32_t iterations);
}
