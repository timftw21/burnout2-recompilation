#include "floating.h"
#include <stdexcept>

extern "C" void b2_fp_enter(void* guest,void* host);
extern "C" void b2_fp_leave(void* guest,const void* host);

namespace b2 {
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
