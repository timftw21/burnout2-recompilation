#pragma once

#include <chrono>
#include <cstdint>
#include <optional>

namespace b2r::host {

struct CompletedFlipFpsSample {
    uint64_t completed_flips = 0u;
    double seconds = 0.0;
    double fps = 0.0;
};

class CompletedFlipFpsSampler {
public:
    using Clock = std::chrono::steady_clock;

    explicit CompletedFlipFpsSampler(
        Clock::duration interval = std::chrono::seconds(1)
    ) : interval_(interval) {}

    void reset(Clock::time_point now, uint64_t completed_flip_count) {
        sample_start_ = now;
        sample_start_flip_count_ = completed_flip_count;
        last_sample_.reset();
        initialized_ = true;
    }

    std::optional<CompletedFlipFpsSample> observe(
        Clock::time_point now,
        uint64_t completed_flip_count
    ) {
        if (!initialized_) {
            reset(now, completed_flip_count);
            return std::nullopt;
        }
        const Clock::duration elapsed = now - sample_start_;
        if (elapsed < interval_) {
            return std::nullopt;
        }
        const uint64_t completed_flips = completed_flip_count >= sample_start_flip_count_
            ? completed_flip_count - sample_start_flip_count_
            : 0u;
        const double seconds = std::chrono::duration<double>(elapsed).count();
        last_sample_ = CompletedFlipFpsSample{
            completed_flips,
            seconds,
            seconds > 0.0 ? static_cast<double>(completed_flips) / seconds : 0.0,
        };
        sample_start_ = now;
        sample_start_flip_count_ = completed_flip_count;
        return last_sample_;
    }

    const std::optional<CompletedFlipFpsSample>& last_sample() const {
        return last_sample_;
    }

private:
    Clock::duration interval_;
    Clock::time_point sample_start_{};
    uint64_t sample_start_flip_count_ = 0u;
    std::optional<CompletedFlipFpsSample> last_sample_;
    bool initialized_ = false;
};

}  // namespace b2r::host
