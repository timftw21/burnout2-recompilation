#define SDL_MAIN_HANDLED

#include "sdl_audio.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>

#include <limits>

namespace b2r::platform {
namespace {

constexpr int kSampleRate = 48000;
constexpr int kChannelCount = 2;
constexpr int kBytesPerSample = 2;
constexpr int kMaximumQueuedBytes =
    kSampleRate * kChannelCount * kBytesPerSample / 4;

}  // namespace

SdlAudioOutput::~SdlAudioOutput() {
    close();
}

SdlAudioOutput& SdlAudioOutput::instance() {
    static SdlAudioOutput output;
    return output;
}

bool SdlAudioOutput::open() {
    std::lock_guard lock(mutex_);
    if (stream_ != nullptr) {
        return true;
    }

    SDL_SetMainReady();
    if (!SDL_InitSubSystem(SDL_INIT_AUDIO)) {
        set_error_locked("SDL audio initialization");
        return false;
    }
    audio_initialized_ = true;

    const SDL_AudioSpec source_spec{
        SDL_AUDIO_S16,
        kChannelCount,
        kSampleRate,
    };
    stream_ = SDL_OpenAudioDeviceStream(
        SDL_AUDIO_DEVICE_DEFAULT_PLAYBACK,
        &source_spec,
        nullptr,
        nullptr);
    if (stream_ == nullptr) {
        set_error_locked("SDL audio stream open");
        SDL_QuitSubSystem(SDL_INIT_AUDIO);
        audio_initialized_ = false;
        return false;
    }
    if (!SDL_ResumeAudioStreamDevice(stream_)) {
        set_error_locked("SDL audio stream resume");
        SDL_DestroyAudioStream(stream_);
        stream_ = nullptr;
        SDL_QuitSubSystem(SDL_INIT_AUDIO);
        audio_initialized_ = false;
        return false;
    }
    last_error_.clear();
    return true;
}

bool SdlAudioOutput::queue(const void* payload, size_t byte_count) {
    if (payload == nullptr || byte_count == 0u
        || byte_count % (kChannelCount * kBytesPerSample) != 0u
        || byte_count > static_cast<size_t>(std::numeric_limits<int>::max())
        || byte_count > static_cast<size_t>(kMaximumQueuedBytes)) {
        std::lock_guard lock(mutex_);
        ++error_count_;
        last_error_ = "invalid SDL audio payload";
        return false;
    }

    std::lock_guard lock(mutex_);
    if (stream_ == nullptr) {
        ++error_count_;
        last_error_ = "SDL audio stream is not open";
        return false;
    }
    const int queued_bytes = SDL_GetAudioStreamQueued(stream_);
    if (queued_bytes < 0) {
        set_error_locked("SDL audio queue query");
        return false;
    }
    // The mixer owns sample-clock pacing. Never turn host audio backpressure
    // into presentation-thread pacing when the device queue is temporarily full.
    if (queued_bytes + static_cast<int>(byte_count) > kMaximumQueuedBytes) {
        ++dropped_buffer_count_;
        last_error_ = "SDL audio queue is full";
        return false;
    }
    if (!SDL_PutAudioStreamData(
            stream_, payload, static_cast<int>(byte_count))) {
        set_error_locked("SDL audio queue submission");
        return false;
    }
    submitted_bytes_ += byte_count;
    ++submitted_buffer_count_;
    last_error_.clear();
    return true;
}

bool SdlAudioOutput::clear() {
    std::lock_guard lock(mutex_);
    if (stream_ == nullptr) {
        return true;
    }
    if (!SDL_ClearAudioStream(stream_)) {
        set_error_locked("SDL audio queue clear");
        return false;
    }
    ++clear_count_;
    last_error_.clear();
    return true;
}

void SdlAudioOutput::close() {
    std::lock_guard lock(mutex_);
    if (stream_ != nullptr) {
        SDL_DestroyAudioStream(stream_);
        stream_ = nullptr;
    }
    if (audio_initialized_) {
        SDL_QuitSubSystem(SDL_INIT_AUDIO);
        audio_initialized_ = false;
    }
}

SdlAudioStatus SdlAudioOutput::status() const {
    std::lock_guard lock(mutex_);
    int queued_bytes = 0;
    if (stream_ != nullptr) {
        queued_bytes = SDL_GetAudioStreamQueued(stream_);
        if (queued_bytes < 0) {
            queued_bytes = 0;
        }
    }
    return {
        stream_ != nullptr,
        queued_bytes,
        submitted_bytes_,
        submitted_buffer_count_,
        dropped_buffer_count_,
        clear_count_,
        error_count_,
        last_error_,
    };
}

void SdlAudioOutput::set_error_locked(const std::string& operation) {
    ++error_count_;
    last_error_ = operation + " failed: " + SDL_GetError();
}

}  // namespace b2r::platform
