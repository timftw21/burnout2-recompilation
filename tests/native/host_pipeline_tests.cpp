#include "host/frame_metrics.h"
#include "host/native_pipeline_state.h"
#include "host_core_test_cases.h"

#include <chrono>
#include <cmath>

namespace b2r::test {

void test_native_pipeline_primitives(TestContext& context) {
    using b2r::host::native_pipeline_primitive_supported;
    context.expect(native_pipeline_primitive_supported(2u), "line-list primitive support");
    context.expect(native_pipeline_primitive_supported(5u), "triangle-list primitive support");
    context.expect(native_pipeline_primitive_supported(6u), "triangle-strip primitive support");
    context.expect(
        !native_pipeline_primitive_supported(3u),
        "unimplemented line-loop primitive stays rejected"
    );
}

void test_pipeline_key_and_fps_sampler(TestContext& context) {
    b2r::host::NativePipelineState first{};
    b2r::host::NativePipelineState second{};
    const b2r::host::NativePipelineStateHash hash{};
    context.expect(first == second, "default pipeline keys match");
    context.expect(hash(first) == hash(second), "equal pipeline keys hash equally");
    second.raw_attribute_fetch = true;
    context.expect(first != second, "pipeline key includes raw attribute mode");
    context.expect(hash(first) != hash(second), "pipeline hash includes raw attribute mode");

    using Clock = b2r::host::CompletedFlipFpsSampler::Clock;
    const Clock::time_point start{};
    b2r::host::CompletedFlipFpsSampler sampler(std::chrono::seconds(1));
    sampler.reset(start, 100u);
    context.expect(
        !sampler.observe(start + std::chrono::milliseconds(999), 124u).has_value(),
        "incomplete FPS interval is retained"
    );
    const auto sample = sampler.observe(start + std::chrono::seconds(1), 124u);
    context.expect(sample.has_value(), "complete FPS interval yields a sample");
    context.expect(sample->completed_flips == 24u, "FPS sample counts completed flips");
    context.expect(std::abs(sample->fps - 24.0) < 0.00001, "FPS sample rate");
    const auto reset_sample = sampler.observe(start + std::chrono::seconds(2), 5u);
    context.expect(reset_sample->completed_flips == 0u, "flip counter regression is bounded");
}

}  // namespace b2r::test
