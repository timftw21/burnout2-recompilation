#include "cpu_checks.h"
#include "cpu.h"
#include "file.h"
#include "xbe.h"
#include <algorithm>
#include <chrono>
#include <cmath>

extern "C" {
#define B2_REFERENCES(bits) \
    std::uint32_t b2_adc##bits##_flags(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint32_t b2_sbb##bits##_flags(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint32_t b2_inc##bits##_flags(std::uint32_t, std::uint32_t); \
    std::uint32_t b2_dec##bits##_flags(std::uint32_t, std::uint32_t); \
    std::uint32_t b2_neg##bits##_flags(std::uint32_t, std::uint32_t); \
    std::uint64_t b2_shl##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t b2_shr##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t b2_sar##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t b2_ror##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t b2_rcr##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t b2_rol##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t b2_rcl##bits(std::uint32_t, std::uint32_t, std::uint32_t); \
    std::uint64_t b2_mul##bits(std::uint32_t, std::uint32_t, std::uint32_t*); \
    std::uint64_t b2_imul##bits(std::uint32_t, std::uint32_t, std::uint32_t*); \
    std::uint64_t b2_div##bits(std::uint64_t, std::uint32_t); \
    std::uint64_t b2_idiv##bits(std::uint64_t, std::uint32_t);
B2_REFERENCES(8)
B2_REFERENCES(16)
B2_REFERENCES(32)
#undef B2_REFERENCES
std::uint64_t b2_shld16(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint64_t b2_shld32(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint64_t b2_shrd16(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint64_t b2_shrd32(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t);
std::uint32_t b2_compare8_flags(std::uint32_t, std::uint32_t);
std::uint32_t b2_and32_flags(std::uint32_t, std::uint32_t);
void b2_string_move(void*, const void*, std::uint32_t, std::uint32_t, std::uint32_t);
void b2_string_fill(void*, std::uint32_t, std::uint32_t, std::uint32_t, std::uint32_t);
void b2_fp_snapshot(void*);
}

namespace b2 {
void guest_00227BD9(Cpu&,Memory&);
void reference_matrix(Cpu&,Memory&);
namespace {
using CarryReference = std::uint32_t (*)(std::uint32_t, std::uint32_t, std::uint32_t);
using UnaryReference = std::uint32_t (*)(std::uint32_t, std::uint32_t);
using ShiftReference = std::uint64_t (*)(std::uint32_t, std::uint32_t, std::uint32_t);
using MultiplyReference = std::uint64_t (*)(std::uint32_t, std::uint32_t, std::uint32_t*);
using DivideReference = std::uint64_t (*)(std::uint64_t, std::uint32_t);
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
                checked += 2;
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

template<class T> unsigned check_strings() {
    unsigned checked = 0;
    constexpr std::uint32_t base = 0x1000, source = 80;
    for (const bool descending : {false, true}) {
        for (const std::uint32_t destination : {20U, 79U, 80U, 81U, 120U}) {
            for (const std::uint32_t count : {0U, 1U, 7U, 16U}) {
                std::array<std::byte, 256> model, reference;
                for (std::size_t i = 0; i < model.size(); ++i) model[i] = std::byte((i * 17U) & 255U);
                reference = model;
                const auto offset = descending && count ? (count - 1U) * sizeof(T) : 0U;
                Memory memory(base, model);
                memory.move_elements<T>(static_cast<std::uint32_t>(base + destination + offset),
                    static_cast<std::uint32_t>(base + source + offset), count, descending);
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
        if (left == right) { expected_flags = b2_and32_flags(0, 0); defined &= ~auxiliary; }
        else if (mismatch < 0) expected_flags = b2_compare8_flags(79, 79);
        else {
            const auto less = comparison < 0 ? 1U : 0U;
            expected_flags = b2_sbb32_flags(0U - less, UINT32_MAX, less);
        }
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
            (cpu.flags & (arithmetic_flags & ~auxiliary)) == (b2_and32_flags(0, 0) & (arithmetic_flags & ~auxiliary)) &&
            (cpu.flags & ~arithmetic_flags) == (flags & ~arithmetic_flags),
            "Recovered three-function constructor chain corrupted registers, flags, stack or object data");
    }
    return 2;
}
unsigned check_matrix() {
    constexpr std::uint32_t base=0x1000,stack=0x1380;
    const auto& matrix=function(0x00227BD9);
    unsigned checked=0;
    for(unsigned precision:{0U,2U,3U}) for(unsigned rounding=0;rounding<4;++rounding)
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
            const auto control=std::uint16_t(0x007F|(precision<<8)|(rounding<<10));
            std::memcpy(original.floating.image.data(),&control,2);
            auto cpu=original,reference=original;
            {FloatingScope scope(reference.floating);reference_matrix(reference,reference_memory);}
            matrix.run(cpu,memory);
            require(bytes==reference_bytes && cpu.registers==reference.registers && cpu.flags==reference.flags && cpu.eip==reference.eip &&
                cpu.floating.instruction==reference.floating.instruction && cpu.floating.opcode==reference.floating.opcode &&
                cpu.floating.data==reference.floating.data && cpu.floating.code_selector==reference.floating.code_selector &&
                cpu.floating.data_selector==reference.floating.data_selector &&
                std::memcmp(cpu.floating.image.data(),reference.floating.image.data(),12)==0,
                std::format("Fused original matrix differs: precision {}, rounding {}, alias {}, sample {}",precision,rounding,alias,sample));
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
    std::uint64_t (*left)(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t),
    std::uint64_t (*right)(std::uint32_t,std::uint32_t,std::uint32_t,std::uint32_t)) {
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
    memory.store<std::uint32_t>(gdt+1,0xAABBCCDD);
    require(memory.exchange<std::uint32_t>(gdt+1,7)==0xAABBCCDD && memory.fetch_add<std::uint32_t>(gdt+1,3)==7 &&
        memory.load<std::uint32_t>(gdt+1)==10,"Unaligned guest atomic memory differs from native x86");
    const auto compare=[&]<class T>() {
        constexpr auto expected=static_cast<T>(0xA1B2C3D4),desired=static_cast<T>(0xABCDEF12);
        memory.store<T>(gdt+1,expected);
        require(memory.compare_exchange<T>(gdt+1,static_cast<T>(expected^1),desired)==expected && memory.load<T>(gdt+1)==expected,
            "Failed unaligned compare/exchange changed memory or lost the prior value");
        require(memory.compare_exchange<T>(gdt+1,expected,desired)==expected && memory.load<T>(gdt+1)==desired,
            "Successful unaligned compare/exchange did not store its replacement");
    };
    compare.operator()<std::uint8_t>();compare.operator()<std::uint16_t>();compare.operator()<std::uint32_t>();
    return 9;
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
    unsigned checked=0;
    for(const auto offset : {0U,4U,28U}) for(const auto words : {0U,1U,15U,16U,17U,64U,96U}) {
        std::ranges::fill(bytes,std::byte{0xCD});
        for(unsigned i=0;i<words*4;++i) bytes[source+i]=static_cast<std::byte>((i*73)^0xB9);
        memory.store<std::uint32_t>(stack,0xABCDEF00);
        memory.store<std::uint32_t>(stack+4,destination+offset);
        memory.store<std::uint32_t>(stack+8,source);
        memory.store<std::uint32_t>(stack+12,words);
        Cpu cpu; cpu.registers[esp]=stack;
        function(0x00219370).run(cpu,memory);
        _mm_sfence();
        require(std::equal(bytes.begin()+source,bytes.begin()+source+words*4,bytes.begin()+destination+offset) &&
            cpu.registers[esp]==stack+16 && cpu.eip==0xABCDEF00,"Original MMX copy changed bytes or its native call boundary");
        require(std::uint16_t(cpu.floating.image[8])==255 && std::uint16_t(cpu.floating.image[9])==255,
            "EMMS did not empty the shared x87/MMX register tags");
        ++checked;
    }
    return checked;
}
CpuChecks check_cpu_batch() {
#define B2_HOST(bits) References{b2_adc##bits##_flags, b2_sbb##bits##_flags, b2_inc##bits##_flags, b2_dec##bits##_flags, b2_neg##bits##_flags, \
    {b2_shl##bits, b2_shr##bits, b2_sar##bits}, b2_mul##bits, b2_imul##bits, b2_div##bits, b2_idiv##bits}
    CpuChecks result;
    result.instruction_cases = check_instructions<8>(B2_HOST(8)) + check_instructions<16>(B2_HOST(16)) + check_instructions<32>(B2_HOST(32));
    result.instruction_cases+=check_rotates<8>(b2_ror8,b2_rcr8)+check_rotates<16>(b2_ror16,b2_rcr16)+check_rotates<32>(b2_ror32,b2_rcr32)+
        check_rotates<8,true>(b2_rol8,b2_rcl8)+check_rotates<16,true>(b2_rol16,b2_rcl16)+check_rotates<32,true>(b2_rol32,b2_rcl32)+
        check_double_shifts<16>(b2_shld16,b2_shrd16)+check_double_shifts<32>(b2_shld32,b2_shrd32);
#undef B2_HOST
    result.memory_cases = check_strings<std::uint8_t>() + check_strings<std::uint16_t>() + check_strings<std::uint32_t>();
    result.game_cases = check_compare() + check_call_chain();
    result.floating_cases=check_floating_contexts()+check_reciprocal_sqrt()+check_vertex_colors()+check_mmx_copy()+check_matrix();
    result.control_cases=check_control_boundaries()+check_compact_jump_table()+check_segment_setup()+check_flags_stack();
    return result;
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
    for(const auto& sample:draws) {
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
        function(0x000C1210).run(cpu,memory);
        const auto last=vertices+(sample.vertex_count-1)*28;
        require(diagnostic.reached_watch && cpu.eip==0xABCDEF00 && cpu.registers[esp]==stack+4 &&
            memory.load<std::uint32_t>(vertices+0x1C00)==sample.vertex_count &&
            load_float(vertices)==10 && load_float(vertices+4)==20 &&
            memory.load<std::uint32_t>(vertices+16)==0x80000000 &&
            load_float(last)==sample.last_x && load_float(last+4)==sample.last_y &&
            memory.load<std::uint32_t>(last+16)==0x80081018,
            std::format("Original Crash-mode UI queue differs for border ({}, {}), fill {}: callback {}, return {}, stack {}, vertices {}, first ({}, {}, {:08X}), last ({}, {}, {:08X})",
                sample.border_x,sample.border_y,sample.fill,diagnostic.reached_watch,hex32(cpu.eip),hex32(cpu.registers[esp]),
                memory.load<std::uint32_t>(vertices+0x1C00),load_float(vertices),load_float(vertices+4),memory.load<std::uint32_t>(vertices+16),
                load_float(last),load_float(last+4),memory.load<std::uint32_t>(last+16)));
        ++cases;
    }
    return cases;
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
