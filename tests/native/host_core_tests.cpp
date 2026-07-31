#include "host/command_work_cache.h"
#include "host/dirty_ranges.h"
#include "host/frame_metrics.h"
#include "host/live_transport_layout.h"
#include "host/native_pipeline_state.h"
#include "nv2a/raster_coordinates.h"
#include "nv2a/texture_layout.h"

#include <array>
#include <chrono>
#include <cmath>
#include <cstring>
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
        kLiveHotPathProfileStateOffset == 36u,
        "hot-path profile control occupies the reserved header word"
    );
    context.expect(
        kLiveHotPathProfileArmed == 1u &&
            kLiveHotPathProfileActive == 2u &&
            kLiveHotPathProfileComplete == 3u,
        "hot-path profile states are stable"
    );
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
        nv2a_texture_format_is_cubemap(0x0C04u),
        "cubemap format flag"
    );
    context.expect(
        !nv2a_texture_format_is_cubemap(0x0C00u),
        "2D format flag"
    );
    context.expect(
        nv2a_texture_uncompressed_bytes_per_pixel(0x0500u) == 2u,
        "RGB565 texel size"
    );
    context.expect(
        nv2a_texture_uncompressed_bytes_per_pixel(0x0600u) == 4u,
        "ARGB8888 texel size"
    );
    context.expect(
        nv2a_texture_uncompressed_bytes_per_pixel(0x0C00u) == 0u,
        "compressed texture has no uncompressed texel size"
    );
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

void test_native_pipeline_primitives(TestContext& context) {
    using b2r::host::native_pipeline_primitive_supported;
    context.expect(
        native_pipeline_primitive_supported(2u),
        "line-list primitive support"
    );
    context.expect(
        native_pipeline_primitive_supported(5u),
        "triangle-list primitive support"
    );
    context.expect(
        native_pipeline_primitive_supported(6u),
        "triangle-strip primitive support"
    );
    context.expect(
        !native_pipeline_primitive_supported(3u),
        "unimplemented line-loop primitive stays rejected"
    );
}

