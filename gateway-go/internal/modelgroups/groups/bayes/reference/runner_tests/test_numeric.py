"""The comparison of numbers that a reordered sum can change in their last bits (numeric.py): it must let a reordering through and stop
a number that moved more than that."""
import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent)]

import numpy as np  # noqa: E402

from numeric import REORDER_RTOL, assert_same_numbers, close  # noqa: E402


class TestReorderedSums(unittest.TestCase):
    def test_the_same_terms_in_another_order_are_within_the_tolerance(self):
        x = np.random.default_rng(1).normal(100.0, 30.0, 10_000)
        a = float(np.sum(x))
        for order in (np.random.default_rng(s).permutation(len(x)) for s in range(20)):
            self.assertTrue(close(a, float(np.sum(x[order]))))
            self.assertTrue(close(a, float(sum(x[order].tolist()))))        # the plain left-to-right sum, the worst case

    def test_the_last_bit_is_let_through_and_a_real_change_is_not(self):
        self.assertTrue(close(153.231257466797, np.nextafter(153.231257466797, 200.0)))
        self.assertFalse(close(1.0, 1.0 + 100 * REORDER_RTOL))
        self.assertFalse(close(1.0, None))
        self.assertTrue(close(None, None) and close(float("nan"), float("nan")) and not close(float("nan"), 1.0))
        self.assertTrue(close(float("inf"), float("inf")) and not close(float("inf"), float("-inf")))

    def test_the_structure_and_what_is_not_a_float_stay_exact(self):
        a = {"quantity": ["mu", "tau"], "mean": [1.0, None], "n": [3, 4]}
        assert_same_numbers(self, a, {"quantity": ["mu", "tau"], "mean": [np.nextafter(1.0, 2.0), None], "n": [3, 4]})
        for bad in ({"quantity": ["mu", "tau2"], "mean": [1.0, None], "n": [3, 4]},
                    {"quantity": ["mu", "tau"], "mean": [1.0, 2.0], "n": [3, 4]},
                    {"quantity": ["mu", "tau"], "mean": [1.0 + 1e-9, None], "n": [3, 4]},
                    {"quantity": ["mu", "tau"], "mean": [1.0, None], "n": [3, 5]},
                    {"quantity": ["mu", "tau"], "mean": [1.0], "n": [3, 4]}):
            with self.assertRaises(AssertionError):
                assert_same_numbers(self, a, bad)


if __name__ == "__main__":
    unittest.main()
