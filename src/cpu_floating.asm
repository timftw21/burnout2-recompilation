option casemap:none
.code

; FNSAVE/FRSTOR use the 108-byte environment in 64-bit mode without a 66 prefix.
; Do not restore XMM registers: the surrounding C++ owns the host ABI registers.
b2_fp_enter PROC
    fnsave [rdx]
    stmxcsr dword ptr [rdx+108]
    frstor [rcx]
    ldmxcsr dword ptr [rcx+108]
    ret
b2_fp_enter ENDP

b2_fp_leave PROC
    fnsave [rcx]
    stmxcsr dword ptr [rcx+108]
    frstor [rdx]
    ldmxcsr dword ptr [rdx+108]
    ret
b2_fp_leave ENDP

; Diagnostic snapshot preserves the resident state, including all 80-bit values.
b2_fp_snapshot PROC
    fnsave [rcx]
    frstor [rcx]
    ret
b2_fp_snapshot ENDP

atomic_memory MACRO symbol,operation,part
symbol PROC
    mov eax,edx
    operation [rcx],part
    ret
symbol ENDP
ENDM
atomic_memory b2_exchange8,xchg,al
atomic_memory b2_exchange16,xchg,ax
atomic_memory b2_exchange32,xchg,eax

atomic_add MACRO symbol,part
symbol PROC
    mov eax,edx
    lock xadd [rcx],part
    ret
symbol ENDP
ENDM
atomic_add b2_fetch_add8,al
atomic_add b2_fetch_add16,ax
atomic_add b2_fetch_add32,eax

atomic_compare MACRO symbol,part
symbol PROC
    mov eax,edx
    lock cmpxchg [rcx],part
    ret
symbol ENDP
ENDM
atomic_compare b2_compare_exchange8,r8b
atomic_compare b2_compare_exchange16,r8w
atomic_compare b2_compare_exchange32,r8d
END
