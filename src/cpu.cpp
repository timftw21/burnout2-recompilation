#include "cpu.h"

namespace b2 {
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
