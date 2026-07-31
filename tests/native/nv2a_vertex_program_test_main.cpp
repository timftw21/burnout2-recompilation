#include "nv2a_vertex_program_test_cases.h"

#include <array>

int main(int argc, char** argv) {
    using namespace b2r::test;
    constexpr std::array<TestCase, 5> kTestCases = {{
        {"field_and_swizzle", test_field_and_swizzle},
        {"vector_operations", test_vector_operations},
        {"write_mask", test_write_mask},
        {"program_execution", test_program_execution},
        {"invalid_program", test_invalid_program},
    }};
    return run_test_cases(argc, argv, kTestCases);
}
