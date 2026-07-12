from __future__ import annotations

import struct
import queue
import unittest

from tools.playability.host_audio import (
    PcmClip,
    RWS_PCM16_CODEC_UUID,
    WindowsPcmOutput,
    parse_rws_pcm,
    parse_rws_xbox_adpcm,
    pcm_wave_bytes,
    scale_pcm_volume,
)


def _rws_pcm_fixture(pcm: bytes, *, sample_rate: int = 22050) -> bytes:
    file_header_data = bytearray(0x44)
    file_header = struct.pack("<III", 0x80A, len(file_header_data), 0x1003FFFF)
    file_header += file_header_data

    stream_header = bytearray(0x0C + 0xA0)
    struct.pack_into("<III", stream_header, 0, 0x803, 0xA0, 0x1003FFFF)
    struct.pack_into("<I", stream_header, 0x10, sample_rate)
    struct.pack_into("<I", stream_header, 0x18, len(pcm))
    stream_header[0x1D] = 1
    struct.pack_into("<I", stream_header, 0x2C, RWS_PCM16_CODEC_UUID)
    stream_data = struct.pack("<III", 0x804, len(pcm), 0x1003FFFF) + pcm
    stream_payload = bytes(stream_header) + stream_data
    stream = struct.pack("<III", 0x802, len(stream_payload), 0x1003FFFF)
    stream += stream_payload
    file_data_payload = struct.pack("<I", 1) + stream
    file_data = struct.pack("<III", 0x80C, len(file_data_payload), 0x1003FFFF)
    file_data += file_data_payload
    body = file_header + file_data
    return struct.pack("<III", 0x809, len(body), 0x1003FFFF) + body


class HostAudioTests(unittest.TestCase):
    def test_master_volume_scales_pcm_without_changing_system_volume(self) -> None:
        pcm = struct.pack("<hhhh", -32768, -1000, 1000, 32767)

        scaled = scale_pcm_volume(pcm, bits_per_sample=16, gain=0.5)

        self.assertEqual(
            struct.unpack("<hhhh", scaled),
            (-16384, -500, 500, 16383),
        )

    def test_parse_renderware_809_pcm_clip(self) -> None:
        pcm = struct.pack("<hhhh", -32768, -1, 0, 32767)
        clips = parse_rws_pcm(_rws_pcm_fixture(pcm, sample_rate=22050))

        self.assertEqual(clips, [PcmClip(22050, 1, 16, pcm)])

    def test_pcm_wave_bytes_builds_native_wave_payload(self) -> None:
        pcm = struct.pack("<hh", -123, 456)
        payload = pcm_wave_bytes(PcmClip(44100, 1, 16, pcm))

        self.assertEqual(payload[:4], b"RIFF")
        self.assertEqual(payload[8:12], b"WAVE")
        self.assertIn(pcm, payload)

    def test_parse_renderware_80d_streamed_xbox_adpcm(self) -> None:
        header_payload = bytearray(0x20)
        struct.pack_into("<I", header_payload, 0x10, 48000)
        header = struct.pack("<III", 0x80E, len(header_payload), 0x1003FFFF)
        header += header_payload
        channel_header = struct.pack("<hBB", 0, 0, 0)
        encoded = channel_header * 2 + bytes(64)
        data = struct.pack("<III", 0x80F, len(encoded), 0x1003FFFF) + encoded
        body = header + data
        payload = struct.pack("<III", 0x80D, len(body), 0x1003FFFF) + body

        clip = parse_rws_xbox_adpcm(payload)

        self.assertEqual(clip.sample_rate, 48000)
        self.assertEqual(clip.channels, 2)
        self.assertEqual(clip.bits_per_sample, 16)
        self.assertEqual(clip.payload, bytes(64 * 2 * 2))

    def test_windows_pcm_output_preserves_loop_request(self) -> None:
        output = WindowsPcmOutput.__new__(WindowsPcmOutput)
        output.available = True
        output.submitted_buffer_count = 0
        output.dropped_buffer_count = 0
        output.error_count = 0
        output.looping = False
        output.master_volume = 0.5
        output._queue = queue.Queue(maxsize=2)

        submitted = output.submit_pcm(
            struct.pack("<hh", -1, 1),
            sample_rate=48000,
            channels=1,
            bits_per_sample=16,
            loop=True,
        )

        self.assertTrue(submitted)
        request = output._queue.get_nowait()
        self.assertTrue(request.loop)
        self.assertTrue(request.wave_payload.startswith(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
