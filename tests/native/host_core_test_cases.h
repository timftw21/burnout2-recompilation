#pragma once

#include "native_test_harness.h"

namespace b2r::test {

void test_transport_layout_and_sequences(TestContext& context);
void test_texture_layout_and_formats(TestContext& context);
void test_native_pipeline_primitives(TestContext& context);
void test_nv2a_raster_coordinates(TestContext& context);
void test_texture_unswizzle(TestContext& context);
void test_dirty_range_lifetime(TestContext& context);
void test_command_work_cache_layout_reuse(TestContext& context);
void test_pipeline_key_and_fps_sampler(TestContext& context);

}  // namespace b2r::test
