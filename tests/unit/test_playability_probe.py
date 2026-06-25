from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from runtime.xbox.shims import XboxRuntimeConfig, XboxRuntimeShims, XboxStatus
from tests.unit.test_xbe_info import _synthetic_xbe
from tools.loader.xbe_loader import ImportResolver
from tools.playability.playability_probe import (
    DynamicBlockCache,
    RuntimeAbiBridge,
    XbeBackedSparseMemory,
    _asset_io_summary_from_invocations,
    _deterministic_service_validation_summary,
    _heap_free_list_boundary_from_step_limit,
    _recovered_render_command_stream,
    _scheduler_boundary_from_step_limit,
    build_playability_probe_summary,
)
from tools.recomp.x86_lifter import (
    CpuState,
    ExecutionTrace,
    LiftedFunction,
    Operand,
    SparseMemory,
    X86Instruction,
    execute_lifted_function,
    lift_x86_function,
)


def _call_indirect_bytes(pointer_address: int) -> bytes:
    return b"\xFF\x15" + struct.pack("<I", pointer_address) + b"\xC3"


def _jmp_indirect_bytes(pointer_address: int) -> bytes:
    return b"\xFF\x25" + struct.pack("<I", pointer_address)


def _push_u32(value: int) -> bytes:
    return b"\x68" + struct.pack("<I", value & 0xFFFFFFFF)


