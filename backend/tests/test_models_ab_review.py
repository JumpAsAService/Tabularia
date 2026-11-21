"""The final review of the A/B test's reports (docs/specs/ab-testing.md, lot E10): what the trial on kind found and the
review corrected — the checks' degrees of freedom on many units, the count of the units without the covariate, the
planning's axis of days and its histogram, a headline that is a verdict only when a test was made, the variants' names
in the notes and in the keys, the header's line."""
from __future__ import annotations

import json
import re

import pytest

from app.engine.models import layouts
from app.engine.models.layouts import common
from app.engine.models.layouts import experiments as e
from app.engine.stats.layouts import LOCALES
from app.reports import formats, glyphs
from app.reports.model import CELL_PAD
from tests.test_models_layouts import draw, fixture, strings
from tests.test_reports_model import build
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service


def _assumptions(model: str, params: dict, locale: str = "en") -> dict:
    [table] = [it for it in layouts.definition(model, params, locale, None, None)["sections"]["body"]["items"] if it["id"] == "assumptions"]
    return table


def _with_check(rows: list[dict], check: str, **numbers: float) -> list[dict]:
    """The rows with the numbers of one check replaced (or added)."""
    keep = [r for r in rows if not (r["section"] == "assumption" and r["level"] == check and r["stat"] in numbers)]
    return keep + [{"section": "assumption", "level": check, "level_2": None, "stat": k, "value": v, "text": None, "x0": None,
                    "x1": None, "ord": 9000 + i, "key": f"assumption|{check}||{k}"} for i, (k, v) in enumerate(numbers.items())]


# ── a) the checks' degrees of freedom: Welch's F on a million units, on one line ────────────────────────────────────
@pytest.mark.parametrize("locale", LOCALES)
def test_welch_s_degrees_of_freedom_on_many_units_are_whole_and_fit_their_column(locale):
    """On kind «1; 17998.49» wrapped onto the Statistic's cell. From a thousand on the degrees of freedom are whole, and
    the A/B test's column has room for «1; 999998» on one line (two variants, a million units) in the table's type."""
    model, params, rows = fixture("ab_test-continuous")
    table = _assumptions(model, params, locale)
    [col] = [c for c in table["columns"] if c["header"] == common.words(model, "continuous", locale)["assumptions"]["df"]]
    size = table.get("style", {}).get("size", 9)
    room = col["width"] - 2 * CELL_PAD + 0.2
    assert glyphs.width_mm("1; 999998", size) <= room, col["width"]  # two variants, a million units: one line
    assert glyphs.width_mm("1999998", size) <= room  # twenty variants and two million: two lines, no word past the column
    assert "FORMAT(" in col["value"] and '>= 1000, FORMAT(' in col["value"]
    # the other models keep their column
    lin, lparams, _ = fixture("linear-unpenalized")
    [other] = [c for c in _assumptions(lin, lparams)["columns"] if c["header"] == common.words(lin, "unpenalized", "en")["assumptions"]["df"]]
    assert other["width"] == 12


def test_a_report_with_welch_s_df_of_a_million_units_draws_without_a_warning(tmp_path, chart_service, monkeypatch):  # noqa: F811
    model, params, rows = fixture("ab_test-continuous")
    wide = _with_check(rows, "covariate_balance", df=19.0, df_2=1999998.49)
    out, page = draw(tmp_path, chart_service, monkeypatch, "ab_test-continuous", fmt="svg", rows=wide, locale="it")
    said = set(strings(json.loads(page)))
    assert "19; 1999998" in said and not any("1999998,49" in s for s in said)
    assert not [w for w in out.warnings if "assumptions" in w], out.warnings
    small = _with_check(rows, "covariate_balance", df=1.0, df_2=245.5)
    _, page = draw(tmp_path / "small", chart_service, monkeypatch, "ab_test-continuous", fmt="svg", rows=small, locale="it")
    assert "1; 245,5" in set(strings(json.loads(page)))  # below a thousand, as before


