from __future__ import annotations

import unittest

from tools.playability.frontend_boot_gate import assess_frontend_boot


def _checkpoint(
    *, text_draws: int, methods: int, abi_calls: int, writes: int, observations: int
) -> dict:
    return {
        "entry_recovery": {
            "execution": {
                "status": "returned",
                "dynamic_block_count": 3519,
                "dynamic_frontier_count": 0,
                "title_text_draw_fast_path": {
                    "invocation_count": text_draws,
                    "sampled_strings": [
                        {"bytes_hex": "Loading - please wait".encode().hex()}
                    ],
                },
                "title_frontend_object_fast_path": {
                    "method_invocation_count": methods
                },
                "guest_thread_executions": [
                    {
                        "status": "render_watchpoint_stop",
                        "runtime_abi_invocation_count": abi_calls,
                        "render_command_stream": {"write_count": writes},
                        "title_frontend_record_table_count_repair": {
                            "observation_count": observations,
                            "repair_count": 2,
                        },
                    }
                ],
            }
        }
    }


class FrontendBootGateTests(unittest.TestCase):
    def test_passes_when_loading_quiesces_while_frontend_progresses(self) -> None:
        result = assess_frontend_boot(
            _checkpoint(
                text_draws=53,
                methods=158,
                abi_calls=4395,
                writes=10739,
                observations=471,
            ),
            _checkpoint(
                text_draws=53,
                methods=299,
                abi_calls=6087,
                writes=32768,
                observations=894,
            ),
        )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["classification"], "title_menu_frontend_loop_reached"
        )
        self.assertTrue(all(result["checks"].values()))

    def test_fails_when_loading_draws_continue(self) -> None:
        early = _checkpoint(
            text_draws=53,
            methods=158,
            abi_calls=4395,
            writes=10739,
            observations=471,
        )
        late = _checkpoint(
            text_draws=54,
            methods=299,
            abi_calls=6087,
            writes=32768,
            observations=894,
        )

        result = assess_frontend_boot(early, late)

        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["checks"]["loading_draw_path_quiesced"])


if __name__ == "__main__":
    unittest.main()
