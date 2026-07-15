"""Minimal RenderWare PCM extraction and Windows host playback."""

from __future__ import annotations

import io
import array
import ctypes
import queue
import struct
import sys
import threading
import time
import wave
from dataclasses import dataclass


RWS_FILE_ID = 0x809
RWS_FILE_HEADER_ID = 0x80A
RWS_FILE_DATA_ID = 0x80C
RWS_STREAM_ID = 0x802
RWS_STREAM_HEADER_ID = 0x803
RWS_STREAM_DATA_ID = 0x804
RWS_PCM16_CODEC_UUID = 0xD01BD217
RWS_STREAMED_FILE_ID = 0x80D
RWS_STREAMED_HEADER_ID = 0x80E
RWS_STREAMED_DATA_ID = 0x80F

_IMA_INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8) * 2
_IMA_STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
    143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449,
    494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
    1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026,
    4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
)


@dataclass(frozen=True)
class PcmClip:
    sample_rate: int
    channels: int
    bits_per_sample: int
    payload: bytes


@dataclass(frozen=True)
class _QueuedPlayback:
    pcm_payload: bytes
    loop: bool
    sample_rate: int = 48000
    channels: int = 2
    bits_per_sample: int = 16
    gain: float = 1.0


class _WaveFormatEx(ctypes.Structure):
    _pack_ = 2
    _fields_ = (
        ("wFormatTag", ctypes.c_uint16),
        ("nChannels", ctypes.c_uint16),
        ("nSamplesPerSec", ctypes.c_uint32),
        ("nAvgBytesPerSec", ctypes.c_uint32),
        ("nBlockAlign", ctypes.c_uint16),
        ("wBitsPerSample", ctypes.c_uint16),
        ("cbSize", ctypes.c_uint16),
    )


class _WaveHeader(ctypes.Structure):
    _fields_ = (
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint32),
        ("dwBytesRecorded", ctypes.c_uint32),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", ctypes.c_uint32),
        ("dwLoops", ctypes.c_uint32),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    )


