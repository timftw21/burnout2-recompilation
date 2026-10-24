#include "cpu.h"
#include <algorithm>
#include <cmath>

namespace b2 {
void title_hud_widget(Cpu& cpu,Memory& memory) {
    auto& layout=*cpu.hud_layout;
    const auto object=cpu.registers[ecx];
    for(unsigned i=0;i<32;++i) {
        if(memory.load<std::uint32_t>(0x3D8580+i*4)!=object) continue;
        const auto first=memory.load<std::uint32_t>(object+8);
        const auto next=i==31?33U:memory.load<std::uint32_t>(memory.load<std::uint32_t>(0x3D8584+i*4)+8);
        if(first>=next || next>layout.anchors.size()) throw NativeBoundary(cpu.eip,"invalid HUD widget range");
        const auto method=memory.load<std::uint32_t>(memory.load<std::uint32_t>(object)+4);
        // World markers and the split divider use scene/screen coordinates.
        if(method==0x1F790 || method==0x24080) break;
        auto anchor=std::bit_cast<float>(memory.load<std::uint32_t>(cpu.registers[esp]));
        // All mode layouts share these authored right-column left edges:
        // full view, standard split, and the two native-wide split panes.
        // Anchor the column's right edge regardless of which widget uses it.
        for(const auto column:{0x4D2158U,0x4D207CU,0x4D1F68U,0x4D1FE4U})
            if(anchor==std::bit_cast<float>(memory.load<std::uint32_t>(column))) {
                anchor+=std::bit_cast<float>(memory.load<std::uint32_t>(0x2B08D4))*
                    std::bit_cast<float>(memory.load<std::uint32_t>(cpu.registers[esp]+8));
                break;
            }
        // Lesson targets ignore the caller's x and use their own right edge.
        if(method==0x21F50) anchor=std::bit_cast<float>(memory.load<std::uint32_t>(0x4D4884));
        for(auto index=first;index<next;++index) {layout.anchors[index]=anchor;layout.active|=1ULL<<index;}
        break;
    }
}
bool title_hud_gauges(Memory& memory) {
    const auto ram=memory.primary();
    const auto manager=ram.load<std::uint32_t>(0x3D8570);
    if(!manager) return false;
    const auto list=ram.load<std::uint32_t>(manager+0x3C),count=ram.load<std::uint32_t>(manager+0x40);
    // 1AA10 installs the two tachometers followed by the two boost meters.
    // C1440 skips queues with bit 0 set or an empty node chain.
    for(unsigned i=0;i<4;++i) {
        const auto widget=ram.load<std::uint32_t>(0x3D8580+i*4);
        if(!widget) continue;
        const auto index=ram.load<std::uint32_t>(widget+8);
        if(index>=count) continue;
        const auto queue=list+index*16;
        if(!(ram.load<std::uint32_t>(queue)&1) && ram.load<std::uint32_t>(queue+8)!=UINT32_MAX) return true;
    }
    return false;
}
void title_timing(Cpu& cpu,Memory& memory) {
    auto& timing=*cpu.game_timing;
    constexpr std::uint64_t frequency=733333333;
    const auto scene=memory.load<std::uint32_t>(0x34AB5C);
    const auto loading=scene?memory.load<std::uint32_t>(scene+0x10):0;
    const bool changed=!timing.started || scene!=timing.scene || loading!=timing.loading;
    if(changed) title_presentation(cpu,memory,0);
    if(changed || !timing.pending) {
        const auto cycles=read_timestamp(cpu);
        const auto rate=memory.load<std::uint32_t>(0x352720)?50U:60U;
        // Physics, replay input, ghost samples and sound events all consume
        // original fixed steps. Only their scheduling follows the host clock.
        // Scene/loading transitions start a fresh schedule, excluding load time.
        const auto elapsed=changed?frequency:timing.remainder+(cycles-timing.last_cycles)*rate;
        timing.pending=elapsed/frequency;timing.remainder=elapsed%frequency;
        timing.last_cycles=cycles;timing.started=true;
        timing.scene=scene;timing.loading=loading;
    }
    timing.update=timing.pending!=0;
    if(timing.update) {--timing.pending;++timing.step;}
    timing.render=timing.pending==0;
}
void title_loading(Cpu& cpu,Memory& memory,bool update) {
    auto& timing=*cpu.game_timing;
    constexpr std::uint64_t frequency=733333333;
    HostFloatingScope floating(cpu.floating);
    if(!update) {
        const auto cycles=read_timestamp(cpu);
        const auto rate=memory.load<std::uint32_t>(0x352720)?50U:60U;
        const auto elapsed=timing.loading_started?cycles-timing.loading_cycles:frequency/rate;
        const auto accumulated=timing.loading_started?timing.loading_remainder+elapsed*rate:frequency;
        if(!timing.loading_started) timing.loading_fade_error=0;
        timing.loading_steps=static_cast<std::uint32_t>(accumulated/frequency);
        timing.loading_remainder=accumulated%frequency;
        timing.loading_delta=float(double(elapsed)/frequency);
        timing.loading_cycles=cycles;timing.loading_started=true;
        return;
    }
    // Loading also runs in blocking scene constructors, outside the main loop.
    // Its UI consumes original fixed steps on a separate clock; the fade uses
    // elapsed display time. Never modify the gameplay delta at 598188.
    const auto saved=cpu;
    for(unsigned i=0;i<timing.loading_steps;++i) {
        const auto stack=saved.registers[esp]-0x1000;
        memory.store<std::uint32_t>(stack,0xADE3D);
        cpu.registers[esp]=stack;cpu.registers[ecx]=0x4CB2C8;
        invoke_native(cpu,memory,0x3F6F0);
    }
    const auto quantum=cpu.quantum_remaining;
    cpu=saved;cpu.quantum_remaining=quantum;
}
std::uint32_t title_loading_fade(Cpu& cpu,Memory& memory) {
    HostFloatingScope floating(cpu.floating);
    auto& timing=*cpu.game_timing;
    // ADE20 copies the last display, then subtracts an integer RGB amount.
    // Preserve its original 8 (NTSC) / 10 (PAL) units per normal frame,
    // carrying fractional units instead of discarding them on short displays.
    const auto rate=memory.load<std::uint32_t>(0x352720)?50U:60U;
    const auto original=std::trunc(double(std::bit_cast<float>(memory.load<std::uint32_t>(0x598188)))*510);
    const auto amount=original*rate*timing.loading_delta+timing.loading_fade_error;
    const auto whole=std::floor(amount+0.5);
    timing.loading_fade_error=amount-whole;
    return static_cast<std::uint32_t>(std::clamp(whole,0.0,255.0));
}
std::uint32_t title_traffic_fade(Cpu& cpu,Memory& memory,std::uint32_t vehicle) {
    const auto ram=memory.primary();
    // 9B380 used the simulation's player-distance cache for body opacity,
    // but its LOD and wheels use the active camera (9BEBC and C3180).
    // A detached Crash flyover can therefore see a nearby car fading away.
    const auto camera=ram.load<std::uint32_t>(ram.load<std::uint32_t>(0x443EB0));
    const auto position=ram.load<std::uint32_t>(camera+4)+0x40;
    std::array<float,3> car,view;
    std::memcpy(car.data(),ram.access(vehicle+0x30,sizeof(car)),sizeof(car));
    std::memcpy(view.data(),ram.access(position,sizeof(view)),sizeof(view));
    const auto original_end=std::bit_cast<float>(ram.load<std::uint32_t>(0x2C1ACC));
    const auto original_width=std::bit_cast<float>(ram.load<std::uint32_t>(0x2C1AD0));
    // All guest reads precede this non-throwing scalar SIMD calculation. Keep
    // x87 resident instead of saving/restoring its whole stack for every car.
    const auto mxcsr=_mm_getcsr();_mm_setcsr(0x1F80);
    // 9E950 admits ordinary traffic to the display list only within 250 units.
    // Moving opacity's endpoint beyond that list makes cars pop at full alpha.
    // Broaden the fade inside the available range without extending simulation.
    const auto end=original_end,width=std::min(original_width*cpu.draw_distance,original_end);
    float squared=0;
    for(unsigned axis=0;axis<3;++axis) {const auto delta=car[axis]-view[axis];squared+=delta*delta;}
    const auto start=end-width;
    // Most visible traffic needs neither a square root nor a division.
    const auto alpha=squared<=start*start?1.f:squared>=end*end?0.f:
        (end-_mm_cvtss_f32(_mm_sqrt_ss(_mm_set_ss(squared))))/width;
    _mm_setcsr(mxcsr);
    return std::bit_cast<std::uint32_t>(alpha);
}
void skip_title_intro(Cpu& cpu,Memory& memory) {
    if(!cpu.skip_intro || memory.load<std::uint32_t>(0x4CCAA8)!=19) return;
    const auto intro=cpu.registers[ecx];
    if(memory.load<std::uint32_t>(intro+8)>=5) return;
    // Finish the last logo. The original update enters record 5 (PRESS START),
    // clears the logo and enables the title controls without pressing Start.
    memory.store<std::uint32_t>(intro+8,4);
    memory.store<std::uint32_t>(intro+4,memory.load<std::uint32_t>(0x333FCC));
}
const CompiledFunction* lookup_native(std::uint32_t) noexcept;
std::uint32_t read_port(Cpu& cpu,std::uint16_t port,unsigned width) {
    if(!cpu.kernel || !cpu.kernel->read_port) throw NativeBoundary(cpu.eip,std::format("unbound {}-bit input port 0x{:04X}",width,port));
    HostFloatingScope host(cpu.floating);
    return cpu.kernel->read_port(cpu.kernel->context,port,width);
}
void write_port(Cpu& cpu,std::uint16_t port,unsigned width,std::uint32_t value) {
    if(!cpu.kernel || !cpu.kernel->write_port) throw NativeBoundary(cpu.eip,std::format("unbound {}-bit output port 0x{:04X}",width,port));
    HostFloatingScope host(cpu.floating);
    cpu.kernel->write_port(cpu.kernel->context,port,width,value);
}
void invoke_native(Cpu& cpu, Memory& memory, std::uint32_t address) {
    cpu.eip = address;
    if(address>0xFFF00000U && address<0xFFF02000U && !(address&15U)) {
        const auto ordinal=(address-0xFFF00000U)/16;
        if(!cpu.kernel || !cpu.kernel->entries[ordinal]) throw NativeBoundary(address,"kernel function is unbound");
        HostFloatingScope host(cpu.floating);
        cpu.kernel->entries[ordinal](cpu.kernel->context,cpu,memory);
        return;
    }
    const auto* found=lookup_native(address);
    if(!found)
        throw NativeBoundary(address, "function is absent from the fixed native table");
    found->run(cpu, memory);
}
}
