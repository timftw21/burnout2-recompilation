#include "cpu_checks.h"
#include "cpu.h"
#include "presentation.h"
#include "file.h"
#include "xbe.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <memory>

extern "C" {
#define B2_REFERENCES(bits) \
    std::uint32_t B2_ASM b2_adc##bits##_flags(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint32_t B2_ASM b2_sbb##bits##_flags(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint32_t B2_ASM b2_inc##bits##_flags(std::uint32_t, std::uint32_t); \
    std::uint32_t B2_ASM b2_dec##bits##_flags(std::uint32_t, std::uint32_t); \
    std::uint32_t B2_ASM b2_neg##bits##_flags(std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_shl##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_shr##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_sar##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_ror##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_rcr##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_rol##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_rcl##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_mul##bits(std::uint32_t, std::uint32_t, std::uint32_t*); \
    std::uint64_t B2_ASM b2_imul##bits(std::uint32_t, std::uint32_t, std::uint32_t*); \
    std::uint64_t B2_ASM b2_div##bits(std::uint64_t, std::uint32_t); \
    std::uint64_t B2_ASM b2_idiv##bits(std::uint64_t, std::uint32_t);
B2_REFERENCES(8)
B2_REFERENCES(16)
B2_REFERENCES(32)
#undef B2_REFERENCES
std::uint64_t B2_ASM b2_shld16(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint64_t B2_ASM b2_shld32(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint64_t B2_ASM b2_shrd16(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint64_t B2_ASM b2_shrd32(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint32_t B2_ASM b2_compare8_flags(std::uint32_t, std::uint32_t);
std::uint32_t B2_ASM b2_and32_flags(std::uint32_t, std::uint32_t);
void B2_ASM b2_string_move(void*, const void*, std::uint32_t, std::uint32_t, std::uint32_t);
void B2_ASM b2_string_fill(void*, std::uint32_t, std::uint32_t, std::uint32_t, std::uint32_t);
void B2_ASM b2_fp_snapshot(void*);
}

namespace b2 {
void guest_00227BD9(Cpu&,Memory&);
void reference_matrix(Cpu&,Memory&);
const CompiledFunction* lookup_native(std::uint32_t) noexcept;
namespace {
using CarryReference = decltype(&b2_adc8_flags);
using UnaryReference = decltype(&b2_inc8_flags);
using ShiftReference = decltype(&b2_shl8);
using MultiplyReference = decltype(&b2_mul8);
using DivideReference = decltype(&b2_div8);
struct References {
    CarryReference adc, sbb;
    UnaryReference inc, dec, neg;
    std::array<ShiftReference, 3> shifts;
    MultiplyReference mul, imul;
    DivideReference div, idiv;
};
constexpr std::uint32_t edges[] = {0, 1, 15, 16, 0x7F, 0x80, 0xFF, 0x100,
    0x7FFF, 0x8000, 0xFFFF, 0x10000, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF};
constexpr std::uint32_t initial_flags = 0x202 | arithmetic_flags;
// Rediscovered from the verified main-loop call graph: 12BC0 -> 12BB0 -> 12B80.
// The innermost routine returns with RET 4; the outer two use plain RET.
constexpr std::uint32_t constructor_address = 0x00012BC0;
void require(bool passed, const std::string& message) {
    if (!passed) throw std::runtime_error(message);
}
const CompiledFunction& function(std::uint32_t address) {
    const auto found = std::ranges::lower_bound(compiled_batch, address, {}, &CompiledFunction::address);
    require(found != compiled_batch.end() && found->address == address, "Required batch function was not compiled: " + hex32(address));
    return *found;
}
void flags_match(std::uint32_t model, std::uint32_t host, std::uint32_t defined,
                 std::uint32_t original, const std::string& description) {
    require((model & defined) == (host & defined) && (model & ~defined) == (original & ~defined), description);
}

template<unsigned Bits> unsigned check_instructions(const References& host) {
    unsigned checked = 0;
    for (const auto a : edges) {
        for (std::uint32_t input_carry = 0; input_carry <= 1; ++input_carry) {
            const auto original = (initial_flags & ~carry) | input_carry;
            for (const auto b : edges) {
                const auto description = std::format("{}-bit carry arithmetic differs for {}, {}, carry {}", Bits, hex32(a), hex32(b), input_carry);
                flags_match(add_flags<Bits>(original, a, b, input_carry), host.adc(a, b, input_carry), arithmetic_flags, original, description);
                flags_match(sub_flags<Bits>(original, a, b, input_carry), host.sbb(a, b, input_carry), arithmetic_flags, original, description);
                const auto addition=arithmetic<Bits,false>(original,a,b,input_carry),subtraction=arithmetic<Bits,true>(original,a,b,input_carry);
                flags_match(addition.flags,host.adc(a,b,input_carry),arithmetic_flags,original,description);
                flags_match(subtraction.flags,host.sbb(a,b,input_carry),arithmetic_flags,original,description);
                require(addition.value==((std::uint64_t(a)+b+input_carry)&mask<Bits>) &&
                    subtraction.value==((std::uint64_t(a)-b-input_carry)&mask<Bits>),description);
                checked += 5;
            }
            const auto description = std::format("{}-bit unary arithmetic differs for {}", Bits, hex32(a));
            flags_match((add_flags<Bits>(original, a, 1) & ~carry) | input_carry, host.inc(a, input_carry), arithmetic_flags, original, description);
            flags_match((sub_flags<Bits>(original, a, 1) & ~carry) | input_carry, host.dec(a, input_carry), arithmetic_flags, original, description);
            flags_match(sub_flags<Bits>(original, 0, a), host.neg(a, input_carry), arithmetic_flags, original, description);
            checked += 3;
        }
        for (const std::uint32_t count : {0U, 1U, 2U, 7U, 8U, 15U, 16U, 31U, 32U, 33U, 63U, 255U}) {
            for (unsigned operation = 0; operation < 3; ++operation) {
                const auto result = shift<Bits>(initial_flags, a, count, static_cast<Shift>(operation));
                const auto reference = host.shifts[operation](a, count, initial_flags);
                const auto masked_count = count & 31U;
                auto defined = masked_count ? parity | zero | sign : arithmetic_flags;
                if (masked_count == 1) defined |= overflow;
                if (masked_count && (operation == 2 || masked_count < Bits)) defined |= carry;
                const auto description = std::format("{}-bit shift {} differs for {}, count {}", Bits, operation, hex32(a), count);
                require(result.value == static_cast<std::uint32_t>(reference), description);
                require((result.flags & defined) == (static_cast<std::uint32_t>(reference >> 32) & defined) &&
                        (result.flags & ~arithmetic_flags) == (initial_flags & ~arithmetic_flags), description);
                ++checked;
            }
        }
        for (const auto b : edges) {
            for (const bool signed_product : {false, true}) {
                std::uint32_t reference_flags;
                const auto reference = (signed_product ? host.imul : host.mul)(a, b, &reference_flags);
                const auto result = signed_product ? multiply<Bits, true>(initial_flags, a, b) : multiply<Bits, false>(initial_flags, a, b);
                constexpr auto product_mask = UINT64_MAX >> (64 - Bits * 2);
                const auto description = std::format("{}-bit multiply differs for {}, {}", Bits, hex32(a), hex32(b));
                require((result.value & product_mask) == reference, description);
                flags_match(result.flags, reference_flags, carry | overflow, initial_flags, description);
                ++checked;
            }
            if (const auto divisor = b & mask<Bits>; divisor && static_cast<std::uint64_t>(a & mask<Bits>) * 3 / divisor <= mask<Bits>) {
                const auto dividend = static_cast<std::uint64_t>(a & mask<Bits>) * 3;
                const auto result = divide<Bits, false>(dividend, divisor);
                const auto reference = host.div(dividend, divisor);
                require(result.value == static_cast<std::uint32_t>(reference) && result.remainder == (reference >> 32), "Unsigned division differs from host CPU");
                ++checked;
            }
            const auto numerator = static_cast<std::int64_t>(std::bit_cast<std::int32_t>(sign_extend<Bits>(a))) * 3;
            const auto denominator = std::bit_cast<std::int32_t>(sign_extend<Bits>(b));
            if (denominator && numerator / denominator >= -(1LL << (Bits - 1)) && numerator / denominator <= (1LL << (Bits - 1)) - 1) {
                constexpr auto dividend_mask = UINT64_MAX >> (64 - Bits * 2);
                const auto dividend = static_cast<std::uint64_t>(numerator) & dividend_mask;
                const auto result = divide<Bits, true>(dividend, static_cast<std::uint32_t>(denominator));
                const auto reference = host.idiv(dividend, static_cast<std::uint32_t>(denominator));
                require(result.value == static_cast<std::uint32_t>(reference) && result.remainder == (reference >> 32), "Signed division differs from host CPU");
                ++checked;
            }
        }
    }
    const auto fault = [&](const auto& run) {
        bool raised = false;
        try { run(); } catch (const std::runtime_error&) { raised = true; }
        require(raised, "Invalid guest division did not report a fault");
        ++checked;
    };
    fault([] { divide<Bits, false>(0, 0); });
    fault([] { divide<Bits, false>(1ULL << Bits, 1); });
    fault([] { divide<Bits, true>(static_cast<std::uint64_t>(-(1LL << (Bits - 1))), mask<Bits>); });
    if constexpr (Bits == 32) fault([] { divide<32, true>(0x8000000000000000ULL, 0xFFFFFFFF); });
    return checked;
}

template<class T> unsigned check_primary() {
    unsigned checked = 0;
    constexpr std::uint32_t base = 0x1000;
    if constexpr(sizeof(T)==4) for(unsigned size=0;size<=5;++size) {
        std::array<std::byte,8> bytes{};
        Memory memory(UINT32_MAX-7,std::span(bytes).first(size));const auto ram=memory.primary();
        for(unsigned offset=0;offset<=size+1;++offset) for(bool write:{false,true}) {
            bool completed=false;
            try {
                if(write) ram.store<T>(UINT32_MAX-7+offset,0x12345678);
                else (void)ram.load<T>(UINT32_MAX-7+offset);
                completed=true;
            } catch(const std::runtime_error&) {}
            require(completed==(offset<=size && sizeof(T)<=size-offset),"Cached word access changed a small mapping boundary");++checked;
        }
    }
    {
        std::array<std::byte,256> primary{};
        std::array<std::byte,64> kernel{};
        Memory memory(base,primary);
        memory.map(0x80000000U+base,kernel);
        memory.map(0x80000000U+base+128,std::span(primary).subspan(128));
        const auto ram=memory.primary();
        memory.store<T>(0x80000000U+base+128,static_cast<T>(0xAABBCCDD));
        require(memory.load<T>(base+128)==static_cast<T>(0xAABBCCDD),"Primary suffix alias lost its shared storage");
        memory.store<T>(0x80000000U+base,static_cast<T>(0x11223344));
        require(memory.load<T>(base)==0 && std::memcmp(kernel.data(),primary.data()+128,sizeof(T))!=0,
            "Primary alias replaced the separate kernel mapping");
        require(!memory.try_access(0x80000000U+base+64,1),"Primary alias mapped the hole before its suffix");
        require(memory.try_access(0x80000000U+base+255,1)==primary.data()+255 &&
            !memory.try_access(0x80000000U+base+255,2) && !memory.try_access(0x80000000U+base+256,1),
            "Primary alias changed its end boundary");
        require(memory.try_access(0x80000000U+base+256,0)==primary.data()+256 &&
            !memory.try_access(0xC0000000U+base+128,1),"Primary alias invented a mapping");
        std::array<std::byte,2> adjacent{};
        Memory edge(0,std::span(adjacent).first(1));edge.map(1,std::span(adjacent).first(1));
        require(edge.try_access(1,0)==adjacent.data()+1,"Adjacent alias changed an empty primary-end access");
        checked+=6;
        ram.store<T>(0x80000000U+base+128,static_cast<T>(0x55667788));
        require(ram.load<T>(base+128)==static_cast<T>(0x55667788) && ram.load<T>(0x80000000U+base)==static_cast<T>(0x11223344),
            "Cached primary storage replaced a separate mapping");++checked;
        memory.map(0x40000000U+base+128,std::span(primary).subspan(128));
        ram.store<T>(0x40000000U+base+128,static_cast<T>(0x99AABBCC));
        require(ram.load<T>(0x80000000U+base+128)==static_cast<T>(0x99AABBCC),"Cached primary lost a later alias");++checked;
        for(const auto address:{0x80000000U+base+64,0x80000000U+base+256,0xC0000000U+base+128}) {
            bool raised=false;try {ram.access(address,1);} catch(const std::runtime_error&) {raised=true;}
            require(raised,"Cached primary bypassed a mapping boundary");++checked;
        }
        require(ram.access(0x80000000U+base+256,0)==primary.data()+256,"Cached primary changed an empty alias-end access");++checked;
        require(edge.primary().access(1,0)==adjacent.data()+1,"Cached adjacent mapping changed an empty primary-end access");++checked;
        struct Device {std::array<std::byte,16> bytes{};unsigned reads=0,writes=0;} device;
        memory.map_io(0xFD000000,16,&device,
            [](void* context,std::uint32_t offset,unsigned width) {
                auto& device=*static_cast<Device*>(context);std::uint32_t value=0;++device.reads;
                std::memcpy(&value,device.bytes.data()+offset,width);return value;
            },
            [](void* context,std::uint32_t offset,unsigned width,std::uint32_t value) {
                auto& device=*static_cast<Device*>(context);++device.writes;std::memcpy(device.bytes.data()+offset,&value,width);
            });
        ram.store<T>(0xFD000000,static_cast<T>(0x12345678));
        require(ram.load<T>(0xFD000000)==static_cast<T>(0x12345678) &&
            device.reads==(sizeof(T)==8?2U:1U) && device.writes==device.reads,"Cached primary bypassed later device bindings");++checked;
    }
    return checked;
}
template<class T> unsigned check_strings() {
    unsigned checked=check_primary<T>();
    constexpr std::uint32_t base=0x1000,source=80;
    for (const bool descending : {false, true}) {
        for (const std::uint32_t destination : {20U, 79U, 80U, 81U, 120U}) {
            for (const std::uint32_t count : {0U, 1U, 7U, 16U}) for(unsigned aliases=0;aliases<9;++aliases) {
                std::array<std::byte, 256> model, reference;
                for (std::size_t i = 0; i < model.size(); ++i) model[i] = std::byte((i * 17U) & 255U);
                reference = model;
                const auto offset = descending && count ? (count - 1U) * sizeof(T) : 0U;
                Memory memory(base, model);
                memory.map(base|0x80000000U,model);
                memory.map(base|0xF0000000U,model);
                constexpr std::array<std::uint32_t,3> address_views{0,0x80000000U,0xF0000000U};
                memory.move_elements<T>(static_cast<std::uint32_t>(base + destination + offset)|address_views[aliases/3],
                    static_cast<std::uint32_t>(base + source + offset)|address_views[aliases%3], count, descending);
                b2_string_move(reference.data() + destination + offset, reference.data() + source + offset,
                    count, sizeof(T), descending);
                require(model == reference, "Guest string copy differs from native REP MOVS");
                ++checked;
            }
        }
        for (const std::uint32_t count : {0U, 1U, 7U, 16U}) {
            std::array<std::byte, 256> model{}, reference{};
            const auto offset = descending && count ? (count - 1U) * sizeof(T) : 0U;
            Memory memory(base, model);
            memory.fill_elements<T>(static_cast<std::uint32_t>(base + source + offset), static_cast<T>(0xAABBCCDD), count, descending);
            b2_string_fill(reference.data() + source + offset, 0xAABBCCDD, count, sizeof(T), descending);
            require(model == reference, "Guest string fill differs from native REP STOS");
            ++checked;
        }
    }
    return checked;
}

unsigned check_compare() {
    const auto& compare = function(B2_COMPARE_ADDRESS);
    unsigned checked = 0;
    for (int mismatch = -2; mismatch < 16; ++mismatch) for (const bool greater : {false, true}) {
        constexpr std::uint32_t base = 0x1000, stack = base + 128, left = base + 16;
        const auto right = mismatch == -2 ? left : base + 48;
        std::array<std::byte, 256> bytes{};
        Memory memory(base, bytes);
        for (unsigned i = 0; i < 16; ++i) {
            memory.store<std::uint8_t>(left + i, static_cast<std::uint8_t>(i + 64));
            memory.store<std::uint8_t>(right + i, static_cast<std::uint8_t>(i + 64));
        }
        if (mismatch >= 0) memory.store<std::uint8_t>(right + mismatch, static_cast<std::uint8_t>(mismatch + (greater ? 63 : 65)));
        Cpu original;
        for (unsigned i = 0; i < 8; ++i) original.registers[i] = 0x13570000U + i;
        original.registers[esp] = stack;
        memory.store<std::uint32_t>(stack, 0xABCDEF00);
        memory.store<std::uint32_t>(stack + 4, left);
        memory.store<std::uint32_t>(stack + 8, right);
        auto expected_bytes = bytes;
        Memory expected_memory(base, expected_bytes);
        expected_memory.store<std::uint32_t>(stack - 4, original.registers[esi]);
        expected_memory.store<std::uint32_t>(stack - 8, original.registers[edi]);
        const auto comparison = std::memcmp(bytes.data() + left - base, bytes.data() + right - base, 16);
        auto expected = original.registers;
        expected[eax] = comparison < 0 ? UINT32_MAX : comparison > 0 ? 1U : 0U;
        if (left != right) expected[ecx] = mismatch < 0 ? 0U : 15U - mismatch;
        expected[esp] += 4;
        std::uint32_t expected_flags, defined = arithmetic_flags;
#ifdef __aarch64__
        if(left==right) {expected_flags=0x44;defined&=~auxiliary;}
        else if(mismatch<0) expected_flags=0x44;
        else expected_flags=comparison<0?0x95:0x11;
#else
        if (left == right) { expected_flags = b2_and32_flags(0, 0); defined &= ~auxiliary; }
        else if (mismatch < 0) expected_flags = b2_compare8_flags(79, 79);
        else {
            const auto less = comparison < 0 ? 1U : 0U;
            expected_flags = b2_sbb32_flags(0U - less, UINT32_MAX, less);
        }
#endif
        auto cpu = original;
        compare.run(cpu, memory);
        require(cpu.registers == expected && cpu.eip == 0xABCDEF00 && bytes == expected_bytes &&
            (cpu.flags & defined) == (expected_flags & defined) &&
            (cpu.flags & ~arithmetic_flags) == (original.flags & ~arithmetic_flags),
            std::format("Game comparator differs from host comparison at byte {}, greater {}", mismatch, greater));
        ++checked;
    }
    return checked;
}

unsigned check_call_chain() {
    const auto& constructor = function(constructor_address);
    constexpr std::uint32_t base = 0x1000, object = base + 16, stack = base + 128;
    for (const auto flags : {0x202U, initial_flags | direction}) {
        std::array<std::byte, 256> bytes;
        bytes.fill(std::byte{0xA5});
        Memory memory(base, bytes);
        Cpu original;
        for (unsigned i = 0; i < 8; ++i) original.registers[i] = 0x13570000U + i;
        original.registers[ecx] = object;
        original.registers[esp] = stack;
        original.flags = flags;
        memory.store<std::uint32_t>(stack, 0xABCDEF00);
        auto expected_bytes = bytes;
        Memory expected_memory(base, expected_bytes);
        // Verify the initialized object and the exact guest stack left by both calls.
        for (unsigned i = 0; i < 6; ++i) expected_memory.store<std::uint32_t>(object + i * 4, UINT32_MAX);
        for (const auto offset : {0x18U, 0x1CU, 0x20U, 0x30U, 0x38U, 0x3CU})
            expected_memory.store<std::uint32_t>(object + offset, 0);
        expected_memory.store<std::uint32_t>(stack - 4, 0x00012BC5);
        expected_memory.store<std::uint32_t>(stack - 8, 1);
        expected_memory.store<std::uint32_t>(stack - 12, 0x00012BB7);
        expected_memory.store<std::uint32_t>(stack - 16, original.registers[esi]);
        auto expected_registers = original.registers;
        expected_registers[eax] = 0;
        expected_registers[edx] = 1;
        expected_registers[esp] += 4;
        auto cpu = original;
        constructor.run(cpu, memory);
        require(cpu.registers == expected_registers && bytes == expected_bytes && cpu.eip == 0xABCDEF00 &&
            (cpu.flags & (arithmetic_flags & ~auxiliary)) == 0x44 &&
            (cpu.flags & ~arithmetic_flags) == (flags & ~arithmetic_flags),
            "Recovered three-function constructor chain corrupted registers, flags, stack or object data");
    }
    return 2;
}
unsigned check_matrix() {
    constexpr std::uint32_t base=0x1000,stack=0x1380;
    const auto& matrix=function(0x00227BD9);
    Performance profile;profile.start("CPU diagnostic",144,1,false,{});
    unsigned checked=0;
    for(unsigned masks:{0x3FU,0x37U}) for(unsigned precision:{0U,2U,3U}) for(unsigned rounding=0;rounding<4;++rounding)
        for(unsigned alias=0;alias<4;++alias) for(unsigned sample=0;sample<5;++sample) {
            std::array<std::byte,1024> bytes{};
            Memory memory(base,bytes);
            for(unsigned i=0;i<32;++i) {
                auto value=std::bit_cast<std::uint32_t>(float(int((i*73+sample*37)%101)-50)/17.f);
                if(sample==3 && i%5==0) value=0x80000000;
                if(sample==4 && i%7==0) value=i%2?0x7F800000:0x7FC12345;
                memory.store<std::uint32_t>(base+i*4,value);
            }
            const auto destination=alias==1?base:alias==2?base+64:base+128;
            memory.store<std::uint32_t>(stack,0xABCDEF00);
            memory.store<std::uint32_t>(stack+4,destination);memory.store<std::uint32_t>(stack+8,base);
            memory.store<std::uint32_t>(stack+12,alias==3?base:base+64);
            auto reference_bytes=bytes;Memory reference_memory(base,reference_bytes);
            Cpu original;
            for(unsigned i=0;i<8;++i) original.registers[i]=0x13570000+i;
            original.registers[esp]=stack;original.flags=initial_flags|direction;
            original.cs_selector=8;original.ds_selector=16;original.ss_selector=24;
            const auto control=std::uint16_t(0x0040|masks|(precision<<8)|(rounding<<10));
            std::memcpy(original.floating.image.data(),&control,2);
            auto cpu=original,reference=original;
            if(sample==4) cpu.profile=&profile;
            {FloatingScope scope(reference.floating);reference_matrix(reference,reference_memory);}
            matrix.run(cpu,memory);
            require(bytes==reference_bytes && cpu.registers==reference.registers && cpu.flags==reference.flags && cpu.eip==reference.eip &&
                cpu.floating.instruction==reference.floating.instruction && cpu.floating.opcode==reference.floating.opcode &&
                cpu.floating.data==reference.floating.data && cpu.floating.code_selector==reference.floating.code_selector &&
                cpu.floating.data_selector==reference.floating.data_selector &&
                std::memcmp(cpu.floating.image.data(),reference.floating.image.data(),12)==0,
                std::format("Fused original matrix differs: masks {}, precision {}, rounding {}, alias {}, sample {}",masks,precision,rounding,alias,sample));
            ++checked;
        }
    // A failed preflight must execute the individual original instructions,
    // retaining earlier writes and the exact floating state at the failed load.
    for(bool bad_output:{false,true}) {
        std::array<std::byte,1024> bytes{};Memory memory(base,bytes);
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        memory.store<std::uint32_t>(stack+4,bad_output?0x13FC:base+128);
        memory.store<std::uint32_t>(stack+8,base);memory.store<std::uint32_t>(stack+12,bad_output?base+64:0x13FC);
        auto reference_bytes=bytes;Memory reference_memory(base,reference_bytes);
        Cpu cpu;cpu.registers[esp]=stack;auto reference=cpu;
        std::string actual_error,reference_error;
        try {matrix.run(cpu,memory);} catch(const std::runtime_error& error) {actual_error=error.what();}
        try {FloatingScope scope(reference.floating);reference_matrix(reference,reference_memory);}
        catch(const std::runtime_error& error) {reference_error=error.what();}
        require(!actual_error.empty() && actual_error==reference_error && bytes==reference_bytes &&
            cpu.floating.instruction==reference.floating.instruction && cpu.floating.data==reference.floating.data &&
            std::memcmp(cpu.floating.image.data(),reference.floating.image.data(),12)==0,"Fused matrix changed a memory-fault boundary");
        ++checked;
    }
    return checked;
}
}

unsigned check_floating_contexts() {
    const auto& load=function(0x000EE6D0);
    constexpr std::uint32_t address=0x005ADCB8,stack=0x1000;
    std::vector<std::byte> bytes(address+4);
    Memory memory(0,bytes);
    memory.store<std::uint32_t>(stack,0xABCDEF00);
    std::array<Cpu,2> contexts;
    contexts[0].floating.mxcsr=0x3F80; contexts[1].floating.mxcsr=0x5F80;
    const auto host_mxcsr=_mm_getcsr();
    std::array<std::byte,108> before{},after{};
    b2_fp_snapshot(before.data());
    unsigned checked=0;
    for(unsigned depth=1;depth<=3;++depth) for(unsigned index=0;index<2;++index) {
        auto& cpu=contexts[index];
        for(unsigned r=0;r<8;++r) cpu.registers[r]=0x13570000U+r;
        cpu.registers[esp]=stack;
        const auto original=cpu.registers;
        memory.store<std::uint32_t>(address,index?0xC0000000:0x3FA00000);
        load.run(cpu,memory);
        auto expected=original; expected[esp]+=4;
        require(cpu.registers==expected && cpu.eip==0xABCDEF00,"Floating game helper changed integer state");
        require(_mm_getcsr()==host_mxcsr && !cpu.floating.active,"Floating entry did not restore host control state");
        const Bytes image(cpu.floating.image);
        const auto top=(u16(image,4)>>11)&7U;
        require(top==8-depth && ((u16(image,8)>>(top*2))&3)==0,"Guest floating stack was lost across entries");
        std::uint64_t significand;
        std::memcpy(&significand,image.data()+28,8);
        require(significand==(index?0x8000000000000000ULL:0xA000000000000000ULL) &&
            u16(image,36)==(index?0xC000:0x3FFF),"Game FLD did not preserve the 80-bit result");
        require(u32(image,12)==0x000EE6D0 && u32(image,20)==address && u16(image,18)==0x105,
            "Saved floating environment did not retain guest instruction/data addresses");
        require(cpu.floating.mxcsr==(index?0x5F80U:0x3F80U),"Guest SIMD control state was lost");
        ++checked;
    }
    b2_fp_snapshot(after.data());
    require(before==after,"Guest floating execution changed the host x87 environment");
    return checked;
}
unsigned check_reciprocal_sqrt() {
    struct Case {std::uint32_t input,expected;};
    // Expected values retained by the original recomp's CPU fix (1e6c8b0).
    constexpr Case cases[]={{0x3F000000,0x3FB4F800},{0x3F800000,0x3F7FF000},
        {0x40000000,0x3F34F800},{0x40800000,0x3EFFF000},{0,0x7F800000},
        {0x80000000,0xFF800000},{1,0x7F800000},{0x80000001,0xFF800000},
        {0xBF800000,0xFFC00000},{0x7F800000,0},{0xFF800000,0xFFC00000},
        {0x7F812345,0x7FC12345},{0xFFC12345,0xFFC12345}};
    const auto host_mxcsr=_mm_getcsr();
    struct Restore {unsigned mxcsr;~Restore(){_mm_setcsr(mxcsr);}} restore{host_mxcsr};
    unsigned checked=0;
    for(unsigned control:{0x1F80U,0x3F80U,0x5F80U,0x7F80U,0x9FC0U}) {
        _mm_setcsr(control);
        for(const auto& sample:cases) {
            const std::array<std::uint32_t,4> input={sample.input,0x7F812345,0xFFFFFFFF,0x13579BDF};
            __m128 value;std::memcpy(&value,input.data(),16);
            const auto scalar=rsqrt_ss(value),packed=rsqrt_ps(value);
            std::array<std::uint32_t,4> result,all;
            std::memcpy(result.data(),&scalar,16);std::memcpy(all.data(),&packed,16);
            require(result==std::array<std::uint32_t,4>{sample.expected,input[1],input[2],input[3]} &&
                all[0]==sample.expected && all[1]==0x7FC12345 && all[2]==0xFFFFFFFF && _mm_getcsr()==control,
                "Xbox reciprocal-square-root estimate, scalar lanes or control state changed");
            ++checked;
        }
    }
    _mm_setcsr(0x1F80);
    // Independently compare the compact table with the preserved midpoint formula.
    for(unsigned exponent:{1U,2U,127U,128U,253U,254U}) for(unsigned bucket=0;bucket<1024;++bucket) {
        const auto bits=(exponent<<23)|(bucket<<13);
        const auto midpoint=std::bit_cast<float>(bits|0x1000U);
        const auto estimate=static_cast<float>(1.0/std::sqrt(static_cast<double>(midpoint)));
        const auto expected=(std::bit_cast<std::uint32_t>(estimate)+0x400U)&0xFFFFF800U;
        for(unsigned offset:{0U,0x1FFFU}) {
            const auto value=_mm_castsi128_ps(_mm_cvtsi32_si128(static_cast<int>(bits|offset)));
            require(static_cast<std::uint32_t>(_mm_cvtsi128_si32(_mm_castps_si128(rsqrt_ss(value))))==expected,
                "Xbox reciprocal-square-root lookup differs from the preserved midpoint formula");
        }
        ++checked;
    }
    return checked;
}
unsigned check_vertex_colors() {
    const auto& pack=function(0x000C2020);
    constexpr std::uint32_t base=0x1000,object=0x2000,colors=0x5000,stack=0x7000;
    struct Case { std::array<float,4> channels; std::uint32_t packed; };
    constexpr Case cases[]={{{0,0,0,0},0},{{255,255,255,255},0xFFFFFFFF},
        {{1,127,0,255},0xFF017F00},{{1.9f,127.9f,0.5f,255.9f},0xFF017F00}};
    unsigned count=0;
    for(const auto& sample : cases) {
        std::array<std::byte,0x8000> bytes;
        bytes.fill(std::byte{0xA5});
        Memory memory(base,bytes);
        memory.store<std::uint32_t>(object+0x1C00,count);
        memory.store<std::uint32_t>(object+0x1C04,0x3F400000);
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        memory.store<std::uint32_t>(stack+4,0x3FA00000);
        memory.store<std::uint32_t>(stack+8,0xC0200000);
        memory.store<std::uint32_t>(stack+12,colors);
        for(unsigned i=0;i<4;++i) memory.store<std::uint32_t>(colors+i*4,std::bit_cast<std::uint32_t>(sample.channels[i]));
        Cpu cpu;
        for(unsigned r=0;r<8;++r) cpu.registers[r]=0x13570000U+r;
        cpu.registers[ecx]=object; cpu.registers[esp]=stack;
        const auto original=cpu.registers;
        const auto host_mxcsr=_mm_getcsr();
        pack.run(cpu,memory);
        const auto vertex=object+count*28;
        require(memory.load<std::uint32_t>(vertex)==0x3FA00000 && memory.load<std::uint32_t>(vertex+4)==0xC0200000 &&
            memory.load<std::uint32_t>(vertex+8)==0x3F400000 && memory.load<std::uint32_t>(vertex+16)==sample.packed &&
            memory.load<std::uint32_t>(object+0x1C00)==count+1,"Game vertex/color conversion differs from expected output");
        auto expected=original; expected[eax]=object; expected[edx]=count*28; expected[ecx]=sample.packed; expected[esp]+=16;
        require(cpu.registers==expected && cpu.eip==0xABCDEF00,"Game vertex/color conversion changed unexpected registers");
        require(_mm_getcsr()==host_mxcsr && (cpu.floating.mxcsr&0x20)==(count==3?0x20U:0U),
            "SIMD conversion did not preserve guest/host exception status");
        require(u16(cpu.floating.image,8)==0xFFFF,"Vertex conversion left values on the floating stack");
        ++count;
    }
    return count;
}
unsigned check_control_boundaries() {
    for(const auto& entry:compiled_batch) {
        require(lookup_native(entry.address)==&entry,"Static dispatch index lost a compiled function");
        for(const auto address:{entry.address-1,entry.address+1}) {
            const auto expected=std::ranges::lower_bound(compiled_batch,address,{},&CompiledFunction::address);
            require(lookup_native(address)==(expected!=compiled_batch.end() && expected->address==address?&*expected:nullptr),
                "Static dispatch index changed an adjacent target");
        }
    }
    constexpr std::uint32_t stack=0x1000, ticks=0x005A7C18, tls_index=0x005A61F0;
    std::vector<std::byte> bytes(0x005ADCC0);
    Memory memory(0,bytes);
    unsigned count=0;
    for(const auto address : {0x00112D20U,0x00112D30U}) for(const auto value :
        {0ULL,1ULL,0xFFFFFFFFULL,0x100000000ULL,0x123456789ABCDEF0ULL,0xFFFFFFFFFFFFFFFFULL}) {
        struct Counter { std::uint64_t value; unsigned calls=0; } counter{value};
        GuestClock clock{&counter,[](void* context) {
            auto& counter=*static_cast<Counter*>(context);
            ++counter.calls;
            return counter.value;
        }};
        Cpu cpu;
        for(unsigned r=0;r<8;++r) cpu.registers[r]=0x13570000U+r;
        cpu.registers[esp]=stack; cpu.flags=0xAD7; cpu.clock=&clock;
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        auto expected=cpu.registers;
        expected[eax]=static_cast<std::uint32_t>(value); expected[edx]=static_cast<std::uint32_t>(value>>32); expected[esp]+=4;
        const auto flags=address==0x00112D20?cpu.flags:add_flags<32>(cpu.flags,stack-8,8);
        function(address).run(cpu,memory);
        require(cpu.registers==expected && cpu.flags==flags && cpu.eip==0xABCDEF00 && counter.calls==1,
            "Game timestamp helper changed registers, flags, return address or clock-read count");
        require(memory.load<std::uint64_t>(address==0x00112D20?ticks:stack-8)==value,
            "Game timestamp helper lost counter bits");
        ++count;
    }
    {
        Cpu cpu; cpu.registers[esp]=stack;
        bool rejected=false;
        try { function(0x00112D20).run(cpu,memory); }
        catch(const std::runtime_error& error) { rejected=std::string_view(error.what()).find("clock binding")!=std::string_view::npos; }
        require(rejected && cpu.eip==0x00112D21 && cpu.registers[esp]==stack-4,"Unbound timestamp counter did not stop at its guest instruction");
        ++count;
    }
    // This fixture checks the native import boundary, not an implementation of
    // Xbox's status conversion service. It never participates in game boot.
    for(const bool floating : {false,true}) {
        Cpu cpu; cpu.registers[esp]=stack; cpu.fs_base=0x2000; cpu.floating.mxcsr=0x3F80;
        memory.store<std::uint32_t>(stack,0xABCDEF00); memory.store<std::uint32_t>(stack+4,0xC0000008);
        memory.store<std::uint8_t>(0x2024,2); memory.store<std::uint32_t>(0x2004,0x3000);
        memory.store<std::uint32_t>(0x3000,0x4000); memory.store<std::uint32_t>(tls_index,0);
        struct Boundary { std::uint32_t host_mxcsr; unsigned calls=0; } boundary{_mm_getcsr()};
        KernelServices kernel; kernel.context=&boundary;
        kernel.entries[301]=[](void* context,Cpu& cpu,Memory& memory) {
            auto& boundary=*static_cast<Boundary*>(context);
            require(!cpu.floating.active && _mm_getcsr()==boundary.host_mxcsr,"Kernel boundary inherited guest floating control state");
            const auto entry_stack=cpu.registers[esp];
            require(memory.load<std::uint32_t>(entry_stack+4)==0xC0000008 && cpu.eip==0x000E047B,
                "Native import received the wrong stack argument or call address");
            cpu.eip=memory.load<std::uint32_t>(entry_stack);
            cpu.registers[esp]+=8; cpu.registers[eax]=6;
            ++boundary.calls;
        };
        cpu.kernel=&kernel;
        if(floating) { FloatingScope scope(cpu.floating); function(0x000E0477).run(cpu,memory); }
        else function(0x000E0477).run(cpu,memory);
        require(boundary.calls==1 && cpu.registers[eax]==6 && cpu.registers[edx]==6 &&
            cpu.registers[esp]==stack+8 && cpu.eip==0xABCDEF00 && memory.load<std::uint32_t>(0x4004)==6,
            "Game status wrapper did not preserve its service result and thread-local error");
        require(_mm_getcsr()==boundary.host_mxcsr,"Import boundary changed host floating control state");
        ++count;
    }
    {
        Cpu cpu; cpu.registers[esp]=stack;
        bool rejected=false;
        try { function(0x000E0477).run(cpu,memory); }
        catch(const std::runtime_error& error) { rejected=std::string_view(error.what()).find("Unbound Xbox kernel ordinal 301")!=std::string_view::npos; }
        require(rejected && cpu.eip==0x000E047B && cpu.registers[esp]==stack-8,"Unbound kernel import did not stop with its guest stack intact");
        ++count;
    }
    {
        Cpu cpu; cpu.registers[esp]=stack;
        const auto original=cpu.registers;
        bool trapped=false;
        try { function(0x000E5DC1).run(cpu,memory); }
        catch(const GuestTrap& trap) { trapped=trap.vector==3 && trap.address==0x000E5DC1 && trap.resume==0x000E5DC2; }
        require(trapped && cpu.eip==0x000E5DC2 && cpu.registers==original && cpu.flags==0x202,
            "Compiled game breakpoint lost its exception vector, saved instruction pointer or CPU state");
        ++count;
    }
    return count;
}
unsigned check_compact_jump_table() {
    // Recovered targets and byte remap from the verified executable's tables at
    // 0x0010A430 and 0x0010A444. Check its original size mapping and guard behavior.
    constexpr std::uint32_t table=0x0010A430,lookup=0x0010A444,stack=0x1000;
    constexpr std::uint32_t targets[]={0x0010A3E1,0x0010A3E7,0x0010A411,0x0010A3ED,0x0010A42B};
    std::vector<std::byte> bytes(lookup+64);
    Memory memory(0,bytes);
    for(unsigned i=0;i<std::size(targets);++i) memory.store<std::uint32_t>(table+i*4,targets[i]);
    for(unsigned value=1;value<=64;++value) {
        const std::uint8_t index=value==1||value==2?0:value==4||value==8?1:value==16||value==32?2:value==64?3:4;
        memory.store<std::uint8_t>(lookup+value-1,index);
    }
    const auto expected=[](std::uint32_t value) {
        switch(value) {
        case 1: case 2: return 1U;
        case 4: case 8: return 2U;
        case 16: case 32: case 1024: case 2048: case 4096: case 8192: return 4U;
        case 64: case 128: return 8U;
        case 256: case 512: return 16U;
        default: return 0U;
        }
    };
    const auto& mapping=function(0x0010A3C0);
    unsigned count=0;
    const auto check=[&](std::uint32_t value) {
        Cpu cpu; cpu.registers[esp]=stack;
        memory.store<std::uint32_t>(stack,0xABCDEF00); memory.store<std::uint32_t>(stack+4,value);
        mapping.run(cpu,memory);
        require(cpu.registers[eax]==expected(value) && cpu.registers[esp]==stack+4 && cpu.eip==0xABCDEF00,
            "Game compact jump table selected the wrong original case");
        ++count;
    };
    for(unsigned value=0;value<=65;++value) check(value);
    for(const auto value : {127U,128U,129U,255U,256U,512U,1024U,2048U,4096U,8192U,8193U,0x80000000U,0xFFFFFFFFU}) check(value);
    memory.store<std::uint32_t>(table,0xDEADFACE);
    memory.store<std::uint32_t>(stack,0xABCDEF00); memory.store<std::uint32_t>(stack+4,1);
    Cpu cpu; cpu.registers[esp]=stack;
    bool rejected=false;
    try { mapping.run(cpu,memory); }
    catch(const std::runtime_error& error) { rejected=std::string_view(error.what()).find("recovered set")!=std::string_view::npos; }
    require(rejected && cpu.eip==0x0010A3DA,"Modified jump table escaped its compiled target set");
    return count+1;
}
template<unsigned Bits,bool Left=false> unsigned check_rotates(ShiftReference ror,ShiftReference rcr) {
    unsigned checked=0;
    for(const auto a : edges) for(unsigned input_carry=0;input_carry<2;++input_carry)
        for(const auto count : {0U,1U,2U,8U,9U,16U,17U,31U,32U,33U,255U}) for(const bool through_carry : {false,true}) {
            const auto original=(initial_flags&~carry)|input_carry;
            const auto result=Left?(through_carry?rotate_left<Bits,true>(a,count,original):rotate_left<Bits,false>(a,count,original)):
                (through_carry?rotate_right<Bits,true>(a,count,original):rotate_right<Bits,false>(a,count,original));
            const auto reference=(through_carry?rcr:ror)(a,count,original);
            const auto defined=carry|((count&31)==1?overflow:0U);
            const auto message=std::format("{}-bit rotate differs for {}, count {}, carry {}",Bits,hex32(a),count,input_carry);
            require(result.value==static_cast<std::uint32_t>(reference),message);
            flags_match(result.flags,static_cast<std::uint32_t>(reference>>32),defined,original,message);
            ++checked;
        }
    return checked;
}
template<unsigned Bits> unsigned check_double_shifts(
    decltype(&b2_shld16) left,decltype(&b2_shrd16) right) {
    unsigned checked=0;
    for(const auto a : edges) for(const auto b : edges)
        for(const auto count : {0U,1U,2U,7U,8U,15U,16U,31U,32U,33U,255U}) for(const bool shift_right : {false,true}) {
            const auto masked=count&31;
            if(masked>Bits) continue; // The ISA leaves these results undefined.
            const auto result=shift_right?double_shift<Bits,true>(a,b,count,initial_flags):double_shift<Bits,false>(a,b,count,initial_flags);
            const auto reference=(shift_right?right:left)(a,b,count,initial_flags);
            const auto defined=masked?carry|parity|zero|sign|(masked==1?overflow:0U):arithmetic_flags;
            const auto message=std::format("{}-bit double shift differs for {}, {}, count {}",Bits,hex32(a),hex32(b),count);
            require(result.value==static_cast<std::uint32_t>(reference),message);
            flags_match(result.flags,static_cast<std::uint32_t>(reference>>32),defined,initial_flags,message);
            ++checked;
        }
    return checked;
}
unsigned check_segment_setup() {
    constexpr std::uint32_t stack=0x1000,gdt=0x1800;
    constexpr std::uint64_t code=0x00CF9B000000FFFF;
    std::vector<std::byte> bytes(0x2000);
    Memory memory(0,bytes);
    memory.store<std::uint64_t>(gdt+8,0x00CF93000000FFFF);
    memory.store<std::uint32_t>(stack,0xABCDEF00);
    memory.store<std::uint64_t>(stack+4,code);
    Cpu cpu; cpu.gdt_base=gdt; cpu.gdt_limit=31; cpu.registers[esp]=stack;
    function(0x000E6D0C).run(cpu,memory);
    require(memory.load<std::uint64_t>(gdt+8)==code && cpu.cs_selector==8 && (cpu.flags&0x200) &&
        cpu.registers[eax]==0x0000FFFF && cpu.registers[edx]==0x00CF9300 && cpu.registers[esp]==stack+12 && cpu.eip==0xABCDEF00,
        "Original code-segment setup lost its descriptor swap, selector or stack");
    bool rejected=false;
    try { load_code_selector(cpu,memory,16,0); } catch(const GuestTrap& trap) { rejected=trap.vector==13; }
    require(rejected,"Invalid code descriptor did not raise a guest protection exception");
#ifdef __aarch64__
    constexpr auto atomic=gdt+4;
#else
    constexpr auto atomic=gdt+1;
#endif
    memory.store<std::uint32_t>(atomic,0xAABBCCDD);
    require(memory.exchange<std::uint32_t>(atomic,7)==0xAABBCCDD && memory.fetch_add<std::uint32_t>(atomic,3)==7 &&
        memory.load<std::uint32_t>(atomic)==10,"Guest atomic memory lost its prior value or replacement");
    const auto compare=[&]<class T>() {
        constexpr auto expected=static_cast<T>(0xA1B2C3D4),desired=static_cast<T>(0xABCDEF12);
        memory.store<T>(atomic,expected);
        require(memory.compare_exchange<T>(atomic,static_cast<T>(expected^1),desired)==expected && memory.load<T>(atomic)==expected,
            "Failed compare/exchange changed memory or lost the prior value");
        require(memory.compare_exchange<T>(atomic,expected,desired)==expected && memory.load<T>(atomic)==desired,
            "Successful compare/exchange did not store its replacement");
    };
    compare.operator()<std::uint8_t>();compare.operator()<std::uint16_t>();compare.operator()<std::uint32_t>();
#ifdef __aarch64__
    const auto unaligned=[&]<class T>() {
        constexpr auto expected=static_cast<T>(0xA1B2C3D4);
        memory.store<T>(gdt+1,expected);
        const auto reject=[&](auto operation) {
            bool unsupported=false;
            try {operation();} catch(const std::runtime_error& error) {
                unsupported=std::string_view(error.what())=="Unaligned guest atomic memory is unsupported on ARM";
            }
            require(unsupported && memory.load<T>(gdt+1)==expected,"ARM unaligned atomic rejection changed memory or was lost");
        };
        reject([&]{memory.exchange<T>(gdt+1,7);});
        reject([&]{memory.fetch_add<T>(gdt+1,3);});
        reject([&]{memory.compare_exchange<T>(gdt+1,expected,7);});
    };
    unaligned.operator()<std::uint16_t>();unaligned.operator()<std::uint32_t>();
    return 15;
#else
    return 9;
#endif
}
unsigned check_flags_stack() {
    constexpr std::uint32_t stack=0x1000;
    std::array<std::byte,8192> bytes{};Memory memory(0,bytes);
    unsigned checked=0;
    for(const auto original:{0x202U,0x240602U,0x240002U}) {
        Cpu cpu;cpu.registers[esp]=stack;cpu.registers[ecx]=0x000D87E0;
        cpu.registers[ebp]=0x12345678;cpu.registers[ebx]=0x89ABCDEF;cpu.registers[esi]=0x13579BDF;cpu.registers[edi]=0x2468ACE0;
        cpu.flags=original;memory.store<std::uint32_t>(stack,0xABCDEF00);
        const auto saved=cpu.registers;function(0x0027B71B).run(cpu,memory);
        require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+4 && cpu.registers[eax]==0 && cpu.registers[ecx]==0 && cpu.registers[edx]==0 &&
            cpu.registers[ebp]==saved[ebp] && cpu.registers[ebx]==saved[ebx] && cpu.registers[esi]==saved[esi] && cpu.registers[edi]==saved[edi] &&
            (cpu.flags&~arithmetic_flags)==(original&~arithmetic_flags) && (cpu.flags&(carry|zero|parity|sign|overflow))==(zero|parity),
            "Original network callback wrapper lost its saved flags, registers or stack");
        ++checked;
    }
    return checked;
}
unsigned check_mmx_copy() {
    constexpr std::uint32_t stack=0x1000,source=0x200,destination=0x600;
    std::vector<std::byte> bytes(0x2000);
    Memory memory(0,bytes);
    memory.map(0x80000000U,bytes);
    unsigned checked=0;
    for(const auto alias : {0U,0x80000000U}) for(const auto offset : {0U,4U,28U}) for(const auto words : {0U,1U,15U,16U,17U,64U,96U}) {
        std::ranges::fill(bytes,std::byte{0xCD});
        for(unsigned i=0;i<words*4;++i) bytes[source+i]=static_cast<std::byte>((i*73)^0xB9);
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        memory.store<std::uint32_t>(stack+4,(destination+offset)|alias);
        memory.store<std::uint32_t>(stack+8,source|alias);
        memory.store<std::uint32_t>(stack+12,words);
        Cpu cpu; cpu.registers[esp]=stack;
        auto reference_bytes=bytes;
        Memory reference_memory(0,reference_bytes);reference_memory.map(0x80000000U,reference_bytes);
        auto reference=cpu;ExecutionDiagnostic diagnostic;reference.diagnostic=&diagnostic;
        function(0x00219370).run(reference,reference_memory);_mm_sfence();
        function(0x00219370).run(cpu,memory);
        _mm_sfence();
        require(bytes==reference_bytes && cpu.registers==reference.registers && cpu.flags==reference.flags && cpu.eip==reference.eip &&
            cpu.floating.image==reference.floating.image,"Batched original MMX copy differs from individual instructions");
        require(std::equal(bytes.begin()+source,bytes.begin()+source+words*4,bytes.begin()+destination+offset) &&
            cpu.registers[esp]==stack+16 && cpu.eip==0xABCDEF00,"Original MMX copy changed bytes or its native call boundary");
        require(std::uint16_t(cpu.floating.image[8])==255 && std::uint16_t(cpu.floating.image[9])==255,
            "EMMS did not empty the shared x87/MMX register tags");
        ++checked;
    }
    // Range preflight must retain earlier stores if a later MMX store fails.
    std::ranges::fill(bytes,std::byte{0xCD});
    memory.store<std::uint32_t>(stack,0xABCDEF00);memory.store<std::uint32_t>(stack+4,0x1FE0);
    memory.store<std::uint32_t>(stack+8,source);memory.store<std::uint32_t>(stack+12,16);
    auto reference_bytes=bytes;Memory reference_memory(0,reference_bytes);
    Cpu cpu;cpu.registers[esp]=stack;auto reference=cpu;ExecutionDiagnostic diagnostic;reference.diagnostic=&diagnostic;
    std::string actual_error,reference_error;
    try {function(0x00219370).run(reference,reference_memory);} catch(const std::exception& error) {reference_error=error.what();}
    _mm_sfence();
    try {function(0x00219370).run(cpu,memory);} catch(const std::exception& error) {actual_error=error.what();}
    _mm_sfence();
    require(!actual_error.empty() && actual_error==reference_error && bytes==reference_bytes && cpu.floating.image==reference.floating.image,
        "Batched MMX range failure changed the original partial stores or floating state");
    ++checked;
    return checked;
}
CpuChecks check_cpu_batch() {
#define B2_HOST(bits) References{b2_adc##bits##_flags, b2_sbb##bits##_flags, b2_inc##bits##_flags, b2_dec##bits##_flags, b2_neg##bits##_flags, \
    {b2_shl##bits, b2_shr##bits, b2_sar##bits}, b2_mul##bits, b2_imul##bits, b2_div##bits, b2_idiv##bits}
    CpuChecks result;
#ifndef __aarch64__
    result.instruction_cases = check_instructions<8>(B2_HOST(8)) + check_instructions<16>(B2_HOST(16)) + check_instructions<32>(B2_HOST(32));
    result.instruction_cases+=check_rotates<8>(b2_ror8,b2_rcr8)+check_rotates<16>(b2_ror16,b2_rcr16)+check_rotates<32>(b2_ror32,b2_rcr32)+
        check_rotates<8,true>(b2_rol8,b2_rcl8)+check_rotates<16,true>(b2_rol16,b2_rcl16)+check_rotates<32,true>(b2_rol32,b2_rcl32)+
        check_double_shifts<16>(b2_shld16,b2_shrd16)+check_double_shifts<32>(b2_shld32,b2_shrd32);
#undef B2_HOST
    result.memory_cases = check_strings<std::uint8_t>() + check_strings<std::uint16_t>() + check_strings<std::uint32_t>() + check_primary<std::uint64_t>();
#else
    result.memory_cases=check_primary<std::uint64_t>();
#endif
    result.game_cases = check_compare() + check_call_chain();
    result.floating_cases=check_floating_contexts()+check_reciprocal_sqrt()+check_vertex_colors()+check_mmx_copy()+check_matrix();
    result.control_cases=check_control_boundaries()+check_compact_jump_table()+check_segment_setup()+check_flags_stack();
    return result;
}
unsigned check_game_timing(Memory& memory) {
    constexpr std::uint64_t frequency=733333333;
    constexpr std::uint32_t timer=0x2600,ghost=0x8000,input=0x2900,stack=0x4000,buffer=0x6000,ghost_frames=0xA000;
    constexpr std::uint32_t controller=0x2FFD68,returned=0xABCDEF00;
    unsigned cases=0;
    for(bool pal:{false,true}) for(unsigned rate:{30U,60U,144U,360U,1000U,0U}) {
        GameTiming timing;std::uint64_t cycles=0;
        GuestClock clock{&cycles,[](void* value){return *static_cast<std::uint64_t*>(value);}};
        Cpu cpu;cpu.game_timing=&timing;cpu.clock=&clock;
        const auto call=[&](std::uint32_t address,std::uint32_t object=timer,std::initializer_list<std::uint32_t> arguments={}) {
            cpu.registers[esp]=stack;cpu.registers[ecx]=object;memory.store(stack,returned);
            unsigned index=0;for(const auto value:arguments) memory.store(stack+4+index++*4,value);
            invoke_native(cpu,memory,address);
            require(cpu.eip==returned && cpu.registers[esp]==stack+4+arguments.size()*4,"Original timing/replay calling convention changed");
            return cpu.registers[eax];
        };
        const unsigned simulation_rate=pal?50:60,total=simulation_rate*10;
        memory.store<std::uint32_t>(0x352720,pal);
        memory.store<std::uint32_t>(0x34AB5C,0x2500);memory.store<std::uint32_t>(0x2510,0);
        memory.store<std::uint32_t>(0x598190,0);memory.store<std::uint32_t>(0x598194,0);
        call(0xD7240);
        std::memset(memory.access(ghost,64),0,64);
        std::array<std::uint32_t,2> ghost_intervals{};
        for(unsigned slow=0;slow<2;++slow) {
            call(slow?0xD72C0:0xD7300);call(0x7BE10,ghost);
            ghost_intervals[slow]=memory.load<std::uint32_t>(ghost+0x24);
        }
        call(0xD7300);
        memory.store(0x3D686C,buffer);
        for(unsigned player=0;player<4;++player) memory.store<std::uint32_t>(0x2FE370+player*4,0);
        std::memset(memory.access(timer,28),0,28);std::memset(memory.access(0x2FFD88,28),0,28);
        call(0x88850,controller,{0});
        std::memset(memory.access(0x2800,64),0,64);
        // Original ghost playback: 22 half-second samples on a straight path,
        // with no traffic/body subparts. Its real spline and frame-advance code
        // must progress identically when presentation cadence changes.
        std::memset(memory.access(ghost,0x660),0,0x660);
        std::memset(memory.access(ghost_frames,22*0x130),0,22*0x130);
        memory.store<std::uint32_t>(ghost+8,ghost_frames);memory.store<std::uint32_t>(ghost+12,22);
        memory.store<std::uint32_t>(0x4BA93C,0x2700);memory.store<std::uint32_t>(0x272C,0);
        memory.store<std::uint32_t>(0x4B9C58,0x2B00);memory.store<std::uint32_t>(0x3688,0);
        memory.store<std::uint32_t>(0x4B9C70,0);
        for(unsigned sample=0;sample<22;++sample) {
            memory.store<std::uint32_t>(ghost_frames+sample*0x130,0x3F800000);
            memory.store<std::uint32_t>(ghost_frames+sample*0x130+4,std::bit_cast<std::uint32_t>(float(sample)));
        }
        std::array<std::array<std::uint32_t,5>,600> recorded{};
        unsigned steps=0,expected_ticks=0;bool was_slow=false;
        const auto run=[&](const auto& update) {
            cycles=0;timing={};title_timing(cpu,memory);
            for(unsigned frame=1;cycles<10*frequency;++frame) {
                constexpr unsigned varying[]{30,144,60,360};
                cycles=std::min(10*frequency,rate?std::uint64_t(frame)*frequency/rate:cycles+frequency/varying[frame%4]);
                unsigned rendered=0;
                do {
                    title_timing(cpu,memory);
                    if(timing.update) update();
                    rendered+=timing.render;
                } while(timing.pending);
                require(rendered==1,"Catch-up steps rendered extra frames");
            }
        };
        run([&] {
            ++steps;
            const bool slow=steps>=5*simulation_rate && steps<7*simulation_rate;
            const bool paused=steps>=3*simulation_rate && steps<4*simulation_rate;
            if(slow!=was_slow) {call(slow?0xD72C0:0xD7300);was_slow=slow;}
            const auto fixed=(pal?0x3CA3D70AU:0x3C888889U)-(slow?0x01000000U:0);
            require(memory.load<std::uint32_t>(0x598188)==fixed,"Original physics step changed with display rate");
            // Check the initializer on a separate empty object, preserving the
            // active ghost's playback state across updates.
            call(0x7BE10,0x2800);
            require(memory.load<std::uint32_t>(0x2824)==ghost_intervals[slow],"Ghost sample interval changed with display rate");
            memory.store<std::uint32_t>(0x2508,paused);call(0x7BB20,ghost);
            memory.store<std::uint32_t>(timer+0x18,!paused);call(0xD73B0);
            if(!paused) expected_ticks+=slow?1:4;
            const double ghost_progress=memory.load<std::uint32_t>(ghost+0x10)+std::bit_cast<float>(memory.load<std::uint32_t>(ghost+0x14));
            require(std::abs(ghost_progress-double(expected_ticks)/(simulation_rate*2))<0.0001,"Ghost playback speed changed with display rate");
            const auto milliseconds=call(0xD7390);
            require(std::abs(double(milliseconds)-double(expected_ticks)*1000/(simulation_rate*4))<1,"Original timer drifted");
            memory.store<std::uint32_t>(input,steps%2);
            memory.store<std::uint32_t>(input+8,std::bit_cast<std::uint32_t>(float(steps%5)*0.2f));
            memory.store<std::uint32_t>(input+12,std::bit_cast<std::uint32_t>(float(int(steps%9)-4)*0.25f));
            memory.store<std::uint32_t>(input+16,std::bit_cast<std::uint32_t>(float(steps%3)*0.5f));
            call(0x88890,controller,{input});
            require(steps<=total && memory.load<std::uint32_t>(controller+4)==steps,"Replay recording consumed extra inputs");
            std::memcpy(recorded[steps-1].data(),memory.access(input,20),20);
        });
        require(steps==total,"Simulation rate followed display rate");
        call(0x88850,controller,{1});steps=0;
        run([&] {
            call(0x88890,controller,{input});
            require(!std::memcmp(recorded[steps].data(),memory.access(input,20),20),"Replay input round trip changed");
            ++steps;
            require(memory.load<std::uint32_t>(controller+4)==steps,"Replay playback consumed extra inputs");
            if(steps<total-16) require(memory.load<std::uint32_t>(controller+0x14)==0,"Replay restarted before its recorded end");
        });
        require(steps==total,"Replay duration followed display rate");
        // Loading can run inside a blocking constructor, with no main-loop
        // scheduler calls. Its clock must still follow real time at every rate.
        timing.loading_started=false;cycles=0;title_loading(cpu,memory,false);
        unsigned loading_steps=0,fade_units=0;double fade_seconds=0;
        for(unsigned frame=1;cycles<10*frequency;++frame) {
            constexpr unsigned varying[]{30,144,60,360};
            cycles=std::min(10*frequency,rate?std::uint64_t(frame)*frequency/rate:cycles+frequency/varying[frame%4]);
            title_loading(cpu,memory,false);
            loading_steps+=timing.loading_steps;fade_seconds+=timing.loading_delta;
            fade_units+=title_loading_fade(cpu,memory);
        }
        require(loading_steps==total && std::abs(fade_seconds-10)<0.00001,"Loading playback followed the display rate");
        require(std::abs(double(fade_units)-total*(pal?10:8))<=1,"Loading fade lost fractional colour steps at high refresh rates");
        // Time spent changing scenes/loading is not a backlog of racing steps.
        cycles+=20*frequency;memory.store<std::uint32_t>(0x2510,1);title_timing(cpu,memory);
        require(timing.update && timing.render && !timing.pending,"Loading accumulated unwanted simulation steps");
        ++cases;
    }
    return cases;
}
unsigned check_presentation(Memory& memory) {
    auto presentation=std::make_unique<Presentation>();
    GameTiming timing;Cpu cpu;cpu.game_timing=&timing;cpu.presentation=presentation.get();
    constexpr std::uint32_t matrix=0xD000;
    for(unsigned i=0;i<4;++i) memory.store<std::uint32_t>(0x2FE370+i*4,0);
    std::array<float,16> start{};start[0]=start[5]=start[10]=1;
    start[3]=std::bit_cast<float>(0x20003U);
    auto finish=start;finish[0]=-1;finish[10]=-1;finish[12]=6;finish[3]=std::bit_cast<float>(3U);
    unsigned checks=0;
    for(unsigned rate:{30U,60U,144U,360U,1000U}) {
        presentation->reset();timing.step=1;timing.render=true;timing.remainder=0;
        std::memcpy(memory.access(matrix,64),start.data(),64);
        presentation->begin(cpu,memory);presentation->matrix(cpu,memory,matrix);presentation->end(cpu,memory);
        ++timing.step;
        std::memcpy(memory.access(matrix,64),finish.data(),64);
        for(unsigned frame=0;frame<=rate;++frame) {
            timing.remainder=std::uint64_t(frame)*733333332/rate;
            presentation->begin(cpu,memory);presentation->matrix(cpu,memory,matrix);
            std::array<float,16> pose;std::memcpy(pose.data(),memory.access(matrix,64),64);
            require(std::abs(pose[12]-6.f*frame/rate)<0.00001f,"Visual movement did not follow the display fraction");
            for(unsigned row=0;row<3;++row) {
                float length=0;for(unsigned column=0;column<3;++column) length+=pose[row*4+column]*pose[row*4+column];
                require(std::abs(length-1)<0.00001f,"Interpolated wheel/body rotation collapsed its geometry");
            }
            presentation->end(cpu,memory);
            require(!std::memcmp(memory.access(matrix,64),finish.data(),64),"Rendered pose leaked into simulation state");
            ++checks;
        }
    }
    // The ghost draw hook receives 7B280's complete render record. Its header
    // and playback state are not transforms; body, wheels and parts all are.
    presentation->reset();timing.step=1;timing.remainder=0;
    constexpr std::uint32_t ghost_pose=0xE800,ghost_model=0xE000,ghost_body=0xF000;
    const auto old_model=memory.load<std::uint32_t>(0x4BA93C),old_body=memory.load<std::uint32_t>(0x4B9C58);
    memory.store(0x4BA93C,ghost_model);memory.store(0x4B9C58,ghost_body);
    memory.store<std::uint32_t>(ghost_model+0x2C,8);memory.store<std::uint32_t>(ghost_body+0xB88,4);
    constexpr std::array<unsigned,13> ghost_offsets{0x10,0x70,0xB0,0xF0,0x130,0x1B0,0x1F0,0x230,0x270,0x2B0,0x2F0,0x330,0x370};
    std::memset(memory.access(ghost_pose,0x3B4),0x5A,0x3B4);
    for(auto offset:ghost_offsets) std::memcpy(memory.access(ghost_pose+offset,64),start.data(),64);
    presentation->begin(cpu,memory);title_presentation(cpu,memory,0x7BEDE,ghost_pose);presentation->end(cpu,memory);
    ++timing.step;
    for(auto offset:ghost_offsets) std::memcpy(memory.access(ghost_pose+offset,64),finish.data(),64);
    std::array<std::byte,0x3B4> ghost_original;std::memcpy(ghost_original.data(),memory.access(ghost_pose,ghost_original.size()),ghost_original.size());
    for(unsigned fraction:{0U,1U,3U}) {
        timing.remainder=733333333ULL*fraction/4;
        presentation->begin(cpu,memory);title_presentation(cpu,memory,0x7BEDE,ghost_pose);
        for(auto offset:ghost_offsets) require(std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(ghost_pose+offset+48))-1.5f*fraction)<0.00001f,
            "Ghost body, wheels and parts did not follow the display fraction");
        require(memory.load<std::uint32_t>(ghost_pose)==0x5A5A5A5A && memory.load<std::uint32_t>(ghost_pose+0x180)==0x5A5A5A5A,
            "Ghost playback fields were treated as a transform");
        presentation->end(cpu,memory);
        require(!std::memcmp(ghost_original.data(),memory.access(ghost_pose,ghost_original.size()),ghost_original.size()),
            "Ghost display smoothing changed the original playback state");
        ++checks;
    }
    memory.store(0x4BA93C,old_model);memory.store(0x4B9C58,old_body);
    // Exercise the original RenderWare hierarchy and camera callback. Restoring
    // only the camera's local pose would leave interpolated view/frustum data
    // visible to the next simulation update.
    presentation->reset();timing={};timing.started=true;timing.step=1;
    constexpr std::uint32_t frame=0xD200,camera=0xD400,stack=0xC000;
    std::memset(memory.access(frame,0x500),0,0x500);
    memory.store<std::uint32_t>(frame+0x90,camera+8);memory.store<std::uint32_t>(frame+0x94,camera+8);
    memory.store<std::uint32_t>(frame+0xA0,frame);
    memory.store<std::uint32_t>(camera+4,frame);
    memory.store<std::uint32_t>(camera+8,frame+0x90);memory.store<std::uint32_t>(camera+12,frame+0x90);
    memory.store<std::uint32_t>(camera+0x10,0xE82F0);memory.store<std::uint32_t>(camera+0x14,1);
    for(unsigned offset:{0x68U,0x6CU,0x70U,0x74U}) memory.store<std::uint32_t>(camera+offset,std::bit_cast<std::uint32_t>(1.f));
    memory.store<std::uint32_t>(camera+0x80,std::bit_cast<std::uint32_t>(0.1f));
    memory.store<std::uint32_t>(camera+0x84,std::bit_cast<std::uint32_t>(1000.f));
    memory.store<std::uint32_t>(0x5ADD5C,0x5ADD5C);memory.store<std::uint32_t>(0x5ADD60,0x5ADD5C);
    const auto synchronize=[&] {
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,0xABCDEF00);memory.store(stack+4,frame);
        invoke_native(cpu,memory,0xE90D0);
        cpu.registers[esp]=stack;invoke_native(cpu,memory,0xF2620);
    };
    std::memcpy(memory.access(frame+0x10,64),start.data(),64);synchronize();
    title_presentation(cpu,memory,1);title_presentation(cpu,memory,2);
    ++timing.step;timing.remainder=733333333/2;
    std::memcpy(memory.access(frame+0x10,64),finish.data(),64);synchronize();
    std::array<std::byte,0x500> original;std::memcpy(original.data(),memory.access(frame,original.size()),original.size());
    // Presentation re-enters real guest frame/camera code. Preserve live x87,
    // SIMD, flags and registers while still charging that work to scheduling.
    memory.store<std::uint32_t>(0x5ADCB8,0x3FA00000);
    cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,0xABCDEF00);
    invoke_native(cpu,memory,0xEE6D0);
    cpu.floating.mxcsr=0x3F80;
    cpu.preempt=[](Cpu&) {};cpu.quantum_remaining=1024;
    std::uint64_t loading_cycles=733333333;
    GuestClock clock{&loading_cycles,[](void* value){return *static_cast<std::uint64_t*>(value);}};
    cpu.clock=&clock;
    const auto before=cpu;
    {
    FloatingScope guest(cpu.floating);
    title_loading(cpu,memory,false);(void)title_loading_fade(cpu,memory);
    title_presentation(cpu,memory,1);
    require(cpu.registers==before.registers && cpu.flags==before.flags && cpu.eip==before.eip &&
        cpu.quantum_remaining<before.quantum_remaining,"Presentation changed CPU state or rewound its scheduling budget");
    }
    require(cpu.floating.image==before.floating.image && cpu.floating.xmm==before.floating.xmm &&
        cpu.floating.mxcsr==before.floating.mxcsr,"Presentation damaged the guest floating-point context");
    cpu.floating={};
    cpu.registers[esp]=stack;invoke_native(cpu,memory,0xF2620);
    require(std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(frame+0x80))-3.f)<0.00001f,"Original camera hierarchy did not receive the intermediate pose");
    title_presentation(cpu,memory,2);
    cpu.preempt=nullptr;
    require(!std::memcmp(original.data(),memory.access(frame,original.size()),original.size()),"Camera view/frustum changed simulation state after presentation");
    ++checks;
    // Mode changes are cuts even when both views share the same RwFrame and
    // are only a few metres apart. Zoom and near clipping must follow the pose.
    constexpr std::uint32_t wrapper=0xE800;
    std::memset(memory.access(wrapper,0x400),0,0x400);
    memory.store(wrapper,camera);memory.store<std::uint32_t>(wrapper+0x228,1);
    const auto lens=[&](float width,float near) {
        for(unsigned i=0;i<2;++i) {
            memory.store<std::uint32_t>(camera+0x68+i*4,std::bit_cast<std::uint32_t>(width));
            memory.store<std::uint32_t>(camera+0x70+i*4,std::bit_cast<std::uint32_t>(1.f/width));
        }
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,0xABCDEF00);
        memory.store(stack+4,camera);memory.store<std::uint32_t>(stack+8,std::bit_cast<std::uint32_t>(near));
        invoke_native(cpu,memory,0xE8490);synchronize();
    };
    presentation->reset();timing.step=1;timing.update=true;timing.render=true;
    lens(1.f,0.1f);title_presentation(cpu,memory,0xADCA0,wrapper);
    title_presentation(cpu,memory,1);title_presentation(cpu,memory,2);
    for(unsigned mode:{1U,2U,0U}) {
        ++timing.step;memory.store(wrapper+0x22C,mode);
        std::memcpy(memory.access(frame+0x10,64),start.data(),64);lens(1.f,0.1f);
        title_presentation(cpu,memory,0xADCA0,wrapper);title_presentation(cpu,memory,1);
        require(!std::memcmp(memory.access(frame+0x10,64),start.data(),64),"Camera mode switch swept through the previous view");
        title_presentation(cpu,memory,2);
        ++timing.step;
        std::memcpy(memory.access(frame+0x10,64),finish.data(),64);lens(2.f,0.2f);
        title_presentation(cpu,memory,0xADCA0,wrapper);
        std::memcpy(original.data(),memory.access(frame,original.size()),original.size());
        title_presentation(cpu,memory,1);
        require(std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(frame+0x40))-3.f)<0.00001f &&
            std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(camera+0x68))-1.5f)<0.00001f &&
            std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(camera+0x80))-0.15f)<0.00001f,
            "Camera pose, zoom and near clipping used different presentation times");
        title_presentation(cpu,memory,2);
        require(!std::memcmp(original.data(),memory.access(frame,original.size()),original.size()),"Camera zoom restoration changed simulation state");
        ++checks;
    }
    // A long pause must not age the camera out while the car keeps sampling
    // stationary poses. Resume both at the same fraction, including lens state.
    {
        const auto saved_timing=timing;
        const auto saved_scene=memory.load<std::uint32_t>(0x34AB5C);
        memory.store<std::uint32_t>(0x34AB5C,0x2500);
        for(unsigned duration:{1U,120U}) for(unsigned fraction:{0U,1U,3U}) {
            presentation->reset();timing={};timing.started=true;timing.step=1;
            timing.remainder=733333333ULL*fraction/4;
            memory.store<std::uint32_t>(0x2508,0);
            auto view=start,body=start;body[12]=10;
            std::memcpy(memory.access(frame+0x10,64),view.data(),64);lens(1.f,0.1f);
            std::memcpy(memory.access(matrix,64),body.data(),64);
            title_presentation(cpu,memory,0xADCA0,wrapper);
            presentation->begin(cpu,memory);presentation->matrix(cpu,memory,matrix);presentation->end(cpu,memory);
            memory.store<std::uint32_t>(0x2508,1);
            for(unsigned step=0;step<duration;++step) {
                ++timing.step;
                presentation->begin(cpu,memory);presentation->matrix(cpu,memory,matrix);presentation->end(cpu,memory);
            }
            memory.store<std::uint32_t>(0x2508,0);++timing.step;
            view[12]=6;body[12]=16;
            std::memcpy(memory.access(frame+0x10,64),view.data(),64);lens(2.f,0.2f);
            std::memcpy(memory.access(matrix,64),body.data(),64);
            title_presentation(cpu,memory,0xADCA0,wrapper);
            presentation->begin(cpu,memory);presentation->matrix(cpu,memory,matrix);
            const auto camera_x=std::bit_cast<float>(memory.load<std::uint32_t>(frame+0x40));
            const auto car_x=std::bit_cast<float>(memory.load<std::uint32_t>(matrix+48));
            require(std::abs(car_x-camera_x-10)<0.00001f &&
                std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(camera+0x68))-(1.f+float(fraction)/4))<0.00001f,
                "Unpause put the car and camera at different presentation times");
            presentation->end(cpu,memory);
            require(!std::memcmp(memory.access(matrix,64),body.data(),64) &&
                !std::memcmp(memory.access(frame+0x10,64),view.data(),64),"Unpause leaked a display pose into physics");
            ++checks;
        }
        memory.store(0x34AB5C,saved_scene);memory.store<std::uint32_t>(0x2508,0);timing=saved_timing;
    }
    presentation->reset();timing.step=1;
    constexpr std::uint32_t parameters=0xD800,asset=0xD900;
    std::array<float,6> preview{359,5,1,1,std::bit_cast<float>(asset),0};
    memory.store<std::uint32_t>(asset+0x8C,0xE000);
    std::memcpy(memory.access(parameters,24),preview.data(),24);
    presentation->begin(cpu,memory);presentation->preview(cpu,memory,parameters);presentation->end(cpu,memory);
    ++timing.step;preview[0]=1;preview[1]=7;
    std::memcpy(memory.access(parameters,24),preview.data(),24);
    presentation->begin(cpu,memory);presentation->preview(cpu,memory,parameters);
    require(std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(parameters))-360)<0.00001f &&
        std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(parameters+4))-6)<0.00001f,
        "Car-select angle wrap changed the direction of rotation");
    require(memory.load<std::uint32_t>(parameters+16)==asset,"Car-select asset pointer was treated as animation data");
    presentation->end(cpu,memory);
    require(!std::memcmp(memory.access(parameters,24),preview.data(),24),"Car-select animation state was not restored");
    ++checks;
    // A skipped menu draw must also skip cleanup of its absent cdecl args.
    timing.update=timing.render=false;
    cpu.registers[esp]=stack;cpu.registers[ecx]=0x2500;
    memory.store<std::uint32_t>(stack,0xABCDEF00);memory.store<std::uint32_t>(0x2504,0x2500);
    invoke_native(cpu,memory,0x133B0);
    require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+4 && cpu.registers[eax]==0x2500,
        "Skipped menu drawing unbalanced the original scene stack");
    ++checks;
    // Run the title's real rotating-scenery loop, including its pause check
    // and matrix rotation, at different display rates. Two views share the
    // same world and must not advance it twice.
    constexpr std::uint64_t frequency=733333333;
    constexpr std::uint32_t manager=0xE000,master=0xE100,definition=0xE300,table=0xE400,instances=0xE500;
    std::memset(memory.access(manager,0x600),0,0x600);
    memory.store(manager+8,master);memory.store<std::uint32_t>(master+0x28,1);
    memory.store(master+0x2C,definition);memory.store(master+0x34,table);
    memory.store<std::uint32_t>(master+0x5C,1);memory.store<std::uint32_t>(definition+0x68,7);
    memory.store<std::uint32_t>(definition+0x64,std::bit_cast<std::uint32_t>(90.f));
    memory.store(table,instances);memory.store<std::uint32_t>(instances,1);memory.store(instances+4,matrix);
    const auto rotate=[&] {
        cpu.registers[esp]=stack;cpu.registers[ecx]=manager;memory.store<std::uint32_t>(stack,0xABCDEF00);
        invoke_native(cpu,memory,0x8C8D0);
        require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+4,"Scenery animation changed the original calling convention");
    };
    for(bool pal:{false,true}) for(bool slow:{false,true}) for(bool paused:{false,true})
    for(unsigned rate:{30U,50U,60U,144U,360U,1000U,0U}) {
        presentation->reset();timing={};timing.started=true;
        memory.store<std::uint32_t>(0x352720,pal);
        memory.store<std::uint32_t>(0x598188,(pal?0x3CA3D70AU:0x3C888889U)-(slow?0x01000000U:0));
        memory.store<std::uint32_t>(0x34AB5C,0x2500);memory.store<std::uint32_t>(0x2508,paused);
        std::memcpy(memory.access(matrix,64),start.data(),64);
        // EA0E0 normalizes its axis with the Xbox's approximate RSQRTSS.
        // Compare against its actual original-cadence result, not ideal trig.
        cpu.presentation=nullptr;
        for(unsigned update=0;update<(pal?50U:60U);++update) rotate();
        std::array<float,16> reference;std::memcpy(reference.data(),memory.access(matrix,64),64);
        cpu.presentation=presentation.get();
        std::memcpy(memory.access(matrix,64),start.data(),64);
        presentation->begin(cpu,memory);presentation->end(cpu,memory);
        double retention=1;
        for(unsigned display=1;timing.last_cycles<frequency;++display) {
            constexpr unsigned varying[]{30,144,60,360};
            timing.last_cycles=std::min(frequency,rate?std::uint64_t(display)*frequency/rate:
                timing.last_cycles+frequency/varying[display%4]);
            presentation->begin(cpu,memory);
            rotate();
            std::array<std::byte,64> once;std::memcpy(once.data(),memory.access(matrix,64),64);
            rotate();require(!std::memcmp(once.data(),memory.access(matrix,64),64),"A second view advanced the same scenery twice");
            retention*=double(title_blur_alpha(cpu,178,0x4431F8))/255;
            (void)title_blur_alpha(cpu,0,0x443858);
            presentation->end(cpu,memory);
        }
        require(std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(matrix))-reference[0])<0.0002 &&
            std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(matrix+4))-reference[1])<0.0002,
            std::format("Scenery rotation timing: PAL {}, slow {}, paused {}, rate {}, matrix [{},{}], expected [{},{}]",
                pal,slow,paused,rate,std::bit_cast<float>(memory.load<std::uint32_t>(matrix)),
                std::bit_cast<float>(memory.load<std::uint32_t>(matrix+4)),reference[0],reference[1]));
        require(std::abs(std::log(retention)/std::log(178.0/255)-(pal?50:60))<0.1,
            "Original blur history decayed at a different rate per second");
        require(title_blur_alpha(cpu,0,0x4431F8)==0,"Disabled blur changed strength");
        ++timing.step;
        require(title_blur_alpha(cpu,255,0x4431F8)==255,"Full blur changed strength");
        timing.render_frames=1;
        require(title_blur_alpha(cpu,178,0x4431F8)==178,"Original-cadence blur changed strength");
        ++checks;
    }
    // Scroll a real material through 8CA00/D7340, including the clock advance
    // after the first draw of a simulation tick. Extra views cannot change it.
    {
        constexpr std::uint32_t world=0x4000,data=0x5000,material=0x5200,timer=0x2FFD88;
        std::memset(memory.access(world,0xE70),0,0xE70);
        std::memset(memory.access(data,0x240),0,0x240);
        memory.store(world+8,data);memory.store<std::uint32_t>(data+8,1);memory.store(data+12,material);
        memory.store<std::uint32_t>(data+0x5C,1);
        memory.store<std::uint32_t>(data+0xAC,std::bit_cast<std::uint32_t>(1000.f));
        memory.store<std::uint32_t>(material+0x10,0x400);
        memory.store<std::uint8_t>(material+0x20,1);
        memory.store<std::uint32_t>(material+0x24,0x3F800000);
        for(bool pal:{false,true}) for(bool slow:{false,true}) for(bool paused:{false,true}) {
            presentation->reset();timing={};timing.started=true;timing.step=2;
            const auto delta=std::bit_cast<float>((pal?0x3CA3D70AU:0x3C888889U)-(slow?0x01000000U:0));
            memory.store<std::uint32_t>(0x352720,pal);memory.store<std::uint32_t>(0x598190,slow);
            memory.store<std::uint32_t>(0x598188,std::bit_cast<std::uint32_t>(delta));
            std::memset(memory.access(timer,28),0,28);
            memory.store<std::uint32_t>(timer+0x18,!paused);
            if(paused) memory.store<std::uint32_t>(timer+0x10,slow?1:4);
            memory.store<std::uint32_t>(material+0x28,0);memory.store<std::uint32_t>(material+0x2C,0);
            for(unsigned fraction=0;fraction<4;++fraction) {
                timing.update=fraction==0;timing.remainder=frequency*fraction/4;
                memory.store<std::uint32_t>(0x59818C,(slow?1:4)*(fraction?2:1));
                presentation->begin(cpu,memory);
                for(unsigned view=0;view<2;++view) {
                    cpu.registers[esp]=stack;cpu.registers[ecx]=world;
                    memory.store<std::uint32_t>(stack,0xABCDEF00);
                    memory.store<std::uint32_t>(stack+4,0);memory.store<std::uint32_t>(stack+8,1);
                    invoke_native(cpu,memory,0x8CA00);
                    const auto phase=std::bit_cast<float>(memory.load<std::uint32_t>(material+0x2C));
                    require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+12 &&
                        std::abs(phase-delta*(paused?1.f:1.f+float(fraction)/4))<0.000001f,
                        "Material scroll stepped with physics or changed between views");
                    ++checks;
                }
                presentation->end(cpu,memory);
            }
        }
    }
    // Interactive scenery uses physics-owned matrix slots, separate from
    // RenderWare frames. Repeated views and recycled slots must stay correct.
    {
        constexpr std::uint32_t props=0x6000,pose=props+0x14C,first=0x7800,second=0x7900;
        cpu.registers[ebx]=props;memory.store(props+0xD4C,first);
        for(unsigned fraction:{0U,1U,3U}) {
            presentation->reset();timing={};timing.started=true;timing.step=1;
            std::memcpy(memory.access(pose,64),start.data(),64);
            presentation->begin(cpu,memory);title_presentation(cpu,memory,0x8EBC5,pose);presentation->end(cpu,memory);
            ++timing.step;timing.remainder=733333333ULL*fraction/4;
            std::memcpy(memory.access(pose,64),finish.data(),64);
            presentation->begin(cpu,memory);
            for(unsigned view=0;view<2;++view) {
                title_presentation(cpu,memory,0x8EBC5,pose);
                require(std::abs(std::bit_cast<float>(memory.load<std::uint32_t>(pose+48))-1.5f*fraction)<0.00001f,
                    "Interactive scenery stepped with physics or advanced twice between views");++checks;
            }
            presentation->end(cpu,memory);
            require(!std::memcmp(memory.access(pose,64),finish.data(),64),"Prop display pose changed its physics matrix");
            ++timing.step;memory.store(props+0xD4C,second);
            auto replacement=start;replacement[12]=80;
            std::memcpy(memory.access(pose,64),replacement.data(),64);
            presentation->begin(cpu,memory);title_presentation(cpu,memory,0x8EBC5,pose);
            require(!std::memcmp(memory.access(pose,64),replacement.data(),64),"A recycled prop slot inherited another body's motion");
            presentation->end(cpu,memory);memory.store(props+0xD4C,first);++checks;
        }
    }
    // Reward overlays need the original text trails. Only visible driving
    // gauges require scene-only feedback to prevent accumulated dark shading.
    {
        constexpr std::uint32_t hud=0x6000,list=0x6100,widgets=0x6200;
        std::array<std::uint32_t,5> saved{};
        saved[0]=memory.load<std::uint32_t>(0x3D8570);
        memory.store(0x3D8570,hud);memory.store(hud+0x3C,list);memory.store<std::uint32_t>(hud+0x40,33);
        for(unsigned i=0;i<4;++i) {
            saved[i+1]=memory.load<std::uint32_t>(0x3D8580+i*4);
            memory.store(0x3D8580+i*4,widgets+i*16);memory.store(widgets+i*16+8,i);
            memory.store<std::uint32_t>(list+i*16,1);memory.store<std::uint32_t>(list+i*16+8,0);
        }
        require(!title_hud_gauges(memory),"Hidden gauges suppressed reward text trails");++checks;
        for(unsigned i=0;i<4;++i) {
            memory.store<std::uint32_t>(list+i*16,0);
            require(title_hud_gauges(memory),"A visible gauge entered repeated scene feedback");++checks;
            memory.store<std::uint32_t>(list+i*16+8,UINT32_MAX);
            require(!title_hud_gauges(memory),"An empty gauge queue suppressed reward text trails");++checks;
            memory.store<std::uint32_t>(list+i*16,1);
        }
        memory.store(0x3D8570,saved[0]);
        for(unsigned i=0;i<4;++i) memory.store(0x3D8580+i*4,saved[i+1]);
    }
    // Exercise the original boost enable/disable and camera-cut routines,
    // comparing one original draw with several displays in the same tick.
    // Stop A97C0 before it requests a GPU surface: the emitted alpha is already
    // on its stack. Completing its verified A9C65 strength copy needs no GPU.
    constexpr std::uint32_t filter=0x4431F8,reference_filter=0x443858;
    const auto set_blur=[&](std::uint32_t object,unsigned enabled) {
        cpu.registers[esp]=stack;cpu.registers[ecx]=object;
        memory.store<std::uint32_t>(stack,0xABCDEF00);memory.store(stack+4,enabled);
        memory.store<std::uint32_t>(stack+8,enabled?0x3F333333U:0U);
        invoke_native(cpu,memory,0xA9750);
        require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+12,"Boost transition changed the original calling convention");
    };
    const auto draw_blur=[&](std::uint32_t object) {
        cpu.registers[esp]=stack;cpu.registers[ecx]=object;
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        ExecutionDiagnostic diagnostic;diagnostic.break_address=0x215CE0;
        cpu.diagnostic=&diagnostic;bool drew=false;
        try {invoke_native(cpu,memory,0xA97C0);}
        catch(const NativeBoundary& boundary) {require(boundary.address==0x215CE0,"Boost draw failed before producing alpha");drew=true;}
        cpu.diagnostic=nullptr;
        require(drew==(cpu.motion_blur && bool(memory.load<std::uint32_t>(object))),"Disabled boost retained a framebuffer pass");
        if(!drew) return 0U;
        const auto alpha=memory.load<std::uint32_t>(stack-4);
        memory.store(object+8,memory.load<std::uint32_t>(object+4));
        return alpha;
    };
    for(bool pal:{false,true}) for(unsigned displays:{1U,2U,6U,17U}) {
        presentation->reset();timing={};timing.started=true;timing.render_frames=1.0/displays;
        memory.store<std::uint32_t>(0x352720,pal);
        std::memset(memory.access(filter,12),0,12);std::memset(memory.access(reference_filter,12),0,12);
        for(unsigned step=1;step<=8;++step) {
            timing.step=step;
            for(const auto object:{reference_filter,filter}) {
                if(step==1 || step==6) set_blur(object,1);
                if(step==5 || step==8) set_blur(object,0);
                if(step==3) {
                    cpu.registers[esp]=stack;cpu.registers[ecx]=object;
                    memory.store<std::uint32_t>(stack,0xABCDEF00);invoke_native(cpu,memory,0xA9790);
                }
            }
            cpu.presentation=nullptr;const auto reference_alpha=draw_blur(reference_filter);
            cpu.presentation=presentation.get();double retention=1;
            for(unsigned display=0;display<displays;++display) {
                const auto alpha=draw_blur(filter);
                if(!reference_alpha) require(!alpha,"Extra displays shortened the original boost transition/history clear");
                retention*=double(alpha)/255;
            }
            require(std::abs(retention-double(reference_alpha)/255)<0.004,
                "Boost transition retained a different amount of history over one original frame");
            require(!std::memcmp(memory.access(filter,12),memory.access(reference_filter,12),12),
                "Boost presentation changed the original filter state");
            ++checks;
        }
    }
    for(const auto object:{filter,reference_filter}) {
        set_blur(object,1);std::array<std::byte,12> original{};
        std::memcpy(original.data(),memory.access(object,12),12);
        cpu.motion_blur=false;
        require(draw_blur(object)==0 && cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+4,
            "Motion blur off did not use the original disabled return");
        require(!std::memcmp(original.data(),memory.access(object,12),12),"Motion blur off changed the original effect state");
        cpu.motion_blur=true;(void)draw_blur(object);++checks;
    }
    // Build the original widget/queue table, stopping before allocation. Audit
    // shared columns with real TOTAL TIME, SCORE/POS and centered timer types.
    {
        std::array<std::byte,0x90> saved{};
        std::memcpy(saved.data(),memory.access(0x3D8570,saved.size()),saved.size());
        cpu.registers[esp]=stack;cpu.registers[ecx]=0x3D8570;
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        ExecutionDiagnostic diagnostic;diagnostic.break_address=0xC1320;cpu.diagnostic=&diagnostic;
        bool reached=false;
        try {invoke_native(cpu,memory,0x1AA10);}
        catch(const NativeBoundary& boundary) {require(boundary.address==0xC1320,"HUD metadata failed before queue allocation");reached=true;}
        cpu.diagnostic=nullptr;require(reached,"HUD metadata did not reach queue allocation");
        for(const auto entry:{0x15C870U,0x15C930U,0x15C9F0U,0x15CA10U,0x15CA20U,0x15CA40U,0x15CA60U,0x15CAC0U,0x15CAE0U}) {
            cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,0xABCDEF00);invoke_native(cpu,memory,entry);
        }
        memory.store<std::uint32_t>(0x4D4884,std::bit_cast<std::uint32_t>(600.f));
        struct Column {std::uint32_t left;float right,scale;};
        for(const auto column:std::array<Column,4>{{{0x4D2158,600,1},{0x4D207C,600,0.85f},{0x4D1F68,415,1},{0x4D1FE4,806,1}}})
        for(unsigned widget:{8U,10U,24U}) for(float scale:{1.f,0.75f,4.f/7}) {
            HudLayout layout;layout.list=0x7000;layout.scale=scale;cpu.hud_layout=&layout;
            const auto object=memory.load<std::uint32_t>(0x3D8580+widget*4),index=memory.load<std::uint32_t>(object+8);
            const auto x=std::bit_cast<float>(memory.load<std::uint32_t>(column.left));
            cpu.registers[ecx]=object;cpu.registers[esp]=stack;
            memory.store(stack,std::bit_cast<std::uint32_t>(x));memory.store(stack+8,std::bit_cast<std::uint32_t>(column.scale));
            title_hud_widget(cpu,memory);
            const auto transform=layout.transform(layout.list+index*16);
            require(std::abs(layout.anchors[index]-column.right)<0.0001f &&
                std::abs((x*transform[0]+transform[1])-(column.right-(column.right-x)*scale))<0.0001f,
                "A mode's right HUD column drifted from its authored right margin");++checks;
        }
        struct Other {unsigned widget;float x,anchor;bool active;};
        for(const auto sample:std::array<Other,8>{{{0,600,600,true},{2,40,40,true},{4,320,320,true},{4,231.5f,231.5f,true},
                {4,622.5f,622.5f,true},{28,0,600,true},{27,504,0,false},{31,504,0,false}}}) {
            HudLayout layout;cpu.hud_layout=&layout;
            const auto object=memory.load<std::uint32_t>(0x3D8580+sample.widget*4),index=memory.load<std::uint32_t>(object+8);
            cpu.registers[ecx]=object;cpu.registers[esp]=stack;
            memory.store(stack,std::bit_cast<std::uint32_t>(sample.x));memory.store<std::uint32_t>(stack+8,0x3F800000);
            title_hud_widget(cpu,memory);
            require(bool(layout.active&(1ULL<<index))==sample.active && (!sample.active || layout.anchors[index]==sample.anchor),
                "Shared column anchoring moved a centered, independent or projected widget");++checks;
        }
        cpu.hud_layout=nullptr;std::memcpy(memory.access(0x3D8570,saved.size()),saved.data(),saved.size());
    }
    return checks;
}
unsigned check_cpu_image(const std::filesystem::path& path) {
    Xbe image(path);
    require(image.supported(),"CPU image checks require the verified target");
    std::vector<std::byte> bytes(image.base+image.image_size);
    image.load(std::span(bytes).subspan(image.base,image.image_size));
    Memory memory(0,bytes);
    constexpr std::uint32_t stack=0x4000,begin=0x1800,size=0x1000;
    std::array<std::byte,size> expected{};
    unsigned cases=0;
    for(bool ultrawide:{false,true}) for(unsigned split:{0U,2U}) {
        Cpu cpu;cpu.ultrawide=ultrawide;cpu.registers[ecx]=0x3D8570;cpu.registers[esp]=stack;
        memory.store<std::uint32_t>(stack,0xABCDEF00);memory.store<std::uint32_t>(stack+4,1);
        memory.store<std::uint32_t>(0x3D8570+0x6AC7C,split);invoke_native(cpu,memory,0xB8700);
        const auto aspect=ultrawide?(split?0x3F955555U:0x40155555U):(split?0x3F638E39U:0x3FE38E39U);
        require(memory.load<std::uint32_t>(0x3D8570+0x6AC84)==aspect && memory.load<std::uint32_t>(0x3D8570+0x6AC84+0x660)==aspect,
            "Selected full/split camera aspect was not applied to both views");
        require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+8,"Camera aspect changed the calling convention");++cases;
    }
    // The PRESS START screen is intro record 5; component 17 is save/load.
    require(memory.load<std::uint32_t>(0x333FCC)==std::bit_cast<std::uint32_t>(5.f) &&
        memory.load<std::uint32_t>(0x333FD8)==std::bit_cast<std::uint32_t>(30.f) &&
        memory.load<std::uint32_t>(0x333FDC)==1,"Original logo/title sequence changed");
    for(bool enabled:{false,true}) for(unsigned screen:{17U,19U}) for(unsigned step:{0U,4U,5U}) {
        memory.store<std::uint32_t>(0x4CCAA8,screen);memory.store<std::uint32_t>(0x4CCAB8,17);
        memory.store<std::uint32_t>(0x4CCAAC,0xFFFFFFFF);memory.store<std::uint32_t>(0x4CCAB4,19);
        memory.store<std::uint32_t>(0x4E2CBC,2);
        constexpr std::uint32_t intro=0x2600;
        memory.store<std::uint32_t>(intro+8,step);memory.store<std::uint32_t>(intro+4,std::bit_cast<std::uint32_t>(0.25f));
        memory.store<std::uint32_t>(stack,0xABCDEF00);memory.store<std::uint32_t>(stack+4,0x3C888889);
        Cpu cpu;cpu.skip_intro=enabled;cpu.registers[esp]=stack;cpu.registers[ecx]=intro;cpu.eip=0x4C9F0;
        const auto registers=cpu.registers;skip_title_intro(cpu,memory);
        const bool skipped=enabled && screen==19 && step<5;
        require(cpu.registers==registers && cpu.eip==0x4C9F0 && memory.load<std::uint32_t>(stack)==0xABCDEF00 &&
            memory.load<std::uint32_t>(stack+4)==0x3C888889 && memory.load<std::uint32_t>(0x4CCAAC)==0xFFFFFFFF &&
            memory.load<std::uint32_t>(0x4E2CBC)==2 && memory.load<std::uint32_t>(intro+8)==(skipped?4:step) &&
            memory.load<std::uint32_t>(intro+4)==std::bit_cast<std::uint32_t>(skipped?5.f:0.25f),
            "Intro skip bypassed PRESS START, reset its timeout or changed a disabled update");
        ++cases;
    }
    for(unsigned alignment=0;alignment<4;++alignment) for(int displacement:{-7,0,1,7,128})
        for(unsigned length:{0U,1U,2U,3U,4U,5U,7U,8U,15U,16U,31U,32U,33U,64U,65U,127U}) {
            for(unsigned i=0;i<size;++i) bytes[begin+i]=static_cast<std::byte>((i*73)^0xA9);
            std::copy_n(bytes.begin()+begin,size,expected.begin());
            const auto source=0x2000+alignment,destination=source+displacement;
            std::memmove(expected.data()+(destination-begin),expected.data()+(source-begin),length);
            memory.store<std::uint32_t>(stack,0xABCDEF00); memory.store<std::uint32_t>(stack+4,destination);
            memory.store<std::uint32_t>(stack+8,source); memory.store<std::uint32_t>(stack+12,length);
            Cpu cpu; cpu.registers[esp]=stack; cpu.registers[ebp]=0x13579BDF;
            cpu.registers[edi]=0xA1B2C3D4; cpu.registers[esi]=0x98765432;
            function(0x0011EFB0).run(cpu,memory);
            require(std::equal(expected.begin(),expected.end(),bytes.begin()+begin) && cpu.registers[eax]==destination &&
                cpu.registers[esp]==stack+4 && cpu.registers[ebp]==0x13579BDF && cpu.registers[edi]==0xA1B2C3D4 &&
                cpu.registers[esi]==0x98765432 && cpu.eip==0xABCDEF00 && !(cpu.flags&direction),
                "Original memory-copy table changed overlap, alignment, bounds or its calling convention");
            ++cases;
        }
    // Original vector normalization must retain the console estimate: even a
    // unit vector is slightly shorter than one. A host estimate changes replays.
    require(memory.load<std::uint32_t>(0x000EA7BD)==0xC0520FF3,"Original RSQRTSS bytes changed");
    for(const auto& sample:std::array{std::array{1.f,0.f,0.f},std::array{1.f,1.f,0.f}}) {
        for(unsigned i=0;i<3;++i) memory.store<std::uint32_t>(0x2400+i*4,std::bit_cast<std::uint32_t>(sample[i]));
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        memory.store<std::uint32_t>(stack+4,0x2500);memory.store<std::uint32_t>(stack+8,0x2400);
        Cpu cpu;cpu.registers[esp]=stack;function(0x000EA760).run(cpu,memory);
        const auto normalized=sample[1] ? 0x3F34F800U : 0x3F7FF000U;
        require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+4 && cpu.floating.xmm[0][0]==normalized &&
            memory.load<std::uint32_t>(0x2500)==normalized &&
            memory.load<std::uint32_t>(0x2504)==(sample[1] ? normalized : 0U) && memory.load<std::uint32_t>(0x2508)==0,
            "Original vector normalization lost the Xbox reciprocal-square-root approximation");
        ++cases;
    }
    // Replay the original queued Crash-mode draw callback without booting.
    // It draws a background and optional filled/unfilled bar segments.
    constexpr std::uint32_t record=0x2000,transform=0x2200,queue=0x2300,owner=0x2400,vertices=0x0057A448;
    struct DrawCase {float border_x,border_y,fill,last_x,last_y;unsigned vertex_count;};
    constexpr DrawCase draws[]={{0,0,0,110,60,10},{0.1f,0,1,100,60,10},{0.1f,0.125f,0.5f,100,55,16}};
    const auto store_float=[&](std::uint32_t address,float value) {memory.store<std::uint32_t>(address,std::bit_cast<std::uint32_t>(value));};
    const auto load_float=[&](std::uint32_t address) {return std::bit_cast<float>(memory.load<std::uint32_t>(address));};
    for(unsigned aspect=0;aspect<7;++aspect) for(const auto& sample:draws) {
        std::fill_n(bytes.begin()+record,0x500,std::byte{});
        constexpr float position[]={10,20,100,40,8,16,24,128},identity[]={0,0,1,1,1,1,1,1};
        for(unsigned i=0;i<8;++i) {
            store_float(record+8+i*4,position[i]);store_float(transform+i*4,identity[i]);
        }
        for(unsigned i=0;i<4;++i) store_float(record+0x28+i*4,1);
        store_float(record+0x38,sample.border_x);store_float(record+0x3C,sample.border_y);
        store_float(record+0x40,sample.fill);memory.store<std::uint32_t>(record+0x48,transform);
        memory.store<std::uint32_t>(record+0x50,0x00034C20);
        memory.store<std::uint32_t>(queue+4,owner);memory.store<std::uint32_t>(owner+8,record);
        memory.store<std::uint32_t>(vertices+0x1C00,0);store_float(vertices+0x1C04,0.75f);
        memory.store<std::uint32_t>(vertices+0x1C10,0);
        store_float(0x002C4F70,640);store_float(0x002C4F6C,480);
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        ExecutionDiagnostic diagnostic;diagnostic.remaining=10000;diagnostic.watch_address=0x00034C20;
        Cpu cpu;cpu.registers[esp]=stack;cpu.registers[ecx]=queue;cpu.diagnostic=&diagnostic;
        HudLayout hud;hud.list=aspect==2?queue+16:queue;hud.active=1;hud.scale=0.75f;hud.anchors[0]=640;
        if(aspect) cpu.hud_layout=&hud;
        // Run the real queue and Crash text/bar callback at an intermediate
        // display time, including the existing widescreen transform.
        std::unique_ptr<Presentation> presentation;
        GameTiming timing;
        if(aspect>=3) {
            presentation=std::make_unique<Presentation>();cpu.presentation=presentation.get();cpu.game_timing=&timing;
            timing.step=1;timing.render=true;timing.started=true;
            for(unsigned i=0;i<4;++i) memory.store<std::uint32_t>(0x2FE370+i*4,0);
            store_float(record+8,-30);
            // A mirrored rectangle is a new orientation, not a scale tween
            // through zero. Exercise both axes with the original draw callback.
            if(aspect==5) store_float(record+0x10,-100);
            if(aspect==6) store_float(record+0x14,-40);
            presentation->begin(cpu,memory);function(0x000C1210).run(cpu,memory);presentation->end(cpu,memory);
            memory.store<std::uint32_t>(stack,0xABCDEF00);cpu.registers[esp]=stack;cpu.registers[ecx]=queue;
            memory.store<std::uint32_t>(vertices+0x1C00,0);store_float(record+8,10);
            store_float(record+0x10,100);store_float(record+0x14,40);
            timing.step=2;timing.remainder=733333333/4;
            if(aspect==4) {
                // A page switch keeps this node but hides it immediately.
                // Old position/opacity must never reappear on the next page.
                title_ui(cpu,memory,0,0);store_float(record+0x24,0);
            }
            presentation->begin(cpu,memory);timing.render_frames=0.25;
        }
        function(0x000C1210).run(cpu,memory);
        if(presentation) presentation->end(cpu,memory);
        const auto last=vertices+(sample.vertex_count-1)*28;
        const auto hud_x=[&](float x) {return aspect==3?(x-30)*0.75f+160:aspect==1 || aspect>=4?x*0.75f+160:x;};
        const auto alpha=aspect==4?0U:0x80000000U;
        require(diagnostic.reached_watch && cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+4 &&
            memory.load<std::uint32_t>(vertices+0x1C00)==sample.vertex_count &&
            load_float(vertices)==hud_x(10) && load_float(vertices+4)==20 &&
            memory.load<std::uint32_t>(vertices+16)==alpha &&
            load_float(last)==hud_x(sample.last_x) && load_float(last+4)==sample.last_y &&
            load_float(record+8)==10 && load_float(record+16)==100 &&
            memory.load<std::uint32_t>(last+16)==(alpha|0x00081018),
            std::format("Original Crash-mode UI queue differs for border ({}, {}), fill {}: callback {}, return {}, stack {}, vertices {}, first ({}, {}, {:08X}), last ({}, {}, {:08X})",
                sample.border_x,sample.border_y,sample.fill,diagnostic.reached_watch,hex32(cpu.eip),hex32(cpu.registers[esp]),
                memory.load<std::uint32_t>(vertices+0x1C00),load_float(vertices),load_float(vertices+4),memory.load<std::uint32_t>(vertices+16),
                load_float(last),load_float(last+4),memory.load<std::uint32_t>(last+16)));
        ++cases;
    }
    {
        // The original driving-message constructor shares a queue between
        // screen-centred text and right-aligned text above the boost meter.
        constexpr std::uint32_t manager=0x2500,message=0x2600,strings=0x2700;
        constexpr std::array globals{0x352728U,0x35272CU,0x4D23ACU};
        std::array<std::uint32_t,globals.size()> saved{};
        for(unsigned i=0;i<globals.size();++i) saved[i]=memory.load<std::uint32_t>(globals[i]);
        memory.store<std::uint32_t>(globals[0],0);memory.store(globals[1],strings);store_float(globals[2],18);
        for(bool wide:{false,true}) for(bool centred:{false,true}) {
            std::fill_n(bytes.begin()+record,0x900,std::byte{});
            memory.store(manager+0x3C,queue);memory.store(queue+4,owner);
            memory.store<std::uint32_t>(queue+8,0xFFFFFFFF);
            memory.store(owner+8,record);memory.store(owner+0x18,record);
            memory.store<std::uint32_t>(strings,0x2800);
            const float x=centred?280.f:126.f,width=centred?134.f:104.f;
            const std::array arguments{manager,0U,transform,std::bit_cast<std::uint32_t>(x),
                std::bit_cast<std::uint32_t>(2.f),std::bit_cast<std::uint32_t>(centred?0.5f:1.f),
                std::bit_cast<std::uint32_t>(width),0U,0U};
            memory.store<std::uint32_t>(stack,0xABCDEF00);
            for(unsigned i=0;i<arguments.size();++i) memory.store(stack+4+i*4,arguments[i]);
            HudLayout hud;hud.list=queue;hud.active=1;hud.scale=wide?0.75f:1.f;hud.anchors[0]=40;
            Cpu cpu;cpu.registers[esp]=stack;cpu.registers[ecx]=message;cpu.hud_layout=&hud;
            function(0x0001D5C0).run(cpu,memory);
            const auto left=40+load_float(record+8)*hud.scale;
            const auto expected=centred?(wide?269.75f:253.f):(wide?56.5f:62.f);
            require(cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+40 &&
                memory.load<std::uint32_t>(message)==record+8 && std::abs(left-expected)<0.0001f &&
                load_float(record+12)==-16 && load_float(record+16)==width && load_float(record+20)==18,
                "Driving text lost its screen-centre or boost-meter anchor");
            ++cases;
        }
        for(unsigned i=0;i<globals.size();++i) memory.store(globals[i],saved[i]);
    }
    {
        constexpr std::uint32_t vehicle=0x2800,holder=0x2B00,camera=0x2C00,frame=0x2D00;
        const auto saved_camera=memory.load<std::uint32_t>(0x443EB0);
        const auto saved_distance=memory.load<std::uint32_t>(0x4B30A0);
        const auto store_float=[&](std::uint32_t address,float value) {memory.store<std::uint32_t>(address,std::bit_cast<std::uint32_t>(value));};
        memory.store<std::uint32_t>(0x443EB0,holder);memory.store<std::uint32_t>(holder,camera);
        memory.store<std::uint32_t>(camera+4,frame);
        // Capture frame-153796275900: the nearby sedan's body was 34% opaque
        // because the player wreck was roughly 241 units away from it.
        const std::array<float,3> viewpoint{-1531.203f,72.14848f,-1318.361f};
        const std::array<float,3> sedan{-1532.230f,59.40054f,-1362.379f};
        for(unsigned axis=0;axis<3;++axis) {
            store_float(frame+0x40+axis*4,viewpoint[axis]);
            store_float(vehicle+0x30+axis*4,sedan[axis]);
        }
        store_float(0x4B30A0,241.40826f);store_float(vehicle+0x1E0,0.6f);
        Cpu cpu;cpu.registers.fill(0xABCD1234);cpu.flags=0x246;
        cpu.registers[esp]=stack;memory.store<std::uint32_t>(stack,0xABCDEF00);
        memory.store<std::uint32_t>(0x5ADCB8,0x3FA00000);invoke_native(cpu,memory,0xEE6D0);
        cpu.floating.mxcsr=0x5F81;
        const auto before=cpu;
        {
        FloatingScope guest(cpu.floating);
        require(title_traffic_fade(cpu,memory,vehicle)==0x3F800000,
            "Crash flyover body opacity still follows the distant player wreck");
        for(unsigned axis=0;axis<3;++axis) for(const auto [distance,opacity]:
            {std::pair{0.f,1.f},{225.f,1.f},{237.5f,0.5f},{250.f,0.f},{300.f,0.f}}) {
            for(unsigned i=0;i<3;++i) {
                store_float(frame+0x40+i*4,0);
                store_float(vehicle+0x30+i*4,i==axis?distance:0.f);
            }
            require(title_traffic_fade(cpu,memory,vehicle)==std::bit_cast<std::uint32_t>(opacity),
                "Traffic camera fade changed its original range or curve");
            ++cases;
        }
        }
        require(cpu.floating.image==before.floating.image && cpu.floating.xmm==before.floating.xmm &&
            cpu.floating.mxcsr==before.floating.mxcsr,"Traffic fade damaged the guest floating-point context");
        require(cpu.registers==before.registers && cpu.flags==before.flags &&
            memory.load<std::uint32_t>(vehicle+0x1E0)==std::bit_cast<std::uint32_t>(0.6f) &&
            memory.load<std::uint32_t>(0x4B30A0)==std::bit_cast<std::uint32_t>(241.40826f),
            "Traffic visual fade changed vehicle alpha, simulation distance or guest registers");
        memory.store<std::uint32_t>(0x443EB0,saved_camera);memory.store<std::uint32_t>(0x4B30A0,saved_distance);
        ++cases;
    }
    return cases+check_game_timing(memory)+check_presentation(memory);
}

