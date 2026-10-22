#include "assembly.h"
.intel_syntax noprefix
.text



b2_function b2_fp_enter
    fnsave [rdx]
    stmxcsr dword ptr [rdx+108]
    frstor [rcx]
    ldmxcsr dword ptr [rcx+108]
    ret
b2_end b2_fp_enter

b2_function b2_fp_leave
    fnsave [rcx]
    stmxcsr dword ptr [rcx+108]
    frstor [rdx]
    ldmxcsr dword ptr [rdx+108]
    ret
b2_end b2_fp_leave


b2_function b2_fp_snapshot
    fnsave [rcx]
    frstor [rcx]
    ret
b2_end b2_fp_snapshot

.macro atomic_memory symbol,operation,part
b2_function \symbol
    mov eax,edx
    \operation [rcx],\part
    ret
b2_end \symbol
.endm
atomic_memory b2_exchange8,xchg,al
atomic_memory b2_exchange16,xchg,ax
atomic_memory b2_exchange32,xchg,eax

.macro atomic_add symbol,part
b2_function \symbol
    mov eax,edx
    lock xadd [rcx],\part
    ret
b2_end \symbol
.endm
atomic_add b2_fetch_add8,al
atomic_add b2_fetch_add16,ax
atomic_add b2_fetch_add32,eax

.macro atomic_compare symbol,part
b2_function \symbol
    mov eax,edx
    lock cmpxchg [rcx],\part
    ret
b2_end \symbol
.endm
atomic_compare b2_compare_exchange8,r8b
atomic_compare b2_compare_exchange16,r8w
atomic_compare b2_compare_exchange32,r8d
#ifndef __APPLE__
.section .note.GNU-stack,"",@progbits
#endif
