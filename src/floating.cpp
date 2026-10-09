#include "floating.h"
#include <stdexcept>

extern "C" void b2_fp_enter(void* guest,void* host);
extern "C" void b2_fp_leave(void* guest,const void* host);

namespace b2 {
namespace {
// The preserved Intel approximation uses 1024 midpoint buckets per exponent
// parity. Build the 4 KiB table at compile time, including its float32 rounding,
// so guest MXCSR and host-vendor estimates cannot change the result.
constexpr auto rsqrt_table=[] {
    std::array<std::uint16_t,2048> table{};
    for(unsigned i=0;i<table.size();++i) {
        const std::uint64_t divisor=(2049U+2U*(i&1023U)) << (i>>10);
        constexpr std::uint64_t numerator=1ULL<<59;
        const auto square=numerator/divisor;
        std::uint64_t mantissa=1U<<24;
        for(;;) {
            const auto next=(mantissa+square/mantissa)/2;
            if(next>=mantissa) break;
            mantissa=next;
        }
        const auto halfway=(2*mantissa+1)*(2*mantissa+1)*divisor;
        if(halfway<4*numerator || (halfway==4*numerator && (mantissa&1))) ++mantissa;
        table[i]=static_cast<std::uint16_t>((mantissa+1024)>>11);
    }
    return table;
}();
static_assert(rsqrt_table[0]==8190 && rsqrt_table[1024]==5791);

std::uint32_t rsqrt_bits(std::uint32_t bits) {
    const auto sign=bits&0x80000000U,exponent=(bits>>23)&255U,fraction=bits&0x7FFFFFU;
    if(!exponent) return sign|0x7F800000U;
    if(exponent==255) return fraction ? bits|0x00400000U : (sign ? 0xFFC00000U : 0U);
    if(sign) return 0xFFC00000U;
    const auto index=(((exponent+1)&1U)<<10)|(fraction>>13);
    return ((380-exponent)/2)<<23 | (std::uint32_t(rsqrt_table[index])-4096U)<<11;
}
}
__m128 rsqrt_ss(__m128 value) {
    const auto bits=rsqrt_bits(static_cast<std::uint32_t>(_mm_cvtsi128_si32(_mm_castps_si128(value))));
    return _mm_move_ss(value,_mm_castsi128_ps(_mm_cvtsi32_si128(static_cast<std::int32_t>(bits))));
}
__m128 rsqrt_ps(__m128 value) {
    std::array<std::uint32_t,4> bits;
    std::memcpy(bits.data(),&value,16);
    for(auto& lane:bits) lane=rsqrt_bits(lane);
    std::memcpy(&value,bits.data(),16);
    return value;
}
void FloatingState::environment(void* output) const {
    auto* bytes=static_cast<std::byte*>(output);
    std::memcpy(bytes+12,&instruction,4);
    std::memcpy(bytes+16,&code_selector,2);
    std::memcpy(bytes+18,&opcode,2);
    std::memcpy(bytes+20,&data,4);
    std::memcpy(bytes+24,&data_selector,2);
}
void FloatingState::restore_environment(const void* input) {
    const auto* bytes=static_cast<const std::byte*>(input);
    std::memcpy(&instruction,bytes+12,4);
    std::memcpy(&code_selector,bytes+16,2);
    std::memcpy(&opcode,bytes+18,2); opcode &= 0x7FF;
    std::memcpy(&data,bytes+20,4);
    std::memcpy(&data_selector,bytes+24,2);
}
FloatingScope::FloatingScope(FloatingState& state) : state_(state),owner_(!state.active) {
    if (!owner_) return;
    if (state.mxcsr & ~0xFFFFU) throw std::runtime_error("Guest MXCSR has reserved bits set");
    b2_fp_enter(&state_,host_.data());
    state_.host_context=host_.data();
    state_.active=true;
}
FloatingScope::~FloatingScope() {
    if (!owner_) return;
    b2_fp_leave(&state_,host_.data());
    state_.environment(state_.image.data());
    state_.active=false;
    state_.host_context=nullptr;
}
HostFloatingScope::HostFloatingScope(FloatingState& state)
    : state_(state),suspended_(state.active),host_context_(state.host_context) {
    if (!suspended_) return;
    b2_fp_leave(&state_,state_.host_context);
    state_.environment(state_.image.data());
    state_.active=false;
}
HostFloatingScope::~HostFloatingScope() {
    if (!suspended_) return;
    b2_fp_enter(&state_,scratch_.data());
    state_.active=true;
    state_.host_context=host_context_;
}
}
