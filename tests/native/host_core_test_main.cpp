#include "host_core_test_cases.h"

#include <array>

int main(int argc, char** argv) {
    using namespace b2r::test;
    constexpr std::array<TestCase, 8> kTestCases = {{
        {"transport_layout_and_sequences", test_transport_layout_and_sequences},
        {"texture_layout_and_formats", test_texture_layout_and_formats},
        {"native_pipeline_primitives", test_native_pipeline_primitives},
        {"nv2a_raster_coordinates", test_nv2a_raster_coordinates},
        {"texture_unswizzle", test_texture_unswizzle},
        {"dirty_range_lifetime", test_dirty_range_lifetime},
        {"command_work_cache_layout_reuse", test_command_work_cache_layout_reuse},
        {"pipeline_key_and_fps_sampler", test_pipeline_key_and_fps_sampler},
    }};
    return run_test_cases(argc, argv, kTestCases);
}
