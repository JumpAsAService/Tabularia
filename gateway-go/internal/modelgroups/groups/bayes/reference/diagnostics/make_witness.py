"""The second witness of the diagnostics (docs/specs/bayes-diagnostics.md, «Witnesses», lot X0): ArviZ on the draws of
cases.json, beside the numbers of the first witness (the R package posterior 1.8.0, written into witness.json by
make_witness.R, which runs first). It adds to witness.json ArviZ's numbers, ArviZ's E-BFMI of the fake run's energy, the
Python closure it ran with and every difference between the two witnesses larger than the spec's relative 1e-8, each with
its reason (read in both sources; the runner follows posterior, the spec's decision). A difference without a reason
stops the script (exit 1): a new one is read and explained here before witness.json is committed.

ArviZ pinned (Apache-2.0): arviz==1.3.0, which brings arviz-stats 1.3.3 (the computations, its numpy array interface
`arviz_stats.base.array_stats`) and arviz-base 1.3.1; witness.json lists the whole closure. Never installed in the group's
image; run by hand, rarely, never in CI. One way, in a throw-away container of any Python 3.12+ image, with a scratch
directory of packages:

    docker run --rm --memory 2g --cpus 2 --user "$(id -u):$(id -g)" -e HOME=/tmp -w /tmp -v <scratch>:/wp \\
      python:3.12-slim pip install --target /wp arviz==1.3.0 numpy==2.5.5
    docker run --rm --memory 2g --cpus 2 --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONPATH=/wp -w /tmp -v <scratch>:/wp:ro \\
      -v "$PWD/gateway-go/internal/modelgroups/groups/bayes/reference/diagnostics:/ref" \\
      python:3.12-slim python /ref/make_witness.py
"""
from __future__ import annotations

import importlib.metadata as metadata
import json
import math
import platform
import sys
from pathlib import Path

import numpy as np
from arviz_stats.base import array_stats as az

HERE = Path(__file__).resolve().parent
ARVIZ = "1.3.0"
TOLERANCE = 1e-8  # the spec's relative tolerance for R-hat, ESS and MCSE

# Why ArviZ 1.3.0 and posterior 1.8.0 differ, read in both sources (arviz_stats/base/diagnostics.py of arviz-stats 1.3.3;
# R/convergence.R and R/split_chains.R of posterior 1.8.0, transcribed in definitions.md). Keyed by measure, or by
# «case/measure» where the reason belongs to one case.
REASONS = {
    "one_chain/rhat": "ArviZ's rank R-hat needs two chains before the split (min_chains=2: NaN); posterior splits the one "
                      "chain in two and computes it",
    "one_chain/rhat_bulk": "ArviZ's R-hat needs two chains before the split (min_chains=2: NaN); posterior computes it",
    "one_chain/rhat_tail": "ArviZ's R-hat needs two chains before the split (min_chains=2: NaN); posterior computes it",
    "odd/rhat_tail": "with an odd number of draws ArviZ folds around the median of the split draws (the middle draw left "
                     "out), posterior around the median of all the draws (fold_draws before .split_chains)",
    "odd/rhat": "the folded R-hat's median, as odd/rhat_tail (when it is the larger of the two)",
    "short/rhat_tail": "7 draws a chain, an odd number: the median of the fold, as odd/rhat_tail",
    "short/rhat": "the folded R-hat's median, as short/rhat_tail (the larger of the two)",
    "too_short/rhat_tail": "5 draws a chain, an odd number: the median of the fold, as odd/rhat_tail (the runner gives NULL: "
                           "the contract's rule, fewer than 3 draws a split chain)",
    "ess_q95": "for the upper quantile posterior's indicator is x >= q (.ess_quantile: «the upper-tail indicator mirrors the "
               "lower-tail indicator»), ArviZ's x <= q; they differ by the draws equal to q: ties and atoms",
    "ess_tail": "the minimum of ess_q5 and ess_q95: ess_q95 differs (see it)",
    "mcse_q95": "from ess_q95 (see it)",
    "short/ess_bulk": "split chains of 3 draws: posterior's initial positive sequence stops before its first pair (max_t = 0) "
                      "and R's 1:0 counts lag 0 twice (tau = 2); ArviZ sums lag 0 once (tau = 0, floored at 1/log10(S))",
    "short/ess_mean": "as short/ess_bulk", "short/ess_sd": "as short/ess_bulk", "short/ess_q5": "as short/ess_bulk",
    "short/ess_q95": "as short/ess_bulk", "short/ess_tail": "as short/ess_bulk", "short/mcse_mean": "from short/ess_mean",
    "short/mcse_sd": "from short/ess_sd", "short/mcse_q5": "from short/ess_q5", "short/mcse_q95": "from short/ess_q95",
    "too_short/ess_bulk": "ArviZ computes an ESS from 2 draws a split chain; posterior's .ess returns NA below 3",
    "too_short/ess_mean": "as too_short/ess_bulk", "too_short/ess_sd": "as too_short/ess_bulk",
    "too_short/ess_q5": "as too_short/ess_bulk", "too_short/ess_q95": "as too_short/ess_bulk",
    "too_short/ess_tail": "as too_short/ess_bulk", "too_short/mcse_mean": "from too_short/ess_mean",
    "too_short/mcse_sd": "from too_short/ess_sd", "too_short/mcse_q5": "from too_short/ess_q5",
    "too_short/mcse_q95": "from too_short/ess_q95",
    "constant/ess_bulk": "a constant quantity: posterior NA (should_return_NA), ArviZ the number of draws (its _ess returns "
                         "ary.size when max - min < the float resolution)",
    "constant/ess_mean": "as constant/ess_bulk", "constant/ess_sd": "as constant/ess_bulk",
    "constant/ess_q5": "as constant/ess_bulk", "constant/ess_q95": "as constant/ess_bulk",
    "constant/ess_tail": "as constant/ess_bulk", "constant/mcse_mean": "posterior NA; ArviZ 0 / sqrt(S) = 0",
    "constant/mcse_q5": "posterior NA; ArviZ (x - x) / 2 = 0", "constant/mcse_q95": "as constant/mcse_q5",
    "mcse_sd": "the same formula written two ways: posterior mean((s - E s)^2), ArviZ mean(s^2) - (E s)^2 (cancellation)",
}


