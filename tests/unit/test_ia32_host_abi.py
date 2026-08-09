from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.recomp.ia32_native_backend import (
    HOST_ABI_CONTRACT_FORMAT,
    HOST_ABI_EXCHANGE_VERSION,
    NATIVE_HOST_SERVICE_WORKLOAD_ABI,
    PHASE4_HOST_ABI_CONTRACT_ID,
    PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS,
    PHASE7_NATIVE_BUFFER_CONFIGURATION_SERVICE_TARGETS,
    PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS,
    PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS,
    PHASE7_OFFLINE_KERNEL_SERVICE_SPECS,
    PHASE7_PROFILED_BOOT_SERVICE_SPECS,
    WORKLOAD_SERVICE_MEMORY,
    Ia32BackendError,
    Ia32CoverageProfile,
    _capsule_host_service_thunks,
    _normal_live_input_audio_c_source,
    _normal_live_resource_publication_c_source,
    _phase4_c_source,
    _phase6_host_service_thunks,
    _phase7_normal_live_optimization_source,
    _phase7_native_workload_source,
    execute_ia32_slice_reference,
    ia32_host_abi_contract,
    ia32_host_abi_contract_id,
)
from tools.recomp.ia32_proof_contract import (
    HOST_ABI_CALLBACK_EIP,
    PHASE4_PROOF_SET_FORMAT,
    PHASE4_PROOF_SET_ID,
    build_asset_free_phase4_fixture,
    run_asset_free_phase4_proofs,
)


IA32_INTEGRATION_AVAILABLE = (
    sys.platform == "win32" and shutil.which("clang-cl") and shutil.which("lld-link")
)


