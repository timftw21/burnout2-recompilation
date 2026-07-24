#include "nv2a_vertex_program.h"

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string_view>

namespace {

struct TestContext {
    int failure_count = 0;

    void expect(bool condition, std::string_view message) {
        if (!condition) {
            std::cerr << "FAILED: " << message << '\n';
            ++failure_count;
        }
    }
};

bool nearly_equal(float lhs, float rhs, float tolerance = 0.00001f) {
    return std::abs(lhs - rhs) <= tolerance;
}

uint32_t float_bits(float value) {
    uint32_t bits = 0;
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

void expect_vector(
    TestContext& context,
    const nv2a_vsh::Vec4& actual,
    const nv2a_vsh::Vec4& expected,
    std::string_view message
) {
    for (size_t lane = 0; lane < actual.size(); ++lane) {
        if (!nearly_equal(actual[lane], expected[lane])) {
            std::cerr << "FAILED: " << message << " lane " << lane
                      << " expected " << expected[lane]
                      << " but received " << actual[lane] << '\n';
            ++context.failure_count;
        }
    }
}

void test_field_and_swizzle(TestContext& context) {
    context.expect(nv2a_vsh::field(0x000000D8u, 3u, 4u) == 0xBu, "field extraction");

    const nv2a_vsh::Vec4 source = {1.0f, 2.0f, 3.0f, 4.0f};
    expect_vector(
        context,
        nv2a_vsh::swizzle(source, 0x1Bu, false),
        source,
        "identity swizzle"
    );
    expect_vector(
        context,
        nv2a_vsh::swizzle(source, 0xE4u, true),
        {-4.0f, -3.0f, -2.0f, -1.0f},
        "negated reverse swizzle"
    );
}

void test_vector_operations(TestContext& context) {
    const nv2a_vsh::Vec4 a = {1.0f, 2.0f, 3.0f, 4.0f};
    const nv2a_vsh::Vec4 b = {5.0f, 6.0f, 7.0f, 8.0f};
    const nv2a_vsh::Vec4 c = {0.5f, 1.0f, 1.5f, 2.0f};
    expect_vector(
        context,
        nv2a_vsh::execute_operation(4u, a, b, c),
        {5.5f, 13.0f, 22.5f, 34.0f},
        "multiply-add operation"
    );
    expect_vector(
        context,
        nv2a_vsh::execute_operation(7u, a, b, {}),
        {70.0f, 70.0f, 70.0f, 70.0f},
        "four-component dot product"
    );
    expect_vector(
        context,
        nv2a_vsh::execute_operation(12u, a, c, {}),
        {1.0f, 1.0f, 1.0f, 1.0f},
        "greater-than-or-equal comparison"
    );
}

void test_write_mask(TestContext& context) {
    nv2a_vsh::Vec4 destination = {10.0f, 20.0f, 30.0f, 40.0f};
    nv2a_vsh::write_mask(destination, {1.0f, 2.0f, 3.0f, 4.0f}, 0xAu);
    expect_vector(context, destination, {1.0f, 20.0f, 3.0f, 40.0f}, "write mask");
}

void test_program_execution(TestContext& context) {
    std::array<std::array<uint32_t, 4>, 136> program{};
    std::array<std::array<uint32_t, 4>, 192> constants{};
    std::array<std::array<float, 4>, 16> inputs{};
    std::array<std::array<uint32_t, 4>, 192> context_output{};
    inputs[0] = {2.0f, 4.0f, 6.0f, 8.0f};

    program[0][1] = 0x1Bu | (1u << 21u);
    program[0][2] = 2u << 26u;
    program[0][3] = 1u | (1u << 11u) | (0xFu << 12u);

    const Nv2aVertexProgramResult output = execute_nv2a_vertex_program(
        program,
        constants,
        0u,
        inputs,
        &context_output
    );
    context.expect(output.valid, "single MOV program terminates successfully");
    context.expect(output.output_masks[0] == 0xFu, "position output mask is retained");
    expect_vector(context, output.outputs[0], inputs[0], "input MOV reaches position output");

    program = {};
    program[0][1] = 0x1Bu | (1u << 21u);
    program[0][2] = 2u << 26u;
    program[0][3] = 1u | (5u << 3u) | (0xFu << 12u);
    execute_nv2a_vertex_program(program, constants, 0u, inputs, &context_output);
    for (size_t lane = 0; lane < inputs[0].size(); ++lane) {
        context.expect(
            context_output[5][lane] == float_bits(inputs[0][lane]),
            "constant output is copied back bit-exactly"
        );
    }
}

void test_invalid_program(TestContext& context) {
    std::array<std::array<uint32_t, 4>, 136> program{};
    std::array<std::array<uint32_t, 4>, 192> constants{};
    std::array<std::array<float, 4>, 16> inputs{};
    program[0][1] = 14u << 21u;
    program[0][3] = 1u;

    const Nv2aVertexProgramResult output = execute_nv2a_vertex_program(
        program,
        constants,
        0u,
        inputs
    );
    context.expect(!output.valid, "unsupported MAC opcode rejects the program");
}

struct TestCase {
    std::string_view name;
    void (*run)(TestContext&);
};

constexpr std::array<TestCase, 5> kTestCases = {{
    {"field_and_swizzle", test_field_and_swizzle},
    {"vector_operations", test_vector_operations},
    {"write_mask", test_write_mask},
    {"program_execution", test_program_execution},
    {"invalid_program", test_invalid_program},
}};

}  // namespace

int main(int argc, char** argv) {
    const std::string_view requested = argc > 1 ? argv[1] : "";
    bool matched = false;
    int failure_count = 0;
    for (const TestCase& test_case : kTestCases) {
        if (!requested.empty() && requested != test_case.name) {
            continue;
        }
        matched = true;
        TestContext context{};
        test_case.run(context);
        failure_count += context.failure_count;
        std::cout << test_case.name << ": "
                  << (context.failure_count == 0 ? "passed" : "failed") << '\n';
    }
    if (!matched) {
        std::cerr << "unknown native test case: " << requested << '\n';
        return 2;
    }
    return failure_count == 0 ? 0 : 1;
}
