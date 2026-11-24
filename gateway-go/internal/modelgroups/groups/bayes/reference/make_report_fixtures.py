"""Writes the two results fixtures of the Bayesian model node's report (docs/specs/bayes-report.md, «Testing strategy»), the ten-column rows
of a run of each variant, with the runner's own writers on fake draws:

    backend/tests/fixtures/models/bayes_model-sample.json
    backend/tests/fixtures/models/bayes_model-prior_predictive.json

By hand, never in CI (the fixtures are committed). From the repository root, in any Python with numpy and pyarrow (the engine's image has them):

    docker run --rm --memory 3g --user "$(id -u)" -v "$PWD:/repo" -w /repo tabularia-backend:ci \\
        python -I -B gateway-go/internal/modelgroups/groups/bayes/reference/make_report_fixtures.py

The model is the template «hier_slopes» in everything but its data: a plate of six schools with a correlation between their intercepts and
slopes, four scalars (the mean and the spread of the intercepts, the effect of the hours studied, the residual spread) and a linear
predictor whose predictor was standardized. The draws are made here (a normal around a location for each quantity, a little apart from chain
to chain, two divergent draws in the third chain), `bayes_pass2.second_pass` measures them and writes the summary, the quantity measures and the
charts, and the sampler, quantity, model and data sections are written with the section writers' own sentences. The hashes, the seconds and
the facts of the sampler are imaginary: consistent with each other, never a run's. The original-scale numbers are the runner's own (`bayes_scale`:
the coefficient over the predictor's sd, the schools' intercepts moved by the predictor's mean), on the fake draws. Every row goes through the contract's declaration
(`bayescontract/testdata/contract.json`) and the catalogue's shapes, as the runner's writer checks them.

The checks' rows (`ppc` in the sample's, `prior_check` in the prior predictive's, with the overlaid densities) are those of the second pass over a
checks file (`bayes_ppc`), made on the fake checks file of the runner's tests (`runner_tests/fake_checks.py`): the outcome is the score of the
six schools, 588 rows as the fixture says, replicated data of a normal around the score's mean. The prior predictive's checks file has three draws
the program threw (all missing), so that its note of left-out draws is on the page.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[5]
sys.path[:0] = [str(HERE.parents[2] / "common" / "py"), str(HERE.parent / "runner" / "py"), str(HERE / "runner_tests")]

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_draws  # noqa: E402
import bayes_pass2  # noqa: E402
import bayes_rows  # noqa: E402
import bayes_scale  # noqa: E402
import fake_checks  # noqa: E402
from bayes_task import Element, Entry, Layout  # noqa: E402

OUT = REPO / "backend" / "tests" / "fixtures" / "models"
COLUMNS = ["section", "level", "level_2", "stat", "value", "text", "x0", "x1", "ord", "key"]
SCHOOLS = ["Liceo Galilei", "Istituto Volta", "Liceo Manzoni", "ITIS Fermi", "Liceo Pascoli", "Istituto Marconi"]
CHAINS, DRAWS, WARMUP, SEED = 4, 250, 500, 20261123
# location and spread of each quantity of the program, in the layout's order; `positive`: the draws are those of a positive number
SCALARS = [("mu_a", "Mean of the intercepts", 6.1, 0.34, False, "parameter"), ("tau_a", "Spread of the intercepts", 0.92, 0.22, True, "parameter"),
           ("b_hours", "Effect of the hours studied", 0.42, 0.06, False, "parameter"), ("sigma", "Residual spread", 1.8, 0.08, True, "parameter")]
UNIT_MEANS = [6.9, 5.4, 6.3, 5.8, 6.6, 5.1]
HOURS = (6.21, 2.4)  # the standardized predictor's mean and sd
# the charts' points, fewer than the catalogue's defaults (200, 200, 300) so that a fixture stays a few hundred kilobytes
LIMITS = {"bayes_trace_points": 40, "bayes_density_points": 50, "bayes_pair_points": 40}


def layout(mode: str) -> Layout:
    """The program's quantities after the sampler's fields: the four scalars, the correlation, the six schools' intercepts."""
    sampler = ("lp__", "accept_stat__", "divergent__") if mode == "sample" else ("lp__", "accept_stat__")
    draws = [Element(n, None, None, None, None) for n in sampler]
    entries = []
    for stan, label, *_ , kind in SCALARS:
        entries.append(Entry(stan, "n_" + stan, label, stan, kind, None, None, len(draws), 1, True, False))
        draws.append(Element(stan, stan, None, None, None))
    entries.append(Entry("rho_g", "n_rho", "Correlation of the intercepts and the slopes", None, "correlation", None, None, len(draws), 1, False, True))
    draws.append(Element("rho_g[b_g,a_g]", "rho_g", "b_g,a_g", 2, 1))
    entries.append(Entry("alpha_school", "n_alpha", "Intercept of the school", "alpha_school", "effect", "school", "School", len(draws), len(SCHOOLS),
                         False, False))
    draws.extend(Element(f"alpha_school[{u}]", "alpha_school", u, i + 1, None) for i, u in enumerate(SCHOOLS))
    p = len(draws)
    return Layout(mode, [e.quantity for e in draws], p, np.arange(p, dtype=np.int64), draws, np.zeros(0, dtype=np.int64), [], entries, len(sampler))


