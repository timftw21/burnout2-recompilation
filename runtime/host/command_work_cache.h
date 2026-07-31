#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iterator>
#include <limits>
#include <list>
#include <string>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

namespace b2r::host {

struct CommandSpanDescriptor {
    uint8_t kind = 0u;
    uint8_t flags = 0u;
    uint32_t address = 0u;
    uint32_t payload_offset = 0u;
    uint32_t payload_size = 0u;
    uint32_t logical_write_count = 0u;

    bool operator==(const CommandSpanDescriptor& other) const {
        return kind == other.kind
            && flags == other.flags
            && address == other.address
            && payload_offset == other.payload_offset
            && payload_size == other.payload_size
            && logical_write_count == other.logical_write_count;
    }
};

inline bool decode_command_span_descriptors(
    const std::vector<uint8_t>& packed_spans,
    std::vector<CommandSpanDescriptor>& descriptors,
    uint64_t* logical_write_count = nullptr,
    uint32_t* mmio_span_count = nullptr) {
    constexpr size_t kHeaderSize = 16u;
    descriptors.clear();
    uint64_t writes = 0u;
    uint32_t mmio = 0u;
    size_t cursor = 0u;
    while (cursor < packed_spans.size()) {
        if (packed_spans.size() - cursor < kHeaderSize) {
            descriptors.clear();
            return false;
        }
        const uint8_t* header = packed_spans.data() + cursor;
        const uint8_t kind = header[0];
        const uint8_t flags = header[1];
        uint32_t address = 0u;
        uint32_t payload_size = 0u;
        uint32_t span_write_count = 0u;
        std::memcpy(&address, header + 4u, sizeof(address));
        std::memcpy(&payload_size, header + 8u, sizeof(payload_size));
        std::memcpy(
            &span_write_count,
            header + 12u,
            sizeof(span_write_count));
        cursor += kHeaderSize;
        if (kind > 1u || (flags & ~1u) != 0u || payload_size == 0u
            || span_write_count == 0u
            || cursor > std::numeric_limits<uint32_t>::max()
            || payload_size > packed_spans.size() - cursor) {
            descriptors.clear();
            return false;
        }
        descriptors.push_back({
            kind,
            flags,
            address,
            static_cast<uint32_t>(cursor),
            payload_size,
            span_write_count,
        });
        writes += span_write_count;
        mmio += static_cast<uint32_t>(kind == 0u);
        cursor += payload_size;
    }
    if (logical_write_count != nullptr) {
        *logical_write_count = writes;
    }
    if (mmio_span_count != nullptr) {
        *mmio_span_count = mmio;
    }
    return true;
}

struct CommandWorkCacheMetrics {
    uint64_t lookup_count = 0u;
    uint64_t hit_count = 0u;
    uint64_t miss_count = 0u;
    uint64_t build_count = 0u;
    uint64_t fallback_count = 0u;
    uint64_t eviction_count = 0u;
    uint64_t epoch_change_count = 0u;
    uint64_t reused_word_count = 0u;
    uint64_t materialized_word_count = 0u;
    uint64_t resident_bytes = 0u;
    uint64_t peak_resident_bytes = 0u;
    uint32_t resident_plan_count = 0u;
    uint32_t peak_resident_plan_count = 0u;
    uint32_t last_word_count = 0u;
    uint32_t last_segment_count = 0u;
    uint32_t last_segment_hit_count = 0u;
    uint32_t last_segment_build_count = 0u;
    bool last_cacheable = false;
    bool last_hit = false;
};

struct CommandWorkCacheTraceDescriptor {
    uint32_t relative_address = 0u;
    uint32_t payload_size = 0u;
};

struct CommandWorkCacheTraceSegment {
    std::vector<CommandWorkCacheTraceDescriptor> descriptors;
};

struct CommandWorkCacheTraceEviction {
    uint64_t layout_id = 0u;
    uint64_t plan_bytes = 0u;
    uint64_t unused_lookup_count = 0u;
};

struct CommandWorkCacheTraceLookup {
    uint64_t layout_id = 0u;
    uint64_t plan_bytes = 0u;
    uint32_t reuse_distance = 0u;
    bool hit = false;
    bool retained = false;
    bool has_reuse_distance = false;
    std::vector<CommandWorkCacheTraceEviction> evictions;
};

struct CommandWorkCacheTraceReload {
    std::string epoch;
    bool cacheable = false;
    bool epoch_changed = false;
    std::vector<CommandWorkCacheTraceSegment> segments;
    std::vector<CommandWorkCacheTraceLookup> lookups;
};

class CommandWorkCache {
public:
    static constexpr size_t kPlanCapacity = 256u;
    static constexpr uint64_t kByteCapacity = 64u * 1024u * 1024u;

