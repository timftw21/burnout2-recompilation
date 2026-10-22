#pragma once
// Intel instruction helpers retain the Win64 register contract. ARM helpers
// are ordinary ahead-of-time C++ functions using the platform's native ABI.
#if defined(_WIN32) || defined(__aarch64__)
#define B2_ASM
#else
#define B2_ASM __attribute__((ms_abi))
#endif
