from __future__ import annotations

import math
import struct
import tempfile
import unittest
from pathlib import Path

from tools.recomp.x86_lifter import (
    CpuState,
    DETERMINISTIC_TSC_STEP,
    SparseMemory,
    X86ExecutionError,
    X86Decoder,
    build_recompilation_summary,
    cpp_sha256,
    emit_cpp,
    execute_lifted_function,
    lift_x86_block,
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
    def test_sparse_memory_reports_only_pages_changed_since_last_boundary(self) -> None:
        memory = SparseMemory({0x3000: 1, 0x8000: 2})

        self.assertEqual(memory.consume_changed_pages(), {0x3, 0x8})
        self.assertEqual(memory.consume_changed_pages(), set())

        memory.write(0x4FFF, b"\xAA\xBB")
        self.assertEqual(memory.consume_changed_pages(), {0x4, 0x5})

    def test_absolute_byte_accumulator_moves_decode_execute_and_emit(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "A000300000"  # mov al, byte ptr [0x3000]
                "A201300000"  # mov byte ptr [0x3001], al
                "C3"
            ),
            base_address=0x1000,
            symbol="absolute_byte_moves",
        )
        state = CpuState.with_registers(eax=0xAABBCCDD, esp=0x8000)
        memory = SparseMemory({0x3000: 0x7B, 0x8000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(function.instructions[0].text(), "mov al, [0x00003000]")
        self.assertEqual(function.instructions[1].text(), "mov [0x00003001], al")
        self.assertEqual(result.state.get_register("eax"), 0xAABBCC7B)
        self.assertEqual(memory.read(0x3001, 1), b"{")
        self.assertEqual(result.return_address, 0xDEADC0DE)
        generated = emit_cpp(function)
        self.assertIn("ctx->read_u8", generated)
        self.assertIn("ctx->write_u8", generated)
        self.assertIn("dirty_page_indices", generated)
        self.assertIn("b2r_mark_dirty_page(ctx, page", generated)

    def test_zero_max_steps_runs_without_a_step_limit(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("9090C3"),
            base_address=0x1000,
            symbol="unlimited_steps",
        )
        result = execute_lifted_function(
            function,
            state=CpuState.with_registers(esp=0x8000),
            memory=SparseMemory({0x8000: 0xDEADC0DE}),
            max_steps=0,
        )

        self.assertEqual(result.steps, 3)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_shld_and_shrd_immediate_forms_execute_observed_double_shifts(self) -> None:
        shld = lift_x86_function(
            bytes.fromhex("0FA4D602C3"),
            base_address=0x1000,
            symbol="shld_observed",
        )
        self.assertEqual(shld.instructions[0].text(), "shld esi, edx, 0x00000002")
        shld_result = execute_lifted_function(
            shld,
            state=CpuState.with_registers(esi=0x40000000, edx=0x80000000, esp=0x8000),
            memory=SparseMemory({0x8000: 0xDEADC0DE}),
        )
        self.assertEqual(shld_result.state.get_register("esi"), 2)
        self.assertTrue(shld_result.state.flags.cf)
        self.assertFalse(shld_result.state.flags.zf)

        shrd = lift_x86_function(
            bytes.fromhex("0FACC101C3"),
            base_address=0x2000,
            symbol="shrd_observed",
        )
        self.assertEqual(shrd.instructions[0].text(), "shrd ecx, eax, 0x00000001")
        shrd_result = execute_lifted_function(
            shrd,
            state=CpuState.with_registers(ecx=1, eax=1, esp=0x9000),
            memory=SparseMemory({0x9000: 0xDEADC0DE}),
        )
        self.assertEqual(shrd_result.state.get_register("ecx"), 0x80000000)
        self.assertTrue(shrd_result.state.flags.cf)
        self.assertTrue(shrd_result.state.flags.of)

        generated = emit_cpp(shld)
        self.assertIn("const uint32_t source = ctx->edx;", generated)
        self.assertIn("ctx->flags.cf", generated)

    def test_shld_cl_form_executes_observed_address_taken_path(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0FA5C2C3"),
            base_address=0x001206AA,
            symbol="shld_cl_observed",
        )

        self.assertEqual(function.instructions[0].text(), "shld edx, eax, cl")
        result = execute_lifted_function(
            function,
            state=CpuState.with_registers(
                eax=0x80000000,
                ecx=2,
                edx=0x40000000,
                esp=0x8000,
            ),
            memory=SparseMemory({0x8000: 0xDEADC0DE}),
        )

        self.assertEqual(result.state.get_register("edx"), 2)
        self.assertTrue(result.state.flags.cf)
        self.assertIn("ctx->ecx & 0xffu", emit_cpp(function))

    def test_byte_one_operand_imul_writes_ax_and_overflow_flags(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("F6EBC3"),
            base_address=0x3000,
            symbol="imul_byte_observed",
        )
        self.assertEqual(function.instructions[0].text(), "imul bl")
        result = execute_lifted_function(
            function,
            state=CpuState.with_registers(eax=0xAAAA007F, ebx=2, esp=0x8000),
            memory=SparseMemory({0x8000: 0xDEADC0DE}),
        )
        self.assertEqual(result.state.get_register("eax"), 0xAAAA00FE)
        self.assertTrue(result.state.flags.cf)
        self.assertTrue(result.state.flags.of)

        fitting = execute_lifted_function(
            function,
            state=CpuState.with_registers(eax=0xBBBB00FE, ebx=3, esp=0x9000),
            memory=SparseMemory({0x9000: 0xDEADC0DE}),
        )
        self.assertEqual(fitting.state.get_register("eax"), 0xBBBBFFFA)
        self.assertFalse(fitting.state.flags.cf)
        self.assertFalse(fitting.state.flags.of)
        self.assertIn("const int32_t product", emit_cpp(function))

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
        memory_writes = [
            event for event in trace if event["operation"] == "memory_write"
        ]
        self.assertTrue(all(event["details"]["size"] == 4 for event in memory_writes))

        branch_events = [
            event for event in trace if event["operation"] == "branch"
        ]
        self.assertEqual(branch_events[0]["details"]["condition"], "ne")
        self.assertFalse(branch_events[0]["details"]["taken"])

    def test_execution_can_skip_per_instruction_trace_for_sustained_runs(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B80100000040C3"),
            base_address=0x1800,
            symbol="untraced_increment",
        )
        result = execute_lifted_function(
            function,
            state=CpuState.with_registers(esp=0x9000),
            memory=SparseMemory({0x9000: 0xDEADC0DE}),
            record_instruction_trace=False,
        )

        self.assertEqual(result.state.get_register("eax"), 2)
        self.assertNotIn(
            "instruction",
            [event["operation"] for event in result.trace.to_list()],
        )

        bounded = execute_lifted_function(
            function,
            state=CpuState.with_registers(esp=0xA000),
            memory=SparseMemory({0xA000: 0xDEADC0DE}),
            trace_max_events=2,
        )
        self.assertLessEqual(len(bounded.trace.to_list()), 2)
        self.assertGreater(bounded.trace.to_list()[0]["sequence"], 0)

        trace_disabled = execute_lifted_function(
            function,
            state=CpuState.with_registers(esp=0xB000),
            memory=SparseMemory({0xB000: 0xDEADC0DE}),
            record_trace=False,
        )
        self.assertEqual(trace_disabled.state.get_register("eax"), 2)
        self.assertEqual(trace_disabled.trace.to_list(), [])

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

    def test_jecxz_short_branch_tests_ecx_directly(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("E306B801000000C3B802000000C3"),
            base_address=0x1000,
            symbol="jecxz_branch",
        )
        taken_state = CpuState.with_registers(esp=0x9000, ecx=0)
        fallthrough_state = CpuState.with_registers(esp=0xA000, ecx=3)
        taken_memory = SparseMemory({0x9000: 0xDEADC0DE})
        fallthrough_memory = SparseMemory({0xA000: 0xBAADF00D})
        target_block = lift_x86_block(
            bytes.fromhex("B802000000C3"),
            base_address=0x1008,
            symbol="jecxz_target",
        )

        taken = execute_lifted_function(
            function,
            state=taken_state,
            memory=taken_memory,
            block_loader=lambda target: target_block if target == 0x1008 else None,
        )
        fallthrough = execute_lifted_function(
            function,
            state=fallthrough_state,
            memory=fallthrough_memory,
        )
        emitted = emit_cpp(function, exported_symbol="jecxz_branch")
        branch_events = [
            event
            for event in taken.trace.to_list()
            if event["operation"] == "branch"
        ]

        self.assertEqual(function.branch_targets, (0x1008,))
        self.assertEqual(function.instructions[0].condition, "ecx_zero")
        self.assertEqual(taken.state.get_register("eax"), 2)
        self.assertEqual(taken.return_address, 0xDEADC0DE)
        self.assertEqual(fallthrough.state.get_register("eax"), 1)
        self.assertEqual(fallthrough.return_address, 0xBAADF00D)
        self.assertTrue(branch_events[0]["details"]["taken"])
        self.assertEqual(branch_events[0]["details"]["condition"], "ecx_zero")
        self.assertIn("ctx->ecx == 0u", emitted)

    def test_missing_instruction_error_preserves_state_and_trace(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B8C2080090FFE0"),
            base_address=0x1000,
            symbol="jump_to_unmapped_target",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        with self.assertRaises(X86ExecutionError) as raised:
            execute_lifted_function(function, state=state, memory=memory)

        error = raised.exception
        self.assertEqual(str(error), "no lifted instruction at 0x900008C2")
        self.assertIsNotNone(error.state)
        self.assertIsNotNone(error.trace)
        self.assertEqual(error.steps, 2)
        self.assertEqual(error.state.eip, 0x900008C2)
        trace = error.trace.to_list()
        self.assertEqual(trace[-1]["operation"], "missing_instruction")
        self.assertEqual(trace[-1]["details"]["eip"], 0x900008C2)

    def test_missing_call_target_can_return_to_hybrid_dispatcher(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("E8FB0F0000C3"),  # call 0x2000; ret
            base_address=0x1000,
            symbol="call_to_deferred_native_target",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            return_on_missing_instruction=True,
        )

        self.assertEqual(result.return_address, 0x2000)
        self.assertEqual(result.steps, 1)
        self.assertEqual(state.eip, 0x2000)
        self.assertEqual(state.get_register("esp"), 0x8FFC)
        self.assertEqual(memory.read_u32(0x8FFC), 0x1005)

    def test_call_handler_can_yield_after_guest_return(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("E8FB0F000040C3"),  # call 0x2000; inc eax; ret
            base_address=0x1000,
            symbol="cooperative_interpreter_call_yield",
        )
        state = CpuState.with_registers(eax=3, esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        def handler(cpu, _memory, _target, _trace) -> None:
            cpu.set_register("eax", cpu.get_register("eax") + 4)

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers={0x2000: handler},
            call_handler_yield_predicate=lambda target: target == 0x2000,
        )

        self.assertEqual(result.return_address, 0x1005)
        self.assertEqual(result.steps, 1)
        self.assertEqual(state.eip, 0x1005)
        self.assertEqual(state.get_register("esp"), 0x9000)
        self.assertEqual(state.get_register("eax"), 7)

    def test_return_can_load_known_hybrid_dispatcher_block(self) -> None:
        callee = lift_x86_function(
            bytes.fromhex("C3"),
            base_address=0x2000,
            symbol="frontier_callee",
        )
        caller = lift_x86_function(
            bytes.fromhex("40C3"),
            base_address=0x1000,
            symbol="frontier_caller",
        )
        state = CpuState.with_registers(eax=4, esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0x1000,
                0x9004: 0xDEADC0DE,
            }
        )

        result = execute_lifted_function(
            callee,
            state=state,
            memory=memory,
            block_loader=lambda target: caller if target == 0x1000 else None,
            return_on_missing_instruction=True,
        )

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.steps, 3)
        self.assertEqual(state.get_register("eax"), 5)

    def test_hybrid_dispatcher_can_yield_at_execution_deadline(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("40EBFD"),  # inc eax; jmp 0x1000
            base_address=0x1000,
            symbol="frontier_deadline_loop",
        )
        state = CpuState.with_registers(eax=1)

        result = execute_lifted_function(
            function,
            state=state,
            execution_yield_predicate=lambda steps: steps >= 5,
            max_steps=100,
        )

        self.assertEqual(result.steps, 5)
        self.assertEqual(result.return_address, 0x1001)
        self.assertEqual(state.get_register("eax"), 4)

    def test_signed_extend_and_signed_divide_match_entry_probe_semantics(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B8F4FFFFFF99B9FEFFFFFFF7F9C3"),
            base_address=0x3000,
            symbol="signed_divide",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("eax"), 6)
        self.assertEqual(result.state.get_register("edx"), 0)
        self.assertIn("signed_divide", operations)

    def test_word_movsx_sign_extends_observed_internal_call_load(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0FBF0500200000C3"),
            base_address=0x3008,
            symbol="signed_word_load",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE, 0x2000: b"\xfe\xff"})

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="signed_word_load")

        self.assertEqual(function.instructions[0].text(), "movsx eax, [0x00002000]")
        self.assertEqual(result.state.get_register("eax"), 0xFFFFFFFE)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("b2r_sign_extend", emitted)

    def test_unsigned_divide_matches_d3d_helper_semantics(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("BA00000000B80A000000B904000000F7F1C3"),
            base_address=0x3010,
            symbol="unsigned_divide",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("eax"), 2)
        self.assertEqual(result.state.get_register("edx"), 2)
        self.assertIn("unsigned_divide", operations)

    def test_immediate_shift_updates_value_and_flags(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B908000000C1E902C3"),
            base_address=0x3100,
            symbol="shift_right",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("ecx"), 2)
        self.assertFalse(result.state.flags.zf)
        self.assertFalse(result.state.flags.sf)
        self.assertFalse(result.state.flags.cf)

    def test_segment_prefixed_movzx_and_low_byte_compare_decode(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("640FB605240000003C02730190C3"),
            base_address=0x3200,
            symbol="fs_byte_probe",
        )
        state = CpuState.with_registers(esp=0x9000, fs_base=0x70000000)
        memory = SparseMemory({0x70000024: b"\x02", 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(function.instructions[0].operands[1].segment, "fs")
        self.assertEqual(function.instructions[0].text(), "movzx eax, fs:[0x00000024]")
        self.assertEqual(result.state.get_register("eax"), 2)
        self.assertTrue(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        bitwise_not = lift_x86_function(
            bytes.fromhex("B834120000F7D0C3"),
            base_address=0x4600,
            symbol="bitwise_not",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(bitwise_not, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0xFFFFEDCB)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_sib_no_base_lea_preserves_index_scale_and_displacement(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B9040000008D0C8D04000000C3"),
            base_address=0x3210,
            symbol="scheduler_array_size",
        )
        operand = function.instructions[1].operands[1]

        self.assertEqual(function.instructions[1].text(), "lea ecx, [ecx*4 + 0x4]")
        self.assertIsNone(operand.base)
        self.assertEqual(operand.index, "ecx")
        self.assertEqual(operand.scale, 4)
        self.assertEqual(operand.displacement, 4)
        self.assertIsNone(operand.absolute)

        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="scheduler_array_size")

        self.assertEqual(result.state.get_register("ecx"), 20)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("(ctx->ecx * 4u)", emitted)
        self.assertIn("0x00000004u", emitted)

    def test_rep_stosd_models_stack_buffer_fill(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B8EFBEADDEB903000000BF00200000F3ABC3"),
            base_address=0x3300,
            symbol="fill_buffer",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("ecx"), 0)
        self.assertEqual(result.state.get_register("edi"), 0x200C)
        self.assertEqual(memory.read_u32(0x2000), 0xDEADBEEF)
        self.assertEqual(memory.read_u32(0x2008), 0xDEADBEEF)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_stosd_models_single_dword_store(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("B8EFBEADDEB903000000BF00200000ABC3"),
            base_address=0x3310,
            symbol="single_dword_store",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("ecx"), 3)
        self.assertEqual(result.state.get_register("edi"), 0x2004)
        self.assertEqual(memory.read_u32(0x2000), 0xDEADBEEF)
        self.assertEqual(memory.read_u32(0x2004), 0)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_internal_direct_call_ret_continues_in_lifted_frame(self) -> None:
        function = X86Decoder().decode_function(
            bytes.fromhex("E801000000C3B834120000C3"),
            base_address=0x1000,
            symbol="nested_call_frame",
            stop_at_ret=False,
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        return_events = [
            event for event in result.trace.to_list() if event["operation"] == "return"
        ]

        self.assertEqual(result.state.get_register("eax"), 0x1234)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(
            [event["details"]["return_address"] for event in return_events],
            [0x1005, 0xDEADC0DE],
        )

    def test_basic_block_lift_stops_at_conditional_branch(self) -> None:
        block = lift_x86_block(
            bytes.fromhex("33C07405B811110000C3"),
            base_address=0x1800,
            symbol="branch_block",
        )

        self.assertEqual(block.instruction_count, 2)
        self.assertEqual(block.instructions[-1].mnemonic, "jcc")
        self.assertEqual(block.branch_targets, (0x1809,))
        self.assertEqual(block.code_size, 4)

    def test_unhandled_call_hook_can_stop_or_model_indirect_frontier(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("FF1500300000C3"),
            base_address=0x2000,
            symbol="indirect_frontier",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x3000: 0x00264E5D, 0x9000: 0xDEADC0DE})
        targets: list[int] = []

        def handler(cpu: CpuState, _memory: SparseMemory, target: int, _trace) -> None:
            targets.append(target)
            cpu.set_register("eax", 0xBEEF)

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            unhandled_call_handler=handler,
        )

        self.assertEqual(targets, [0x00264E5D])
        self.assertEqual(result.state.get_register("eax"), 0xBEEF)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_setcc_writes_observed_startup_boolean_byte(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("33C083F8010F95C0C3"),
            base_address=0x3400,
            symbol="set_not_equal",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 1)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_byte_mov_loads_observed_nested_callee_byte(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("8A0500200000C3"),
            base_address=0x3500,
            symbol="byte_load",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x2000: b"\x7F", 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0x7F)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_byte_immediate_string_move_and_int3_decode(self) -> None:
        byte_immediate = lift_x86_function(
            bytes.fromhex("B27FC3"),
            base_address=0x3600,
            symbol="byte_immediate",
        )
        state = CpuState.with_registers(esp=0x9000, edx=0x12345600)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_immediate, state=state, memory=memory)

        self.assertEqual(result.state.get_register("edx"), 0x1234567F)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        string_move = lift_x86_function(
            bytes.fromhex("A5C3"),
            base_address=0x3700,
            symbol="string_move",
        )
        state = CpuState.with_registers(esp=0x9000, esi=0x2000, edi=0x3000)
        memory = SparseMemory({0x2000: 0xCAFEBABE, 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(string_move, state=state, memory=memory)

        self.assertEqual(memory.read_u32(0x3000), 0xCAFEBABE)
        self.assertEqual(result.state.get_register("esi"), 0x2004)
        self.assertEqual(result.state.get_register("edi"), 0x3004)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_string_move = lift_x86_function(
            bytes.fromhex("A4C3"),
            base_address=0x3710,
            symbol="byte_string_move",
        )
        state = CpuState.with_registers(esp=0x9000, esi=0x2000, edi=0x3000)
        memory = SparseMemory({0x2000: b"\xA7", 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_string_move, state=state, memory=memory)

        self.assertEqual(memory.read(0x3000, 1), b"\xA7")
        self.assertEqual(result.state.get_register("esi"), 0x2001)
        self.assertEqual(result.state.get_register("edi"), 0x3001)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        repeated_byte_string_move = lift_x86_function(
            bytes.fromhex("F3A4C3"),
            base_address=0x3720,
            symbol="repeated_byte_string_move",
        )
        state = CpuState.with_registers(
            esp=0x9000, ecx=3, esi=0x2000, edi=0x3000
        )
        memory = SparseMemory({0x2000: b"\x01\x02\x03", 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            repeated_byte_string_move, state=state, memory=memory
        )

        self.assertEqual(memory.read(0x3000, 3), b"\x01\x02\x03")
        self.assertEqual(result.state.get_register("ecx"), 0)
        self.assertEqual(result.state.get_register("esi"), 0x2003)
        self.assertEqual(result.state.get_register("edi"), 0x3003)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_repe_cmpsb_matches_and_stops_on_mismatch(self) -> None:
        equal_compare = lift_x86_function(
            bytes.fromhex(
                "BE00100000"  # mov esi, 0x1000
                "BF00200000"  # mov edi, 0x2000
                "B903000000"  # mov ecx, 3
                "F3A6"  # repe cmpsb
                "C3"
            ),
            base_address=0x3730,
            symbol="repe_cmpsb_equal",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {0x1000: b"abc", 0x2000: b"abc", 0x9000: 0xDEADC0DE}
        )

        result = execute_lifted_function(equal_compare, state=state, memory=memory)
        operations = [event["operation"] for event in result.trace.to_list()]
        emitted = emit_cpp(equal_compare, exported_symbol="repe_cmpsb_equal")

        self.assertEqual(result.state.get_register("esi"), 0x1003)
        self.assertEqual(result.state.get_register("edi"), 0x2003)
        self.assertEqual(result.state.get_register("ecx"), 0)
        self.assertTrue(result.state.flags.zf)
        self.assertFalse(result.state.flags.cf)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("string_compare", operations)
        self.assertIn("b2r_sub_flags", emitted)
        self.assertIn("--ctx->ecx;", emitted)

        mismatch_compare = lift_x86_function(
            bytes.fromhex(
                "BE00100000"  # mov esi, 0x1000
                "BF00200000"  # mov edi, 0x2000
                "B903000000"  # mov ecx, 3
                "F3A6"  # repe cmpsb
                "C3"
            ),
            base_address=0x3740,
            symbol="repe_cmpsb_mismatch",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {0x1000: b"abc", 0x2000: b"axc", 0x9000: 0xDEADC0DE}
        )

        result = execute_lifted_function(mismatch_compare, state=state, memory=memory)

        self.assertEqual(result.state.get_register("esi"), 0x1002)
        self.assertEqual(result.state.get_register("edi"), 0x2002)
        self.assertEqual(result.state.get_register("ecx"), 1)
        self.assertFalse(result.state.flags.zf)
        self.assertTrue(result.state.flags.cf)
        self.assertTrue(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_cmpsb_updates_byte_compare_flags(self) -> None:
        byte_compare = lift_x86_function(
            bytes.fromhex(
                "BE00100000"  # mov esi, 0x1000
                "BF00200000"  # mov edi, 0x2000
                "A6"  # cmpsb
                "C3"
            ),
            base_address=0x3750,
            symbol="cmpsb_once",
        )
        state = CpuState.with_registers(esp=0x9000, ecx=7)
        memory = SparseMemory(
            {0x1000: b"b", 0x2000: b"a", 0x9000: 0xDEADC0DE}
        )

        result = execute_lifted_function(byte_compare, state=state, memory=memory)

        self.assertEqual(result.state.get_register("esi"), 0x1001)
        self.assertEqual(result.state.get_register("edi"), 0x2001)
        self.assertEqual(result.state.get_register("ecx"), 7)
        self.assertFalse(result.state.flags.zf)
        self.assertFalse(result.state.flags.cf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_repe_cmpsd_compares_dwords_and_stops_on_mismatch(self) -> None:
        dword_compare = lift_x86_function(
            bytes.fromhex("F3A7C3"),
            base_address=0x3758,
            symbol="repe_cmpsd_mismatch",
        )
        state = CpuState.with_registers(
            esp=0x9000,
            ecx=3,
            esi=0x1000,
            edi=0x2000,
        )
        memory = SparseMemory(
            {
                0x1000: struct.pack("<III", 1, 2, 3),
                0x2000: struct.pack("<III", 1, 5, 3),
                0x9000: 0xDEADC0DE,
            }
        )

        result = execute_lifted_function(dword_compare, state=state, memory=memory)
        emitted = emit_cpp(dword_compare, exported_symbol="repe_cmpsd_mismatch")

        self.assertEqual(dword_compare.instructions[0].mnemonic, "rep_cmpsd")
        self.assertEqual(result.state.get_register("esi"), 0x1008)
        self.assertEqual(result.state.get_register("edi"), 0x2008)
        self.assertEqual(result.state.get_register("ecx"), 1)
        self.assertFalse(result.state.flags.zf)
        self.assertTrue(result.state.flags.cf)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("ctx->read_u32", emitted)
        self.assertIn("ctx->esi += 4u", emitted)
        self.assertIn("result, 32u", emitted)

        one_dword = lift_x86_function(
            bytes.fromhex("A7C3"),
            base_address=0x375B,
            symbol="cmpsd_once",
        )
        self.assertEqual(one_dword.instructions[0].mnemonic, "cmpsd")

    def test_observed_repne_scasb_scans_until_match_or_count_exhaustion(self) -> None:
        scan = lift_x86_function(
            bytes.fromhex(
                "BF00200000"  # mov edi, 0x2000
                "B903000000"  # mov ecx, 3
                "33C0"  # xor eax, eax
                "F2AE"  # repne scasb
                "C3"
            ),
            base_address=0x3760,
            symbol="repne_scasb_match",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x2000: b"a\x00c", 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(scan, state=state, memory=memory)
        operations = [event["operation"] for event in result.trace.to_list()]
        emitted = emit_cpp(scan, exported_symbol="repne_scasb_match")

        self.assertEqual(result.state.get_register("edi"), 0x2002)
        self.assertEqual(result.state.get_register("ecx"), 1)
        self.assertTrue(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("string_scan", operations)
        self.assertIn("--ctx->ecx;", emitted)

        exhausted = lift_x86_function(
            bytes.fromhex(
                "BF00200000"  # mov edi, 0x2000
                "B903000000"  # mov ecx, 3
                "33C0"  # xor eax, eax
                "F2AE"  # repne scasb
                "C3"
            ),
            base_address=0x3770,
            symbol="repne_scasb_exhausted",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x2000: b"abc", 0x9000: 0xBAADF00D})

        result = execute_lifted_function(exhausted, state=state, memory=memory)

        self.assertEqual(result.state.get_register("edi"), 0x2003)
        self.assertEqual(result.state.get_register("ecx"), 0)
        self.assertFalse(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xBAADF00D)

    def test_observed_xchg_helper_swaps_memory_and_register(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "8B4C2408"  # mov ecx, [esp+8]
                "8B442404"  # mov eax, [esp+4]
                "8708"  # xchg [eax], ecx
                "C3"
            ),
            base_address=0x3760,
            symbol="xchg_helper",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x9004: 0x2000,
                0x9008: 0x12345678,
                0x2000: 0xCAFEBABE,
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="xchg_helper")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(function.instructions[2].text(), "xchg [eax], ecx")
        self.assertEqual(memory.read_u32(0x2000), 0x12345678)
        self.assertEqual(result.state.get_register("ecx"), 0xCAFEBABE)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("exchange", operations)
        self.assertIn("const uint32_t left", emitted)

        int3_block = X86Decoder().decode_function(
            bytes.fromhex("90CC90"),
            base_address=0x3800,
            symbol="padding_trap",
        )
        self.assertEqual(int3_block.instruction_count, 2)
        self.assertEqual(int3_block.instructions[-1].mnemonic, "int3")

        fpu_clear = lift_x86_function(
            bytes.fromhex("DBE2C3"),
            base_address=0x3810,
            symbol="fpu_clear",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(fpu_clear, state=state, memory=memory)

        self.assertEqual(fpu_clear.instructions[0].mnemonic, "fnclex")
        self.assertEqual(result.return_address, 0xDEADC0DE)

        fpu_control = lift_x86_function(
            bytes.fromhex("9BD93D00200000D92D00200000C3"),
            base_address=0x3820,
            symbol="fpu_control",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(fpu_control, state=state, memory=memory)

        self.assertIn("fwait", [instruction.mnemonic for instruction in fpu_control.instructions])
        self.assertEqual(memory.read(0x2000, 2), (0x037F).to_bytes(2, "little"))
        self.assertEqual(result.return_address, 0xDEADC0DE)

        fpu_log10 = lift_x86_function(
            bytes.fromhex(
                "D9EC"  # fldlg2
                "D90500200000"  # fld dword [0x2000]
                "D9F1"  # fyl2x
                "D80504200000"  # fadd dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3840,
            symbol="fpu_log10",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 100.0),
                0x2004: struct.pack("<f", 0.5),
            }
        )

        result = execute_lifted_function(fpu_log10, state=state, memory=memory)
        emitted = emit_cpp(fpu_log10, exported_symbol="fpu_log10")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertAlmostEqual(
            struct.unpack("<f", memory.read(0x2008, 4))[0],
            2.5,
            places=5,
        )
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("fpu_load_constant", operations)
        self.assertIn("fpu_y_log2_x", operations)
        self.assertIn("0.30102999566f", emitted)
        self.assertIn("std::log2", emitted)

        fpu_tangent = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D9F2"  # fptan
                "DDD8"  # fstp st(0), discard the pushed 1.0
                "D91D04200000"  # fstp dword [0x2004]
                "C3"
            ),
            base_address=0x3860,
            symbol="fpu_tangent",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", math.pi / 4.0),
            }
        )

        result = execute_lifted_function(fpu_tangent, state=state, memory=memory)
        emitted = emit_cpp(fpu_tangent, exported_symbol="fpu_tangent")

        self.assertEqual(fpu_tangent.instructions[1].mnemonic, "fptan")
        self.assertAlmostEqual(
            struct.unpack("<f", memory.read(0x2004, 4))[0],
            1.0,
            places=5,
        )
        self.assertEqual(result.state.fpu_stack, [])
        self.assertIn("fpu_partial_tangent", {
            event["operation"] for event in result.trace.to_list()
        })
        self.assertIn("std::tan", emitted)

        fpu_scalar = lift_x86_function(
            bytes.fromhex(
                "DB442440"  # fild dword [esp+0x40]
                "D80500200000"  # fadd dword [0x2000]
                "D91D04200000"  # fstp dword [0x2004]
                "C3"
            ),
            base_address=0x3830,
            symbol="fpu_scalar",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x9040: 3,
                0x2000: struct.pack("<f", 0.5),
            }
        )

        result = execute_lifted_function(fpu_scalar, state=state, memory=memory)
        emitted = emit_cpp(fpu_scalar, exported_symbol="fpu_scalar")

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_scalar.instructions[:3]],
            ["fild", "fadd", "fstp"],
        )
        self.assertEqual(struct.unpack("<f", memory.read(0x2004, 4))[0], 3.5)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("b2r_fpu_push", emitted)

        fpu_integer_subtract = lift_x86_function(
            bytes.fromhex(
                "DB442440"  # fild dword [esp+0x40]
                "DA65FC"  # fisub dword [ebp-4]
                "D91D04200000"  # fstp dword [0x2004]
                "C3"
            ),
            base_address=0x3840,
            symbol="fpu_integer_subtract",
        )
        state = CpuState.with_registers(ebp=0x9104, esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x9040: 10,
                0x9100: 3,
            }
        )

        result = execute_lifted_function(
            fpu_integer_subtract,
            state=state,
            memory=memory,
        )
        emitted = emit_cpp(
            fpu_integer_subtract,
            exported_symbol="fpu_integer_subtract",
        )

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_integer_subtract.instructions[:3]],
            ["fild", "fisub", "fstp"],
        )
        self.assertEqual(struct.unpack("<f", memory.read(0x2004, 4))[0], 7.0)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("static_cast<int32_t>", emitted)

        fpu_integer_multiply = lift_x86_function(
            bytes.fromhex(
                "D9442404"  # fld dword [esp+4]
                "DA4C2408"  # fimul dword [esp+8]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x00082E71,
            symbol="fpu_integer_multiply",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x9004: struct.pack("<f", 1.5),
                0x9008: -3,
            }
        )

        result = execute_lifted_function(
            fpu_integer_multiply,
            state=state,
            memory=memory,
        )
        emitted = emit_cpp(
            fpu_integer_multiply,
            exported_symbol="fpu_integer_multiply",
        )

        self.assertEqual(
            [
                instruction.mnemonic
                for instruction in fpu_integer_multiply.instructions[:3]
            ],
            ["fld", "fimul", "fstp"],
        )
        self.assertEqual(fpu_integer_multiply.instructions[1].address, 0x00082E75)
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], -4.5)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("*= static_cast<float>(static_cast<int32_t>", emitted)

        fpu_integer_divide = lift_x86_function(
            bytes.fromhex(
                "DB0500200000"  # fild dword [0x2000]
                "DA3504200000"  # fidiv dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3848,
            symbol="fpu_integer_divide",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: 10,
                0x2004: 4,
            }
        )

        result = execute_lifted_function(
            fpu_integer_divide,
            state=state,
            memory=memory,
        )
        emitted = emit_cpp(
            fpu_integer_divide,
            exported_symbol="fpu_integer_divide",
        )

        self.assertEqual(
            [
                instruction.mnemonic
                for instruction in fpu_integer_divide.instructions[:3]
            ],
            ["fild", "fidiv", "fstp"],
        )
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 2.5)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("/= static_cast<float>(static_cast<int32_t>", emitted)

        fld_scalar = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D80504200000"  # fadd dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3850,
            symbol="fld_scalar",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 1.25),
                0x2004: struct.pack("<f", 2.5),
            }
        )

        result = execute_lifted_function(fld_scalar, state=state, memory=memory)
        emitted = emit_cpp(fld_scalar, exported_symbol="fld_scalar")

        self.assertEqual(
            [instruction.mnemonic for instruction in fld_scalar.instructions[:3]],
            ["fld", "fadd", "fstp"],
        )
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 3.75)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("b2r_read_f32", emitted)

        fmul_scalar = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D80D04200000"  # fmul dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3858,
            symbol="fmul_scalar",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 1.5),
                0x2004: struct.pack("<f", 4.0),
            }
        )

        result = execute_lifted_function(fmul_scalar, state=state, memory=memory)
        emitted = emit_cpp(fmul_scalar, exported_symbol="fmul_scalar")

        self.assertEqual(
            [instruction.mnemonic for instruction in fmul_scalar.instructions[:3]],
            ["fld", "fmul", "fstp"],
        )
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 6.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("*= b2r_read_f32", emitted)

        fsub_scalar = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D82504200000"  # fsub dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3860,
            symbol="fsub_scalar",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 7.0),
                0x2004: struct.pack("<f", 2.25),
            }
        )

        result = execute_lifted_function(fsub_scalar, state=state, memory=memory)
        emitted = emit_cpp(fsub_scalar, exported_symbol="fsub_scalar")

        self.assertEqual(
            [instruction.mnemonic for instruction in fsub_scalar.instructions[:3]],
            ["fld", "fsub", "fstp"],
        )
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 4.75)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("-= b2r_read_f32", emitted)

        fdivr_scalar = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D83D04200000"  # fdivr dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3870,
            symbol="fdivr_scalar",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 4.0),
                0x2004: struct.pack("<f", 20.0),
            }
        )

        result = execute_lifted_function(fdivr_scalar, state=state, memory=memory)
        emitted = emit_cpp(fdivr_scalar, exported_symbol="fdivr_scalar")

        self.assertEqual(
            [instruction.mnemonic for instruction in fdivr_scalar.instructions[:3]],
            ["fld", "fdivr", "fstp"],
        )
        self.assertEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 5.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("/ ctx->fpu_stack[0]", emitted)

        fdiv_zero = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D83504200000"  # fdiv dword [0x2004]
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3878,
            symbol="fdiv_zero",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 4.0),
                0x2004: struct.pack("<f", 0.0),
            }
        )

        result = execute_lifted_function(fdiv_zero, state=state, memory=memory)
        emitted = emit_cpp(fdiv_zero, exported_symbol="fdiv_zero")

        self.assertEqual(
            [instruction.mnemonic for instruction in fdiv_zero.instructions[:3]],
            ["fld", "fdiv", "fstp"],
        )
        self.assertTrue(math.isinf(struct.unpack("<f", memory.read(0x2008, 4))[0]))
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("/= b2r_read_f32", emitted)

        fpu_compare = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D81504200000"  # fcom dword [0x2004]
                "D81D08200000"  # fcomp dword [0x2008]
                "DFE0"  # fnstsw ax
                "C3"
            ),
            base_address=0x38C0,
            symbol="fpu_compare",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 3.0),
                0x2004: struct.pack("<f", 2.0),
                0x2008: struct.pack("<f", 3.0),
            }
        )

        result = execute_lifted_function(fpu_compare, state=state, memory=memory)
        emitted = emit_cpp(fpu_compare, exported_symbol="fpu_compare")
        trace = result.trace.to_list()

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_compare.instructions[:4]],
            ["fld", "fcom", "fcomp", "fnstsw"],
        )
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.state.fpu_status_word, 0x4000)
        self.assertEqual(result.state.get_register("eax") & 0xFFFF, 0x4000)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        compare_events = [
            event for event in trace if event["operation"] in {"fpu_compare", "fpu_compare_pop"}
        ]
        self.assertEqual(
            [event["operation"] for event in compare_events],
            ["fpu_compare", "fpu_compare_pop"],
        )
        self.assertEqual(
            [event["details"]["comparison"] for event in compare_events],
            ["greater", "equal"],
        )
        self.assertIn("b2r_fpu_compare_status", emitted)
        self.assertIn("fpu_status_word", emitted)
        self.assertIn("b2r_fpu_pop", emitted)

        fpu_compare_pop_twice = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "DED9"  # fcompp
                "DFE0"  # fnstsw ax
                "C3"
            ),
            base_address=0x38D0,
            symbol="fpu_compare_pop_twice",
        )
        state = CpuState.with_registers(
            esp=0x9000,
            fpu_status_word=0x4000,
        )
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 3.0),
                0x2004: struct.pack("<f", 2.0),
            }
        )

        result = execute_lifted_function(
            fpu_compare_pop_twice,
            state=state,
            memory=memory,
        )
        emitted = emit_cpp(
            fpu_compare_pop_twice,
            exported_symbol="fpu_compare_pop_twice",
        )
        compare_event = next(
            event
            for event in result.trace.to_list()
            if event["operation"] == "fpu_compare_pop_twice"
        )

        self.assertEqual(
            [
                instruction.mnemonic
                for instruction in fpu_compare_pop_twice.instructions[:4]
            ],
            ["fld", "fld", "fcompp", "fnstsw"],
        )
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.state.fpu_status_word, 0x0100)
        self.assertEqual(result.state.get_register("eax") & 0xFFFF, 0x0100)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(compare_event["details"]["comparison"], "less")
        self.assertEqual(compare_event["details"]["stack_depth"], 0)
        self.assertIn(
            "b2r_fpu_compare_status(ctx->fpu_stack[0], ctx->fpu_stack[1]",
            emitted,
        )
        self.assertEqual(emitted.count("(void)b2r_fpu_pop(ctx);"), 2)

        fpu_add_pop = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "DEC1"  # faddp st(1), st
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x38E0,
            symbol="fpu_add_pop",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 2.25),
                0x2004: struct.pack("<f", 3.5),
            }
        )

        result = execute_lifted_function(fpu_add_pop, state=state, memory=memory)
        emitted = emit_cpp(fpu_add_pop, exported_symbol="fpu_add_pop")

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_add_pop.instructions[:4]],
            ["fld", "fld", "faddp", "fstp"],
        )
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 5.75)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("+= ctx->fpu_stack[0]", emitted)

        fpu_sign_multiply_pop = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D9E0"  # fchs
                "D90504200000"  # fld dword [0x2004]
                "DEC9"  # fmulp st(1), st
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3900,
            symbol="fpu_sign_multiply_pop",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 4.0),
                0x2004: struct.pack("<f", 2.0),
            }
        )

        result = execute_lifted_function(
            fpu_sign_multiply_pop,
            state=state,
            memory=memory,
        )
        emitted = emit_cpp(
            fpu_sign_multiply_pop,
            exported_symbol="fpu_sign_multiply_pop",
        )

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_sign_multiply_pop.instructions[:5]],
            ["fld", "fchs", "fld", "fmulp", "fstp"],
        )
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], -8.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("-ctx->fpu_stack[0]", emitted)
        self.assertIn("*= ctx->fpu_stack[0]", emitted)

        fpu_double_top = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "DCC0"  # fadd st(0), st(0)
                "D91D04200000"  # fstp dword [0x2004]
                "C3"
            ),
            base_address=0x3910,
            symbol="fpu_double_top",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 3.0),
            }
        )

        result = execute_lifted_function(fpu_double_top, state=state, memory=memory)
        emitted = emit_cpp(fpu_double_top, exported_symbol="fpu_double_top")

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_double_top.instructions[:3]],
            ["fld", "fadd_st", "fstp"],
        )
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2004, 4))[0], 6.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn(
            "ctx->fpu_stack[0u] + ctx->fpu_stack[0]",
            emitted,
        )

        fpu_add_float64 = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "DC0508200000"  # fadd qword [0x2008]
                "DC0D10200000"  # fmul qword [0x2010]
                "D91D04200000"  # fstp dword [0x2004]
                "C3"
            ),
            base_address=0x3930,
            symbol="fpu_add_float64",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 1.25),
                0x2008: struct.pack("<d", 2.5),
                0x2010: struct.pack("<d", 2.0),
            }
        )

        result = execute_lifted_function(fpu_add_float64, state=state, memory=memory)
        emitted = emit_cpp(fpu_add_float64, exported_symbol="fpu_add_float64")

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_add_float64.instructions[:4]],
            ["fld", "fadd", "fmul", "fstp"],
        )
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2004, 4))[0], 7.5)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("b2r_read_f64", emitted)

        fpu_integer64 = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "DF7C2408"  # fistp qword [esp+8]
                "DF6C2408"  # fild qword [esp+8]
                "D91D04200000"  # fstp dword [0x2004]
                "C3"
            ),
            base_address=0x387C,
            symbol="fpu_integer64",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 448.0),
            }
        )

        result = execute_lifted_function(fpu_integer64, state=state, memory=memory)
        emitted = emit_cpp(fpu_integer64, exported_symbol="fpu_integer64")

        self.assertEqual(
            [instruction.mnemonic for instruction in fpu_integer64.instructions[:4]],
            ["fld", "fistp", "fild", "fstp"],
        )
        self.assertEqual(struct.unpack("<q", memory.read(0x9008, 8))[0], 448)
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2004, 4))[0], 448.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("b2r_write_i64", emitted)
        self.assertIn("b2r_read_i64", emitted)

        timestamp = lift_x86_function(
            bytes.fromhex("0F31C3"),
            base_address=0x3840,
            symbol="timestamp_counter",
        )
        state = CpuState.with_registers(
            esp=0x9000,
            timestamp_counter=0x00000001_FFFFFFF0,
        )
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(timestamp, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0xFFFFFFF0)
        self.assertEqual(result.state.get_register("edx"), 0x00000001)
        self.assertEqual(
            result.state.timestamp_counter,
            0x00000001_FFFFFFF0 + DETERMINISTIC_TSC_STEP,
        )
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_x87_double_memory_load_and_store_pop(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "DD0500200000"  # fld qword [0x2000]
                "DD1D08200000"  # fstp qword [0x2008]
                "C3"
            ),
            base_address=0x3A80,
            symbol="x87_double_round_trip",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<d", 123.25),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="x87_double_round_trip")

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(struct.unpack("<d", memory.read(0x2008, 8))[0], 123.25)
        self.assertIn("b2r_read_f64", emitted)
        self.assertIn("b2r_write_f64", emitted)

    def test_observed_x87_exponential_sequence_operations(self) -> None:
        cases = (
            ("D9E8C3", [], [1.0]),
            ("D9EEC3", [], [0.0]),
            ("D9FCC3", [2.6], [3.0]),
            ("D9FDC3", [1.5, 2.0], [6.0, 2.0]),
            ("D9F0C3", [0.5], [math.sqrt(2.0) - 1.0]),
            ("D9FAC3", [4.0], [2.0]),
            ("D9FFC3", [0.0], [1.0]),
        )
        for index, (code, initial_stack, expected_stack) in enumerate(cases):
            with self.subTest(code=code):
                function = lift_x86_function(
                    bytes.fromhex(code),
                    base_address=0x3AA0 + index * 0x10,
                    symbol=f"x87_exponential_{index}",
                )
                state = CpuState.with_registers(esp=0x9000)
                state.fpu_stack = list(initial_stack)
                memory = SparseMemory({0x9000: 0xDEADC0DE})

                result = execute_lifted_function(
                    function,
                    state=state,
                    memory=memory,
                )

                self.assertEqual(result.return_address, 0xDEADC0DE)
                self.assertEqual(len(result.state.fpu_stack), len(expected_stack))
                for actual, expected in zip(result.state.fpu_stack, expected_stack):
                    self.assertAlmostEqual(actual, expected)

        sequence = lift_x86_function(
            bytes.fromhex("D9E8D9FCD9FDD9F0C3"),
            base_address=0x3AF0,
            symbol="x87_exponential_sequence",
        )
        emitted = emit_cpp(sequence, exported_symbol="x87_exponential_sequence")
        self.assertEqual(
            [instruction.mnemonic for instruction in sequence.instructions[:4]],
            ["fld1", "frndint", "fscale", "f2xm1"],
        )
        self.assertIn("std::nearbyint", emitted)
        self.assertIn("std::ldexp", emitted)
        self.assertIn("std::exp2", emitted)

        constants = lift_x86_function(
            bytes.fromhex("D9E8D9E9D9EAD9EBD9ECD9EDD9EEC3"),
            base_address=0x3B00,
            symbol="x87_constant_family",
        )
        self.assertEqual(
            [instruction.mnemonic for instruction in constants.instructions[:7]],
            ["fld1", "fldl2t", "fldl2e", "fldpi", "fldlg2", "fldln2", "fldz"],
        )

        register_destination = lift_x86_function(
            bytes.fromhex("D9C0D9FCDCE1C3"),
            base_address=0x3B20,
            symbol="x87_exponential_register_destination",
        )
        state = CpuState.with_registers(esp=0x9000)
        state.fpu_stack = [1.75]
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            register_destination,
            state=state,
            memory=memory,
        )
        emitted = emit_cpp(
            register_destination,
            exported_symbol="x87_exponential_register_destination",
        )

        self.assertEqual(
            [
                instruction.mnemonic
                for instruction in register_destination.instructions[:3]
            ],
            ["fld", "frndint", "fsubr_st"],
        )
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.fpu_stack, [2.0, 0.25])
        self.assertIn(
            "ctx->fpu_stack[1u] = ctx->fpu_stack[0] - ctx->fpu_stack[1u]",
            emitted,
        )

        test_and_absolute = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D9E4"  # ftst
                "9B"  # wait
                "DD3D04200000"  # fnstsw word [0x2004]
                "D9E1"  # fabs
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3B30,
            symbol="x87_test_and_absolute",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", -2.5),
            }
        )

        result = execute_lifted_function(
            test_and_absolute,
            state=state,
            memory=memory,
        )
        emitted = emit_cpp(
            test_and_absolute,
            exported_symbol="x87_test_and_absolute",
        )

        self.assertEqual(
            [instruction.mnemonic for instruction in test_and_absolute.instructions[:6]],
            ["fld", "ftst", "fwait", "fnstsw", "fabs", "fstp"],
        )
        self.assertEqual(memory.read(0x2004, 2), struct.pack("<H", 0x0100))
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 2.5)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("b2r_fpu_compare_status(ctx->fpu_stack[0], 0.0f", emitted)
        self.assertIn("std::fabs(ctx->fpu_stack[0])", emitted)

    def test_observed_x87_stack_register_arithmetic_and_pop_forms(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "D8E1"  # fsub st(1)
                "D9C1"  # fld st(1)
                "DEF9"  # fdivp st(1), st
                "D9C9"  # fxch st(1)
                "D8E9"  # fsubr st(1)
                "DEE9"  # fsubp st(1), st
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x3880,
            symbol="x87_stack_arithmetic",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 4.0),
                0x2004: struct.pack("<f", 10.0),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="x87_stack_arithmetic")

        self.assertEqual(
            [instruction.mnemonic for instruction in function.instructions[:8]],
            ["fld", "fld", "fsub", "fld", "fdivp", "fxch", "fsubr", "fsubp"],
        )
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 4.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("ctx->fpu_stack[1u] - ctx->fpu_stack[0]", emitted)
        self.assertIn("ctx->fpu_stack[1u] /= ctx->fpu_stack[0]", emitted)
        self.assertIn("ctx->fpu_stack[1u] -= ctx->fpu_stack[0]", emitted)

    def test_observed_fsubrp_reverse_subtracts_and_pops(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "DEE1"  # fsubrp st(1), st
                "D91D08200000"  # fstp dword [0x2008]
                "C3"
            ),
            base_address=0x00062561,
            symbol="observed_fsubrp",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 4.0),
                0x2004: struct.pack("<f", 10.0),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="observed_fsubrp")
        reverse_subtract = next(
            event
            for event in result.trace.to_list()
            if event["operation"] == "fpu_reverse_subtract_pop"
        )

        self.assertEqual(
            [instruction.mnemonic for instruction in function.instructions[:4]],
            ["fld", "fld", "fsubrp", "fstp"],
        )
        self.assertEqual(function.instructions[2].address, 0x0006256D)
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2008, 4))[0], 6.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(reverse_subtract["details"]["top"], "10.0")
        self.assertEqual(reverse_subtract["details"]["target"], "4.0")
        self.assertEqual(reverse_subtract["details"]["result"], "6.0")
        self.assertEqual(reverse_subtract["details"]["stack_depth"], 1)
        self.assertIn(
            "ctx->fpu_stack[1u] = ctx->fpu_stack[0] - ctx->fpu_stack[1u]",
            emitted,
        )
        self.assertEqual(emitted.count("b2r_fpu_pop(ctx)"), 2)

    def test_observed_x87_stack_store_and_stack_pop_forms(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D90500200000"  # fld dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "D90508200000"  # fld dword [0x2008]
                "D8C2"  # fadd st(2)
                "D8C9"  # fmul st(1)
                "D8F1"  # fdiv st(1)
                "D9150C200000"  # fst dword [0x200c]
                "DDD8"  # fstp st(0)
                "DDD9"  # fstp st(1)
                "D91D10200000"  # fstp dword [0x2010]
                "C3"
            ),
            base_address=0x38A0,
            symbol="x87_stack_store_pop",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 2.0),
                0x2004: struct.pack("<f", 3.0),
                0x2008: struct.pack("<f", 4.0),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="x87_stack_store_pop")

        self.assertEqual(
            [instruction.mnemonic for instruction in function.instructions[:9]],
            ["fld", "fld", "fld", "fadd", "fmul", "fdiv", "fst", "fstp", "fstp"],
        )
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x200C, 4))[0], 6.0)
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2010, 4))[0], 3.0)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("ctx->fpu_stack[2u]", emitted)
        self.assertIn("ctx->fpu_stack[0] /= ctx->fpu_stack[1u]", emitted)
        self.assertIn("ctx->fpu_stack[1u] = ctx->fpu_stack[0]", emitted)

    def test_observed_mxcsr_store_modify_and_load_decode_and_execute(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "0FAE5C2408"  # stmxcsr [esp+8]
                "8B442408"  # mov eax, [esp+8]
                "0D00800000"  # or eax, 0x8000
                "89442408"  # mov [esp+8], eax
                "0FAE542408"  # ldmxcsr [esp+8]
                "C3"
            ),
            base_address=0x3840,
            symbol="mxcsr_mode_update",
        )
        state = CpuState.with_registers(esp=0x9000, mxcsr=0x00001F80)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="mxcsr_mode_update")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(function.instructions[0].text(), "stmxcsr [esp + 0x8]")
        self.assertEqual(function.instructions[4].text(), "ldmxcsr [esp + 0x8]")
        self.assertEqual(result.state.mxcsr, 0x00009F80)
        self.assertEqual(memory.read_u32(0x9008), 0x00009F80)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("sse_store_mxcsr", operations)
        self.assertIn("sse_load_mxcsr", operations)
        self.assertIn("ctx->mxcsr", emitted)

    def test_observed_sse_scalar_truncate_convert_decode_and_execute(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "F30F2C442408"  # cvttss2si eax, dword [esp+8]
                "F30F2C4C240C"  # cvttss2si ecx, dword [esp+0xc]
                "C3"
            ),
            base_address=0x38F0,
            symbol="sse_truncate_scalar",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x9008: struct.pack("<f", 3.75),
                0x900C: struct.pack("<f", -3.75),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="sse_truncate_scalar")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(
            [instruction.mnemonic for instruction in function.instructions[:2]],
            ["cvttss2si", "cvttss2si"],
        )
        self.assertEqual(result.state.get_register("eax"), 3)
        self.assertEqual(result.state.get_register("ecx"), 0xFFFFFFFD)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("sse_truncate_scalar_float_to_i32", operations)
        self.assertIn("b2r_cvttss2si", emitted)

    def test_observed_sse_packed_float_math_decode_and_execute(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "0F280500200000"  # movaps xmm0, [0x2000]
                "0FC6C055"  # shufps xmm0, xmm0, 0x55
                "0F590510200000"  # mulps xmm0, [0x2010]
                "0F580520200000"  # addps xmm0, [0x2020]
                "0F290530200000"  # movaps [0x2030], xmm0
                "C3"
            ),
            base_address=0x3920,
            symbol="sse_packed_float_math",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<4f", 1.0, 2.0, 3.0, 4.0),
                0x2010: struct.pack("<4f", 10.0, 20.0, 30.0, 40.0),
                0x2020: struct.pack("<4f", 1.0, 1.0, 1.0, 1.0),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="sse_packed_float_math")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(
            [instruction.mnemonic for instruction in function.instructions[:5]],
            ["movaps", "shufps", "mulps", "addps", "movaps"],
        )
        self.assertEqual(
            struct.unpack("<4f", memory.read(0x2030, 16)),
            (21.0, 41.0, 61.0, 81.0),
        )
        self.assertEqual(
            result.state.get_xmm_register("xmm0"),
            (21.0, 41.0, 61.0, 81.0),
        )
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("xmm_shuffle_packed_float", operations)
        self.assertIn("xmm_multiply_packed_float", operations)
        self.assertIn("xmm_add_packed_float", operations)
        self.assertIn("b2r_xmm_shuffle", emitted)
        self.assertIn("b2r_write_xmm", emitted)

    def test_sqrtps_decodes_and_computes_all_packed_lanes(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0F510500200000C3"),
            base_address=0x3950,
            symbol="sse_packed_sqrt",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<4f", 1.0, 4.0, 9.0, 16.0),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="sse_packed_sqrt")

        self.assertEqual(function.instructions[0].mnemonic, "sqrtps")
        self.assertEqual(result.state.get_xmm_register("xmm0"), (1.0, 2.0, 3.0, 4.0))
        self.assertIn("b2r_xmm_sqrt", emitted)

    def test_observed_sse_packed_half_interleave_and_move_forms(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "0F280500200000"  # movaps xmm0, [0x2000]
                "0F280D10200000"  # movaps xmm1, [0x2010]
                "0F14C1"  # unpcklps xmm0, xmm1
                "0F15C9"  # unpckhps xmm1, xmm1
                "0F16C1"  # movlhps xmm0, xmm1
                "0F12C8"  # movhlps xmm1, xmm0
                "0F130520200000"  # movlps [0x2020], xmm0
                "0F170528200000"  # movhps [0x2028], xmm0
                "C3"
            ),
            base_address=0x3960,
            symbol="sse_packed_half_forms",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<4f", 1.0, 2.0, 3.0, 4.0),
                0x2010: struct.pack("<4f", 5.0, 6.0, 7.0, 8.0),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="sse_packed_half_forms")

        self.assertEqual(
            [instruction.mnemonic for instruction in function.instructions[2:6]],
            ["unpcklps", "unpckhps", "movlhps", "movhlps"],
        )
        self.assertEqual(result.state.get_xmm_register("xmm0"), (1.0, 5.0, 7.0, 7.0))
        self.assertEqual(result.state.get_xmm_register("xmm1"), (7.0, 7.0, 8.0, 8.0))
        self.assertEqual(struct.unpack("<2f", memory.read(0x2020, 8)), (1.0, 5.0))
        self.assertEqual(struct.unpack("<2f", memory.read(0x2028, 8)), (7.0, 7.0))
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("b2r_xmm_unpack_low", emitted)
        self.assertIn("b2r_xmm_move_high_to_low", emitted)

    def test_observed_sse_scalar_move_and_rsqrt_decode_and_execute(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "F30F100500200000"  # movss xmm0, [0x2000]
                "0F290510200000"  # movaps [0x2010], xmm0
                "F30F52C0"  # rsqrtss xmm0, xmm0
                "F30F110520200000"  # movss [0x2020], xmm0
                "C3"
            ),
            base_address=0x3940,
            symbol="sse_scalar_move_rsqrt",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory(
            {
                0x9000: 0xDEADC0DE,
                0x2000: struct.pack("<f", 4.0),
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="sse_scalar_move_rsqrt")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(
            [instruction.mnemonic for instruction in function.instructions[:4]],
            ["movss", "movaps", "rsqrtss", "movss"],
        )
        self.assertEqual(
            struct.unpack("<4f", memory.read(0x2010, 16)),
            (4.0, 0.0, 0.0, 0.0),
        )
        self.assertAlmostEqual(struct.unpack("<f", memory.read(0x2020, 4))[0], 0.5)
        self.assertEqual(result.state.get_xmm_register("xmm0")[0], 0.5)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("xmm_reciprocal_sqrt_scalar", operations)
        self.assertIn("b2r_rsqrtss", emitted)

    def test_observed_cache_writeback_invalidate_decodes_as_noop(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0F09C3"),
            base_address=0x3860,
            symbol="cache_writeback_invalidate",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="cache_writeback_invalidate")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(function.instructions[0].text(), "wbinvd")
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("cache_writeback_invalidate", operations)
        self.assertIn("case 0x00003860u", emitted)

    def test_observed_prefetch_hint_decodes_as_traced_noop(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0F1808C3"),
            base_address=0x3864,
            symbol="cache_prefetch",
        )
        state = CpuState.with_registers(eax=0x12340000, esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="cache_prefetch")
        trace = result.trace.to_list()
        prefetch_events = [
            event for event in trace if event["operation"] == "cache_prefetch"
        ]

        self.assertEqual(function.instructions[0].text(), "prefetcht0 [eax]")
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(prefetch_events[0]["details"]["memory_address"], 0x12340000)
        self.assertIn("eip = 0x00003867u;", emitted)

    def test_observed_mmx_qword_copy_decodes_and_executes(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0F6F060FE7070F77C3"),
            base_address=0x3867,
            symbol="mmx_qword_copy",
        )
        state = CpuState.with_registers(esi=0x2000, edi=0x3000, esp=0x9000)
        memory = SparseMemory(
            {
                0x2000: bytes.fromhex("8877665544332211"),
                0x9000: 0xDEADC0DE,
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="mmx_qword_copy")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(function.instructions[0].text(), "movq mm0, [esi]")
        self.assertEqual(function.instructions[1].text(), "movntq [edi], mm0")
        self.assertEqual(memory.read(0x3000, 8), bytes.fromhex("8877665544332211"))
        self.assertEqual(result.state.get_mmx_register("mm0"), 0x1122334455667788)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("mmx_empty_state", operations)
        self.assertIn("b2r_read_u64", emitted)
        self.assertIn("b2r_write_u64", emitted)

    def test_observed_store_fence_decodes_as_noop(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("0FAEF8C3"),
            base_address=0x3868,
            symbol="store_fence",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(function.instructions[0].text(), "sfence")
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("store_fence", operations)

    def test_observed_byte_port_write_decodes_as_traced_noop(self) -> None:
        function = lift_x86_function(
            bytes.fromhex("BAC0800000B05AEEC3"),
            base_address=0x3870,
            symbol="byte_port_write",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)
        emitted = emit_cpp(function, exported_symbol="byte_port_write")
        port_events = [
            event
            for event in result.trace.to_list()
            if event["operation"] == "port_write"
        ]

        self.assertEqual(function.instructions[2].text(), "out dx, al")
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(port_events[0]["details"]["port"], 0x80C0)
        self.assertEqual(port_events[0]["details"]["value"], 0x5A)
        self.assertIn("ctx->edx & 0xffffu", emitted)

    def test_observed_byte_test_and_immediate_group_flags(self) -> None:
        byte_test = lift_x86_function(
            bytes.fromhex("B08084C0C3"),
            base_address=0x3900,
            symbol="byte_test",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_test, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0x80)
        self.assertFalse(result.state.flags.zf)
        self.assertTrue(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_accumulator_and = lift_x86_function(
            bytes.fromhex("B0F0240FC3"),
            base_address=0x3910,
            symbol="byte_accumulator_and",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            byte_accumulator_and, state=state, memory=memory
        )

        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertTrue(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_accumulator_sub = lift_x86_function(
            bytes.fromhex("B0102C11C3"),
            base_address=0x3920,
            symbol="byte_accumulator_sub",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            byte_accumulator_sub, state=state, memory=memory
        )

        self.assertEqual(result.state.get_register("eax"), 0xFF)
        self.assertTrue(result.state.flags.cf)
        self.assertTrue(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_cmp = lift_x86_function(
            bytes.fromhex("B01080F810C3"),
            base_address=0x3A00,
            symbol="byte_cmp",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_cmp, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0x10)
        self.assertTrue(result.state.flags.zf)
        self.assertFalse(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_register_cmp = lift_x86_function(
            bytes.fromhex("B07F3C7F38C0C3"),
            base_address=0x3B00,
            symbol="byte_register_cmp",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            byte_register_cmp,
            state=state,
            memory=memory,
        )

        self.assertTrue(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_store = lift_x86_function(
            bytes.fromhex("C605002000005AC3"),
            base_address=0x3C00,
            symbol="byte_store",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_store, state=state, memory=memory)

        self.assertEqual(memory.read(0x2000, 1), b"\x5A")
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_group_test = lift_x86_function(
            bytes.fromhex("B080F6C080C3"),
            base_address=0x3D00,
            symbol="byte_group_test",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_group_test, state=state, memory=memory)

        self.assertFalse(result.state.flags.zf)
        self.assertTrue(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        ff_dec = lift_x86_function(
            bytes.fromhex("B801000000FFC8C3"),
            base_address=0x3E00,
            symbol="ff_dec",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(ff_dec, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        ff_inc = lift_x86_function(
            bytes.fromhex("B8FFFFFFFFFFC0C3"),
            base_address=0x3E10,
            symbol="ff_inc",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(ff_inc, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertTrue(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_operand_size_prefix_preserves_low_word_semantics(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "B8FFFF3412"  # mov eax, 0x1234FFFF
                "6683E0F8"  # and ax, -8
                "66C705002000000100"  # mov word [0x2000], 1
                "66890502200000"  # mov word [0x2002], ax
                "C3"
            ),
            base_address=0x3F00,
            symbol="word_operations",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0x1234FFF8)
        self.assertEqual(memory.read(0x2000, 4), b"\x01\x00\xF8\xFF")
        self.assertTrue(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_test_al_and_movsx_decode_and_execute(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "B8FE000000"  # mov eax, 0xFE
                "A880"  # test al, 0x80
                "0FBEC0"  # movsx eax, al
                "C3"
            ),
            base_address=0x4000,
            symbol="signed_byte_load",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0xFFFFFFFE)
        self.assertFalse(result.state.flags.zf)
        self.assertTrue(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_word_movzx_and_group3_test_decode_and_execute(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "B8EFBEADDE"  # mov eax, 0xDEADBEEF
                "0FB70500200000"  # movzx eax, word [0x2000]
                "F7C000800000"  # test eax, 0x8000
                "C3"
            ),
            base_address=0x4100,
            symbol="word_movzx_and_test",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x2000: b"\x34\x80", 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(function, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 0x8034)
        self.assertFalse(result.state.flags.zf)
        self.assertFalse(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_imul_neg_and_adc_decode_and_execute(self) -> None:
        multiply = lift_x86_function(
            bytes.fromhex(
                "BEFEFFFFFF"  # mov esi, -2
                "B807000000"  # mov eax, 7
                "0FAFF0"  # imul esi, eax
                "C3"
            ),
            base_address=0x4200,
            symbol="signed_multiply",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(multiply, state=state, memory=memory)

        self.assertEqual(result.state.get_register("esi"), 0xFFFFFFF2)
        self.assertFalse(result.state.flags.cf)
        self.assertFalse(result.state.flags.of)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        immediate_multiply = lift_x86_function(
            bytes.fromhex(
                "B9FEFFFFFF"  # mov ecx, -2
                "6BF903"  # imul edi, ecx, 3
                "C3"
            ),
            base_address=0x4210,
            symbol="signed_immediate_multiply",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            immediate_multiply, state=state, memory=memory
        )

        self.assertEqual(result.state.get_register("edi"), 0xFFFFFFFA)
        self.assertFalse(result.state.flags.cf)
        self.assertFalse(result.state.flags.of)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        unsigned_multiply = lift_x86_function(
            bytes.fromhex(
                "B800000080"  # mov eax, 0x80000000
                "B902000000"  # mov ecx, 2
                "F7E1"  # mul ecx
                "C3"
            ),
            base_address=0x4220,
            symbol="unsigned_multiply",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(unsigned_multiply, state=state, memory=memory)
        emitted = emit_cpp(unsigned_multiply, exported_symbol="unsigned_multiply")
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertEqual(result.state.get_register("edx"), 1)
        self.assertTrue(result.state.flags.cf)
        self.assertTrue(result.state.flags.of)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("unsigned_multiply", operations)
        self.assertIn("static_cast<uint64_t>(ctx->eax)", emitted)

        signed_wide_multiply = lift_x86_function(
            bytes.fromhex(
                "B8FDFFFFFF"  # mov eax, -3
                "B902000000"  # mov ecx, 2
                "F7E9"  # imul ecx
                "C3"
            ),
            base_address=0x4230,
            symbol="signed_wide_multiply",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            signed_wide_multiply, state=state, memory=memory
        )
        emitted = emit_cpp(
            signed_wide_multiply, exported_symbol="signed_wide_multiply"
        )

        self.assertEqual(result.state.get_register("eax"), 0xFFFFFFFA)
        self.assertEqual(result.state.get_register("edx"), 0xFFFFFFFF)
        self.assertFalse(result.state.flags.cf)
        self.assertFalse(result.state.flags.of)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("const int64_t product", emitted)

        negate_byte = lift_x86_function(
            bytes.fromhex("B101F6D9C3"),
            base_address=0x4300,
            symbol="negate_byte",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(negate_byte, state=state, memory=memory)

        self.assertEqual(result.state.get_register("ecx"), 0xFF)
        self.assertTrue(result.state.flags.cf)
        self.assertTrue(result.state.flags.sf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        add_with_carry = lift_x86_function(
            bytes.fromhex(
                "B8FFFFFFFF"  # mov eax, 0xFFFFFFFF
                "BB01000000"  # mov ebx, 1
                "13C3"  # adc eax, ebx
                "C3"
            ),
            base_address=0x4400,
            symbol="add_with_carry",
        )
        state = CpuState.with_registers(esp=0x9000)
        state.flags.cf = True
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(add_with_carry, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 1)
        self.assertTrue(result.state.flags.cf)
        self.assertFalse(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        subtract_with_borrow = lift_x86_function(
            bytes.fromhex(
                "B800000000"  # mov eax, 0
                "BBFFFFFFFF"  # mov ebx, 0xFFFFFFFF
                "1BC3"  # sbb eax, ebx
                "C3"
            ),
            base_address=0x4500,
            symbol="subtract_with_borrow",
        )
        state = CpuState.with_registers(esp=0x9000)
        state.flags.cf = True
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            subtract_with_borrow,
            state=state,
            memory=memory,
        )

        self.assertEqual(result.state.get_register("eax"), 0)
        self.assertTrue(result.state.flags.cf)
        self.assertTrue(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

    def test_observed_variable_shift_stosb_byte_inc_and_interrupt_decode(self) -> None:
        variable_shift = lift_x86_function(
            bytes.fromhex(
                "B001"  # mov al, 1
                "B102"  # mov cl, 2
                "D3E0"  # shl eax, cl
                "B203"  # mov dl, 3
                "D2E2"  # shl dl, cl
                "C0E201"  # shl dl, 1
                "C3"
            ),
            base_address=0x4700,
            symbol="variable_shift",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(variable_shift, state=state, memory=memory)

        self.assertEqual(result.state.get_register("eax"), 4)
        self.assertEqual(result.state.get_register("edx"), 24)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        bit_scan = lift_x86_function(
            bytes.fromhex(
                "B820000000"  # mov eax, 0x20
                "0FBCC8"  # bsf ecx, eax
                "C3"
            ),
            base_address=0x4710,
            symbol="bit_scan",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(bit_scan, state=state, memory=memory)

        self.assertEqual(result.state.get_register("ecx"), 5)
        self.assertFalse(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        byte_logic = lift_x86_function(
            bytes.fromhex(
                "B06F"  # mov al, 'o'
                "04E0"  # add al, -0x20
                "2C20"  # sub al, 0x20
                "0C30"  # or al, 0x30
                "B133"  # mov cl, 0x33
                "08C8"  # or al, cl
                "30C8"  # xor al, cl
                "32C1"  # xor al, cl
                "C3"
            ),
            base_address=0x4720,
            symbol="byte_logic",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_logic, state=state, memory=memory)
        emitted = emit_cpp(byte_logic, exported_symbol="byte_logic")

        self.assertEqual(byte_logic.instructions[1].text(), "add al, 0x000000E0")
        self.assertEqual(result.state.get_register("eax"), 0x3F)
        self.assertFalse(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("+", emitted)
        self.assertIn("|", emitted)

        byte_inc = lift_x86_function(
            bytes.fromhex("B0FF88450FFE450FC3"),
            base_address=0x4800,
            symbol="byte_increment",
        )
        state = CpuState.with_registers(esp=0x9000, ebp=0x2000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(byte_inc, state=state, memory=memory)

        self.assertEqual(memory.read(0x200F, 1), b"\x00")
        self.assertTrue(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        store_bytes = lift_x86_function(
            bytes.fromhex("B05ABF00300000B903000000F3AAC3"),
            base_address=0x4900,
            symbol="store_bytes",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x9000: 0xDEADC0DE})

        result = execute_lifted_function(store_bytes, state=state, memory=memory)

        self.assertEqual(memory.read(0x3000, 3), b"ZZZ")
        self.assertEqual(result.state.get_register("edi"), 0x3003)
        self.assertEqual(result.state.get_register("ecx"), 0)
        self.assertEqual(result.return_address, 0xDEADC0DE)

        interrupt_block = lift_x86_block(
            bytes.fromhex("90CD2DCCC3"),
            base_address=0x4A00,
            symbol="interrupt_block",
        )
        self.assertEqual(interrupt_block.instruction_count, 2)
        self.assertEqual(interrupt_block.instructions[-1].mnemonic, "int")
        interrupt_source = emit_cpp(
            interrupt_block,
            exported_symbol="interrupt_block",
            resumable=True,
        )
        self.assertIn(
            "ctx->module_exit_reason = B2R_MODULE_EXIT_SOFTWARE_INTERRUPT;",
            interrupt_source,
        )

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

        resumable = emit_cpp(function, exported_symbol="sum_helper_loop", resumable=True)
        self.assertIn("ctx->eip != 0u ? ctx->eip", resumable)
        self.assertIn("ctx->yield_requested", resumable)
        self.assertIn("ctx->step_budget", resumable)
        self.assertIn("eip = 0x00002000u", resumable)
        self.assertNotIn("ctx->call(ctx->user, 0x00002000u, ctx);", resumable)
        self.assertIn("ctx->eip = return_address;", resumable)
        self.assertIn(
            "pending_module_exit_reason = B2R_MODULE_EXIT_RETURN;",
            resumable,
        )
        self.assertIn("b2r_begin_instruction(ctx, 0x00001000u)", resumable)
        self.assertNotIn("--ctx->steps", resumable)
        self.assertIn("ctx->module_exit_reason", resumable)
        self.assertIn("B2R_MODULE_EXIT_CALL", resumable)
        self.assertIn("eip = return_address;", resumable)
        self.assertNotIn("return return_address;", resumable)
        self.assertIn("b2r_read_u8(ctx, address + 1u)", resumable)
        self.assertIn("b2r_write_u8(ctx, address + 1u", resumable)
        self.assertIn("b2r_read_u32(ctx, address + 4u)", resumable)
        self.assertIn("b2r_write_u32(ctx, address + 4u", resumable)
        self.assertNotIn("ctx->read_u8(ctx->user, address + 1u)", resumable)
        self.assertNotIn("ctx->read_u32(ctx->user, address + 4u)", resumable)
        self.assertIn("b2r_cache_address(B2RContext* ctx", resumable)
        self.assertIn("ctx->cache_physical_aliases", resumable)
        self.assertIn("address >= 0xa0000000u", resumable)

        observed = emit_cpp(
            function,
            exported_symbol="sum_helper_observed",
            resumable=True,
            observer_addresses={function.base_address},
        )
        self.assertIn("ctx->observe(ctx->user, ctx)", observed)

        signed_divide = emit_cpp(
            lift_x86_function(
                bytes.fromhex("99F7F9C3"),
                base_address=0x3000,
                symbol="signed_divide",
            ),
            exported_symbol="signed_divide",
        )
        self.assertIn("ctx->edx = (ctx->eax & 0x80000000u)", signed_divide)
        self.assertIn("const int32_t divisor", signed_divide)

        shifted = emit_cpp(
            lift_x86_function(
                bytes.fromhex("C1E902C3"),
                base_address=0x3100,
                symbol="shift_right",
            ),
            exported_symbol="shift_right",
        )
        self.assertIn("const uint32_t count", shifted)
        self.assertIn("lhs >> count", shifted)

        movzx = emit_cpp(
            lift_x86_function(
                bytes.fromhex("640FB60524000000C3"),
                base_address=0x3200,
                symbol="fs_byte_probe",
            ),
            exported_symbol="fs_byte_probe",
        )
        self.assertIn("ctx->read_u8", movzx)
        self.assertIn("ctx->fs_base", movzx)

        stosd = emit_cpp(
            lift_x86_function(
                bytes.fromhex("F3ABC3"),
                base_address=0x3300,
                symbol="fill_buffer",
            ),
            exported_symbol="fill_buffer",
        )
        self.assertIn("ctx->write_u32", stosd)
        self.assertIn("--ctx->ecx;", stosd)

        setcc = emit_cpp(
            lift_x86_function(
                bytes.fromhex("0F95C0C3"),
                base_address=0x3400,
                symbol="set_not_equal",
            ),
            exported_symbol="set_not_equal",
        )
        self.assertIn("!ctx->flags.zf", setcc)
        self.assertIn("ctx->eax = (ctx->eax & 0xffffff00u)", setcc)

        word_operations = emit_cpp(
            lift_x86_function(
                bytes.fromhex("6683E0F866C7050020000001000FBEC0C3"),
                base_address=0x3F00,
                symbol="word_operations",
            ),
            exported_symbol="word_operations",
        )
        self.assertIn("b2r_write_u16", word_operations)
        self.assertIn("b2r_sign_extend", word_operations)
        self.assertIn("ctx->eax = (ctx->eax & 0xffff0000u)", word_operations)

        frontier_operations = emit_cpp(
            lift_x86_function(
                bytes.fromhex("0FAFF013C31BC30FBCC8F6D9F7D0C3"),
                base_address=0x4200,
                symbol="frontier_operations",
            ),
            exported_symbol="frontier_operations",
        )
        self.assertIn("b2r_signed_value", frontier_operations)
        self.assertIn("rhs_with_carry", frontier_operations)
        self.assertIn("rhs_with_borrow", frontier_operations)
        self.assertIn("cursor >>= 1u", frontier_operations)
        self.assertIn("b2r_sub_flags(ctx, 0u, lhs", frontier_operations)
        self.assertIn("~(", frontier_operations)

        timestamp = emit_cpp(
            lift_x86_function(
                bytes.fromhex("0F31C3"),
                base_address=0x3830,
                symbol="timestamp_counter",
            ),
            exported_symbol="timestamp_counter",
        )
        self.assertIn("ctx->timestamp_counter", timestamp)
        self.assertIn("ctx->edx = static_cast<uint32_t>(value >> 32)", timestamp)

    def test_reachable_coverage_batch_decodes_all_observed_forms(self) -> None:
        cases = {
            "FC": "cld",
            "FA": "cli",
            "F30F2DC0": "cvtss2si",
            "DEF1": "fdivrp",
            "DDC3": "ffree",
            "DA4640": "fiadd",
            "DB5C240C": "fistp",
            "DB2D20803400": "fld",
            "DD7108": "fnsave",
            "D9F3": "fpatan",
            "DD6108": "frstor",
            "EC": "in",
            "EA2C6D0E000800": "jmp_far",
            "F00FC102": "xadd",
            "0F5F0570635A00": "maxps",
            "0F5D05E0303400": "minps",
            "0F7F01": "movq",
            "0F1000": "movups",
            "D1D8": "rcr",
            "C1C910": "ror",
            "9E": "sahf",
            "1CFF": "sbb",
            "0F01442406": "sgdt",
            "0FADD0": "shrd",
            "FD": "std",
            "FB": "sti",
            "0FC101": "xadd",
            "86E0": "xchg",
            "91": "xchg",
            "3401": "xor",
        }

        for index, (bytes_hex, mnemonic) in enumerate(cases.items()):
            with self.subTest(bytes_hex=bytes_hex, mnemonic=mnemonic):
                function = X86Decoder().decode_function(
                    bytes.fromhex(bytes_hex),
                    base_address=0x5000 + index * 0x20,
                    symbol=f"coverage_{mnemonic}_{index}",
                    max_instructions=1,
                    stop_at_ret=False,
                )
                self.assertEqual(function.instructions[0].mnemonic, mnemonic)
                self.assertEqual(function.instructions[0].bytes_hex, bytes_hex)

    def test_batched_x87_frontiers_execute_with_stack_and_state_semantics(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "D9E8"  # fld1: y
                "D9E8"  # fld1: x
                "D9F3"  # fpatan
                "DA0500200000"  # fiadd dword [0x2000]
                "D90504200000"  # fld dword [0x2004]
                "DEF1"  # fdivrp st(1), st
                "DB1D08200000"  # fistp dword [0x2008]
                "DB2D10200000"  # fld tbyte [0x2010]
                "DD3500210000"  # fnsave [0x2100]
                "DD2500210000"  # frstor [0x2100]
                "DDC0"  # ffree st(0)
                "C3"
            ),
            base_address=0x6000,
            symbol="batched_x87_frontiers",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x2000: struct.pack("<i", 2),
                0x2004: struct.pack("<f", 8.0),
                0x2010: bytes.fromhex("00000000000000C00040"),
                0x2100: bytes(108),
                0x8000: 0xDEADC0DE,
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(memory.read_u32(0x2008), 3)
        self.assertEqual(result.state.fpu_stack, [])
        self.assertEqual(result.state.fpu_control_word, 0x037F)
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertIn("fpu_partial_arctangent", operations)
        self.assertIn("fpu_integer_add", operations)
        self.assertIn("fpu_reverse_divide_pop", operations)
        self.assertIn("fpu_save_state", operations)
        self.assertIn("fpu_restore_state", operations)
        self.assertIn("b2r_read_f80", emit_cpp(function))

    def test_batched_sse_and_mmx_frontiers_preserve_values(self) -> None:
        function = lift_x86_function(
            bytes.fromhex(
                "0F100500200000"  # movups xmm0, [0x2000]
                "0F5D0510200000"  # minps xmm0, [0x2010]
                "0F5F0520200000"  # maxps xmm0, [0x2020]
                "0F110530200000"  # movups [0x2030], xmm0
                "F30F2DC0"  # cvtss2si eax, xmm0
                "0F7F0540200000"  # movq [0x2040], mm0
                "C3"
            ),
            base_address=0x6100,
            symbol="batched_vector_frontiers",
        )
        state = CpuState.with_registers(esp=0x8000)
        state.set_mmx_register("mm0", 0x1122334455667788)
        memory = SparseMemory(
            {
                0x2000: struct.pack("<4f", 5.0, -2.0, float("nan"), -0.0),
                0x2010: struct.pack("<4f", 4.0, -3.0, 1.0, 0.0),
                0x2020: struct.pack("<4f", 6.0, -4.0, 2.0, -0.0),
                0x2030: bytes(16),
                0x2040: bytes(8),
                0x8000: 0xDEADC0DE,
            }
        )

        result = execute_lifted_function(function, state=state, memory=memory)
        vector = struct.unpack("<4f", memory.read(0x2030, 16))

        self.assertEqual(vector[:3], (6.0, -3.0, 2.0))
        self.assertLess(math.copysign(1.0, vector[3]), 0.0)
        self.assertEqual(result.state.get_register("eax"), 6)
        self.assertEqual(
            memory.read(0x2040, 8),
            bytes.fromhex("8877665544332211"),
        )
        self.assertEqual(result.return_address, 0xDEADC0DE)

        rounding = lift_x86_function(
            bytes.fromhex("F30F2DC0C3"),
            base_address=0x6200,
            symbol="cvtss2si_rounding",
        )
        round_state = CpuState.with_registers(esp=0x8100, mxcsr=0x00005F80)
        round_state.set_xmm_register("xmm0", (2.5, 0.0, 0.0, 0.0))
        round_result = execute_lifted_function(
            rounding,
            state=round_state,
            memory=SparseMemory({0x8100: 0xDEADC0DE}),
        )
        self.assertEqual(round_result.state.get_register("eax"), 3)

    def test_batched_integer_string_and_system_frontiers_execute(self) -> None:
        reverse_copy = lift_x86_function(
            bytes.fromhex("FDF3A5FCC3"),
            base_address=0x6300,
            symbol="reverse_copy",
        )
        copy_state = CpuState.with_registers(
            esi=0x3008,
            edi=0x4008,
            ecx=3,
            esp=0x8000,
        )
        copy_memory = SparseMemory(
            {
                0x3000: struct.pack("<III", 1, 2, 3),
                0x4000: bytes(12),
                0x8000: 0xDEADC0DE,
            }
        )
        copy_result = execute_lifted_function(
            reverse_copy,
            state=copy_state,
            memory=copy_memory,
        )
        self.assertEqual(copy_memory.read(0x4000, 12), struct.pack("<III", 1, 2, 3))
        self.assertEqual(copy_result.state.get_register("esi"), 0x2FFC)
        self.assertEqual(copy_result.state.get_register("edi"), 0x3FFC)
        self.assertFalse(copy_result.state.flags.df)

        arithmetic = lift_x86_function(
            bytes.fromhex(
                "C1C910"  # ror ecx, 16
                "66C1C808"  # ror ax, 8
                "D1D8"  # rcr eax, 1
                "C3"
            ),
            base_address=0x6400,
            symbol="batched_rotates",
        )
        arithmetic_state = CpuState.with_registers(
            eax=0xAABBCCDD,
            ecx=0x12345678,
            esp=0x8100,
        )
        arithmetic_state.flags.cf = True
        arithmetic_result = execute_lifted_function(
            arithmetic,
            state=arithmetic_state,
            memory=SparseMemory({0x8100: 0xDEADC0DE}),
        )
        self.assertEqual(arithmetic_result.state.get_register("ecx"), 0x56781234)
        self.assertEqual(arithmetic_result.state.get_register("eax"), 0xD55DEEE6)
        self.assertFalse(arithmetic_result.state.flags.cf)

        exchange_add = lift_x86_function(
            bytes.fromhex("F00FC102C3"),
            base_address=0x6500,
            symbol="locked_exchange_add",
        )
        exchange_state = CpuState.with_registers(eax=3, edx=0x5000, esp=0x8200)
        exchange_memory = SparseMemory({0x5000: 5, 0x8200: 0xDEADC0DE})
        exchange_result = execute_lifted_function(
            exchange_add,
            state=exchange_state,
            memory=exchange_memory,
        )
        self.assertEqual(exchange_memory.read_u32(0x5000), 8)
        self.assertEqual(exchange_result.state.get_register("eax"), 5)

        system = lift_x86_function(
            bytes.fromhex(
                "0F010500600000"  # sgdt [0x6000]
                "FA"  # cli
                "FB"  # sti
                "EC"  # in al, dx
                "EA117000000800"  # jmp 0x0008:0x00007011
                "C3"
            ),
            base_address=0x7000,
            symbol="batched_system_frontiers",
        )
        system_state = CpuState.with_registers(
            eax=0xFFFFFFFF,
            edx=0x80C0,
            esp=0x8300,
            gdtr_base=0x12345000,
            gdtr_limit=0x03FF,
        )
        system_memory = SparseMemory({0x6000: bytes(6), 0x8300: 0xDEADC0DE})
        system_result = execute_lifted_function(
            system,
            state=system_state,
            memory=system_memory,
        )
        self.assertEqual(system_memory.read(0x6000, 6), struct.pack("<HI", 0x03FF, 0x12345000))
        self.assertEqual(system_result.state.get_register("eax"), 0xFFFFFF00)
        self.assertTrue(system_result.state.flags.interrupt_enabled)
        self.assertEqual(system_result.state.cs_selector, 0x0008)
        self.assertEqual(system_result.return_address, 0xDEADC0DE)

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