def location(q: int) -> tuple[float, float, bool]:
    """Location, spread, positive of the program's q-th quantity."""
    if q < len(SCALARS):
        return SCALARS[q][2], SCALARS[q][3], SCALARS[q][4]
    if q == len(SCALARS):
        return -0.31, 0.2, False
    return UNIT_MEANS[q - len(SCALARS) - 1], 0.42, False


def block(lay: Layout, chain: int, first: int, count: int, sampling: bool) -> np.ndarray:
    """The draws of a chain from its draw `first`: the sampler's fields, then the program's quantities (8 significant digits, as CmdStan's)."""
    rng = np.random.default_rng([SEED, chain, first])
    p, ns = len(lay.draws), lay.n_sampler
    out = np.zeros((count, p))
    out[:, 0] = -148.0 - rng.gamma(4.0, 1.4, count)
    out[:, 1] = np.clip(rng.beta(8, 2, count), 0, 1) if sampling else 0.0
    if sampling:
        out[:, 2] = 0.0
        if chain == 3:
            out[(np.arange(first, first + count) % 113) == 0, 2] = 1.0
    for k in range(p - ns):
        mean, sd, positive = location(k)
        shift = 0.015 * sd * (chain - 2.5)  # the chains are a little apart
        if positive:
            out[:, ns + k] = mean * np.exp(rng.normal(0, sd / mean, count) + shift)
        else:
            out[:, ns + k] = mean + shift + sd * rng.normal(size=count)
    return np.array([[float(f"{v:.8g}") for v in row] for row in out])


def write_draws(path: Path, lay: Layout, chains: int, draws: int, sampling: bool) -> np.ndarray:
    writer = bayes_draws.DrawsWriter(str(path), lay.draws, lay.draw_columns, chains, draws, 100, {"seed": SEED}, n_unchecked=lay.n_sampler)
    full = np.empty((len(lay.draws), chains, draws))
    for chain, first, count in writer.schedule():
        b = block(lay, chain, first, count, sampling)
        writer.write_block(chain, first, b)
        full[:, chain - 1, first - 1:first - 1 + count] = b.T
    writer.close()
    return full


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PriorWriter(bayes_rows.ResultsWriter):
    """The writer of the prior predictive variant, which declares no `hist` and no `point` section (the contract: the rank histograms and
    the units' charts are the sample mode's): `bayes_pass2` writes them in both modes, so the rows are dropped here, as the declaration
    would refuse them."""

    def points(self, section, level, stat, x, y, level_2=None, labels=None, x1=None):
        if section not in ("hist", "point"):
            super().points(section, level, stat, x, y, level_2, labels, x1)


class Stub:
    """What `quantity_section` asks of a task: the limits' defaults."""

    def limit(self, name: str) -> int:
        return bayes_pass2.limit({}, name)


