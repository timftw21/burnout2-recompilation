#pragma once
#include "cpu.h"
#include <filesystem>
#include <initializer_list>
#include <string>

namespace b2 {
struct SpatialListener {
    std::array<float,3> position{},velocity{},front{0,0,1},up{0,1,0};
    float distance=1,rolloff=1,doppler=1;
};
// Original 76-byte Xbox DS3DBUFFER, including its three per-source factors.
using SpatialParameters=std::array<std::uint32_t,19>;
using EnvironmentParameters=std::array<std::uint32_t,9>;
struct SpatialResult {
    float gain=1;
    std::array<float,2> pan{1,1};
    std::array<float,2> front_back{1,0},environment_gain{1,0},environment_pole{};
    double pitch=1;
    float distance=0;
    std::array<float,2> angles{};
    std::array<std::int32_t,3> adjustments{};
};
class Spatial {
public:
    explicit Spatial(Memory&);
    SpatialResult calculate(const SpatialListener&,const SpatialParameters&,std::span<const std::byte> curve,
                            const EnvironmentParameters* environment=nullptr,bool mute_at_max=false);
    static void validate(const SpatialParameters&);
    static void validate(const EnvironmentParameters&);
private:
    void call(std::uint32_t,std::initializer_list<std::uint32_t>);
    Memory& memory_;
    std::array<std::byte,8192> scratch_{};
    std::array<std::uint32_t,12> environment_listener_{};
};
std::string check_spatial(const std::filesystem::path&);
}