class Ia32HostAbiTests(unittest.TestCase):
    def test_offline_registry_closes_complete_native_kernel_service_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase4_fixture(Path(temp_dir))
        profile = Ia32CoverageProfile(
            workload="unit-offline-kernel-services",
            mode="normal-validation",
            targets=(
                {
                    "address": 0x00001000,
                    "slice": "boot-frontend-continuity",
                    "lane": "primary",
                },
            ),
        )

        ordinary = _phase6_host_service_thunks(fixture.capsule, profile)
        complete = _phase6_host_service_thunks(
            fixture.capsule,
            profile,
            complete_offline_registry=True,
        )

        self.assertTrue(PHASE7_OFFLINE_KERNEL_SERVICE_SPECS.keys() <= complete.keys())
        self.assertTrue(PHASE7_OFFLINE_KERNEL_SERVICE_SPECS.keys().isdisjoint(ordinary))
        self.assertEqual(complete[0xE00002E0].stack_cleanup_bytes, 12)
        self.assertEqual(complete[0xE0000680].runtime_value, 9)

    def test_offline_kernel_service_bodies_are_native_and_bounded(self) -> None:
        source = _phase7_native_workload_source(
            _phase4_c_source(
                page_addresses=(0x00001000,),
                mapped_page_addresses=(0x00001000,),
                guest_virtual_size=0x00002000,
                code_spans=((0x00001000, b"\xC3"),),
                stop_eip=0x00001001,
            )
        )

        self.assertIn("matched + 4u <= length", source)
        self.assertIn("service->runtime_value == 10u", source)
        self.assertIn("*(U32*)(event + 4u) = 1u", source)

    def test_offline_boundary_optimizations_have_compiled_runtime_paths(self) -> None:
        source = "\n".join(
            (
                _normal_live_resource_publication_c_source(),
                _normal_live_input_audio_c_source(),
                inspect.getsource(_phase7_normal_live_optimization_source),
            )
        )
        expected_ids = (
            "phase7-sha256-direct-block-transform-v1",
            "phase7-sha256-rolling-schedule-v1",
            "phase7-vertex-range-sorted-merge-v1",
            "phase7-audio-gain-memoization-v1",
            "phase7-audio-idle-quantum-bypass-v1",
            "phase7-audio-accumulator-wide-clear-v1",
            "phase7-audio-unity-resample-fast-path-v1",
            "phase7-allocation-page-direct-cache-v1",
        )

        self.assertEqual(
            tuple(
                str(record["id"])
                for record in PHASE7_OFFLINE_BOUNDARY_OPTIMIZATIONS[-8:]
            ),
            expected_ids,
        )
        for snippet in (
            "while (size >= 64u)",
            "U32 words[16];",
            "state->retained_vertex_range_ends[index] < start",
            "g_live_audio_gain_cache_keys[cache_slot] == cache_key",
            "g_live_audio_active_playback_masks[0] |",
            "index += 8u",
            "playback->step_q32 == (1ull << 32u)",
            "g_workload_allocation_page_cache[slot]",
        ):
            self.assertIn(snippet, source)

    def test_native_xinput_control_family_closes_observed_input_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase4_fixture(Path(temp_dir))
        services = _phase6_host_service_thunks(
            fixture.capsule,
            Ia32CoverageProfile(
                workload="unit-xinput-control",
                mode="normal-validation",
                targets=(
                    {
                        "address": 0x00001000,
                        "slice": "boot-frontend-continuity",
                        "lane": "primary",
                    },
                ),
                boundary_exits=(
                    {
                        "source": 0x00001000,
                        "target": 0x0028CF40,
                        "boundary": "input",
                        "count": 1,
                    },
                ),
            ),
        )

        self.assertTrue(PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS <= services.keys())
        self.assertEqual(services[0x0028CF96].shim_name, "XInputClose")
        self.assertEqual(services[0x0028D1EC].shim_name, "XInputSetState")

    def test_native_audio_families_close_observed_audio_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase4_fixture(Path(temp_dir))
        services = _phase6_host_service_thunks(
            fixture.capsule,
            Ia32CoverageProfile(
                workload="unit-audio-lifetime",
                mode="normal-validation",
                targets=(
                    {
                        "address": 0x00001000,
                        "slice": "boot-frontend-continuity",
                        "lane": "primary",
                    },
                ),
                boundary_exits=(
                    {
                        "source": 0x00001000,
                        "target": 0x0022F8EA,
                        "boundary": "audio",
                        "count": 1,
                    },
                ),
            ),
        )

        self.assertTrue(
            PHASE7_NATIVE_BUFFER_CONFIGURATION_SERVICE_TARGETS <= services.keys()
        )
        self.assertTrue(PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS <= services.keys())
        self.assertEqual(services[0x0022C11B].runtime_value, 29)
        self.assertEqual(services[0x0022CC0B].runtime_value, 30)

    def test_native_input_audio_families_do_not_leak_into_unrelated_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase4_fixture(Path(temp_dir))
        services = _phase6_host_service_thunks(
            fixture.capsule,
            Ia32CoverageProfile(
                workload="unit-unrelated-service",
                mode="normal-validation",
                targets=(
                    {
                        "address": 0x00001000,
                        "slice": "boot-frontend-continuity",
                        "lane": "primary",
                    },
                ),
            ),
        )

        self.assertTrue(PHASE7_NATIVE_BUFFER_CONFIGURATION_SERVICE_TARGETS.isdisjoint(services))
        self.assertTrue(PHASE7_NATIVE_AUDIO_LIFETIME_SERVICE_TARGETS.isdisjoint(services))
        self.assertTrue(PHASE7_NATIVE_XINPUT_CONTROL_SERVICE_TARGETS.isdisjoint(services))

    def test_phase4_contract_freezes_thunks_trace_and_native_broker(self) -> None:
        contract = ia32_host_abi_contract()

        self.assertEqual(contract["format"], HOST_ABI_CONTRACT_FORMAT)
        self.assertEqual(contract["exchange"]["version"], HOST_ABI_EXCHANGE_VERSION)
        self.assertEqual(ia32_host_abi_contract_id(), PHASE4_HOST_ABI_CONTRACT_ID)
        self.assertEqual(contract["calling_conventions"]["supported"], ["cdecl", "stdcall"])
        self.assertEqual(
            contract["broker"]["transport"],
            "named-shared-memory-and-native-events",
        )
        self.assertEqual(contract["normal_runtime_python_callbacks"], 0)

    def test_service_rich_reference_records_order_stack_memory_and_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase4_fixture(Path(temp_dir))
            result = execute_ia32_slice_reference(
                fixture.capsule,
                stop_eip=fixture.stop_eip,
            )

        self.assertEqual(
            tuple(call["shim_name"] for call in result.host_service_calls),
            fixture.expected_service_order,
        )
        self.assertEqual(result.state.get_register("esp"), fixture.capsule.state.get_register("esp"))
        self.assertEqual(result.host_service_calls[1]["callback_target"], HOST_ABI_CALLBACK_EIP)
        self.assertEqual(result.host_service_calls[1]["callback_count"], 1)
        self.assertEqual(result.memory.read_u32(0x000D1000), 0xAABBCCDD)
        self.assertEqual(result.memory.read_u32(0x000D1004), 15)
        self.assertEqual(result.memory.read_u32(0x000D1008), 0x55667788)

    def test_unregistered_calling_convention_fails_closed_with_service_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase4_fixture(Path(temp_dir))
        service_state = dict(fixture.capsule.service_state)
        registry = [dict(record) for record in service_state["service_registry"]]
        registry[0]["calling_convention"] = "thiscall"
        service_state["service_registry"] = registry
        malformed = replace(fixture.capsule, service_state=service_state)

        with self.assertRaisesRegex(Ia32BackendError, "B2RMemoryWrite.*calling convention"):
            _capsule_host_service_thunks(malformed)

    def test_workload_service_accepts_five_native_arguments_and_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = build_asset_free_phase4_fixture(Path(temp_dir))
        service_state = {
            "service_registry": [
                {
                    "target": 0xE00003A0,
                    "shim_name": "MmAllocateContiguousMemoryEx",
                    "kind": NATIVE_HOST_SERVICE_WORKLOAD_ABI,
                    "value": 0,
                    "calling_convention": "stdcall",
                    "argument_count": 5,
                    "stack_cleanup_bytes": 20,
                    "boundary": "memory",
                    "execution": "native32",
                    "runtime_kind": WORKLOAD_SERVICE_MEMORY,
                    "runtime_value": 1,
                }
            ]
        }
        capsule = replace(fixture.capsule, service_state=service_state)

        service = _capsule_host_service_thunks(capsule)[0xE00003A0]

        self.assertEqual(service.argument_count, 5)
        self.assertEqual(service.stack_cleanup_bytes, 20)
        self.assertEqual(service.runtime_kind, WORKLOAD_SERVICE_MEMORY)
        self.assertEqual(service.runtime_value, 1)

    def test_workload_allocator_extends_across_reserved_windows_regions(self) -> None:
        source = _phase7_native_workload_source(
            _phase4_c_source(
                page_addresses=(0x00001000,),
                mapped_page_addresses=(0x00001000,),
                guest_virtual_size=0x00002000,
                code_spans=((0x00001000, b"\xC3"),),
                stop_eip=0x00001001,
            )
        )

        self.assertIn("for (page_address = commit_address;", source)
        self.assertIn("region_address = (U32)page_address & ~0xFFFFu;", source)
        self.assertIn(
            "VirtualAlloc((void*)region_address, 0x10000u, 0x00002000u, 0x01u)",
            source,
        )
        self.assertNotIn("region_size = workload_align_up", source)

    def test_contiguous_allocator_maps_kernel_and_uncached_aliases_as_one_arena(
        self,
    ) -> None:
        source = _phase7_native_workload_source(
            _phase4_c_source(
                page_addresses=(0x00001000,),
                mapped_page_addresses=(0x00001000,),
                guest_virtual_size=0x00002000,
                code_spans=((0x00001000, b"\xC3"),),
                stop_eip=0x00001001,
            )
        )

        self.assertIn("CreateFileMappingA(", source)
        self.assertIn("const U32 ARENA_SIZE = 0x0B000000u;", source)
        self.assertIn("(void*)0x82000000u", source)
        self.assertIn("(void*)0xF2000000u", source)
        self.assertIn("(U64)address + reserved_size > 0x8D000000ull", source)
        self.assertIn("allocation->contiguous = contiguous;", source)
        self.assertIn(
            "base = 0xF0000000u | (allocation->address & 0x0FFFFFFFu);",
            source,
        )
        self.assertIn(
            "? (allocation->address & 0x7FFFFFFFu) + offset",
            source,
        )
        self.assertNotIn("(void*)0x03000000u", source)
        self.assertIn("workload_choose_contiguous_address(", source)
        self.assertIn(
            "arguments[0], arguments[1], arguments[2], alignment", source
        )
        self.assertIn(
            "if (*cursor < address + reserved_size) *cursor = address + reserved_size;",
            source,
        )

    def test_mm_claim_gpu_instance_memory_owns_two_argument_abi(self) -> None:
        spec = PHASE7_PROFILED_BOOT_SERVICE_SPECS[0xE00003B0]

        self.assertEqual(spec["shim_name"], "MmClaimGpuInstanceMemory")
        self.assertEqual(spec["stack_cleanup_bytes"], 8)
        source = _phase7_native_workload_source(
            _phase4_c_source(
                page_addresses=(0x00001000,),
                mapped_page_addresses=(0x00001000,),
                guest_virtual_size=0x00002000,
                code_spans=((0x00001000, b"\xC3"),),
                stop_eip=0x00001001,
            )
        )
        self.assertIn("if (arguments[1]) *(U32*)arguments[1] = 0u;", source)

    @unittest.skipUnless(
        IA32_INTEGRATION_AVAILABLE,
        "the IA-32 Phase-4 proof requires the supported Windows LLVM toolchain",
    )
    def test_asset_free_phase4_proof_covers_native_and_brokered_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = run_asset_free_phase4_proofs(Path(temp_dir))

        self.assertEqual(report["format"], PHASE4_PROOF_SET_FORMAT)
        self.assertEqual(report["host_abi_contract_id"], ia32_host_abi_contract_id())
        self.assertEqual(report["proof_set_id"], PHASE4_PROOF_SET_ID)
        self.assertTrue(report["passed"])
        proof = report["proofs"][0]
        self.assertTrue(proof["checks"]["service_trace_match"])
        self.assertTrue(proof["checks"]["callback_reentered_once"])
        self.assertTrue(proof["checks"]["native_broker_is_separate_process"])
        self.assertTrue(proof["checks"]["normal_runtime_python_callbacks_zero"])


if __name__ == "__main__":
    unittest.main()
