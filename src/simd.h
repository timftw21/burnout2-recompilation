#pragma once
#ifndef B2_PORTABLE_CPU
#include <immintrin.h>
#else
#include <array>
#include <atomic>
#include <bit>
#include <cstdint>
#include <cstring>
#include <stdexcept>
// ARM's native headers also define float32_t/float64_t. Keep the C library's
// types scoped while retaining its ordinary C linkage and native ABI.
namespace b2 { extern "C" {
#include <softfloat.h>
}}

// Fixed-size values lower to native vector loads/stores. Arithmetic uses the
// Xbox's IEEE rounding and exception rules, independent of ARM's FP control.
struct alignas(16) __m128 {std::array<std::uint32_t,4> lane{};};
using __m128i=__m128;
// Clang reserves these four spellings as Intel builtins even in the portable
// validation build. Keep the generated interface but use ordinary C++ names.
#define _mm_getcsr b2_simd_getcsr
#define _mm_setcsr b2_simd_setcsr
#define _mm_sfence b2_simd_sfence
#define _mm_prefetch b2_simd_prefetch
namespace b2 {
inline thread_local unsigned simd_control=0x1F80;
inline unsigned fp_round(unsigned value) {constexpr unsigned modes[]={0,2,3,1};return modes[(value>>13)&3U];}
inline void sse_begin() {softfloat_roundingMode=fp_round(simd_control);softfloat_exceptionFlags=0;}
inline void sse_end() {
    const unsigned flags=softfloat_exceptionFlags;
    const unsigned mapped=((flags&16)>>4)|((flags&8)>>1)|((flags&4)<<1)|((flags&2)<<3)|((flags&1)<<5);
    simd_control|=mapped;
    if(mapped&~(simd_control>>7)&63U) throw std::runtime_error("Unmasked Xbox SIMD exception");
}
inline float32_t sse_input(std::uint32_t value) {
    if(!(value&0x7F800000U) && (value&0x7FFFFFU)) {
        if(simd_control&64U) value&=0x80000000U;
        else {simd_control|=2;if(!(simd_control&0x100U)) throw std::runtime_error("Unmasked Xbox SIMD denormal exception");}
    }
    return {value};
}
inline std::uint32_t sse_output(float32_t value) {
    if((simd_control&0x8000U) && !(value.v&0x7F800000U) && (value.v&0x7FFFFFU)) {
        softfloat_exceptionFlags|=softfloat_flag_underflow|softfloat_flag_inexact;value.v&=0x80000000U;
    }
    return value.v;
}
template<auto Operation,bool Scalar> inline __m128 sse_binary(__m128 a,__m128 b) {
    sse_begin();for(unsigned i=0;i<(Scalar?1:4);++i) a.lane[i]=sse_output(Operation(sse_input(a.lane[i]),sse_input(b.lane[i])));
    sse_end();return a;
}
template<bool Scalar> inline __m128 sse_sqrt(__m128 a) {
    sse_begin();for(unsigned i=0;i<(Scalar?1:4);++i) a.lane[i]=sse_output(f32_sqrt(sse_input(a.lane[i])));
    sse_end();return a;
}
template<bool Maximum,bool Scalar> inline __m128 sse_minmax(__m128 a,__m128 b) {
    sse_begin();for(unsigned i=0;i<(Scalar?1:4);++i) {
        const auto x=sse_input(a.lane[i]),y=sse_input(b.lane[i]);
        const bool take=Maximum?f32_lt(y,x):f32_lt(x,y);a.lane[i]=take?x.v:y.v;
    }
    sse_end();return a;
}
}
inline unsigned _mm_getcsr() {return b2::simd_control;}
inline void _mm_setcsr(unsigned value) {b2::simd_control=value;}
inline __m128 _mm_castsi128_ps(__m128i a) {return a;}
inline __m128i _mm_castps_si128(__m128 a) {return a;}
inline __m128i _mm_cvtsi32_si128(int a) {return {{{std::bit_cast<std::uint32_t>(a),0,0,0}}};}
inline int _mm_cvtsi128_si32(__m128i a) {return std::bit_cast<int>(a.lane[0]);}
inline __m128 _mm_set_ss(float a) {return {{{std::bit_cast<std::uint32_t>(a),0,0,0}}};}
inline __m128 _mm_set1_ps(float a) {const auto x=std::bit_cast<std::uint32_t>(a);return {{{x,x,x,x}}};}
inline __m128 _mm_loadu_ps(const float* p) {__m128 a;std::memcpy(&a,p,16);return a;}
inline __m128 _mm_load_ss(const float* p) {__m128 a;std::memcpy(&a,p,4);return a;}
inline void _mm_storeu_ps(float* p,__m128 a) {std::memcpy(p,&a,16);}
inline void _mm_stream_ps(float* p,__m128 a) {_mm_storeu_ps(p,a);}
inline void _mm_store_ss(float* p,__m128 a) {std::memcpy(p,&a,4);}
inline float _mm_cvtss_f32(__m128 a) {return std::bit_cast<float>(a.lane[0]);}
inline __m128 _mm_move_ss(__m128 a,__m128 b) {a.lane[0]=b.lane[0];return a;}
inline __m128 _mm_shuffle_ps(__m128 a,__m128 b,int n) {return {{{a.lane[n&3],a.lane[(n>>2)&3],b.lane[(n>>4)&3],b.lane[(n>>6)&3]}}};}
inline __m128 _mm_unpacklo_ps(__m128 a,__m128 b) {return {{{a.lane[0],b.lane[0],a.lane[1],b.lane[1]}}};}
inline __m128 _mm_unpackhi_ps(__m128 a,__m128 b) {return {{{a.lane[2],b.lane[2],a.lane[3],b.lane[3]}}};}
inline __m128 _mm_movehl_ps(__m128 a,__m128 b) {return {{{b.lane[2],b.lane[3],a.lane[2],a.lane[3]}}};}
inline __m128 _mm_movelh_ps(__m128 a,__m128 b) {return {{{a.lane[0],a.lane[1],b.lane[0],b.lane[1]}}};}
#define B2_SSE_BINARY(Name,Operation) \
    inline __m128 _mm_##Name##_ps(__m128 a,__m128 b) {return b2::sse_binary<Operation,false>(a,b);} \
    inline __m128 _mm_##Name##_ss(__m128 a,__m128 b) {return b2::sse_binary<Operation,true>(a,b);}
