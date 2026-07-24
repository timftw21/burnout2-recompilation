from __future__ import annotations

import os
import struct
import unittest
import uuid

from tools.playability import live_transport


@unittest.skipUnless(os.name == "nt", "named pagefile mappings require Windows")
class LiveControlTransportTests(unittest.TestCase):
    def test_fixed_schema_carries_stop_controller_and_presentation_state(self) -> None:
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))
        with live_transport.LiveControlTransport.create(name) as owner:
            with live_transport.LiveControlTransport.open(name) as guest:
                self.assertFalse(guest.stop_requested())
                owner.request_stop()
                self.assertTrue(guest.stop_requested())

                struct.pack_into(
                    "<HBBhhhhBBH",
                    owner._mapping,
                    live_transport._CONTROLLER_PAYLOAD_OFFSET,
                    0x1010,
                    17,
                    29,
                    -123,
                    456,
                    -789,
                    1024,
                    1,
                    1,
                    0,
                )
                struct.pack_into(
                    "<I",
                    owner._mapping,
                    live_transport._CONTROLLER_SEQUENCE_OFFSET,
                    2,
                )
                controller = guest.read_controller(0)

                self.assertIsNotNone(controller)
                assert controller is not None
                self.assertEqual(controller.buttons, 0x1010)
                self.assertEqual(controller.thumb_lx, -123)
                self.assertEqual(controller.thumb_ry, 1024)
                self.assertTrue(controller.host_connected)
                self.assertIsNone(guest.read_controller(controller.sequence))

                struct.pack_into(
                    "<QQ",
                    owner._mapping,
                    live_transport._PRESENTATION_PAYLOAD_OFFSET,
                    123456789,
                    42,
                )
                struct.pack_into(
                    "<I",
                    owner._mapping,
                    live_transport._PRESENTATION_SEQUENCE_OFFSET,
                    2,
                )

                self.assertEqual(
                    guest.read_presentation_ack(),
                    (123456789, 42),
                )

                self.assertFalse(guest.manifest_available())
                owner.publish_manifest(
                    {
                        "guest_flip_count": 42,
                        "resource_snapshots_unchanged": True,
                        "command_snapshot_path": "commands.bin",
                    }
                )
                self.assertTrue(guest.manifest_available())
                payload_size = struct.unpack_from(
                    "<I",
                    owner._mapping,
                    live_transport._MANIFEST_SIZE_OFFSET,
                )[0]
                payload = owner._mapping[
                    live_transport._MANIFEST_PAYLOAD_OFFSET :
                    live_transport._MANIFEST_PAYLOAD_OFFSET + payload_size
                ]
                self.assertTrue(payload.startswith(b"B2MAN001"))

    def test_payload_transports_wrap_commands_and_publish_resource_slots(self) -> None:
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))
        with live_transport.LiveCommandTransport.create(name) as command_owner:
            with live_transport.LiveCommandTransport.open(name) as command_guest:
                near_end = command_guest.capacity - 2
                struct.pack_into(
                    "<Q",
                    command_owner._mapping,
                    live_transport._COMMAND_WRITE_CURSOR_OFFSET,
                    near_end,
                )
                struct.pack_into(
                    "<Q",
                    command_owner._mapping,
                    live_transport._COMMAND_READ_CURSOR_OFFSET,
                    near_end,
                )

                self.assertEqual(command_guest.write(b"abcd"), near_end + 4)
                self.assertEqual(
                    command_owner._mapping[
                        live_transport.LIVE_COMMAND_SIZE - 2 :
                    ],
                    b"ab",
                )
                self.assertEqual(
                    command_owner._mapping[
                        live_transport._COMMAND_DATA_OFFSET :
                        live_transport._COMMAND_DATA_OFFSET + 2
                    ],
                    b"cd",
                )

        with live_transport.LiveResourceTransport.create(name) as resource_owner:
            with live_transport.LiveResourceTransport.open(name) as resource_guest:
                slot = resource_guest.publish(b"B2TEX001payload", 7)

                self.assertEqual(slot, 1)
                metadata = live_transport._RESOURCE_SLOT_METADATA.unpack_from(
                    resource_owner._mapping,
                    live_transport._RESOURCE_SLOT_METADATA_OFFSET[slot],
                )
                self.assertEqual(metadata[1:], (15, 7))
                slot_offset = (
                    live_transport._RESOURCE_HEADER_SIZE
                    + resource_guest.slot_capacity
                )
                self.assertEqual(
                    resource_owner._mapping[slot_offset : slot_offset + 15],
                    b"B2TEX001payload",
                )


if __name__ == "__main__":
    unittest.main()