    template <typename Word>
    bool materialize(
        std::string_view epoch,
        const std::vector<uint8_t>& packed_spans,
        const std::vector<CommandSpanDescriptor>& descriptors,
        uint32_t push_buffer_begin,
        uint32_t push_buffer_end,
        std::vector<Word>& words) {
        metrics_.last_cacheable = false;
        metrics_.last_hit = false;
        metrics_.last_word_count = 0u;
        metrics_.last_segment_count = 0u;
        metrics_.last_segment_hit_count = 0u;
        metrics_.last_segment_build_count = 0u;
        reset_trace(epoch);
        trace_.epoch_changed = begin_epoch(epoch);
        words.clear();
        if (!layout_is_cacheable(
                descriptors,
                packed_spans.size(),
                push_buffer_begin,
                push_buffer_end)) {
            ++metrics_.fallback_count;
            return false;
        }
        metrics_.last_cacheable = true;
        trace_.cacheable = true;
        uint32_t run_id_offset = 0u;
        auto materialize_unit = [&](bool allow_contiguous_merge) {
            normalize_segment(segment_descriptors_, normalized_descriptors_);
            const uint64_t hash = layout_hash(normalized_descriptors_);
            ++metrics_.lookup_count;
            ++metrics_.last_segment_count;
            CommandWorkCacheTraceLookup* trace_lookup = nullptr;
            if (trace_enabled_) {
                trace_.lookups.push_back({});
                trace_lookup = &trace_.lookups.back();
                trace_lookup->layout_id = hash;
            }
            const auto indexed_plans = plan_index_.equal_range(hash);
            for (auto indexed = indexed_plans.first;
                 indexed != indexed_plans.second;
                 ++indexed) {
                const PlanIterator plan_iterator = indexed->second;
                Plan& plan = *plan_iterator;
                if (plan.hash != hash
                    || plan.descriptors != normalized_descriptors_) {
                    continue;
                }
                if (trace_lookup != nullptr) {
                    trace_lookup->hit = true;
                    trace_lookup->retained = true;
                    trace_lookup->has_reuse_distance = true;
                    trace_lookup->plan_bytes = byte_size(plan);
                    trace_lookup->reuse_distance = static_cast<uint32_t>(
                        std::distance(std::next(plan_iterator), plans_.end()));
                }
                ++metrics_.hit_count;
                ++metrics_.last_segment_hit_count;
                metrics_.reused_word_count += plan.words.size();
                plan.last_use = ++use_tick_;
                append_plan(
                    plan,
                    packed_spans,
                    segment_descriptors_,
                    run_id_offset,
                    allow_contiguous_merge,
                    words);
                plans_.splice(plans_.end(), plans_, plan_iterator);
                return true;
            }

            ++metrics_.miss_count;
            ++metrics_.last_segment_build_count;
            Plan plan{};
            plan.hash = hash;
            plan.descriptors = normalized_descriptors_;
            if (!build_plan(
                    segment_descriptors_,
                    packed_spans.size(),
                    push_buffer_begin,
                    push_buffer_end,
                    plan.words)) {
                return false;
            }
            ++metrics_.build_count;
            append_plan(
                plan,
                packed_spans,
                segment_descriptors_,
                run_id_offset,
                allow_contiguous_merge,
                words);
            plan.last_use = ++use_tick_;
            const uint64_t plan_bytes = byte_size(plan);
            if (trace_lookup != nullptr) {
                trace_lookup->plan_bytes = plan_bytes;
            }
            if (plan_bytes <= kByteCapacity) {
                while (!plans_.empty()
                       && (plans_.size() >= kPlanCapacity
                           || metrics_.resident_bytes + plan_bytes
                               > kByteCapacity)) {
                    evict_oldest(trace_lookup);
                }
                metrics_.resident_bytes += plan_bytes;
                plans_.push_back(std::move(plan));
                const PlanIterator inserted = std::prev(plans_.end());
                plan_index_.emplace(inserted->hash, inserted);
                update_residency_metrics();
                if (trace_lookup != nullptr) {
                    trace_lookup->retained = true;
                }
            }
            return true;
        };
        auto materialize_segment = [&](size_t begin, size_t end) {
            if (begin >= end) {
                return true;
            }
            uint32_t aperture_begin = push_buffer_end;
            uint32_t aperture_end = push_buffer_begin;
            for (size_t index = begin; index < end; ++index) {
                const CommandSpanDescriptor& descriptor = descriptors[index];
                aperture_begin = std::min(
                    aperture_begin, descriptor.address);
                aperture_end = std::max(
                    aperture_end,
                    descriptor.address + descriptor.payload_size);
            }
            if (trace_enabled_) {
                CommandWorkCacheTraceSegment trace_segment{};
                trace_segment.descriptors.reserve(end - begin);
                for (size_t index = begin; index < end; ++index) {
                    const CommandSpanDescriptor& descriptor =
                        descriptors[index];
                    trace_segment.descriptors.push_back({
                        descriptor.address - aperture_begin,
                        descriptor.payload_size,
                    });
                }
                trace_.segments.push_back(std::move(trace_segment));
            }
            constexpr uint32_t kWindowBytes = 16u * 1024u;
            bool allow_contiguous_merge = false;
            for (uint32_t window_begin = aperture_begin;
                 window_begin < aperture_end;) {
                const uint32_t window_end = static_cast<uint32_t>(
                    std::min<uint64_t>(
                        aperture_end,
                        static_cast<uint64_t>(window_begin)
                            + kWindowBytes));
                segment_descriptors_.clear();
                for (size_t index = begin; index < end; ++index) {
                    const CommandSpanDescriptor& descriptor =
                        descriptors[index];
                    const uint32_t descriptor_end = descriptor.address
                        + descriptor.payload_size;
                    const uint32_t overlap_begin = std::max(
                        descriptor.address, window_begin);
                    const uint32_t overlap_end = std::min(
                        descriptor_end, window_end);
                    if (overlap_begin >= overlap_end) {
                        continue;
                    }
                    CommandSpanDescriptor fragment = descriptor;
                    fragment.address = overlap_begin;
                    fragment.payload_offset += overlap_begin
                        - descriptor.address;
                    fragment.payload_size = overlap_end - overlap_begin;
                    segment_descriptors_.push_back(fragment);
                }
                if (segment_descriptors_.empty()) {
                    allow_contiguous_merge = false;
                } else if (!materialize_unit(allow_contiguous_merge)) {
                    return false;
                } else {
                    allow_contiguous_merge = true;
                }
                window_begin = window_end;
            }
            return true;
        };

        size_t segment_begin = descriptors.size();
        uint32_t segment_max_address = 0u;
        for (size_t index = 0u; index < descriptors.size(); ++index) {
            const CommandSpanDescriptor& descriptor = descriptors[index];
            const bool push_buffer_span = descriptor.kind == 1u
                && descriptor.address >= push_buffer_begin
                && descriptor.address < push_buffer_end;
            if (!push_buffer_span) {
                if (!materialize_segment(segment_begin, index)) {
                    ++metrics_.fallback_count;
                    words.clear();
                    return false;
                }
                segment_begin = descriptors.size();
                segment_max_address = 0u;
                continue;
            }
            if (segment_begin != descriptors.size()
                && descriptor.address + 0x1000u < segment_max_address) {
                if (!materialize_segment(segment_begin, index)) {
                    ++metrics_.fallback_count;
                    words.clear();
                    return false;
                }
                segment_begin = descriptors.size();
                segment_max_address = 0u;
            }
            if (segment_begin == descriptors.size()) {
                segment_begin = index;
            }
            segment_max_address = std::max(
                segment_max_address,
                descriptor.address + descriptor.payload_size);
        }
        if (!materialize_segment(segment_begin, descriptors.size())) {
            ++metrics_.fallback_count;
            words.clear();
            return false;
        }
        metrics_.last_hit = metrics_.last_segment_count != 0u
            && metrics_.last_segment_hit_count == metrics_.last_segment_count;
        metrics_.last_word_count = static_cast<uint32_t>(words.size());
        return true;
    }

