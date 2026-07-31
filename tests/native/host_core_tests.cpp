#include "host/command_work_cache.h"
#include "host_core_test_cases.h"

#include <cstring>
#include <cstdint>
#include <vector>

namespace b2r::test {

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

}  // namespace b2r::test
