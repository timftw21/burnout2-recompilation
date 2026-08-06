from __future__ import annotations

import unittest
from unittest import mock

from tools import quality_gate
from tools.dev_check import BASE_NODE_NAMES, build_validation_nodes, select_checks


class DevCheckTests(unittest.TestCase):
    def test_python_test_file_selects_its_individual_module(self) -> None:
        selection = select_checks(("tests/unit/test_native_build.py",))

        self.assertIn("tests.unit.test_native_build", selection.python_tests)
        self.assertFalse(selection.all_python_tests)
        self.assertIn("compileall", selection.nodes)
        self.assertIn("ruff", selection.nodes)

    def test_x86_lifter_fans_out_to_dependent_regressions(self) -> None:
        selection = select_checks(("tools/recomp/x86_lifter.py",))

        self.assertTrue(
            {
                "tests.unit.test_recomp_x86",
                "tests.unit.test_representative_aot",
            }.issubset(selection.python_tests)
        )
        self.assertIn("aot_representative", selection.nodes)
        self.assertIn("ia32_phase0", selection.nodes)
        self.assertIn("ia32_phase1", selection.nodes)
        self.assertIn("ia32_phase2", selection.nodes)
        self.assertIn("ia32_phase3", selection.nodes)
        self.assertIn("ia32_phase4", selection.nodes)
        self.assertIn("ia32_phase5", selection.nodes)
        self.assertIn("ia32_phase6", selection.nodes)
        self.assertIn("ia32_phase7", selection.nodes)
        self.assertFalse(selection.all_python_tests)

    def test_ia32_backend_selects_phase_contract_gates(self) -> None:
        selection = select_checks(("tools/recomp/ia32_native_backend.py",))

        self.assertIn("ia32_phase0", selection.nodes)
        self.assertIn("ia32_phase1", selection.nodes)
        self.assertIn("ia32_phase2", selection.nodes)
        self.assertIn("ia32_phase3", selection.nodes)
        self.assertIn("ia32_phase4", selection.nodes)
        self.assertIn("ia32_phase5", selection.nodes)
        self.assertIn("ia32_phase6", selection.nodes)
        self.assertIn("ia32_phase7", selection.nodes)
        self.assertIn("tests.unit.test_ia32_native_backend", selection.python_tests)
        self.assertIn("tests.unit.test_ia32_proof_contract", selection.python_tests)
        self.assertIn("tests.unit.test_ia32_persistent_worker", selection.python_tests)
        self.assertIn(
            "tests.unit.test_ia32_decoded_store_artifact",
            selection.python_tests,
        )
        self.assertIn(
            "tests.unit.test_ia32_architecture_memory",
            selection.python_tests,
        )
        self.assertIn("tests.unit.test_ia32_host_abi", selection.python_tests)
        self.assertIn("tests.unit.test_ia32_resident_scheduler", selection.python_tests)
        self.assertIn("tests.unit.test_ia32_coverage_growth", selection.python_tests)
        self.assertIn("tests.unit.test_ia32_launcher_cutover", selection.python_tests)

    def test_phase2_xbe_fixture_selects_decoded_store_gate(self) -> None:
        selection = select_checks(("tools/xbe/synthetic_fixture.py",))

        self.assertIn("ia32_phase2", selection.nodes)
        self.assertIn(
            "tests.unit.test_ia32_decoded_store_artifact",
            selection.python_tests,
        )

    def test_unmapped_source_falls_back_to_all_python_tests(self) -> None:
        selection = select_checks(("tools/new_guest_semantics.py",))

        self.assertTrue(selection.all_python_tests)
        self.assertIn("python_tests", selection.nodes)

    def test_all_selects_complete_base_gate(self) -> None:
        selection = select_checks((), all_checks=True)

        self.assertTrue(set(BASE_NODE_NAMES).issubset(selection.nodes))
        self.assertTrue(selection.all_python_tests)

    def test_global_python_configuration_uses_conservative_suite_fanout(self) -> None:
        selection = select_checks(("pyproject.toml",))

        self.assertTrue(selection.all_python_tests)
        self.assertIn("mypy", selection.nodes)

    def test_generated_fixture_fans_out_to_codegen_consumers(self) -> None:
        selection = select_checks(("tests/fixtures/recomp/flags.json",))

        self.assertTrue(
            {
                "tests.unit.test_recomp_x86",
                "tests.unit.test_native_executor",
                "tests.unit.test_playability_probe",
            }.issubset(selection.python_tests)
        )

    def test_native_area_maps_to_target_and_ctest_label(self) -> None:
        selection = select_checks(("runtime/host/nv2a_command_processor.cpp",))
        nodes = build_validation_nodes(selection, jobs=5)
        command = nodes["native_debug"].command

        self.assertIn("b2r_host_core_tests", selection.native_targets)
        self.assertIn("renderer", selection.ctest_labels)
        self.assertIn("--target", command)
        self.assertIn("--label", command)
        self.assertIn("--skip-toolchain-validation", command)
        self.assertIn("renderer", command)
        self.assertEqual(nodes["native_debug"].dependencies, ("native_toolchain",))

    def test_leaf_native_edit_selects_ninja_target_and_ctest_regex(self) -> None:
        selection = select_checks(("runtime/host/dirty_ranges.h",))
        command = build_validation_nodes(selection, jobs=3)["native_debug"].command

        self.assertEqual(selection.native_targets, {"b2r_host_core_tests"})
        self.assertIn(r"^native\.host_core\.dirty_range_lifetime$", selection.ctest_regexes)
        self.assertIn("--test-regex", command)

    def test_quality_gate_forwards_to_complete_cached_graph(self) -> None:
        with mock.patch("tools.dev_check.main", return_value=0) as dev_main:
            result = quality_gate.main(("--full", "--no-cache", "--explain", "--jobs", "3"))

        self.assertEqual(result, 0)
        dev_main.assert_called_once_with(
            ["--all", "--jobs", "3", "--full", "--no-cache", "--explain"]
        )


if __name__ == "__main__":
    unittest.main()
