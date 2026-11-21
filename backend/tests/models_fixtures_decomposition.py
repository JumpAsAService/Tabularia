"""The results fixtures of the decomposition node (docs/specs/ts-decomposition.md, «The output», «Explaining the
method in the report»): one table per variant (stl, classical, mstl, emd), made from one small imaginary series,
so that the report lane (D4) can be built and `results.py` can be tried before the runner exists.

    cd backend && python -m tests.models_fixtures decomposition

The series is made here (a trend, one or two sine patterns and a deterministic wobble), its components are the
ones that made it, and the numbers of the checks are the formulas of the spec applied to them (Ljung–Box's Q with
a χ² tail, the strengths F_T and F_S, the rank tests' statistics as placeholders): consistent with each other, not
the runner's own (the real ones replace them, lots D2 and D3). Every verdict is a p-value against the node's alpha.
Every row the catalogue declares for the variant that a run with these parameters writes is here, and the `reading`
rows list the templates of «Explaining the method in the report» a run says, as `@id name=value` (the notes'
format: a space, a comma and a percent in a value are %20, %2C and %25).
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any
from urllib.parse import quote

EPOCH = date(1970, 1, 1)

# the parameters of each fixture: the variant they make is in the file's name
PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("decomposition", "stl"): {"time": "month", "value": "boxes", "frequency": "month", "method": "stl", "robust": True,
                               "transform": "log", "output": "components"},
    ("decomposition", "classical"): {"time": "month", "value": "boxes", "frequency": "month", "method": "classical",
                                     "type": "multiplicative", "output": "components"},
    ("decomposition", "mstl"): {"time": "day", "value": "orders", "frequency": "day", "method": "mstl", "periods": [7, 30],
                                "robust": True, "missing": "linear", "output": "components"},
    ("decomposition", "emd"): {"time": "t", "value": "signal", "frequency": "index", "method": "emd", "period": 8,
                               "noise_imfs": 1, "output": "components"},
}
ALPHA = 0.05


def _enc(v: Any) -> str:
    """A value of a note: a number keeps 17 digits, a text its characters with %, space and comma encoded."""
    if isinstance(v, float):
        return repr(v)
    return quote(str(v), safe="").replace("%2F", "/")


def note(id_: str, **values: Any) -> str:
    return "@" + id_ + "".join(f" {k}={_enc(v)}" for k, v in values.items())


class Table:
    def __init__(self) -> None:
        self.rows: list[list[Any]] = []

    def add(self, section: str, stat: str, value: Any = None, text: Any = None, level: str | None = None,
            level_2: str | None = None, x0: Any = None, x1: Any = None, index: int | None = None) -> None:
        key = "|".join((section, level or "", level_2 or "", stat)) + ("" if index is None else f"|{index}")
        v = None if value is None else round(float(value), 12) + 0.0
        self.rows.append([section, level, level_2, stat, v, text, x0, x1, len(self.rows), key])


def _stars(p: float) -> str:
    return next((c for t, c in ((0.001, "***"), (0.01, "**"), (0.05, "*"), (0.1, ".")) if p < t), "")


def _chi2_sf(x: float, df: int) -> float:
    """The upper tail of a χ² with `df` degrees of freedom: the regularised upper incomplete gamma Q(df/2, x/2)
    (a series for x below df/2 + 1, else Lentz's continued fraction)."""
    a, z = df / 2.0, x / 2.0
    if z <= 0:
        return 1.0
    if z < a + 1:
        term = total = 1.0 / a
        n = a
        for _ in range(500):
            n += 1
            term *= z / n
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        return max(0.0, 1.0 - total * math.exp(-z + a * math.log(z) - math.lgamma(a)))
    tiny = 1e-300
    b = z + 1 - a
    c, d = 1 / tiny, 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return min(1.0, math.exp(-z + a * math.log(z) - math.lgamma(a)) * h)


def _var(v: list[float]) -> float:
    m = sum(v) / len(v)
    return sum((x - m) ** 2 for x in v) / (len(v) - 1)


def _acf(v: list[float], k: int) -> float:
    m = sum(v) / len(v)
    den = sum((x - m) ** 2 for x in v)
    return sum((v[i] - m) * (v[i + k] - m) for i in range(len(v) - k)) / den


def _wobble(i: int, scale: float) -> float:
    """A deterministic, small, irregular number: the remainder of the imaginary series."""
    return scale * (math.sin(i * 12.9898 + 4.1414) * 43758.5453 % 1.0 - 0.5)


def _series(variant: str) -> dict[str, Any]:
    """The imaginary series of a variant and the components it was made of."""
    if variant == "emd":
        n = 64
        modes = [[0.8 * math.sin(2 * math.pi * i / 3.1 + 0.4) for i in range(n)],
                 [2.0 * math.sin(2 * math.pi * i / 8.0) for i in range(n)],
                 [3.0 * math.sin(2 * math.pi * i / 21.0 + 1.0) for i in range(n)]]
        residue = [10 + 0.05 * i for i in range(n)]
        x = [sum(m[i] for m in modes) + residue[i] + _wobble(i, 0.05) for i in range(n)]
        modes[0] = [modes[0][i] + _wobble(i, 0.05) for i in range(n)]
        return {"n": n, "x": x, "modes": modes, "residue": residue, "times": list(range(1, n + 1)), "kind": "integer"}
    if variant == "mstl":
        n, periods = 90, [7, 30]
        start = (date(2026, 1, 1) - EPOCH).days
        trend = [50 + 0.2 * i for i in range(n)]
        seasonal = [[6 * math.sin(2 * math.pi * i / 7) for i in range(n)], [3 * math.sin(2 * math.pi * i / 30 + 0.5) for i in range(n)]]
        remainder = [_wobble(i, 2.0) for i in range(n)]
        x = [trend[i] + seasonal[0][i] + seasonal[1][i] + remainder[i] for i in range(n)]
        return {"n": n, "x": x, "trend": trend, "seasonal": seasonal, "remainder": remainder, "periods": periods,
                "times": [start + i for i in range(n)], "kind": "date"}
    n, periods = 60, [12]
    y, m0 = 2022, 1
    times = [(date(y + (m0 - 1 + i) // 12, (m0 - 1 + i) % 12 + 1, 1) - EPOCH).days for i in range(n)]
    trend = [100 + 1.5 * i for i in range(n)]
    pattern = [math.sin(2 * math.pi * i / 12) for i in range(12)]
    if variant == "classical":  # multiplicative: ratios around 1
        seasonal = [[1 + 0.25 * pattern[i % 12] for i in range(n)]]
        remainder = [1 + _wobble(i, 0.04) for i in range(n)]
        x = [trend[i] * seasonal[0][i] * remainder[i] for i in range(n)]
    else:  # stl on the logarithm: the components are logarithms
        trend = [math.log(t) for t in trend]
        seasonal = [[0.2 * pattern[i % 12] for i in range(n)]]
        remainder = [_wobble(i, 0.04) for i in range(n)]
        x = [trend[i] + seasonal[0][i] + remainder[i] for i in range(n)]
    return {"n": n, "x": x, "trend": trend, "seasonal": seasonal, "remainder": remainder, "periods": periods, "times": times,
            "kind": "date"}


def _positions(period: int, frequency: str) -> tuple[str, list[str]]:
    """What the calendar names a position of a cycle: (the kind, the codes of the positions 1…p)."""
    if frequency == "day" and period == 7:
        return "weekday", [f"weekday_{i}" for i in range(1, 8)]
    if frequency == "month" and period == 12:
        return "month", [f"month_{i}" for i in range(1, 13)]
    return "step", [f"step_{i}" for i in range(1, period + 1)]


def build(model: str, variant: str, params: dict[str, Any], spec: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
    s = _series(variant)
    t, n, alpha = Table(), s["n"], checked["alpha"]
    emd = variant == "emd"
    sec = spec["sections"]
    transformed = checked["transform"] != "none"
    frequency = checked["frequency"]
    periods: list[int] = s.get("periods") or ([int(checked["period"])] if checked.get("period") else [])
    # ── the components and the remainder the checks read ─────────────────────────────────────────────────────────
    if emd:
        k_noise = checked["noise_imfs"]
        remainder = [sum(s["modes"][j][i] for j in range(k_noise)) for i in range(n)]
        trend, seasonal = s["residue"], []
    else:
        remainder, trend, seasonal = s["remainder"], s["trend"], s["seasonal"]
    x0s = s["times"]
    first, last = x0s[0], x0s[-1]
    wobble = [remainder[i] for i in range(n)]
    notes: list[tuple[str, str | None, str]] = []  # (block, level_2, note)

    def say(block: str, level_2: str | None, id_: str, **values: Any) -> None:
        notes.append((block, level_2, note(id_, **values)))

    # ── model ───────────────────────────────────────────────────────────────────────────────────────────────────
    label = {"stl": "STL", "classical": "classical", "mstl": "MSTL", "emd": "EMD"}[variant]
    texts = {"model": model, "variant": variant,
             "formula": (f"{checked['value']} = " + (" + ".join(["imf" + str(j + 1) for j in range(3)]) + " + residue" if emd else
                         "trend · seasonal[%s] · remainder" % ",".join(map(str, periods)) if variant == "classical" else
                         "trend + " + " + ".join(f"seasonal[{p}]" for p in periods) + " + remainder") + f" · {label}"),
             "software": "R 4.6.1; stats 4.6.1; forecast 9.0.2; Rlibeemd 1.4.4; urca 1.3-4; tseries 0.10-63",
             "group_version": "1", "rows_from": "trial", "note_1": "A placeholder note: this table is a fixture.",
             "method": variant, "frequency": frequency, "transform": checked["transform"], "time_kind": s["kind"],
             "missing": checked["missing"], "seasonality_test": checked["seasonality_test"],
             "periods": ",".join(map(str, periods))}
    if variant == "classical":
        texts["type"] = checked["type"]
    if emd:
        texts["boundary"] = "wu_huang_2009"
    numbers = {"n": n, "n_missing": 0, "n_filled": 2 if variant == "mstl" else 0, "first": first, "last": last, "alpha": alpha,
               "lb_lag_used": 10 if not periods else min(2 * max(periods), n // 5)}
    if transformed:
        numbers["box_cox_lambda"] = 0.0
    if variant in ("stl", "mstl"):
        numbers["t_window_used"] = 13.0 if variant == "stl" else 19.0
    if variant == "mstl":
        numbers["iterate"] = 2
    if emd:
        numbers.update(max_imf=checked["max_imf"], n_imfs=len(s["modes"]), s_number=checked["s_number"],
                       max_siftings=checked["max_siftings"], noise_imfs=checked["noise_imfs"])
    for stat in sec["model"]["texts"]:
        if stat == "note_*":
            t.add("model", "note_1", text=texts["note_1"])
        elif stat in texts:
            t.add("model", stat, text=texts[stat])
    for stat in sec["model"]["numbers"]:
        if stat in numbers:
            t.add("model", stat, numbers[stat])

    # ── the seasons ─────────────────────────────────────────────────────────────────────────────────────────────
    additive = not (variant == "classical" and checked["type"] == "multiplicative")
    base = 0.0 if additive else 1.0
    shape: dict[int, list[float]] = {}
    for j, p in enumerate(periods):
        comp = seasonal[j] if not emd else [s["x"][i] - trend[i] - remainder[i] for i in range(n)]
        by_pos = [[comp[i] for i in range(n) if i % p == k] for k in range(p)]
        shape[p] = [sum(v) / len(v) for v in by_pos]
        lo, hi = min(range(p), key=shape[p].__getitem__), max(range(p), key=shape[p].__getitem__)
        kind, codes = _positions(p, frequency)
        if variant in ("stl", "mstl"):
            t.add("season", "s_window_used", 11.0 if variant == "stl" else 15.0 + 4 * j, level=str(p))
        t.add("season", "cycles", n // p, level=str(p))
        t.add("season", "amplitude", shape[p][hi] / shape[p][lo] if not additive else shape[p][hi] - shape[p][lo], level=str(p))
        t.add("season", "peak_position", hi + 1, level=str(p))
        t.add("season", "peak_value", shape[p][hi], level=str(p))
        t.add("season", "trough_position", lo + 1, level=str(p))
        t.add("season", "trough_value", shape[p][lo], level=str(p))
        t.add("season", "position_kind", text=kind, level=str(p))
        t.add("season", "peak_label", text=codes[hi], level=str(p))
        t.add("season", "trough_label", text=codes[lo], level=str(p))
        say("season", str(p), "season_shape_ratio" if not additive else "season_shape", period=p, peak=codes[hi], trough=codes[lo],
            peak_value=shape[p][hi], trough_value=shape[p][lo], position=kind)

    # ── the IMFs ────────────────────────────────────────────────────────────────────────────────────────────────
    if emd:
        total = sum(v * v for v in s["x"])
        for j, mode in enumerate(s["modes"] + [s["residue"]]):
            name = f"imf_{j + 1}" if j < len(s["modes"]) else "residue"
            peaks = sum(1 for i in range(1, n - 1) if mode[i] > mode[i - 1] and mode[i] > mode[i + 1])
            crossings = sum(1 for i in range(1, n) if (mode[i] - 0) * (mode[i - 1] - 0) < 0) if name != "residue" else 0
            mean_period = n / peaks if peaks else None
            t.add("imf", "mean_period", mean_period, level=name)
            t.add("imf", "mean_period_time", mean_period, level=name)
            t.add("imf", "variance_share", _var(mode) / _var(s["x"]), level=name)
            t.add("imf", "energy_share", sum(v * v for v in mode) / total, level=name)
            t.add("imf", "extrema", 2 * peaks, level=name)
            t.add("imf", "zero_crossings", crossings, level=name)
        say("imfs", None, "imfs_found", n_imfs=len(s["modes"]), max_imf=checked["max_imf"])
        say("imfs", None, "imfs_empty", first=len(s["modes"]) + 1, last=checked["max_imf"])
        for j in range(len(s["modes"])):
            say("imfs", None, "imf_mode", imf=j + 1, mean_period=n / max(1, sum(
                1 for i in range(1, n - 1) if s["modes"][j][i] > s["modes"][j][i - 1] and s["modes"][j][i] > s["modes"][j][i + 1])))

    # ── the checks: tests against the node's alpha; measures with no verdict; facts ─────────────────────────────
    def test(level: str, level_2: str | None, h0: str, statistic: float, df: int | None, p: float, verdict_when_small: str,
             advice: str) -> None:
        small = p < alpha
        verdict = verdict_when_small if small else ("ok" if verdict_when_small == "failed" else "failed")
        t.add("assumption", "h0", text=h0, level=level, level_2=level_2)
        t.add("assumption", "statistic", statistic, level=level, level_2=level_2)
        if df is not None:
            t.add("assumption", "df", df, level=level, level_2=level_2)
        t.add("assumption", "p_value", p, level=level, level_2=level_2)
        t.add("assumption", "significance", text=_stars(p), level=level, level_2=level_2)
        t.add("assumption", "verdict", text=verdict, level=level, level_2=level_2)
        if verdict == "failed" and advice:
            t.add("assumption", "advice", text=advice, level=level, level_2=level_2)
            t.add("assumption", "advice_id", text=level + "_failed", level=level, level_2=level_2)

    def measure(level: str, level_2: str | None, value: float | None, **extra: float) -> None:
        t.add("assumption", "value", value, level=level, level_2=level_2)
        for stat, v in extra.items():
            t.add("assumption", stat, v, level=level, level_2=level_2)

    for p in periods:
        blocks = n // p
        q = 12.0 * blocks / (p * (p + 1)) * sum((b - blocks * (p + 1) / 2) ** 2 for b in [blocks * (p + 1) / 2 + 3 * (1 if i % 2 else -1) for i in range(p)])
        p_season = _chi2_sf(q, p - 1)
        test("seasonality", str(p), f"the series has no seasonal pattern of period {p}", q, p - 1, p_season, "ok",
             "the seasonal component of period %d may be noise: read its strength and its chart; try without this period" % p)
        t.add("assumption", "count", blocks, level="seasonality", level_2=str(p))
        say("seasonality", str(p), "seasonality_supported" if p_season < alpha else "seasonality_not_supported", period=p,
            p_value=p_season, alpha=alpha, cycles=blocks, test=checked["seasonality_test"])
    h = numbers["lb_lag_used"]
    r_acf = [_acf(wobble, k) for k in range(1, h + 1)]
    q_lb = n * (n + 2) * sum(r ** 2 / (n - k) for k, r in enumerate(r_acf, start=1))
    p_lb = _chi2_sf(q_lb, h)
    test("ljung_box", None, f"the remainder is not autocorrelated up to lag {h}", q_lb, h, p_lb, "failed",
         "structure left in the remainder: a season missing (another period), a wider trend window, or a model of the remainder")
    big = max(range(h), key=lambda k: abs(r_acf[k]))
    measure("largest_acf", None, abs(r_acf[big]), lag=big + 1)
    say("remainder", None, "remainder_white" if p_lb >= alpha else "remainder_structure", lag=h, p_value=p_lb, alpha=alpha,
        largest_acf=abs(r_acf[big]), at_lag=big + 1)
    adf_stat, kpss_stat = -4.1, 0.21
    p_adf, p_kpss = 0.004, 0.10
    test("adf", None, "the remainder has a unit root", adf_stat, None, p_adf, "ok", "")
    test("kpss", None, "the remainder is stationary around a constant", kpss_stat, None, p_kpss, "failed",
         "the remainder is not stationary: a trend window too wide, or a trend left in it")
    say("stationarity", None, "stationarity_both_ok" if p_adf < alpha and p_kpss >= alpha else "stationarity_neither",
        p_adf=p_adf, p_kpss=p_kpss, alpha=alpha)
    if "trend_strength" in sec["assumption"]["levels"]:
        if emd and checked["noise_imfs"] == 0:
            for check in ("trend_strength", "seasonal_strength"):
                t.add("assumption", "verdict", text="not_computed", level=check, level_2=None)
                t.add("assumption", "advice", text="EMD does not separate a noise: choose how many of the fastest IMFs are noise",
                      level=check, level_2=None)
            say("strength", None, "strength_not_computed")
        else:
            resid_var = _var(remainder)
            f_t = max(0.0, 1 - resid_var / _var([trend[i] + remainder[i] for i in range(n)]))
            measure("trend_strength", None, f_t)
            fs = []
            for j, p in enumerate(periods):
                comp = seasonal[j] if not emd else [s["x"][i] - trend[i] - remainder[i] for i in range(n)]
                f_s = max(0.0, 1 - resid_var / _var([comp[i] + remainder[i] for i in range(n)]))
                fs.append(f_s)
                measure("seasonal_strength", str(p), f_s)
            say("strength", None, "strength", trend=f_t, seasonal=fs[0] if fs else 0.0, period=periods[0] if periods else 0)
    if "robust_weights" in sec["assumption"]["levels"]:
        measure("robust_weights", None, 2.0, count=2)
    if emd:
        for j in range(len(s["modes"])):
            t.add("assumption", "h0", text="the mode is an intrinsic mode function", level="imf_condition", level_2=f"imf_{j + 1}")
            t.add("assumption", "verdict", text="ok", level="imf_condition", level_2=f"imf_{j + 1}")
        t.add("assumption", "h0", text="the residue has at most one extremum", level="residue_monotonic")
        t.add("assumption", "verdict", text="ok", level="residue_monotonic")
        measure("orthogonality", None, 0.012)
    # ── the summary and the method ──────────────────────────────────────────────────────────────────────────────
    say("summary", None, f"summary_{variant}", value=checked["value"], n=n, first=float(first), last=float(last),
        periods=",".join(map(str, periods)))
    if transformed:
        say("summary", None, "transform_" + ("log" if checked["transform"] == "log" else "box_cox"))
    if numbers["n_filled"]:
        say("summary", None, "filled_" + checked["missing"], count=numbers["n_filled"])
    say("method", None, f"method_{variant}", period=periods[0] if periods else 0)
    say("formulas", None, f"formula_{variant}")
    counters: dict[tuple[str, str | None], int] = {}
    for block, level_2, text in notes:
        counters[(block, level_2)] = counters.get((block, level_2), 0) + 1
        t.add("reading", f"line_{counters[(block, level_2)]}", text=text, level=block, level_2=level_2)

    # ── the curves, the remainder's charts ──────────────────────────────────────────────────────────────────────
    deseasonalized = [s["x"][i] - sum(seasonal[j][i] for j in range(len(seasonal))) if additive else s["x"][i] / seasonal[0][i]
                      for i in range(n)]
    if emd:
        series = {"observed": s["x"], **{f"imf_{j + 1}": s["modes"][j] for j in range(len(s["modes"]))}, "residue": s["residue"]}
        for name, values in series.items():
            for i in range(n):
                t.add("curve", "y", values[i], level="imfs", level_2=name, x0=x0s[i], index=i)
    else:
        series = {"observed": s["x"], "trend": trend, **{f"seasonal_{p}": seasonal[j] for j, p in enumerate(periods)},
                  "remainder": remainder}
        for name, values in series.items():
            for i in range(n):
                t.add("curve", "y", values[i], level="components", level_2=name, x0=x0s[i], index=i)
        for i in range(n):
            t.add("curve", "y", deseasonalized[i], level="adjusted", level_2="deseasonalized", x0=x0s[i], index=i)
    for p in periods:
        comp = seasonal[periods.index(p)] if not emd else [s["x"][i] - trend[i] - remainder[i] for i in range(n)]
        for k in range(p):
            at = [comp[i] for i in range(n) if i % p == k]
            for stat, v in (("mean", sum(at) / len(at)), ("low", min(at)), ("high", max(at))):
                t.add("curve", stat, v, level="subseries", level_2=f"subseries_{p}", x0=k + 1, index=k)
    for i in range(n):
        t.add("point", "y", remainder[i], text=None, level="remainder", x0=x0s[i], index=i)
    for k, r in enumerate(r_acf[: max(h, 20)], start=1):
        t.add("lag", "acf", r, level="resid_acf", x0=k, index=k)
    t.add("lag", "bound", 1.959963984540054 / math.sqrt(n), level="resid_acf")
    lo, hi = min(remainder), max(remainder)
    width = (hi - lo) / 8 or 1.0
    counts = [0] * 8
    for v in remainder:
        counts[min(7, int((v - lo) / width))] += 1
    for i, c in enumerate(counts):
        t.add("hist", "count", c, level="remainder", x0=lo + i * width, x1=lo + (i + 1) * width, index=i)
    return {"model": model, "variant": variant, "params": params, "rows": t.rows}
