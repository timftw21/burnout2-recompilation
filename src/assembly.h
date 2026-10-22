// Shared Intel helper directives for ELF and Mach-O.
.macro b2_function symbol
#ifdef __APPLE__
.globl _\symbol
_\symbol:
#else
.globl \symbol
.type \symbol,@function
\symbol:
#endif
.endm
.macro b2_end symbol
#ifndef __APPLE__
.size \symbol, .-\symbol
#endif
.endm