# ── b) the units without the covariate are counted as such ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("locale", LOCALES)
def test_the_units_without_the_covariate_and_the_variants_without_variance_are_counted_in_their_own_words(locale):
    """On kind the value of «Empty covariates spread alike» said «11,881 rows over the threshold» (the common words of a
    count): they are units without the covariate. And a variant without variance is counted, not a bare number."""
    model, params, rows = fixture("ab_test-continuous")
    rows = _with_check(rows, "covariate_missing_balance", count=11881.0)
    rows = _with_check(rows, "variance_zero", count=1.0)
    said = {r["level"]: r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "assumption" and r["stat"] == "count"}
    w = common.words(model, "continuous", locale)
    group = formats.locale(locale).group
    assert said["covariate_missing_balance"] == w["checks"]["covariate_missing_balance"]["count"]["other"].replace("{n}", f"11{group}881")
    assert said["variance_zero"] == w["checks"]["variance_zero"]["count"]["one"].replace("{n}", "1")
    threshold = common.words(model, "continuous", locale)["assumptions"]["count"]["other"].replace("{n}", "")
    assert threshold.strip() not in said["covariate_missing_balance"]
    english = common.words(model, "continuous", "en")["checks"]["covariate_missing_balance"]
    assert english["count"]["other"] == "{n} units without the covariate" and english["name"].startswith("Units without the covariate")


# ── c) the planning's charts: days are days, every bar has its interval ────────────────────────────────────────────
def _charts(case: str, locale: str = "en") -> dict[str, dict]:
    model, params, rows = fixture(case)
    built = build(layouts.definition(model, params, locale, None, None), layouts.with_labels(model, params, rows, locale))
    return {c.alt: c.option for c in built.charts}


def test_the_smallest_lift_against_the_days_writes_days_on_x_and_the_lift_in_percent_on_y():
    """On kind the x axis of days was written as percentages («3,000.00% … 15,000.00%», the labels touching)."""
    ch = common.words("ab_plan", "continuous", "en")["charts"]
    options = _charts("ab_plan-continuous")
    [days] = [o for alt, o in options.items() if ch["mde_days"] in alt]
    assert days["xAxis"]["axisLabel"]["formatter"]["$fmt"] == "compact" and days["xAxis"]["axisLabel"]["hideOverlap"] is True
    assert days["yAxis"]["axisLabel"]["formatter"]["$fmt"] == "percent"
    [power] = [o for alt, o in options.items() if ch["power_lift"] in alt]
    assert power["xAxis"]["type"] == "log" and power["xAxis"]["axisLabel"]["formatter"]["$fmt"] == "percent"
    assert power["yAxis"]["axisLabel"]["formatter"]["$fmt"] != "percent"  # the power, 0 to 1, as before


def test_x_format_is_an_xy_chart_s_and_not_one_of_dates():
    from app.reports import definition as rd

    base = {"id": "c", "type": "chart", "dataset": "result", "x": 0, "y": 0, "w": 90, "h": 55, "category": "[x0]",
            "series": [{"value": "[value]", "name": "y"}]}
    doc = {"v": 1, "locale": "en", "datasets": [{"name": "result"}], "sections": {"body": {"items": []}}}
    for kind, options, wrong in (("column", {"x_format": "#,##0"}, True), ("xy", {"x_format": "#,##0"}, False),
                                 ("xy", {"x_format": "#,##0", "x_time": True}, True)):
        d = {**doc, "sections": {"body": {"items": [{**base, "kind": kind, "options": options}]}}}
        problems = [p for p in rd.problems(rd.load(d)) if "x_format" in str(p)]
        assert bool(problems) == wrong, (kind, options, problems)


@pytest.mark.parametrize("case", ("ab_plan-continuous", "ab_test-continuous"))
def test_every_bar_of_the_history_is_said_by_its_interval_never_blank(case):
    """On kind the first bar of the planning's histogram read «(blank)»: its open bin had no lower edge. The runner now
    gives each open bin the smallest (largest) value; a row with one edge only would still be said by that edge."""
    model, params, rows = fixture(case)
    hist = [r for r in layouts.with_labels(model, params, rows, "it") if r["section"] == "hist"]
    assert hist and all(isinstance(r["text"], str) and r["text"].startswith("[") for r in hist), [r["text"] for r in hist][:3]


# ── the headline: a verdict only from a test ───────────────────────────────────────────────────────────────────────
def test_no_comparison_computed_is_no_verdict_and_no_difference_is_said_only_of_a_test(tmp_path, chart_service, monkeypatch):  # noqa: F811
    model, params, rows = fixture("ab_test-continuous")
    untested = [{**r, "text": "not_computed"} if r["section"] == "comparison" and r["stat"] == "decision" else r for r in rows]
    _, page = draw(tmp_path, chart_service, monkeypatch, "ab_test-continuous", fmt="svg", rows=untested)
    said = set(strings(json.loads(page)))
    h = common.words(model, "continuous", "en")
    assert any(s.startswith("No treatment could be compared with the control A") for s in said), sorted(s for s in said if s.startswith("No"))
    assert not any(s.startswith("No variant shows a difference") for s in said)
    _, page = draw(tmp_path / "tested", chart_service, monkeypatch, "ab_test-continuous", fmt="svg")
    said = set(strings(json.loads(page)))
    assert any(s.startswith("No variant shows a difference from the control A at α = 0.05. With these units the test had a probability of 80 %")
               for s in said), sorted(s for s in said if s.startswith("No"))
    assert h["tables"]["prob_best"] == "P(ahead of all)"  # not «highest»: with «lower is better» it is the lowest