def checks_rows(w: bayes_rows.ResultsWriter, variant: str, tmp: Path):
    """The predictive checks' rows of the variant, from a fake checks file of 4 × 250 draws of the replicated score (the outcome is `score`, the
    units the six schools); returns the second pass's report."""
    shift, n = 5.1, 588
    real_table, real_simulate = fake_checks.table, fake_checks.simulate
    fake_checks.UNITS = SCHOOLS
    fake_checks.table = lambda n, kind="continuous", units=True, seed=7: {
        **real_table(n, kind, units, seed), "y": [round(v + shift, 4) for v in real_table(n, kind, units, seed)["y"]]}
    fake_checks.simulate = lambda plan, kind, seed, chain, first: real_simulate(plan, kind, seed, chain, first) + shift
    try:
        sampling = variant == "sample"
        root = tmp / f"{variant}-checks"
        root.mkdir()
        task, plan, facts, layout = fake_checks.prepared(root, n=n, kind="continuous", units=True, run="checks" if sampling else "prior")
        plan.outcomes[0].stan = "score"
        path = root / "checks.parquet"
        lost = () if sampling else ((2, 17), (3, 200), (4, 9))  # the draws the program threw
        fake_checks.write(str(path), plan, layout, "continuous", CHAINS, DRAWS, 125, seed=SEED % 1000, nan_draws=lost)
        return plan.rows(str(path), w, "ppc" if sampling else "prior_check")
    finally:
        fake_checks.table, fake_checks.simulate = real_table, real_simulate


def declaration(variant: str):
    contract = json.loads((REPO / "gateway-go" / "internal" / "bayescontract" / "testdata" / "contract.json").read_text(encoding="utf-8"))
    shapes = json.loads((REPO / "backend" / "app" / "engine" / "models" / "catalog.json").read_text(encoding="utf-8"))["section_shapes"]
    return contract["variants"][variant], {**shapes, **contract["shapes"]}


def prior_of(lay: Layout, sampling: bool):
    """The draws of each quantity's prior (wide: the data narrow it), for the density charts."""
    rng = np.random.default_rng([SEED, 99])

    def prior(position: int):
        k = position - lay.n_sampler
        if k < 0:
            return None
        mean, sd, positive = location(k)
        if positive:
            return np.abs(rng.normal(0, 2.5, 600))
        return rng.normal(mean if sampling else 0.0, 3.0 if k != len(SCALARS) else 0.6, 600)

    return prior


def model_rows(w: bayes_rows.ResultsWriter, variant: str, lay: Layout, report, seconds: dict[str, float], n_quantities: int) -> None:
    sampling = variant == "sample"
    w.text("model", "model", "bayes_model")
    w.text("model", "variant", variant)
    w.text("model", "rows_from", "run")
    w.text("model", "group_version", "3")
    w.text("model", "software", "CmdStan 2.40.0; stanc3 v2.40.0; Python 3.14.2; numpy 2.5.5; pyarrow 26.0.1")
    w.text("model", "bayes_model", "17")
    w.text("model", "bayes_version", "4")
    w.text("model", "semantic_hash", sha("hier_slopes semantics"))
    w.text("model", "program_sha256", sha("hier_slopes program"))
    w.text("model", "binary_sha256", sha("hier_slopes binary"))
    w.text("model", "sampler", "nuts" if sampling else "fixed_param")
    w.text("model", "metric", "diag_e")
    w.text("model", "precision", "8 significant digits")
    for stat, value in (("n", 588), ("n_missing", 12), ("n_not_finite", 0), ("chains", CHAINS), ("warmup", WARMUP if sampling else 0),
                        ("draws", DRAWS), ("seed", SEED), ("adapt_delta", 0.8), ("max_treedepth", 10), ("threads", 4),
                        ("n_quantities", n_quantities)):
        w.number("model", stat, value)
    for stat, value in seconds.items():
        w.number("model", f"seconds_{stat}", value)


def data_rows(w: bayes_rows.ResultsWriter) -> None:
    """The bound variables: the outcome, a standardized predictor with its missing values, the schools, a category."""
    for stan, column, kind, std, extra in (("score", "exam_score", "real", None, {"n_missing": 0}),
                                           ("hours", "hours_studied", "real", HOURS, {"n_missing": 12}),
                                           ("school", "school_name", "group_index", None, {"n_units": len(SCHOOLS), "n_missing": 0}),
                                           ("track", "track", "category", None, {"n_levels": 3, "n_missing": 0, "reference": "Scientific"})):
        w.text("data", "column", column, level=stan)
        w.text("data", "type", kind, level=stan)
        w.text("data", "standardized", "yes" if std else "no", level=stan)
        if std:
            w.number("data", "mean", std[0], level=stan)
            w.number("data", "sd", std[1], level=stan)
        for stat, value in extra.items():
            (w.text if stat == "reference" else w.number)("data", stat, value, level=stan)