def num(v) -> float | None:
    v = float(np.asarray(v))
    return v if math.isfinite(v) else None


def measures(x: np.ndarray) -> dict:
    """ArviZ's numbers for draws shaped (chains, draws), the names of make_witness.R's."""
    with np.errstate(all="ignore"):
        q = np.quantile(x, [0.05, 0.25, 0.5, 0.75, 0.95], method="linear")
        return {
            "chains": x.shape[0], "draws": x.shape[1],
            "mean": num(x.mean()), "sd": num(x.std(ddof=1)),
            "q5": num(q[0]), "q25": num(q[1]), "q50": num(q[2]), "q75": num(q[3]), "q95": num(q[4]),
            "rhat": num(az.rhat(x, method="rank")),
            "rhat_bulk": num(az.rhat(x, method="z_scale")),
            "rhat_tail": num(az.rhat(x, method="folded")),
            "ess_bulk": num(az.ess(x, method="bulk")),
            "ess_tail": num(az.ess(x, method="tail", prob=0.05)),
            "ess_q5": num(az.ess(x, method="quantile", prob=0.05)),
            "ess_q95": num(az.ess(x, method="quantile", prob=0.95)),
            "ess_mean": num(az.ess(x, method="mean")),
            "ess_sd": num(az.ess(x, method="sd")),
            "mcse_mean": num(az.mcse(x, method="mean")),
            "mcse_sd": num(az.mcse(x, method="sd")),
            "mcse_q5": num(az.mcse(x, method="quantile", prob=0.05)),
            "mcse_q95": num(az.mcse(x, method="quantile", prob=0.95)),
        }


def gap(a: float | None, b: float | None) -> float | None:
    """The relative gap of ArviZ's a from posterior's b; None when both are null; inf when only one is."""
    if a is None and b is None:
        return None
    if a is None or b is None:
        return math.inf
    return abs(a - b) / max(abs(b), 1e-300) if b != 0 else abs(a)


def main() -> int:
    assert metadata.version("arviz") == ARVIZ, metadata.version("arviz")
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    doc = json.loads((HERE / "witness.json").read_text(encoding="utf-8"))
    if "posterior" not in doc or "arviz" in doc:
        print("witness.json must be make_witness.R's, fresh: run make_witness.R first", file=sys.stderr)
        return 1
    ours = {}
    differences = []
    unexplained = 0
    for c in cases["cases"]:
        x = np.asarray(c["draws"], dtype=float)
        ours[c["name"]] = m = measures(x)
        theirs = doc["posterior"]["cases"][c["name"]]
        for k, v in m.items():
            if k in ("chains", "draws"):
                continue
            g = gap(v, theirs[k])
            if g is None or g <= TOLERANCE:
                continue
            reason = REASONS.get(f"{c['name']}/{k}") or REASONS.get(k)
            if reason is None:
                unexplained += 1
                reason = "UNEXPLAINED"
            differences.append({"case": c["name"], "measure": k, "posterior": theirs[k], "arviz": v,
                                "relative_gap": None if math.isinf(g) else g, "reason": reason})
            print(f"{c['name']:16s} {k:10s} posterior {theirs[k]!r:24s} arviz {v!r:24s} gap {g:.3g}  {reason[:60]}")
    energy = np.asarray(cases["facts"]["energy__"], dtype=float)
    facts = {"e_bfmi": [num(v) for v in az.bfmi(energy)],
             "note": "arviz-stats 1.3.3's _bfmi: mean(diff(E)^2) over var(E, ddof=1) per chain (the spec's formula divides "
                     "by the variance with divisor N)"}
    doc["arviz"] = {"version": ARVIZ, "cases": ours, "facts": facts}
    doc["differences"] = differences
    doc["python"] = [f"Python {platform.python_version()}"] + sorted(
        f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions(path=[p for p in sys.path if p]) if d.metadata["Name"])
    text = json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    (HERE / "witness.json").write_text(text, encoding="utf-8")
    print(f"witness.json: ArviZ {ARVIZ} on {len(ours)} cases, {len(differences)} differences, {unexplained} unexplained")
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
