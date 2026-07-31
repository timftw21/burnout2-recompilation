from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from tools.playability.render_capture_tail import trim_render_capture


class RenderCaptureTailTests(unittest.TestCase):
    def test_retains_complete_span_suffix_and_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "commands.bin"
            resources = root / "resources.bin"
            bootstrap = root / "interpreter-bootstrap.bin"
            spans = [
                struct.pack("<BBHIII", 1, 0, 0, 0x8000, 8, 2) + b"a" * 8,
                struct.pack("<BBHIII", 1, 0, 0, 0x9000, 16, 4) + b"b" * 16,
                struct.pack("<BBHIII", 0, 0, 0, 0xA000, 4, 1) + b"c" * 4,
            ]
            commands.write_bytes(b"B2SPAN01" + b"".join(spans))
            resources.write_bytes(b"resources")
            bootstrap.write_bytes(b"bootstrap")
            manifest = root / "render.json"
            manifest.write_text(
                json.dumps(
                    {
                        "command_snapshot_path": str(commands),
                        "resource_snapshot_path": str(resources),
                        "interpreter_bootstrap_path": str(bootstrap),
                    }
                ),
                encoding="utf-8",
            )

            output = trim_render_capture(manifest, root / "tail", 52)

            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["command_snapshot_record_count"], 5)
            self.assertEqual(result["command_snapshot_trimmed_record_count"], 2)
            self.assertEqual(
                (root / "tail" / "commands.bin").read_bytes(),
                b"B2SPAN01" + b"".join(spans[1:]),
            )
            self.assertEqual(
                (root / "tail" / "interpreter-bootstrap.bin").read_bytes(),
                b"bootstrap",
            )


if __name__ == "__main__":
    unittest.main()
