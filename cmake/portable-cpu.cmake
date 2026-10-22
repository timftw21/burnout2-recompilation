# Berkeley SoftFloat supplies IEEE arithmetic, including the 80-bit Xbox format.
# The upstream native makefile is the authoritative list of C translation units.
FetchContent_Declare(softfloat
    GIT_REPOSITORY https://github.com/ucb-bar/berkeley-softfloat-3.git
    GIT_TAG 32eb3295a3c814c1551f94ebc29eed1dec3cb3b1)
FetchContent_MakeAvailable(softfloat)
file(READ "${softfloat_SOURCE_DIR}/build/Linux-x86_64-GCC/Makefile" softfloat_make)
string(REGEX MATCHALL "[A-Za-z_][A-Za-z_0-9]*\\$\\(OBJ\\)" softfloat_objects "${softfloat_make}")
list(REMOVE_DUPLICATES softfloat_objects)
set(softfloat_sources "")
foreach(object IN LISTS softfloat_objects)
    string(REPLACE "$(OBJ)" ".c" source "${object}")
    if(EXISTS "${softfloat_SOURCE_DIR}/source/8086-SSE/${source}")
        list(APPEND softfloat_sources "${softfloat_SOURCE_DIR}/source/8086-SSE/${source}")
    elseif(EXISTS "${softfloat_SOURCE_DIR}/source/${source}")
        list(APPEND softfloat_sources "${softfloat_SOURCE_DIR}/source/${source}")
    endif()
endforeach()
# x87 exposes whether rounding incremented the significand (status bit C1).
# SoftFloat has the required decision internally, but does not publish it.
set(softfloat_rounding "${softfloat_SOURCE_DIR}/source/s_roundPackToExtF80.c")
file(READ "${softfloat_rounding}" rounding_source)
if(NOT rounding_source MATCHES "uint_fast8_t roundingMode;" OR NOT rounding_source MATCHES " packReturn:")
    message(FATAL_ERROR "Pinned SoftFloat rounding source changed.")
endif()
string(REPLACE "    uint_fast8_t roundingMode;" "    extern __thread bool b2_softfloat_round_up;\n    const int_fast32_t b2_input_exp = exp;\n    const uint64_t b2_input_sig = sig;\n    uint_fast8_t roundingMode;" rounding_source "${rounding_source}")
string(REPLACE " packReturn:\n" " packReturn:\n    if ((softfloat_exceptionFlags & softfloat_flag_inexact) && sig) {\n        const int shift = softfloat_countLeadingZeros64(sig);\n        const int_fast32_t result_exp = (exp ? exp : 1) - shift;\n        b2_softfloat_round_up = result_exp > b2_input_exp ||\n            (result_exp == b2_input_exp && (uint64_t)(sig << shift) > b2_input_sig);\n    }\n" rounding_source "${rounding_source}")
file(CONFIGURE OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/generated/softfloat-rounding.c" CONTENT "${rounding_source}" @ONLY)
list(REMOVE_ITEM softfloat_sources "${softfloat_rounding}")
list(APPEND softfloat_sources "${CMAKE_CURRENT_BINARY_DIR}/generated/softfloat-rounding.c")
add_library(b2-softfloat STATIC ${softfloat_sources})
target_include_directories(b2-softfloat SYSTEM PUBLIC "${softfloat_SOURCE_DIR}/source/include"
    PRIVATE "${softfloat_SOURCE_DIR}/source/8086-SSE" "${softfloat_SOURCE_DIR}/build/Linux-x86_64-GCC")
target_compile_definitions(b2-softfloat PUBLIC SOFTFLOAT_FAST_INT64 LITTLEENDIAN=1 THREAD_LOCAL=__thread
    PRIVATE SOFTFLOAT_ROUND_ODD INLINE_LEVEL=5 SOFTFLOAT_FAST_DIV32TO16 SOFTFLOAT_FAST_DIV64TO32)
target_compile_options(b2-softfloat PRIVATE -O2)
pkg_check_modules(MPFR REQUIRED IMPORTED_TARGET mpfr>=4.2)
pkg_check_modules(GMP REQUIRED IMPORTED_TARGET gmp)
add_library(b2-portable-cpu STATIC "${CMAKE_CURRENT_LIST_DIR}/../src/portable_floating.cpp")
target_compile_definitions(b2-portable-cpu PUBLIC B2_PORTABLE_CPU)
target_link_libraries(b2-portable-cpu PUBLIC b2-softfloat PkgConfig::MPFR PkgConfig::GMP)
