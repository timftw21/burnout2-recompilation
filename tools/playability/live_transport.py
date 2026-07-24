"""Versioned fixed-layout control transport for live guest/presenter runs."""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass


LIVE_CONTROL_MAGIC = b"B2LIV001"
LIVE_CONTROL_SCHEMA_VERSION = 1
LIVE_CONTROL_SIZE = 65536
LIVE_COMMAND_SIZE = 64 * 1024 * 1024
LIVE_RESOURCE_SIZE = 64 * 1024 * 1024

_HEADER = struct.Struct("<8sII")
_STOP_REQUESTED_OFFSET = 32
_CONTROLLER_SEQUENCE_OFFSET = 64
_CONTROLLER_PAYLOAD_OFFSET = 68
_CONTROLLER_PAYLOAD = struct.Struct("<HBBhhhhBBH")
_PRESENTATION_SEQUENCE_OFFSET = 128
_PRESENTATION_PAYLOAD_OFFSET = 136
_PRESENTATION_PAYLOAD = struct.Struct("<QQ")
_MANIFEST_SEQUENCE_OFFSET = 256
_MANIFEST_SIZE_OFFSET = 260
_MANIFEST_PAYLOAD_OFFSET = 264
_MANIFEST_MAGIC = b"B2MAN001"
_MANIFEST_HEADER = struct.Struct("<8sII")
_MANIFEST_RECORD = struct.Struct("<HBBI")
_MANIFEST_TYPE_U64 = 1
_MANIFEST_TYPE_BOOL = 2
_MANIFEST_TYPE_UTF8 = 3
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")

_COMMAND_MAGIC = b"B2CMD001"
_COMMAND_HEADER = struct.Struct("<8sIIQQQ")
_COMMAND_DATA_OFFSET = 64
_COMMAND_WRITE_CURSOR_OFFSET = 24
_COMMAND_READ_CURSOR_OFFSET = 32

_RESOURCE_MAGIC = b"B2RES001"
_RESOURCE_HEADER = struct.Struct("<8sIIQ")
_RESOURCE_HEADER_SIZE = 64
_RESOURCE_SLOT_METADATA_OFFSET = (24, 40)
_RESOURCE_SLOT_METADATA = struct.Struct("<IIQ")

_MANIFEST_FIELD_IDS = {
    field: index
    for index, field in enumerate(
        (
            "resource_snapshots_unchanged",
            "resource_snapshot_path",
            "command_snapshot_path",
            "frontend_text",
            "frontend_text_x_bits",
            "frontend_text_y_bits",
            "frontend_text_size_bits",
            "frontend_text_color_argb",
            "interpreter_bootstrap_path",
            "command_snapshot_record_count",
            "lossless_flip_audit",
            "audit_flip_index",
            "audit_guest_steps",
            "audit_flip_value",
            "audit_command_record_count",
            "audit_health_reason",
            "guest_flip_count",
            "guest_steps",
            "guest_compiled_blocks",
            "guest_invalidations",
            "presentable_command_record_count",
            "published_command_record_count",
            "command_snapshot_base_record_count",
            "command_transport_format",
            "presentable_command_byte_count",
            "command_snapshot_base_byte_count",
            "resource_stream_generation",
            "command_stream_generation",
            "presentation_ack_path",
            "presentation_event_name",
            "publication_event_name",
            "resource_transport_slot",
            "resource_transport_size",
            "resource_transport_format",
        ),
        start=1,
    )
}


class LiveControlTransportError(RuntimeError):
    """The named live control mapping is unavailable or incompatible."""


@dataclass(frozen=True)
class LiveControllerSnapshot:
    sequence: int
    connected: bool
    buttons: int
    left_trigger: int
    right_trigger: int
    thumb_lx: int
    thumb_ly: int
    thumb_rx: int
    thumb_ry: int
    host_connected: bool


