"""The tests of the group «bayes»'s runner that need no image (docs/specs/bayes-image.md, «Testing strategy», Python): the exact
Kolmogorov–Smirnov p-value of bayes_stats against SciPy's `kstwo` on a few fixed statistics (SciPy is the witness outside the
image, which holds numpy alone), and check 7's comparison on a hand-made CSV: every value equal to the witness's passes, one value
off by more than its tolerance fails and names the case.

Run it by hand from the repository root, with the pins the catalogue's witness uses (nothing in Tabularia runs it):

    uv run --no-project --with scipy==1.18.1 --with numpy==2.5.5 \\
      python -I gateway-go/internal/modelgroups/groups/bayes/reference/test_bayes_runner.py

It prints one line a test and exits 1 when one fails.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent.parent.parent / "common" / "py"), str(HERE.parent / "runner" / "py")]

import numpy as np  # noqa: E402
from scipy.stats import kstwo  # noqa: E402

import bayes_cmdstan  # noqa: E402
import bayes_selftest  # noqa: E402
import bayes_stats  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok     " if ok else "FAILED ") + name + (f": {detail}" if detail else ""), flush=True)
    if not ok:
        FAILED.append(name)


def test_ks_against_scipy() -> None:
    # the p-value of the exact distribution, as the self-test computes it for 4,000 draws, against SciPy's at a relative 1e-7
    # (both are doubles; the product of the 4,000 factors of Marsaglia, Tsang and Wang loses a few digits)
    for n, d in [(10, 0.3), (100, 0.1), (4000, 0.005), (4000, 0.01), (4000, 0.02), (4000, 0.03), (4000, 0.04)]:
        ours, theirs = 1.0 - bayes_stats.ks_cdf(n, d), float(kstwo.sf(d, n))
        check(f"KS p-value n={n} d={d}", abs(ours - theirs) <= 1e-7 * theirs, f"{ours!r} against SciPy's {theirs!r}")
    u = np.random.default_rng(20261123).random(4000)
    d, p = bayes_stats.ks_uniform(u)
    check("KS statistic of 4,000 uniform draws", abs(d - float(np.max(np.abs(np.arange(1, 4001) / 4000 - np.sort(u))))) <= 1 / 4000 and p > 0.001,
          f"D={d:.5f} p={p:.4f}")


def test_points_comparison() -> None:
    points = json.loads((HERE / "data" / "points.json").read_text(encoding="utf-8"))
    layout = json.loads((HERE / "data" / "catalog_points.json").read_text(encoding="utf-8"))["distributions"]
    data, cases, left = bayes_selftest.points_data(points, layout)
    check("the cases of kind marginal are left out", left == sum(c["kind"] == "marginal" for c in points["cases"]) and left > 0, f"{left}")
    check("every other case is given to the program", len(cases) == len(points["cases"]) - left and all(data[f"n_{d}"] >= 0 for d in layout))
    # a CSV as CmdStan writes it, each value the witness's: nothing differs
    header, row = ["lp__", "accept_stat__"], [0.0, 0.0]
    for c in cases:
        header.append(f"lp_{c['dist']}.{c['_index']}")
        row.append(c["logpmf" if c["kind"] == "mass" else "logpdf"])
        if layout[c["dist"]]["lcdf"] and "logcdf" in c:
            header.append(f"lcdf_{c['dist']}.{c['_index']}")
            row.append(c["logcdf"])
    with tempfile.TemporaryDirectory() as tmp:
        csv = Path(tmp) / "points.csv"
        csv.write_text("# a comment\n" + ",".join(header) + "\n" + ",".join(repr(v) for v in row) + "\n", encoding="utf-8")
        names, rows = bayes_cmdstan.read_csv(csv)
    values = dict(zip(names, rows[0]))
    checked, failures = bayes_selftest.compare_points(cases, layout, values)
    check("the witness's own values pass", checked == len(header) - 2 and not failures, f"{checked} values, {failures[:2]}")
    # one log-density off by twice its tolerance, one log-CDF off by twice its own: both fail, named
    gamma = next(c for c in cases if c["dist"] == "gamma" and abs(c["logpdf"]) > 1)
    lp, lcdf = f"lp_gamma.{gamma['_index']}", f"lcdf_gamma.{gamma['_index']}"
    off = dict(values)
    off[lp] = values[lp] * (1 + 2e-10)
    off[lcdf] = values[lcdf] * (1 + 2e-8)
    _, failures = bayes_selftest.compare_points(cases, layout, off)
    check("a value off by more than its tolerance fails and names the case", len(failures) == 2 and all(
        f"gamma{tuple(gamma['args'])} at {gamma['x']!r}" in f for f in failures) and "logpdf" in failures[0] and "logcdf" in failures[1],
        "; ".join(failures))
    # within its tolerance it passes
    near = dict(values)
    near[lp] = values[lp] * (1 + 5e-11)
    _, failures = bayes_selftest.compare_points(cases, layout, near)
    check("a value within its tolerance passes", not failures, "; ".join(failures))


if __name__ == "__main__":
    test_ks_against_scipy()
    test_points_comparison()
    sys.exit(1 if FAILED else 0)
