#pragma once
#include "effects.h"
#include <array>
#include <stdexcept>

namespace b2 {
// Native state used by the ahead-of-time effect functions. No program decoder is
// present in the player; generated functions access registers and samples directly.
struct Dsp {
    std::array<std::uint32_t,6144> x{};
    std::array<std::uint32_t,64> registers{};
    std::array<std::int64_t,2> accumulators{};
    std::vector<std::byte> scratch;
    std::uint32_t dma_next=0x4000,dma_control=0,interrupts=0;
    std::uint64_t transferred_words=0;
    std::uint32_t work_remaining=100000,active_effect=0;
    bool negative=false,zero=false,overflow=false;

    explicit Dsp(const EffectsImage&);
    static std::int32_t signed24(std::uint32_t value) {return static_cast<std::int32_t>(value<<8)>>8;}
    static std::int64_t signed56(std::uint64_t value) {return static_cast<std::int64_t>(value<<8)>>8;}
    std::uint32_t read(unsigned);
    void write(unsigned,std::uint32_t);
    std::uint32_t load(std::uint32_t);
    void store(std::uint32_t,std::uint32_t);
    std::uint32_t ea(unsigned index,unsigned mode);
    std::int64_t operand(unsigned);
    void add(unsigned,std::int64_t,bool subtract=false,bool save=true);
    void clear(unsigned);
    void negate(unsigned);
    void shift(unsigned source,unsigned destination,unsigned count,bool right=false);
    void multiply(unsigned,std::uint32_t,std::uint32_t,bool accumulate,bool round,bool negate=false);
    void logical_and(unsigned,std::uint32_t);
    void guard(unsigned address) {
        if(!work_remaining--) throw std::runtime_error("Static audio effect exceeded its bounded work budget at P:"+std::to_string(address));
    }
    bool less() const {return negative!=overflow;}
private:
    void flags(std::int64_t,bool=false);
    void arithmetic(unsigned,std::int64_t,bool,bool rounded=false);
    void transfer();
};
// Supplied by b2-tool's bounded, native build-time generator.
bool compiled_effects_match(const EffectsImage&);
void run_effect(Dsp&,unsigned index);
std::string check_effects(const std::filesystem::path&);
}
