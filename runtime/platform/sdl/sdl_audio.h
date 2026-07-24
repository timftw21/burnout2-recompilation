#pragma once

#include <SDL3/SDL_audio.h>

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>

namespace b2r::platform {

struct SdlAudioStatus {
    bool open = false;
    int queued_bytes = 0;
    uint64_t submitted_bytes = 0u;
    uint64_t submitted_buffer_count = 0u;
    uint64_t dropped_buffer_count = 0u;
    uint64_t clear_count = 0u;
    uint64_t error_count = 0u;
    std::string last_error;
};

class SdlAudioOutput {
public:
    ~SdlAudioOutput();

    SdlAudioOutput(const SdlAudioOutput&) = delete;
    SdlAudioOutput& operator=(const SdlAudioOutput&) = delete;

    static SdlAudioOutput& instance();

    bool open();
    bool queue(const void* payload, size_t byte_count);
    bool clear();
    void close();
    SdlAudioStatus status() const;

private:
    SdlAudioOutput() = default;

    void set_error_locked(const std::string& operation);

    mutable std::mutex mutex_;
    SDL_AudioStream* stream_ = nullptr;
    bool audio_initialized_ = false;
    uint64_t submitted_bytes_ = 0u;
    uint64_t submitted_buffer_count_ = 0u;
    uint64_t dropped_buffer_count_ = 0u;
    uint64_t clear_count_ = 0u;
    uint64_t error_count_ = 0u;
    std::string last_error_;
};

}  // namespace b2r::platform