    const CommandWorkCacheMetrics& metrics() const {
        return metrics_;
    }

    void set_trace_enabled(bool enabled) {
        trace_enabled_ = enabled;
        if (!enabled) {
            trace_ = {};
        }
    }

    const CommandWorkCacheTraceReload& trace() const {
        return trace_;
    }

    void mark_unused() {
        metrics_.last_cacheable = false;
        metrics_.last_hit = false;
        metrics_.last_word_count = 0u;
        metrics_.last_segment_count = 0u;
        metrics_.last_segment_hit_count = 0u;
        metrics_.last_segment_build_count = 0u;
        if (trace_enabled_) {
            trace_ = {};
        }
    }

private:
    struct WordLayout {
        uint32_t descriptor_index = 0u;
        uint32_t descriptor_payload_offset = 0u;
        uint32_t run_id = 0u;
    };

    struct Plan {
        uint64_t hash = 0u;
        uint64_t last_use = 0u;
        std::vector<CommandSpanDescriptor> descriptors;
        std::vector<WordLayout> words;
    };

    using PlanList = std::list<Plan>;
    using PlanIterator = PlanList::iterator;

    static uint64_t mix_hash(uint64_t hash, uint32_t value) {
        constexpr uint64_t kPrime = 1099511628211ull;
        for (uint32_t shift = 0u; shift < 32u; shift += 8u) {
            hash ^= (value >> shift) & 0xFFu;
            hash *= kPrime;
        }
        return hash;
    }

