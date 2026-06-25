from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.recomp.x86_lifter import (
    CpuState,
    SparseMemory,
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

        timestamp = lift_x86_function(
            bytes.fromhex("0F31C3"),
            base_address=0x3830,
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
        self.assertEqual(result.state.timestamp_counter, 0x00000002_000B2F38)
        self.assertEqual(result.return_address, 0xDEADC0DE)

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
                "B00F"  # mov al, 0x0F
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

        self.assertEqual(result.state.get_register("eax"), 0x3F)
        self.assertFalse(result.state.flags.zf)
        self.assertEqual(result.return_address, 0xDEADC0DE)

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
        self.assertIn("ctx->ecx = 0u", stosd)

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
