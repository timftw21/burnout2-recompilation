from __future__ import annotations

import unittest

from tools.recomp.representative_aot import (
    build_synthetic_hot_corpus,
    select_representative_functions,
)


class RepresentativeAotTests(unittest.TestCase):
    def test_synthetic_corpus_covers_distinct_emitter_families(self) -> None:
        corpus = build_synthetic_hot_corpus()
        mnemonics = {
            instruction.mnemonic
            for function in corpus
            for instruction in function.instructions
        }

        self.assertEqual(len(corpus), 4)
        self.assertTrue({"cmp", "mulps", "fld", "rdtsc", "rep_movsd"} <= mnemonics)

    def test_selection_is_hot_first_and_budgeted(self) -> None:
        corpus = build_synthetic_hot_corpus()
        selected = select_representative_functions(
            corpus,
            hot_addresses=(corpus[-1].base_address,),
            instruction_budget=corpus[-1].instruction_count,
        )

        self.assertEqual(selected, (corpus[-1],))


if __name__ == "__main__":
    unittest.main()
