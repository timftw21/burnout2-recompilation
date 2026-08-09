from __future__ import annotations

import queue
import struct
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.playability import host_audio
from tools.playability.host_audio import (
    PcmClip,
    RWS_PCM16_CODEC_UUID,
    SdlPcmOutput,
    _convert_pcm16_to_stereo_48k,
    _prepare_queued_playback,
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
    def test_host_audio_uses_sdl3_bridge_instead_of_winmm(self) -> None:
        host_source = Path("tools/playability/host_audio.py").read_text(
            encoding="utf-8"
        )
        native_source = Path("runtime/platform/sdl/sdl_audio.cpp").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("WinDLL", host_source)
        self.assertNotIn("waveOut", host_source)
        self.assertIn("b2r_audio_queue", host_source)
        self.assertIn("SDL_OpenAudioDeviceStream", native_source)
        self.assertIn("SDL_PutAudioStreamData", native_source)
        self.assertIn("SDL_ClearAudioStream", native_source)
        self.assertIn('last_error_ = "SDL audio queue is full"', native_source)
        self.assertNotIn("kQueueWaitLimit", native_source)
        self.assertNotIn("sleep_for", native_source)

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

    def test_pcm_conversion_linearly_interpolates_between_source_frames(
        self,
    ) -> None:
        converted = _convert_pcm16_to_stereo_48k(
            struct.pack("<hhh", 0, 1000, 2000),
            sample_rate=24000,
            channels=1,
            bits_per_sample=16,
        )

        self.assertEqual(
            struct.unpack("<12h", converted),
            (0, 0, 500, 500, 1000, 1000, 1500, 1500, 2000, 2000, 2000, 2000),
        )

    def test_parse_renderware_80d_streamed_xbox_adpcm(self) -> None:
        header_payload = bytearray(0xE8)
        struct.pack_into("<I", header_payload, 0x40 - 0x18, 1)
        struct.pack_into("<I", header_payload, 0x4C - 0x18, 0xA0)
        struct.pack_into("<I", header_payload, 0x90 - 0x18, 0x140)
        struct.pack_into("<I", header_payload, 0x98 - 0x18, 72 * 2)
        struct.pack_into(
            "<7I",
            header_payload,
            0xC8 - 0x18,
            7,
            0xA0,
            0,
            0x00040004,
            0,
            72,
            0,
        )
        struct.pack_into("<I", header_payload, 0xE4 - 0x18, 48000)
        header = struct.pack("<III", 0x80E, len(header_payload), 0x1003FFFF)
        header += header_payload
        channel_header = struct.pack("<hBB", 0, 0, 0)
        encoded = channel_header * 2 + bytes(64)
        packets = (encoded + bytes(0xA0 - len(encoded))) * 2
        data = struct.pack("<III", 0x80F, len(packets), 0x1003FFFF) + packets
        body = header + data
        payload = struct.pack("<III", 0x80D, len(body), 0x1003FFFF) + body

        clip = parse_rws_xbox_adpcm(payload)

        self.assertEqual(clip.sample_rate, 48000)
        self.assertEqual(clip.channels, 2)
        self.assertEqual(clip.bits_per_sample, 16)
        self.assertEqual(clip.payload, bytes(64 * 2 * 2 * 2))

    def test_sdl_pcm_output_preserves_loop_request(self) -> None:
        output = SdlPcmOutput.__new__(SdlPcmOutput)
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
        self.assertEqual(
            struct.unpack("<hhhh", _prepare_queued_playback(request)),
            (0, 0, 0, 0),
        )
        self.assertEqual(output.summary()["backend"], "sdl3_audio_stream")

    def test_sdl_pcm_output_closes_worker_before_native_audio(self) -> None:
        output = SdlPcmOutput.__new__(SdlPcmOutput)
        output.available = True
        output.looping = True
        output._closed = False
        output._atexit_registered = True
        output._shutdown = threading.Event()
        output._queue = queue.Queue(maxsize=1)
        lifecycle: list[str] = []
        worker = Mock()
        worker.join.side_effect = lambda: lifecycle.append("worker")
        sink = Mock()
        sink.close.side_effect = lambda: lifecycle.append("sink")
        output._worker_thread = worker
        output._sink = sink

        with patch.object(host_audio.atexit, "unregister") as unregister:
            output.close()

        self.assertTrue(output._shutdown.is_set())
        worker.join.assert_called_once_with()
        sink.close.assert_called_once_with()
        self.assertEqual(lifecycle, ["worker", "sink"])
        self.assertIsNone(output._sink)
        unregister.assert_called_once()
        self.assertFalse(output.available)
        self.assertFalse(output.looping)

    def test_sdl_audio_sink_close_is_idempotent(self) -> None:
        sink = host_audio._SdlAudioSink.__new__(host_audio._SdlAudioSink)
        sink._closed = False
        sink._close_audio = Mock()

        sink.close()
        sink.close()

        sink._close_audio.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
