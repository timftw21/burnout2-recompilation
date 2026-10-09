#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <immintrin.h>

namespace b2 {
struct FloatingState {
    std::array<std::byte,108> image = [] {
        std::array<std::byte,108> value{};
        value[0]=std::byte{0x7F}; value[1]=std::byte{3};
        value[8]=value[9]=std::byte{0xFF};
        return value;
    }();
    std::uint32_t mxcsr = 0x1F80;
    std::uint32_t instruction = 0, data = 0;
    std::uint16_t opcode = 0, code_selector = 0, data_selector = 0;
    bool active = false;
    const void* host_context = nullptr;
    std::array<std::array<std::uint32_t,4>,8> xmm{};
    void environment(void* output) const;
    void restore_environment(const void* input);
};
static_assert(offsetof(FloatingState,mxcsr)==108);

// The native x87 stack stays resident across direct compiled calls. Only external
// entries save/restore it. The XMM bank remains in C++ state; host XMM registers
// are compiler temporaries and must obey the Windows x64 calling convention.
class FloatingScope {
public:
    explicit FloatingScope(FloatingState&);
    ~FloatingScope();
    FloatingScope(const FloatingScope&) = delete;
    FloatingScope& operator=(const FloatingScope&) = delete;
private:
    FloatingState& state_;
    std::array<std::byte,112> host_{};
    bool owner_;
};
// Kernel services run with host floating-point control state. Resume the guest
// stack afterwards, including when a service throws a diagnostic exception.
class HostFloatingScope {
public:
    explicit HostFloatingScope(FloatingState&);
    ~HostFloatingScope();
    HostFloatingScope(const HostFloatingScope&) = delete;
    HostFloatingScope& operator=(const HostFloatingScope&) = delete;
private:
    FloatingState& state_;
    std::array<std::byte,112> scratch_;
    bool suspended_;
    const void* host_context_;
};
inline __m128 xmm(const FloatingState& state, unsigned index) {
    __m128 value;
    std::memcpy(&value,state.xmm[index].data(),16);
    return value;
}
inline void set_xmm(FloatingState& state, unsigned index, __m128 value) {
    std::memcpy(state.xmm[index].data(),&value,16);
}
}
