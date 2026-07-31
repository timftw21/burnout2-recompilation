from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import validation_closeout


class ValidationCloseoutTests(unittest.TestCase):
    def test_matrix_contains_all_native_tiers_and_optional_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            capsule = root / "manual.b2rcap"
            nodes = validation_closeout.build_closeout_nodes(
                jobs=6,
                run_directory=root,
                replay_capsules=(capsule,),
                replay_max_steps=1234,
            )

        self.assertEqual(
            set(nodes),
            {
                "asset_free",
                "native_debug",
                "native_release",
                "native_sanitizer",
                "strict_presenter",
                "long_replay_00",
            },
        )
        self.assertIn("--presenter", nodes["strict_presenter"].command)
        self.assertEqual(nodes["strict_presenter"].dependencies, ("native_release",))
        self.assertIn("1234", nodes["long_replay_00"].command)

    def test_launch_requests_supersession_and_returns_without_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prior = root / "prior"
            prior.mkdir()
            (prior / "manifest.json").write_text(
                json.dumps({"status": "running", "branch": "main"}),
                encoding="utf-8",
            )
            process = mock.Mock(pid=4242)
            with (
                mock.patch.object(
                    validation_closeout,
                    "worktree_identity",
                    return_value=("a" * 40, "main", "b" * 64),
                ),
                mock.patch.object(validation_closeout.subprocess, "Popen", return_value=process),
            ):
                launched = validation_closeout.launch_closeout(
                    jobs=3,
                    closeout_root=root,
                )

            manifest = json.loads((launched / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "queued")
            self.assertEqual(manifest["pid"], 4242)
            self.assertEqual(manifest["superseded_runs"], ["prior"])
            self.assertTrue((prior / "cancel.requested").is_file())


if __name__ == "__main__":
    unittest.main()