    static uint64_t layout_hash(
        const std::vector<CommandSpanDescriptor>& descriptors) {
        uint64_t hash = 1469598103934665603ull;
        for (const CommandSpanDescriptor& descriptor : descriptors) {
            hash = mix_hash(hash, descriptor.kind);
            hash = mix_hash(hash, descriptor.flags);
            hash = mix_hash(hash, descriptor.address);
            hash = mix_hash(hash, descriptor.payload_offset);
            hash = mix_hash(hash, descriptor.payload_size);
            hash = mix_hash(hash, descriptor.logical_write_count);
        }
        return hash;
    }

    static bool layout_is_cacheable(
        const std::vector<CommandSpanDescriptor>& descriptors,
        size_t packed_size,
        uint32_t push_buffer_begin,
        uint32_t push_buffer_end) {
        if (push_buffer_begin >= push_buffer_end) {
            return false;
        }
        for (const CommandSpanDescriptor& descriptor : descriptors) {
            if (descriptor.payload_size == 0u
                || descriptor.payload_offset > packed_size
                || descriptor.payload_size
                    > packed_size - descriptor.payload_offset) {
                return false;
            }
            if (descriptor.kind != 1u
                || descriptor.address < push_buffer_begin
                || descriptor.address >= push_buffer_end) {
                continue;
            }
            if ((descriptor.address & 3u) != 0u
                || (descriptor.payload_size & 3u) != 0u
                || descriptor.payload_size
                    > push_buffer_end - descriptor.address) {
                return false;
            }
        }
        return true;
    }

    static void normalize_segment(
        const std::vector<CommandSpanDescriptor>& descriptors,
        std::vector<CommandSpanDescriptor>& normalized) {
        normalized.clear();
        if (normalized.capacity() < descriptors.size()) {
            normalized.reserve(descriptors.size());
        }
        if (descriptors.empty()) {
            return;
        }
        const uint32_t segment_anchor = descriptors.front().address;
        for (const CommandSpanDescriptor& descriptor : descriptors) {
            CommandSpanDescriptor structural = descriptor;
            structural.address = descriptor.address - segment_anchor;
            structural.flags = 0u;
            structural.payload_offset = 0u;
            structural.logical_write_count = 0u;
            normalized.push_back(structural);
        }
    }

