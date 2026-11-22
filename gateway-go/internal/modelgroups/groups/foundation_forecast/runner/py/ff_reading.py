"""The explanation of the report of Forecast (Timestral) (docs/specs/foundation-forecast.md, «Explaining the method in the
report»). No sentence is written here: each row of the section `reading` is a template's id with the run's raw numbers and
names,

  reading|<block>|<series or empty>|line_<k>  =  @<id> name=value name=value…

in the notes' format (a space, a comma and a percent inside a value are %20, %2C and %25; a list is joined with «,»; a number
is the shortest text that reads back as the same double). The layout says each one in the page's language
(backend/app/engine/models/report_texts/timestral.json: `reading_texts`, `formula_texts`); the ids and the names each carries
are that file's. A verdict is said only from the test's p-value against alpha; a measure is said by its number.

  line(id, **values)     one row's text
  blocks(ctx)            the blocks of a run, in the report's order: summary, forecast (one per series drawn), holdout,
                         coverage, comparison, limits, licence, method, formulas
  write(rows, blocks)    the rows
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from tab_results import Rows, text

# the series whose cut context the limits block names (the rest are in the series' table: n_used)
CONTEXT_LINES = 3


def encode(x: Any) -> str:
    s = str(x)
    return s.replace("%", "%25").replace(" ", "%20").replace(",", "%2C")


def number(x: float) -> str:
    v = float(x)
    if v == 0:
        return "0"
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)


def line(id_: str, **values: Any) -> str:
    """«@id name=value …»: a None or NaN value is left out (the template says what it can without it); a list is joined
    with «,»; a name or a text is encoded."""
    parts = []
    for name, v in values.items():
        items = v if isinstance(v, (list, tuple)) else [v]
        items = [x for x in items if x is not None and not (isinstance(x, float) and math.isnan(x))]
        if not items:
            continue
        said = ",".join(number(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else encode(x) for x in items)
        parts.append(f"{name}={said}")
    return "@" + id_ + ("" if not parts else " " + " ".join(parts))


@dataclass
class Block:
    block: str
    lines: list[str]
    level_2: str | None = None


@dataclass
class Context:
    """What the reading says, gathered by the runner (raw numbers, labels, ids)."""

    size_name: str                       # Timestral-28M
    value: str
    series: str | None                   # the series column
    n_series: int
    horizon: int
    last: float                          # the latest end, as the chart's x (days since 1970, or the integer)
    integer_time: bool
    context: int                         # the most values a series' forecast read
    past: list[str]
    known: list[str]
    n_filled: int
    n_model_missing: int
    bands: list[dict[str, Any]]          # per series drawn: label, at, median, low80, high80, low90, high90
    holdout: int                         # the largest holdout (0: none)
    across: dict[str, float]
    n_not_measured: int                  # series with a holdout asked and no measures
    steps: int
    in80: int
    in90: int
    test: Any                            # ff_measures.Wilcoxon or None
    better: int
    verdict: str                         # ok, failed, not_computed
    median_d: float
    alpha: float
    cuts: list[tuple[str | None, int, int]]   # (series, values read, values of the series)
    clipped: bool                        # clip_nonnegative on and a series never negative
    licence: str
    accepted: dict[str, Any] | None      # {"by": …, "on": days since 1970} when the task says who accepted the licence
    missing: str
    extra: list[Block] = field(default_factory=list)


def blocks(ctx: Context) -> list[Block]:
    out = [_summary(ctx), *_forecasts(ctx), *_holdout(ctx), _limits(ctx), _licence(ctx), _method(ctx), _formulas(ctx)]
    return [b for b in out if b is not None and b.lines]


def _summary(ctx: Context) -> Block:
    head = "summary" + ("" if ctx.series else "_one") + ("_steps" if ctx.integer_time else "")
    lines = [line(head, size=ctx.size_name, n_series=ctx.n_series, value=ctx.value, series=ctx.series, horizon=ctx.horizon,
                  last=ctx.last, context=ctx.context)]
    if ctx.known:
        lines.append(line("summary_known", covariate=ctx.known))
    if ctx.past:
        lines.append(line("summary_past", covariate=ctx.past))
    if ctx.n_filled:
        lines.append(line("filled_linear", count=ctx.n_filled))
    if ctx.n_model_missing:
        lines.append(line("missing_model", count=ctx.n_model_missing))
    return Block("summary", lines)


def _forecasts(ctx: Context) -> list[Block]:
    head = "forecast_bands" + ("_steps" if ctx.integer_time else "")
    return [Block("forecast", [line(head, median=b["median"], at=b["at"], low80=b["low80"], high80=b["high80"], low90=b["low90"],
                                    high90=b["high90"])], b["label"] if b["label"] is not None else None) for b in ctx.bands]


def _holdout(ctx: Context) -> list[Block]:
    if not ctx.holdout:
        return [Block("holdout", [line("holdout_none")])]
    a = ctx.across
    lines = []
    if a.get("n_measured"):
        lines.append(line("holdout_mase", holdout=ctx.holdout, mase=a["mase"], baseline_mase=a["baseline_mase"]))
    if ctx.n_not_measured:
        lines.append(line("holdout_not_measured", count=ctx.n_not_measured, n_series=ctx.n_series))
    out = [Block("holdout", lines)]
    if ctx.steps:
        out.append(Block("coverage", [line("coverage", in80=ctx.in80, in90=ctx.in90, steps=ctx.steps, share80=ctx.in80 / ctx.steps,
                                           share90=ctx.in90 / ctx.steps)]))
    n = int(a.get("n_measured") or 0)
    if ctx.test is not None and ctx.verdict != "not_computed":
        t = ctx.test
        head = ("comparison_not_supported" if ctx.verdict == "ok" else
                "comparison_supported" if t.rank_biserial < 0 else "comparison_opposite")
        lines = [line(head, better=ctx.better, n_series=n, p_value=t.p_value, alpha=ctx.alpha)]
        if t.min_p >= ctx.alpha:
            lines.append(line("comparison_few_series", n_series=n, min_p=t.min_p, alpha=ctx.alpha))
        out.append(Block("comparison", lines))
    elif n == 1 and ctx.n_series == 1:
        out.append(Block("comparison", [line("comparison_one_series", mase=a["mase"], baseline_mase=a["baseline_mase"])]))
    else:
        out.append(Block("comparison", [line("comparison_not_computed", n_measured=n)]))
    return out


def _limits(ctx: Context) -> Block:
    lines = [line("limits")]
    for label, used, n in ctx.cuts[:CONTEXT_LINES]:
        lines.append(line("limits_context", series=label if label is not None else ctx.value, used=used, n=n))
    if ctx.clipped:
        lines.append(line("limits_clip"))
    return Block("limits", lines)


def _licence(ctx: Context) -> Block:
    lines = [line("licence", licence=ctx.licence)]
    if ctx.accepted and ctx.accepted.get("by") and ctx.accepted.get("on") is not None:
        lines.append(line("licence_accepted", by=ctx.accepted["by"], on=ctx.accepted["on"]))
    return Block("licence", lines)


def _method(ctx: Context) -> Block:
    ids = ["method_zero_shot", "method_quantiles"]
    if ctx.holdout:
        ids += ["method_holdout", "method_baseline", "method_coverage"]
    if ctx.test is not None and ctx.verdict != "not_computed":
        ids.append("method_wilcoxon")
    ids.append("method_limits")
    if ctx.n_filled or ctx.n_model_missing:
        ids.append("method_missing")
    return Block("method", [line(i) for i in ids])


def _formulas(ctx: Context) -> Block:
    ids = []
    if ctx.holdout:
        ids += ["formula_naive", "formula_mase", "formula_smape", "formula_pinball", "formula_coverage"]
    if ctx.test is not None and ctx.verdict != "not_computed":
        ids.append("formula_wilcoxon")
    return Block("formulas", [line(i) for i in ids])


def write(rows: Rows, out: list[Block]) -> Rows:
    for b in out:
        for k, said in enumerate(b.lines, start=1):
            text(rows, "reading", f"line_{k}", said, level=b.block, level_2=b.level_2)
    return rows
