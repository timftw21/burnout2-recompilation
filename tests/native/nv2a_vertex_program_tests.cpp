#include "nv2a_vertex_program_test_cases.h"

namespace b2r::test {

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

}  // namespace b2r::test
