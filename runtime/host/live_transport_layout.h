#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace b2r::live_transport {

inline constexpr std::array<uint8_t, 8> kLiveControlMagic = {
    'B', '2', 'L', 'I', 'V', '0', '0', '1'};
inline constexpr uint32_t kLiveControlSchemaVersion = 1u;
inline constexpr uint32_t kLiveControlSize = 65536u;
inline constexpr size_t kLiveHotPathProfileStateOffset = 36u;
inline constexpr uint32_t kLiveHotPathProfileDisabled = 0u;
inline constexpr uint32_t kLiveHotPathProfileArmed = 1u;
inline constexpr uint32_t kLiveHotPathProfileActive = 2u;
inline constexpr uint32_t kLiveHotPathProfileComplete = 3u;
inline constexpr size_t kLiveReplayCaptureStateOffset = 40u;
inline constexpr uint32_t kLiveReplayCaptureDisabled = 0u;
inline constexpr uint32_t kLiveReplayCaptureArmed = 1u;
inline constexpr uint32_t kLiveReplayCaptureRequested = 2u;
inline constexpr uint32_t kLiveReplayCaptureComplete = 3u;
inline constexpr size_t kLiveControllerSequenceOffset = 64u;
inline constexpr size_t kLiveControllerPayloadOffset = 68u;
inline constexpr size_t kLivePresentationSequenceOffset = 128u;
inline constexpr size_t kLivePresentationPayloadOffset = 136u;
inline constexpr size_t kLiveAudioSequenceOffset = 8192u;
inline constexpr size_t kLiveAudioAcknowledgedSequenceOffset = 8196u;
inline constexpr size_t kLiveAudioPayloadSizeOffset = 8200u;
inline constexpr size_t kLiveAudioPayloadOffset = 8224u;
inline constexpr size_t kLiveAudioPayloadCapacity = 16u * 1024u;
inline constexpr size_t kLiveManifestSequenceOffset = 256u;
inline constexpr size_t kLiveManifestSizeOffset = 260u;
inline constexpr size_t kLiveManifestPayloadOffset = 264u;
inline constexpr size_t kLiveManifestPayloadCapacity =
    kLiveAudioSequenceOffset - kLiveManifestPayloadOffset;
inline constexpr std::array<uint8_t, 8> kLiveManifestMagic = {
    'B', '2', 'M', 'A', 'N', '0', '0', '1'};
inline constexpr uint8_t kLiveManifestTypeU64 = 1u;
inline constexpr uint8_t kLiveManifestTypeBool = 2u;
inline constexpr uint8_t kLiveManifestTypeUtf8 = 3u;

inline constexpr uint32_t kLiveCommandSize = 64u * 1024u * 1024u;
inline constexpr size_t kLiveCommandDataOffset = 64u;
inline constexpr size_t kLiveCommandWriteCursorOffset = 24u;
inline constexpr size_t kLiveCommandReadCursorOffset = 32u;
inline constexpr std::array<uint8_t, 8> kLiveCommandMagic = {
    'B', '2', 'C', 'M', 'D', '0', '0', '1'};

inline constexpr uint32_t kLiveResourceSize = 64u * 1024u * 1024u;
inline constexpr size_t kLiveResourceHeaderSize = 64u;
inline constexpr std::array<size_t, 2> kLiveResourceMetadataOffsets = {24u, 40u};
inline constexpr std::array<uint8_t, 8> kLiveResourceMagic = {
    'B', '2', 'R', 'E', 'S', '0', '0', '1'};

constexpr bool sequence_is_published(uint32_t sequence) {
    return sequence != 0u && (sequence & 1u) == 0u;
}

constexpr bool sequence_is_stable(uint32_t before, uint32_t after) {
    return before == after && sequence_is_published(after);
}

}  // namespace b2r::live_transport
