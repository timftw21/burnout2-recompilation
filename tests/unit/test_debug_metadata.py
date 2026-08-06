from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.playability.replay_capsule import load_replay_capsule
from tools.recomp.debug_metadata import (
    build_module_debug_metadata,
    generated_body,
    guest_address_ranges,
    load_native_debug_index,
    lookup_guest_address,
    write_module_debug_metadata,
    write_native_debug_index,
)
from tools.recomp.native_executor import NativeResumableExecutor
from tools.recomp.x86_lifter import (
    CpuState,
    LiftedFunction,
    SparseMemory,
    X86Instruction,
    emit_cpp,
    lift_x86_function,
)


class NativeDebugMetadataTests(unittest.TestCase):
    def test_alternate_instruction_entries_coalesce_into_one_owned_range(self) -> None:
        function = LiftedFunction(
            symbol="overlapping_entries",
            base_address=0x11440,
            code_size=6,
            instructions=(
                X86Instruction(0x11440, 6, "add"),
                X86Instruction(0x11441, 1, "ret"),
            ),
        )

        self.assertEqual(guest_address_ranges(function), ((0x11440, 0x11446),))

    def test_generated_cases_map_to_ir_blocks_and_source_lines(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40890500200000C3"),
            base_address=0x1000,
            symbol="debug_test",
        )
        symbol = "b2r_guest_00001000_00001008"
        source = emit_cpp(function, exported_symbol=symbol, resumable=True)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            source_path = root / "native-loop-test.cpp"
            source_path.write_text(source, encoding="utf-8")
            metadata = build_module_debug_metadata(
                function,
                source,
                source_path=source_path,
                object_path=root / "native-loop-test.obj",
                artifact_path=root / "native-loop-test.dll",
                pdb_path=root / "native-loop-test.pdb",
                native_symbol=symbol,
                configuration_key="configuration",
                content_digest="content",
            )
            metadata_path = write_module_debug_metadata(
                root / "native-loop-test.debug.json", metadata
            )
            index_path = write_native_debug_index(root, ((metadata_path, metadata),))
            index = load_native_debug_index(index_path)
            entry = lookup_guest_address(index, 0x1001)

            self.assertNotIn("entries", index)
            self.assertEqual(index["modules"][0]["entry_count"], 3)
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry["native_symbol"], symbol)
            self.assertEqual(entry["generated_ir"]["mnemonic"], "mov")
            self.assertEqual(entry["decoded_block"]["start"], 0x1000)
            body = generated_body(index_path, entry)
            self.assertIn("case 0x00001001u", body)
            self.assertIn("b2r_write_u32", body)
            self.assertNotIn("case 0x00001007u", body)
            last_entry = lookup_guest_address(index, 0x1007)
            assert last_entry is not None
            self.assertNotIn("default:", generated_body(index_path, last_entry))

    @unittest.skipUnless(shutil.which("clang-cl"), "clang-cl is unavailable")
    def test_local_debug_executor_retains_pdb_and_index(self) -> None:
        function = lift_x86_function(b"\x40\xc3", base_address=0x3000)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            executor = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                compiler_cache_mode="off",
                preserve_debug_symbols=True,
            )
            index = load_native_debug_index(executor.debug_metadata_path)
            entry = lookup_guest_address(index, 0x3000)

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertTrue((Path(temp_dir) / entry["pdb"]).is_file())
            self.assertEqual(entry["native_symbol"], "b2r_guest_00003000_00003002")
            self.assertEqual(executor.cache_summary["generated_pdb_count"], 1)
            run_state = CpuState.with_registers(esp=0x8000)
            run_memory = SparseMemory({0x8000: 0})
            executor.run(run_state, run_memory, max_steps=2, profile_hot_paths=False)
            diagnostic_context = executor.last_run_summary["diagnostic_context"]
            self.assertEqual(
                diagnostic_context["native_symbol"],
                "b2r_guest_00003000_00003002",
            )
            self.assertEqual(diagnostic_context["dispatch_eip"], 0x3000)
            capsule_path = Path(temp_dir) / "manual.b2rcap"
            state = CpuState.with_registers(eax=4, esp=0x8000)
            state.eip = 0x3000
            with mock.patch(
                "tools.playability.replay_capsule.require_local_capsule_output"
            ):
                executor.capture_manual_replay_capsule(
                    capsule_path,
                    state=state,
                    memory=SparseMemory({0x8000: 0}),
                    scheduler_state={"lane": "primary"},
                    service_state={"sequence": 7},
                    provenance={"reason": "unit-test"},
                )
            capsule = load_replay_capsule(capsule_path)
            self.assertTrue(capsule.manifest["provenance"]["manual_capture"])
            self.assertEqual(capsule.state.eip, 0x3000)
            module_metadata = next(Path(temp_dir).glob("native-loop-*.debug.json"))
            metadata_mtime = module_metadata.stat().st_mtime_ns
            warm = NativeResumableExecutor(
                function,
                build_dir=Path(temp_dir),
                compiler_cache_mode="off",
                preserve_debug_symbols=True,
            )
            self.assertEqual(warm.cache_summary["warm_hit_count"], 1)
            self.assertEqual(module_metadata.stat().st_mtime_ns, metadata_mtime)
            self.assertLess(
                warm.debug_metadata_path.stat().st_size,
                module_metadata.stat().st_size,
            )


if __name__ == "__main__":
    unittest.main()
