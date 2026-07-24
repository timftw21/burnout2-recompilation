#include "host/dirty_ranges.h"
#include "host/frame_metrics.h"
#include "host/live_transport_layout.h"
#include "host/native_pipeline_state.h"
#include "nv2a/texture_layout.h"

#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <string_view>
#include <vector>

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

void test_transport_layout_and_sequences(TestContext& context) {
    using namespace b2r::live_transport;
    context.expect(kLiveControlMagic[0] == 'B', "control magic");
    context.expect(kLiveControlSchemaVersion == 1u, "schema version");
    context.expect(
        kLiveManifestPayloadOffset < kLiveControlSize,
        "manifest payload fits control mapping"
    );
    context.expect(
        kLiveCommandReadCursorOffset + sizeof(uint64_t) <= kLiveCommandDataOffset,
        "command cursors fit before ring data"
    );
    context.expect(
        kLiveResourceMetadataOffsets[1] + 16u <= kLiveResourceHeaderSize,
        "resource slot metadata fits header"
    );
    context.expect(!sequence_is_published(0u), "zero sequence is unpublished");
    context.expect(!sequence_is_published(3u), "odd sequence is being written");
    context.expect(sequence_is_published(4u), "even nonzero sequence is published");
    context.expect(sequence_is_stable(4u, 4u), "matching published sequence is stable");
    context.expect(!sequence_is_stable(4u, 6u), "changed sequence is unstable");
}

void test_texture_layout_and_formats(TestContext& context) {
    using namespace b2r::nv2a;
    context.expect(
        nv2a_canonical_resource_address(0x21234567u) == 0x01234567u,
        "DMA alias canonicalization"
    );
    context.expect(
        nv2a_canonical_resource_address(0x11234567u) == 0x11234567u,
        "unaliased address preservation"
    );
    context.expect(nv2a_texture_format_matches("DXT1", 0x0C00u), "DXT1 format");
    context.expect(
        nv2a_texture_format_matches("format_2A", 0x2A00u),
        "unknown format fallback name"
    );
    context.expect(nv2a_texture_format_is_linear(0x1200u), "linear format flag");
    context.expect(!nv2a_texture_format_is_linear(0x0C00u), "swizzled format flag");
    context.expect(
        nv2a_texture_extent(0x1200u, (320u << 16u) | 240u)
            == std::pair<uint32_t, uint32_t>{320u, 240u},
        "linear image rectangle extent"
    );
    context.expect(
        nv2a_texture_extent((6u << 20u) | (5u << 24u), 0u)
            == std::pair<uint32_t, uint32_t>{64u, 32u},
        "logarithmic swizzled extent"
    );
}

void test_texture_unswizzle(TestContext& context) {
    const std::vector<uint8_t> swizzled = {0u, 1u, 2u, 3u, 4u, 5u, 6u, 7u};
    const std::vector<uint8_t> linear = b2r::nv2a::nv2a_unswizzle_texture_2d(
        swizzled,
        4u,
        2u,
        1u
    );
    context.expect(
        linear == std::vector<uint8_t>({0u, 1u, 4u, 5u, 2u, 3u, 6u, 7u}),
        "rectangular Morton unswizzle"
    );
    context.expect(
        b2r::nv2a::nv2a_unswizzle_texture_2d(swizzled, 4u, 2u, 0u)
            == std::vector<uint8_t>(swizzled.size(), 0u),
        "zero-sized pixel format is bounded"
    );
}

void test_dirty_range_lifetime(TestContext& context) {
    std::vector<b2r::host::DirtyRange> ranges;
    b2r::host::append_dirty_range(ranges, 100u, 16u);
    b2r::host::append_dirty_range(ranges, 140u, 4u);
    b2r::host::append_dirty_range(ranges, 300u, 20u);
    b2r::host::append_dirty_range(ranges, 400u, 0u);

    context.expect(ranges.size() == 2u, "nearby dirty ranges merge");
    context.expect(
        ranges[0] == b2r::host::DirtyRange{100u, 44u},
        "merged dirty range covers the complete interval"
    );
    context.expect(
        ranges[1] == b2r::host::DirtyRange{300u, 20u},
        "distant dirty range remains independent"
    );
    context.expect(b2r::host::dirty_range_bytes(ranges) == 64u, "dirty byte total");
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

struct TestCase {
    std::string_view name;
    void (*run)(TestContext&);
};

constexpr std::array<TestCase, 5> kTestCases = {{
    {"transport_layout_and_sequences", test_transport_layout_and_sequences},
    {"texture_layout_and_formats", test_texture_layout_and_formats},
    {"texture_unswizzle", test_texture_unswizzle},
    {"dirty_range_lifetime", test_dirty_range_lifetime},
    {"pipeline_key_and_fps_sampler", test_pipeline_key_and_fps_sampler},
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