CpuTiming benchmark_call_chain(std::uint32_t iterations) {
    const auto& constructor = function(constructor_address);
    constexpr std::uint32_t base = 0x1000, stack = base + 512;
    std::array<std::byte, 1024> bytes{};
    Memory memory(base, bytes);
    memory.store<std::uint32_t>(stack, 0xABCDEF00);
    Cpu cpu;
    std::uint64_t checksum = 0;
    const auto run = [&](std::uint32_t count) {
        for (std::uint32_t i = 0; i < count; ++i) {
            cpu.registers[esp] = stack;
            cpu.registers[ecx] = base + (i & 3U) * 64U;
            constructor.run(cpu, memory);
            checksum += memory.load<std::uint32_t>(cpu.registers[ecx]) + std::uint64_t(cpu.registers[edx]);
        }
    };
    run(10000);
    checksum = 0;
    const auto start = std::chrono::steady_clock::now();
    run(iterations);
    const auto elapsed = std::chrono::duration<double, std::nano>(std::chrono::steady_clock::now() - start).count();
    return {elapsed / iterations, checksum};
}
MatrixTiming benchmark_matrix(std::uint32_t iterations) {
    std::array<std::byte,1024> bytes{};Memory memory(0x1000,bytes);
    for(unsigned i=0;i<32;++i) memory.store<std::uint32_t>(0x1000+i*4,std::bit_cast<std::uint32_t>(float(int(i%9)-4)/17.f));
    memory.store<std::uint32_t>(0x1380,0xABCDEF00);memory.store<std::uint32_t>(0x1384,0x1080);
    memory.store<std::uint32_t>(0x1388,0x1000);memory.store<std::uint32_t>(0x138C,0x1040);
    const auto measure=[&](auto run) {
        Cpu cpu;FloatingScope scope(cpu.floating);std::uint64_t checksum=0;
        const auto repeat=[&](unsigned count) {
            for(unsigned i=0;i<count;++i) {
                cpu.registers[esp]=0x1380;run(cpu,memory);
                checksum+=memory.load<std::uint32_t>(0x1080+(i%16)*4);
            }
        };
        repeat(1000);checksum=0;
        const auto started=std::chrono::steady_clock::now();repeat(iterations);
        const auto ns=std::chrono::duration<double,std::nano>(std::chrono::steady_clock::now()-started).count();
        return CpuTiming{ns/iterations,checksum};
    };
    return {measure(guest_00227BD9),measure(reference_matrix)};
}
}