class LiveControlTransport:
    """Own or open the fixed binary mapping shared by launcher, guest, and host."""

    def __init__(self, mapping: mmap.mmap, name: str) -> None:
        self._mapping = mapping
        self.name = name

    @classmethod
    def create(cls, name: str) -> "LiveControlTransport":
        mapping = cls._map(name)
        mapping[:] = bytes(LIVE_CONTROL_SIZE)
        _HEADER.pack_into(
            mapping,
            0,
            LIVE_CONTROL_MAGIC,
            LIVE_CONTROL_SCHEMA_VERSION,
            LIVE_CONTROL_SIZE,
        )
        return cls(mapping, name)

    @classmethod
    def open(cls, name: str) -> "LiveControlTransport":
        mapping = cls._map(name)
        header = _HEADER.unpack_from(mapping, 0)
        expected = (
            LIVE_CONTROL_MAGIC,
            LIVE_CONTROL_SCHEMA_VERSION,
            LIVE_CONTROL_SIZE,
        )
        if header != expected:
            mapping.close()
            raise LiveControlTransportError(
                f"incompatible live control transport {name!r}: {header!r}"
            )
        return cls(mapping, name)

    @staticmethod
    def _map(name: str) -> mmap.mmap:
        return LiveControlTransport._map_named(name, LIVE_CONTROL_SIZE)

    @staticmethod
    def _map_named(name: str, size: int) -> mmap.mmap:
        if os.name != "nt":
            raise LiveControlTransportError(
                "named live control mappings require Windows"
            )
        try:
            return mmap.mmap(
                -1,
                size,
                tagname=name,
                access=mmap.ACCESS_WRITE,
            )
        except OSError as exc:
            raise LiveControlTransportError(
                f"cannot open live control transport {name!r}: {exc}"
            ) from exc

    def request_stop(self) -> None:
        _U32.pack_into(self._mapping, _STOP_REQUESTED_OFFSET, 1)

    def stop_requested(self) -> bool:
        return bool(_U32.unpack_from(self._mapping, _STOP_REQUESTED_OFFSET)[0])

    def read_controller(
        self,
        last_sequence: int,
    ) -> LiveControllerSnapshot | None:
        sequence = _U32.unpack_from(
            self._mapping,
            _CONTROLLER_SEQUENCE_OFFSET,
        )[0]
        if sequence == 0 or sequence == last_sequence or sequence & 1:
            return None
        payload = _CONTROLLER_PAYLOAD.unpack_from(
            self._mapping,
            _CONTROLLER_PAYLOAD_OFFSET,
        )
        confirmed = _U32.unpack_from(
            self._mapping,
            _CONTROLLER_SEQUENCE_OFFSET,
        )[0]
        if confirmed != sequence or confirmed & 1:
            return None
        (
            buttons,
            left_trigger,
            right_trigger,
            thumb_lx,
            thumb_ly,
            thumb_rx,
            thumb_ry,
            connected,
            host_connected,
            _reserved,
        ) = payload
        return LiveControllerSnapshot(
            sequence=sequence,
            connected=bool(connected),
            buttons=buttons,
            left_trigger=left_trigger,
            right_trigger=right_trigger,
            thumb_lx=thumb_lx,
            thumb_ly=thumb_ly,
            thumb_rx=thumb_rx,
            thumb_ry=thumb_ry,
            host_connected=bool(host_connected),
        )

    def read_presentation_ack(self) -> tuple[int, int] | None:
        sequence = _U32.unpack_from(
            self._mapping,
            _PRESENTATION_SEQUENCE_OFFSET,
        )[0]
        if sequence == 0 or sequence & 1:
            return None
        payload = _PRESENTATION_PAYLOAD.unpack_from(
            self._mapping,
            _PRESENTATION_PAYLOAD_OFFSET,
        )
        confirmed = _U32.unpack_from(
            self._mapping,
            _PRESENTATION_SEQUENCE_OFFSET,
        )[0]
        return payload if confirmed == sequence and not confirmed & 1 else None

    def publish_manifest(self, manifest: dict[str, object]) -> None:
        records = bytearray()
        record_count = 0
        for field, field_id in _MANIFEST_FIELD_IDS.items():
            value = manifest.get(field)
            if value is None:
                continue
            if isinstance(value, bool):
                kind = _MANIFEST_TYPE_BOOL
                encoded = bytes((int(value),))
            elif isinstance(value, int):
                if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
                    raise LiveControlTransportError(
                        f"manifest field {field!r} is outside uint64 range"
                    )
                kind = _MANIFEST_TYPE_U64
                encoded = struct.pack("<Q", value)
            elif isinstance(value, str):
                kind = _MANIFEST_TYPE_UTF8
                encoded = value.encode("utf-8")
            else:
                continue
            records.extend(
                _MANIFEST_RECORD.pack(field_id, kind, 0, len(encoded))
            )
            records.extend(encoded)
            record_count += 1
        payload = bytearray(
            _MANIFEST_HEADER.pack(
                _MANIFEST_MAGIC,
                LIVE_CONTROL_SCHEMA_VERSION,
                record_count,
            )
        )
        payload.extend(records)
        capacity = LIVE_CONTROL_SIZE - _MANIFEST_PAYLOAD_OFFSET
        if len(payload) > capacity:
            raise LiveControlTransportError(
                f"live manifest is too large: {len(payload)}/{capacity} bytes"
            )
        sequence = _U32.unpack_from(
            self._mapping,
            _MANIFEST_SEQUENCE_OFFSET,
        )[0]
        publishing = ((sequence + 1) | 1) & 0xFFFFFFFF
        published = (publishing + 1) & 0xFFFFFFFF
        _U32.pack_into(self._mapping, _MANIFEST_SEQUENCE_OFFSET, publishing)
        _U32.pack_into(self._mapping, _MANIFEST_SIZE_OFFSET, len(payload))
        self._mapping[
            _MANIFEST_PAYLOAD_OFFSET : _MANIFEST_PAYLOAD_OFFSET + len(payload)
        ] = payload
        _U32.pack_into(self._mapping, _MANIFEST_SEQUENCE_OFFSET, published)

    def manifest_available(self) -> bool:
        sequence = _U32.unpack_from(
            self._mapping,
            _MANIFEST_SEQUENCE_OFFSET,
        )[0]
        if sequence == 0 or sequence & 1:
            return False
        payload_size = _U32.unpack_from(
            self._mapping,
            _MANIFEST_SIZE_OFFSET,
        )[0]
        confirmed = _U32.unpack_from(
            self._mapping,
            _MANIFEST_SEQUENCE_OFFSET,
        )[0]
        return (
            confirmed == sequence
            and not confirmed & 1
            and _MANIFEST_HEADER.size <= payload_size
            <= LIVE_CONTROL_SIZE - _MANIFEST_PAYLOAD_OFFSET
        )

    def close(self) -> None:
        self._mapping.close()

    def __enter__(self) -> "LiveControlTransport":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class LiveCommandTransport:
    """Bounded SPSC byte ring for append-only normal-run command spans."""

    def __init__(self, mapping: mmap.mmap, name: str) -> None:
        self._mapping = mapping
        self.name = name
        self.capacity = LIVE_COMMAND_SIZE - _COMMAND_DATA_OFFSET

    @classmethod
    def create(cls, control_name: str) -> "LiveCommandTransport":
        name = control_name + "_commands"
        mapping = LiveControlTransport._map_named(name, LIVE_COMMAND_SIZE)
        mapping[:_COMMAND_DATA_OFFSET] = bytes(_COMMAND_DATA_OFFSET)
        _COMMAND_HEADER.pack_into(
            mapping,
            0,
            _COMMAND_MAGIC,
            LIVE_CONTROL_SCHEMA_VERSION,
            LIVE_COMMAND_SIZE,
            LIVE_COMMAND_SIZE - _COMMAND_DATA_OFFSET,
            0,
            0,
        )
        return cls(mapping, name)

    @classmethod
    def open(cls, control_name: str) -> "LiveCommandTransport":
        name = control_name + "_commands"
        mapping = LiveControlTransport._map_named(name, LIVE_COMMAND_SIZE)
        header = _COMMAND_HEADER.unpack_from(mapping, 0)
        expected = (
            _COMMAND_MAGIC,
            LIVE_CONTROL_SCHEMA_VERSION,
            LIVE_COMMAND_SIZE,
            LIVE_COMMAND_SIZE - _COMMAND_DATA_OFFSET,
        )
        if header[:4] != expected:
            mapping.close()
            raise LiveControlTransportError(
                f"incompatible live command transport {name!r}: {header!r}"
            )
        return cls(mapping, name)

    def write(self, payload: bytes | bytearray | memoryview) -> int:
        data = memoryview(payload).cast("B")
        write_cursor = _U64.unpack_from(
            self._mapping,
            _COMMAND_WRITE_CURSOR_OFFSET,
        )[0]
        read_cursor = _U64.unpack_from(
            self._mapping,
            _COMMAND_READ_CURSOR_OFFSET,
        )[0]
        if read_cursor > write_cursor:
            raise LiveControlTransportError("live command cursors are inverted")
        if len(data) > self.capacity - (write_cursor - read_cursor):
            raise LiveControlTransportError(
                "live command ring filled before presenter acknowledgement"
            )
        start = write_cursor % self.capacity
        first = min(len(data), self.capacity - start)
        data_offset = _COMMAND_DATA_OFFSET + start
        self._mapping[data_offset : data_offset + first] = data[:first]
        if first < len(data):
            remaining = len(data) - first
            self._mapping[
                _COMMAND_DATA_OFFSET : _COMMAND_DATA_OFFSET + remaining
            ] = data[first:]
        write_cursor += len(data)
        _U64.pack_into(
            self._mapping,
            _COMMAND_WRITE_CURSOR_OFFSET,
            write_cursor,
        )
        return int(write_cursor)

    def cursors(self) -> tuple[int, int]:
        write_cursor = _U64.unpack_from(
            self._mapping,
            _COMMAND_WRITE_CURSOR_OFFSET,
        )[0]
        read_cursor = _U64.unpack_from(
            self._mapping,
            _COMMAND_READ_CURSOR_OFFSET,
        )[0]
        return (
            int(write_cursor),
            int(read_cursor),
        )

    def close(self) -> None:
        self._mapping.close()

    def __enter__(self) -> "LiveCommandTransport":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class LiveResourceTransport:
    """Two immutable fixed slots for handshake-ordered resource generations."""

    def __init__(self, mapping: mmap.mmap, name: str) -> None:
        self._mapping = mapping
        self.name = name
        self.slot_capacity = (LIVE_RESOURCE_SIZE - _RESOURCE_HEADER_SIZE) // 2

    @classmethod
    def create(cls, control_name: str) -> "LiveResourceTransport":
        name = control_name + "_resources"
        mapping = LiveControlTransport._map_named(name, LIVE_RESOURCE_SIZE)
        mapping[:_RESOURCE_HEADER_SIZE] = bytes(_RESOURCE_HEADER_SIZE)
        _RESOURCE_HEADER.pack_into(
            mapping,
            0,
            _RESOURCE_MAGIC,
            LIVE_CONTROL_SCHEMA_VERSION,
            LIVE_RESOURCE_SIZE,
            (LIVE_RESOURCE_SIZE - _RESOURCE_HEADER_SIZE) // 2,
        )
        return cls(mapping, name)

    @classmethod
    def open(cls, control_name: str) -> "LiveResourceTransport":
        name = control_name + "_resources"
        mapping = LiveControlTransport._map_named(name, LIVE_RESOURCE_SIZE)
        header = _RESOURCE_HEADER.unpack_from(mapping, 0)
        expected = (
            _RESOURCE_MAGIC,
            LIVE_CONTROL_SCHEMA_VERSION,
            LIVE_RESOURCE_SIZE,
            (LIVE_RESOURCE_SIZE - _RESOURCE_HEADER_SIZE) // 2,
        )
        if header != expected:
            mapping.close()
            raise LiveControlTransportError(
                f"incompatible live resource transport {name!r}: {header!r}"
            )
        return cls(mapping, name)

    def publish(self, payload: bytes, generation: int) -> int:
        if len(payload) > self.slot_capacity:
            raise LiveControlTransportError(
                f"live resource generation is too large: "
                f"{len(payload)}/{self.slot_capacity} bytes"
            )
        slot = int(generation) & 1
        metadata_offset = _RESOURCE_SLOT_METADATA_OFFSET[slot]
        sequence, _size, _generation = _RESOURCE_SLOT_METADATA.unpack_from(
            self._mapping,
            metadata_offset,
        )
        publishing = ((sequence + 1) | 1) & 0xFFFFFFFF
        published = (publishing + 1) & 0xFFFFFFFF
        _RESOURCE_SLOT_METADATA.pack_into(
            self._mapping,
            metadata_offset,
            publishing,
            len(payload),
            int(generation),
        )
        slot_offset = _RESOURCE_HEADER_SIZE + slot * self.slot_capacity
        self._mapping[slot_offset : slot_offset + len(payload)] = payload
        _RESOURCE_SLOT_METADATA.pack_into(
            self._mapping,
            metadata_offset,
            published,
            len(payload),
            int(generation),
        )
        return slot

    def close(self) -> None:
        self._mapping.close()

    def __enter__(self) -> "LiveResourceTransport":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def live_control_transport_name(run_id: str) -> str:
    """Return a local-session mapping name derived from the run identity."""
    token = "".join(character for character in run_id if character.isalnum())
    if not token:
        raise ValueError("run identity does not contain a mapping-name token")
    return f"Local\\b2_recomp_live_{token}"
