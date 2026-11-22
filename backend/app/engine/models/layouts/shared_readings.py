"""The lines of the explanation the regressions and the survival models share (docs/specs/report-explanations.md, «The
lines every model shares», lot B2): the checks of the assumptions and the tests of the coefficients, chosen from the rows
the runner wrote. A verdict comes only from a p-value compared with the node's α; a check read by a threshold is said
by its number and its threshold, never as a verdict. Nothing is computed: the lines select, count and sort rows.

Every model's file calls these from the function it registers in `common.READINGS`; the words are each model's
(`report_texts/<model>.json`: `assumptions_*`, `threshold_*`, `coef_*`, `more_in_table`).
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from app.engine.models.layouts import common as c

LISTED = 5  # a reading names five terms, checks, pairs or strata at most; «and k more» says the rest (the table has them)
INTERCEPT = "(Intercept)"
# the checks read by a threshold, not by a test, which a reading says by their number (`threshold_<check>`), in the
# order the readings say them; those of PER_TERM have a row a term, said in one line
THRESHOLD_CHECKS = ("vif", "condition_number", "cooks_distance", "leverage", "deviance_residuals", "dfbeta", "epv",
                    "few_at_risk")
PER_TERM = frozenset({"vif", "dfbeta"})
MARKED = ("doubtful", "failed")  # how the table marks a check past its threshold


def keyed(rows: list[dict]) -> dict[str, dict]:
    return {r["key"]: r for r in rows if r.get("key")}


def number(k: Mapping[str, dict], key: str) -> float | None:
    v = (k.get(key) or {}).get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def text(k: Mapping[str, dict], key: str) -> str | None:
    v = (k.get(key) or {}).get("text")
    return v if isinstance(v, str) and v else None


def model(k: Mapping[str, dict], stat: str) -> float | None:
    return number(k, c.model_key(stat))


def alpha(checked: Mapping[str, Any]) -> float:
    """The node's α (the catalogue's default filled in): the one the runner compared its checks with."""
    a = checked.get("alpha")
    return float(a) if isinstance(a, (int, float)) else 0.05


def conf(checked: Mapping[str, Any]) -> float:
    """The level of the intervals the runners write, 1 − α (the node's setting said as a level, not a number of the data)."""
    return round(1.0 - alpha(checked), 12)


def term(r: Mapping[str, Any]) -> str:
    """A term as the tables write it: «price», «channel: web»."""
    return str(r["level"]) + (f": {r['level_2']}" if r.get("level_2") not in (None, "") else "")


def listed(block: str, names: list[str]) -> tuple[list[str], list[c.ReadingLine]]:
    """The first LISTED names, and the line that says how many more the table has (none when there are no more)."""
    more = len(names) - LISTED
    return names[:LISTED], ([(block, None, "more_in_table", {"more": more})] if more > 0 else [])


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def assumption_lines(rows: list[dict], checked: Mapping[str, Any], *, tests: bool = True) -> list[c.ReadingLine]:
    """The reading of the checks: those that are tests, by their p-values against α (`assumptions_rejected`, the checks
    whose test rejects its hypothesis, counted; `assumptions_none_rejected`); those read
    by a threshold and marked past it, by their numbers (`threshold_<check>`); with many rows, that a test then rejects
    departures too small to matter (`assumptions_many_rows`)."""
    a, k = alpha(checked), keyed(rows)
    lines: list[c.ReadingLine] = []
    tested = [r for r in rows if r["section"] == "assumption" and r["stat"] == "p_value" and is_number(r.get("value"))]
    if tests and tested:
        rejected = [r for r in tested if r["value"] < a]
        if rejected:  # counted, not named: the table marks them (a check's name in the page's words is not the layout's)
            lines.append(("assumptions", None, "assumptions_rejected", {"count": len(rejected), "total": len(tested), "alpha": a}))
        else:
            lines.append(("assumptions", None, "assumptions_none_rejected", {"total": len(tested), "alpha": a}))
    with_p = {(r["level"], r.get("level_2")) for r in tested}
    for check in THRESHOLD_CHECKS:
        marked = [r for r in rows if r["section"] == "assumption" and r["level"] == check and r["stat"] == "verdict"
                  and r.get("text") in MARKED and (check, r.get("level_2")) not in with_p]
        if not marked:
            continue

        def of(r: dict, stat: str) -> float | None:
            return number(k, f"assumption|{check}|{r.get('level_2') or ''}|{stat}")

        threshold = of(marked[0], "threshold")
        if threshold is None:
            continue
        if check in PER_TERM:
            terms = [r for r in marked if of(r, "value") is not None]
            if not terms:
                continue
            shown, more = listed("assumptions", [str(r.get("level_2") or "") for r in terms])
            lines += [("assumptions", None, f"threshold_{check}", {"names": shown, "values": [of(r, "value") for r in terms[:LISTED]],
                                                                   "threshold": threshold}), *more]
        else:
            r = marked[0]
            lines.append(("assumptions", None, f"threshold_{check}", {"value": of(r, "value"), "threshold": threshold,
                                                                     "count": of(r, "count")}))
    n = model(k, "n")
    if tests and tested and n is not None and n > c.MANY_ROWS:
        lines.append(("assumptions", None, "assumptions_many_rows", {"n": n}))
    return lines


def coefficient_lines(rows: list[dict], checked: Mapping[str, Any]) -> tuple[list[c.ReadingLine], list[dict]]:
    """The reading of the coefficients' tests (the intercept left out: its test asks whether the outcome is 0 when every
    predictor is): the terms whose p-value is below α, five named at most by their p-values (`coef_some`; `coef_all`
    when every term's is), or none (`coef_none`). Also the significant rows, by their p-values, for the line of a ratio."""
    a = alpha(checked)
    tested = [r for r in rows if r["section"] == "coef" and r["stat"] == "p_value" and r["level"] != INTERCEPT
              and is_number(r.get("value"))]
    if not tested:
        return [], []
    significant = sorted((r for r in tested if r["value"] < a), key=lambda r: r["value"])
    if not significant:
        return [("coefficients", None, "coef_none", {"total": len(tested), "alpha": a})], []
    shown, more = listed("coefficients", [term(r) for r in significant])
    every = len(significant) == len(tested)  # «for the others…» would speak of none
    return [("coefficients", None, "coef_all" if every else "coef_some",
             {"count": len(significant), "total": len(tested), "names": shown, "alpha": a}), *more], significant


def ratio_line(rows: list[dict], checked: Mapping[str, Any], significant: list[dict], stats: tuple[str, str, str],
               id_: str) -> list[c.ReadingLine]:
    """What the first significant term (by its p-value) does on the ratio's scale, with its interval: an odds ratio, a
    hazard ratio, a time ratio (`<id_>` for a numeric term, `<id_>_level` for a level against the reference). An
    interaction is left out: its ratio is read with its terms', which a sentence cannot say alone."""
    k = keyed(rows)
    for r in significant:
        if "×" in str(r["level"]):
            continue
        base = f"coef|{r['level']}|{r.get('level_2') or ''}|"
        ratio, low, high = (number(k, base + s) for s in stats)
        if ratio is None or low is None or high is None:
            continue
        values: dict[str, Any] = {"covariate": r["level"], "ratio": ratio, "low": low, "high": high, "conf": conf(checked)}
        if r.get("level_2") not in (None, ""):
            return [("coefficients", None, id_ + "_level", {**values, "variant": r["level_2"]})]
        return [("coefficients", None, id_, values)]
    return []


def method_lines(*ids: str) -> list[c.ReadingLine]:
    return [("method", None, i, {}) for i in ids if i]


def formula_lines(*ids: str) -> list[c.ReadingLine]:
    return [("formulas", None, i, {}) for i in ids if i]
