"""Writes backend/tests/fixtures/models/<model>-<variant>.json: one table per model and
variant, made from the catalogue's declaration with placeholder numbers, so that the report
can be built before the runners exist (docs/specs/statistical-models.md, piece 0).

    cd backend && python -m tests.models_fixtures

The numbers are placeholders: plausible (a p-value between 0 and 1, counts whole, curves
that go down), never computed. A runner's lane replaces its fixture with the runner's real
output on the reference table; `test_models_results.py` checks every fixture there is,
whoever wrote it. Run this again only for a fixture whose model has no runner yet.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.engine.models import catalog
from app.engine.stats.output import OUTPUT_NAMES

HERE = Path(__file__).resolve().parent / "fixtures" / "models"

# The parameters of each fixture: the variant they make is in the file's name.
PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("linear", "unpenalized"): {"outcome": "quantity", "predictors": ["price", "channel"],
                                "interactions": [["price", "channel"]], "se": "hc3", "order_by": "day"},
    ("linear", "penalized"): {"outcome": "quantity", "predictors": ["price", "channel", "month"], "penalty": "lasso"},
    ("logistic", "unpenalized"): {"outcome": "churned", "event_value": "yes", "predictors": ["tenure", "plan"]},
    ("logistic", "penalized"): {"outcome": "churned", "event_value": "yes", "predictors": ["tenure", "plan"],
                                "penalty": "elastic_net", "mixing": 0.5},
    ("kaplan_meier", "default"): {"time": "days", "event": "status", "event_value": "failed", "strata": "line",
                                  "pairwise": True, "times": [30, 90], "rmst_tau": 180},
    ("cox", "default"): {"time": "days", "event": "status", "event_value": "failed", "predictors": ["load", "line"]},
    ("arima", "manual"): {"time": "month", "value": "boxes", "frequency": "month", "p": 1, "d": 1, "q": 1,
                          "P": 0, "D": 1, "Q": 1, "holdout": 12},
    ("arima", "auto"): {"time": "month", "value": "boxes", "frequency": "month", "orders": "auto"},
}

# The example a declared stat ending in «*» is written as.
WILDCARDS = {"note_*": ["note_1"], "surv_at_*": ["surv_at_30", "surv_at_30_low", "surv_at_30_high"],
             "ar_root_modulus_*": ["ar_root_modulus_1"], "ma_root_modulus_*": ["ma_root_modulus_1"],
             "orders_reason_*": ["orders_reason_1", "orders_reason_2", "orders_reason_3"],
             "order_test_*": ["order_test_seasonal_strength", "order_test_kpss_d0_p_value"]}

TEXTS = {"formula": "quantity ~ price + channel + price × channel", "software": "R 4.6.1; stats 4.6.1",
         "group_version": "1", "rows_from": "trial", "note_1": "A placeholder note: this table is a fixture.",
         "family": "gaussian", "link": "identity", "se_type": "hc3", "penalty": "lasso", "cv_measure": "mse",
         "estimation": "ml", "event_value": "yes", "conf_type": "log", "test": "logrank", "ties": "efron",
         "model_name": "SARIMA(1,1,1)(0,1,1)[12]", "frequency": "month", "transform": "none", "criterion": "aicc",
         "orders_reason_1": "D = 1: the seasonal strength 0.82 is above 0.64",
         "orders_reason_2": "d = 1: KPSS rejects stationarity of the series (p 0.01) and not of its first difference (p 0.10)",
         "orders_reason_3": "ARIMA(1,1,1)(0,1,1)[12] has the lowest AICc of 3 candidates"}

TERMS = {
    "linear": [("(Intercept)", None), ("price", None), ("channel", "web"), ("price × channel", "web"), ("month", None)],
    "logistic": [("(Intercept)", None), ("tenure", None), ("plan", "premium")],
    "cox": [("load", None), ("line", "B")],
    "arima": [("ar1", None), ("ma1", None), ("sma1", None)],
}
STRATA = ["line=A", "line=B"]


class Table:
    def __init__(self) -> None:
        self.rows: list[list[Any]] = []

    def add(self, section: str, stat: str, value: Any = None, text: Any = None, level: str | None = None,
            level_2: str | None = None, x0: Any = None, x1: Any = None, index: int | None = None) -> None:
        key = "|".join((section, level or "", level_2 or "", stat)) + ("" if index is None else f"|{index}")
        v = None if value is None else round(float(value), 10) + 0.0
        self.rows.append([section, level, level_2, stat, v, text, x0, x1, len(self.rows), key])


def _number(stat: str, i: int) -> float:
    """A placeholder for a number: a probability where the name says one, a count where it
    says one, something positive otherwise."""
    if "p_value" in stat or stat.startswith("p_") or stat in ("p_adjusted",):
        return [0.0004, 0.03, 0.21, 0.67, 0.008][i % 5]
    if stat in ("n", "events", "censored", "count", "observed", "n_selected", "candidates", "n_strata", "horizon",
                "holdout") or stat.startswith(("df", "f_df", "lr_df", "wald_df", "score_df", "test_df")):
        return float([120, 48, 3, 12, 7][i % 5])
    if stat in ("p", "d", "q", "P", "D", "Q"):
        return float([1, 1, 1, 0, 1, 1][("p", "d", "q", "P", "D", "Q").index(stat)])
    if stat == "s":
        return 12.0
    if stat in ("selected", "converged", "chosen"):
        return float(i % 2)
    return round(0.5 + 0.37 * math.sin(i + len(stat)), 6)


def _expand(names: list[str]) -> list[str]:
    out = []
    for n in names:
        out += WILDCARDS.get(n, [n]) if n.endswith("*") else [n]
    return out


def _significance(p: float) -> str:
    return next((code for threshold, code in ((0.001, "***"), (0.01, "**"), (0.05, "*"), (0.1, "."))
                 if p < threshold), "")


def build(model: str, variant: str) -> dict[str, Any]:
    params = PARAMS[(model, variant)]
    checked = catalog.check_params(model, params)
    spec = catalog.variant_spec(model, checked)
    assert spec["id"] == variant, (model, variant, spec["id"])
    t = Table()
    sections = spec["sections"]
    texts = {**TEXTS, "model": model, "variant": variant}
    if model == "logistic":
        texts.update(family="binomial", link="logit", formula="churned ~ tenure + plan")
    decl = sections["model"]
    for stat in _expand(decl["texts"]):
        if stat.endswith("_significance"):
            continue
        t.add("model", stat, text=texts.get(stat, stat))
    for i, stat in enumerate(_expand(decl["numbers"])):
        value = _number(stat, i)
        t.add("model", stat, value)
        sig = stat[: -len("p_value")] + "significance"
        if stat.endswith("p_value") and sig in decl["texts"]:
            t.add("model", sig, text=_significance(value))
    if "coef" in sections:
        d = sections["coef"]
        for j, (term, level_2) in enumerate(TERMS.get(model, [])):
            for i, stat in enumerate(d["numbers"]):
                value = _number(stat, i + j)
                t.add("coef", stat, value, level=term, level_2=level_2)
                if stat == "p_value" and "significance" in d["texts"]:
                    t.add("coef", "significance", text=_significance(value), level=term, level_2=level_2)
    if "cv" in sections:
        for i in range(8):
            log_lambda = -6 + i * 0.75
            mean = 1.0 + 0.05 * (log_lambda + 3) ** 2
            for stat, value in (("cv_mean", mean), ("cv_low", mean - 0.1), ("cv_high", mean + 0.1),
                                ("n_selected", max(0, 5 - i // 2))):
                t.add("cv", stat, value, x0=log_lambda, index=i)
    if "candidate" in sections:
        for j, name in enumerate(["ARIMA(1,1,1)(0,1,1)[12]", "ARIMA(2,1,1)(0,1,1)[12]", "ARIMA(0,1,1)(0,1,1)[12]"]):
            for stat, value in (("aicc", 812.4 + 3 * j), ("aic", 811.9 + 3 * j), ("bic", 823.0 + 3 * j),
                                ("converged", 1), ("chosen", 1 if j == 0 else 0)):
                t.add("candidate", stat, value, level=name)
    if "assumption" in sections:
        d = sections["assumption"]
        for j, check in enumerate(d["levels"]):
            per_term = check in ("vif", "box_tidwell", "schoenfeld", "martingale_form", "dfbeta")
            terms = [term for term, _ in TERMS.get(model, []) if term != "(Intercept)"][:2] if per_term else [None]
            for k, term in enumerate(terms):
                p = _number("p_value", j + k)
                verdict = "not_computed" if check == "censoring_informative" else ("failed" if p < 0.05 else "ok")
                t.add("assumption", "h0", text=f"placeholder H0 of {check}", level=check, level_2=term)
                if verdict != "not_computed":
                    t.add("assumption", "statistic", 1.5 + j, level=check, level_2=term)
                    t.add("assumption", "df", 1, level=check, level_2=term)
                    t.add("assumption", "p_value", p, level=check, level_2=term)
                    t.add("assumption", "significance", text=_significance(p), level=check, level_2=term)
                t.add("assumption", "verdict", text=verdict, level=check, level_2=term)
                t.add("assumption", "advice", text=f"placeholder advice of {check}", level=check, level_2=term)
    for section in ("point", "curve"):
        if section not in sections:
            continue
        d = sections[section]
        for chart in d["levels"]:
            for series, stat in _series(model, section, chart, d["numbers"]):
                for i in range(12):
                    x = i / 2.0
                    y = _curve_y(stat, x, i)
                    label = str(17 + i) if section == "point" and i == 0 else None
                    t.add(section, stat, y, text=label, level=chart, level_2=series, x0=x, index=i)
    if "lag" in sections:
        for chart in sections["lag"]["levels"]:
            stat = "pacf" if chart.endswith("pacf") else "acf"
            for lag in range(1, 13):
                t.add("lag", stat, 0.6 * (-0.5) ** lag if chart.startswith("resid") else 0.8 ** lag,
                      level=chart, x0=lag, index=lag)
            t.add("lag", "bound", 1.96 / math.sqrt(132), level=chart)
    if "hist" in sections:
        for chart in sections["hist"]["levels"]:
            for series in (["no", "yes"] if chart == "prob" else [None]):
                for i in range(8):
                    t.add("hist", "count", [1, 4, 9, 15, 14, 8, 3, 1][i], level=chart, level_2=series,
                          x0=-2 + i * 0.5, x1=-1.5 + i * 0.5, index=i)
    if "stratum" in sections:
        d = sections["stratum"]
        for j, stratum in enumerate(STRATA):
            for i, stat in enumerate(_expand(d["numbers"])):
                t.add("stratum", stat, _number(stat, i + j) if not stat.startswith("surv_at") else 0.8 - 0.1 * j,
                      level=stratum)
    if "pair" in sections:
        t.add("pair", "statistic", 4.2, level=STRATA[0], level_2=STRATA[1])
        t.add("pair", "p_value", 0.04, level=STRATA[0], level_2=STRATA[1])
        t.add("pair", "p_adjusted", 0.04, level=STRATA[0], level_2=STRATA[1])
        t.add("pair", "significance", text="*", level=STRATA[0], level_2=STRATA[1])
    if "cell" in sections:
        for a in ("no", "yes"):
            for b in ("no", "yes"):
                t.add("cell", "observed", 40 if a == b else 8, level=a, level_2=b)
    return {"model": model, "variant": variant, "params": params, "fixture": "placeholder numbers, not computed",
            "columns": list(OUTPUT_NAMES), "rows": t.rows}


def _series(model: str, section: str, chart: str, stats: list[str]) -> list[tuple[str | None, str]]:
    """The series of a chart in a fixture: (level_2, stat)."""
    if chart == "km":
        return [(s, stat) for s in STRATA for stat in ("surv", "lower", "upper")]
    if chart == "cumhaz":
        return [(s, "cumhaz") for s in STRATA]
    if chart in ("loglog", "adjusted_survival"):
        return [(s, "surv" if chart == "adjusted_survival" else "y") for s in STRATA]
    if chart in ("censored", "at_risk"):
        return [(s, "surv" if chart == "censored" else "n_risk") for s in STRATA]
    if chart == "forecast":
        return [(None, s) for s in ("observed", "fitted", "forecast", "lower_80", "upper_80", "lower", "upper")]
    if chart == "paths":
        return [("price", "y"), ("channel: web", "y"), ("others", "y")]
    if chart == "schoenfeld" and section == "curve":
        return [(term, stat) for term, _ in TERMS["cox"] for stat in ("smooth", "lower", "upper")]
    if chart == "schoenfeld":
        return [(term, "y") for term, _ in TERMS["cox"]]
    if chart == "martingale":
        return [("load", "lowess" if section == "curve" else "y")]
    if section == "curve" and chart in ("resid_fitted", "scale_location"):
        return [("lowess", "y")]
    if section == "curve" and chart == "qq":
        return [("line", "y")]
    return [(None, "y")]


def _curve_y(stat: str, x: float, i: int) -> float:
    if stat in ("surv", "lower", "upper"):
        base = math.exp(-0.15 * x)
        return base * (0.9 if stat == "lower" else 1.1 if stat == "upper" else 1.0)
    if stat == "cumhaz":
        return 0.15 * x
    if stat == "n_risk":
        return float(60 - 4 * i)
    if stat in ("observed", "fitted", "forecast", "lower_80", "upper_80", "lower", "upper"):
        level = 100 + 10 * math.sin(i / 2)
        return level + {"lower_80": -5, "upper_80": 5, "lower": -9, "upper": 9, "fitted": 1}.get(stat, 0)
    return round(math.sin(i * 1.3) * (1 + x / 6), 6)


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)
    for model, variant in PARAMS:
        path = HERE / f"{model}-{variant}.json"
        path.write_text(json.dumps(build(model, variant), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{path.name} written")


if __name__ == "__main__":
    main()
