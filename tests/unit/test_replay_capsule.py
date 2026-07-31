from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.playability.replay_capsule import (
    ReplayCapsuleError,
    build_synthetic_capsule,
    capture_replay_capsule,
    clone_capsule_state,
    load_replay_capsule,
)
from tools.recomp.x86_lifter import CpuState, SparseMemory, lift_x86_function


class ReplayCapsuleTests(unittest.TestCase):
    def test_synthetic_capsule_is_deterministic_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = build_synthetic_capsule(root / "first.b2rcap")
            second = build_synthetic_capsule(root / "second.b2rcap")

            self.assertEqual(first.read_bytes(), second.read_bytes())
            capsule = load_replay_capsule(first)
            self.assertEqual(capsule.state.eip, 0x1000)
            self.assertEqual(capsule.state.get_register("eax"), 41)
            self.assertEqual(capsule.memory.read(0x2000, 1), b"\0")
            self.assertEqual(capsule.manifest["capture_kind"], "synthetic")
            self.assertEqual(capsule.events["render_events"][0]["kind"], "typed_draw")
            self.assertIn("render/typed-draw.json", capsule.resources)

    def test_capsule_preserves_sparse_pages_and_exact_float_bits(self) -> None:
        function = lift_x86_function(b"\xc3", base_address=0x4000, symbol="return")
        state = CpuState.with_registers(eax=7, esp=0x9000)
        state.eip = 0x4000
        state.flags.cf = True
        state.fpu_stack = [-0.0, 1.25]
        state.set_xmm_register("xmm0", (-0.0, 1.0, 2.0, 3.0))
        state.set_mmx_register("mm0", 0xFEDCBA9876543210)
        memory = SparseMemory()
        memory.write_u32(0x12FFC, 0x12345678)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "manual.b2rcap"
            capture_replay_capsule(
                output,
                state=state,
                memory=memory,
                functions=(function,),
                scheduler_state={"lane": 3},
                service_state={"last": 0xDEAD},
                provenance={"manual_capture": True, "target_sha256": "a" * 64},
            )
            capsule = load_replay_capsule(output)
            cloned_state, cloned_memory = clone_capsule_state(capsule)

            self.assertEqual(cloned_state.flags.cf, True)
            self.assertEqual(cloned_state.fpu_stack, [-0.0, 1.25])
            self.assertEqual(cloned_state.get_mmx_register("mm0"), 0xFEDCBA9876543210)
            self.assertEqual(cloned_memory.read_u32(0x12FFC), 0x12345678)
            self.assertEqual(capsule.scheduler_state, {"lane": 3})
            self.assertEqual(capsule.service_state, {"last": 0xDEAD})

    def test_manual_capsule_requires_manual_capture_provenance(self) -> None:
        function = lift_x86_function(b"\xc3", base_address=0x1000)
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ReplayCapsuleError, "manual_capture"):
                capture_replay_capsule(
                    Path(temp_dir) / "invalid.b2rcap",
                    state=CpuState(),
                    memory=SparseMemory(),
                    functions=(function,),
                    scheduler_state={},
                    service_state={},
                    provenance={},
                )

    def test_capsule_rejects_undeclared_archive_members(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = build_synthetic_capsule(Path(temp_dir) / "tampered.b2rcap")
            with zipfile.ZipFile(output, "a") as bundle:
                bundle.writestr("undeclared.bin", b"tamper")

            with self.assertRaisesRegex(ReplayCapsuleError, "undeclared"):
                load_replay_capsule(output)


if __name__ == "__main__":
    unittest.main()
