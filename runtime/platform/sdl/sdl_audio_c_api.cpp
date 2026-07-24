#include "sdl_audio_c_api.h"

#include "sdl_audio.h"

B2R_AUDIO_EXPORT int B2R_AUDIO_CALL b2r_audio_open() {
    return b2r::platform::SdlAudioOutput::instance().open() ? 1 : 0;
}

B2R_AUDIO_EXPORT int B2R_AUDIO_CALL b2r_audio_queue(
    const void* payload,
    uint32_t byte_count) {
    return b2r::platform::SdlAudioOutput::instance().queue(
        payload, byte_count) ? 1 : 0;
}

B2R_AUDIO_EXPORT int B2R_AUDIO_CALL b2r_audio_clear() {
    return b2r::platform::SdlAudioOutput::instance().clear() ? 1 : 0;
}

B2R_AUDIO_EXPORT void B2R_AUDIO_CALL b2r_audio_close() {
    b2r::platform::SdlAudioOutput::instance().close();
}

B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_queued_bytes() {
    return static_cast<uint64_t>(
        b2r::platform::SdlAudioOutput::instance().status().queued_bytes);
}

B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_submitted_bytes() {
    return b2r::platform::SdlAudioOutput::instance().status().submitted_bytes;
}

B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_dropped_buffers() {
    return b2r::platform::SdlAudioOutput::instance().status().dropped_buffer_count;
}

B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_error_count() {
    return b2r::platform::SdlAudioOutput::instance().status().error_count;
}
