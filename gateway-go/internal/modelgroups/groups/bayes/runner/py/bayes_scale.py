"""The original scale of a linear predictor's coefficients (docs/specs/bayes-runner.md, «The original scale», lot R4).

A standardized predictor enters the model as z = (x - mean) / sd, so the coefficient b of z and the intercept a are on the
column's standardized scale. On the column's own scale the same line has the coefficient b / sd and the intercept
a - sum(b_k * mean_k / sd_k): draw by draw, never from the summaries (the intercept's draws and the coefficients' are correlated).

`bayes.scale_pairs` lists them (the Go side, `bayeschecks.ScalePairs`): a term `{"coef", "data"}` and an intercept
`{"intercept", "lp", "data"}`, whose `intercept` is a quantity (a scalar, or a quantity over a group plate taken at the row's
unit: every element is shifted by the same sum) or null (a predictor without an intercept slot: nothing to shift, no row). The
means and the sds are pass A's facts (sample sd, n - 1), `standardized` below.

`original_scale` reads the pairs' columns of the draws file back by blocks (a row group's `value` column at a time, the same read
as the second pass), computes the five measures (mean, sd, the quantiles) of the original-scale draws a sub-block at a time and
hands each quantity's row to `emit`. Memory: a block of `pass_bytes`, never the number of draws times the number of quantities.
At most `bayes_max_quantity_entries` quantities get a row (the contract's cap, 93,490 result rows).
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

import bayes_diag
from bayes_task import Layout, ManifestMismatch


@dataclass
class TermJob:
    position: int            # the coefficient's column among the draws' quantities
    sd: float


@dataclass
class InterceptJob:
    positions: list[int]                         # the intercept's columns (one for a scalar, one a unit for a plate)
    lp: list[tuple[int, float]]                  # (the term coefficient's column, mean / sd)


@dataclass
class ScalePlan:
    terms: list[TermJob] = field(default_factory=list)
    intercepts: list[InterceptJob] = field(default_factory=list)
    capped: int = 0                              # the quantities left without a row by the cap

    @property
    def count(self) -> int:
        return len(self.terms) + sum(len(j.positions) for j in self.intercepts)


def _scalar(layout: Layout, stan: str) -> int:
    for e in layout.entries:
        if e.stan == stan:
            if not e.scalar:
                raise ManifestMismatch(f"the scale pair's coefficient «{stan}» is not a single value")
            return e.first
    raise ManifestMismatch(f"the scale pair names «{stan}», which the manifest's quantities do not have")


def _moments(standardized: Mapping[str, Mapping[str, float]], data: str) -> tuple[float, float]:
    s = standardized.get(data)
    if s is None or not s["sd"] > 0:
        raise ManifestMismatch(f"the scale pair names the data «{data}», which was not standardized")
    return float(s["mean"]), float(s["sd"])


def plan(pairs: Sequence[Mapping[str, Any]], layout: Layout, standardized: Mapping[str, Mapping[str, float]], cap: int) -> ScalePlan:
    """The jobs of the pairs, in the pairs' order, up to `cap` quantities (an intercept's plate is cut where the cap falls)."""
    out = ScalePlan()
    seen: set[int] = set()
    room = cap
    for pair in pairs:
        if "lp" not in pair:
            p = _scalar(layout, pair["coef"])
            if p in seen:
                continue
            if room < 1:
                out.capped += 1
                continue
            _, sd = _moments(standardized, pair["data"])
            seen.add(p)
            out.terms.append(TermJob(p, sd))
            room -= 1
            continue
        if pair["intercept"] is None:
            continue
        entry = next((e for e in layout.entries if e.stan == pair["intercept"]), None)
        if entry is None:
            raise ManifestMismatch(f"the scale pair names «{pair['intercept']}», which the manifest's quantities do not have")
        lp = []
        for coef, data in zip(pair["lp"], pair["data"]):
            mean, sd = _moments(standardized, data)
            lp.append((_scalar(layout, coef), mean / sd))
        positions = [p for p in range(entry.first, entry.first + entry.count) if p not in seen]
        if len(positions) > room:
            out.capped += len(positions) - room
            positions = positions[:room]
        if positions:
            seen.update(positions)
            out.intercepts.append(InterceptJob(positions, lp))
            room -= len(positions)
    return out


def read_columns(read: Callable[..., Any], groups: Sequence[Sequence[int]], p_all: int, positions: Sequence[int], chains: int,
                 draws: int) -> np.ndarray:
    """The draws of `positions` (columns of the quantity-major long file), shaped (len(positions), chains, draws): one read of
    each row group's `value` column."""
    out = np.empty((len(positions), chains, draws))
    for i, (chain, first_draw, count) in enumerate(groups):
        values = read(i, columns=["value"]).column(0).to_numpy()
        if values.shape[0] != p_all * count:
            raise ManifestMismatch(f"the row group {i + 1} holds {values.shape[0]} values, not {p_all} × {count}")
        for j, p in enumerate(positions):
            out[j, chain - 1, first_draw - 1:first_draw - 1 + count] = values[p * count:(p + 1) * count]
        del values
    return out


def original_scale(scale: ScalePlan, read: Callable[..., Any], groups: Sequence[Sequence[int]], p_all: int, chains: int, draws: int,
                   per_block: int, per_sub: int, emit: Callable[[int, dict[str, np.ndarray], int], None]) -> None:
    """Computes the original-scale measures of every job and calls `emit(position, measures, j)` (measures of a sub-block, `j` the
    quantity's index in it). `per_block` quantities are read at once, `per_sub` measured at once."""

    def measured(block: np.ndarray, positions: Sequence[int]) -> None:
        for a in range(0, len(positions), per_sub):
            b = min(len(positions), a + per_sub)
            m = bayes_diag.measures(block[a:b], diagnostics=False)
            for j in range(b - a):
                emit(positions[a + j], m, j)

    for a in range(0, len(scale.terms), per_block):
        jobs = scale.terms[a:a + per_block]
        block = read_columns(read, groups, p_all, [t.position for t in jobs], chains, draws)
        block /= np.array([t.sd for t in jobs])[:, None, None]
        measured(block, [t.position for t in jobs])
        del block
    for job in scale.intercepts:
        shift = np.zeros((chains, draws))
        for a in range(0, len(job.lp), per_block):
            part = job.lp[a:a + per_block]
            coefs = read_columns(read, groups, p_all, [p for p, _ in part], chains, draws)
            shift += np.tensordot(np.array([w for _, w in part]), coefs, axes=1)
            del coefs
        for a in range(0, len(job.positions), per_block):
            positions = job.positions[a:a + per_block]
            block = read_columns(read, groups, p_all, positions, chains, draws)
            block -= shift[None, :, :]
            measured(block, positions)
            del block
