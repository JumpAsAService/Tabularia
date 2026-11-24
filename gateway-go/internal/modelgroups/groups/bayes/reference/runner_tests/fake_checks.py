"""A synthetic checks file for the tests of the predictive checks' second pass (lot P3b): a task with one checked outcome `y`, its plan
observed in pass A on a small table, and a `checks.parquet` written by the contract's DrawsWriter, block by block, with the statistics, the
bin counts and the unit sums of replicated data a seeded generator draws (what the program writes per draw: never a replicated row). The
draws of a block depend only on (seed, chain, first draw), so a large file never needs a large array; with `keep` the statistics of every
draw are returned in the chain-major order for the test to compare."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_checks  # noqa: E402
import bayes_data  # noqa: E402
import bayes_draws  # noqa: E402
import bayes_task  # noqa: E402
from test_checks import extension_entries, make_task  # noqa: E402

UNITS = "abcdefghij"


def table(n: int, kind: str = "continuous", units: bool = True, seed: int = 7) -> dict:
    """The observed table: `n` rows, the unit of a row cycling over ten with unequal weights."""
    rng = np.random.default_rng(seed)
    out = {}
    if units:
        weights = np.arange(len(UNITS), 0, -1, dtype=float)
        out["grp"] = list(rng.choice(list(UNITS), size=n, p=weights / weights.sum()))
    if kind == "continuous":
        out["y"] = [float(v) for v in np.round(rng.normal(1.0, 1.5, n), 4)]
    elif kind == "counts":
        out["y"] = [int(v) for v in rng.poisson(2.5, n)]
    else:
        out["y"] = [int(v) for v in rng.integers(0, 2, n)]
    return out


def prepared(root: Path, n: int = 60, kind: str = "continuous", units: bool = True, run: str = "checks", checks: dict | None = None):
    """(task, plan, facts, layout): the plan observed on the table, the layout of the checks' columns expanded with the run's sizes."""
    data, quantities = extension_entries(0, kind=kind, units=units, y_kind="real" if kind == "continuous" else "int")
    quantities = quantities + [{"stan": "Ppc_o1_count", "role": "check", "dims": [{"size": "Ppc_o1_slots"}], "check": "bin_count", "of": "y"}]
    if units:
        quantities.append({"stan": "Ppc_o1_usum", "role": "check", "dims": [{"size": "Ppc_o1_units"}], "check": "unit_sum", "of": "y"})
    task = make_task(data, quantities, **({"checks": checks} if checks else {}))
    plan = bayes_checks.plan(task)
    path = root / "input.parquet"
    pq.write_table(pa.table(table(n, kind, units)), str(path), row_group_size=7)
    facts = bayes_data.prepare(task, str(path), root / "work", batch_rows=11, checks=plan, run=run)
    layout = bayes_task.build_layout(task, facts.sizes)
    return task, plan, facts, layout


def statistic(name: str, y: np.ndarray) -> float:
    if name == "mean":
        return float(y.mean())
    if name == "sd":
        return float(y.std(ddof=1))
    if name == "min":
        return float(y.min())
    if name == "max":
        return float(y.max())
    if name == "zeros":
        return float((y == 0).sum())
    return float(np.quantile(y, {"q10": 0.1, "q50": 0.5, "q90": 0.9}[name]))


def simulate(plan, kind: str, seed: int, chain: int, first: int):
    """One draw's replicated data (the program's `Ppc_o1_y`)."""
    o = plan.outcomes[0]
    rng = np.random.default_rng([seed, chain, first])
    if kind == "continuous":
        return rng.normal(rng.normal(1.0, 0.4), 1.5, o.n)
    if kind == "counts":
        return rng.poisson(np.exp(rng.normal(0.9, 0.2)), o.n).astype(float)
    return (rng.random(o.n) < rng.uniform(0.3, 0.7)).astype(float)


def write(path, plan, layout, kind: str, chains: int, draws: int, block_draws: int, seed: int = 3, nan_draws: tuple = (), keep: bool = False):
    """The checks file; `nan_draws` are (chain, draw) whose every check is NaN (the program threw). Returns, with `keep`, the draws in the file's
    order: {"valid": bool array, "stats": {name: values}, "counts": (draws, slots), "usum": (draws, units) or None}."""
    o = plan.outcomes[0]
    meta = layout.checks
    writer = bayes_draws.DrawsWriter(path, meta, layout.check_columns, chains, draws, block_draws, {"seed": seed}, n_unchecked=len(meta),
                                     kind="checks")
    names = [m.quantity for m in meta]
    where = {n: i for i, n in enumerate(names)}
    order: list[tuple[int, int]] = []
    record = {"chain": [], "draw": [], "valid": [], "stats": {s: [] for s in o.statistics}, "counts": [], "usum": []}
    for chain, first, count in writer.schedule():
        block = np.zeros((count, layout.n_columns))
        for d in range(count):
            y = simulate(plan, kind, seed, chain, first + d)
            row = {}
            for s in o.statistics:
                row[f"Ppc_o1_{s}[1]"] = statistic(s, y)
            counts = np.bincount(o.bins.slot_of(y) - 1, minlength=o.bins.slots)
            for i, c in enumerate(counts, 1):
                row[f"Ppc_o1_count[{i}]"] = float(c)
            usum = None
            if o.unit_texts:
                usum = np.bincount(o.slots, weights=y, minlength=o.units + 1)[1:o.units + 1]
                for i, v in enumerate(usum, 1):
                    row[f"Ppc_o1_usum[{i}]"] = float(v)
            ok = (chain, first + d) not in nan_draws
            for name, v in row.items():
                block[d, layout.check_columns[where[name]]] = v if ok else np.nan
            if keep:
                record["chain"].append(chain)
                record["draw"].append(first + d)
                record["valid"].append(ok)
                for s in o.statistics:
                    record["stats"][s].append(row[f"Ppc_o1_{s}[1]"])
                record["counts"].append(counts)
                record["usum"].append(usum)
        writer.write_block(chain, first, block)
        order.append((chain, first))
    writer.close()
    if not keep:
        return None
    at = np.lexsort((record["draw"], record["chain"]))               # the chain-major order, whatever the blocks were
    return {"valid": np.array(record["valid"])[at], "stats": {s: np.array(v)[at] for s, v in record["stats"].items()},
            "counts": np.array(record["counts"])[at], "usum": np.array(record["usum"])[at] if o.unit_texts else None}