# ── the readings the review changed ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("locale", LOCALES)
def test_a_comparison_says_its_p_value_and_then_the_corrected_one(locale):
    model, params, rows = fixture("ab_test-binary")
    loc = formats.locale(locale)
    [b] = [r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["key"] == "reading|comparison|B|line_1"]
    assert common.number_form(0.002548942994697878, "p", loc) in b and common.number_form(0.005097885989, "p", loc) in b
    none = {**params, "correction": "none"}
    [b] = [r["text"] for r in layouts.with_labels(model, none, rows, locale) if r["key"] == "reading|comparison|B|line_1"]
    assert common.number_form(0.005097885989, "p", loc) not in b  # no correction: the p-value alone


@pytest.mark.parametrize("locale", LOCALES)
def test_several_treatments_better_are_said_together_with_the_numbers_of_one(locale):
    w = common.words("ab_test", "binary", locale)
    line = "@decision_better_many variants=B,C variant=C control=A lift=0.12 p_adjusted=0.004 alpha=0.05"
    said, _ = common.reading_said(w, "decision", line, formats.locale(locale), {}, "")
    assert "B, C" in said and "C" in said.replace("B, C", "") and "{" not in said
    assert any(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", said, re.I) for v in w["verdict_words"])  # a verdict, of a test


@pytest.mark.parametrize("locale", LOCALES)
def test_a_rate_s_plan_says_its_baseline_as_a_percent_and_the_lift_relative(locale):
    model, params, rows = fixture("ab_plan-binary")
    said = [r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading" and r["level"] == "plan"]
    assert said and not any("{" in s for s in said)
    if locale == "en":
        assert any(re.search(r"relative lift of [\d.]+ % on a rate of [\d.]+ %", s) for s in said), said
    w = common.words(model, "binary", locale)["reading_texts"]
    assert "{baseline:pct}" in w["plan_size"]["metric_type=binary"] and "{baseline:num}" in w["plan_size"][""]


# ── names of variants in notes and keys ─────────────────────────────────────────────────────────────────────────────
def test_a_value_holding_a_mark_of_the_sentence_is_put_in_as_it_is():
    """A variant called «{p_adjusted:p}» was replaced again by the next value of the note (a value is never read twice)."""
    w = {"reading_texts": {"x": "{variant} beats {control}: {p_adjusted:p}"}}
    said = common.structured_note(w, "@x variant=%7Bp_adjusted:p%7D control=%7Bvariant%7D p_adjusted=0.004", formats.locale("en"), "reading_texts")
    assert said == "{p_adjusted:p} beats {variant}: 0.0040"
    said = common.structured_note(w, "@x variant={p_adjusted:p} control=A p_adjusted=0.004", formats.locale("en"), "reading_texts")
    assert said == "{p_adjusted:p} beats A: 0.0040"


def test_a_variant_with_a_bar_or_a_backslash_is_found_by_its_own_label(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The runner's keys join their parts with «|» and escape nothing (results.py checks them so): the page looks a
    variant up by its label as it is."""
    model, params, rows = fixture("ab_test-binary")
    renamed = []
    for r in rows:
        old = "|".join(x or "" for x in (r["section"], r["level"], r["level_2"], r["stat"]))
        r = {**r, **{part: "B|x\\y" if r[part] == "B" else r[part] for part in ("level", "level_2")}}
        r["key"] = "|".join(x or "" for x in (r["section"], r["level"], r["level_2"], r["stat"])) + r["key"][len(old):]
        renamed.append(r)
    _, page = draw(tmp_path, chart_service, monkeypatch, "ab_test-binary", fmt="svg", rows=renamed)
    said = strings(json.loads(page))
    assert "B|x\\y" in said and "12,480" in said and any(s.startswith("0.0025") for s in said)  # its units and its p-value, by its key


def test_the_header_s_line_is_cut_and_one_line():
    params = {"variant": "v", "control": "A" * 5000, "metric_type": "binary", "metric": "converted\nnow"}
    said = layouts.LAYOUTS["ab_test"][2](params, "en")
    assert len(said) <= e.ABOUT_CHARS and "\n" not in said and said.endswith("…")
    assert layouts.LAYOUTS["ab_plan"][2]({**params, "metric": "m " * 400}, "en").endswith("…")