class _WaveOutPcmSink:
    """Keep several raw PCM buffers queued in WinMM to avoid boundary gaps."""

    _WHDR_DONE = 0x00000001
    _CALLBACK_NULL = 0
    _WAVE_MAPPER = 0xFFFFFFFF

    def __init__(self, *, buffer_count: int = 4) -> None:
        self._winmm = ctypes.WinDLL("winmm")
        self._handle = ctypes.c_void_p()
        self._pending: list[tuple[ctypes.Array[ctypes.c_char], _WaveHeader]] = []
        self._buffer_count = max(2, buffer_count)
        self._configure_api()
        format_info = _WaveFormatEx(
            1,
            2,
            48000,
            48000 * 2 * 2,
            2 * 2,
            16,
            0,
        )
        self._check(
            self._winmm.waveOutOpen(
                ctypes.byref(self._handle),
                self._WAVE_MAPPER,
                ctypes.byref(format_info),
                0,
                0,
                self._CALLBACK_NULL,
            ),
            "waveOutOpen",
        )

    def _configure_api(self) -> None:
        header_pointer = ctypes.POINTER(_WaveHeader)
        self._winmm.waveOutOpen.argtypes = (
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_uint,
            ctypes.POINTER(_WaveFormatEx),
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_uint32,
        )
        self._winmm.waveOutOpen.restype = ctypes.c_uint
        for name in ("waveOutPrepareHeader", "waveOutWrite", "waveOutUnprepareHeader"):
            function = getattr(self._winmm, name)
            function.argtypes = (ctypes.c_void_p, header_pointer, ctypes.c_uint)
            function.restype = ctypes.c_uint
        for name in ("waveOutReset", "waveOutClose"):
            function = getattr(self._winmm, name)
            function.argtypes = (ctypes.c_void_p,)
            function.restype = ctypes.c_uint

    @staticmethod
    def _check(result: int, operation: str) -> None:
        if result:
            raise RuntimeError(f"{operation} failed with WinMM status {result}")

    def _release_completed(self) -> None:
        retained: list[tuple[ctypes.Array[ctypes.c_char], _WaveHeader]] = []
        for buffer, header in self._pending:
            if header.dwFlags & self._WHDR_DONE:
                self._check(
                    self._winmm.waveOutUnprepareHeader(
                        self._handle, ctypes.byref(header), ctypes.sizeof(header)
                    ),
                    "waveOutUnprepareHeader",
                )
            else:
                retained.append((buffer, header))
        self._pending = retained

    def write(self, payload: bytes) -> None:
        while len(self._pending) >= self._buffer_count:
            self._release_completed()
            if len(self._pending) >= self._buffer_count:
                time.sleep(0.001)
        buffer = ctypes.create_string_buffer(payload)
        header = _WaveHeader(
            ctypes.cast(buffer, ctypes.c_void_p),
            len(payload),
            0,
            0,
            0,
            0,
            None,
            0,
        )
        self._check(
            self._winmm.waveOutPrepareHeader(
                self._handle, ctypes.byref(header), ctypes.sizeof(header)
            ),
            "waveOutPrepareHeader",
        )
        try:
            self._check(
                self._winmm.waveOutWrite(
                    self._handle, ctypes.byref(header), ctypes.sizeof(header)
                ),
                "waveOutWrite",
            )
        except RuntimeError:
            self._winmm.waveOutUnprepareHeader(
                self._handle, ctypes.byref(header), ctypes.sizeof(header)
            )
            raise
        self._pending.append((buffer, header))

    def close(self) -> None:
        if not self._handle:
            return
        self.reset()
        self._winmm.waveOutClose(self._handle)
        self._handle = ctypes.c_void_p()

    def reset(self) -> None:
        if not self._handle:
            return
        self._winmm.waveOutReset(self._handle)
        self._release_completed()
        self._pending.clear()


def parse_rws_pcm(payload: bytes) -> list[PcmClip]:
    """Extract PCM16 clips from the 0x809 RenderWare audio container."""

    def u32(offset: int) -> int:
        if offset < 0 or offset + 4 > len(payload):
            raise ValueError("truncated RWS integer")
        return struct.unpack_from("<I", payload, offset)[0]

    if len(payload) < 0x1C or u32(0) != RWS_FILE_ID:
        raise ValueError("not a RenderWare 0x809 audio stream")
    file_header = 0x0C
    if u32(file_header) != RWS_FILE_HEADER_ID:
        raise ValueError("missing RWS file header")
    file_data = file_header + 0x0C + u32(file_header + 4)
    if u32(file_data) != RWS_FILE_DATA_ID:
        raise ValueError("missing RWS file data chunk")

    stream_count = u32(file_data + 0x0C)
    stream_offset = file_data + 0x10
    clips: list[PcmClip] = []
    for _ in range(stream_count):
        if u32(stream_offset) != RWS_STREAM_ID:
            raise ValueError("missing RWS stream chunk")
        stream_chunk_size = u32(stream_offset + 4)
        stream_header = stream_offset + 0x0C
        if u32(stream_header) != RWS_STREAM_HEADER_ID:
            raise ValueError("missing RWS stream header")
        stream_header_size = u32(stream_header + 4)
        sample_rate = u32(stream_header + 0x10)
        pcm_size = u32(stream_header + 0x18)
        channels = payload[stream_header + 0x1D]
        codec_uuid = u32(stream_header + 0x2C)
        if codec_uuid != RWS_PCM16_CODEC_UUID:
            raise ValueError(f"unsupported RWS codec 0x{codec_uuid:08X}")
        data_chunk = stream_header + stream_header_size + 0x0C
        if u32(data_chunk) != RWS_STREAM_DATA_ID or u32(data_chunk + 4) != pcm_size:
            raise ValueError("invalid RWS stream data chunk")
        pcm_start = data_chunk + 0x0C
        pcm_end = pcm_start + pcm_size
        if pcm_end > len(payload):
            raise ValueError("truncated RWS PCM payload")
        clips.append(PcmClip(sample_rate, channels, 16, payload[pcm_start:pcm_end]))
        stream_offset += stream_chunk_size + 0x0C
    return clips


