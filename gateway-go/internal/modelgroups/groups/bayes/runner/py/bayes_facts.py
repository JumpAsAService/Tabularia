"""The sampler's facts, as accumulators fed by each block while the CSV is converted (docs/specs/bayes-diagnostics.md, «The
sampler's facts», lot X2): per chain the divergent draws, the draws at the largest tree depth and the deepest tree, E-BFMI, the
step size and the means of the acceptance statistic and of the number of leapfrog steps; and the same for all the chains.
Facts only: no threshold, no verdict, no reference value.

The accumulator is a sink of the follower (`write_block(chain, first_draw, block)`, `abort()`): `block` is draws x ALL the CSV's
columns in the header's order; the accumulator keeps a few numbers a chain and no reference to the block. Blocks of a chain
must arrive in order (the follower's order: block-major, chain-minor).

The results do not depend on the size of the blocks, bit for bit: counts are integers, and every sum of floats is kept
**exactly** (Shewchuk's expansion, the algorithm of Python's math.fsum, kept between blocks as a few partial sums) and rounded
once at the end, so no order of additions changes a bit. E-BFMI follows ArviZ 1.3.0 (arviz-stats `_bfmi`): the mean of the
squared differences of successive energies, over N - 1 differences, divided by the energies' variance with divisor N - 1.
The variance comes from exact sums of the energies' offsets from the chain's first energy (the offset keeps the subtraction
benign) instead of Welford's recurrence, which is not the same bit for two block sizes. The energy of the previous block's last
draw is kept for the first difference of the next.

A column the CSV does not have (a fixed_param run writes only `lp__` and `accept_stat__`) gives None for the facts that rest on
it; so does E-BFMI of a chain with one draw or a constant energy. A non-finite value is refused (the writers refuse it first).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any

import numpy as np

ACCEPT, STEPSIZE, TREEDEPTH, LEAPFROG, DIVERGENT, ENERGY = (
    "accept_stat__", "stepsize__", "treedepth__", "n_leapfrog__", "divergent__", "energy__")
FACTS = ("draws", "divergences", "treedepth_hits", "treedepth_max", "e_bfmi", "stepsize", "accept_stat_mean", "n_leapfrog_mean")


class ExactSum:
    """A sum of floats kept exactly as a short list of non-overlapping partial sums (Shewchuk 1997; math.fsum's algorithm)."""

    __slots__ = ("partials",)

    def __init__(self) -> None:
        self.partials: list[float] = []

    def add(self, values: np.ndarray) -> None:
        partials = self.partials
        for x in values.tolist():
            i = 0
            for y in partials:
                if abs(x) < abs(y):
                    x, y = y, x
                hi = x + y
                lo = y - (hi - x)
                if lo:
                    partials[i] = lo
                    i += 1
                x = hi
            partials[i:] = [x]

    def value(self) -> float:
        """The sum rounded once, to the nearest double."""
        return math.fsum(self.partials)

    def exact(self) -> Fraction:
        return sum((Fraction(p) for p in self.partials), Fraction(0))


class _Chain:
    def __init__(self) -> None:
        self.draws = 0
        self.divergences = 0
        self.hits = 0
        self.depth_max: int | None = None
        self.accept = ExactSum()
        self.step = ExactSum()
        self.leapfrog = ExactSum()
        self.diff2 = ExactSum()      # sum of (E_n - E_{n-1})^2
        self.offset_sum = ExactSum()  # sum of (E_n - E_1)
        self.offset_sq = ExactSum()   # sum of (E_n - E_1)^2
        self.first_energy: float | None = None
        self.last_energy: float | None = None


class FactsAccumulator:
    """The follower's sink for the sampler's facts. `header` is the CSV's header, `chains` their number, `max_treedepth` the
    node's (the draws whose `treedepth__` equals it are the hits; None counts none)."""

    def __init__(self, header: Sequence[str], chains: int, max_treedepth: int | None) -> None:
        names = list(header)
        self.column = {name: names.index(name) for name in (ACCEPT, STEPSIZE, TREEDEPTH, LEAPFROG, DIVERGENT, ENERGY) if name in names}
        self.columns = len(names)
        self.max_treedepth = max_treedepth
        self.chains = {c: _Chain() for c in range(1, chains + 1)}

    def write_block(self, chain: int, first_draw: int, block: np.ndarray) -> None:
        state = self.chains[chain]
        if block.ndim != 2 or block.shape[1] != self.columns:
            raise ValueError(f"a block of {block.shape} for a header of {self.columns} columns")
        if first_draw != state.draws + 1:
            raise ValueError(f"chain {chain}: the block starts at draw {first_draw} but {state.draws} draws came before")
        if not len(block):
            return
        used = [block[:, i] for i in self.column.values()]
        if not all(np.isfinite(col).all() for col in used):
            raise ValueError(f"chain {chain}: a sampler column holds a value that is not finite")
        col = lambda name: block[:, self.column[name]] if name in self.column else None  # noqa: E731
        if (x := col(DIVERGENT)) is not None:
            state.divergences += int(np.count_nonzero(x == 1))
        if (x := col(TREEDEPTH)) is not None:
            if self.max_treedepth is not None:
                state.hits += int(np.count_nonzero(x == self.max_treedepth))
            deepest = int(x.max())
            state.depth_max = deepest if state.depth_max is None else max(state.depth_max, deepest)
        for name, acc in ((ACCEPT, state.accept), (STEPSIZE, state.step), (LEAPFROG, state.leapfrog)):
            if (x := col(name)) is not None:
                acc.add(x)
        if (e := col(ENERGY)) is not None:
            if state.first_energy is None:
                state.first_energy = float(e[0])
            shifted = e - state.first_energy
            state.offset_sum.add(shifted)
            state.offset_sq.add(shifted * shifted)
            edge = np.concatenate(([state.last_energy], e)) if state.last_energy is not None else e
            d = np.diff(edge)
            state.diff2.add(d * d)
            state.last_energy = float(e[-1])
        state.draws += len(block)

    def abort(self) -> None:
        """Nothing is written anywhere: there is nothing to delete."""
        self.chains = {c: _Chain() for c in self.chains}

    def facts(self, step_sizes: Mapping[int, float | None] | None = None) -> dict[Any, dict[str, Any]]:
        """{chain: facts, ..., "all": facts}. `step_sizes` are the step sizes after the adaptation (the follower's
        ChainFacts.step_size, the CSV's comment); where one is missing the mean of the draws' `stepsize__` stands in. For "all":
        the sums of the counts, the deepest tree, the smallest E-BFMI, the step size, the acceptance and the leapfrog means
        pooled over the draws (the step size the mean of the chains')."""
        step_sizes = step_sizes or {}
        per: dict[Any, dict[str, Any]] = {}
        for c, s in self.chains.items():
            n = s.draws
            has = self.column.keys()
            step = step_sizes.get(c)
            if step is None and STEPSIZE in has and n:
                step = s.step.value() / n
            per[c] = {
                "draws": n,
                "divergences": s.divergences if DIVERGENT in has else None,
                "treedepth_hits": s.hits if TREEDEPTH in has and self.max_treedepth is not None else None,
                "treedepth_max": s.depth_max,
                "e_bfmi": self._e_bfmi(s),
                "stepsize": step,
                "accept_stat_mean": s.accept.value() / n if ACCEPT in has and n else None,
                "n_leapfrog_mean": s.leapfrog.value() / n if LEAPFROG in has and n else None,
            }
        per["all"] = self._all(per)
        return per

    @staticmethod
    def _e_bfmi(s: _Chain) -> float | None:
        n = s.draws
        if s.first_energy is None or n < 2:
            return None
        # sum (E - mean)^2 = sum d^2 - (sum d)^2 / n for d the offsets, in exact arithmetic on the exact sums
        total, squares = s.offset_sum.exact(), s.offset_sq.exact()
        m2 = squares - total * total / n
        if m2 <= 0:
            return None
        return float((s.diff2.exact() / (n - 1)) / (m2 / (n - 1)))

    def _all(self, per: dict[Any, dict[str, Any]]) -> dict[str, Any]:
        rows = [per[c] for c in self.chains]
        total = sum(r["draws"] for r in rows)

        def present(key: str) -> list[Any]:
            return [r[key] for r in rows if r[key] is not None]

        def pooled(attr: str, key: str) -> float | None:
            if not present(key) or not total:
                return None
            return math.fsum(getattr(s, attr).value() for s in self.chains.values()) / total

        steps = present("stepsize")
        return {
            "draws": total,
            "divergences": sum(present("divergences")) if present("divergences") else None,
            "treedepth_hits": sum(present("treedepth_hits")) if present("treedepth_hits") else None,
            "treedepth_max": max(present("treedepth_max")) if present("treedepth_max") else None,
            "e_bfmi": min(present("e_bfmi")) if present("e_bfmi") else None,
            "stepsize": math.fsum(steps) / len(steps) if steps else None,
            "accept_stat_mean": pooled("accept", "accept_stat_mean"),
            "n_leapfrog_mean": pooled("leapfrog", "n_leapfrog_mean"),
        }
