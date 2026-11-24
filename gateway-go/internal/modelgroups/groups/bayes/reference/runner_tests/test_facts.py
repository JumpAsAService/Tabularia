"""The sampler's facts (lot X2 of docs/specs/bayes-diagnostics.md): the accumulators fed block by block (1, 7, 1,000 draws and a
block cut at random) against the same facts computed on the whole fake chain of reference/diagnostics/cases.json («facts»):
counts to the last bit, every float to the last bit between block sizes, E-BFMI against ArviZ 1.3.0's (witness.json) within the
spec's 1e-10, the energy kept across a block's edge, the refusals, a fixed_param header, and constant memory. Small data only."""
import json
import math
import random
import sys
import tracemalloc
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py")]
WITNESSES = HERE.parents[1] / "reference" / "diagnostics"

import numpy as np  # noqa: E402

import bayes_facts as F  # noqa: E402

HEADER = ["lp__", "accept_stat__", "stepsize__", "treedepth__", "n_leapfrog__", "divergent__", "energy__", "mu", "sigma"]
CASE = json.loads((WITNESSES / "cases.json").read_text(encoding="utf-8"))["facts"]
WITNESS = json.loads((WITNESSES / "witness.json").read_text(encoding="utf-8"))["arviz"]["facts"]["e_bfmi"]
MAX_DEPTH = CASE["max_treedepth"]


def chain_matrix(c: int, accept=None, step=0.37) -> np.ndarray:
    """The whole chain c (0-based) of the fake run as the CSV's columns; accept and the step size are made up, deterministically."""
    energy = np.array(CASE["energy__"][c], dtype=np.float64)
    n = len(energy)
    rng = np.random.default_rng(c + 1)
    m = np.zeros((n, len(HEADER)))
    m[:, 1] = rng.uniform(0.5, 1.0, n) if accept is None else accept
    m[:, 2] = step * (1 + c)
    m[:, 3] = CASE["treedepth__"][c]
    m[:, 4] = 2.0 ** m[:, 3] - 1
    m[:, 5] = CASE["divergent__"][c]
    m[:, 6] = energy
    m[:, 7:] = rng.normal(size=(n, 2))
    return m


def feed(matrices, sizes, header=HEADER, depth=MAX_DEPTH):
    """Blocks of `sizes` (an int or a list cycled) draws, block-major and chain-minor as the follower hands them."""
    acc = F.FactsAccumulator(header, len(matrices), depth)
    n = len(matrices[0])
    cuts, at, k = [0], 0, 0
    while at < n:
        at = min(n, at + (sizes if isinstance(sizes, int) else sizes[k % len(sizes)]))
        cuts.append(at)
        k += 1
    for a, b in zip(cuts, cuts[1:]):
        for c, m in enumerate(matrices, start=1):
            acc.write_block(c, a + 1, m[a:b])
    return acc


def arviz_bfmi(e: np.ndarray) -> float:
    return float(np.mean(np.diff(e) ** 2) / np.var(e, ddof=1))


class TheFakeRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chains = [chain_matrix(c) for c in range(4)]
        cls.whole = feed(cls.chains, 1000).facts()

    def test_the_counts_are_those_of_the_whole_chain(self):
        for c in range(4):
            got = self.whole[c + 1]
            self.assertEqual(got["divergences"], int(sum(CASE["divergent__"][c])))
            self.assertEqual(got["treedepth_hits"], sum(1 for t in CASE["treedepth__"][c] if t == MAX_DEPTH))
            self.assertEqual(got["treedepth_max"], int(max(CASE["treedepth__"][c])))
            self.assertEqual(got["draws"], 1000)
        a = self.whole["all"]
        self.assertEqual(a["divergences"], int(sum(map(sum, CASE["divergent__"]))))
        self.assertEqual(a["treedepth_hits"], sum(self.whole[c]["treedepth_hits"] for c in range(1, 5)))
        self.assertEqual(a["treedepth_max"], max(self.whole[c]["treedepth_max"] for c in range(1, 5)))
        self.assertEqual(a["draws"], 4000)
        self.assertGreater(a["divergences"], 0)

    def test_e_bfmi_equals_arviz(self):
        for c in range(4):
            got = self.whole[c + 1]["e_bfmi"]
            self.assertLess(abs(got - WITNESS[c]) / abs(WITNESS[c]), 1e-10, (c, got, WITNESS[c]))
            self.assertLess(abs(got - arviz_bfmi(np.array(CASE["energy__"][c]))) / got, 1e-10)
        self.assertEqual(self.whole["all"]["e_bfmi"], min(self.whole[c]["e_bfmi"] for c in range(1, 5)))

    def test_the_gap_with_arviz_is_recorded(self):
        gaps = [abs(self.whole[c + 1]["e_bfmi"] - WITNESS[c]) / abs(WITNESS[c]) for c in range(4)]
        print(f"\nE-BFMI largest relative gap with ArviZ 1.3.0: {max(gaps):.3e}", file=sys.stderr)
        self.assertLess(max(gaps), 1e-10)

    def test_the_means(self):
        for c in range(4):
            m = self.chains[c]
            got = self.whole[c + 1]
            self.assertAlmostEqual(got["accept_stat_mean"], math.fsum(m[:, 1]) / 1000, places=15)
            self.assertAlmostEqual(got["n_leapfrog_mean"], math.fsum(m[:, 4]) / 1000, places=9)
            self.assertAlmostEqual(got["stepsize"], 0.37 * (1 + c), places=15)   # the mean of stepsize__ stands in
        self.assertAlmostEqual(self.whole["all"]["accept_stat_mean"], math.fsum(np.concatenate([m[:, 1] for m in self.chains])) / 4000, places=15)

    def test_the_adaptation_step_size_wins(self):
        facts = feed(self.chains, 1000).facts({1: 0.5, 2: None, 3: 0.25, 4: 0.125})
        self.assertEqual([facts[c]["stepsize"] for c in (1, 3, 4)], [0.5, 0.25, 0.125])
        self.assertAlmostEqual(facts[2]["stepsize"], 0.74, places=15)
        self.assertEqual(facts["all"]["stepsize"], math.fsum([0.5, facts[2]["stepsize"], 0.25, 0.125]) / 4)


