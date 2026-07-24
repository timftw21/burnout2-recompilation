#pragma once

#include <cstdint>

#if defined(_WIN32)
#define B2R_AUDIO_EXPORT extern "C" __declspec(dllexport)
#define B2R_AUDIO_CALL __cdecl
#else
#define B2R_AUDIO_EXPORT extern "C"
#define B2R_AUDIO_CALL
#endif

B2R_AUDIO_EXPORT int B2R_AUDIO_CALL b2r_audio_open();
B2R_AUDIO_EXPORT int B2R_AUDIO_CALL b2r_audio_queue(
    const void* payload,
    uint32_t byte_count);
B2R_AUDIO_EXPORT int B2R_AUDIO_CALL b2r_audio_clear();
B2R_AUDIO_EXPORT void B2R_AUDIO_CALL b2r_audio_close();
B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_queued_bytes();
B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_submitted_bytes();
B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_dropped_buffers();
B2R_AUDIO_EXPORT uint64_t B2R_AUDIO_CALL b2r_audio_error_count();
