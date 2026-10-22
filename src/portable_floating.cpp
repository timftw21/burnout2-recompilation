#include "portable_floating.h"
#include <atomic>
#include <cinttypes>
#include <mpfr.h>
#include <cstdlib>

namespace b2 {
namespace {
thread_local FloatingState* active=nullptr;
struct HostState {FloatingState* previous;unsigned control;};
static_assert(sizeof(HostState)<=112);
template<class T> T* atomic_pointer(void* pointer) {
#ifdef __aarch64__
    if(reinterpret_cast<std::uintptr_t>(pointer)&(alignof(T)-1))
        throw std::runtime_error("Unaligned guest atomic memory is unsupported on ARM");
#endif
    return static_cast<T*>(pointer);
}
struct MathWorkspace {alignas(std::max_align_t) std::array<std::byte,65536> bytes{};std::size_t used=0;bool active=false;};
thread_local MathWorkspace workspace;
void*(*system_allocate)(std::size_t)=nullptr;
void*(*system_resize)(void*,std::size_t,std::size_t)=nullptr;
void(*system_free)(void*,std::size_t)=nullptr;
bool temporary(const void* p) {
    const auto address=reinterpret_cast<std::uintptr_t>(p),first=reinterpret_cast<std::uintptr_t>(workspace.bytes.data());
    return address>=first && address-first<workspace.bytes.size();
}
void* math_allocate(std::size_t size) {
    if(!workspace.active) return system_allocate(size);
    size=std::max(size,std::size_t{1});
    constexpr auto alignment=alignof(std::max_align_t);
    const auto position=(workspace.used+alignment-1)&~(alignment-1);
    if(size>workspace.bytes.size()-position) throw std::runtime_error("Extended math exceeds its 64 KiB workspace");
    workspace.used=position+size;return workspace.bytes.data()+position;
}
void* math_resize(void* p,std::size_t old_size,std::size_t size) {
    if(!p) return math_allocate(size);
    if(!temporary(p)) {
        if(workspace.active) throw std::runtime_error("Extended math tried to resize persistent storage during execution");
        return system_resize(p,old_size,size);
    }
    if(size<=old_size) return p;
    auto* result=math_allocate(size);std::memcpy(result,p,old_size);return result;
}
void math_free(void* p,std::size_t size) {if(!temporary(p)) system_free(p,size);}
struct MathCall {
    MathCall() {if(workspace.active) throw std::runtime_error("Nested extended math workspace");workspace.used=0;workspace.active=true;}
    ~MathCall() {
        // MPFR constants can retain their buffers. Release that thread-local
        // cache before recycling the temporary arena, not the persistent state.
        mpfr_free_cache2(MPFR_FREE_LOCAL_CACHE);workspace.active=false;
    }
};
struct Transcend {
    mpfr_t a,b,result,scratch;
    Transcend() {
        static const bool ready=[] {
            if(!mpfr_buildopt_tls_p()) throw std::runtime_error("Extended math requires a thread-safe MPFR build");
            mp_get_memory_functions(&system_allocate,&system_resize,&system_free);
            mp_set_memory_functions(math_allocate,math_resize,math_free);return true;
        }();
        (void)ready;mpfr_inits2(192,a,b,scratch,(mpfr_ptr)nullptr);mpfr_init2(result,64);
    }
    ~Transcend() {mpfr_clears(a,b,result,scratch,(mpfr_ptr)nullptr);}
};
thread_local Transcend math;
void set(mpfr_ptr output,extFloat80_t value) {
    const auto exponent=value.signExp&0x7FFF;
    if(exponent==0x7FFF) {
        if(value.signif==0x8000000000000000ULL) mpfr_set_inf(output,value.signExp&0x8000?-1:1);
        else mpfr_set_nan(output);
        return;
    }
    mpfr_set_uj(output,value.signif,MPFR_RNDN);mpfr_mul_2si(output,output,(exponent?exponent:1)-16383-63,MPFR_RNDN);
    if(value.signExp&0x8000) mpfr_neg(output,output,MPFR_RNDN);
}
extFloat80_t get(mpfr_srcptr value) {
    if(mpfr_nan_p(value)) return fp_indefinite;
    const auto sign=mpfr_signbit(value)?0x8000U:0U;
    if(mpfr_inf_p(value)) return {0x8000000000000000ULL,std::uint16_t(sign|0x7FFF)};
    if(mpfr_zero_p(value)) return {0,std::uint16_t(sign)};
    const auto exponent=mpfr_get_exp(value)-1;
    if(exponent>16383) {softfloat_raiseFlags(softfloat_flag_overflow|softfloat_flag_inexact);return {0x8000000000000000ULL,std::uint16_t(sign|0x7FFF)};}
    mpfr_abs(math.scratch,value,MPFR_RNDN);mpfr_mul_2si(math.scratch,math.scratch,63-std::max<mpfr_exp_t>(exponent,-16382),MPFR_RNDN);
    return {static_cast<std::uint64_t>(mpfr_get_uj(math.scratch,MPFR_RNDN)),std::uint16_t(sign|(exponent>=-16382?exponent+16383:0))};
}
}
FloatingState& portable_floating() {if(!active) throw std::runtime_error("ARM floating operation outside a guest scope");return *active;}
template<unsigned Operation> extFloat80_t x87_transcend(extFloat80_t a,extFloat80_t b) {
    // Persistent values are prepared at guest entry. GMP's supported allocator
    // hooks keep all per-operation temporary storage in this fixed workspace.
    (void)&math;MathCall call;
    set(math.a,a);set(math.b,b);mpfr_clear_flags();
    constexpr mpfr_rnd_t modes[]={MPFR_RNDN,MPFR_RNDZ,MPFR_RNDD,MPFR_RNDU};
    const auto rounding=modes[std::min<unsigned>(softfloat_roundingMode,3)];int inexact=0;
    if constexpr(Operation==0xF0) {mpfr_const_log2(math.scratch,MPFR_RNDN);mpfr_mul(math.scratch,math.scratch,math.a,MPFR_RNDN);inexact=mpfr_expm1(math.result,math.scratch,rounding);}
    else if constexpr(Operation==0xF1) {mpfr_log2(math.scratch,math.a,MPFR_RNDN);inexact=mpfr_mul(math.result,math.scratch,math.b,rounding);}
    else if constexpr(Operation==0xF2) inexact=mpfr_tan(math.result,math.a,rounding);
    else if constexpr(Operation==0xF3) inexact=mpfr_atan2(math.result,math.b,math.a,rounding);
    else if constexpr(Operation==0xF9) {mpfr_log2p1(math.scratch,math.a,MPFR_RNDN);inexact=mpfr_mul(math.result,math.scratch,math.b,rounding);}
    else if constexpr(Operation==0xFB || Operation==0xFE) inexact=mpfr_sin(math.result,math.a,rounding);
    else if constexpr(Operation==0xFF) inexact=mpfr_cos(math.result,math.a,rounding);
    else if constexpr(Operation==0xFD) {mpfr_trunc(math.b,math.b);mpfr_clear_flags();inexact=mpfr_mul_2si(math.result,math.a,mpfr_get_si(math.b,MPFR_RNDZ),rounding);}
    else static_assert(Operation==0,"Unsupported x87 transcendental");
    if(inexact || mpfr_inexflag_p()) softfloat_raiseFlags(softfloat_flag_inexact);
    b2_softfloat_round_up=inexact && (mpfr_signbit(math.result)?inexact<0:inexact>0);
    if(mpfr_nanflag_p()) softfloat_raiseFlags(softfloat_flag_invalid);
    if(mpfr_divby0_p()) softfloat_raiseFlags(softfloat_flag_infinite);
    if(mpfr_overflow_p()) softfloat_raiseFlags(softfloat_flag_overflow|softfloat_flag_inexact);
    if(mpfr_underflow_p()) softfloat_raiseFlags(softfloat_flag_underflow|softfloat_flag_inexact);
    return get(math.result);
}
#define B2_TRANSCEND(Operation) template extFloat80_t x87_transcend<Operation>(extFloat80_t,extFloat80_t);
B2_TRANSCEND(0xF0) B2_TRANSCEND(0xF1) B2_TRANSCEND(0xF2) B2_TRANSCEND(0xF3) B2_TRANSCEND(0xF9)
B2_TRANSCEND(0xFB) B2_TRANSCEND(0xFD) B2_TRANSCEND(0xFE) B2_TRANSCEND(0xFF)
#undef B2_TRANSCEND
}
extern "C" {
__thread bool b2_softfloat_round_up=false;
void B2_ASM b2_fp_enter(void* guest,void* host) {
    using namespace b2;
    // Initialize the per-thread math scratch during entry, before translated
    // instructions run. Basic x87/SSE arithmetic itself never allocates.
    (void)&math;
    const HostState saved{active,_mm_getcsr()};std::memcpy(host,&saved,sizeof(saved));
    active=static_cast<FloatingState*>(guest);_mm_setcsr(active->mxcsr);
}
void B2_ASM b2_fp_leave(void* guest,const void* host) {
    using namespace b2;static_cast<FloatingState*>(guest)->mxcsr=_mm_getcsr();
    HostState saved;std::memcpy(&saved,host,sizeof(saved));active=saved.previous;_mm_setcsr(saved.control);
}
void B2_ASM b2_fp_snapshot(void* output) {
    b2::FloatingState state;b2::fp_reset(state);std::memcpy(output,state.image.data(),108);
}
#define B2_ATOMIC(Bits,Type) \
    std::uint32_t B2_ASM b2_exchange##Bits(void* p,std::uint32_t v) {return __atomic_exchange_n(b2::atomic_pointer<Type>(p),Type(v),__ATOMIC_SEQ_CST);} \
    std::uint32_t B2_ASM b2_fetch_add##Bits(void* p,std::uint32_t v) {return __atomic_fetch_add(b2::atomic_pointer<Type>(p),Type(v),__ATOMIC_SEQ_CST);} \
    std::uint32_t B2_ASM b2_compare_exchange##Bits(void* p,std::uint32_t expected,std::uint32_t v) {auto old=Type(expected);__atomic_compare_exchange_n(b2::atomic_pointer<Type>(p),&old,Type(v),false,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST);return old;}
B2_ATOMIC(8,std::uint8_t)
B2_ATOMIC(16,std::uint16_t)
B2_ATOMIC(32,std::uint32_t)
#undef B2_ATOMIC
}
