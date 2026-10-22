#pragma once
#include "floating.h"
#include <algorithm>
#include <bit>
#include <limits>
extern "C" __thread bool b2_softfloat_round_up;

namespace b2 {
static_assert(offsetof(extFloat80_t,signif)==0 && offsetof(extFloat80_t,signExp)==8);
FloatingState& portable_floating();
template<unsigned Operation> extFloat80_t x87_transcend(extFloat80_t,extFloat80_t);
inline std::uint16_t fp_word(const FloatingState& s,unsigned offset) {std::uint16_t v;std::memcpy(&v,s.image.data()+offset,2);return v;}
inline void fp_word(FloatingState& s,unsigned offset,std::uint16_t value) {std::memcpy(s.image.data()+offset,&value,2);}
inline unsigned fp_top(const FloatingState& s) {return (fp_word(s,4)>>11)&7;}
inline void fp_top(FloatingState& s,unsigned top) {fp_word(s,4,(fp_word(s,4)&~0x3800U)|((top&7)<<11));}
inline void fp_tag(FloatingState& s,unsigned i,unsigned tag) {const auto shift=((fp_top(s)+i)&7)*2;fp_word(s,8,(fp_word(s,8)&~(3U<<shift))|(tag<<shift));}
inline unsigned fp_tag(const FloatingState& s,unsigned i) {return (fp_word(s,8)>>(((fp_top(s)+i)&7)*2))&3;}
inline constexpr extFloat80_t fp_indefinite{0xC000000000000000ULL,0xFFFF};
inline void fp_reset(FloatingState& s) {std::fill(s.image.begin(),s.image.end(),std::byte{});fp_word(s,0,0x037F);fp_word(s,8,0xFFFF);}
inline extFloat80_t fp_get(FloatingState& s,unsigned i) {
    if(fp_tag(s,i)==3) {fp_word(s,4,(fp_word(s,4)&~0x200U)|0x41U);return fp_indefinite;}
    extFloat80_t v{};std::memcpy(&v.signif,s.image.data()+28+i*10,8);std::memcpy(&v.signExp,s.image.data()+36+i*10,2);return v;
}
inline void fp_set(FloatingState& s,unsigned i,extFloat80_t v) {
    std::memcpy(s.image.data()+28+i*10,&v.signif,8);std::memcpy(s.image.data()+36+i*10,&v.signExp,2);
    const auto exponent=v.signExp&0x7FFF;
    fp_tag(s,i,!exponent&&!v.signif?1:(!exponent || exponent==0x7FFF || !(v.signif>>63))?2:0);
}
inline void fp_pop(FloatingState& s) {
    std::array<std::byte,10> popped;std::memcpy(popped.data(),s.image.data()+28,10);
    fp_tag(s,0,3);std::memmove(s.image.data()+28,s.image.data()+38,70);
    std::memcpy(s.image.data()+98,popped.data(),10);fp_top(s,fp_top(s)+1);
}
inline void fp_push(FloatingState& s,extFloat80_t v) {
    const bool overflow=fp_tag(s,7)!=3;
    std::memmove(s.image.data()+38,s.image.data()+28,70);fp_top(s,fp_top(s)+7);
    if(overflow) {fp_word(s,4,fp_word(s,4)|0x241);v=fp_indefinite;}
    fp_set(s,0,v);
}
template<class T> inline T fp_load(const void* p) {T value;std::memcpy(&value,p,sizeof(T));return value;}
template<class T> inline void fp_store(void* p,T value) {std::memcpy(p,&value,sizeof(T));}
inline void fp_begin(FloatingState& s) {
    constexpr unsigned rounding[]={0,2,3,1};const auto control=fp_word(s,0);
    softfloat_roundingMode=rounding[(control>>10)&3];softfloat_exceptionFlags=0;b2_softfloat_round_up=false;
    extF80_roundingPrecision=((control>>8)&3)==0?32:((control>>8)&3)==2?64:80;
    fp_word(s,4,fp_word(s,4)&~0x200U);
}
inline void fp_end(FloatingState& s) {
    const unsigned flags=softfloat_exceptionFlags;
    const unsigned mapped=((flags&16)>>4)|((flags&8)>>1)|((flags&4)<<1)|((flags&2)<<3)|((flags&1)<<5);
    fp_word(s,4,fp_word(s,4)|mapped|(b2_softfloat_round_up?0x200U:0U));
    if((fp_word(s,4)&63U)&~fp_word(s,0)) {fp_word(s,4,fp_word(s,4)|0x8080U);throw std::runtime_error("Unmasked Xbox x87 exception");}
}
inline void fp_rounded(FloatingState& s,extFloat80_t before,extFloat80_t after) {
    if(!(softfloat_exceptionFlags&softfloat_flag_inexact) || (softfloat_exceptionFlags&softfloat_flag_invalid)) return;
    before.signExp&=0x7FFF;after.signExp&=0x7FFF;
    if(after.signExp>before.signExp || (after.signExp==before.signExp && after.signif>before.signif)) fp_word(s,4,fp_word(s,4)|0x200U);
}
template<bool Quiet> inline std::uint32_t fp_compare(FloatingState& s,extFloat80_t a,extFloat80_t b) {
    const bool equal=Quiet?extF80_eq(a,b):extF80_eq_signaling(a,b);
    const bool less=extF80_lt_quiet(a,b),greater=extF80_lt_quiet(b,a);
    const auto flags=equal?0x40U:less?1U:greater?0U:0x45U;
    fp_word(s,4,(fp_word(s,4)&~0x4500U)|((flags&1)<<8)|((flags&4)<<8)|((flags&64)<<8));return flags;
}

// Opcode and ModRM are compile-time constants supplied by the recompiler.
// Each instantiation is a direct native function; no guest bytes are decoded
// or dispatched at runtime.
template<unsigned Opcode,unsigned Modrm> inline std::uint32_t portable_x87(void* memory=nullptr) {
    auto& s=portable_floating();constexpr bool mem=Modrm<0xC0;
    constexpr unsigned group=(Modrm>>3)&7,index=Modrm&7;
    if constexpr(Opcode==0x9B) {
        if((fp_word(s,4)&63U)&~fp_word(s,0)) throw std::runtime_error("Unmasked Xbox x87 exception");
        return 0;
    }
    if constexpr(mem && Opcode==0xD9 && group==5) {fp_word(s,0,fp_load<std::uint16_t>(memory));return 0;}
    if constexpr(mem && Opcode==0xD9 && group==7) {fp_store(memory,fp_word(s,0));return 0;}
    if constexpr((mem && Opcode==0xDD && group==7) || (!mem && Opcode==0xDF && Modrm==0xE0)) {
        const auto value=fp_word(s,4);if constexpr(mem) fp_store(memory,value);return value;
    }
    if constexpr(mem && ((Opcode==0xD9 && (group==4 || group==6)) || (Opcode==0xDD && (group==4 || group==6)))) {
        constexpr auto size=Opcode==0xD9?28:108;
        if constexpr(group==4) {
            const auto previous=fp_top(s);std::memcpy(s.image.data(),memory,size);
            if constexpr(Opcode==0xD9) {
                const auto difference=(fp_top(s)+8-previous)&7;std::array<std::byte,80> registers;
                std::memcpy(registers.data(),s.image.data()+28,80);
                for(unsigned i=0;i<8;++i) std::memcpy(s.image.data()+28+i*10,registers.data()+((i+difference)&7)*10,10);
            }
        }
        else {std::memcpy(memory,s.image.data(),size);if constexpr(Opcode==0xDD) fp_reset(s);else fp_word(s,0,fp_word(s,0)|63);}
        return 0;
    }
    if constexpr(!mem && Opcode==0xDB && Modrm==0xE2) {fp_word(s,4,fp_word(s,4)&~0x80FFU);return 0;}
    if constexpr(!mem && Opcode==0xDB && Modrm==0xE3) {fp_reset(s);return 0;}
    if constexpr(!mem && Opcode==0xD9 && Modrm==0xD0) return 0;
    fp_begin(s);std::uint32_t flags=0;
    if constexpr(mem && ((Opcode==0xD9 && group==0) || (Opcode==0xDD && group==0) ||
                         (Opcode==0xDB && (group==0 || group==5)) || (Opcode==0xDF && (group==0 || group==5)))) {
        extFloat80_t value{};
        if constexpr(Opcode==0xD9) value=f32_to_extF80({fp_load<std::uint32_t>(memory)});
        if constexpr(Opcode==0xDD) value=f64_to_extF80({fp_load<std::uint64_t>(memory)});
        if constexpr(Opcode==0xDB && group==0) value=i32_to_extF80(fp_load<std::int32_t>(memory));
        if constexpr(Opcode==0xDF && group==0) value=i32_to_extF80(fp_load<std::int16_t>(memory));
        if constexpr(Opcode==0xDF && group==5) value=i64_to_extF80(fp_load<std::int64_t>(memory));
        if constexpr(Opcode==0xDB && group==5) {value.signif=fp_load<std::uint64_t>(memory);value.signExp=fp_load<std::uint16_t>(static_cast<std::byte*>(memory)+8);}
        fp_push(s,value);
    } else if constexpr(mem && ((Opcode==0xD9 && (group==2 || group==3)) || (Opcode==0xDD && (group==2 || group==3)) ||
                                (Opcode==0xDB && (group==1 || group==2 || group==3 || group==7)) ||
                                (Opcode==0xDF && (group==1 || group==2 || group==3 || group==7)) || (Opcode==0xDD && group==1))) {
        const auto value=fp_get(s,0);
        if constexpr(Opcode==0xD9) {const auto result=extF80_to_f32(value);fp_store(memory,result.v);fp_rounded(s,value,f32_to_extF80(result));}
        else if constexpr(Opcode==0xDD && group!=1) {const auto result=extF80_to_f64(value);fp_store(memory,result.v);fp_rounded(s,value,f64_to_extF80(result));}
        else if constexpr(Opcode==0xDB && group==7) {fp_store(memory,value.signif);fp_store(static_cast<std::byte*>(memory)+8,value.signExp);}
        else {
            const auto rounding=group==1?softfloat_round_minMag:softfloat_roundingMode;
            if constexpr((Opcode==0xDF && group==7) || Opcode==0xDD) {
                const auto result=std::int64_t(extF80_to_i64(value,rounding,true));fp_store(memory,result);fp_rounded(s,value,i64_to_extF80(result));
            }
            else if constexpr(Opcode==0xDF) {
                const auto integer=extF80_to_i32(value,rounding,true);
                if(integer<INT16_MIN || integer>INT16_MAX) {softfloat_raiseFlags(softfloat_flag_invalid);fp_store(memory,std::int16_t(INT16_MIN));}
                else {fp_store(memory,static_cast<std::int16_t>(integer));fp_rounded(s,value,i32_to_extF80(integer));}
            } else {const auto result=std::int32_t(extF80_to_i32(value,rounding,true));fp_store(memory,result);fp_rounded(s,value,i32_to_extF80(result));}
        }
        if constexpr(group==1 || group==3 || group==7) fp_pop(s);
    } else if constexpr((mem && (Opcode==0xD8 || Opcode==0xDC || Opcode==0xDA || Opcode==0xDE)) ||
                        (!mem && (Opcode==0xD8 || Opcode==0xDC || Opcode==0xDE))) {
        auto a=fp_get(s,0);extFloat80_t b{};
        if constexpr(mem) {
            if constexpr(Opcode==0xD8) b=f32_to_extF80({fp_load<std::uint32_t>(memory)});
            if constexpr(Opcode==0xDC) b=f64_to_extF80({fp_load<std::uint64_t>(memory)});
            if constexpr(Opcode==0xDA) b=i32_to_extF80(fp_load<std::int32_t>(memory));
            if constexpr(Opcode==0xDE) b=i32_to_extF80(fp_load<std::int16_t>(memory));
        } else b=fp_get(s,index);
        if constexpr(group==2 || group==3) {
            fp_compare<false>(s,a,b);if constexpr(group==3) fp_pop(s);
            if constexpr(Opcode==0xDE && !mem && Modrm==0xD9) fp_pop(s);
        } else {
            constexpr bool destination_i=!mem && (Opcode==0xDC || Opcode==0xDE);
            if constexpr(destination_i) std::swap(a,b);
            extFloat80_t value{};
            if constexpr(group==0) value=extF80_add(a,b);
            if constexpr(group==1) value=extF80_mul(a,b);
            if constexpr(group==4) value=destination_i?extF80_sub(b,a):extF80_sub(a,b);
            if constexpr(group==5) value=destination_i?extF80_sub(a,b):extF80_sub(b,a);
            if constexpr(group==6) value=destination_i?extF80_div(b,a):extF80_div(a,b);
            if constexpr(group==7) value=destination_i?extF80_div(a,b):extF80_div(b,a);
            fp_set(s,destination_i?index:0,value);if constexpr(!mem && Opcode==0xDE) fp_pop(s);
        }
    } else if constexpr(!mem && Opcode==0xD9 && group==0) fp_push(s,fp_get(s,index));
    else if constexpr(!mem && Opcode==0xD9 && group==1) {const auto a=fp_get(s,0),b=fp_get(s,index);fp_set(s,0,b);fp_set(s,index,a);}
    else if constexpr(!mem && Opcode==0xDD && group==0) fp_tag(s,index,3);
    else if constexpr(!mem && Opcode==0xDD && (group==2 || group==3)) {fp_set(s,index,fp_get(s,0));if constexpr(group==3) fp_pop(s);}
    else if constexpr(!mem && ((Opcode==0xDD && (group==4 || group==5)) || (Opcode==0xDA && Modrm==0xE9) ||
                              ((Opcode==0xDB || Opcode==0xDF) && (group==5 || group==6)))) {
        flags=fp_compare<(Opcode==0xDD || Opcode==0xDA || group==5)>(s,fp_get(s,0),fp_get(s,Opcode==0xDA?1:index));
        if constexpr((Opcode==0xDD && group==5) || Opcode==0xDF || Opcode==0xDA) fp_pop(s);
        if constexpr(Opcode==0xDA) fp_pop(s);
    } else if constexpr(!mem && Opcode==0xD9 && (Modrm==0xE0 || Modrm==0xE1)) {
        auto a=fp_get(s,0);if constexpr(Modrm==0xE0) a.signExp^=0x8000;else a.signExp&=0x7FFF;fp_set(s,0,a);
    } else if constexpr(!mem && Opcode==0xD9 && Modrm==0xE4) fp_compare<false>(s,fp_get(s,0),extFloat80_t{});
    else if constexpr(!mem && Opcode==0xD9 && Modrm>=0xE8 && Modrm<=0xEE) {
        constexpr extFloat80_t constants[]={{0x8000000000000000ULL,0x3FFF},{0xD49A784BCD1B8AFEULL,0x4000},
            {0xB8AA3B295C17F0BCULL,0x3FFF},{0xC90FDAA22168C235ULL,0x4000},{0x9A209A84FBCFF799ULL,0x3FFD},
            {0xB17217F7D1CF79ACULL,0x3FFE},{0,0}};
        auto value=constants[Modrm-0xE8];const auto rounding=(fp_word(s,0)>>10)&3;
        if constexpr(Modrm>=0xEA && Modrm<=0xED) {if(rounding==1 || rounding==3) --value.signif;}
        if constexpr(Modrm==0xE9) {if(rounding==2) ++value.signif;}
        fp_push(s,value);
    } else if constexpr(!mem && Opcode==0xD9 && Modrm==0xFA) fp_set(s,0,extF80_sqrt(fp_get(s,0)));
    else if constexpr(!mem && Opcode==0xD9 && Modrm==0xFC) {
        const auto before=fp_get(s,0),after=extF80_roundToInt(before,softfloat_roundingMode,true);fp_rounded(s,before,after);fp_set(s,0,after);
    }
    else if constexpr(!mem && Opcode==0xD9 && (Modrm==0xF6 || Modrm==0xF7)) {
        constexpr unsigned shift=Modrm==0xF6?7:1;std::array<std::byte,80> registers;
        std::memcpy(registers.data(),s.image.data()+28,80);
        for(unsigned i=0;i<8;++i) std::memcpy(s.image.data()+28+i*10,registers.data()+((i+shift)&7)*10,10);
        fp_top(s,fp_top(s)+shift);
    }
    else if constexpr(!mem && Opcode==0xD9 && (Modrm==0xF0 || Modrm==0xF1 || Modrm==0xF2 || Modrm==0xF3 ||
                       Modrm==0xF9 || Modrm==0xFB || Modrm==0xFD || Modrm==0xFE || Modrm==0xFF)) {
        const auto a=fp_get(s,0);const auto b=(Modrm==0xF1 || Modrm==0xF3 || Modrm==0xF9 || Modrm==0xFD)?fp_get(s,1):extFloat80_t{};
        if constexpr(Modrm==0xF2 || Modrm==0xFB || Modrm==0xFE || Modrm==0xFF)
            if((a.signExp&0x7FFF)>=0x403E && (a.signExp&0x7FFF)!=0x7FFF) {fp_word(s,4,fp_word(s,4)|0x400U);return 0;}
        const auto result=x87_transcend<Modrm>(a,b);
        if constexpr(Modrm==0xF1 || Modrm==0xF3 || Modrm==0xF9) {fp_set(s,1,result);fp_pop(s);}
        else {fp_set(s,0,result);if constexpr(Modrm==0xF2) fp_push(s,i32_to_extF80(1));if constexpr(Modrm==0xFB) fp_push(s,x87_transcend<0xFF>(a,{}));}
        fp_word(s,4,fp_word(s,4)&~0x400U);
    } else throw std::runtime_error("Unsupported ARM x87 instruction");
    fp_end(s);return flags;
}

inline void fp_mmx_finish(FloatingState& s,bool empty=false) {
    const auto top=fp_top(s);
    if(top) {
        std::array<std::byte,80> registers;std::memcpy(registers.data(),s.image.data()+28,80);
        for(unsigned i=0;i<8;++i) std::memcpy(s.image.data()+28+i*10,registers.data()+((i+8-top)&7)*10,10);
        fp_top(s,0);
    }
    if(empty) {fp_word(s,8,0xFFFF);return;}
    const auto tags=fp_word(s,8);
    if(tags&(tags>>1)&0x5555) for(unsigned i=0;i<8;++i) {
        extFloat80_t value{fp_load<std::uint64_t>(s.image.data()+28+i*10),fp_word(s,36+i*10)};fp_set(s,i,value);
    }
}
template<unsigned Opcode,unsigned Modrm,unsigned Immediate=0> inline void portable_mmx(void* memory=nullptr) {
    auto& s=portable_floating();constexpr unsigned reg=(Modrm>>3)&7,rm=Modrm&7;
    constexpr bool mem=Modrm<0xC0;
    if constexpr(Opcode==0x77) {fp_mmx_finish(s,true);return;}
    // MMX names physical registers, whereas FSAVE stores them in stack order.
    const auto slot=[&](unsigned r){return (r+8-fp_top(s))&7;};
    const auto get=[&](unsigned r){return fp_load<std::uint64_t>(s.image.data()+28+slot(r)*10);};
    const auto put=[&](unsigned r,std::uint64_t v){fp_store(s.image.data()+28+slot(r)*10,v);fp_store(s.image.data()+36+slot(r)*10,std::uint16_t(0xFFFF));fp_tag(s,slot(r),2);};
    const auto a=get(reg);std::uint64_t b=0;
    if constexpr(Opcode!=0x6E && Opcode!=0x2D && Opcode!=0x71 && Opcode!=0x72) b=mem?fp_load<std::uint64_t>(memory):get(rm);
    if constexpr(Opcode==0x6F) put(reg,b);
    else if constexpr(Opcode==0x7F || Opcode==0xE7) {if constexpr(mem) fp_store(memory,a);else put(rm,a);}
    else if constexpr(Opcode==0x6E) put(reg,fp_load<std::uint32_t>(memory));
    else if constexpr(Opcode==0x7E) fp_store(memory,std::uint32_t(a));
    else if constexpr(Opcode==0x2D) {
        const auto* p=static_cast<const std::byte*>(memory);sse_begin();
        const auto lo=std::uint32_t(f32_to_i32(sse_input(fp_load<std::uint32_t>(p)),softfloat_roundingMode,true));
        const auto hi=std::uint32_t(f32_to_i32(sse_input(fp_load<std::uint32_t>(p+4)),softfloat_roundingMode,true));
        sse_end();put(reg,lo|(std::uint64_t(hi)<<32));
    } else if constexpr(Opcode==0x70) {
        std::uint64_t value=0;for(unsigned i=0;i<4;++i) value|=((b>>(((Immediate>>(2*i))&3)*16))&0xFFFF)<<(i*16);put(reg,value);
    } else if constexpr(Opcode==0x61 || Opcode==0x69 || Opcode==0x62 || Opcode==0x6A) {
        constexpr auto bits=(Opcode==0x61 || Opcode==0x69)?16:32;
        constexpr auto shift=(Opcode==0x69 || Opcode==0x6A)?32:0;
        std::uint64_t value=0;for(unsigned i=0;i<32/bits;++i) {const auto mask=(1ULL<<bits)-1;value|=((a>>(shift+i*bits))&mask)<<(2*i*bits);value|=((b>>(shift+i*bits))&mask)<<((2*i+1)*bits);}put(reg,value);
    } else if constexpr(Opcode==0x6B) {
        std::uint64_t value=0;for(unsigned i=0;i<4;++i) {const auto source=i<2?a:b;const auto word=std::bit_cast<std::int32_t>(std::uint32_t(source>>((i&1)*32)));
            value|=std::uint64_t(std::uint16_t(std::clamp(word,-32768,32767)))<<(i*16);}put(reg,value);
    } else if constexpr(Opcode==0xFE) {
        put(reg,std::uint64_t(std::uint32_t(a)+std::uint32_t(b))|(std::uint64_t(std::uint32_t(a>>32)+std::uint32_t(b>>32))<<32));
    } else if constexpr(Opcode==0x71 && reg==4) {
        auto value=get(rm);std::uint64_t result=0;for(unsigned i=0;i<4;++i) result|=std::uint64_t(std::uint16_t(std::bit_cast<std::int16_t>(std::uint16_t(value>>(i*16)))>>std::min(Immediate,15U)))<<(i*16);put(rm,result);
    } else if constexpr(Opcode==0x72 && (reg==2 || reg==6)) {
        const auto value=get(rm);std::uint64_t result=0;if constexpr(Immediate<32) for(unsigned i=0;i<2;++i) {
            const auto lane=std::uint32_t(value>>(i*32));result|=std::uint64_t(reg==2?lane>>Immediate:lane<<Immediate)<<(i*32);
        }put(rm,result);
    } else throw std::runtime_error("Unsupported ARM MMX instruction");
    fp_mmx_finish(s);
}
}