def sampler_rows(w: bayes_rows.ResultsWriter, full: np.ndarray, lay: Layout) -> None:
    facts = {}
    for chain in range(1, CHAINS + 1):
        divergent = int(full[2, chain - 1].sum())
        facts[chain] = {"divergences": divergent, "treedepth_hits": 0, "treedepth_max": 5 + (chain % 2), "e_bfmi": 0.86 + 0.02 * chain,
                        "stepsize": 0.31 + 0.012 * chain, "accept_stat_mean": float(full[1, chain - 1].mean()), "n_leapfrog_mean": 15.0 + chain,
                        "draws": DRAWS}
    facts["all"] = {"divergences": sum(f["divergences"] for f in facts.values()), "treedepth_hits": 0, "treedepth_max": 6,
                    "e_bfmi": min(f["e_bfmi"] for f in facts.values()), "stepsize": float(np.mean([f["stepsize"] for f in facts.values()])),
                    "accept_stat_mean": float(full[1].mean()), "n_leapfrog_mean": 16.5, "draws": CHAINS * DRAWS}
    bayes_pass2.sampler_section(w, facts, {c: (11.0 + c, 14.0 + c) for c in range(1, CHAINS + 1)})


def scale_plan(lay: Layout) -> bayes_scale.ScalePlan:
    """The coefficient of the standardized predictor and the schools' intercepts on the column's scale, by the runner's own lot for them
    (`bayes_scale`, the task's `scale_pairs`): the hours' mean and sd are the data section's."""
    pairs = [{"coef": "b_hours", "data": "hours"}, {"intercept": "alpha_school", "lp": ["b_hours"], "data": ["hours"]}]
    return bayes_scale.plan(pairs, lay, {"hours": {"mean": HOURS[0], "sd": HOURS[1]}}, bayes_pass2.limit({}, "bayes_max_quantity_entries"))


def make(variant: str, tmp: Path) -> dict:
    sampling = variant == "sample"
    lay = layout(variant)
    draws_path = tmp / f"{variant}-draws.parquet"
    full = write_draws(draws_path, lay, CHAINS, DRAWS, sampling)
    dec, shapes = declaration(variant)
    w = (bayes_rows.ResultsWriter if sampling else PriorWriter)(str(tmp / f"{variant}-results.parquet"), declaration=dec, shapes=shapes,
                                                                flush_rows=2000)
    entries = bayes_rows.quantity_section(w, Stub(), lay)
    report = bayes_pass2.second_pass(str(draws_path), lay, LIMITS, w, summary_path=str(tmp / f"{variant}-summary.parquet"), diagnostics=sampling,
                                     prior=prior_of(lay, sampling), seed=SEED, quantity_entries=entries,
                                     scale=scale_plan(lay) if sampling else None)
    checks = checks_rows(w, variant, tmp)
    data_rows(w)
    if sampling:
        sampler_rows(w, full, lay)
        seconds = {"data": 0.4, "warmup": 15.0, "sampling": 18.0, "conversion": 0.6, "pass_2": round(report.seconds, 3),
                   "checks": round(checks.seconds, 3)}
    else:
        seconds = {"data": 0.4, "sampling": 0.3, "conversion": 0.2, "pass_2": round(report.seconds, 3),
                   "checks": round(checks.seconds, 3)}
    model_rows(w, variant, lay, report, seconds, len(lay.draws) - lay.n_sampler)
    w.close()
    rows = pq.read_table(tmp / f"{variant}-results.parquet").to_pylist()
    ordered = sorted(rows, key=lambda r: r["ord"])
    params = {"mode": variant} if not sampling else {"mode": "sample", "output": "summary"}
    return {
        "model": "bayes_model", "variant": variant, "params": params,
        "fixture": ("a run of the template hier_slopes on imaginary draws, the runner's writers (reference/make_report_fixtures.py): "
                    "the hashes, the seconds and the sampler's facts are placeholders; the predictive checks' rows are those of the second pass on a fake checks file"),
        "columns": COLUMNS,
        "rows": [[(None if r[c] is None else round(r[c], 12) + 0.0) if c in ("value", "x0", "x1") else r[c] for c in COLUMNS] for r in ordered],
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        for variant in ("sample", "prior_predictive"):
            fixture = make(variant, Path(tmp))
            path = OUT / f"bayes_model-{variant}.json"
            path.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{path.relative_to(REPO)}: {len(fixture['rows'])} rows")


if __name__ == "__main__":
    main()
