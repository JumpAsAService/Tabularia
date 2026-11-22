"""The results fixture of the foundation forecast node (docs/specs/foundation-forecast.md, «The results table», «Explaining
the method in the report»): one table, made from three imaginary series of orders per day (a weekly pattern and a trend), so that
the report lane (F4) can be built and `results.py` can be tried before the runner exists.

    cd backend && python -m tests.models_fixtures timestral

The numbers are placeholders made here by formulas (a forecast is the series' own pattern, its bands widen with the distance, the
baseline is the same weekday of the last week): consistent with each other, never the model's. The comparison is Wilcoxon's
signed-rank test on three series, whose smallest two-sided p-value is 0.25: its verdict cannot be «supported» (the spec's
reading says why a test with few series cannot reach alpha). Every row the catalogue declares for the variant that a run with
these parameters writes is here, and the `reading` rows list the templates of the spec as `@id name=value` (the notes' format).
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from tests.models_fixtures_decomposition import Table, note

EPOCH = date(1970, 1, 1)

PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("timestral", "default"): {"time": "day", "value": "orders", "series": "channel", "frequency": "day", "season": 7, "size": "28m",
                               "horizon": 14, "holdout": 14, "quantiles": [0.05, 0.1, 0.9, 0.95], "output": "quantile_forecast"},
}
SERIES = ["Online", "Phone", "Shop"]
LEVELS = {"q0.05": 0.05, "q0.1": 0.1, "q0.9": 0.9, "q0.95": 0.95}
# the standard normal quantile of each level, to widen a band
Z = {0.05: -1.644854, 0.1: -1.281552, 0.9: 1.281552, 0.95: 1.644854}
SIZE = {"5m": "Timestral-5M", "28m": "Timestral-28M", "123m": "Timestral-123M", "209m": "Timestral-209M"}
WEIGHTS = {"28m": ("mistralai/Timestral-28M", "49b26409ac87b5935539a5d8ffe7f397b720db47",
                   "dfc83c6220937ce5fc6687b35a4df94cd25e28a2cfe6fb92a23dad6301b2015d")}
LICENCE_SHA = "648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c"


def _level(channel: int, i: int) -> float:
    """The imaginary orders of a channel on day i (a weekly pattern, a trend, a deterministic wobble)."""
    base = [60.0, 25.0, 40.0][channel] + 0.05 * i
    weekly = [8.0, 3.0, 5.0][channel] * math.sin(2 * math.pi * i / 7 + channel)
    wobble = 1.5 * (math.sin(i * 12.9898 + channel * 7.1) * 43758.5453 % 1.0 - 0.5)
    return base + weekly + wobble


def build(model: str, variant: str, params: dict[str, Any], spec: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
    t = Table()
    n_each, horizon, holdout, season = 120, checked["horizon"], checked["holdout"], checked["season"]
    start = (date(2026, 3, 1) - EPOCH).days
    days = [start + i for i in range(n_each)]
    cut = n_each - holdout  # the first holdout instant
    size = checked["size"]
    repo, revision, weights_sha = WEIGHTS[size]
    notes: list[tuple[str, str | None, str]] = []

    def say(block: str, level_2: str | None, id_: str, **values: Any) -> None:
        notes.append((block, level_2, note(id_, **values)))

    # ── the three series: observed, the holdout's forecast, the forecast, the baseline ──────────────────────────────────
    measures: list[dict[str, float]] = []
    chart: list[tuple[str, str, list[float], list[float]]] = []  # (series, line, x, y)
    for c, name in enumerate(SERIES):
        y = [_level(c, i) for i in range(n_each)]
        scale = sum(abs(y[i] - y[i - season]) for i in range(season, cut)) / (cut - season)
        hold_med = [_level(c, cut + h) * 0.99 + 0.2 * c for h in range(holdout)]
        fut_med = [_level(c, n_each + h) + 0.1 * c for h in range(horizon)]
        base_hold = [y[cut - season + (h % season)] for h in range(holdout)]
        base_fut = [y[n_each - season + (h % season)] for h in range(horizon)]
        width = 1.6 + 0.6 * c

        def band(med: list[float], q: float) -> list[float]:
            return [m + Z[q] * width * math.sqrt(1 + 0.15 * h) for h, m in enumerate(med)]

        mase = sum(abs(y[cut + h] - hold_med[h]) for h in range(holdout)) / holdout / scale
        mase_b = sum(abs(y[cut + h] - base_hold[h]) for h in range(holdout)) / holdout / scale
        smape = sum(200 * abs(y[cut + h] - hold_med[h]) / (abs(y[cut + h]) + abs(hold_med[h])) for h in range(holdout)) / holdout
        smape_b = sum(200 * abs(y[cut + h] - base_hold[h]) / (abs(y[cut + h]) + abs(base_hold[h])) for h in range(holdout)) / holdout
        lo80, hi80, lo90, hi90 = band(hold_med, 0.1), band(hold_med, 0.9), band(hold_med, 0.05), band(hold_med, 0.95)
        in80 = sum(lo80[h] <= y[cut + h] <= hi80[h] for h in range(holdout))
        in90 = sum(lo90[h] <= y[cut + h] <= hi90[h] for h in range(holdout))
        pinball = sum(max(q * (y[cut + h] - band(hold_med, q)[h]), (q - 1) * (y[cut + h] - band(hold_med, q)[h]))
                      for q in LEVELS.values() for h in range(holdout)) / (holdout * len(LEVELS))
        mean_abs = sum(abs(y[cut + h]) for h in range(holdout)) / holdout
        measures.append({"mase": mase, "baseline_mase": mase_b, "relative_mase": mase / mase_b, "smape": smape, "baseline_smape": smape_b,
                         "pinball": pinball, "wql": pinball / mean_abs, "coverage_80": in80 / holdout, "coverage_90": in90 / holdout,
                         "in80": in80, "in90": in90, "scale": scale, "total": sum(abs(v) for v in y)})
        m = measures[-1]
        for stat, value in (("n", n_each), ("n_used", n_each), ("n_missing", 0), ("n_filled", 0), ("first", days[0]), ("last", days[-1]),
                            ("horizon", horizon), ("scale", scale), ("mase", m["mase"]), ("baseline_mase", m["baseline_mase"]),
                            ("relative_mase", m["relative_mase"]), ("smape", smape), ("baseline_smape", smape_b), ("pinball", pinball),
                            ("wql", m["wql"]), ("coverage_80", m["coverage_80"]), ("coverage_90", m["coverage_90"]), ("total", m["total"])):
            t.add("series", stat, value, level=name)
        history = max(3 * horizon, 4 * season)  # the last instants of the history drawn
        shown = list(range(n_each - history, n_each))
        chart.append((name, "observed", [days[i] for i in shown], [y[i] for i in shown]))
        fx = [days[-1] + 1 + h for h in range(horizon)]
        hx = [days[cut + h] for h in range(holdout)]
        chart.append((name, "median", fx, fut_med))
        chart.append((name, "baseline", fx, base_fut))
        for line, q in LEVELS.items():
            chart.append((name, line, fx, band(fut_med, q)))
            chart.append((name, "holdout_" + line, hx, band(hold_med, q)))
        chart.append((name, "holdout_median", hx, hold_med))

    # ── the comparison: Wilcoxon's signed-rank test on the three series' differences of MASE ───────────────────────────
    diffs = [m["mase"] - m["baseline_mase"] for m in measures]
    order = sorted(range(3), key=lambda i: abs(diffs[i]))
    w_plus = sum(rank + 1 for rank, i in enumerate(order) if diffs[i] > 0)
    w = min(w_plus, 6 - w_plus)
    # the exact two-sided p-value: the share of the 2^3 sign patterns of the ranks 1, 2, 3 whose smaller sum is at most W
    p = sum(min(s, 6 - s) <= w for s in (sum(r for r, up in zip((1, 2, 3), signs) if up)
                                          for signs in ((a, b, c) for a in (0, 1) for b in (0, 1) for c in (0, 1)))) / 8
    better = sum(d < 0 for d in diffs)
    median_d = sorted(diffs)[1]
    alpha = checked["alpha"]
    t.add("assumption", "h0", text="the differences of MASE between Timestral and the seasonal naïve are symmetric around 0",
          level="baseline_comparison")
    t.add("assumption", "statistic", float(w), level="baseline_comparison")
    t.add("assumption", "p_value", p, level="baseline_comparison")
    t.add("assumption", "significance", text="", level="baseline_comparison")
    t.add("assumption", "verdict", text="ok" if p >= alpha else "failed", level="baseline_comparison")
    t.add("assumption", "count", 3, level="baseline_comparison")
    t.add("assumption", "value", median_d, level="baseline_comparison")
    pooled = {k: sum(m[k] for m in measures) / 3 for k in ("coverage_80", "coverage_90")}
    geomean = math.exp(sum(math.log(m["relative_mase"]) for m in measures) / 3)
    t.add("assumption", "value", pooled["coverage_80"], level="coverage_80")
    t.add("assumption", "value", pooled["coverage_90"], level="coverage_90")
    t.add("assumption", "value", geomean, level="relative_mase")

    # ── the model section ───────────────────────────────────────────────────────────────────────────────────────────
    texts = {"model": model, "variant": variant,
             "formula": f"orders ~ {SIZE[size]} · 3 series by channel · daily · {horizon} days ahead",
             "software": "Python 3.14.2; torch 2.14.1+cpu; timestral 1.0.0 (a534bdbd)", "group_version": "1", "rows_from": "trial",
             "note_1": "A placeholder note: this table is a fixture.", "frequency": checked["frequency"], "time_kind": "date",
             "missing": checked["missing"], "size": size, "weights": f"{repo}@{revision[:8]}", "weights_sha256": weights_sha,
             "licence": "Modified MIT License (Mistral AI)", "licence_sha256": LICENCE_SHA, "cpu_capability": "AVX2"}
    numbers = {"n": 3 * n_each, "n_series": 3, "n_points": 3 * n_each, "n_missing": 0, "n_filled": 0, "n_model_missing": 0,
               "n_future_rows": 0, "horizon": horizon, "holdout": holdout, "season": season, "alpha": alpha, "threads": 2, "batches": 1,
               "seconds_load": 1.4, "seconds_forecast": 0.9, "n_quantiles": len(checked["quantiles"]),
               "mase": sum(m["mase"] for m in measures) / 3, "baseline_mase": sum(m["baseline_mase"] for m in measures) / 3,
               "relative_mase_geomean": geomean, "smape": sum(m["smape"] for m in measures) / 3,
               "baseline_smape": sum(m["baseline_smape"] for m in measures) / 3, "wql": sum(m["wql"] for m in measures) / 3,
               "coverage_80": pooled["coverage_80"], "coverage_90": pooled["coverage_90"], "n_measured": 3}
    for stat in spec["sections"]["model"]["texts"]:
        if stat == "note_*":
            t.add("model", "note_1", text=texts["note_1"])
        else:
            t.add("model", stat, text=texts[stat])
    for stat in spec["sections"]["model"]["numbers"]:
        t.add("model", stat, numbers[stat])

    # ── the reading: the templates a run says ───────────────────────────────────────────────────────────────────────
    say("summary", None, "summary", size=SIZE[size], n_series=3, value=checked["value"], series=checked["series"], horizon=horizon,
        last=float(days[-1]), context=n_each)
    first = measures[0]
    say("forecast", "Online", "forecast_bands", median=fut_med_value(chart, "Online", "median"), low80=fut_med_value(chart, "Online", "q0.1"),
        high80=fut_med_value(chart, "Online", "q0.9"), low90=fut_med_value(chart, "Online", "q0.05"),
        high90=fut_med_value(chart, "Online", "q0.95"), at=float(days[-1] + 1))
    say("holdout", None, "holdout_mase", holdout=holdout, mase=numbers["mase"], baseline_mase=numbers["baseline_mase"])
    say("coverage", None, "coverage", in80=first["in80"], in90=first["in90"], steps=holdout, share80=first["coverage_80"],
        share90=first["coverage_90"])
    say("comparison", None, "comparison_not_supported", better=better, n_series=3, p_value=p, alpha=alpha)
    say("limits", None, "limits")
    say("licence", None, "licence", licence=texts["licence"])
    say("method", None, "method_zero_shot")
    say("formulas", None, "formula_mase")
    counters: dict[tuple[str, str | None], int] = {}
    for block, level_2, text in notes:
        counters[(block, level_2)] = counters.get((block, level_2), 0) + 1
        t.add("reading", f"line_{counters[(block, level_2)]}", text=text, level=block, level_2=level_2)

    # ── the charts' lines ───────────────────────────────────────────────────────────────────────────────────────────
    for name, line, xs, ys in chart:
        for i, (x, v) in enumerate(zip(xs, ys)):
            t.add("series_curve", "y", v, level=name, level_2=line, x0=x, index=i)
    return {"model": model, "variant": variant, "params": params, "rows": t.rows}


def fut_med_value(chart: list[tuple[str, str, list[float], list[float]]], name: str, line: str) -> float:
    """The first forecast value of a line of a series."""
    return next(ys[0] for n, l, _, ys in chart if n == name and l == line)
