"""The second witness of the group «survival» (docs/specs/statistical-models.md, «Reference
values»): lifelines, a second implementation, computes what it can of expected.json from the
same tables, OUTSIDE Tabularia, and says where the two differ by more than 1e-6 (relative).
Run by hand, with the versions of witness.json, in a scratch environment:

    uv venv /tmp/witness && uv pip install --python /tmp/witness/bin/python lifelines==0.30.3
    /tmp/witness/bin/python -I gateway-go/internal/modelgroups/groups/survival/reference/make_witness.py

Kaplan–Meier (lane M4), per case of expected.json: per stratum the survival at the times
asked, the quartiles and the median, the restricted mean, the curve and the Nelson–Aalen
cumulative hazard at the steps expected.json names; the log-rank test (Peto's: lifelines'
Fleming–Harrington with p = 1, q = 0, which is survdiff's ρ = 1) and its pairs. The
intervals only where both use the same transformation: lifelines' is log(−log) (R's
conf_type = "log-log"). Where a curve is exactly at a quartile over a step, R takes the
middle of the step and lifelines its start: the witness applies R's rule and prints both.
Exit 1 when a value differs.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from lifelines import KaplanMeierFitter, NelsonAalenFitter
from lifelines.statistics import multivariate_logrank_test, pairwise_logrank_test
from lifelines.utils import restricted_mean_survival_time

HERE = Path(__file__).resolve().parent
TOL = 1e-6


def label(t: float) -> str:
    return str(int(t)) if float(t).is_integer() else repr(float(t))


def is_event(column: pd.Series, value, kind: str) -> np.ndarray:
    if kind == "boolean":
        return (column.astype(str).str.lower() == str(value).lower()).to_numpy()
    if kind in ("number", "integer"):
        return (pd.to_numeric(column) == float(value)).to_numpy()
    return (column.astype(str) == str(value)).to_numpy()


def km_case(case: dict) -> dict[str, float]:
    """What lifelines says of the keys of a Kaplan–Meier case."""
    p, types = case["params"], case["types"]
    df = pd.read_csv(HERE / case["data"], keep_default_na=False, na_values=[""], dtype=str)
    used = [p["time"], p["event"], *([p["strata"]] if p.get("strata") else [])]
    df = df.dropna(subset=used)
    time = pd.to_numeric(df[p["time"]]).to_numpy()
    event = is_event(df[p["event"]], p["event_value"], types[p["event"]]).astype(int)
    alpha = p.get("alpha", 0.05)
    loglog = p.get("conf_type", "log") == "log-log"
    strata = sorted(df[p["strata"]].unique()) if p.get("strata") else ["all"]
    groups = df[p["strata"]].to_numpy() if p.get("strata") else np.array(["all"] * len(df))
    tau = p.get("rmst_tau", float(time.max()))
    out: dict[str, float] = {}
    for s in strata:
        name = f"{p['strata']}={s}" if p.get("strata") else "all"
        m = groups == s
        kmf = KaplanMeierFitter(alpha=alpha).fit(time[m], event[m])
        key = f"stratum|{name}||"
        sf = kmf.survival_function_.iloc[:, 0]
        ci = kmf.confidence_interval_survival_function_
        for t in p.get("times", []):
            at = kmf.survival_function_at_times(t).iloc[0]
            past = t > time[m].max() and at > 0
            out[key + f"surv_at_{label(t)}"] = math.nan if past else float(at)
            if loglog and not past:
                row = ci[ci.index <= t].iloc[-1]
                out[key + f"surv_at_{label(t)}_low"] = float(row.iloc[0])
                out[key + f"surv_at_{label(t)}_high"] = float(row.iloc[1])
        for stat, q in (("q25", 0.75), ("median", 0.5), ("q75", 0.25)):
            # lifelines: the first time the curve is at or below q. Where the curve is exactly q,
            # R takes the middle between that time and the next step down (quantile.survfit).
            below = sf[sf <= q + 1e-12]
            first = float(below.index[0]) if len(below) else math.nan
            if len(below) and abs(below.iloc[0] - q) < 1e-12:
                later = sf.index[sf < q - 1e-12]  # where the curve next steps down
                out[f"{key}{stat} (as lifelines)"] = first
                first = (first + float(later[0])) / 2 if len(later) else first
            out[key + stat] = first
        out[key + "rmst"] = float(restricted_mean_survival_time(kmf, t=tau))
        # the curve's and the cumulative hazard's steps: every distinct time, from 0
        steps = sf.index.to_numpy()
        naf = NelsonAalenFitter(nelson_aalen_smoothing=False).fit(time[m], event[m])
        ch = naf.cumulative_hazard_.iloc[:, 0]
        for i, t in enumerate(steps):
            out[f"curve|km|{name}|surv|{i}"] = float(sf.loc[t])
            out[f"curve|cumhaz|{name}|cumhaz|{i}"] = float(ch.loc[t])
    if len(strata) >= 2:
        peto = p.get("test") == "peto"
        kw = {"weightings": "fleming-harrington", "p": 1, "q": 0} if peto else {}
        test = multivariate_logrank_test(time, groups, event, **kw)
        out["model|||test_statistic"] = float(test.test_statistic)
        out["model|||test_p_value"] = float(test.p_value)
        if p.get("pairwise"):
            pairs = pairwise_logrank_test(time, groups, event, **kw).summary
            for (a, b), row in pairs.iterrows():
                key = f"pair|{p['strata']}={a}|{p['strata']}={b}|"
                out[key + "statistic"] = float(row["test_statistic"])
                out[key + "p_value"] = float(row["p"])
    return out


def main() -> int:
    expected = json.loads((HERE / "expected.json").read_text(encoding="utf-8"))
    bad = compared = 0
    worst = 0.0
    for case in expected["cases"]:
        if case["model"] != "kaplan_meier" or case.get("refuses"):
            continue
        said = km_case(case)
        for e in case["expect"]:
            if "text" in e or e["key"] not in said:
                continue
            own = said.get(e["key"] + " (as lifelines)")
            if own is not None:
                print(f"MIDDLE OF A STEP {case['id']} {e['key']}: R {e['value']!r}, lifelines' first time {own!r}, "
                      f"the step's middle {said[e['key']]!r}")
            want, got = e["value"], said[e["key"]]
            compared += 1
            if want is None or math.isnan(got):
                ok = want is None and math.isnan(got)
            else:
                ok = abs(got - want) <= max(1e-12, TOL * abs(want))
                worst = max(worst, abs(got - want) / max(abs(want), 1e-12))
            if not ok:
                bad += 1
                print(f"DIFFERS {case['id']} {e['key']}: R {want!r}, lifelines {got!r}")
    print(f"{compared} values compared with lifelines, {bad} differ by more than {TOL:g}; the largest relative difference "
          f"{worst:.3g}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
