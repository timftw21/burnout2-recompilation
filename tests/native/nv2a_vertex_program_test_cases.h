#pragma once

#include "native_test_harness.h"
#include "nv2a_vertex_program.h"

#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string_view>

namespace b2r::test {

inline bool nearly_equal(float lhs, float rhs, float tolerance = 0.00001f) {
    return std::abs(lhs - rhs) <= tolerance;
}

inline uint32_t float_bits(float value) {
    uint32_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

inline void expect_vector(
    TestContext& context,
    const nv2a_vsh::Vec4& actual,
    const nv2a_vsh::Vec4& expected,
    std::string_view message
) {
    for (std::size_t lane = 0; lane < actual.size(); ++lane) {
        if (!nearly_equal(actual[lane], expected[lane])) {
            std::cerr << "FAILED: " << message << " lane " << lane
                      << " expected " << expected[lane]
                      << " but received " << actual[lane] << '\n';
            ++context.failure_count;
        }
    }
}

void test_field_and_swizzle(TestContext& context);
void test_vector_operations(TestContext& context);
void test_write_mask(TestContext& context);
void test_program_execution(TestContext& context);
void test_invalid_program(TestContext& context);

}  // namespace b2r::test
