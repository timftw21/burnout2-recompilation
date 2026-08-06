from __future__ import annotations

import os
import struct
import unittest
import uuid

from tools.playability import live_transport


@unittest.skipUnless(os.name == "nt", "named pagefile mappings require Windows")
class LiveControlTransportTests(unittest.TestCase):
    def test_boot_profile_can_start_active_before_the_guest_opens_mapping(self) -> None:
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))
        with live_transport.LiveControlTransport.create(name) as owner:
            owner.configure_hot_path_profile(True, start_active=True)
            with live_transport.LiveControlTransport.open(name) as guest:
                self.assertEqual(
                    guest.hot_path_profile_state(),
                    live_transport.HOT_PATH_PROFILE_STATE_ACTIVE,
                )

    def test_fixed_schema_carries_stop_controller_and_presentation_state(self) -> None:
        name = live_transport.live_control_transport_name(str(uuid.uuid4()))
        with live_transport.LiveControlTransport.create(name) as owner:
            with live_transport.LiveControlTransport.open(name) as guest:
                self.assertFalse(guest.stop_requested())
                self.assertEqual(
                    guest.hot_path_profile_state(),
                    live_transport.HOT_PATH_PROFILE_STATE_DISABLED,
                )
                self.assertEqual(
                    guest.replay_capture_state(),
                    live_transport.REPLAY_CAPTURE_STATE_DISABLED,
                )
                owner.configure_hot_path_profile(True)
                owner.configure_replay_capture(True)
                self.assertEqual(
                    guest.hot_path_profile_state(),
                    live_transport.HOT_PATH_PROFILE_STATE_ARMED,
                )
                self.assertEqual(
                    guest.replay_capture_state(),
                    live_transport.REPLAY_CAPTURE_STATE_ARMED,
                )
                self.assertEqual(
                    owner.request_replay_capture(),
                    live_transport.REPLAY_CAPTURE_STATE_REQUESTED,
                )
                self.assertEqual(
                    guest.replay_capture_state(),
                    live_transport.REPLAY_CAPTURE_STATE_REQUESTED,
                )
                owner.complete_replay_capture()
                self.assertEqual(
                    guest.replay_capture_state(),
                    live_transport.REPLAY_CAPTURE_STATE_COMPLETE,
                )
                owner.request_stop()
                self.assertTrue(guest.stop_requested())

                published_sequence = owner.publish_controller(
                    buttons=0x1010,
                    left_trigger=17,
                    right_trigger=29,
                    thumb_lx=-123,
                    thumb_ly=456,
                    thumb_rx=-789,
                    thumb_ry=1024,
                )
                controller = guest.read_controller(0)

                self.assertIsNotNone(controller)
                assert controller is not None
                self.assertEqual(controller.sequence, published_sequence)
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

                for offset, value in zip(
                    (236, 240, 244, 248, 252),
                    (3, 0x5000, 0x6000, 0x7000, 48000),
                    strict=True,
                ):
                    struct.pack_into("<I", owner._mapping, offset, value)
                diagnostic = guest.diagnostic_state()
                self.assertEqual(
                    diagnostic["hot_path_profile_state"],
                    live_transport.HOT_PATH_PROFILE_STATE_ARMED,
                )
                self.assertEqual(
                    diagnostic["replay_capture_state"],
                    live_transport.REPLAY_CAPTURE_STATE_COMPLETE,
                )
                self.assertEqual(diagnostic["audio_buffer_play_stage"], 3)
                self.assertEqual(diagnostic["audio_last_buffer"], 0x5000)
                self.assertEqual(diagnostic["audio_last_data"], 0x6000)
                self.assertEqual(diagnostic["audio_last_size"], 0x7000)
                self.assertEqual(diagnostic["audio_last_sample_rate"], 48000)

                self.assertFalse(guest.manifest_available())
                owner.publish_manifest(
                    {
                        "guest_flip_count": 42,
                        "resource_snapshots_unchanged": True,
                        "command_snapshot_path": "commands.bin",
                    }
                )
                self.assertTrue(guest.manifest_available())
                self.assertEqual(
                    guest.read_manifest(),
                    {
                        "resource_snapshots_unchanged": True,
                        "command_snapshot_path": "commands.bin",
                        "guest_flip_count": 42,
                    },
                )
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
