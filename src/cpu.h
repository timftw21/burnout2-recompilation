#pragma once
#include "floating.h"
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <format>
#include <span>
#include <stdexcept>
#include <string_view>
#include <type_traits>

namespace b2 {
extern "C" {
std::uint32_t b2_exchange8(void*,std::uint32_t);
std::uint32_t b2_exchange16(void*,std::uint32_t);
std::uint32_t b2_exchange32(void*,std::uint32_t);
std::uint32_t b2_fetch_add8(void*,std::uint32_t);
std::uint32_t b2_fetch_add16(void*,std::uint32_t);
std::uint32_t b2_fetch_add32(void*,std::uint32_t);
std::uint32_t b2_compare_exchange8(void*,std::uint32_t,std::uint32_t);
std::uint32_t b2_compare_exchange16(void*,std::uint32_t,std::uint32_t);
std::uint32_t b2_compare_exchange32(void*,std::uint32_t,std::uint32_t);
}
class Memory;
struct Cpu;
struct KernelServices {
    void* context = nullptr;
    std::array<void (*)(void*, Cpu&, Memory&), 512> entries{};
    std::uint32_t (*read_port)(void*,std::uint16_t,unsigned) = nullptr;
    void (*write_port)(void*,std::uint16_t,unsigned,std::uint32_t) = nullptr;
};
struct GuestClock {
    void* context = nullptr;
    std::uint64_t (*read_cycles)(void*) = nullptr;
};
struct ExecutionDiagnostic {
    std::uint64_t remaining = 1000000, visits = 0;
    std::array<std::uint32_t,64> history{};
    std::uint32_t watch_address=0;
    std::uint32_t break_address=0,break_hit=1;
    bool reached_watch=false;
};
enum Register : unsigned { eax, ecx, edx, ebx, esp, ebp, esi, edi };
struct Cpu {
    std::array<std::uint32_t, 8> registers{};
    std::uint32_t flags = 0x202;
    std::uint32_t eip = 0;
    std::uint32_t fs_base = 0, gs_base = 0;
    std::uint16_t cs_selector = 0, ds_selector = 0, ss_selector = 0, fs_selector = 0, gs_selector = 0;
    FloatingState floating;
    KernelServices* kernel = nullptr;
    GuestClock* clock = nullptr;
    ExecutionDiagnostic* diagnostic = nullptr;
    void (*preempt)(Cpu&) = nullptr;
    std::uint32_t quantum_remaining = 1024;
    std::uint32_t gdt_base = 0;
    std::uint16_t gdt_limit = 0;
};

class NativeBoundary : public std::runtime_error {
public:
    NativeBoundary(std::uint32_t address, std::string_view reason)
        : std::runtime_error(std::format("Native boundary at 0x{:08X}: {}", address, reason)), address(address) {}
    const std::uint32_t address;
};
inline bool checkpoint_due(Cpu& cpu) {
    const bool expired = cpu.preempt && !--cpu.quantum_remaining;
    return cpu.diagnostic || expired;
}
inline void checkpoint(Cpu& cpu, std::uint32_t address) {
    cpu.eip = address;
    if (cpu.diagnostic) {
        auto& diagnostic = *cpu.diagnostic;
        if(address==diagnostic.break_address && !--diagnostic.break_hit)
            throw NativeBoundary(address,"execution diagnostic breakpoint reached");
        if (!diagnostic.remaining) throw NativeBoundary(address, "execution diagnostic budget exhausted");
        --diagnostic.remaining;
        diagnostic.reached_watch|=address==diagnostic.watch_address;
        diagnostic.history[diagnostic.visits++ % diagnostic.history.size()] = address;
    }
    if (cpu.preempt && !cpu.quantum_remaining) {
        cpu.quantum_remaining = 1024;
        cpu.preempt(cpu);
    }
}
void invoke_native(Cpu& cpu, Memory& memory, std::uint32_t address);
std::uint32_t read_port(Cpu&,std::uint16_t,unsigned);
void write_port(Cpu&,std::uint16_t,unsigned,std::uint32_t);

class GuestTrap : public std::runtime_error {
public:
    GuestTrap(unsigned vector, std::uint32_t address, std::uint32_t resume)
        : std::runtime_error(std::format("Guest exception {} at 0x{:08X}", vector, address)),
          vector(vector), address(address), resume(resume) {}
    const unsigned vector;
    const std::uint32_t address, resume;
};
inline std::uint64_t read_timestamp(const Cpu& cpu) {
    if (!cpu.clock || !cpu.clock->read_cycles) throw std::runtime_error("Guest timestamp counter has no clock binding");
    return cpu.clock->read_cycles(cpu.clock->context);
}
template<std::uint32_t Ordinal> void call_kernel(Cpu& cpu, Memory& memory) {
    static_assert(Ordinal > 0 && Ordinal < 512);
    if (!cpu.kernel || !cpu.kernel->entries[Ordinal])
        throw std::runtime_error(std::format("Unbound Xbox kernel ordinal {} at 0x{:08X}", Ordinal, cpu.eip));
    HostFloatingScope host(cpu.floating);
    cpu.kernel->entries[Ordinal](cpu.kernel->context, cpu, memory);
}

class Memory {
public:
    Memory(std::uint32_t base, std::span<std::byte> bytes) : base_(base), bytes_(bytes) {
        if (bytes.size() > 0x100000000ULL - base)
            throw std::runtime_error("Guest memory wraps the address space");
    }
    void map(std::uint32_t base,std::span<std::byte> bytes) {
        const auto end=static_cast<std::uint64_t>(base)+bytes.size();
        if(bytes.empty() || end>0x100000000ULL || region_count_==regions_.size())
            throw std::runtime_error("Invalid or exhausted guest memory mappings");
        const auto overlaps=[&](std::uint32_t other,std::size_t size) {
            return base<static_cast<std::uint64_t>(other)+size && other<end;
        };
        if(overlaps(base_,bytes_.size())) throw std::runtime_error("Guest mappings overlap");
        for(unsigned i=0;i<region_count_;++i) if(overlaps(regions_[i].base,regions_[i].bytes.size()))
            throw std::runtime_error("Guest mappings overlap");
        regions_[region_count_++]={base,bytes};
    }
    void* access(std::uint32_t address, std::size_t size) const { return pointer(address,size); }
    void prefetch(std::uint32_t address) const {
        if(address>=base_ && static_cast<std::uint64_t>(address)-base_<bytes_.size())
            _mm_prefetch(reinterpret_cast<const char*>(bytes_.data()+address-base_),_MM_HINT_NTA);
    }
    template<class T> T exchange(std::uint32_t address,T value) {
        auto* target=pointer(address,sizeof(T));
        if constexpr(sizeof(T)==1) return static_cast<T>(b2_exchange8(target,value));
        else if constexpr(sizeof(T)==2) return static_cast<T>(b2_exchange16(target,value));
        else { static_assert(sizeof(T)==4); return b2_exchange32(target,value); }
    }
    template<class T> T fetch_add(std::uint32_t address,T value) {
        auto* target=pointer(address,sizeof(T));
        if constexpr(sizeof(T)==1) return static_cast<T>(b2_fetch_add8(target,value));
        else if constexpr(sizeof(T)==2) return static_cast<T>(b2_fetch_add16(target,value));
        else { static_assert(sizeof(T)==4); return b2_fetch_add32(target,value); }
    }
    template<class T> T compare_exchange(std::uint32_t address,T expected,T desired) {
        auto* target=pointer(address,sizeof(T));
        if constexpr(sizeof(T)==1) return static_cast<T>(b2_compare_exchange8(target,expected,desired));
        else if constexpr(sizeof(T)==2) return static_cast<T>(b2_compare_exchange16(target,expected,desired));
        else { static_assert(sizeof(T)==4); return b2_compare_exchange32(target,expected,desired); }
    }
    template<class T> T load(std::uint32_t address) const {
        static_assert(std::is_unsigned_v<T>);
        if(address>=io_floor_) if(const auto* io=find_io(address,sizeof(T))) {
            if constexpr(sizeof(T)<=4) return static_cast<T>(io->read(io->context,address-io->base,sizeof(T)));
            else {static_assert(sizeof(T)==8);return static_cast<T>(io->read(io->context,address-io->base,4))|
                (static_cast<T>(io->read(io->context,address-io->base+4,4))<<32);}
        }
        T value;
        std::memcpy(&value, pointer(address, sizeof(T)), sizeof(T));
        if constexpr (std::endian::native == std::endian::big) value = std::byteswap(value);
        return value;
    }
    template<class T> void store(std::uint32_t address, T value) {
        static_assert(std::is_unsigned_v<T>);
        if(address>=io_floor_) if(const auto* io=find_io(address,sizeof(T))) {
            if constexpr(sizeof(T)<=4) io->write(io->context,address-io->base,sizeof(T),static_cast<std::uint32_t>(value));
            else {static_assert(sizeof(T)==8);io->write(io->context,address-io->base,4,static_cast<std::uint32_t>(value));
                io->write(io->context,address-io->base+4,4,static_cast<std::uint32_t>(value>>32));}
            return;
        }
        if constexpr (std::endian::native == std::endian::big) value = std::byteswap(value);
        std::memcpy(pointer(address, sizeof(T)), &value, sizeof(T));
    }
    template<class T> void move_elements(std::uint32_t destination, std::uint32_t source,
                                         std::uint32_t count, bool descending) {
        if (!count) return;
        const auto size = static_cast<std::uint64_t>(count) * sizeof(T);
        const auto displacement = descending ? size - sizeof(T) : 0;
        if (displacement > destination || displacement > source)
            throw std::runtime_error("String move wraps the mapped address space");
        if((source>=io_floor_ && find_io(static_cast<std::uint32_t>(source-displacement),size)) ||
           (destination>=io_floor_ && find_io(static_cast<std::uint32_t>(destination-displacement),size))) {
            for(std::uint32_t i=0;i<count;++i) {
                const auto offset=(descending?0U-i:i)*static_cast<std::uint32_t>(sizeof(T));
                store<T>(destination+offset,load<T>(source+offset));
            }
            return;
        }
        auto* to = pointer(static_cast<std::uint32_t>(destination - displacement), size);
        const auto* from = pointer(static_cast<std::uint32_t>(source - displacement), size);
        const auto distance = destination > source ? destination - source : source - destination;
        if (distance >= size || (descending ? destination >= source : destination <= source)) {
            std::memmove(to, from, static_cast<std::size_t>(size));
        } else {
            // An overlapping REP MOVS can read data written by an earlier element.
            for (std::uint32_t i = 0; i < count; ++i) {
                const auto offset = (descending ? count - 1U - i : i) * sizeof(T);
                T value;
                std::memcpy(&value, from + offset, sizeof(T));
                std::memcpy(to + offset, &value, sizeof(T));
            }
        }
    }
    template<class T> void fill_elements(std::uint32_t destination, T value, std::uint32_t count, bool descending) {
        if (!count) return;
        const auto size = static_cast<std::uint64_t>(count) * sizeof(T);
        const auto displacement = descending ? size - sizeof(T) : 0;
        if (displacement > destination) throw std::runtime_error("String fill wraps the mapped address space");
        if(destination>=io_floor_ && find_io(static_cast<std::uint32_t>(destination-displacement),size)) {
            for(std::uint32_t i=0;i<count;++i) store<T>(destination+(descending?0U-i:i)*static_cast<std::uint32_t>(sizeof(T)),value);
            return;
        }
        auto* to = pointer(static_cast<std::uint32_t>(destination - displacement), size);
        if constexpr (sizeof(T) == 1) std::memset(to, value, static_cast<std::size_t>(size));
        else for (std::uint32_t i = 0; i < count; ++i) std::memcpy(to + i * sizeof(T), &value, sizeof(T));
    }
    using IoRead=std::uint32_t(*)(void*,std::uint32_t,unsigned);
    using IoWrite=void(*)(void*,std::uint32_t,unsigned,std::uint32_t);
    void map_io(std::uint32_t base,std::uint32_t size,void* context,IoRead read,IoWrite write) {
        const auto end=static_cast<std::uint64_t>(base)+size;
        if(!size || end>0x100000000ULL || io_count_==io_.size() || !read || !write)
            throw std::runtime_error("Invalid native memory-mapped device");
        const auto overlaps=[&](std::uint32_t start,std::size_t count){return base<static_cast<std::uint64_t>(start)+count && start<end;};
        if(overlaps(base_,bytes_.size())) throw std::runtime_error("Native IO overlaps primary memory");
        for(unsigned i=0;i<region_count_;++i) if(overlaps(regions_[i].base,regions_[i].bytes.size()))
            throw std::runtime_error("Native IO overlaps mapped storage");
        for(unsigned i=0;i<io_count_;++i) if(overlaps(io_[i].base,io_[i].size)) throw std::runtime_error("Native IO regions overlap");
        io_[io_count_++]={base,size,context,read,write};io_floor_=std::min(io_floor_,base);
    }
private:
    struct IoRegion {std::uint32_t base,size;void* context;IoRead read;IoWrite write;};
    const IoRegion* find_io(std::uint32_t address,std::uint64_t size) const {
        for(unsigned i=0;i<io_count_;++i) {
            const auto& io=io_[i];
            if(address>=io.base && address-io.base<io.size && size<=io.size-(address-io.base)) return &io;
        }
        return nullptr;
    }
    std::byte* pointer(std::uint32_t address, std::size_t size) const {
        if(address>=base_ && address-base_<=bytes_.size() && size<=bytes_.size()-(address-base_))
            return bytes_.data()+(address-base_);
        for(unsigned i=0;i<region_count_;++i) {
            const auto& region=regions_[i];
            if(address>=region.base && address-region.base<=region.bytes.size() && size<=region.bytes.size()-(address-region.base))
                return region.bytes.data()+(address-region.base);
        }
        throw std::runtime_error(std::format("Guest memory access at 0x{:08X}, {} bytes, outside the mapped regions", address, size));
    }
    std::uint32_t base_;
    std::span<std::byte> bytes_;
    struct Region { std::uint32_t base; std::span<std::byte> bytes; };
    std::array<Region,8> regions_{};
    unsigned region_count_=0;
    std::array<IoRegion,4> io_{};
    unsigned io_count_=0;
    std::uint32_t io_floor_=UINT32_MAX;
};

inline void load_code_selector(Cpu& cpu,const Memory& memory,std::uint16_t selector,std::uint32_t offset) {
    const auto index=selector&~7U;
    if((selector&7U) || index+7U>cpu.gdt_limit) throw GuestTrap(13,cpu.eip,cpu.eip);
    const auto descriptor=memory.load<std::uint64_t>(cpu.gdt_base+index);
    const auto access=static_cast<std::uint32_t>(descriptor>>40)&0xFF;
    if((access&0x98)!=0x98 || (access&0x60)) throw GuestTrap(13,cpu.eip,cpu.eip);
    const auto base=((descriptor>>16)&0xFFFF)|(((descriptor>>32)&0xFF)<<16)|((descriptor>>56)<<24);
    if(base || !(descriptor&(1ULL<<54))) throw NativeBoundary(cpu.eip,"non-flat or 16-bit code segment");
    auto limit=(descriptor&0xFFFF)|((descriptor>>32)&0xF0000);
    if(descriptor&(1ULL<<55)) limit=(limit<<12)|0xFFF;
    if(offset>limit) throw GuestTrap(13,cpu.eip,cpu.eip);
    cpu.cs_selector=selector;
}

inline __m128 scalar_vector(std::uint32_t bits) {
    return _mm_castsi128_ps(_mm_cvtsi32_si128(std::bit_cast<std::int32_t>(bits)));
}
inline __m128 load_vector(const Memory& memory,std::uint32_t address) {
    __m128 value;
    std::memcpy(&value,memory.access(address,16),16);
    return value;
}
inline void store_vector(Memory& memory,std::uint32_t address,__m128 value,bool scalar) {
    std::memcpy(memory.access(address,scalar?4:16),&value,scalar?4:16);
}
inline void stream_vector(Memory& memory,std::uint32_t address,__m128 value) {
    if(address&15U) throw std::runtime_error("Unaligned guest MOVNTPS");
    auto* target=memory.access(address,16);
    if(reinterpret_cast<std::uintptr_t>(target)&15U) std::memcpy(target,&value,16);
    else _mm_stream_ps(reinterpret_cast<float*>(target),value);
}

inline constexpr std::uint32_t carry = 1, parity = 4, auxiliary = 16,
    zero = 64, sign = 128, direction = 1024, overflow = 2048;
inline constexpr std::uint32_t arithmetic_flags = carry | parity | auxiliary | zero | sign | overflow;

template<unsigned Bits> constexpr std::uint32_t mask = static_cast<std::uint32_t>((1ULL << Bits) - 1);
template<unsigned Bits> constexpr std::uint32_t szp(std::uint32_t value) {
    value &= mask<Bits>;
    return (value == 0 ? zero : 0) | ((value >> (Bits - 1)) ? sign : 0) |
           ((std::popcount(value & 255U) & 1) == 0 ? parity : 0);
}
template<unsigned Bits> constexpr std::uint32_t logic_flags(std::uint32_t flags, std::uint32_t value) {
    // AF is architecturally undefined for logical operations; keep its previous value.
    return (flags & ~(carry | parity | zero | sign | overflow)) | szp<Bits>(value);
}
template<unsigned Bits> constexpr std::uint32_t sub_flags(std::uint32_t flags, std::uint32_t a, std::uint32_t b,
                                                        std::uint32_t borrow = 0) {
    a &= mask<Bits>; b &= mask<Bits>;
    const auto result = (a - b - borrow) & mask<Bits>;
    return (flags & ~arithmetic_flags) | szp<Bits>(result) |
           (static_cast<std::uint64_t>(a) < static_cast<std::uint64_t>(b) + borrow ? carry : 0) |
           (((a ^ b ^ result) & 16U) ? auxiliary : 0) |
           (((a ^ b) & (a ^ result) & (1U << (Bits - 1))) ? overflow : 0);
}
template<unsigned Bits> constexpr std::uint32_t add_flags(std::uint32_t flags, std::uint32_t a, std::uint32_t b,
                                                        std::uint32_t input_carry = 0) {
    a &= mask<Bits>; b &= mask<Bits>;
    const auto sum = static_cast<std::uint64_t>(a) + b + input_carry;
    const auto result = static_cast<std::uint32_t>(sum) & mask<Bits>;
    return (flags & ~arithmetic_flags) | szp<Bits>(result) | (sum > mask<Bits> ? carry : 0) |
           (((a ^ b ^ result) & 16U) ? auxiliary : 0) |
           ((~(a ^ b) & (a ^ result) & (1U << (Bits - 1))) ? overflow : 0);
}
template<unsigned Bits> constexpr std::uint32_t sign_extend(std::uint32_t value) {
    constexpr auto high = 1U << (Bits - 1);
    return ((value & mask<Bits>) ^ high) - high;
}

enum class Shift { left, right, arithmetic_right };
struct ShiftResult { std::uint32_t value, flags; };
template<unsigned Bits> constexpr ShiftResult shift(std::uint32_t flags, std::uint32_t value,
                                                  std::uint32_t count, Shift operation) {
    value &= mask<Bits>;
    count &= 31U;
    if (!count) return {value, flags};
    std::uint32_t result, last;
    if (operation == Shift::left) {
        result = static_cast<std::uint32_t>(static_cast<std::uint64_t>(value) << count) & mask<Bits>;
        last = count <= Bits ? (value >> (Bits - count)) & 1U : 0;
    } else if (operation == Shift::right) {
        result = value >> count;
        last = (value >> (count - 1)) & 1U;
    } else {
        const auto extended = std::bit_cast<std::int32_t>(sign_extend<Bits>(value));
        result = static_cast<std::uint32_t>(extended >> count) & mask<Bits>;
        last = (static_cast<std::uint32_t>(extended) >> (count - 1)) & 1U;
    }
    flags = (flags & ~(carry | parity | zero | sign)) | szp<Bits>(result) | last;
    if (count == 1) {
        const auto ov = operation == Shift::left ? ((result >> (Bits - 1)) ^ last) & 1U :
                        operation == Shift::right ? value >> (Bits - 1) : 0;
        flags = (flags & ~overflow) | (ov ? overflow : 0);
    }
    return {result, flags};
}

struct Product { std::uint64_t value; std::uint32_t flags; };
template<unsigned Bits,bool ThroughCarry> constexpr ShiftResult rotate_right(std::uint32_t value,unsigned count,std::uint32_t flags) {
    count&=31;
    value&=mask<Bits>;
    if(!count) return {value,flags};
    constexpr unsigned width=Bits+(ThroughCarry?1:0);
    const auto rotation=count%width;
    const std::uint64_t extended=value|(ThroughCarry?static_cast<std::uint64_t>(flags&carry)<<Bits:0);
    const auto result=((extended>>rotation)|(extended<<(width-rotation)))&((1ULL<<width)-1);
    flags=(flags&~carry)|static_cast<std::uint32_t>((result>>(ThroughCarry?Bits:Bits-1))&1);
    if(count==1) {
        const auto of=((result>>(Bits-1))^(result>>(Bits-2)))&1;
        flags=(flags&~overflow)|(of?overflow:0);
    }
    return {static_cast<std::uint32_t>(result)&mask<Bits>,flags};
}
template<unsigned Bits,bool ThroughCarry> constexpr ShiftResult rotate_left(std::uint32_t value,unsigned count,std::uint32_t flags) {
    count&=31;
    value&=mask<Bits>;
    if(!count) return {value,flags};
    constexpr unsigned width=Bits+(ThroughCarry?1:0);
    const auto rotation=count%width;
    const std::uint64_t extended=value|(ThroughCarry?static_cast<std::uint64_t>(flags&carry)<<Bits:0);
    const auto result=((extended<<rotation)|(extended>>(width-rotation)))&((1ULL<<width)-1);
    const auto cf=static_cast<std::uint32_t>((result>>(ThroughCarry?Bits:0))&1);
    flags=(flags&~carry)|cf;
    if(count==1) flags=(flags&~overflow)|((((result>>(Bits-1))^cf)&1)?overflow:0);
    return {static_cast<std::uint32_t>(result)&mask<Bits>,flags};
}
inline std::uint32_t pop_flags(std::uint32_t original,std::uint32_t value,unsigned width,unsigned privilege) {
    auto writable=width==16?0x7FD5U:0x247FD5U;
    if(privilege) writable&=~0x3000U;
    if(privilege>((original>>12)&3)) writable&=~0x200U;
    return ((original&~(writable|0x10000U))|(value&writable)|2U);
}
template<unsigned Bits,bool Right> constexpr ShiftResult double_shift(std::uint32_t value,std::uint32_t source,unsigned count,std::uint32_t flags) {
    count&=31;
    value&=mask<Bits>; source&=mask<Bits>;
    if(!count) return {value,flags};
    const auto pair=Right?(static_cast<std::uint64_t>(source)<<Bits)|value:(static_cast<std::uint64_t>(value)<<Bits)|source;
    const auto result=static_cast<std::uint32_t>(Right?pair>>count:(pair<<count)>>Bits)&mask<Bits>;
    const auto cf=static_cast<std::uint32_t>((pair>>(Right?count-1:Bits*2-count))&1);
    flags=(flags&~(carry|parity|zero|sign))|szp<Bits>(result)|cf;
    if(count==1) flags=(flags&~overflow)|(((value^result)&(1U<<(Bits-1)))?overflow:0);
    return {result,flags};
}
template<unsigned Bits, bool Signed> constexpr Product multiply(std::uint32_t flags, std::uint32_t a, std::uint32_t b) {
    std::uint64_t product;
    bool too_large;
    if constexpr (Signed) {
        const auto full = static_cast<std::int64_t>(std::bit_cast<std::int32_t>(sign_extend<Bits>(a))) *
                          std::bit_cast<std::int32_t>(sign_extend<Bits>(b));
        product = static_cast<std::uint64_t>(full);
        too_large = full != std::bit_cast<std::int32_t>(sign_extend<Bits>(static_cast<std::uint32_t>(product)));
    } else {
        product = static_cast<std::uint64_t>(a & mask<Bits>) * (b & mask<Bits>);
        too_large = product > mask<Bits>;
    }
    return {product, (flags & ~(carry | overflow)) | (too_large ? carry | overflow : 0)};
}

struct Quotient { std::uint32_t value, remainder; };
template<unsigned Bits, bool Signed> Quotient divide(std::uint64_t dividend, std::uint32_t divisor) {
    divisor &= mask<Bits>;
    if (!divisor) throw std::runtime_error("Guest integer division by zero");
    if constexpr (Signed) {
        std::int64_t numerator;
        if constexpr (Bits == 32) numerator = std::bit_cast<std::int64_t>(dividend);
        else {
            constexpr auto high = 1ULL << (Bits * 2 - 1);
            numerator = static_cast<std::int64_t>((dividend & ((high << 1) - 1)) ^ high) - static_cast<std::int64_t>(high);
        }
        const auto denominator = std::bit_cast<std::int32_t>(sign_extend<Bits>(divisor));
        if (numerator == INT64_MIN && denominator == -1) throw std::runtime_error("Guest integer division overflow");
        const auto quotient = numerator / denominator;
        if (quotient < -(1LL << (Bits - 1)) || quotient > (1LL << (Bits - 1)) - 1)
            throw std::runtime_error("Guest integer division overflow");
        return {static_cast<std::uint32_t>(quotient) & mask<Bits>, static_cast<std::uint32_t>(numerator % denominator) & mask<Bits>};
    } else {
        const auto quotient = dividend / divisor;
        if (quotient > mask<Bits>) throw std::runtime_error("Guest integer division overflow");
        return {static_cast<std::uint32_t>(quotient), static_cast<std::uint32_t>(dividend % divisor)};
    }
}

struct CompiledFunction {
    std::uint32_t address;
    std::string_view source_sha256;
    void (*run)(Cpu&, Memory&);
};
extern const std::span<const CompiledFunction> compiled_batch;
}