void test_nv2a_raster_coordinates(TestContext& context) {
    using namespace b2r::nv2a;
    constexpr float kWidth = 640.0f;
    constexpr float kHeight = 480.0f;
    constexpr float kTolerance = 0.000001f;
    context.expect(
        std::abs(kNv2aToVulkanPixelCenterBias - 0.03125f) < kTolerance,
        "NV2A to Vulkan pixel-center bias"
    );
    context.expect(
        std::abs(
            nv2a_screen_coordinate_to_vulkan_ndc(
                kNv2aViewportSubpixelBias,
                static_cast<uint32_t>(kWidth))
            - (-1.0f + 1.0f / kWidth)) < kTolerance,
        "left NV2A viewport edge reaches the first Vulkan pixel center"
    );
    context.expect(
        std::abs(
            nv2a_screen_coordinate_to_vulkan_ndc(
                kNv2aViewportSubpixelBias + kHeight,
                static_cast<uint32_t>(kHeight))
            - (1.0f + 1.0f / kHeight)) < kTolerance,
        "bottom NV2A viewport edge remains beyond the last Vulkan pixel center"
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

void test_command_work_cache_layout_reuse(TestContext& context) {
    using b2r::host::CommandSpanDescriptor;
    using b2r::host::CommandWorkCache;
    struct Word {
        uint32_t address = 0u;
        uint32_t value = 0u;
        uint32_t run_id = 0u;
    };
    constexpr uint32_t kPushBegin = 0x80000000u;
    constexpr uint32_t kPushEnd = 0x81000000u;
    std::vector<uint8_t> payload(24u);
    auto write_u32 = [&](size_t offset, uint32_t value) {
        std::memcpy(payload.data() + offset, &value, sizeof(value));
    };
    write_u32(0u, 10u);
    write_u32(4u, 11u);
    write_u32(8u, 12u);
    write_u32(12u, 20u);
    write_u32(16u, 21u);
    const std::vector<CommandSpanDescriptor> descriptors = {
        {1u, 0u, kPushBegin, 0u, 12u, 3u},
        {1u, 1u, kPushBegin + 4u, 12u, 8u, 2u},
    };
    CommandWorkCache cache;
    cache.set_trace_enabled(true);
    std::vector<Word> words;
    context.expect(
        cache.materialize(
            "epoch-a",
            payload,
            descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "aligned command layout is cacheable"
    );
    context.expect(words.size() == 3u, "overlapping spans retain three words");
    context.expect(
        words[0].value == 10u && words[1].value == 20u
            && words[2].value == 21u,
        "later span payload replaces overlapping words"
    );
    context.expect(
        words[0].run_id == words[2].run_id,
        "contiguous reconstructed words share one run"
    );
    write_u32(12u, 30u);
    context.expect(
        cache.materialize(
            "epoch-a",
            payload,
            descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "repeated layout reuses its plan"
    );
    context.expect(
        cache.metrics().last_hit && words[1].value == 30u,
        "cache hit reads current dynamic payload values"
    );
    const std::vector<CommandSpanDescriptor> shifted_descriptors = {
        {1u, 0u, kPushBegin + 0x2000u, 0u, 12u, 3u},
        {1u, 1u, kPushBegin + 0x2004u, 12u, 8u, 2u},
    };
    context.expect(
        cache.materialize(
            "epoch-a",
            payload,
            shifted_descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "shifted command layout reuses its structural plan"
    );
    context.expect(
        cache.metrics().last_hit
            && words[0].address == kPushBegin + 0x2000u
            && words[1].address == kPushBegin + 0x2004u
            && words[1].value == 30u,
        "structural hit uses current addresses and payload values"
    );
    const std::vector<CommandSpanDescriptor> segmented_descriptors = {
        shifted_descriptors[0],
        shifted_descriptors[1],
        {0u, 0u, 0xFED00008u, 20u, 4u, 1u},
        {1u, 0u, kPushBegin + 0x4000u, 20u, 4u, 1u},
    };
    context.expect(
        cache.materialize(
            "epoch-a",
            payload,
            segmented_descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "mixed command layout caches independent reconstruction segments"
    );
    context.expect(
        !cache.metrics().last_hit
            && cache.metrics().last_segment_count == 2u
            && cache.metrics().last_segment_hit_count == 1u
            && cache.metrics().last_segment_build_count == 1u,
        "stable segment hits when another segment changes"
    );
    context.expect(
        words.size() == 4u
            && words.front().run_id != words.back().run_id,
        "segment boundaries preserve distinct push-buffer runs"
    );
    std::vector<uint8_t> window_payload(20u * 1024u);
    auto write_window_u32 = [&](size_t offset, uint32_t value) {
        std::memcpy(
            window_payload.data() + offset,
            &value,
            sizeof(value));
    };
    write_window_u32(0u, 40u);
    write_window_u32(16u * 1024u, 41u);
    const std::vector<CommandSpanDescriptor> window_descriptors = {
        {1u, 0u, kPushBegin + 0x10000u, 0u,
         static_cast<uint32_t>(window_payload.size()), 5120u},
    };
    context.expect(
        cache.materialize(
            "epoch-a",
            window_payload,
            window_descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "large reconstruction segment splits into bounded windows"
    );
    context.expect(
        cache.metrics().last_segment_count == 2u
            && cache.metrics().last_segment_build_count == 2u,
        "first large segment builds two window plans"
    );
    const std::vector<CommandSpanDescriptor> shifted_window_descriptors = {
        {1u, 0u, kPushBegin + 0x20000u, 0u,
         static_cast<uint32_t>(window_payload.size()), 5120u},
    };
    context.expect(
        cache.materialize(
            "epoch-a",
            window_payload,
            shifted_window_descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "shifted large segment reuses both window plans"
    );
    context.expect(
        cache.metrics().last_hit
            && cache.metrics().last_segment_hit_count == 2u
            && words.size() == 5120u
            && words.front().value == 40u
            && words[4096u].value == 41u
            && words.front().run_id == words.back().run_id,
        "window hits retain current values and cross-window run continuity"
    );
    context.expect(
        cache.trace().segments.size() == 1u
            && cache.trace().lookups.size() == 2u
            && cache.trace().lookups[0].hit
            && cache.trace().lookups[0].has_reuse_distance
            && cache.trace().lookups[0].plan_bytes != 0u,
        "diagnostic trace records segment layouts, reuse distance, and plan bytes"
    );

    CommandWorkCache eviction_cache;
    eviction_cache.set_trace_enabled(true);
    std::vector<uint8_t> eviction_payload(
        (CommandWorkCache::kPlanCapacity + 1u) * sizeof(uint32_t));
    for (uint32_t plan_index = 0u;
         plan_index <= CommandWorkCache::kPlanCapacity;
         ++plan_index) {
        const uint32_t payload_size = (plan_index + 1u) * 4u;
        const std::vector<CommandSpanDescriptor> eviction_descriptors = {{
            1u,
            0u,
            kPushBegin,
            0u,
            payload_size,
            plan_index + 1u,
        }};
        context.expect(
            eviction_cache.materialize(
                "stable-epoch",
                eviction_payload,
                eviction_descriptors,
                kPushBegin,
                kPushEnd,
                words),
            "capacity trace fixture remains cacheable"
        );
    }
    context.expect(
        eviction_cache.trace().lookups.size() == 1u
            && eviction_cache.trace().lookups[0].evictions.size() == 1u
            && eviction_cache.trace().lookups[0]
                .evictions[0].plan_bytes != 0u
            && eviction_cache.trace().lookups[0]
                .evictions[0].unused_lookup_count
                == CommandWorkCache::kPlanCapacity,
        "diagnostic trace records the LRU eviction and unused lookup age"
    );
    context.expect(
        cache.materialize(
            "epoch-b",
            payload,
            descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "new epoch rebuilds a valid layout"
    );
    context.expect(
        !cache.metrics().last_hit
            && cache.metrics().epoch_change_count == 1u,
        "epoch change invalidates retained plans"
    );
    const std::vector<CommandSpanDescriptor> unaligned_descriptors = {
        {1u, 0u, kPushBegin + 1u, 0u, 4u, 1u},
    };
    context.expect(
        !cache.materialize(
            "epoch-b",
            payload,
            unaligned_descriptors,
            kPushBegin,
            kPushEnd,
            words),
        "unaligned command layout uses the generic fallback"
    );
    context.expect(
        cache.metrics().fallback_count == 1u,
        "uncacheable layout increments the fallback counter"
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

struct TestCase {
    std::string_view name;
    void (*run)(TestContext&);
};

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