def parse_rws_xbox_adpcm(payload: bytes, *, channels: int = 2) -> PcmClip:
    """Decode a RenderWare 0x80D streamed Xbox IMA-ADPCM track to PCM16."""

    if channels <= 0:
        raise ValueError("channel count must be positive")
    if len(payload) < 0x24:
        raise ValueError("truncated streamed RWS track")
    file_id, file_size = struct.unpack_from("<II", payload, 0)
    if file_id != RWS_STREAMED_FILE_ID or file_size + 12 > len(payload):
        raise ValueError("not a RenderWare 0x80D streamed audio track")
    header_offset = 12
    header_id, header_size = struct.unpack_from("<II", payload, header_offset)
    if header_id != RWS_STREAMED_HEADER_ID:
        raise ValueError("missing streamed RWS header")
    data_offset = header_offset + 12 + header_size
    if data_offset + 12 > len(payload):
        raise ValueError("missing streamed RWS data")
    data_id, data_size = struct.unpack_from("<II", payload, data_offset)
    if data_id != RWS_STREAMED_DATA_ID or data_offset + 12 + data_size > len(payload):
        raise ValueError("invalid streamed RWS data chunk")

    sample_rate = _find_streamed_sample_rate(payload[header_offset:data_offset])
    encoded = _strip_stream_packet_headers(
        payload[data_offset + 12:data_offset + 12 + data_size]
    )
    block_align = 36 * channels
    if len(encoded) % block_align:
        raise ValueError("streamed Xbox ADPCM payload is not block-aligned")

    block_count = len(encoded) // block_align
    output = bytearray(block_count * 64 * channels * 2)
    output_offset = 0
    packed_block = struct.Struct(f"<{64 * channels}h")
    for block_offset in range(0, len(encoded), block_align):
        channel_samples = []
        for channel in range(channels):
            channel_block = bytearray(
                encoded[block_offset + channel * 4:block_offset + channel * 4 + 4]
            )
            for group in range(8):
                group_offset = block_offset + channels * 4 + group * channels * 4
                channel_block.extend(
                    encoded[group_offset + channel * 4:group_offset + channel * 4 + 4]
                )
            channel_samples.append(_decode_xbox_adpcm_block(bytes(channel_block)))
        interleaved = [
            channel_samples[channel][sample_index]
            for sample_index in range(64)
            for channel in range(channels)
        ]
        packed_block.pack_into(output, output_offset, *interleaved)
        output_offset += packed_block.size
    return PcmClip(sample_rate, channels, 16, bytes(output))


def _strip_stream_packet_headers(payload: bytes) -> bytes:
    """Remove the observed 48-byte headers between 0x10800-byte RWS packets."""

    first_audio_size = 0x10818
    packet_header_size = 0x30
    later_audio_size = 0x107D0
    if len(payload) <= first_audio_size:
        return payload
    output = bytearray(payload[:first_audio_size])
    offset = first_audio_size
    while offset < len(payload):
        offset += packet_header_size
        if offset >= len(payload):
            break
        audio_end = min(len(payload), offset + later_audio_size)
        output.extend(payload[offset:audio_end])
        offset = audio_end
    # A terminal packet header can leave no audio or a partial ADPCM block.
    return bytes(output[:len(output) - (len(output) % 72)])


def _find_streamed_sample_rate(header: bytes) -> int:
    for sample_rate in (48000, 44100, 32000, 22050):
        if struct.pack("<I", sample_rate) in header:
            return sample_rate
    raise ValueError("streamed RWS sample rate was not found")


