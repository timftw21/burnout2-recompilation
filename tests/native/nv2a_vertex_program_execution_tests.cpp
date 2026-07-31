#include "nv2a_vertex_program_test_cases.h"

#include <array>
#include <cstddef>
#include <cstdint>

namespace b2r::test {

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
        program, constants, 0u, inputs, &context_output);
    context.expect(output.valid, "single MOV program terminates successfully");
    context.expect(output.output_masks[0] == 0xFu, "position output mask is retained");
    expect_vector(context, output.outputs[0], inputs[0], "input MOV reaches position output");

    program = {};
    program[0][1] = 0x1Bu | (1u << 21u);
    program[0][2] = 2u << 26u;
    program[0][3] = 1u | (5u << 3u) | (0xFu << 12u);
    execute_nv2a_vertex_program(program, constants, 0u, inputs, &context_output);
    for (std::size_t lane = 0; lane < inputs[0].size(); ++lane) {
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
        program, constants, 0u, inputs);
    context.expect(!output.valid, "unsupported MAC opcode rejects the program");
}

}  // namespace b2r::test
