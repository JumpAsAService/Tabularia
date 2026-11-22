"""The reference cases of the group «foundation_forecast» (docs/specs/foundation-forecast.md, «Self-test, reference values,
witnesses»; lot F3): the tables of reference/data/ff_*.csv (by formula and fixed seeds; ff_types.csv is
make_reference_series.py's) and reference/expected.json, the values the self-test compares the runner with.

The values are computed OUTSIDE Tabularia and outside the group's image, by the official, unmodified `timestral` package at
its commit and the pins of witness.json, the weights read from a local directory filled from the pinned revisions (each
model.safetensors checked here against the recipe's sha256; never committed). Every array the model reads is built again
here from the CSV, by hand (a second construction, not the runner's code), and forecast one series at a time; the measures
are written again by their formulas, Wilcoxon's test is scipy.stats.wilcoxon's. With uv, from the repository's root:

  uv venv --python 3.14 <venv>
  uv pip install --python <venv>/bin/python "torch==2.14.1+cpu" --index-url https://download.pytorch.org/whl/cpu \\
      --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match numpy==2.5.5 safetensors==0.8.0 \\
      huggingface_hub==2.2.0 scipy==1.18.1 pyarrow==26.0.1
  uv pip install --python <venv>/bin/python --no-deps \\
      "timestral @ git+https://github.com/mistralai/timestral.git@a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff"
  <venv>/bin/python gateway-go/internal/modelgroups/groups/foundation_forecast/reference/make_reference.py <weights folder>

<weights folder>/<size>/ holds config.json, model.safetensors and LICENSE of each size (for instance copied out of the
group's image). Two threads; one size in memory at a time. The self-test's tolerances: a quantile within 5e-5 of the
largest quantile of its series (the gap measured between this witness and the runner in the image: at most 6.4e-7 of it,
1.15e-5 on one series of Timestral-5M, the batch's shape and the process — the same CPU — making it), a measure made of
the forecasts within 1e-3 of itself (the quantiles' gap divided by the series' scale), every number made of the data
alone within 1e-12.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
RECIPE = HERE.parent / "recipe.json"
LEVELS = (0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99)
SIZE_ORDER = ("5m", "28m", "123m", "209m")
DAY0 = dt.date(2025, 1, 1)
EPOCH = dt.date(1970, 1, 1)
CHECK_LEVELS = (0.01, 0.1, 0.5, 0.9, 0.99)   # the levels of the quantiles checked, at the first, the middle and the last step
CHECKED_SERIES = 3                            # the series of a case whose quantiles and every measure are checked (the rest: MASE)
SERIES_STATS = ("mase", "baseline_mase", "relative_mase", "smape", "baseline_smape", "pinball", "wql", "coverage_80", "coverage_90")
MAX_CONTEXT = 32768
TOL_QUANTILE = 5e-5
TOL_FORECAST = 1e-3
TOL_DATA = 1e-12
SEASON = {"day": 7, "index": 1}


# ── the tables ──────────────────────────────────────────────────────────────────
def _day(i: int) -> str:
    return (DAY0 + dt.timedelta(days=i)).isoformat()


def _write(name: str, header: list[str], rows: list[list]) -> None:
    DATA.mkdir(exist_ok=True)
    with (DATA / name).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _num(x: float) -> str:
    return f"{x:.4f}"


def write_tables() -> None:
    g = np.random.Generator(np.random.PCG64(20261122))
    # one daily series, a weekly pattern, a trend, noise: 400 days
    _write("ff_daily_one.csv", ["giorno", "ordini"],
           [[_day(i), _num(60 + 8 * math.sin(2 * math.pi * i / 7) + 0.03 * i + g.normal(0, 1.5))] for i in range(400)])
    # three series of different spans and ends
    rows = []
    for name, first, last, level, amp in (("Online", 0, 299, 80.0, 12.0), ("Negozio", 20, 279, 40.0, 6.0),
                                          ("Telefono", 40, 259, 25.0, 3.0)):
        for i in range(first, last + 1):
            rows.append([name, _day(i), _num(level + amp * math.sin(2 * math.pi * i / 7 + 1) + 0.02 * i + g.normal(0, 1.2))])
    _write("ff_daily_three.csv", ["canale", "giorno", "ordini"], rows)
    # two shops: 300 days observed and 30 future days whose covariates are known (holidays, the kind of day), a past price
    # and a past zone; «vuoto» holds nothing (the empty covariate's refusal)
    rows = []
    for s, shop in enumerate(("Milano", "Roma")):
        price = 10.0
        for i in range(330):
            festivo = (i % 7 == 6) or (i % 61 == 17)
            tipo = ("base", "promo", "saldi")[(i // 5 + s) % 3]
            price += g.normal(0, 0.1)
            zona = ("nord", "sud")[(i // 11 + s) % 2]
            value = (120 - 30 * s + 15 * festivo + {"base": 0, "promo": 10, "saldi": 18}[tipo] - 0.5 * price
                     + 6 * math.sin(2 * math.pi * i / 7) + g.normal(0, 2))
            rows.append([shop, _day(i), _num(value) if i < 300 else "", "true" if festivo else "false", tipo, _num(price), zona, ""])
    _write("ff_covariates.csv", ["negozio", "giorno", "vendite", "festivo", "tipo", "prezzo", "zona", "vuoto"], rows)
    # 120 days with three without a value: two empty values (days 30, 31) and a missing row (day 50)
    rows = []
    for i in range(120):
        if i == 50:
            continue
        v = 30 + 5 * math.sin(2 * math.pi * i / 7) + g.normal(0, 0.8)
        rows.append([_day(i), "" if i in (30, 31) else _num(v)])
    _write("ff_gaps.csv", ["giorno", "valore"], rows)
    # the edges: a constant series, one value, a negative series, names that look like code, the known future's refusals
    _write("ff_edge_constant.csv", ["giorno", "valore"], [[_day(i), "5.0000"] for i in range(40)])
    _write("ff_edge_one.csv", ["giorno", "valore"], [[_day(0), "3.0000"]])
    _write("ff_edge_negative.csv", ["giorno", "valore"],
           [[_day(i), _num(-50 + 5 * math.sin(2 * math.pi * i / 7) + g.normal(0, 1))] for i in range(60)])
    _write("ff_edge_names.csv", ["a`b", 'x); system("id")', "ü ñ"],
           [[str(k), _num(10 + 3 * s + math.sin(k / 3 + s) + g.normal(0, 0.2)), ("α", "β")[s]] for s in range(2) for k in range(1, 51)])
    _write("ff_edge_future_gap.csv", ["giorno", "valore", "festivo"],
           [[_day(i), _num(20 + math.sin(i)) if i < 40 else "", "true" if i % 7 == 6 else "false"] for i in range(44) if i != 42])
    _write("ff_edge_future_missing.csv", ["giorno", "valore", "festivo"],
           [[_day(i), _num(20 + math.sin(i)), "true" if i % 7 == 6 else "false"] for i in range(40)])
    # twelve series of 60 days: the comparison's test across series (six with a strong weekly pattern, six without)
    rows = []
    for s in range(12):
        for i in range(60):
            weekly = (10 if s % 2 == 0 else 1) * math.sin(2 * math.pi * i / 7 + s)
            rows.append([f"s{s + 1:02d}", _day(i), _num(50 + 2 * s + weekly + 0.1 * (s - 6) * i + g.normal(0, 1 + s % 3))])
    _write("ff_wilcoxon.csv", ["serie", "giorno", "valore"], rows)


# ── the second construction: series and arrays from the CSV, by hand ────────────
def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _when(text: str, kind: str) -> int:
    return (dt.date.fromisoformat(text) - EPOCH).days if kind == "date" else int(text)


def build_series(rows: list[dict[str, str]], p: dict, kind: str) -> list[dict]:
    """The series of a table: per series (text order of the labels) its span from the first to the last observed value, one
    place per step, the values (NaN where none), the filled values (linear: R's approx), the input row of each place, the
    rows after the last value (the known future)."""
    by: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by.setdefault(r[p["series"]] if p.get("series") else "", []).append(r)
    out = []
    for label in sorted(by):
        rs = sorted(by[label], key=lambda r: _when(r[p["time"]], kind))
        times = [_when(r[p["time"]], kind) for r in rs]
        seen = [i for i, r in enumerate(rs) if r[p["value"]] != ""]
        t0, t1 = times[seen[0]], times[seen[-1]]
        n = t1 - t0 + 1
        y = np.full(n, np.nan)
        at = {}
        for i, r in enumerate(rs):
            if t0 <= times[i] <= t1:
                at[times[i] - t0] = r
                if r[p["value"]] != "":
                    y[times[i] - t0] = float(r[p["value"]])
        future = [r for i, r in enumerate(rs) if times[i] > t1]
        z = y.copy()
        if p.get("missing") == "linear":
            idx = np.flatnonzero(~np.isnan(y))
            for g in np.flatnonzero(np.isnan(y)):
                j = int(np.searchsorted(idx, g))
                lo, hi = idx[j - 1], idx[j]
                z[g] = y[lo] + (y[hi] - y[lo]) * ((g - lo) / (hi - lo))
        out.append({"label": label if p.get("series") else p["value"], "t0": t0, "t1": t1, "y": y, "z": z, "at": at,
                    "future": future, "n": n})
    return out


def covariate_rows(s: dict, p: dict, types: dict[str, str], all_rows: list[dict[str, str]]) -> dict:
    """Each covariate's values on the span's places and on the future rows: numbers as they are, booleans 0/1, texts as the
    code of their label in the text order of the whole column."""
    codes = {}
    for c in [*p.get("past_covariates", []), *p.get("known_covariates", [])]:
        if types.get(c) == "text":
            codes[c] = {v: float(i) for i, v in enumerate(sorted({r[c] for r in all_rows if r[c] != ""}))}

    def value(r: dict[str, str] | None, c: str) -> float:
        if r is None or r[c] == "":
            return math.nan
        if types.get(c) == "boolean":
            return 1.0 if r[c].lower() in ("true", "1") else 0.0
        if types.get(c) == "text":
            return codes[c][r[c]]
        return float(r[c])

    span = {c: np.array([value(s["at"].get(i), c) for i in range(s["n"])]) for c in codes.keys() | set(
        [*p.get("past_covariates", []), *p.get("known_covariates", [])])}
    future = {c: np.array([value(r, c) for r in s["future"]]) for c in p.get("known_covariates", [])}
    return {"span": span, "future": future}


def forecast_one(fc, s: dict, p: dict, types: dict[str, str], covs: dict, end: int, h: int) -> np.ndarray:
    """The official package on one series alone: the context's last values before `end` (at most 32,768), its covariates,
    `h` steps; the quantiles (h, 21) as float64."""
    start = max(0, end - MAX_CONTEXT)
    ctx = s["z"][start:end][None]
    kw = {}
    past = [c for c in p.get("past_covariates", [])]
    known = [c for c in p.get("known_covariates", [])]
    num_p = [covs["span"][c][start:end] for c in past if types.get(c) != "text"]
    cat_p = [covs["span"][c][start:end] for c in past if types.get(c) == "text"]

    def known_row(c: str) -> np.ndarray:
        if end + h <= s["n"]:
            return covs["span"][c][start:end + h]
        return np.concatenate([covs["span"][c][start:end], covs["future"][c][:h]])

    num_k = [known_row(c) for c in known if types.get(c) != "text"]
    cat_k = [known_row(c) for c in known if types.get(c) == "text"]
    if num_p:
        kw["past_covariates"] = np.stack(num_p)[None]
    if cat_p:
        kw["categorical_past_covariates"] = np.stack(cat_p)[None]
    if num_k:
        kw["known_covariates"] = np.stack(num_k)[None]
    if cat_k:
        kw["categorical_known_covariates"] = np.stack(cat_k)[None]
    f = fc.forecast(ctx, h, symmetric_averaging=p.get("symmetric_averaging", True), clip_nonnegative=p.get("clip_nonnegative", True),
                    **kw)
    return f.quantiles[0, 0].numpy().astype(np.float64)


# ── the measures, written again ─────────────────────────────────────────────────
def naive(z: np.ndarray, end: int, h: int, m: int) -> np.ndarray:
    out = []
    for step in range(1, h + 1):
        j = end - 1 + step - m * math.ceil(step / m)
        while j >= 0 and math.isnan(z[j]):
            j -= m
        out.append(z[j] if j >= 0 else math.nan)
    return np.array(out)


def measures(history: np.ndarray, actual: np.ndarray, q: np.ndarray, base: np.ndarray, m: int) -> dict:
    pairs = [abs(history[t] - history[t - m]) for t in range(m, len(history))
             if not (math.isnan(history[t]) or math.isnan(history[t - m]))]
    if not pairs:
        return {"reason": "too_short"}
    s = sum(pairs) / len(pairs)
    if s == 0:
        return {"reason": "zero_scale", "scale": s}
    keep = [t for t in range(len(actual)) if not (math.isnan(actual[t]) or math.isnan(base[t]))]
    k = len(keep)
    med = LEVELS.index(0.5)
    mase = sum(abs(actual[t] - q[t, med]) for t in keep) / k / s
    bmase = sum(abs(actual[t] - base[t]) for t in keep) / k / s

    def smape(f):
        terms = [200 * abs(actual[t] - f[t]) / (abs(actual[t]) + abs(f[t])) for t in keep if abs(actual[t]) + abs(f[t]) > 0]
        return sum(terms) / len(terms) if terms else math.nan

    pin = sum(max(lv * (actual[t] - q[t, j]), (lv - 1) * (actual[t] - q[t, j])) for t in keep for j, lv in enumerate(LEVELS)) / (k * 21)
    mean_abs = sum(abs(actual[t]) for t in keep) / k
    in80 = int(sum(bool(q[t, LEVELS.index(0.1)] <= actual[t] <= q[t, LEVELS.index(0.9)]) for t in keep))
    in90 = int(sum(bool(q[t, LEVELS.index(0.05)] <= actual[t] <= q[t, LEVELS.index(0.95)]) for t in keep))
    return {"reason": None, "scale": float(s), "mase": float(mase), "baseline_mase": bmase, "relative_mase": mase / bmase,
            "smape": smape([q[t, med] for t in range(len(actual))]), "baseline_smape": smape(base), "pinball": pin,
            "wql": pin / mean_abs, "coverage_80": in80 / k, "coverage_90": in90 / k, "steps": k, "in80": in80, "in90": in90,
            "abs_sum": mean_abs * k}


# ── the cases ───────────────────────────────────────────────────────────────────
D = {"giorno": "date"}
SIZE_CASES = [
    {"id": "one", "data": "data/ff_daily_one.csv", "types": {**D, "ordini": "number"}, "every_size": True,
     "params": {"time": "giorno", "value": "ordini", "frequency": "day", "horizon": 28, "holdout": 28,
                "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "three", "data": "data/ff_daily_three.csv", "types": {**D, "ordini": "number", "canale": "text"}, "every_size": True,
     "params": {"time": "giorno", "value": "ordini", "series": "canale", "frequency": "day", "horizon": 14, "holdout": 14,
                "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "covariates", "data": "data/ff_covariates.csv", "every_size": True,
     "types": {**D, "vendite": "number", "negozio": "text", "festivo": "boolean", "tipo": "text", "prezzo": "number", "zona": "text"},
     "params": {"time": "giorno", "value": "vendite", "series": "negozio", "frequency": "day", "known_covariates": ["festivo", "tipo"],
                "past_covariates": ["prezzo", "zona"], "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "wilcoxon", "data": "data/ff_wilcoxon.csv", "types": {**D, "valore": "number", "serie": "text"}, "every_size": True,
     "params": {"time": "giorno", "value": "valore", "series": "serie", "frequency": "day", "horizon": 7, "holdout": 7,
                "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "gaps_linear", "data": "data/ff_gaps.csv", "types": {**D, "valore": "number"}, "every_size": False,
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "horizon": 14, "holdout": 14, "missing": "linear",
                "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "gaps_model", "data": "data/ff_gaps.csv", "types": {**D, "valore": "number"}, "every_size": False,
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "horizon": 14, "holdout": 14, "missing": "model",
                "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "constant", "data": "data/ff_edge_constant.csv", "types": {**D, "valore": "number"}, "every_size": False,
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "horizon": 7, "holdout": 7, "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "one_value", "data": "data/ff_edge_one.csv", "types": {**D, "valore": "number"}, "every_size": False,
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "horizon": 7, "holdout": 7, "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "negative", "data": "data/ff_edge_negative.csv", "types": {**D, "valore": "number"}, "every_size": False,
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "horizon": 7, "holdout": 7, "quantiles": [0.01, 0.1, 0.9, 0.99]}},
    {"id": "names", "data": "data/ff_edge_names.csv", "types": {"a`b": "integer", 'x); system("id")': "number", "ü ñ": "text"},
     "every_size": False,
     "params": {"time": "a`b", "value": 'x); system("id")', "series": "ü ñ", "frequency": "index", "horizon": 5, "holdout": 5,
                "quantiles": [0.01, 0.1, 0.9, 0.99]}},
]
REFUSALS = [
    {"id": "refuse_gaps", "model": "timestral", "data": "data/ff_gaps.csv", "types": {**D, "valore": "number"},
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "size": "5m"},
     "refuses": {"code": "gaps", "message": "the series has 3 periods without a value inside its span (the first: 2025-01-31): "
                                            "fill them in the flow (a calendar Left join and Fill nulls with 0 when an empty day "
                                            "means none), or choose missing = linear or model"}},
    {"id": "refuse_quantile_level", "model": "timestral", "data": "data/ff_daily_one.csv", "types": {**D, "ordini": "number"},
     "params": {"time": "giorno", "value": "ordini", "frequency": "day", "size": "5m", "quantiles": [0.1, 0.025]},
     "refuses": {"code": "quantile_level", "message": "Timestral forecasts the levels 0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, "
                                                      "0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, "
                                                      "0.99: 0.025 is not one"}},
    {"id": "refuse_known_future_gap", "model": "timestral", "data": "data/ff_edge_future_gap.csv",
     "types": {**D, "valore": "number", "festivo": "boolean"},
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "size": "5m", "known_covariates": ["festivo"]},
     "refuses": {"code": "known_future_gap", "message": "the future rows of the series skip 2025-02-12: the future must be "
                                                        "complete on the grid"}},
    {"id": "refuse_known_future_missing", "model": "timestral", "data": "data/ff_edge_future_missing.csv",
     "types": {**D, "valore": "number", "festivo": "boolean"},
     "params": {"time": "giorno", "value": "valore", "frequency": "day", "size": "5m", "known_covariates": ["festivo"]},
     "refuses": {"code": "known_future_missing", "message": "the series has no row after its last value: with known covariates "
                                                            "the future rows hold them"}},
    {"id": "refuse_empty_covariate", "model": "timestral", "data": "data/ff_covariates.csv",
     "types": {**D, "vendite": "number", "negozio": "text", "vuoto": "number"},
     "params": {"time": "giorno", "value": "vendite", "series": "negozio", "frequency": "day", "size": "5m",
                "past_covariates": ["vuoto"]},
     "refuses": {"code": "empty_covariate", "message": "the column «vuoto» has no value: drop it from the covariates"}},
]


def _r(x: float) -> float:
    """A quantile, compact: 13 significant digits (its tolerance is 1e-6 of the series' largest)."""
    return float(f"{x:.13g}")


def _f(x: float) -> float:
    """A number checked at 1e-12: every digit."""
    return float(x)


def run_case(fc, case: dict) -> dict:
    """The expected values of one case with one size: quantiles at a few steps and levels, the measures, the test."""
    from scipy.stats import wilcoxon

    p = {"missing": "refuse", "symmetric_averaging": True, "clip_nonnegative": True, **case["params"]}
    kind = "integer" if p["frequency"] == "index" else "date"
    m = SEASON[p["frequency"]]
    rows = read_csv(case["data"].removeprefix("data/"))
    types = case["types"]
    series = build_series(rows, p, kind)
    quantiles, expect = [], []
    diffs, measured = [], []
    for s in series:
        covs = covariate_rows(s, p, types, rows)
        h = len(s["future"]) if p.get("known_covariates") else p["horizon"]
        k = p.get("holdout", h)
        final = forecast_one(fc, s, p, types, covs, s["n"], h)
        top = float(np.abs(final).max())
        held = None
        if k and s["n"] >= k + m + 1:
            held = forecast_one(fc, s, p, types, covs, s["n"] - k, k)
            top = max(top, float(np.abs(held).max()))
        tol = TOL_QUANTILE * top
        drawn = len(quantiles) < 2 * CHECKED_SERIES
        for name, q, steps in (("forecast", final, h), ("holdout", held, k)):
            if q is None or not drawn:
                continue
            at = sorted({1, (steps + 1) // 2, steps})
            quantiles.append({"series": s["label"], "kind": name, "abs": _r(max(tol, 1e-12)), "steps": at,
                              "values": [[_r(q[step - 1, LEVELS.index(lv)]) for lv in CHECK_LEVELS] for step in at]})
        lab = s["label"]
        expect.append({"key": f"series|{lab}||n", "value": s["n"], "rel": TOL_DATA})
        if held is None:
            expect.append({"key": f"series|{lab}||not_measured", "text": "too_short" if k else "no_holdout"})
            continue
        cut = s["n"] - k
        base = naive(s["z"], cut, k, m)
        mm = measures(s["y"][:cut], s["y"][cut:], held, base, m)
        if mm["reason"]:
            expect.append({"key": f"series|{lab}||not_measured", "text": mm["reason"]})
            continue
        expect.append({"key": f"series|{lab}||scale", "value": _f(mm["scale"]), "rel": TOL_DATA})
        stats = SERIES_STATS if len(measured) < CHECKED_SERIES else ("mase", "baseline_mase")
        for stat in stats:
            expect.append({"key": f"series|{lab}||{stat}", "value": _r(mm[stat]), "rel": TOL_FORECAST, "abs": 1e-9})
        measured.append(mm)
        diffs.append(mm["mase"] - mm["baseline_mase"])
    n = len(measured)
    expect.append({"key": "model|||n_series", "value": len(series), "rel": TOL_DATA})
    expect.append({"key": "model|||n_points", "value": sum(s["n"] for s in series), "rel": TOL_DATA})
    expect.append({"key": "model|||n_measured", "value": n, "rel": TOL_DATA})
    if n:
        steps = sum(x["steps"] for x in measured)
        expect.append({"key": "model|||mase", "value": _r(sum(x["mase"] for x in measured) / n), "rel": TOL_FORECAST})
        expect.append({"key": "model|||coverage_80", "value": _r(sum(x["in80"] for x in measured) / steps), "rel": TOL_FORECAST,
                       "abs": 1e-9})
        expect.append({"key": "model|||wql", "value": _r(sum(x["pinball"] * x["steps"] for x in measured)
                                                        / sum(x["abs_sum"] for x in measured)), "rel": TOL_FORECAST})
        expect.append({"key": "model|||relative_mase_geomean",
                       "value": _r(math.exp(sum(math.log(x["relative_mase"]) for x in measured) / n)), "rel": TOL_FORECAST})
    if n >= 2:
        r = wilcoxon(diffs, zero_method="wilcox", correction=False, method="auto")
        expect.append({"key": "assumption|baseline_comparison||statistic", "value": _r(float(r.statistic)), "rel": TOL_FORECAST})
        expect.append({"key": "assumption|baseline_comparison||p_value", "value": _r(float(r.pvalue)), "rel": TOL_FORECAST})
        verdict = "failed" if r.pvalue < p.get("alpha", 0.05) else "ok"
        expect.append({"key": "assumption|baseline_comparison||verdict", "text": verdict})
        # the side of a rejection: the larger of the rank sums (scipy's one-sided statistic is W+), not the median's sign
        w_plus = float(wilcoxon(diffs, zero_method="wilcox", correction=False, method="auto", alternative="greater").statistic)
        nz = len([d for d in diffs if d != 0])
        head = ("comparison_not_supported" if verdict == "ok" else
                "comparison_supported" if w_plus < nz * (nz + 1) / 4 else "comparison_opposite")
        expect.append({"key": "reading|comparison||line_1", "id": head})
        expect.append({"key": "assumption|baseline_comparison||count", "value": len([d for d in diffs if d != 0]), "rel": TOL_DATA})
    else:
        expect.append({"key": "assumption|baseline_comparison||verdict", "text": "not_computed"})
    one = "" if p.get("series") else "_one"
    expect.append({"key": "reading|summary||line_1", "id": "summary" + one + ("_steps" if kind == "integer" else "")})
    return {"quantiles": quantiles, "expect": expect}


# ── the measures' and the test's own cases ──────────────────────────────────────
def measure_cases() -> list[dict]:
    """Fixed arrays (no model): the seasonal naïve, the scale and the measures written again, at 1e-12."""
    g = np.random.Generator(np.random.PCG64(7))
    out = []
    for m, missing in ((7, False), (1, False), (7, True), (12, False)):
        hist = 20 + 5 * np.sin(np.arange(60) * 2 * math.pi / max(m, 2)) + g.normal(0, 1, 60)
        if missing:
            hist[[10, 11, 53]] = np.nan
        actual = 20 + 5 * np.sin(np.arange(60, 74) * 2 * math.pi / max(m, 2)) + g.normal(0, 1, 14)
        actual[3] = 0.0
        med = actual + g.normal(0, 1, 14)
        med[3] = 0.0                                    # a step where the value and the forecast are both 0
        widths = np.array([lv - 0.5 for lv in LEVELS]) * 6
        q = np.round(med[:, None] + widths[None, :], 6)
        base = naive(hist, 60, 14, m)
        mm = measures(hist, actual, q, base, m)
        hist = np.round(hist, 6)
        actual = np.round(actual, 6)
        base = naive(hist, 60, 14, m)
        mm = measures(hist, actual, q, base, m)
        out.append({"season": m, "history": [None if math.isnan(x) else _f(x) for x in hist], "actual": [_f(x) for x in actual],
                    "quantiles": q.tolist(), "baseline": [None if math.isnan(x) else _f(x) for x in base],
                    "expect": {k: (_f(v) if isinstance(v, float) else v) for k, v in mm.items() if k != "abs_sum"}})
    return out


def wilcoxon_cases() -> list[dict]:
    """Differences for the paths of the test: exact, the sign patterns with ties or zeros, the normal approximation."""
    from scipy.stats import wilcoxon

    g = np.random.Generator(np.random.PCG64(11))
    cases = [g.normal(-0.3, 1, 5), g.normal(-0.5, 1, 12), np.round(g.normal(0.1, 1, 10), 1), np.r_[0.0, g.normal(0.4, 1, 11)],
             g.normal(-0.2, 1, 50), g.normal(-0.2, 1, 51), np.round(g.normal(0.2, 1, 30), 1), np.r_[0.0, 0.0, g.normal(0, 1, 40)],
             np.array([-0.1, -0.2, -0.3]), np.array([0.5, 0.5, -0.5, 0.25])]
    out = []
    for d in cases:
        r = wilcoxon(d, zero_method="wilcox", correction=False, method="auto")
        d = np.round(d, 9)
        r = wilcoxon(d, zero_method="wilcox", correction=False, method="auto")
        out.append({"d": [_f(x) for x in d], "statistic": float(r.statistic), "p_value": float(r.pvalue)})
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    weights = Path(argv[0])
    import torch
    from timestral import TimestralForecaster

    torch.set_num_threads(2)
    recipe = json.loads(RECIPE.read_text(encoding="utf-8"))
    write_tables()
    # the size cases' rows are read again from the tables just written (the CSV is the reference)
    by_size: dict[str, list[dict]] = {}
    for size in SIZE_ORDER:
        folder = weights / size
        want = recipe["weights"]["sizes"][size]["files"]["model.safetensors"]["sha256"]
        got = hashlib.sha256((folder / "model.safetensors").read_bytes()).hexdigest()
        if got != want:
            raise SystemExit(f"{size}: model.safetensors is not the recipe's ({got})")
        fc = TimestralForecaster.from_pretrained(str(folder), device="cpu")
        by_size[size] = []
        for case in SIZE_CASES:
            print(f"{size}: {case['id']}", flush=True)
            by_size[size].append({"id": case["id"], **run_case(fc, case)})
        del fc
    doc = {"format_version": 1,
           "generated": {"by": "make_reference.py", "python": sys.version.split()[0], "torch": torch.__version__,
                         "timestral": "1.0.0 (a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff, official)", "threads": 2},
           "check_levels": list(CHECK_LEVELS),
           "about": "cases: refusals (any size, run by the library's self-test); sizes: the cases of each size, run by the group's "
                    "own check with the runner (quantiles within `abs`, the measures within `rel`); measures and wilcoxon: the "
                    "measures and the test on fixed numbers, at 1e-12.",
           "cases": REFUSALS,
           "size_cases": [{k: v for k, v in c.items()} for c in SIZE_CASES],
           "sizes": by_size,
           "measures": measure_cases(),
           "wilcoxon": wilcoxon_cases()}
    (HERE / "expected.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"expected.json: {len(REFUSALS)} refusals, {len(SIZE_CASES)} cases × {len(by_size)} sizes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
