#include "host/dirty_ranges.h"
#include "host/live_transport_layout.h"
#include "host_core_test_cases.h"

#include <cstdint>
#include <vector>

namespace b2r::test {

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

}  // namespace b2r::test