class PlayabilityProbeTests(unittest.TestCase):
    def test_xbe_backed_memory_clears_observed_nv2a_status_poll_bit(self) -> None:
        class DummyArena:
            def read(self, _address: int, _size: int) -> bytes:
                raise AssertionError("fallback arena should not be read")

        class DummyLoaded:
            arena = DummyArena()

        observed_writes: list[tuple[int, bytes]] = []
        memory = XbeBackedSparseMemory(
            DummyLoaded(),  # type: ignore[arg-type]
            write_observer=lambda address, payload: observed_writes.append(
                (address, payload)
            ),
        )

        memory.write_u32(0xFD100410, 0x00010000)

        self.assertEqual(observed_writes, [(0xFD100410, b"\x00\x00\x01\x00")])
        self.assertEqual(memory.read_u32(0xFD100410), 0)

    def test_runtime_abi_bridge_invokes_registered_kernel_import_target(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[127])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        function = lift_x86_function(
            _call_indirect_bytes(0x3000),
            base_address=0x1000,
            symbol="call_frequency",
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x3000: target, 0x8000: 0xDEADC0DE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=bridge.call_handlers(),
        )
        trace_operations = [event["operation"] for event in result.trace.to_list()]

        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("eax"), 10_000_000)
        self.assertEqual(len(bridge.invocations), 1)
        self.assertEqual(bridge.invocations[0].shim_name, "KeQueryPerformanceFrequency")
        self.assertIn("runtime_abi_call", trace_operations)

    def test_runtime_abi_bridge_cleans_observed_tv_encoder_guest_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[2])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        function = lift_x86_function(
            _push_u32(0x00225208)
            + _push_u32(0)
            + _push_u32(6)
            + _push_u32(0)
            + _call_indirect_bytes(0x3000),
            base_address=0x1000,
            symbol="send_tv_encoder_option_callsite",
        )
        state = CpuState.with_registers(esp=0x9000)
        memory = SparseMemory({0x3000: target, 0x9000: 0xDEADC0DE})

        result = execute_lifted_function(
            function,
            state=state,
            memory=memory,
            call_handlers=bridge.call_handlers(),
        )

        invocation = bridge.invocations[0]
        self.assertEqual(result.return_address, 0xDEADC0DE)
        self.assertEqual(result.state.get_register("esp"), 0x9004)
        self.assertEqual(invocation.shim_name, "AvSendTVEncoderOption")
        self.assertEqual(invocation.arguments, (0, 6, 0, 0x00225208))
        self.assertEqual(invocation.handler_arguments, (0, 6))
        self.assertEqual(invocation.stack_cleanup_bytes, 16)

    def test_runtime_abi_bridge_marshals_pci_space_read_buffer(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[46])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x7000: b"\xFF\xFF\xFF\xFF",
                0x8000: 0xDEADC0DE,
            }
        )
        arguments = (3, 0, 0x4C, 0x7000, 4, 0)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "HalReadWritePCISpace")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 24)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read(0x7000, 4), b"\x00\x00\x00\x00")
        self.assertEqual(memory.read_u32(0x8000 + 24), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["pci_read_buffer"],
        )

    def test_runtime_abi_bridge_marshals_pci_space_write_payload(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[46])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x7000: b"\x12\x34\x56\x78",
                0x8000: 0xDEADC0DE,
            }
        )
        arguments = (3, 0, 0x4C, 0x7000, 4, 1)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "HalReadWritePCISpace")
        self.assertEqual(invocation.stack_cleanup_bytes, 24)
        self.assertEqual(invocation.result["bytes_read"], 4)
        self.assertTrue(invocation.result["data_elided"])
        self.assertEqual(memory.read(0x7000, 4), b"\x12\x34\x56\x78")
        self.assertEqual(invocation.memory_writes, ())

    def test_runtime_abi_bridge_maps_ps_create_system_thread_ex_arguments(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[255])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        handle_pointer = 0x6000
        thread_id_pointer = 0x6004
        start_context1 = 0x11112222
        start_context2 = 0x33334444
        start_routine = 0x000E682C
        arguments = (
            handle_pointer,
            0x20,
            0x3000,
            0x40,
            thread_id_pointer,
            start_context1,
            start_context2,
            0,
            1,
            start_routine,
        )
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "PsCreateSystemThreadEx")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.return_kind, "status_dict")
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8000 + 40)
        self.assertEqual(memory.read_u32(0x8000 + 40), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(handle_pointer), 0x104)
        self.assertEqual(memory.read_u32(thread_id_pointer), 0x104)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["thread_handle", "thread_id"],
        )

    def test_runtime_abi_bridge_cleans_nonvolatile_setting_query(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[24])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x01, 0x6000, 0x6004, 4, 0x6008)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "ExQueryNonVolatileSetting")
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(0x6000), 4)
        self.assertEqual(memory.read_u32(0x6004), 0)
        self.assertEqual(memory.read_u32(0x6008), 4)

    def test_runtime_abi_bridge_cleans_kernel_timer_pointer_call(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.ke_initialize_timer_ex(0x5000, 0)
        runtime.register_kernel_imports(resolver, imported_ordinals=[149])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x5000, 0xB5659000, 0xFFFFFFCD, 0x5010)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "KeSetTimer")
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 16)
        self.assertEqual(invocation.return_kind, "bool")
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(memory.read_u32(0x8000 + 16), 0xDEADC0DE)

    def test_runtime_abi_bridge_cleans_contiguous_memory_ex_call(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[166])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x40000, 0, 0x03FFFFFF, 0, 0x404)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "MmAllocateContiguousMemoryEx")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.return_kind, "int")
        self.assertEqual(state.get_register("eax"), 0x20000000)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)

    def test_runtime_abi_bridge_maps_virtual_memory_out_parameters(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[184])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        base_pointer = 0x6000
        size_pointer = 0x6004
        arguments = (base_pointer, 0, size_pointer, 0x2000, 0x04)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        memory.write_u32(base_pointer, 0)
        memory.write_u32(size_pointer, 0x3000)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtAllocateVirtualMemory")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.return_kind, "status_dict")
        self.assertEqual(state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(state.get_register("esp"), 0x8000 + 20)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)
        self.assertEqual(memory.read_u32(base_pointer), 0x10000000)
        self.assertEqual(memory.read_u32(size_pointer), 0x3000)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["base_address", "region_size"],
        )

    def test_runtime_abi_bridge_preserves_virtual_memory_commit_base(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[184])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        reserve_base_pointer = 0x6000
        reserve_size_pointer = 0x6004
        commit_base_pointer = 0x6010
        commit_size_pointer = 0x6014
        memory = SparseMemory()
        memory.write_u32(reserve_base_pointer, 0)
        memory.write_u32(reserve_size_pointer, 0x4000)
        reserve_arguments = (
            reserve_base_pointer,
            0,
            reserve_size_pointer,
            0x2000,
            0x04,
        )
        reserve_state = CpuState.with_registers(esp=0x8000)
        memory.write_u32(0x8000, 0xDEADC0DE)
        for index, argument in enumerate(reserve_arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(reserve_state, memory, target, ExecutionTrace())

        reserved_base = memory.read_u32(reserve_base_pointer)
        requested_commit_base = reserved_base + 0x1000
        memory.write_u32(commit_base_pointer, requested_commit_base)
        memory.write_u32(commit_size_pointer, 0x1000)
        commit_arguments = (
            commit_base_pointer,
            0,
            commit_size_pointer,
            0x1000,
            0x04,
        )
        commit_state = CpuState.with_registers(esp=0x9000)
        memory.write_u32(0x9000, 0xFEEDC0DE)
        for index, argument in enumerate(commit_arguments):
            memory.write_u32(0x9004 + index * 4, argument)

        bridge.invoke(commit_state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[-1]
        self.assertEqual(invocation.shim_name, "NtAllocateVirtualMemory")
        self.assertEqual(invocation.arguments, commit_arguments)
        self.assertEqual(invocation.result["allocation_policy"], "commit_existing_reservation")
        self.assertEqual(invocation.result["allocated_address"], requested_commit_base)
        self.assertEqual(memory.read_u32(commit_base_pointer), requested_commit_base)
        self.assertEqual(memory.read_u32(commit_size_pointer), 0x1000)
        self.assertEqual(len(runtime.memory.allocations), 1)
        self.assertEqual(commit_state.get_register("eax"), XboxStatus.SUCCESS)
        self.assertEqual(commit_state.get_register("esp"), 0x9000 + 20)
        self.assertEqual(memory.read_u32(0x9000 + 20), 0xFEEDC0DE)

    def test_runtime_abi_bridge_reads_compare_memory_ulong_payload(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[269])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        arguments = (0x6000, 12, 0xA5A5A5A5)
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})
        memory.write(0x6000, b"\xA5" * 8 + b"\x00" * 4)
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "RtlCompareMemoryUlong")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertEqual(invocation.return_kind, "int")
        self.assertEqual(state.get_register("eax"), 8)
        self.assertEqual(memory.read_u32(0x8000 + 12), 0xDEADC0DE)

    def test_runtime_abi_bridge_preserves_stack_for_kf_lower_irql(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[161])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory({0x8000: 0xDEADC0DE})

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "KfLowerIrql")
        self.assertEqual(invocation.arguments, ())
        self.assertEqual(invocation.handler_arguments, ())
        self.assertEqual(invocation.stack_cleanup_bytes, 0)
        self.assertEqual(state.get_register("eax"), 0)
        self.assertEqual(state.get_register("esp"), 0x8000)
        self.assertEqual(memory.read_u32(0x8000), 0xDEADC0DE)

    def test_scheduler_boundary_classifies_step_limit_loop(self) -> None:
        state = CpuState.with_registers(eax=1, esi=8)
        trace_events = [
            {
                "sequence": 0,
                "address": 0xF29F0,
                "address_hex": "0x000F29F0",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x71000024,
                    "value": 0x40D,
                    "size": 4,
                },
            },
            {
                "address": 0xF2A10,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x71000024,
                    "value": 0x40D,
                },
            },
            {
                "address": 0xF2A14,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x410,
                },
            },
            {
                "address": 0xF2A1D,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000038,
                    "value": 0x00000008,
                },
            },
            {
                "address": 0xF2A22,
                "operation": "branch",
                "details": {
                    "taken": True,
                    "target": 0xF2A10,
                    "target_hex": "0x000F2A10",
                },
            }
        ]

        boundary = _scheduler_boundary_from_step_limit(
            trace_events,
            state,
            65536,
        )

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "scheduler_boundary")
        self.assertEqual(boundary["owner"], "guest_thread_scheduler")
        self.assertEqual(boundary["loop_entry_hex"], "0x000F2A10")
        self.assertEqual(boundary["steps"], 65536)
        self.assertEqual(
            boundary["scheduler_queue_scan"]["producer_consumer_hint"],
            "awaiting_work_item_matching_scan_key",
        )
        self.assertEqual(boundary["scheduler_queue_scan"]["scan_key_hex"], "0x0000040D")
        self.assertEqual(
            boundary["scheduler_queue_scan"]["scan_key_source_address_hex"],
            "0x71000024",
        )
        self.assertEqual(boundary["scheduler_queue_scan"]["next_node_hex"], "0x00000008")
        self.assertEqual(
            boundary["scheduler_queue_scan"]["current_node_base_hex"],
            "0x00000008",
        )
        self.assertTrue(
            boundary["scheduler_queue_scan"]["next_pointer_matches_current_node"]
        )
        self.assertFalse(
            boundary["scheduler_queue_scan"]["scan_key_matched_current_node"]
        )
        awaited = boundary["scheduler_queue_scan"]["awaited_work_item"]
        self.assertEqual(awaited["key_hex"], "0x0000040D")
        self.assertEqual(awaited["status"], "not_present_in_observed_scan")
        self.assertEqual(awaited["observed_node_self_loop"], True)
        self.assertEqual(awaited["loop_esi_hex"], "0x00000008")
        self.assertEqual(
            [read["label"] for read in boundary["scheduler_queue_scan"]["reads"]],
            ["scan_key", "current_node_key", "next_node"],
        )
        self.assertEqual(boundary["scheduler_queue_scan"]["producer_candidate_write_count"], 1)
        self.assertEqual(boundary["scheduler_queue_scan"]["observed_node_key_write_values"], [])
        self.assertEqual(
            boundary["scheduler_queue_scan"]["producer_candidate_writes"][0]["reasons"],
            ["watched_scheduler_address", "wrote_scan_key_value"],
        )
        producer_trace = boundary["scheduler_queue_scan"]["producer_trace"]
        self.assertEqual(
            producer_trace["status"],
            "awaited_key_seen_in_candidate_writes",
        )
        self.assertEqual(producer_trace["scan_key_value_write_count"], 1)
        self.assertEqual(producer_trace["candidate_instruction_group_count"], 1)
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["instruction_address_hex"],
            "0x000F29F0",
        )
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["roles"],
            ["scan_key_source_writer", "awaited_key_value_writer"],
        )
        self.assertEqual(
            producer_trace["producer_semantic_counts"],
            {"unknown_scheduler_writer": 1},
        )

    def test_scheduler_boundary_names_next_anchor_when_awaited_key_is_absent(self) -> None:
        state = CpuState.with_registers(edx=0x40D, esi=8, edi=0x3445C0)
        trace_events = [
            {
                "sequence": 10,
                "address": 0xF2A63,
                "address_hex": "0x000F2A63",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x401,
                    "size": 4,
                },
            },
            {
                "sequence": 11,
                "address": 0xF2AF9,
                "address_hex": "0x000F2AF9",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x00000038,
                    "value": 0x00000008,
                    "size": 4,
                },
            },
            {
                "sequence": 12,
                "address": 0xF29F6,
                "address_hex": "0x000F29F6",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x003445C0,
                    "size": 4,
                },
            },
            {
                "address": 0xF2A10,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x70FFFECC,
                    "value": 0x40D,
                },
            },
            {
                "address": 0xF2A14,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000010,
                    "value": 0x003445C0,
                },
            },
            {
                "address": 0xF2A1D,
                "operation": "memory_read",
                "details": {
                    "memory_address": 0x00000038,
                    "value": 0x00000008,
                },
            },
            {
                "address": 0xF2A22,
                "operation": "branch",
                "details": {
                    "taken": True,
                    "target": 0xF2A10,
                    "target_hex": "0x000F2A10",
                },
            },
        ]

        boundary = _scheduler_boundary_from_step_limit(trace_events, state, 70000)

        self.assertIsNotNone(boundary)
        assert boundary is not None
        scan = boundary["scheduler_queue_scan"]
        self.assertEqual(scan["awaited_work_item"]["status"], "not_present_in_observed_scan")
        self.assertEqual(scan["observed_node_key_write_values"], ["0x00000401", "0x003445C0"])
        self.assertEqual(scan["observed_next_node_write_values"], ["0x00000008"])
        producer_trace = scan["producer_trace"]
        self.assertEqual(
            producer_trace["status"],
            "awaited_key_not_seen_in_candidate_writes",
        )
        self.assertEqual(producer_trace["scan_key_value_write_count"], 0)
        self.assertFalse(producer_trace["awaited_key_enqueue_observed"])
        self.assertEqual(producer_trace["observed_work_item_key_values"], ["0x00000401"])
        self.assertEqual(
            producer_trace["key_flow"]["consumer_requested_key"]["stack_offset_hex"],
            "0x00000024",
        )
        self.assertEqual(
            producer_trace["key_flow"]["producer_enqueued_key"]["write_instruction_address_hex"],
            "0x000F2A63",
        )
        self.assertEqual(
            producer_trace["key_flow"]["producer_enqueued_key"]["observed_values_hex"],
            ["0x00000401"],
        )
        self.assertFalse(
            producer_trace["key_flow"]["awaited_key_enqueued_in_observed_trace"]
        )
        self.assertTrue(
            producer_trace["key_flow"]["producer_key_source_matches_consumer_offset"]
        )
        self.assertEqual(producer_trace["candidate_instruction_group_count"], 3)
        self.assertEqual(
            producer_trace["producer_semantic_counts"],
            {
                "queue_slot_replacement_writer": 1,
                "work_item_key_writer": 1,
                "work_item_next_link": 1,
            },
        )
        self.assertEqual(
            producer_trace["queue_node_layout"]["key_offset_hex"],
            "0x00000008",
        )
        self.assertEqual(
            producer_trace["latest_current_node_key_writer"]["instruction_address_hex"],
            "0x000F29F6",
        )
        self.assertEqual(
            producer_trace["latest_current_node_key_writer"]["semantic"],
            "queue_slot_replacement_writer",
        )
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["instruction_address_hex"],
            "0x000F29F6",
        )
        self.assertEqual(
            producer_trace["next_recovery_anchor"]["roles"],
            ["current_node_key_writer"],
        )
        self.assertEqual(
            producer_trace["watched_address_transitions"][0]["values_hex"],
            ["0x00000401", "0x003445C0"],
        )
        self.assertEqual(
            producer_trace["instruction_groups"][0]["semantic"],
            "work_item_key_writer",
        )
        self.assertEqual(
            producer_trace["instruction_groups"][1]["semantic"],
            "work_item_next_link",
        )

    def test_heap_free_list_boundary_classifies_null_head_walk(self) -> None:
        state = CpuState.with_registers(
            ebx=0x10000000,
            ecx=0,
            edx=0xFFFFFFF8,
            esi=0x10000180,
            edi=0x4B9A,
        )
        trace_events = [
            {
                "sequence": 0,
                "address": 0x000E46B1,
                "address_hex": "0x000E46B1",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x10000180,
                    "value": 0x10000180,
                    "size": 4,
                },
            },
            {
                "sequence": 1,
                "address": 0x000E5080,
                "address_hex": "0x000E5080",
                "operation": "memory_read",
                "details": {
                    "memory_address": 0xFFFFFFF8,
                    "value": 0,
                },
            },
            {
                "sequence": 2,
                "address": 0x000E5085,
                "address_hex": "0x000E5085",
                "operation": "memory_read",
                "details": {
                    "memory_address": 0,
                    "value": 0,
                },
            },
            {
                "sequence": 3,
                "address": 0x000E5087,
                "address_hex": "0x000E5087",
                "operation": "jump",
                "details": {
                    "target": 0x000E506F,
                    "target_hex": "0x000E506F",
                },
            },
        ]

        boundary = _heap_free_list_boundary_from_step_limit(
            trace_events,
            state,
            65536,
        )

        self.assertIsNotNone(boundary)
        assert boundary is not None
        self.assertEqual(boundary["status"], "heap_free_list_boundary")
        self.assertEqual(boundary["boundary_kind"], "title_heap_free_list_scan")
        self.assertEqual(boundary["loop_entry_hex"], "0x000E506F")
        scan = boundary["heap_free_list_scan"]
        self.assertEqual(scan["diagnosis"], "free_list_head_resolved_to_null")
        self.assertEqual(scan["heap_base_hex"], "0x10000000")
        self.assertEqual(scan["list_head_address_hex"], "0x10000180")
        self.assertEqual(scan["current_link_hex"], "0x00000000")
        self.assertEqual(scan["candidate_header_hex"], "0xFFFFFFF8")
        self.assertEqual(scan["free_chunk_units_hex"], "0x00004B9A")
        self.assertTrue(scan["current_link_is_null"])
        self.assertTrue(scan["candidate_header_underflow"])
        self.assertEqual(scan["head_write_count"], 1)
        self.assertEqual(scan["latest_head_writes"][0]["value_hex"], "0x10000180")
        self.assertEqual(
            [read["label"] for read in scan["reads"]],
            ["candidate_header_size", "next_free_link"],
        )

    def test_dynamic_block_cache_round_trips_decoded_metadata(self) -> None:
        function = LiftedFunction(
            symbol="dynamic_block_00001000",
            base_address=0x1000,
            code_size=2,
            instructions=(
                X86Instruction(
                    address=0x1000,
                    size=2,
                    mnemonic="mov",
                    operands=(Operand.register("eax"), Operand.immediate_u32(7)),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dynamic-block-cache.json"
            cache = DynamicBlockCache(path)
            key = cache.key(
                image_sha256="ABCDEF",
                target=0x1000,
                entry_bytes=0x200,
                max_block_instructions=224,
            )
            self.assertIsNone(cache.get(key))
            cache.put(key, function)
            cache.save()

            loaded_cache = DynamicBlockCache(path)
            cached = loaded_cache.get(key)

        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.symbol, "dynamic_block_00001000")
        self.assertEqual(cached.instructions[0].mnemonic, "mov")
        self.assertEqual(cached.instructions[0].operands[1].immediate, 7)

    def test_recovered_render_command_stream_captures_d3d_writes(self) -> None:
        trace_events = [
            {
                "sequence": 7,
                "address_hex": "0x0028CEB2",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0xFED00040,
                    "value": 0x00002A29,
                },
            },
            {
                "sequence": 8,
                "address_hex": "0x0028CEBB",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x8000070C,
                    "value": 0x0000236F,
                    "size": 2,
                },
            },
            {
                "sequence": 9,
                "address_hex": "0x0028CEC0",
                "operation": "memory_write",
                "details": {
                    "memory_address": 0x1000,
                    "value": 0x11111111,
                },
            },
        ]

        stream = _recovered_render_command_stream(trace_events)

        self.assertEqual(stream["write_count"], 2)
        self.assertEqual(stream["mmio_write_count"], 1)
        self.assertEqual(stream["push_buffer_write_count"], 1)
        self.assertEqual(stream["writes"][0]["kind"], "d3d_mmio")
        self.assertEqual(stream["writes"][0]["offset_hex"], "0x00000040")
        self.assertEqual(stream["writes"][1]["kind"], "d3d_push_buffer")
        self.assertEqual(stream["writes"][1]["value_hex"], "0x0000236F")
        self.assertEqual(stream["writes"][1]["size"], 2)

    def test_asset_io_summary_identifies_next_edge_after_dash_and_d_link(self) -> None:
        invocations = [
            {
                "shim_name": "NtCreateFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "guest_path": "d:\\dashupdate.xbe",
                    "root_kind": "disc",
                    "mode": "rb",
                    "desired_access": 0x80100080,
                },
            },
            {
                "shim_name": "NtReadFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "requested_length": 376,
                    "bytes_read": 376,
                    "data_elided": True,
                },
            },
            {
                "shim_name": "NtQueryInformationFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "guest_path": "d:\\dashupdate.xbe",
                    "file_information_class": 14,
                    "position": 376,
                    "size": 1024,
                },
            },
            {
                "shim_name": "NtSetInformationFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "guest_path": "d:\\dashupdate.xbe",
                    "file_information_class": 14,
                    "position": 760,
                    "bytes_consumed": 8,
                },
            },
            {
                "shim_name": "NtReadFile",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x108,
                    "requested_length": 464,
                    "bytes_read": 464,
                    "data_elided": True,
                },
            },
            {"shim_name": "NtClose", "arguments": ["0x00000108"], "result": 0},
            {
                "shim_name": "NtCreateFile",
                "result": {
                    "status": XboxStatus.NO_SUCH_FILE,
                    "handle": None,
                    "guest_path": "y:\\xboxdash.xbe",
                    "root_kind": "cache",
                    "mode": "rb",
                },
            },
            {
                "shim_name": "NtOpenSymbolicLinkObject",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x10C,
                    "guest_path": "\\??\\D:",
                    "target": "\\Device\\Cdrom0",
                },
            },
            {
                "shim_name": "NtQuerySymbolicLinkObject",
                "result": {
                    "status": XboxStatus.SUCCESS,
                    "handle": 0x10C,
                    "name": "\\??\\D:",
                    "target": "\\Device\\Cdrom0",
                },
            },
            {"shim_name": "NtClose", "arguments": ["0x0000010C"], "result": 0},
            {
                "shim_name": "NtOpenFile",
                "result": {
                    "status": XboxStatus.NO_SUCH_FILE,
                    "handle": None,
                    "guest_path": "\\Device\\Harddisk0\\partition2\\XODash\\xonlinedash.xbe",
                    "root_kind": "dashboard",
                    "mode": "rb",
                    "desired_access": 0x80100000,
                },
            },
            {
                "shim_name": "NtOpenFile",
                "result": {
                    "status": XboxStatus.NO_SUCH_FILE,
                    "handle": None,
                    "guest_path": "\\Device\\Cdrom0\\XODash\\xonlinedash.xbe",
                    "root_kind": "disc",
                    "mode": "rb",
                    "desired_access": 0x80100000,
                },
            },
        ]

        summary = _asset_io_summary_from_invocations(invocations)

        self.assertTrue(summary["dashupdate_sequence"]["complete"])
        self.assertEqual(summary["dashupdate_sequence"]["read_count"], 2)
        self.assertEqual(summary["dashupdate_sequence"]["bytes_read_total"], 840)
        self.assertTrue(summary["symbolic_link_sequence"]["complete"])
        self.assertEqual(summary["symbolic_link_sequence"]["target"], "\\Device\\Cdrom0")
        self.assertEqual(
            summary["next_edge_after_dashupdate_and_d_link"]["guest_path"],
            "\\Device\\Harddisk0\\partition2\\XODash\\xonlinedash.xbe",
        )
        self.assertEqual(
            summary["next_edge_after_dashupdate_and_d_link"]["root_kind"],
            "dashboard",
        )
        self.assertEqual(summary["root_kind_counts"]["cache"], 1)
        self.assertGreaterEqual(summary["status_counts"]["not_found"], 3)
        assessment = summary["dashboard_cache_probe_assessment"]
        self.assertEqual(
            assessment["status"],
            "deterministic_not_found_preserved_for_observed_probes",
        )
        self.assertEqual(assessment["dashboard_probe_count"], 1)
        self.assertEqual(assessment["cache_probe_count"], 1)
        self.assertEqual(assessment["clean_not_found_probe_count"], 2)
        self.assertFalse(assessment["requires_configured_roots_for_observed_edges"])
        self.assertEqual(
            assessment["later_gameplay_requirement"],
            "unproven_until_execution_advances_past_current_scheduler_boundary",
        )

    def test_deterministic_service_validation_summarizes_runtime_models(self) -> None:
        runtime_summary = {
            "trace": [
                {
                    "subsystem": "filesystem",
                    "operation": "read_file",
                    "details": {
                        "bytes_read": 128,
                        "deterministic_latency_100ns": 750,
                    },
                },
                {
                    "subsystem": "filesystem",
                    "operation": "write_file",
                    "details": {"bytes_written": 32},
                },
            ],
            "open_files": [
                {"streaming": True, "save_data": False, "cache_data": False},
                {"streaming": False, "save_data": True, "cache_data": False},
            ],
            "audio_initialized": True,
            "audio_streams": [
                {
                    "submitted_buffer_count": 2,
                    "queued_bytes": 64,
                    "played_bytes": 128,
                }
            ],
            "input": {
                "sequence": 4,
                "ports": {
                    "0": {"poll_count": 2, "last_latency_samples": 1},
                    "1": {"poll_count": 0, "last_latency_samples": 0},
                },
            },
            "clock": {"performance_counter": 10},
            "determinism": {"input": "sequenced_snapshots"},
        }

        summary = _deterministic_service_validation_summary(runtime_summary)

        self.assertEqual(summary["streaming"]["read_event_count"], 1)
        self.assertEqual(summary["streaming"]["bytes_read_total"], 128)
        self.assertEqual(
            summary["streaming"]["deterministic_latency_100ns_total"],
            750,
        )
        self.assertEqual(summary["save_data"]["write_event_count"], 1)
        self.assertEqual(summary["save_data"]["open_save_file_count"], 1)
        self.assertEqual(summary["audio"]["submitted_buffer_count"], 2)
        self.assertEqual(summary["input_latency"]["poll_count"], 2)
        self.assertEqual(summary["input_latency"]["max_last_latency_samples"], 1)

    def test_runtime_abi_bridge_initializes_guest_ansi_string(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[289])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        memory = SparseMemory(
            {
                0x7000: b"media\\boot.ini\x00",
                0x8000: 0xDEADC0DE,
                0x8004: 0x6000,
                0x8008: 0x7000,
            }
        )

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "RtlInitAnsiString")
        self.assertEqual(invocation.stack_cleanup_bytes, 8)
        self.assertEqual(memory.read(0x6000, 2), (14).to_bytes(2, "little"))
        self.assertEqual(memory.read(0x6002, 2), (15).to_bytes(2, "little"))
        self.assertEqual(memory.read_u32(0x6004), 0x7000)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["ansi_string"],
        )

    def test_runtime_abi_bridge_decodes_nt_open_file_object_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            runtime.register_kernel_imports(resolver, imported_ordinals=[202])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            path = b"D:\\media\\intro.bin"
            ansi_string = (
                len(path).to_bytes(2, "little")
                + (len(path) + 1).to_bytes(2, "little")
                + (0x7000).to_bytes(4, "little")
            )
            object_attributes = bytearray(24)
            struct.pack_into("<I", object_attributes, 8, 0x6000)
            state = CpuState.with_registers(esp=0x8000)
            memory = SparseMemory(
                {
                    0x6000: ansi_string,
                    0x6100: bytes(object_attributes),
                    0x7000: path + b"\x00",
                    0x8000: 0xDEADC0DE,
                    0x8004: 0x6200,
                    0x8008: 0x80100000,
                    0x800C: 0x6100,
                    0x8010: 0x6210,
                    0x8014: 0x00000001,
                    0x8018: 0x00000000,
                }
            )

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtOpenFile")
        self.assertEqual(invocation.stack_cleanup_bytes, 24)
        self.assertEqual(invocation.eax, 0)
        self.assertEqual(invocation.result["guest_path"], "D:\\media\\intro.bin")
        self.assertEqual(memory.read_u32(0x6200), invocation.result["handle"])
        self.assertEqual(memory.read_u32(0x6210), 0)
        self.assertEqual(memory.read_u32(0x8000 + 24), 0xDEADC0DE)

    def test_runtime_abi_bridge_decodes_nt_open_symbolic_link_object(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        runtime.register_kernel_imports(resolver, imported_ordinals=[203])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        path = b"\\??\\D:"
        ansi_string = (
            len(path).to_bytes(2, "little")
            + (len(path) + 1).to_bytes(2, "little")
            + (0x7000).to_bytes(4, "little")
        )
        object_attributes = bytearray(24)
        struct.pack_into("<I", object_attributes, 8, 0x6000)
        state = CpuState.with_registers(esp=0x8000)
        arguments = (0x6200, 0x6100)
        memory = SparseMemory(
            {
                0x6000: ansi_string,
                0x6100: bytes(object_attributes),
                0x7000: path + b"\x00",
                0x8000: 0xDEADC0DE,
            }
        )
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtOpenSymbolicLinkObject")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 8)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(invocation.result["guest_path"], "\\??\\D:")
        self.assertEqual(invocation.result["target"], "\\Device\\Cdrom0")
        self.assertEqual(memory.read_u32(0x6200), invocation.result["handle"])
        self.assertEqual(memory.read_u32(0x8000 + 8), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["symbolic_link_handle"],
        )

    def test_runtime_abi_bridge_materializes_nt_query_symbolic_link_object(self) -> None:
        resolver = ImportResolver()
        runtime = XboxRuntimeShims()
        opened = runtime.nt_open_symbolic_link_object("\\??\\D:")
        runtime.register_kernel_imports(resolver, imported_ordinals=[215])
        bridge = RuntimeAbiBridge(runtime)
        target = runtime.registered_shims[0].target_address
        state = CpuState.with_registers(esp=0x8000)
        ansi_string = (
            (0).to_bytes(2, "little")
            + (32).to_bytes(2, "little")
            + (0x6400).to_bytes(4, "little")
        )
        arguments = (opened["handle"], 0x6300, 0x6500)
        memory = SparseMemory(
            {
                0x6300: ansi_string,
                0x8000: 0xDEADC0DE,
            }
        )
        for index, argument in enumerate(arguments):
            memory.write_u32(0x8004 + index * 4, argument)

        bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        target_bytes = b"\\Device\\Cdrom0"
        self.assertEqual(invocation.shim_name, "NtQuerySymbolicLinkObject")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 12)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read(0x6400, len(target_bytes)), target_bytes)
        self.assertEqual(memory.read(0x6300, 2), len(target_bytes).to_bytes(2, "little"))
        self.assertEqual(memory.read_u32(0x6500), len(target_bytes))
        self.assertEqual(memory.read_u32(0x8000 + 12), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["symbolic_link_target", "returned_length"],
        )

    def test_runtime_abi_bridge_materializes_nt_read_file_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            opened = runtime.filesystem.open_file("D:\\media\\intro.bin", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[219])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0,
                0,
                0,
                0x6200,
                0x6300,
                4,
                0x6400,
            )
            memory = SparseMemory(
                {
                    0x6400: 2,
                    0x6404: 0,
                    0x8000: 0xDEADC0DE,
                }
            )
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtReadFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read(0x6300, 4), b"rnou")
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 4)
        self.assertNotIn("data", invocation.result)
        self.assertTrue(invocation.result["data_elided"])
        self.assertEqual(invocation.result["bytes_read"], 4)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["read_buffer", "io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_nt_query_information_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            opened = runtime.filesystem.open_file("D:\\media\\intro.bin", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[211])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0x6200,
                0x6300,
                24,
                5,
            )
            memory = SparseMemory({0x8000: 0xDEADC0DE})
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtQueryInformationFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 24)
        self.assertEqual(memory.read_u32(0x6300), 2048)
        self.assertEqual(memory.read_u32(0x6308), 7)
        self.assertEqual(memory.read_u32(0x6310), 1)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["file_information", "io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_nt_set_information_file_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "media"
            media.mkdir()
            (media / "intro.bin").write_bytes(b"burnout")
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root)
            )
            opened = runtime.filesystem.open_file("D:\\media\\intro.bin", "rb")
            runtime.register_kernel_imports(resolver, imported_ordinals=[226])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0x6200,
                0x6300,
                8,
                14,
            )
            memory = SparseMemory(
                {
                    0x6300: (2).to_bytes(8, "little"),
                    0x8000: 0xDEADC0DE,
                }
            )
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

            handle_info = runtime.filesystem.query_file_handle_information(
                opened["handle"]
            )
            positioned_read = runtime.filesystem.read_file(opened["handle"], 4)

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtSetInformationFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.stack_cleanup_bytes, 20)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(handle_info["position"], 2)
        self.assertEqual(positioned_read["data"], b"rnou")
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 8)
        self.assertEqual(memory.read_u32(0x8000 + 20), 0xDEADC0DE)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["io_status", "io_information"],
        )

    def test_runtime_abi_bridge_materializes_nt_write_file_save_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root, save_data_root=save_root)
            )
            opened = runtime.filesystem.open_file(
                "\\Device\\Harddisk0\\Partition1\\UDATA\\41430019\\save.dat",
                "wb",
            )
            runtime.register_kernel_imports(resolver, imported_ordinals=[236])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            state = CpuState.with_registers(esp=0x8000)
            arguments = (
                opened["handle"],
                0,
                0,
                0,
                0x6200,
                0x6300,
                8,
                0,
            )
            memory = SparseMemory(
                {
                    0x6300: b"progress",
                    0x8000: 0xDEADC0DE,
                }
            )
            for index, argument in enumerate(arguments):
                memory.write_u32(0x8004 + index * 4, argument)

            bridge.invoke(state, memory, target, ExecutionTrace())

            written = save_root / "UDATA" / "41430019" / "save.dat"
            self.assertEqual(written.read_bytes(), b"progress")

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtWriteFile")
        self.assertEqual(invocation.arguments, arguments)
        self.assertEqual(invocation.handler_arguments, arguments)
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6200), XboxStatus.SUCCESS)
        self.assertEqual(memory.read_u32(0x6204), 8)
        self.assertEqual(invocation.result["bytes_written"], 8)
        self.assertEqual(
            [write["label"] for write in invocation.memory_writes],
            ["io_status", "io_information"],
        )

    def test_runtime_abi_bridge_creates_save_file_from_guest_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            save_root = root / "save"
            resolver = ImportResolver()
            runtime = XboxRuntimeShims(
                XboxRuntimeConfig(extracted_disc_root=root, save_data_root=save_root)
            )
            runtime.register_kernel_imports(resolver, imported_ordinals=[190])
            bridge = RuntimeAbiBridge(runtime)
            target = runtime.registered_shims[0].target_address
            path = b"\\Device\\Harddisk0\\Partition1\\UDATA\\41430019\\settings.dat"
            ansi_string = (
                len(path).to_bytes(2, "little")
                + (len(path) + 1).to_bytes(2, "little")
                + (0x7000).to_bytes(4, "little")
            )
            object_attributes = bytearray(24)
            struct.pack_into("<I", object_attributes, 8, 0x6000)
            state = CpuState.with_registers(esp=0x8000)
            memory = SparseMemory(
                {
                    0x6000: ansi_string,
                    0x6100: bytes(object_attributes),
                    0x7000: path + b"\x00",
                    0x8000: 0xDEADC0DE,
                    0x8004: 0x6200,
                    0x8008: 0x40000002,
                    0x800C: 0x6100,
                    0x8010: 0x6210,
                    0x8014: 0,
                    0x8018: 0,
                    0x801C: 0,
                    0x8020: 3,
                    0x8024: 0,
                    0x8028: 0,
                    0x802C: 0,
                }
            )

            bridge.invoke(state, memory, target, ExecutionTrace())

            created = save_root / "UDATA" / "41430019" / "settings.dat"
            self.assertTrue(created.is_file())

        invocation = bridge.invocations[0]
        self.assertEqual(invocation.shim_name, "NtCreateFile")
        self.assertEqual(invocation.eax, XboxStatus.SUCCESS)
        self.assertEqual(invocation.result["mode"], "ab")
        self.assertEqual(memory.read_u32(0x6200), invocation.result["handle"])
        self.assertEqual(memory.read_u32(0x6210), XboxStatus.SUCCESS)

    def test_playability_probe_executes_synthetic_entry_import_call(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_raw = text_raw + 0x20
        data[entry_raw : entry_raw + 7] = _call_indirect_bytes(
            layout["kernel_thunk_addr"]
        )
        struct.pack_into("<I", data, text_raw + 0x100, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=16,
                max_instructions=8,
            )

        self.assertEqual(summary["format"], "b2-recomp-playability-probe")
        self.assertEqual(summary["runtime"]["registered_kernel_shim_count"], 1)
        self.assertEqual(summary["entry_recovery"]["status"], "decoded")
        self.assertEqual(summary["entry_recovery"]["instruction_count"], 2)
        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(summary["runtime_abi_bridge"]["invocation_count"], 1)
        self.assertEqual(
            summary["runtime_abi_bridge"]["invocations"][0]["shim_name"],
            "KeQueryPerformanceFrequency",
        )

    def test_playability_probe_dispatches_runtime_tail_jump(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_raw = text_raw + 0x20
        entry = _jmp_indirect_bytes(layout["kernel_thunk_addr"]) + b"\xC3"
        data[entry_raw : entry_raw + len(entry)] = entry
        struct.pack_into("<I", data, text_raw + 0x100, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=16,
                max_instructions=8,
            )

        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(summary["runtime_abi_bridge"]["invocation_count"], 1)
        self.assertEqual(
            summary["runtime_abi_bridge"]["invocations"][0]["shim_name"],
            "KeQueryPerformanceFrequency",
        )

    def test_playability_probe_recovers_first_internal_handoff_import_call(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        handoff_va = layout["text_va"] + 0x40
        entry_raw = text_raw + 0x20
        handoff_raw = text_raw + 0x40
        rel = handoff_va - (entry_va + 5)
        data[entry_raw : entry_raw + 6] = b"\xE8" + struct.pack("<i", rel) + b"\xC3"
        data[handoff_raw : handoff_raw + 7] = _call_indirect_bytes(
            layout["kernel_thunk_addr"]
        )
        struct.pack_into("<I", data, text_raw + 0x100, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x80,
                max_instructions=16,
            )

        handoffs = summary["entry_recovery"]["internal_handoffs"]
        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(
            summary["entry_recovery"]["execution"]["executed_internal_targets"],
            [f"0x{handoff_va:08X}"],
        )
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["status"], "decoded")
        self.assertEqual(summary["runtime_abi_bridge"]["invocation_count"], 1)
        self.assertEqual(
            summary["runtime_abi_bridge"]["invocations"][0]["shim_name"],
            "KeQueryPerformanceFrequency",
        )

    def test_playability_probe_batches_branch_target_and_fallthrough_blocks(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        handoff_va = layout["text_va"] + 0x40
        fallthrough_va = handoff_va + 4
        branch_target_va = handoff_va + 10
        entry_raw = text_raw + 0x20
        handoff_raw = text_raw + 0x40
        rel = handoff_va - (entry_va + 5)
        data[entry_raw : entry_raw + 6] = b"\xE8" + struct.pack("<i", rel) + b"\xC3"
        handoff = (
            b"\x33\xC0"
            b"\x74\x06"
            b"\xB8\x11\x11\x00\x00"
            b"\xC3"
            b"\xB8\x22\x22\x00\x00"
            b"\xC3"
        )
        data[handoff_raw : handoff_raw + len(handoff)] = handoff
        struct.pack_into("<I", data, text_raw + 0x100, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x80,
                max_instructions=16,
                max_block_instructions=4,
            )

        recovery = summary["entry_recovery"]["control_flow_recovery"]
        decoded_targets = set(recovery["decoded_block_targets"])
        self.assertEqual(summary["entry_recovery"]["execution"]["status"], "returned")
        self.assertEqual(
            summary["entry_recovery"]["execution"]["state"]["registers"]["eax"],
            "0x00002222",
        )
        self.assertEqual(recovery["frontier_count"], 0)
        self.assertIn(f"0x{handoff_va:08X}", decoded_targets)
        self.assertIn(f"0x{fallthrough_va:08X}", decoded_targets)
        self.assertIn(f"0x{branch_target_va:08X}", decoded_targets)

    def test_playability_probe_recovers_executed_indirect_call_block(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        pointer_va = layout["text_va"] + 0x140
        dynamic_va = layout["text_va"] + 0x180
        entry_raw = text_raw + 0x20
        pointer_raw = text_raw + 0x140
        dynamic_raw = text_raw + 0x180
        data[entry_raw : entry_raw + 7] = _call_indirect_bytes(pointer_va)
        struct.pack_into("<I", data, pointer_raw, dynamic_va)
        data[dynamic_raw : dynamic_raw + 6] = b"\xB8\x55\x44\x00\x00\xC3"

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x200,
                max_instructions=8,
                max_block_instructions=8,
            )

        execution = summary["entry_recovery"]["execution"]
        self.assertEqual(execution["status"], "returned")
        self.assertEqual(execution["dynamic_block_count"], 1)
        self.assertEqual(execution["dynamic_block_targets"], [f"0x{dynamic_va:08X}"])
        self.assertEqual(
            execution["state"]["registers"]["eax"],
            "0x00004455",
        )
        non_import_gaps = [
            gap for gap in summary["playability_gaps"] if gap["area"] != "imports"
        ]
        self.assertEqual(non_import_gaps, [])

    def test_playability_probe_reads_larger_dynamic_block_window(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        pointer_va = layout["text_va"] + 0x140
        dynamic_va = layout["text_va"] + 0x180
        entry_raw = text_raw + 0x20
        pointer_raw = text_raw + 0x140
        dynamic_raw = text_raw + 0x180
        data[entry_raw : entry_raw + 7] = _call_indirect_bytes(pointer_va)
        struct.pack_into("<I", data, pointer_raw, dynamic_va)
        long_dynamic_block = (b"\x90" * 0x40) + b"\xB8\x77\x66\x00\x00\xC3"
        data[dynamic_raw : dynamic_raw + len(long_dynamic_block)] = long_dynamic_block

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x20,
                max_instructions=8,
                max_block_instructions=80,
            )

        execution = summary["entry_recovery"]["execution"]
        self.assertEqual(execution["status"], "returned")
        self.assertEqual(execution["dynamic_block_count"], 1)
        self.assertEqual(execution["dynamic_block_targets"], [f"0x{dynamic_va:08X}"])
        self.assertEqual(
            execution["state"]["registers"]["eax"],
            "0x00006677",
        )

    def test_playability_probe_executes_created_guest_thread_start(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_raw = text_raw + 0x20
        thread_va = layout["text_va"] + 0x180
        thread_raw = text_raw + 0x180
        handle_pointer = layout["data_va"] + 0x100
        thread_id_pointer = layout["data_va"] + 0x104
        entry = b"".join(
            [
                _push_u32(thread_va),
                _push_u32(0),
                _push_u32(0),
                _push_u32(0x33334444),
                _push_u32(0x11112222),
                _push_u32(thread_id_pointer),
                _push_u32(0x40),
                _push_u32(0x3000),
                _push_u32(0x20),
                _push_u32(handle_pointer),
                _call_indirect_bytes(layout["kernel_thunk_addr"]),
            ]
        )
        data[entry_raw : entry_raw + len(entry)] = entry
        data[thread_raw : thread_raw + 7] = _call_indirect_bytes(
            layout["kernel_thunk_addr"] + 4
        )
        struct.pack_into("<I", data, text_raw + 0x100, 0x800000FF)
        struct.pack_into("<I", data, text_raw + 0x104, 0x8000007F)
        struct.pack_into("<I", data, text_raw + 0x108, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x200,
                max_instructions=24,
                max_block_instructions=8,
            )

        execution = summary["entry_recovery"]["execution"]
        invocations = summary["runtime_abi_bridge"]["invocations"]
        self.assertEqual(execution["status"], "returned")
        self.assertEqual(execution["guest_thread_execution_count"], 1)
        self.assertEqual(execution["guest_thread_count"], 1)
        self.assertEqual(execution["guest_threads"][0]["start_address_hex"], f"0x{thread_va:08X}")
        self.assertEqual(execution["guest_thread_executions"][0]["status"], "returned")
        self.assertEqual(
            execution["guest_thread_executions"][0]["state"]["registers"]["eax"],
            "0x00989680",
        )
        self.assertEqual(
            [invocation["shim_name"] for invocation in invocations],
            ["PsCreateSystemThreadEx", "KeQueryPerformanceFrequency"],
        )
        self.assertEqual(invocations[0]["return_kind"], "status_dict")
        self.assertEqual(invocations[0]["eax_hex"], "0x00000000")
        self.assertEqual(
            [write["label"] for write in invocations[0]["memory_writes"]],
            ["thread_handle", "thread_id"],
        )
        non_import_gaps = [
            gap for gap in summary["playability_gaps"] if gap["area"] != "imports"
        ]
        self.assertEqual(non_import_gaps, [])

    def test_runtime_abi_bridge_cleans_stdcall_guest_arguments(self) -> None:
        blob, layout = _synthetic_xbe()
        data = bytearray(blob)
        text_raw = 0x1000
        entry_va = layout["text_va"] + 0x20
        handoff_va = layout["text_va"] + 0x40
        entry_raw = text_raw + 0x20
        handoff_raw = text_raw + 0x40
        rel = handoff_va - (entry_va + 5)
        data[entry_raw : entry_raw + 6] = b"\xE8" + struct.pack("<i", rel) + b"\xC3"
        handoff = (b"\x6A\x00" * 10) + _call_indirect_bytes(
            layout["kernel_thunk_addr"]
        )
        data[handoff_raw : handoff_raw + len(handoff)] = handoff
        struct.pack_into("<I", data, text_raw + 0x100, 0x800000FF)
        struct.pack_into("<I", data, text_raw + 0x104, 0)

        with tempfile.TemporaryDirectory() as temp_dir:
            xbe_path = Path(temp_dir) / "default.xbe"
            xbe_path.write_bytes(bytes(data))
            summary = build_playability_probe_summary(
                xbe_path,
                entry_bytes=0x80,
                max_instructions=24,
            )

        invocation = summary["runtime_abi_bridge"]["invocations"][0]
        entry_execution = summary["entry_recovery"]["execution"]
        self.assertEqual(invocation["shim_name"], "PsCreateSystemThreadEx")
        self.assertEqual(invocation["stack_cleanup_bytes"], 40)
        self.assertEqual(entry_execution["status"], "returned")
        self.assertEqual(
            entry_execution["executed_internal_targets"],
            [f"0x{handoff_va:08X}"],
        )


if __name__ == "__main__":
    unittest.main()
