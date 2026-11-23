"""Fake draws for the tests of the second pass (lot X3): a layout (sampler fields, four scalars, a correlation, a plate of units) and a
long draws parquet written by the contract's DrawsWriter, block by block, from values that depend only on (seed, chain, first draw),
so a test can rebuild the same numbers in memory (`block`) and a large file never needs a large array."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parent / "py")]

import numpy as np  # noqa: E402

import bayes_draws  # noqa: E402
from bayes_task import Element, Entry, Layout  # noqa: E402

SAMPLER = ("lp__", "accept_stat__", "divergent__")
SCALARS = ("mu", "tau", "beta", "delta")


def make_layout(quantities: int, shuffle: bool = False) -> Layout:
    """`quantities` program scalars: mu, tau, beta, delta, the correlation rho[b,a] and quantities - 5 units of the plate «school»."""
    units = max(0, quantities - 5)
    draws = [Element(n, None, None, None, None) for n in SAMPLER]
    entries = []
    for name in SCALARS:
        entries.append(Entry(name, name, name.upper(), name, "parameter", None, None, len(draws), 1, True, False))
        draws.append(Element(name, name, None, None, None))
    entries.append(Entry("rho", "rho", "Correlation", None, "correlation", None, None, len(draws), 1, False, True))
    draws.append(Element("rho[b,a]", "rho", "b,a", 2, 1))
    if units:
        entries.append(Entry("alpha", "alpha", "School effect", "alpha", "effect", "school", "School", len(draws), units, False, False))
        draws.extend(Element(f"alpha[s{i}]", "alpha", f"s{i}", i + 1, None) for i in range(units))
    if shuffle:
        draws[3], draws[4] = draws[4], draws[3]
    p = len(draws)
    return Layout("sample", [e.quantity for e in draws], p, np.arange(p, dtype=np.int64), draws, np.zeros(0, dtype=np.int64), [],
                  entries, len(SAMPLER))


def block(seed: int, chain: int, first: int, count: int, p: int, divergent_every: int = 0) -> np.ndarray:
    """The block (count, p) of a chain from its first draw: sampler fields, then quantities with their own location and scale."""
    rng = np.random.default_rng([seed, chain, first])
    out = np.round(rng.normal(size=(count, p)) * (1.0 + 0.1 * np.arange(p)) + 0.5 * np.arange(p) + 0.05 * chain, 6)
    out[:, 0], out[:, 1], out[:, 2] = -10.0 - rng.random(count), rng.random(count), 0.0
    if divergent_every:
        out[(np.arange(first, first + count) % divergent_every) == 0, 2] = 1.0
    return out


def write(path, layout: Layout, chains: int, draws: int, block_draws: int, seed: int = 1, divergent_every: int = 0,
          keep: bool = False):
    """Writes the draws file; returns the full array (quantities, chains, draws) of the program's quantities when `keep`."""
    p = len(layout.draws)
    writer = bayes_draws.DrawsWriter(path, layout.draws, layout.draw_columns, chains, draws, block_draws, {"seed": seed},
                                     n_unchecked=layout.n_sampler)
    full = np.empty((p, chains, draws)) if keep else None
    for chain, first, count in writer.schedule():
        b = block(seed, chain, first, count, p, divergent_every)
        writer.write_block(chain, first, b)
        if full is not None:
            full[:, chain - 1, first - 1:first - 1 + count] = b.T
    writer.close()
    return full
