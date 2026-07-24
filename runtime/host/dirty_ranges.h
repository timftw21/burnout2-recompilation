#pragma once

#include <algorithm>
#include <cstdint>
#include <vector>

namespace b2r::host {

struct DirtyRange {
    uint32_t offset = 0u;
    uint32_t size = 0u;

    bool operator==(const DirtyRange& other) const {
        return offset == other.offset && size == other.size;
    }
};

inline void append_dirty_range(
    std::vector<DirtyRange>& ranges,
    uint32_t offset,
    uint32_t size,
    uint32_t merge_gap_bytes = 64u
) {
    if (size == 0u) {
        return;
    }
    const uint64_t range_end = static_cast<uint64_t>(offset) + size;
    if (!ranges.empty()) {
        DirtyRange& previous = ranges.back();
        const uint64_t previous_end = static_cast<uint64_t>(previous.offset)
            + previous.size;
        if (offset <= previous_end + merge_gap_bytes) {
            previous.size = static_cast<uint32_t>(
                std::max(previous_end, range_end) - previous.offset);
            return;
        }
    }
    ranges.push_back({offset, size});
}

inline uint64_t dirty_range_bytes(const std::vector<DirtyRange>& ranges) {
    uint64_t total = 0u;
    for (const DirtyRange& range : ranges) {
        total += range.size;
    }
    return total;
}

}  // namespace b2r::host