B2_SSE_BINARY(add,b2::f32_add)
B2_SSE_BINARY(sub,b2::f32_sub)
B2_SSE_BINARY(mul,b2::f32_mul)
B2_SSE_BINARY(div,b2::f32_div)
#undef B2_SSE_BINARY
inline __m128 _mm_sqrt_ps(__m128 a) {return b2::sse_sqrt<false>(a);}
inline __m128 _mm_sqrt_ss(__m128 a) {return b2::sse_sqrt<true>(a);}
inline __m128 _mm_min_ps(__m128 a,__m128 b) {return b2::sse_minmax<false,false>(a,b);}
inline __m128 _mm_max_ps(__m128 a,__m128 b) {return b2::sse_minmax<true,false>(a,b);}
inline __m128 _mm_min_ss(__m128 a,__m128 b) {return b2::sse_minmax<false,true>(a,b);}
inline __m128 _mm_max_ss(__m128 a,__m128 b) {return b2::sse_minmax<true,true>(a,b);}
#define B2_SSE_BITS(Name,Expression) \
    inline __m128 _mm_##Name##_ps(__m128 a,__m128 b) {for(unsigned i=0;i<4;++i) a.lane[i]=(Expression);return a;}
B2_SSE_BITS(xor,a.lane[i]^b.lane[i])
B2_SSE_BITS(and,a.lane[i]&b.lane[i])
B2_SSE_BITS(or,a.lane[i]|b.lane[i])
B2_SSE_BITS(andnot,~a.lane[i]&b.lane[i])
#undef B2_SSE_BITS
inline int _mm_cvt_ss2si(__m128 a) {b2::sse_begin();const auto r=b2::f32_to_i32(b2::sse_input(a.lane[0]),b2::softfloat_roundingMode,true);b2::sse_end();return static_cast<int>(r);}
inline int _mm_cvtt_ss2si(__m128 a) {b2::sse_begin();const auto r=b2::f32_to_i32_r_minMag(b2::sse_input(a.lane[0]),true);b2::sse_end();return static_cast<int>(r);}
inline __m128 _mm_cvtsi32_ss(__m128 a,int b) {b2::sse_begin();a.lane[0]=b2::i32_to_f32(b).v;b2::sse_end();return a;}
inline void _mm_sfence() {std::atomic_thread_fence(std::memory_order_seq_cst);}
inline constexpr int _MM_HINT_NTA=0;
inline void _mm_prefetch(const char* p,int) {__builtin_prefetch(p,0,0);}
#endif