    static bool build_plan(
        const std::vector<CommandSpanDescriptor>& descriptors,
        size_t packed_size,
        uint32_t push_buffer_begin,
        uint32_t push_buffer_end,
        std::vector<WordLayout>& words) {
        if (push_buffer_begin >= push_buffer_end) {
            return false;
        }
        uint32_t aperture_begin = push_buffer_end - push_buffer_begin;
        uint32_t aperture_end = 0u;
        for (const CommandSpanDescriptor& descriptor : descriptors) {
            if (descriptor.payload_size == 0u
                || descriptor.payload_offset > packed_size
                || descriptor.payload_size
                    > packed_size - descriptor.payload_offset) {
                return false;
            }
            if (descriptor.kind != 1u
                || descriptor.address < push_buffer_begin
                || descriptor.address >= push_buffer_end) {
                continue;
            }
            if ((descriptor.address & 3u) != 0u
                || (descriptor.payload_size & 3u) != 0u
                || descriptor.payload_size
                    > push_buffer_end - descriptor.address) {
                return false;
            }
            const uint32_t begin = descriptor.address - push_buffer_begin;
            aperture_begin = std::min(aperture_begin, begin);
            aperture_end = std::max(
                aperture_end, begin + descriptor.payload_size);
        }
        words.clear();
        if (aperture_begin >= aperture_end) {
            return true;
        }
        if ((aperture_begin & 3u) != 0u || (aperture_end & 3u) != 0u) {
            return false;
        }

        constexpr uint32_t kInvalidDescriptor =
            std::numeric_limits<uint32_t>::max();
        const uint32_t word_span =
            (aperture_end - aperture_begin) / sizeof(uint32_t);
        std::vector<uint32_t> pending_descriptor_indices(
            word_span, kInvalidDescriptor);
        std::vector<uint32_t> pending_descriptor_offsets(word_span, 0u);
        uint32_t pending_min_word = word_span;
        uint32_t pending_max_word = 0u;
        uint32_t pending_max_address = 0u;
        uint32_t run_id = 0u;

        auto flush = [&]() {
            bool emitted_word = false;
            uint32_t previous_address = 0u;
            for (uint32_t word_index = pending_min_word;
                 word_index < pending_max_word;
                 ++word_index) {
                const uint32_t descriptor_index =
                    pending_descriptor_indices[word_index];
                if (descriptor_index == kInvalidDescriptor) {
                    continue;
                }
                const uint32_t descriptor_offset =
                    pending_descriptor_offsets[word_index];
                const uint32_t address =
                    descriptors[descriptor_index].address
                    + descriptor_offset;
                if (!emitted_word
                    || address != previous_address + sizeof(uint32_t)) {
                    ++run_id;
                }
                words.push_back({
                    descriptor_index,
                    descriptor_offset,
                    run_id,
                });
                emitted_word = true;
                previous_address = address;
            }
            if (pending_min_word < pending_max_word) {
                std::fill(
                    pending_descriptor_indices.begin() + pending_min_word,
                    pending_descriptor_indices.begin() + pending_max_word,
                    kInvalidDescriptor);
            }
            pending_min_word = word_span;
            pending_max_word = 0u;
            pending_max_address = 0u;
        };

        for (size_t descriptor_index = 0u;
             descriptor_index < descriptors.size();
             ++descriptor_index) {
            const CommandSpanDescriptor& descriptor =
                descriptors[descriptor_index];
            if (descriptor.kind != 1u
                || descriptor.address < push_buffer_begin
                || descriptor.address >= push_buffer_end) {
                flush();
                continue;
            }
            if (pending_min_word != word_span
                && descriptor.address + 0x1000u < pending_max_address) {
                flush();
            }
            const uint32_t first_word = (
                descriptor.address - push_buffer_begin - aperture_begin)
                / sizeof(uint32_t);
            const uint32_t payload_words =
                descriptor.payload_size / sizeof(uint32_t);
            for (uint32_t word_index = 0u;
                 word_index < payload_words;
                 ++word_index) {
                const uint32_t destination = first_word + word_index;
                pending_descriptor_indices[destination] =
                    static_cast<uint32_t>(descriptor_index);
                pending_descriptor_offsets[destination] =
                    word_index * sizeof(uint32_t);
            }
            pending_min_word = std::min(pending_min_word, first_word);
            pending_max_word = std::max(
                pending_max_word, first_word + payload_words);
            pending_max_address = std::max(
                pending_max_address,
                descriptor.address + descriptor.payload_size);
        }
        flush();
        return true;
    }

