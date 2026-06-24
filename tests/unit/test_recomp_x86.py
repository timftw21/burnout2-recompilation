from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.recomp.x86_lifter import (
    CpuState,
    SparseMemory,
    build_recompilation_summary,
    cpp_sha256,
    emit_cpp,
    execute_lifted_function,
    lift_x86_function,
)


def _sum_helper_bytes() -> bytes:
    return bytes.fromhex(
        "55"  # push ebp
        "8BEC"  # mov ebp, esp
        "83EC04"  # sub esp, 4
        "8B4508"  # mov eax, [ebp+8]
        "03450C"  # add eax, [ebp+0xC]
        "8945FC"  # mov [ebp-4], eax
        "83F807"  # cmp eax, 7
        "7505"  # jne skip_call
        "E8E70F0000"  # call 0x2000
        "8B45FC"  # skip_call: mov eax, [ebp-4]
        "C9"  # leave
        "C20800"  # ret 8
    )


class X86RecompPrototypeTests(unittest.TestCase):
    def test_lifts_small_x86_function_and_records_control_targets(self) -> None:
        function = lift_x86_function(
            _sum_helper_bytes(),
            base_address=0x1000,
            symbol="sum_helper",
        )

        self.assertEqual(function.instruction_count, 12)
        self.assertEqual(function.code_size, len(_sum_helper_bytes()))
        self.assertEqual(function.call_targets, (0x2000,))
        self.assertEqual(function.branch_targets, (0x1019,))
        self.assertEqual(function.instructions[0].text(), "push ebp")
        self.assertEqual(function.instructions[-1].ret_stack_adjust, 8)

    def test_execution_trace_covers_flags_stack_memory_and_call_targets(self) -> None:
        function = lift_x86_function(
            _sum_helper_bytes(),
            base_address=0x1000,
            symbol="sum_helper",
        )
        state = CpuState.with_registers(esp=0x8000, ebp=0x77770000)
        memory = SparseMemory(
            {
                0x8000: 0xDEADC0DE,
                0x8004: 3,
                0x8008: 4,
            }
        )
        calls: list[int] = []

        def call_handler(cpu: CpuState, _memory: SparseMemory, target: int, _trace) -> None:
            calls.append(target)
            cpu.set_register("edx", 0xCA110000 | len(calls))

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers={0x2000: call_handler},
        )
        trace = result.trace.to_list()
        operations = [event["operation"] for event in trace]

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("eax"), 7)
        self.assertEqual(result.state.get_register("edx"), 0xCA110001)
        self.assertEqual(result.state.get_register("ebp"), 0x77770000)
        self.assertEqual(result.state.get_register("esp"), 0x800C)
        self.assertTrue(result.state.flags.zf)
        self.assertFalse(result.state.flags.cf)
        self.assertEqual(memory.read_u32(0x7FF8), 7)
        self.assertEqual(memory.read_u32(0x7FF4), 0x1019)
        self.assertEqual(calls, [0x2000])
        self.assertIn("memory_read", operations)
        self.assertIn("memory_write", operations)
        self.assertIn("flags", operations)
        self.assertIn("call", operations)
        self.assertIn("return", operations)

        branch_events = [
            event for event in trace if event["operation"] == "branch"
        ]
        self.assertEqual(branch_events[0]["details"]["condition"], "ne")
        self.assertFalse(branch_events[0]["details"]["taken"])

    def test_conditional_branch_trace_matches_skipped_call_path(self) -> None:
        function = lift_x86_function(
            _sum_helper_bytes(),
            base_address=0x1000,
            symbol="sum_helper",
        )
        state = CpuState.with_registers(esp=0x8000, ebp=0x77770000)
        memory = SparseMemory(
            {
                0x8000: 0xDEADC0DE,
                0x8004: 1,
                0x8008: 2,
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        trace = result.trace.to_list()
        operations = [event["operation"] for event in trace]
        branch_events = [
            event for event in trace if event["operation"] == "branch"
        ]

        self.assertEqual(result.state.get_register("eax"), 3)
        self.assertFalse(result.state.flags.zf)
        self.assertEqual(memory.read_u32(0x7FF8), 3)
        self.assertEqual(result.state.get_register("esp"), 0x800C)
        self.assertNotIn("call", operations)
        self.assertTrue(branch_events[0]["details"]["taken"])
        self.assertEqual(branch_events[0]["details"]["target"], 0x1019)

    def test_cplusplus_emission_is_deterministic_and_windows_scoped(self) -> None:
        function = lift_x86_function(
            _sum_helper_bytes(),
            base_address=0x1000,
            symbol="sum_helper",
        )

        first = emit_cpp(function, exported_symbol="sum_helper")
        second = emit_cpp(function, exported_symbol="sum_helper")

        self.assertEqual(first, second)
        self.assertEqual(cpp_sha256(first), cpp_sha256(second))
        self.assertIn("#ifndef _WIN32", first)
        self.assertIn('extern "C" __declspec(dllexport)', first)
        self.assertIn("ctx->call(ctx->user, 0x00002000u, ctx);", first)
        self.assertNotIn("55 8B EC", first)

    def test_summary_and_generated_cpp_can_be_written_to_ignored_artifact_paths(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("33C0C3"),
            base_address=0x12594,
            symbol="zero_return",
        )
        cpp = emit_cpp(function, exported_symbol="zero_return")

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "reports" / "local" / "recomp" / "zero_return.cpp"
            output.parent.mkdir(parents=True)
            output.write_text(cpp, encoding="utf-8", newline="\n")
            summary = build_recompilation_summary(
                function,
                cpp_source=cpp,
                cpp_output=output,
                source_kind="synthetic_hex",
            )

        self.assertEqual(summary["format"], "b2-recomp-recompile-range")
        self.assertTrue(summary["public_safe"])
        self.assertEqual(summary["target_platform"], "windows")
        self.assertEqual(summary["generated_language"], "c++17")
        self.assertEqual(summary["renderer_backend"], "vulkan")
        self.assertEqual(summary["lifted"]["instruction_count"], 2)
        self.assertEqual(summary["generated_cpp"]["sha256"], cpp_sha256(cpp))


if __name__ == "__main__":
    unittest.main()