class NoMatterTheBlocks(unittest.TestCase):
    def test_every_block_size_gives_the_same_bits(self):
        chains = [chain_matrix(c) for c in range(4)]
        rng = random.Random(7)
        cuts = [rng.randint(1, 300) for _ in range(40)]
        reference = feed(chains, 1000).facts()
        for sizes in (1, 7, 64, 999, cuts):
            self.assertEqual(feed(chains, sizes).facts(), reference, sizes)

    def test_hard_floats_every_block_size_gives_the_same_bits(self):
        # energies of very different sizes: the order of additions would change the last bits of a plain sum
        rng = np.random.default_rng(3)
        n = 700
        base = rng.normal(size=n) * 10.0 ** rng.integers(-8, 8, n)
        m = np.zeros((n, len(HEADER)))
        m[:, 1] = rng.uniform(size=n) * 10.0 ** rng.integers(-6, 6, n)
        m[:, 2] = rng.uniform(size=n)
        m[:, 3] = rng.integers(1, 6, n)
        m[:, 6] = 1e6 + base
        reference = feed([m, m[::-1].copy()], n, depth=5).facts()
        for sizes in (1, 3, 50, 333):
            self.assertEqual(feed([m, m[::-1].copy()], sizes, depth=5).facts(), reference, sizes)

    def test_the_energy_crosses_a_block_edge(self):
        e = np.array([1.0, 4.0, 2.0, 7.0, 3.0, 5.0])
        m = np.zeros((6, len(HEADER)))
        m[:, 6] = e
        got = feed([m], 2).facts()[1]["e_bfmi"]
        self.assertEqual(got, feed([m], 6).facts()[1]["e_bfmi"])
        self.assertAlmostEqual(got, arviz_bfmi(e), places=14)
        self.assertAlmostEqual(got, np.mean(np.diff(e) ** 2) / np.var(e, ddof=1), places=14)


class Edges(unittest.TestCase):
    def test_fixed_param_has_no_sampler_columns(self):
        header = ["lp__", "accept_stat__", "x"]
        m = np.column_stack([np.zeros(10), np.zeros(10), np.arange(10.0)])
        facts = feed([m, m], 4, header=header).facts({1: None, 2: None})
        self.assertEqual(facts[1], {"draws": 10, "divergences": None, "treedepth_hits": None, "treedepth_max": None, "e_bfmi": None,
                                    "stepsize": None, "accept_stat_mean": 0.0, "n_leapfrog_mean": None})
        self.assertEqual(facts["all"]["divergences"], None)
        self.assertEqual(facts["all"]["draws"], 20)

    def test_one_draw_and_a_constant_energy_give_none(self):
        one = np.zeros((1, len(HEADER)))
        self.assertIsNone(feed([one], 1).facts()[1]["e_bfmi"])
        flat = np.zeros((20, len(HEADER)))
        flat[:, 6] = 3.0
        self.assertIsNone(feed([flat], 5).facts()[1]["e_bfmi"])

    def test_no_max_treedepth_counts_no_hits(self):
        m = chain_matrix(0)
        self.assertIsNone(feed([m], 100, depth=None).facts()[1]["treedepth_hits"])

    def test_a_gap_or_a_repeat_is_refused(self):
        acc = F.FactsAccumulator(HEADER, 1, 10)
        m = chain_matrix(0)
        acc.write_block(1, 1, m[:10])
        with self.assertRaises(ValueError):
            acc.write_block(1, 5, m[10:20])
        with self.assertRaises(ValueError):
            acc.write_block(1, 12, m[10:20])
        with self.assertRaises(ValueError):
            acc.write_block(1, 11, m[10:20, :5])

    def test_a_non_finite_value_is_refused_not_summed(self):
        m = chain_matrix(0)[:10].copy()
        m[3, 6] = math.inf
        with self.assertRaises(ValueError):
            F.FactsAccumulator(HEADER, 1, 10).write_block(1, 1, m)
        m = chain_matrix(0)[:10].copy()
        m[3, 1] = math.nan
        with self.assertRaises(ValueError):
            F.FactsAccumulator(HEADER, 1, 10).write_block(1, 1, m)

    def test_the_sink_has_the_followers_interface(self):
        acc = F.FactsAccumulator(HEADER, 2, 10)
        acc.write_block(1, 1, chain_matrix(0)[:5])
        acc.abort()
        self.assertEqual(acc.facts()[1]["draws"], 0)
        self.assertFalse(hasattr(acc, "block_draws"))   # any block size suits it


class Memory(unittest.TestCase):
    def test_what_is_kept_does_not_grow_with_the_draws(self):
        def kept(draws):
            m = chain_matrix(0)
            acc = F.FactsAccumulator(HEADER, 1, MAX_DEPTH)
            tracemalloc.start()
            for k in range(draws // 1000):
                acc.write_block(1, k * 1000 + 1, m)
            size, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return size, peak, acc

        small, _, a1 = kept(1000)
        large, _, a2 = kept(10000)
        self.assertLess(large - small, 20_000)
        self.assertLess(max(len(s) for c in a2.chains.values() for s in (c.accept.partials, c.step.partials, c.leapfrog.partials,
                                                                          c.diff2.partials, c.offset_sum.partials, c.offset_sq.partials)), 40)


if __name__ == "__main__":
    unittest.main()