def _decode_xbox_adpcm_block(block: bytes) -> list[int]:
    if len(block) != 36:
        raise ValueError("Xbox ADPCM channel block must be 36 bytes")
    predictor, step_index = struct.unpack_from("<hB", block, 0)
    if step_index >= len(_IMA_STEP_TABLE):
        raise ValueError("invalid Xbox ADPCM step index")
    samples = [predictor]
    for packed in block[4:]:
        for nibble in (packed & 0x0F, packed >> 4):
            if len(samples) == 64:
                return samples
            step = _IMA_STEP_TABLE[step_index]
            difference = step >> 3
            if nibble & 1:
                difference += step >> 2
            if nibble & 2:
                difference += step >> 1
            if nibble & 4:
                difference += step
            predictor += -difference if nibble & 8 else difference
            predictor = max(-32768, min(32767, predictor))
            step_index = max(0, min(88, step_index + _IMA_INDEX_TABLE[nibble]))
            samples.append(predictor)
    if len(samples) != 64:
        raise ValueError("truncated Xbox ADPCM block")
    return samples


def pcm_wave_bytes(clip: PcmClip) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(clip.channels)
        writer.setsampwidth(clip.bits_per_sample // 8)
        writer.setframerate(clip.sample_rate)
        writer.writeframes(clip.payload)
    return output.getvalue()


def scale_pcm_volume(payload: bytes, *, bits_per_sample: int, gain: float) -> bytes:
    """Apply an application-local master gain without changing Windows volume."""
    gain = max(0.0, min(1.0, float(gain)))
    if gain == 1.0 or not payload:
        return bytes(payload)
    if bits_per_sample == 8:
        return bytes(
            max(0, min(255, 128 + int((sample - 128) * gain)))
            for sample in payload
        )
    if bits_per_sample != 16 or len(payload) % 2:
        raise ValueError("host volume scaling requires aligned 8-bit or 16-bit PCM")
    samples = array.array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    for index, sample in enumerate(samples):
        samples[index] = int(sample * gain)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class WindowsPcmOutput:
    """Queue PCM clips to the native Windows waveform output off the guest thread."""

    def __init__(self, *, queue_depth: int = 16, master_volume: float = 0.5) -> None:
        self.available = sys.platform == "win32"
        self.master_volume = max(0.0, min(1.0, float(master_volume)))
        self.submitted_buffer_count = 0
        self.dropped_buffer_count = 0
        self.error_count = 0
        self.looping = False
        self._queue: queue.Queue[_QueuedPlayback] = queue.Queue(maxsize=max(1, queue_depth))
        self._sink: _WaveOutPcmSink | None = None
        if not self.available:
            return
        try:
            self._sink = _WaveOutPcmSink()
        except (OSError, RuntimeError):
            self.available = False
            self.error_count += 1
            return
        threading.Thread(
            target=self._worker,
            name="b2-recomp-audio",
            daemon=True,
        ).start()

    def submit_pcm(
        self,
        payload: bytes,
        *,
        sample_rate: int,
        channels: int,
        bits_per_sample: int,
        loop: bool = False,
    ) -> bool:
        if not self.available:
            return False
        if bits_per_sample != 16 or channels not in {1, 2} or sample_rate <= 0:
            raise ValueError(
                "host mixer requires mono/stereo PCM16 with a valid sample rate"
            )
        request = _QueuedPlayback(
            bytes(payload),
            loop,
            sample_rate,
            channels,
            bits_per_sample,
            getattr(self, "master_volume", 0.5),
        )
        try:
            self._queue.put_nowait(request)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self.dropped_buffer_count += 1
            self._queue.put_nowait(request)
        self.submitted_buffer_count += 1
        return True

    def stop(self) -> None:
        if not self.available:
            return
        try:
            self._queue.put_nowait(_QueuedPlayback(b"", False))
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(_QueuedPlayback(b"", False))

    def _worker(self) -> None:
        loop_payload = b""
        loop_cursor = 0
        one_shots: list[tuple[bytes, int]] = []
        chunk_samples = 2400 * 2  # 50 ms at 48 kHz stereo.
        while True:
            if not loop_payload and not one_shots:
                request = self._queue.get()
                prepared_payload = _prepare_queued_playback(request)
                if request.loop:
                    loop_payload = prepared_payload
                    loop_cursor = 0
                elif prepared_payload:
                    one_shots.append((prepared_payload, 0))
                else:
                    self.looping = False
                    if self._sink is not None:
                        self._sink.reset()
                    continue
            while True:
                try:
                    request = self._queue.get_nowait()
                except queue.Empty:
                    break
                if not request.pcm_payload:
                    loop_payload = b""
                    loop_cursor = 0
                    one_shots.clear()
                    if self._sink is not None:
                        self._sink.reset()
                elif request.loop:
                    loop_payload = _prepare_queued_playback(request)
                    loop_cursor = 0
                else:
                    one_shots.append((_prepare_queued_playback(request), 0))

            mixed = [0] * chunk_samples
            if loop_payload:
                for index in range(chunk_samples):
                    if loop_cursor >= len(loop_payload):
                        loop_cursor = 0
                    mixed[index] = struct.unpack_from("<h", loop_payload, loop_cursor)[0]
                    loop_cursor += 2

            remaining_shots: list[tuple[bytes, int]] = []
            for payload, cursor in one_shots:
                for index in range(chunk_samples):
                    if cursor >= len(payload):
                        break
                    sample = struct.unpack_from("<h", payload, cursor)[0]
                    mixed[index] = max(-32768, min(32767, mixed[index] + sample))
                    cursor += 2
                if cursor < len(payload):
                    remaining_shots.append((payload, cursor))
            one_shots = remaining_shots
            self.looping = bool(loop_payload)

            chunk_payload = struct.pack(f"<{chunk_samples}h", *mixed)
            try:
                assert self._sink is not None
                self._sink.write(chunk_payload)
            except RuntimeError:
                self.error_count += 1
                time.sleep(0.05)

    def summary(self) -> dict[str, int | float | bool | str]:
        return {
            "backend": "winmm_waveout",
            "available": self.available,
            "master_volume": self.master_volume,
            "submitted_buffer_count": self.submitted_buffer_count,
            "dropped_buffer_count": self.dropped_buffer_count,
            "error_count": self.error_count,
            "looping": self.looping,
        }


def _convert_pcm16_to_stereo_48k(
    payload: bytes,
    *,
    sample_rate: int,
    channels: int,
    bits_per_sample: int,
) -> bytes:
    """Normalize host clips for low-latency music/SFX software mixing."""

    if bits_per_sample != 16 or channels not in {1, 2} or sample_rate <= 0:
        raise ValueError("host mixer requires mono/stereo PCM16 with a valid sample rate")
    if sample_rate == 48000 and channels == 2 and len(payload) % 4 == 0:
        return bytes(payload)
    samples = array.array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    frame_count = len(samples) // channels
    output_frame_count = round(frame_count * 48000 / sample_rate)
    output = array.array("h")
    for output_frame in range(output_frame_count):
        source_frame = min(frame_count - 1, output_frame * sample_rate // 48000)
        left = samples[source_frame * channels]
        right = samples[source_frame * channels + 1] if channels == 2 else left
        output.extend((left, right))
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


def _prepare_queued_playback(request: _QueuedPlayback) -> bytes:
    if not request.pcm_payload:
        return b""
    scaled_payload = scale_pcm_volume(
        request.pcm_payload,
        bits_per_sample=request.bits_per_sample,
        gain=request.gain,
    )
    return _convert_pcm16_to_stereo_48k(
        scaled_payload,
        sample_rate=request.sample_rate,
        channels=request.channels,
        bits_per_sample=request.bits_per_sample,
    )