    template <typename Word>
    void append_plan(
        const Plan& plan,
        const std::vector<uint8_t>& packed_spans,
        const std::vector<CommandSpanDescriptor>& descriptors,
        uint32_t& run_id_offset,
        bool allow_contiguous_merge,
        std::vector<Word>& words) {
        const size_t required_size = words.size() + plan.words.size();
        if (words.capacity() < required_size) {
            words.reserve(required_size);
        }
        bool merge_first_run = false;
        if (allow_contiguous_merge && !words.empty()
            && !plan.words.empty()) {
            const WordLayout& first_layout = plan.words.front();
            const CommandSpanDescriptor& first_descriptor =
                descriptors[first_layout.descriptor_index];
            const uint32_t first_address = first_descriptor.address
                + first_layout.descriptor_payload_offset;
            merge_first_run = first_address
                == words.back().address + sizeof(uint32_t);
        }
        for (const WordLayout& layout : plan.words) {
            const CommandSpanDescriptor& descriptor =
                descriptors[layout.descriptor_index];
            uint32_t value = 0u;
            std::memcpy(
                &value,
                packed_spans.data() + descriptor.payload_offset
                    + layout.descriptor_payload_offset,
                sizeof(value));
            words.push_back({
                descriptor.address + layout.descriptor_payload_offset,
                value,
                run_id_offset + layout.run_id
                    - static_cast<uint32_t>(merge_first_run),
            });
        }
        if (!plan.words.empty()) {
            run_id_offset += plan.words.back().run_id
                - static_cast<uint32_t>(merge_first_run);
        }
        metrics_.materialized_word_count += plan.words.size();
    }

    void reset_trace(std::string_view epoch) {
        if (!trace_enabled_) {
            return;
        }
        trace_ = {};
        trace_.epoch.assign(epoch.data(), epoch.size());
    }

    bool begin_epoch(std::string_view epoch) {
        if (epoch_ == epoch) {
            return false;
        }
        if (!epoch_.empty()) {
            ++metrics_.epoch_change_count;
        }
        plans_.clear();
        plan_index_.clear();
        metrics_.resident_bytes = 0u;
        metrics_.resident_plan_count = 0u;
        epoch_.assign(epoch.data(), epoch.size());
        return true;
    }

    static uint64_t byte_size(const Plan& plan) {
        return static_cast<uint64_t>(
            plan.descriptors.capacity() * sizeof(CommandSpanDescriptor)
            + plan.words.capacity() * sizeof(WordLayout));
    }

    void evict_oldest(CommandWorkCacheTraceLookup* trace_lookup) {
        const PlanIterator oldest = plans_.begin();
        if (trace_lookup != nullptr) {
            trace_lookup->evictions.push_back({
                oldest->hash,
                byte_size(*oldest),
                use_tick_ - oldest->last_use,
            });
        }
        const auto indexed_plans = plan_index_.equal_range(oldest->hash);
        for (auto indexed = indexed_plans.first;
             indexed != indexed_plans.second;
             ++indexed) {
            if (indexed->second == oldest) {
                plan_index_.erase(indexed);
                break;
            }
        }
        metrics_.resident_bytes -= byte_size(*oldest);
        plans_.erase(oldest);
        ++metrics_.eviction_count;
        metrics_.resident_plan_count = static_cast<uint32_t>(plans_.size());
    }

    void update_residency_metrics() {
        metrics_.resident_plan_count = static_cast<uint32_t>(plans_.size());
        metrics_.peak_resident_plan_count = std::max(
            metrics_.peak_resident_plan_count,
            metrics_.resident_plan_count);
        metrics_.peak_resident_bytes = std::max(
            metrics_.peak_resident_bytes,
            metrics_.resident_bytes);
    }

    std::string epoch_;
    PlanList plans_;
    std::unordered_multimap<uint64_t, PlanIterator> plan_index_;
    std::vector<CommandSpanDescriptor> segment_descriptors_;
    std::vector<CommandSpanDescriptor> normalized_descriptors_;
    CommandWorkCacheMetrics metrics_{};
    CommandWorkCacheTraceReload trace_{};
    bool trace_enabled_ = false;
    uint64_t use_tick_ = 0u;
};

}  // namespace b2r::host
