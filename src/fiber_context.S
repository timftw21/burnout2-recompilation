// Cooperative contexts, preserving the native ABI's nonvolatile registers and
// floating control state. Stacks are allocated and guarded by host_posix.cpp.
.text
.globl _b2_fiber_switch
#if defined(__aarch64__)
.p2align 2
_b2_fiber_switch:
    stp x19,x20,[x0,#0]
    stp x21,x22,[x0,#16]
    stp x23,x24,[x0,#32]
    stp x25,x26,[x0,#48]
    stp x27,x28,[x0,#64]
    stp x29,x30,[x0,#80]
    mov x9,sp
    str x9,[x0,#96]
    stp d8,d9,[x0,#112]
    stp d10,d11,[x0,#128]
    stp d12,d13,[x0,#144]
    stp d14,d15,[x0,#160]
    mrs x9,fpcr
    mrs x10,fpsr
    stp x9,x10,[x0,#176]
    ldp x19,x20,[x1,#0]
    ldp x21,x22,[x1,#16]
    ldp x23,x24,[x1,#32]
    ldp x25,x26,[x1,#48]
    ldp x27,x28,[x1,#64]
    ldp x29,x30,[x1,#80]
    ldr x9,[x1,#96]
    mov sp,x9
    ldp d8,d9,[x1,#112]
    ldp d10,d11,[x1,#128]
    ldp d12,d13,[x1,#144]
    ldp d14,d15,[x1,#160]
    ldp x9,x10,[x1,#176]
    msr fpcr,x9
    msr fpsr,x10
    ret
#else
.p2align 4
_b2_fiber_switch:
    movq %rbx,0(%rdi)
    movq %rbp,8(%rdi)
    movq %r12,16(%rdi)
    movq %r13,24(%rdi)
    movq %r14,32(%rdi)
    movq %r15,40(%rdi)
    movq %rsp,48(%rdi)
    fxsave64 64(%rdi)
    movq 0(%rsi),%rbx
    movq 8(%rsi),%rbp
    movq 16(%rsi),%r12
    movq 24(%rsi),%r13
    movq 32(%rsi),%r14
    movq 40(%rsi),%r15
    movq 48(%rsi),%rsp
    fxrstor64 64(%rsi)
    ret
#endif
#ifndef __APPLE__
.section .note.GNU-stack,"",@progbits
#endif
