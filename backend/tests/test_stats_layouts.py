"""The report of a hypothesis test (app/engine/stats/layouts.py): every test of the catalogue
has a layout and its words in five languages; a layout is a definition the report engine
accepts, reads only rows the test writes, and renders on the reference data; the page says
when its rows are a development sample's; the route and the task that draw it from a flow's
chain, and the rows they remember. The chart service is the fake of test_reports_render.py
(the real one is Node): what it is asked is checked here, what it draws was looked at."""
from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from datetime import datetime, timezone

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import exports
from app.api.routes import reports as report_routes
from app.api.routes import tasks
from app.engine import rowcount
from app.engine.stats import layouts
from app.engine.stats.catalog import TESTS, operation_name
from app.engine.stats.compute import run_hypothesis_test
from app.engine.stats.output import OUTPUT_NAMES
from app.engine.stats.polars_source import PolarsStatSource
from app.reports import definition as rd
from app.reports import glyphs, pdf, render
from app.tasks import jobs, report_jobs
from tests.conftest import BUCKET, upload_df
from tests.fakes import FakeRedis
from tests.test_data_correctness import _engine
from tests.test_export_route import Result, Storage, _play
from tests.reports_both_ways import both_ways  # noqa: F401 — every report, both ways
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

NOW = datetime(2026, 11, 19, 15, 30, tzinfo=timezone.utc)
# The reference test's report (Welch, two-sided, English), byte for byte. It changes with the
# layout, its words, the template, the fonts or Typst's version: look at the new PDF with the
# real chart service, then update the hash.
GOLDEN_PDF_SHA256 = "658e6061947d233cb42fca5085436120eadb2e956463eec0cafc08df8c05fb5b"

A = [5.1, 4.9, 6.2, 5.8, 6.0, 5.5, 5.3, 6.7]
B = [6.9, 7.2, 6.5, 7.8, 7.1, 6.4, 8.0]
C = [5.9, 6.1, 7.0, 6.6, 5.7, 6.3]
BEFORE = [72, 80, 65, 90, 85, 77, 68, 95, 88, 70]
AFTER = [75, 78, 70, 94, 85, 83, 71, 97, 86, 76]
PX = [1.2, 2.3, 2.9, 4.1, 5.0, 6.2, 6.8, 8.1, 9.3, 10.0]
PY = [2.1, 2.9, 3.2, 4.8, 4.9, 6.9, 6.5, 8.8, 8.9, 10.9]
WELCH = {"value": "v", "group": "g", "groups": ["a", "b"]}
# what each test of the catalogue is asked on the reference data
REFERENCE = {
    "t_one_sample": {"value": "v", "mu": 5},
    "t_two_sample": WELCH,
    "t_paired": {"x": "before", "y": "after"},
    "anova": {"value": "v", "group": "g", "posthoc": "tukey"},
    "var_f": WELCH,
    "levene": {"value": "v", "group": "g"},
    "pearson": {"x": "px", "y": "py"},
    "prop_one_sample": {"value": "answer", "success": "yes", "p0": 0.25},
    "prop_two_sample": {"value": "answer", "success": "yes", "group": "g"},
    "chi2_independence": {"x": "x", "y": "y"},
    "chi2_gof": {"value": "cat"},
    "fisher_exact": {"x": "x", "y": "y"},
    "mcnemar": {"x": "x", "y": "y"},
    "cmh": {"x": "x", "y": "y", "strata": "z"},
    "g_test": {"x": "x", "y": "y"},
    "spearman": {"x": "px", "y": "py"},
    "mann_whitney": WELCH,
    "wilcoxon_signed_rank": {"x": "before", "y": "after"},
    "kruskal_wallis": {"value": "v", "group": "g", "posthoc": "dunn"},
    "ks_two_sample": WELCH,
    "normality": {"value": "nv"},
}
# and the variants of each, together every row it may write
VARIANTS = {
    "t_one_sample": ({}, {"alternative": "less"}, {"alternative": "greater", "mu": 6}),
    "t_two_sample": ({}, {"alternative": "less"}, {"alternative": "greater", "var_equal": True}),
    "t_paired": ({}, {"alternative": "less"}, {"alternative": "greater", "mu": -1}),
    "anova": ({}, {"var_equal": False, "posthoc": "games_howell"}, {"posthoc": "pairwise_t"},
              {"posthoc": "pairwise_t", "p_adjust": "bonferroni", "var_equal": False}, {"posthoc": "none"}),
    "var_f": ({}, {"alternative": "less"}, {"alternative": "greater", "ratio": 2}),
    "levene": ({}, {"center": "mean"}),
    "pearson": ({}, {"alternative": "less"}, {"alternative": "greater"}),
    "prop_one_sample": ({}, {"correction": False, "alternative": "less"}, {"method": "exact"},
                        {"method": "exact", "alternative": "greater"}, {"alternative": "greater", "success": "no"}),
    "prop_two_sample": ({}, {"correction": False}, {"alternative": "less"}, {"alternative": "greater", "groups": ["b", "a"]}),
    "chi2_independence": ({}, {"posthoc": "cells"}, {"posthoc": "pairs", "p_adjust": "bonferroni"}, {"posthoc": "cells", "p_adjust": "none"}),
    "chi2_gof": ({}, {"expected": {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4}}),
    "fisher_exact": ({}, {"alternative": "less"}, {"alternative": "greater"}),
    "mcnemar": ({}, {"method": "exact"}, {"correction": False}),
    "cmh": ({}, {"correction": False}),
    "g_test": ({}, {"williams": True}, {"posthoc": "cells", "p_adjust": "sidak"}),
    "spearman": ({}, {"alternative": "less"}, {"alternative": "greater"}),
    "mann_whitney": ({}, {"method": "asymptotic"}, {"alternative": "less", "correction": False, "method": "asymptotic"},
                     {"alternative": "greater"}),
    "wilcoxon_signed_rank": ({}, {"zeros": "pratt", "alternative": "less"}, {"y": None, "mu": 80.5},
                             {"y": None, "mu": 80, "method": "asymptotic", "alternative": "greater"}),
    "kruskal_wallis": ({}, {"posthoc": "none"}, {"p_adjust": "bonferroni"}),
    "ks_two_sample": ({}, {"method": "asymptotic"}, {"alternative": "greater", "method": "asymptotic"}, {"alternative": "less"}),
    "normality": ({}, {"method": "jarque_bera"}),
}
NV = [4.2, 5.1, 3.9, 6.3, 5.5, 4.8, 5.0, 7.9, 4.4, 5.2, 6.1, 3.5, 5.7, 4.9, 5.3, 9.4, 4.6, 5.8, 5.0, 4.1, 6.6, 5.4, 4.7, 5.9]


def strata_table(dims: tuple[int, int, int], values: list[int]) -> pl.DataFrame:
    """A table within strata given as R's array(values, dim = c(r, c, K)) (column-major), as the
    rows (x, y, z) that make it, and a row with no stratum."""
    r, c, k = dims
    cells = [(f"r{i}", f"c{j}", f"s{s}") for s in range(k) for j in range(c) for i in range(r)
             for _ in range(values[s * r * c + j * r + i])]
    cells.append(("r0", "c0", None))
    return pl.DataFrame({"x": [a for a, _, _ in cells], "y": [b for _, b, _ in cells], "z": [z for _, _, z in cells]},
                        schema={"x": pl.String, "y": pl.String, "z": pl.String})


RABBITS = ((2, 2, 5), [0, 0, 6, 5, 3, 0, 3, 6, 6, 2, 0, 4, 5, 6, 1, 0, 2, 5, 0, 0])  # R's mantelhaen.test example


def abc() -> pl.DataFrame:
    """The reference samples, with what every engine leaves out (test_stats_reference.py)."""
    rows = [("a", v) for v in A] + [("b", v) for v in B] + [("c", v) for v in C]
    rows += [("a", None), ("b", math.nan), (None, 9.9)]
    return pl.DataFrame({"g": [g for g, _ in rows], "v": [v for _, v in rows]}, schema={"g": pl.String, "v": pl.Float64})


def pairs() -> pl.DataFrame:
    """Two pairs of columns of the same rows, and a row each pair leaves out."""
    return pl.DataFrame({"before": [float(v) for v in BEFORE] + [None], "after": [float(v) for v in AFTER] + [80.0],
                         "px": PX + [math.nan], "py": PY + [3.0]})


def counts_table(rows: list[str], cols: list[str], counts: list[list[int]]) -> pl.DataFrame:
    """A contingency table as the rows that make it (x, y), and a row each column leaves out."""
    cells = [(a, b) for a, row in zip(rows, counts) for b, n in zip(cols, row) for _ in range(n)]
    cells += [(None, cols[0]), (rows[0], None)]
    return pl.DataFrame({"x": [a for a, _ in cells], "y": [b for _, b in cells]}, schema={"x": pl.String, "y": pl.String})


def proportions() -> pl.DataFrame:
    """37 successes of 120 in «a», 52 of 110 in «b»; a row with no group, one with no answer."""
    g = ["a"] * 120 + ["b"] * 110 + [None, "a"]
    answer = ["yes"] * 37 + ["no"] * 83 + ["yes"] * 52 + ["no"] * 58 + ["yes", None]
    return pl.DataFrame({"g": g, "answer": answer}, schema={"g": pl.String, "answer": pl.String})


def frame_of(test: str) -> pl.DataFrame:
    if test == "prop_one_sample":
        return proportions().filter(pl.col("g") == "a")
    if test == "prop_two_sample":
        return proportions()
    if test == "chi2_independence":
        return counts_table(["x1", "x2", "x3"], ["m", "f"], [[30, 15], [20, 25], [10, 20]])
    if test == "fisher_exact":
        return counts_table(["a", "b"], ["x", "y"], [[12, 5], [7, 16]])
    if test == "mcnemar":
        return counts_table(["a", "b"], ["a", "b"], [[794, 150], [86, 570]])
    if test == "cmh":
        return strata_table(*RABBITS)
    if test == "g_test":
        return counts_table(["x1", "x2", "x3"], ["m", "f"], [[30, 15], [20, 25], [10, 20]])
    if test == "chi2_gof":
        return pl.DataFrame({"cat": ["a"] * 18 + ["b"] * 22 + ["c"] * 30 + ["d"] * 30 + [None]})
    if test in ("t_paired", "pearson", "spearman", "wilcoxon_signed_rank"):
        return pairs()
    if test == "normality":
        return pl.DataFrame({"nv": NV + [None]}, schema={"nv": pl.Float64})
    if test == "t_one_sample":
        return abc().filter(pl.col("g") == "a")
    return abc()


def rows_of(test: str = "t_two_sample", **params) -> list[dict]:
    out = run_hypothesis_test(PolarsStatSource(frame_of(test).lazy()), test, {**REFERENCE[test], **params, "detail": "report"}, "polars")
    return [dict(zip(OUTPUT_NAMES, r)) for r in out]


def strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in strings(v)]
    return []


def draw(tmp_path, chart_service, monkeypatch, fmt="pdf", locale="en", sample=None, step=None, test="t_two_sample", **params):  # noqa: F811
    """The report of the reference test, and what the template was handed (the page's texts)."""
    handed: list[str] = []
    run = pdf.run

    def spy(job, *args, **kwargs):
        if "inputs" in job:
            handed.append(job["inputs"]["model"])
        return run(job, *args, **kwargs)

    monkeypatch.setattr(pdf, "run", spy)
    asked = {**REFERENCE[test], **params}
    datasets = {"result": {"rows": layouts.with_labels(test, rows_of(test, **params), locale), "columns": layouts.columns()}}
    out = render.render(layouts.definition(test, asked, locale, sample, step), datasets, {}, fmt, tmp_path,
                        title=layouts.title(test, asked, locale), now=NOW,
                        limits=render.Limits(chart_url=chart_service[0], max_rows=layouts.MAX_REPORT_ROWS))
    return out, (handed[0] if handed else "")


# ── the catalogue, the words ─────────────────────────────────────────────────
def test_every_test_of_the_catalogue_has_a_layout_and_its_reference():
    assert set(layouts.LAYOUTS) == set(TESTS) == set(REFERENCE)


def _keys(node, prefix=""):
    if isinstance(node, dict):
        return {k for name, v in node.items() for k in _keys(v, f"{prefix}{name}.")}
    return {prefix.rstrip(".")}


def test_the_words_are_complete_in_the_five_languages():
    words = layouts.all_texts()
    assert tuple(words) == layouts.LOCALES
    english = _keys(words["en"])
    for code in layouts.LOCALES:
        assert _keys(words[code]) == english, f"{code}: {sorted(_keys(words[code]) ^ english)}"
        assert all(isinstance(s, str) and s.strip() for s in strings(words[code])), code
    # every test has its name, and every number it writes a name of its own in each language
    for test in TESTS:
        written = {(r["section"], r["stat"]) for params in VARIANTS[test] for r in rows_of(test, **params)}
        for code in layouts.LOCALES:
            assert words[code]["tests"][test]["name"]
            unnamed = sorted(s for s in written if layouts.stat_label(test, *s, code) == s[1] and s != ("group", "n"))
            assert unnamed == [], f"{code}: no word for {unnamed}"
    assert layouts.stat_label("t_two_sample", "test", "note_2", "it") == "Nota 2"
    assert layouts.stat_label("t_two_sample", "test", "estimate", "en") == "Difference of the means (m1 − m2)"
    assert layouts.stat_label("t_two_sample", "test", "newer_than_the_words", "de") == "newer_than_the_words"
    assert layouts.texts("xx") is words["en"]


def test_the_words_and_the_formulas_use_only_characters_the_pdf_font_has():
    for code, words in layouts.all_texts().items():
        assert glyphs.missing(set("".join(strings(words)))) == set(), code
    engine_texts = "".join(r["text"] for test in TESTS for p in VARIANTS[test] for r in rows_of(test, **p) if r["text"])
    assert glyphs.missing(set(engine_texts) | set("−∞+∞ · —")) == set()


# ── the definition ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("test", sorted(TESTS))
@pytest.mark.parametrize("locale", layouts.LOCALES)
def test_a_layout_is_a_definition_the_report_engine_accepts(test, locale):
    columns = {"result": {c["name"]: c["type"] for c in layouts.columns()}}
    for sample in (None, [{"first": 100_000}]):
        d = layouts.definition(test, REFERENCE[test], locale, sample, "Channels compared")
        assert [str(p) for p in rd.check(d, columns)] == []
        assert d["locale"] == locale and d["parameters"] == [] and [x["name"] for x in d["datasets"]] == ["result"]
        assert d["page"]["size"] == "A4" and d["page"]["orientation"] == "portrait"
        ids = [it["id"] for band in d["sections"].values() for it in band["items"]]
        assert len(ids) == len(set(ids))


@pytest.mark.parametrize("test", sorted(TESTS))
def test_a_layout_reads_only_rows_the_test_writes(test):
    """Every row a layout names by its key is a row of the test, in every variant (the side a
    one-sided alternative leaves open is a row too, with no number)."""
    for params in VARIANTS[test]:
        rows = rows_of(test, **params)
        # the explanation's rows are the layout's own (`with_labels`, docs/specs/report-explanations.md)
        rows += [r for r in layouts.with_labels(test, rows, "en") if r["section"] == "reading"]
        written = {r["key"] for r in rows}
        text = json.dumps(layouts.definition(test, {**REFERENCE[test], **params}, "en"), ensure_ascii=False)
        named = set(re.findall(r'LOOKUP\(\\"([^"\\]+)\\", \[key\]', text))
        # a reading is read line by line until there is none: its eight places, of which a run writes a few
        said = {k for k in named if k.startswith("reading|")}
        assert said and all(re.fullmatch(r"reading\|(result|pairs)\|\|line_[1-8]", k) for k in said), sorted(said)
        named -= said
        assert named and named <= written, sorted(named - written)
        # a level in a key is escaped as the output escapes it (`_key_part`): read here as the level itself
        for field in ("[level]", "[level_2]"):
            text = text.replace(json.dumps(layouts._key_part(field))[1:-1], field)
        # a key made of a group's label is a row of every group; of a pair's, of every pair
        groups = {r["level"] for r in rows if r["section"] == "group"}
        # (the margins of a contingency table: a category of x has its row's numbers, one of y its column's)
        margin = {kind: {r["level"] for r in rows if r["stat"] == f"{kind}_total"} for kind in ("row", "col")}
        for stat in re.findall(r'\\"group\|\\" & \[level\] & \\"\|\|(\w+)\\"', text):
            have = {g for g in groups if f"group|{g}||{stat}" in written}
            # of every group (of its margin), or of none: a table of another variant (CMH's strata of a 2 × 2 table)
            assert have in (set(), margin.get(stat.split("_")[0]) or groups), stat
        # a cell's key: of every cell (of a contingency table, or of the categories of a goodness of fit)
        cells = {(r["level"], r["level_2"]) for r in rows if r["section"] == "cell"}
        for stat in re.findall(r'\\"cell\|\\" & \[level\] & \\"\|\\" & \[level_2\] & \\"\|(\w+)\\"', text):
            if test == "cmh" and stat in ("index", "expected", "deviation"):
                continue  # the generalized statistic's (a table larger than 2 × 2), n − m of the first r − 1 rows and c − 1 columns
            assert {f"cell|{a}|{b}|{stat}" for a, b in cells} <= written, stat
        for stat in re.findall(r'\\"cell\|\\" & \[level\] & \\"\|\|(\w+)\\"', text):
            assert {f"cell|{a}||{stat}" for a, _ in cells} <= written, stat
        pairs_ = {(r["level"], r["level_2"]) for r in rows if r["section"] == "pair"}
        for stat in re.findall(r'\\"pair\|\\" & \[level\] & \\"\|\\" & \[level_2\] & \\"\|(\w+)\\"', text):
            assert {f"pair|{a}|{b}|{stat}" for a, b in pairs_} <= written, stat
        sections = set(re.findall(r'\[section\] = \\"(\w+)\\"', text))
        # (a variant that may be exact on the data, McNemar's, has no curve: its chart is not drawn then)
        # (and CMH's generalized statistic, whose tables are drawn for a table larger than 2 × 2: its margins of each stratum, V)
        larger = {"stratum", "covariance"} if test == "cmh" else set()
        assert {r["section"] for r in rows} <= sections and sections - {r["section"] for r in rows} <= {"curve"} | larger, (params, sections)


def test_a_label_with_a_bar_has_a_key_of_its_own(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The key is the section, the levels and the stat joined by bars: a level with a bar in it
    is escaped («a|» is «a\\|»), else («a|», «b») and («a», «|b») were one key, and the
    report's LOOKUP read one cell's count for the other's. The report escapes its levels the
    same way."""
    from app.engine.stats.output import key_part

    assert key_part("a|") == "a\\|" and key_part("\\|") == "\\\\\\|" and key_part(None) == "" and key_part("plain") == "plain"
    frame = counts_table(["a|", "a"], ["b", "|b"], [[11, 12], [13, 14]])
    params = {"x": "x", "y": "y"}
    rows = [dict(zip(OUTPUT_NAMES, r)) for r in run_hypothesis_test(PolarsStatSource(frame.lazy()), "chi2_independence",
                                                                    {**params, "detail": "report"}, "polars")]
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys))
    observed = {r["key"]: r["value"] for r in rows if r["stat"] == "observed"}
    assert observed == {"cell|a\\||b|observed": 11, "cell|a\\||\\|b|observed": 12, "cell|a|b|observed": 13, "cell|a|\\|b|observed": 14}
    out = render.render(layouts.definition("chi2_independence", params, "en"),
                        {"result": {"rows": layouts.with_labels("chi2_independence", rows, "en"), "columns": layouts.columns()}}, {},
                        "xlsx", tmp_path, title="t", now=NOW, limits=render.Limits(chart_url=chart_service[0]))
    with zipfile.ZipFile(out.path) as z:
        names = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
        sheet = z.read(f"xl/worksheets/sheet{names.index('Observed and expected') + 1}.xml").decode()
    # each cell's own count, in the order of the rows: a|–b, a|–|b, a–b, a–|b (sorted labels: «a» first)
    counts = [float(v) for v in re.findall(r'<c r="C\d+"[^>]*><v>([^<]+)</v>', sheet)]
    assert sorted(counts) == [11.0, 12.0, 13.0, 14.0]


def test_the_report_reads_as_many_rows_as_a_test_may_write():
    assert len(rows_of()) == 382  # the reference test: 27 of the test (its significance code too), 24 of its groups, 10 intervals, 321 of the curve
    assert layouts.MAX_REPORT_ROWS >= 64 + 200 * (12 + 30) + 603  # 200 groups, each with 30 intervals, and the curve
    assert layouts.MAX_REPORT_ROWS >= 64 + 2500 * 9 + 200 + 603  # a table of 50 × 50 cells, with the post-hoc of the cells
    assert layouts.MAX_REPORT_ROWS < 25_000


def test_the_name_of_a_report_is_its_test_and_its_columns():
    assert layouts.title("t_two_sample", {"value": "amount", "group": "channel"}, "en") == "Welch's t test — amount by channel"
    assert layouts.title("t_two_sample", {"value": "amount", "group": "channel", "var_equal": True}, "it") == \
        "Test t di Student — amount per channel"
    assert layouts.title("t_two_sample", {"value": "amount", "group": "channel", "var_equal": None}, "de") == \
        "Welch-t-Test — amount nach channel"
    assert layouts.title("t_two_sample", {}, "xx") == "Welch's t test"


# ── the development sample ───────────────────────────────────────────────────
def test_a_chain_says_its_development_samples_in_the_main_chain_or_in_a_branch():
    test = {"type": "stat_t_two_sample", "params": WELCH}
    first = {"type": "limit", "params": {"n": 100000, "_dev_sample": True}}
    random = {"type": "sample", "params": {"fraction": 0.1, "seed": 42, "_dev_sample": True}}
    branch = {"type": "join", "params": {"on": ["k"], "right": {"source": {"bucket": "b", "key": "k"}, "operations": [random]}}}
    assert layouts.samples_of([test]) == []
    # a limit or a sample of the person's own is a step, not a development sample
    assert layouts.samples_of([{"type": "limit", "params": {"n": 5}}, {"type": "sample", "params": {"fraction": 0.5}}, test]) == []
    assert layouts.samples_of([first, test]) == [{"first": 100000}]
    assert layouts.samples_of([random, test]) == [{"fraction": 0.1}]
    assert layouts.samples_of([branch, test]) == [{"fraction": 0.1}]
    assert layouts.samples_of([first, {**branch, "params": {**branch["params"], "right": {"operations": [first]}}}, test]) == [{"first": 100000}]
    assert layouts.samples_of([first, branch, test]) == [{"first": 100000}, {"fraction": 0.1}]
    assert layouts.sample_line(None, "en") is None and layouts.sample_line([], "it") is None
    assert layouts.sample_line([{"first": 100000}], "en") == "Rows: a development sample of the source — the first 100,000 rows"
    assert layouts.sample_line([{"first": 100000}], "it") == "Righe: un campione di sviluppo della sorgente — le prime 100.000 righe"
    assert layouts.sample_line([{"fraction": 0.1}], "en") == "Rows: a development sample of the source — a random 10% of the rows"
    assert layouts.sample_line([{"first": 500}, {"fraction": 0.025}], "en") == \
        "Rows: a development sample of the source — the first 500 rows; a random 2.5% of the rows"
    assert layouts.sample_line([{}], "en") == "Rows: a development sample of the source"


def test_the_page_says_a_development_sample_only_when_the_chain_has_one(tmp_path, chart_service, monkeypatch):  # noqa: F811
    plain, page = draw(tmp_path / "plain", chart_service, monkeypatch)
    assert plain.warnings == [] and "development sample" not in page and "Channels compared" not in page
    sampled, page = draw(tmp_path / "sampled", chart_service, monkeypatch, sample=[{"first": 100000}], step="Channels compared")
    line = "Rows: a development sample of the source — the first 100,000 rows"
    model = json.loads(page)
    assert sampled.warnings == [] and line in strings(model["header"])  # in the header band, and again in the footer
    assert f"Computed by Tabularia · {line}" in strings(model["footer"])
    assert "v by g · Channels compared" in strings(model["header"])
    # the header is taller by the line, and the body starts under it
    assert model["header"]["h"] > json.loads(draw(tmp_path / "again", chart_service, monkeypatch)[1])["header"]["h"]


# ── the pages ────────────────────────────────────────────────────────────────
def test_the_reference_report_holds_the_welch_numbers(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """Every piece needed to redo the computation by hand is on the page, with R's numbers."""
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch)
    assert out.pages == 2 and out.warnings == []
    said = set(strings(json.loads(page)))
    for text in (
        "Welch's t test", "v by g", "-4.6090", "12.7279", "0.0005 ***", "-1.4411", "95% CI: -2.1180 to -0.7641",
        "At α = 0.05 the null hypothesis is rejected (p = 0.0005).",  # (the sentence's «p {p}»: «= 0.0005», or «< 0.0001»)
        "mean(v | a) − mean(v | b) = 0", "mean(v | a) − mean(v | b) ≠ 0", "two-sided",
        "t = (m1 − m2 − mu) / √(s1²/n1 + s2²/n2), df = (s1²/n1 + s2²/n2)² / ((s1²/n1)²/(n1 − 1) + (s2²/n2)²/(n2 − 1))",
        "Distribution of the statistic under H0: t(12.7279).",
        "Critical region at α = 0.05: statistic ≤ -2.1651 or statistic ≥ 2.1651.",
        # the numbers of the test, to ten decimals
        "0.05", "0.95", "15", "3", "0.3648489011", "-1.4410714286", "0.3126632227", "-4.6090212217", "12.7278927075",
        "-2.1650730211", "2.1650730211", "0.0005168703", "-2.1180101367", "-0.7641327205", "-2.3857722701", "-2.2454327248",
        # the groups: n, mean, variance, standard deviation, standard error, the interval of the mean; the quartiles
        "8", "5.6875", "0.3641071429", "0.6034129124", "0.2133386811", "5.1830341808", "6.1919658192",
        "7", "7.1285714286", "0.3657142857", "0.6047431568", "0.2285714286", "6.5692772912", "7.687865566",
        "4.9", "5.25", "5.65", "6.05", "6.7", "6.4", "7.1", "7.5",
        "Rows used: 15. Rows left out: 3 (no group, or no value in one of the two groups).",
        "Computed in the engine: polars.", "Computed by Tabularia",
        # R's significance codes: beside the p-value in its card and in the table of the numbers, with their legend
        "Signif.", "***", "Signif. codes: 0 ‘***’ 0.001 ‘**’ 0.01 ‘*’ 0.05 ‘.’ 0.1 ‘ ’ 1",
    ):
        assert text in said, text
    assert any(s.startswith("1 = «a», 2 = «b». m: mean, s²: variance, n: rows used") for s in said)
    # the charts, as the chart service was asked for them
    charts = {spec["option"]["title"]["text"]: spec["option"] for spec in chart_service[1] if "title" in spec["option"]}
    assert sorted(charts) == ["Box plot: v by g", "Distribution of the statistic under H0", "Histogram: v by g",
                              "Means with their confidence intervals"]
    box = charts["Box plot: v by g"]
    assert box["xAxis"]["data"] == ["a", "b"]
    assert [b["value"] for b in box["series"][0]["data"]] == [[4.9, 5.25, 5.65, 6.05, 6.7], [6.4, 6.7, 7.1, 7.5, 8.0]]
    means = charts["Means with their confidence intervals"]
    assert [round(x, 6) for x in means["series"][0]["data"][0]["value"]] == [5.183034, 5.6875, 5.6875, 5.6875, 6.191966]
    hist = charts["Histogram: v by g"]
    assert hist["xAxis"]["data"] == ["[4.9 – 5.52)", "[5.52 – 6.14)", "[6.14 – 6.76)", "[6.76 – 7.38)", "[7.38 – 8]"]
    assert {s["name"]: [p and p["value"] for p in s["data"]] for s in hist["series"]} == {"a": [4, 2, 2, 0, 0], "b": [0, 0, 2, 3, 2]}
    curve = charts["Distribution of the statistic under H0"]
    line, low, high = (s for s in curve["series"] if s["type"] == "line")
    assert (line["name"], low["name"], high["name"]) == ("density", "reject_low", "reject_high")  # the line first: the rows' order
    assert len(line["data"]) == 201 and "areaStyle" not in line and "areaStyle" in low and "areaStyle" in high
    assert max(x for x, _ in low["data"]) <= -2.1650730211 and min(x for x, _ in high["data"]) >= 2.1650730211
    assert abs(line["markLine"]["data"][0]["xAxis"] - -4.609021221688905) < 1e-12
    assert curve["legend"]["show"] is False  # its series have the rows' raw names: the caption says what they are


def test_a_one_sided_report_leaves_its_open_side_open(tmp_path, chart_service, monkeypatch):  # noqa: F811
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch, alternative="less")
    said = set(strings(json.loads(page)))
    assert out.warnings == []
    for text in ("0.0003 ***", "95% CI: −∞ to -0.8865", "mean(v | a) − mean(v | b) < 0", "one-sided, less",
                 "Critical region at α = 0.05: statistic ≤ -1.7738.", "0.0002584351", "-1.7738331494", "-0.8864590396"):
        assert text in said, text
    assert "critical_high" not in said and "ci_low" not in said  # no row for a number that is not there
    curve = next(spec["option"] for spec in chart_service[1] if spec["option"].get("title", {}).get("text") == "Distribution of the statistic under H0")
    assert [s["name"] for s in curve["series"] if s["type"] == "line"] == ["density", "reject_low"]
    out, page = draw(tmp_path / "greater", chart_service, monkeypatch, groups=["a", "c"], alternative="greater", alpha=0.01)
    said = set(strings(json.loads(page)))
    assert "At α = 0.01 the null hypothesis is not rejected (p = 0.9660)." in said and "99% CI: -1.3536 to +∞" in said
    assert any(s.startswith("1 = «a», 2 = «c».") for s in said)


def test_a_very_small_p_value_is_never_printed_as_zero(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """On a very large table a p-value is tiny more often than not: the card says «< 0.0001»,
    the sentence too, and the table of the numbers writes it by its exponent."""
    [p_value] = [r["value"] for r in rows_of(mu=40.0) if r["key"] == "test|||p_value"]
    assert 0 < p_value < 1e-15
    _, page = draw(tmp_path / "en", chart_service, monkeypatch, mu=40.0)
    said = set(strings(json.loads(page)))
    assert {"< 0.0001 ***", "At α = 0.05 the null hypothesis is rejected (p < 0.0001).", layouts.tiny(p_value, "en")} <= said
    assert re.fullmatch(r"\d\.\d{1,9}e-\d+", layouts.tiny(p_value, "en")) and "0.0000" not in said
    _, page = draw(tmp_path / "it", chart_service, monkeypatch, locale="it", mu=40.0)
    said = set(strings(json.loads(page)))
    assert {"< 0,0001 ***", "Con α = 0,05 l'ipotesi nulla è rifiutata (p < 0,0001).", layouts.tiny(p_value, "it")} <= said
    assert "Codici di significatività: 0 ‘***’ 0,001 ‘**’ 0,01 ‘*’ 0,05 ‘.’ 0,1 ‘ ’ 1" in said  # the language's decimal mark
    assert layouts.tiny(5.168702726e-12, "en") == "5.168702726e-12" and layouts.tiny(-2.5e-7, "it") == "-2,5e-7"
    assert layouts.tiny(1e-300, "de") == "1e-300" and layouts.tiny(3.0e-9, "fr") == "3e-9"
    for plain in (0, 0.0, 1e-6, 0.000516870272598936, -4.6, 15, None, "text", True):
        assert layouts.tiny(plain, "en") is None
    labelled = layouts.with_labels("t_two_sample", rows_of(), "it")
    assert all(set(r) == {*OUTPUT_NAMES, "label", "tiny"} for r in labelled) and {r["tiny"] for r in labelled} == {None}
    assert [c["name"] for c in layouts.columns()] == [*OUTPUT_NAMES, "label", "tiny"]


def test_large_numbers_and_long_names_fit_the_page(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """A measure near a billion, a difference of millions, columns and a step with long names:
    nothing is wider than its place (the render would say so), nothing is read as an expression."""
    value, group = "net amount of the invoices issued in the fiscal year, in euro " * 3, "sales channel of the order " * 4
    frame = pl.DataFrame({group: ["a"] * 8 + ["b"] * 7, value: [1e9 + v for v in A] + [1e9 + 2.5e6 + v for v in B]})
    params = {"value": value, "group": group}
    out = run_hypothesis_test(PolarsStatSource(frame.lazy()), "t_two_sample", {**params, "detail": "report"}, "polars")
    rows = [dict(zip(OUTPUT_NAMES, r)) for r in out]
    d = layouts.definition("t_two_sample", params, "en", [{"first": 10**9}, {"fraction": 0.123456}], "=SUM([value]) " + "a very long name " * 12)
    handed: list[str] = []
    run = pdf.run
    monkeypatch.setattr(pdf, "run", lambda job, *a, **k: (handed.append(job.get("inputs", {}).get("model", "")), run(job, *a, **k))[1])
    del chart_service[1][:]
    done = render.render(d, {"result": {"rows": layouts.with_labels("t_two_sample", rows, "en"), "columns": layouts.columns()}}, {}, "pdf",
                         tmp_path, title="t", now=NOW, limits=render.Limits(chart_url=chart_service[0]))
    assert done.warnings == []
    model = json.loads(handed[0])
    said = set(strings(model))
    assert {"-2,500,001", "95% CI: -2,500,002 to -2,500,001", "1000000004.9", "1002500008"} <= said
    # the groups' numbers as they are, never rounded to fit (the data whole in Excel): ten decimals at most
    from app.reports import formats

    means = [r["value"] for r in rows if r["section"] == "group" and r["stat"] == "mean"]
    assert {formats.format_general(v, formats.locale("en")) for v in means} <= said and all(abs(v - 1e9) > 5 for v in means)
    assert any(re.fullmatch(r"0\.3641\d{6}", x) for x in said)  # a variance keeps its ten decimals
    [subtitle] = [s for s in strings(model["header"]) if s.startswith("net amount")]
    assert subtitle.endswith("…") and glyphs.width_mm(subtitle, 9) <= 136
    titles = [spec["option"]["title"]["text"] for spec in chart_service[1] if "title" in spec["option"]]
    assert len(set(titles)) == 4 and all(glyphs.width_mm(t, 7.5, 700) <= 91 for t in titles)
    # a step whose name starts as an expression does is a name
    named = layouts.definition("t_two_sample", {"value": "=v", "group": "g"}, "en", None, None)
    assert [it["value"] for it in named["sections"]["page_header"]["items"] if it["id"] == "h_step"] == ['="=v by g"']
    assert [str(p) for p in rd.check(named, {"result": {c["name"]: c["type"] for c in layouts.columns()}})] == []


@pytest.mark.parametrize("locale", layouts.LOCALES)
def test_the_report_renders_in_the_five_languages(tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    for i, params in enumerate(({}, {"var_equal": True, "alternative": "greater"})):
        out, page = draw(tmp_path / str(i), chart_service, monkeypatch, locale=locale, sample=[{"fraction": 0.1}], **params)
        # («How this report was computed» fits the second page: docs/specs/report-explanations.md)
        assert out.pages == 2 and out.warnings == []  # no character the font lacks, no value wider than its card
        said = set(strings(json.loads(page)))
        words = layouts.texts(locale)
        assert layouts.test_name("t_two_sample", params, locale) in said
        assert {words["sections"][s] for s in words["sections"] if s not in ("comparisons", "comparisons_cells", "table", "strata")} <= said  # (no post-hoc, no table of counts)
        assert words["stats"]["test"]["df"] in said and words["tests"]["t_two_sample"]["stats"]["test"]["estimate"] in said
    svg = draw(tmp_path / "svg", chart_service, monkeypatch, fmt="svg", locale=locale)[0]
    assert len(json.loads(svg.path.read_text())["pages"]) == 2


def test_the_reference_report_matches_its_golden_pdf(tmp_path, chart_service, monkeypatch):  # noqa: F811
    a, _ = draw(tmp_path / "a", chart_service, monkeypatch)
    b, _ = draw(tmp_path / "b", chart_service, monkeypatch)
    first = a.path.read_bytes()
    assert first.startswith(b"%PDF-") and first == b.path.read_bytes()
    assert hashlib.sha256(first).hexdigest() == GOLDEN_PDF_SHA256


def test_excel_holds_the_tables_one_sheet_each(tmp_path, chart_service, monkeypatch):  # noqa: F811
    params = {"var_equal": True, "groups": ["c", "b"]}
    [pooled] = [r["value"] for r in rows_of(**params) if r["key"] == "test|||pooled_var"]
    out, _ = draw(tmp_path, chart_service, monkeypatch, fmt="xlsx", **params)
    with zipfile.ZipFile(out.path) as z:
        book = z.read("xl/workbook.xml").decode()
        cells = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/"))
    # the variances differ by less than four times: no note, so no sheet of notes
    # (and «Reading», the method's paragraphs: docs/specs/report-explanations.md)
    assert re.findall(r'<sheet name="([^"]+)"', book) == ["Numbers of the test", "Groups", "Intervals of the means", "Five-number summary",
                                                          "Reading", "Parameters"]
    # the numbers as numbers, whole (an engine's last digit may differ between two runs)
    [written] = re.findall(r"<t>Pooled variance \(sp²\)</t></is></c><c [^>]*><is><t>pooled_var</t></is></c><c [^>]*><v>([^<]+)</v>", cells)
    assert math.isclose(float(written), pooled, rel_tol=1e-12)


# ── the route and the task ───────────────────────────────────────────────────
@pytest.fixture
def stack(monkeypatch, chart_service, storage):  # noqa: F811
    """The engine's side of a request: the export routes, the task run in place, a Polars
    engine on the fake storage, Valkey as a dict. `reads`: the chains the engine was asked."""
    files, results, reads, copies = Storage(), {}, [], []
    monkeypatch.setattr(jobs.materialize_step_task, "apply_async", lambda args, **kwargs: copies.append((args, kwargs)))
    monkeypatch.setattr(jobs, "get_storage_service", lambda: files)
    monkeypatch.setattr(exports, "get_storage_service", lambda: files)
    monkeypatch.setattr(exports, "_client", FakeRedis())
    monkeypatch.setattr(rowcount, "_client", FakeRedis())
    monkeypatch.setattr(render.Limits, "from_settings", classmethod(lambda cls: cls(chart_url=chart_service[0], max_rows=10)))
    engine = _engine("polars", storage)
    preview = engine.preview

    def spy(source, operations, **kwargs):
        reads.append((operations, kwargs))
        return preview(source=source, operations=operations, **kwargs)

    monkeypatch.setattr(engine, "preview", spy)
    import app.engine as engines

    monkeypatch.setattr(engines, "get_engine", lambda name=None: engine)

    def send_task(name, kwargs, task_id):
        assert name == "app.tasks.report_jobs.render_stat_report_task"
        results[task_id] = Result(report_jobs.render_stat_report_task.apply(kwargs=kwargs, task_id=task_id).get())
        return results[task_id]

    def lookup(task_id, app=None):
        return results.get(task_id) or Result(None, state="PENDING")

    monkeypatch.setattr(report_routes.celery_app, "send_task", send_task)
    monkeypatch.setattr(tasks, "AsyncResult", lookup)
    monkeypatch.setattr(exports, "AsyncResult", lookup)
    upload_df(storage, abc(), "datasets/stats_abc.parquet")
    return files, reads, copies


def ask(operations=None, **body):
    chain = [{"type": "stat_t_two_sample", "params": WELCH}] if operations is None else operations
    return report_routes.stat_report(report_routes.StatReport(**{
        "bucket": BUCKET, "input_key": "datasets/stats_abc.parquet", "operations": chain, "engine": "polars", "owner": "u1", **body}))


def test_a_report_of_a_step_is_followed_and_taken_as_an_export(stack, monkeypatch):
    files, reads, _ = stack
    handed: list[str] = []
    run = pdf.run
    monkeypatch.setattr(pdf, "run", lambda job, *a, **k: (handed.append(job.get("inputs", {}).get("model", "")), run(job, *a, **k))[1])
    started = ask(format="svg", locale="en")
    assert started.filename == "Welch's t test — v by g.json" and started.status == "submitted"
    status = tasks.export_status(started.task_id, owner="u1")
    assert status == {"status": "ready", "filename": started.filename, "size": status["size"], "pages": 2}
    with pytest.raises(HTTPException) as e:
        tasks.export_file(started.task_id, owner="u2")
    assert e.value.status_code == 404
    headers, body = _play(tasks.export_file(started.task_id, owner="u1"))
    screen = json.loads(body)
    assert headers["content-type"] == "application/json" and len(screen["pages"]) == 2 and screen["pages"][0].startswith("<svg")
    assert files.keys("exports/") == []
    # the pages hold the Welch numbers: what the template was handed
    said = set(strings(json.loads(handed[0])))
    assert {"-4.6090", "12.7279", "0.0005 ***", "-1.4411", "-4.6090212217", "0.0005168703", "Computed in the engine: polars."} <= said
    # the chain was read once, as it came but for the rows of the report, with room for every row
    [(operations, kwargs)] = reads
    assert operations == [{"type": "stat_t_two_sample", "params": {**WELCH, "detail": "report"}}]
    assert kwargs == {"limit": layouts.MAX_REPORT_ROWS, "use_cache": True}


def test_the_pdf_and_the_excel_asked_after_the_pages_read_no_data(stack):
    files, reads, _ = stack
    first = ask(format="svg", locale="it")
    assert first.filename == "Test t di Welch — v per g.json" and len(reads) == 1
    pdf_ = ask(format="pdf", locale="it", filename="Canali a confronto", step="Canali")
    assert pdf_.filename == "Canali a confronto.pdf"
    headers, body = _play(tasks.export_file(pdf_.task_id, owner="u1"))
    assert headers["content-type"] == "application/pdf" and body.startswith(b"%PDF-")
    xlsx = ask(format="xlsx", locale="de")
    assert tasks.export_status(xlsx.task_id, owner="u1")["status"] == "ready" and xlsx.filename.endswith(".xlsx")
    assert len(reads) == 1  # another format, another language: the rows are remembered
    [key] = [k for k in rowcount._client.strings if k.startswith("dataprep:statreport:")]
    remembered = json.loads(rowcount._client.strings[key])["rows"]
    assert len(remembered) == 382 and [r[8] for r in remembered] == list(range(382))
    # other parameters are another test: read again
    ask(format="svg", operations=[{"type": "stat_t_two_sample", "params": {**WELCH, "alternative": "less"}}])
    assert len(reads) == 2
    # Valkey that holds something else there, or nothing, is rows not remembered: never an error
    rowcount._client.strings[key] = "not json"
    assert tasks.export_status(ask(format="svg").task_id, owner="u1")["status"] == "ready" and len(reads) == 3


def test_rows_too_heavy_are_not_remembered(stack, monkeypatch):
    _, reads, copies = stack
    monkeypatch.setattr(report_jobs, "STAT_ROWS_MAX_BYTES", 1000)
    ask(format="svg")
    ask(format="svg")
    assert len(reads) == 2 and not [k for k in getattr(rowcount._client, "strings", {}) if k.startswith("dataprep:statreport:")]


def test_the_report_of_a_sampled_chain_says_so_and_keeps_the_chain(stack, monkeypatch):
    _, reads, copies = stack
    handed: list[str] = []
    run = pdf.run
    monkeypatch.setattr(pdf, "run", lambda job, *a, **k: (handed.append(job.get("inputs", {}).get("model", "")), run(job, *a, **k))[1])
    sample = {"type": "limit", "params": {"n": 20, "_dev_sample": True}}
    started = ask(format="svg", operations=[sample, {"type": "stat_t_two_sample", "params": WELCH}])
    assert tasks.export_status(started.task_id, owner="u1")["status"] == "ready"
    assert reads[-1][0][0] == sample  # nothing added to the chain, nothing taken out of it
    # the step cache is on for what is before the test: its copy is queued for later, as after a preview
    assert copies == [(["polars", BUCKET, "datasets/stats_abc.parquet", [sample]], {"queue": "preview", "priority": 9})]
    model = json.loads(handed[-1])
    assert "Rows: a development sample of the source — the first 20 rows" in strings(model["header"])
    # the test ran on the sample's rows: the first 20 of the table are those of a, b and five of c
    assert "15" in strings(model) and "Rows used: 15. Rows left out: 0 (no group, or no value in one of the two groups)." in strings(model)
    ask(format="svg")
    assert "development sample" not in handed[-1]


def test_what_is_not_a_test_is_refused_before_the_queue_and_a_test_s_refusal_is_the_task_s(stack):
    _, reads, copies = stack
    for chain, said in (
        ([{"type": "filter", "params": {"column": "g", "operator": "eq", "value": "a"}}], "is not a statistical test (it is «filter»)"),
        ([{"type": "stat_t_two_sample", "params": WELCH}, {"type": "limit", "params": {"n": 5}}], "is not a statistical test (it is «limit»)"),
        ([{"type": "stat_no_such_test", "params": {}}], "is not a statistical test"),
    ):
        with pytest.raises(HTTPException) as e:
            ask(operations=chain)
        assert e.value.status_code == 422 and said in e.value.detail
    with pytest.raises(Exception):  # no chain at all, a format that is not a report's
        ask(operations=[])
    with pytest.raises(Exception):
        ask(format="html")
    assert reads == []
    # three groups and none chosen: the test's own sentence, as a failed export
    refused = ask(operations=[{"type": "stat_t_two_sample", "params": {"value": "v", "group": "g"}}])
    status = tasks.export_status(refused.task_id, owner="u1")
    assert status["status"] == "failed" and status["code"] == 400
    assert status["detail"] == ("stat_t_two_sample: the test compares two groups and the column «g» has 3 (a, b, c): "
                                "choose the two to compare in 'groups'")


def test_a_test_that_runs_out_of_time_says_what_to_do(stack, monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    def slow(*args, **kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(report_jobs, "stat_rows", slow)
    status = tasks.export_status(ask().task_id, owner="u1")
    assert status == {"status": "failed", "code": 504, "detail": (
        "The test took longer than the workers allow: give the source a development sample, or filter before the test")}


def test_a_minute_that_turns_during_a_report_changes_nothing(stack, monkeypatch):
    """The task gives the render no instant: the render reads the clock, and the page prints the
    time to the minute. The two renders tests/reports_both_ways.py compares are drawn at one
    instant, or a minute turning between them would make them differ (it did, now and then)."""
    from datetime import timedelta

    from app.reports import render as render_module

    class Ticking(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):  # a fifth of a second a reading, the minute turning between the first two
            cls.calls += 1
            return datetime(2026, 11, 19, 15, 30, 59, 700000, tzinfo=timezone.utc) + timedelta(seconds=0.2 * cls.calls)

    monkeypatch.setattr(render_module, "datetime", Ticking)
    assert tasks.export_status(ask(format="svg").task_id, owner="u1")["status"] == "ready"


def test_every_statistics_operation_has_its_report_route():
    """A test of the catalogue without a layout would be refused with a sentence, not an error."""
    assert all(operation_name(t).startswith("stat_") for t in layouts.LAYOUTS)
    assert "app.tasks.report_jobs.render_stat_report_task" in __import__("app.core.export_slots", fromlist=["TASKS"]).TASKS


# ── lot 2: every test's report ───────────────────────────────────────────────
@pytest.mark.parametrize("test", sorted(t for t in TESTS if t != "t_two_sample"))
@pytest.mark.parametrize("locale", layouts.LOCALES)
def test_every_report_renders_in_the_five_languages(tmp_path, chart_service, monkeypatch, test, locale):  # noqa: F811
    """Every variant of every test, in each language: no character the font lacks, no value
    wider than its place (the render would warn), the test's name in the header."""
    for i, params in enumerate(VARIANTS[test]):
        out, page = draw(tmp_path / str(i), chart_service, monkeypatch, locale=locale, test=test, sample=[{"first": 500}], **params)
        assert out.warnings == [], (params, out.warnings)
        said = set(strings(json.loads(page)))
        assert layouts.test_name(test, {**REFERENCE[test], **params}, locale) in said
        words = layouts.texts(locale)
        assert words["sections"]["computed"] in said and words["tests"][test]["left_out"].split("{")[0] in "".join(said)


# what each report says, in English, of the reference data: R's and SciPy's numbers
ON_THE_PAGE = {
    "t_one_sample": ({}, ["One-sample t test", "3.2226", "0.0146 *", "5.6875", "95% CI: 5.1830 to 6.1920", "mean(v) = 5", "mean(v) ≠ 5",
                          "t = (m − mu) / (s / √n), df = n − 1", "3.2225754675", "0.0146011836", "1.139352483",
                          "Rows used: 8. Rows left out: 1 (no number: NULL or NaN)."]),
    "t_paired": ({}, ["Paired t test", "-2.6434", "0.0268 *", "-2.5000", "95% CI: -4.6394 to -0.3606", "mean(before − after) = 0",
                      "-2.6434026632", "-0.8359173189", "2.9907264075", "before − after", "79", "81.5"]),
    "anova": ({}, ["One-way ANOVA", "11.9313", "2, 18", "0.0005 ***", "0.5700", "Distribution of the statistic under H0: F(2, 18).",
                   "11.9312926066", "7.790297619", "5.8763690476",
                   "Pairwise comparisons", "a – b", "a – c", "b – c", "6.8917673906", "0.000343101", "3.6093038287", "H0 rejected",
                   "H0 not rejected", "Critical region at α = 0.05: statistic ≥ 3.5546.", "Signif. codes: 0 ‘***’ 0.001 ‘**’ 0.01 ‘*’ 0.05 ‘.’ 0.1 ‘ ’ 1"]),
    "var_f": ({}, ["F test of two variances", "0.9956", "7, 6", "0.9793", "95% CI: 0.1748 to 5.0961", "var(v | a) / var(v | b) = 1"]),
    "levene": ({}, ["Brown–Forsythe test (Levene, median)", "0.2686", "2, 18", "0.7675", "3", "5.65", "var(v | a) = var(v | b) = var(v | c)"]),
    "pearson": ({}, ["Pearson's correlation", "17.0305", "< 0.0001 ***", "0.9865", "95% CI: 0.9419 to 0.9969", "cor(px, py) = 0",
                     "1.43496376e-7"]),
    "prop_one_sample": ({}, ["Test of one proportion (score)", "1.3703", "1.8778", "0.1706", "0.3083", "95% CI: 0.2290 to 0.4002",
                             "P(answer = yes) = 0.25", "0.1705869329", "Rows used: 120. Rows left out: 1 (no value: NULL or NaN)."]),
    "prop_two_sample": ({}, ["Test of two proportions", "-2.4215", "0.0155 *", "-0.1644", "95% CI: -0.2977 to -0.0311",
                             "P(answer = yes | a) − P(answer = yes | b) = 0", "0.3869565217"]),
    "chi2_independence": ({"posthoc": "cells"}, ["Chi-square test of independence", "8.8889", "0.0117 *", "0.2722", "Table of the counts",
                                                 "Comparisons of the cells", "x1 – m", "2.8284271247", "0.0280664099", "Critical region at α = 0.05: statistic ≥ 5.9915."]),
    "chi2_gof": ({}, ["Chi-square goodness-of-fit test", "4.3200", "0.2289", "P(cat = a) = 0.25, P(cat = b) = 0.25, ..., P(cat = d) = 0.25"]),
    "mcnemar": ({}, ["McNemar's test", "236", "16.8178", "< 0.0001 ***", "1.7442", "95% CI: 1.3292 to 2.3010", "Discordant",
                     "Concordant", "P(x = i, y = j) = P(x = j, y = i)", "118"]),
    "cmh": ({}, ["Cochran–Mantel–Haenszel test", "3.9286", "0.0475 *", "7.0000", "95% CI: 1.0267 to 47.7251", "Strata", "s4"]),
    "g_test": ({}, ["G test of independence", "9.0517", "0.0108 *", "Term of G", "P(x, y) = P(x)·P(y)"]),
    "mann_whitney": ({}, ["Mann–Whitney U test", "2.0000", "—", "0.0012 **", "-0.9286", "P(v | a > v | b) = 0.5", "0.0012432012", "38", "82",
                          "An exact test: its p-value counts every arrangement of the values, and it has no curve to draw."]),
    "wilcoxon_signed_rank": ({}, ["Wilcoxon signed-rank test", "4.0000", "0.0321 *", "-0.8222", "median(before − after) = 0", "before − after",
                                  "Critical region at α = 0.05: z ≤ -1.9600 or z ≥ 1.9600.", "-2.143768803"]),
    "kruskal_wallis": ({}, ["Kruskal–Wallis test", "11.5227", "0.0031 **", "0.5761", "F(v | a) = F(v | b) = F(v | c)", "a – b", "-10.875",
                            "3.2113081447", "0.0021239374", "H0 rejected", "H0 not rejected", "Pairwise comparisons"]),
    "spearman": ({}, ["Spearman's rank correlation", "cor(rank(px), rank(py)) = 0", "Rank correlation ρ"]),
    "ks_two_sample": ({}, ["Two-sample Kolmogorov–Smirnov test", "0.8750", "0.0025 **", "F(v | a) = F(v | b)", "0.875"]),
    "normality": ({}, ["D'Agostino–Pearson normality test", "14.4517", "1.4301", "0.0007 ***", "5.5156", "2.9157928616", "(skewness(nv), kurtosis(nv)) = (0, 3)"]),
    "fisher_exact": ({}, ["Fisher's exact test", "2 × 2", "5.4857142857", "0.0238 *", "5.2297", "95% CI: 1.1694 to 27.3059", "OR(x, y) = 1",
                          "Distribution of the statistic under H0: hypergeometric(19, 21, 17)."]),
}


@pytest.mark.parametrize("test", sorted(ON_THE_PAGE))
def test_each_report_holds_the_reference_numbers(tmp_path, chart_service, monkeypatch, test):  # noqa: F811
    params, wanted = ON_THE_PAGE[test]
    out, page = draw(tmp_path, chart_service, monkeypatch, test=test, **params)
    said = set(strings(json.loads(page)))
    missing = [w for w in wanted if w not in said]
    assert missing == [], (missing, sorted(s for s in said if any(c.isdigit() for c in s))[:200])


def test_the_comparisons_follow_the_method(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """Holm's correction has no simultaneous interval: no table nor chart of intervals; the
    others do; no post-hoc, no comparisons at all."""
    def items(**params):
        d = layouts.definition("anova", {**REFERENCE["anova"], **params}, "en")
        return {it["id"] for it in d["sections"]["body"]["items"]}

    assert {"pairs", "pairs_p", "pairs_ci", "c_pairs"} <= items()
    assert {"pairs", "pairs_p"} <= items(posthoc="pairwise_t") and not {"pairs_ci", "c_pairs"} & items(posthoc="pairwise_t")
    assert {"pairs_ci", "c_pairs"} <= items(posthoc="pairwise_t", p_adjust="sidak")
    assert not {"pairs", "pairs_p", "s_pairs"} & items(posthoc="none")
    del chart_service[1][:]
    draw(tmp_path, chart_service, monkeypatch, test="anova")
    charts = {spec["option"]["title"]["text"]: spec["option"] for spec in chart_service[1] if "title" in spec["option"]}
    pairs = charts["Differences of the means with their intervals"]
    assert pairs["xAxis"]["data"] == ["a – b", "a – c", "b – c"]
    assert [round(v, 6) for v in pairs["series"][0]["data"][0]["value"]] == [-2.195778, -1.441071, -1.441071, -1.441071, -0.686364]
    assert charts["Distribution of the statistic under H0"]["series"][0]["markLine"]["data"][0]["xAxis"] == pytest.approx(11.93129260658611)


# the shapes a test of categories takes on its data, not on its parameters
OTHER_SHAPES = {
    "fisher_exact": (counts_table(["m", "n"], ["x", "y", "z"], [[30, 20, 10], [15, 25, 20]]), {"x": "x", "y": "y"},
                     ["2 × 3", "0.0129 *", "0.0128672787", "Distribution of the statistic under H0: hypergeometric(2 × 3)."]),
    "mcnemar": (counts_table(["a", "b", "c"], ["a", "b", "c"], [[20, 10, 9], [5, 25, 12], [3, 4, 30]]), {"x": "x", "y": "y"},
                ["8.6667", "0.0341 *", "3", "Distribution of the statistic under H0: chi-square(3)."]),
    "cmh": (strata_table((3, 3, 2), [10, 6, 3, 4, 9, 7, 2, 5, 12, 8, 7, 2, 5, 10, 6, 3, 4, 11]), {"x": "x", "y": "y", "strata": "z"},
            ["23.8815", "< 0.0001 ***", "4", "2"]),
}


@pytest.mark.parametrize("test", sorted(OTHER_SHAPES))
@pytest.mark.parametrize("locale", ("en", "it"))
def test_a_larger_table_has_its_own_cards(tmp_path, chart_service, monkeypatch, test, locale):  # noqa: F811
    """Fisher's test of a 2 × 3 table, Bowker's of a 3 × 3, the generalized CMH: the cards and
    the tables of a 2 × 2 table are not drawn, those of the larger one are; R's numbers."""
    frame, params, wanted = OTHER_SHAPES[test]
    rows = [dict(zip(OUTPUT_NAMES, r)) for r in run_hypothesis_test(PolarsStatSource(frame.lazy()), test, {**params, "detail": "report"}, "polars")]
    handed: list[str] = []
    run = pdf.run
    monkeypatch.setattr(pdf, "run", lambda job, *a, **k: (handed.append(job.get("inputs", {}).get("model", "")), run(job, *a, **k))[1])
    out = render.render(layouts.definition(test, params, locale), {"result": {"rows": layouts.with_labels(test, rows, locale), "columns": layouts.columns()}},
                        {}, "pdf", tmp_path, title="t", now=NOW, limits=render.Limits(chart_url=chart_service[0], max_rows=layouts.MAX_REPORT_ROWS))
    assert out.warnings == []
    said = set(strings(json.loads(handed[0])))
    if locale == "en":
        assert [w for w in wanted if w not in said] == []
    assert not {"95% CI", "IC 95%"} & {s[:6] for s in said}  # no interval of an odds ratio there is not


def test_numbers_in_the_hypotheses_use_the_decimal_of_the_language():
    row = {"section": "test", "stat": "h1", "text": "max P(v | i > v | j) ≠ 0.5", "value": None}
    column = {"section": "test", "stat": "h0", "text": "mean(ab1.5) = 1.5e-07 and m₂^1.5", "value": None}
    assert layouts.with_labels("kruskal_wallis", [row], "en")[0]["text"] == "max P(v | i > v | j) ≠ 0.5"
    for loc in ("it", "de", "es", "fr"):
        assert layouts.with_labels("kruskal_wallis", [row], loc)[0]["text"] == "max P(v | i > v | j) ≠ 0,5"
    # a name that has a dot in it is not a number
    assert layouts.with_labels("t_one_sample", [column], "it")[0]["text"] == "mean(ab1.5) = 1,5e-07 and m₂^1,5"


def test_the_arguments_of_a_function_are_set_apart_by_a_semicolon_where_the_decimal_is_a_comma():
    """«min(0,5, |O − E|)» reads as three arguments: in the languages with a decimal comma the
    arguments inside brackets are set apart by «; », the sentence's own commas stay."""
    formula = {"section": "test", "stat": "formula", "text": "χ² = (|k − n·p0| − Y)², Y = min(0.5, |k − n·p0|), df = 1", "value": None}
    assert layouts.with_labels("prop_one_sample", [formula], "en")[0]["text"] == formula["text"]
    for loc in ("it", "de", "es", "fr"):
        assert layouts.with_labels("prop_one_sample", [formula], loc)[0]["text"] == \
            "χ² = (|k − n·p0| − Y)², Y = min(0,5; |k − n·p0|), df = 1"
    note = {"section": "test", "stat": "note_1", "text": "Ties (two values, or more) at 0.5", "value": None}
    assert layouts.with_labels("t_one_sample", [note], "it")[0]["text"] == "Ties (two values, or more) at 0,5"
    [said] = [r["text"] for r in layouts.with_labels("prop_one_sample", rows_of("prop_one_sample"), "it") if r["stat"] == "formula"]
    assert "min(0,5; |k − n·p0|)" in said


def test_a_p_value_below_the_smallest_double_is_said_so():
    """M3: a chi-square of 1500 on one degree of freedom has a tail below the smallest double:
    the rows hold 0 (the double nearest), the page and the Excel file «< 2.2e-308», never «0»."""
    from app.engine.stats import dist

    assert dist.chi2_sf(1500.0, 1.0) == 0.0
    row = {"section": "test", "stat": "p_value", "value": 0.0, "text": None, "level": None, "level_2": None}
    assert layouts.with_labels("chi2_gof", [row], "en")[0]["tiny"] == "< 2.2e-308"
    assert layouts.with_labels("chi2_gof", [row], "it")[0]["tiny"] == "< 2,2e-308"
    pair = {**row, "section": "pair", "stat": "p_adjusted"}
    assert layouts.with_labels("anova", [pair], "en")[0]["tiny"] == "< 2.2e-308"
    for other in ({**row, "value": 1e-300}, {**row, "stat": "estimate"}, {**row, "value": None}):
        assert layouts.with_labels("chi2_gof", [other], "en")[0]["tiny"] is None
    # on a page: 1500 rows of one category among two expected even: χ² = 1500, df = 1
    frame = pl.DataFrame({"cat": ["a"] * 1500 + ["b"] * 0 + ["a", "b"]})
    rows = [dict(zip(OUTPUT_NAMES, r)) for r in run_hypothesis_test(PolarsStatSource(frame.lazy()), "chi2_gof", {"value": "cat", "detail": "report"}, "polars")]
    assert [r["value"] for r in rows if r["key"] == "test|||p_value"] == [0.0]
    d = layouts.definition("chi2_gof", {"value": "cat"}, "en")
    [numbers] = [it for it in d["sections"]["body"]["items"] if it["id"] == "numbers"]
    assert numbers["columns"][2]["value"] == "IF(ISNULL([tiny]), [value], [tiny])" and numbers["columns"][2]["format"] == "General"


def _sheet(path, title: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
        return z.read(f"xl/worksheets/sheet{names.index(title) + 1}.xml").decode()


def test_the_tables_write_the_numbers_whole_and_excel_gets_numbers(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """M4 and the Excel file: a group's numbers are never rounded in the data (a variance of
    1e-12 is not 0), the tables write them in the General
    format (a very small one by its exponent); Excel gets numbers, a tiny p-value too, with
    the General format, not a text."""
    frame = pl.DataFrame({"g": ["a"] * 4 + ["b"] * 4,
                          "v": [1.0, 1.000001, 1.000002, 1.000003, 2.0, 2.0001, 2.0002, 2.0003]})
    params = {"value": "v", "group": "g"}
    rows = [dict(zip(OUTPUT_NAMES, r)) for r in run_hypothesis_test(PolarsStatSource(frame.lazy()), "t_two_sample", {**params, "detail": "report"}, "polars")]
    [var_a] = [r["value"] for r in rows if r["key"] == "group|a||var"]
    [mean_b] = [r["value"] for r in rows if r["key"] == "group|b||mean"]
    [p_value] = [r["value"] for r in rows if r["key"] == "test|||p_value"]
    assert 1e-13 < var_a < 1e-11 and p_value < 1e-6
    labelled = layouts.with_labels("t_two_sample", rows, "en")
    handed: list[str] = []
    run = pdf.run
    monkeypatch.setattr(pdf, "run", lambda job, *a, **k: (handed.append(job.get("inputs", {}).get("model", "")), run(job, *a, **k))[1])
    out = render.render(layouts.definition("t_two_sample", params, "en"), {"result": {"rows": labelled, "columns": layouts.columns()}}, {},
                        "pdf", tmp_path / "pdf", title="t", now=NOW, limits=render.Limits(chart_url=chart_service[0]))
    assert out.warnings == []
    said = set(strings(json.loads(handed[0])))
    from app.reports import formats

    en = formats.locale("en")
    assert {formats.format_general(var_a, en), formats.format_general(mean_b, en), formats.format_general(p_value, en)} <= said
    assert re.fullmatch(r"\d(\.\d+)?e-1[23]", formats.format_general(var_a, en)) and "2.00015" in said
    xlsx = render.render(layouts.definition("t_two_sample", params, "en"), {"result": {"rows": labelled, "columns": layouts.columns()}}, {},
                         "xlsx", tmp_path / "xlsx", title="t", now=NOW, limits=render.Limits(chart_url=chart_service[0]))
    numbers = _sheet(xlsx.path, "Numbers of the test")
    # the p-value a number of its own: <v>, not an inline string
    assert re.search(r"<t>p_value</t></is></c><c [^>]*><v>([^<]+)</v>", numbers)
    [written] = re.findall(r"<t>p_value</t></is></c><c [^>]*><v>([^<]+)</v>", numbers)
    assert math.isclose(float(written), p_value, rel_tol=1e-12)
    groups = _sheet(xlsx.path, "Groups")
    assert any(math.isclose(float(v), var_a, rel_tol=1e-9) for v in re.findall(r"<v>([^<]+)</v>", groups))
    assert any(float(v) == mean_b for v in re.findall(r"<v>([^<]+)</v>", groups))
    with zipfile.ZipFile(xlsx.path) as z:
        assert 'formatCode="General"' in z.read("xl/styles.xml").decode() or "numFmtId=\"0\"" in z.read("xl/styles.xml").decode()


def test_a_card_writes_a_tiny_number_by_its_exponent():
    """A card writes four decimals: a number they would write 0.0000 is written by its exponent."""
    from app.reports import formats

    expr = layouts._shown("[value]")
    assert "0.0000e+0" in expr and "0.0000e+0" in layouts._shown("[value]", layouts.P4)
    assert formats.display(1.23456e-7, layouts.P4_TINY, formats.locale("en")) == "1.2346e-7"
    assert formats.display(-4.2e-5, layouts.P4_TINY, formats.locale("it")) == "-4,2000e-5"


def test_a_chart_left_out_says_why():
    """The histogram past six groups, the intervals of the pairs past fifteen, the curve of
    McNemar's exact test: a line in their place says why they are not drawn."""
    def items(test, **params):
        return {it["id"]: it for it in layouts.definition(test, {**REFERENCE[test], **params}, "en")["sections"]["body"]["items"]}

    anova = items("anova")
    assert anova["c_hist_note"]["visible"].endswith("> 6") and anova["c_hist"]["visible"].endswith("<= 6")
    assert "6" in anova["c_hist_note"]["value"] and anova["c_pairs_note"]["visible"].endswith("> 15")
    assert "c_hist_note" in items("kruskal_wallis")
    mcnemar = items("mcnemar", method="exact")
    assert mcnemar["c_curve_exact"]["visible"] == '=COUNTIF([section] = "curve") = 0'


def test_a_legend_names_only_the_symbols_of_its_variant():
    """Welch's t has no sp², the classic ANOVA no w nor Λ, the generalized CMH no a, b, c, d:
    the legend is chosen by the variant row."""
    words = layouts.texts("en")["tests"]
    assert "sp²" not in words["t_two_sample"]["symbols"]["welch"] and "sp²" in words["t_two_sample"]["symbols"]["student"]
    assert "Λ" not in words["anova"]["symbols"]["one_way"] and "Λ" in words["anova"]["symbols"]["welch"]
    assert "a, b" not in words["cmh"]["symbols"]["generalized"] and "V^(−1)" in words["cmh"]["symbols"]["generalized"]
    d = layouts.definition("t_two_sample", WELCH, "en")
    [legend] = [it["value"] for it in d["sections"]["body"]["items"] if it["id"] == "symbols"]
    assert legend.startswith('=IF(LOOKUP("test|||variant", [key], [text]) = "student"')


def test_the_generalized_cmh_can_be_redone_from_its_rows(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """M1: the margins of each stratum give m and V; n − m and V are rows too, and
    (n − m)'·V^(−1)·(n − m) is the statistic. Redone here from the rows alone (R's
    mantelhaen.test gives 23.8815 on this table)."""
    frame, params, _ = OTHER_SHAPES["cmh"]
    rows = [dict(zip(OUTPUT_NAMES, r)) for r in run_hypothesis_test(PolarsStatSource(frame.lazy()), "cmh", {**params, "detail": "report"}, "polars")]
    by = {r["key"]: r["value"] for r in rows}
    strata = sorted({r["level"] for r in rows if r["section"] == "stratum"})
    xs = sorted({r["level_2"] for r in rows if r["stat"] == "row_total"})
    ys = sorted({r["level_2"] for r in rows if r["stat"] == "col_total"})
    r_, c_ = len(xs), len(ys)
    size = (r_ - 1) * (c_ - 1)
    dev, V = [0.0] * size, [[0.0] * size for _ in range(size)]
    for s_ in strata:
        R = [by[f"stratum|{s_}|{a}|row_total"] for a in xs]
        C = [by[f"stratum|{s_}|{b}|col_total"] for b in ys]
        N = sum(R)
        for j in range(c_ - 1):
            for i in range(r_ - 1):
                k = j * (r_ - 1) + i
                for jj in range(c_ - 1):
                    for ii in range(r_ - 1):
                        V[k][jj * (r_ - 1) + ii] += ((N * C[j] if j == jj else 0) - C[j] * C[jj]) * \
                            ((N * R[i] if i == ii else 0) - R[i] * R[ii]) / (N * N * (N - 1))
    for j, b in enumerate(ys[:-1]):
        for i, a in enumerate(xs[:-1]):
            k = j * (r_ - 1) + i
            m = sum(by[f"stratum|{s_}|{a}|row_total"] * by[f"stratum|{s_}|{b}|col_total"]
                    / sum(by[f"stratum|{s_}|{x}|row_total"] for x in xs) for s_ in strata)
            assert by[f"cell|{a}|{b}|index"] == k + 1
            assert math.isclose(by[f"cell|{a}|{b}|expected"], m, rel_tol=1e-12)
            dev[k] = by[f"cell|{a}|{b}|observed"] - m
            assert math.isclose(by[f"cell|{a}|{b}|deviation"], dev[k], rel_tol=1e-9, abs_tol=1e-12)
    for k1 in range(size):
        for k2 in range(size):
            assert math.isclose(by[f"covariance|{k1 + 1}|{k2 + 1}|v"], V[k1][k2], rel_tol=1e-12, abs_tol=1e-15)
    # (n − m)'·V^(−1)·(n − m), by Gaussian elimination
    a = [row[:] + [d] for row, d in zip(V, dev)]
    for col in range(size):
        piv = max(range(col, size), key=lambda q: abs(a[q][col]))
        a[col], a[piv] = a[piv], a[col]
        for q in range(col + 1, size):
            f = a[q][col] / a[col][col]
            for k in range(col, size + 1):
                a[q][k] -= f * a[col][k]
    x = [0.0] * size
    for q in range(size - 1, -1, -1):
        x[q] = (a[q][size] - sum(a[q][k] * x[k] for k in range(q + 1, size))) / a[q][q]
    assert math.isclose(sum(d * v for d, v in zip(dev, x)), by["test|||statistic"], rel_tol=1e-10)
    assert math.isclose(by["test|||statistic"], 23.8815, rel_tol=1e-5)
    # the page: its own legend and tables, the words of each language
    handed: list[str] = []
    run = pdf.run
    monkeypatch.setattr(pdf, "run", lambda job, *a, **k: (handed.append(job.get("inputs", {}).get("model", "")), run(job, *a, **k))[1])
    out = render.render(layouts.definition("cmh", params, "en"), {"result": {"rows": layouts.with_labels("cmh", rows, "en"), "columns": layouts.columns()}},
                        {}, "pdf", tmp_path, title="t", now=NOW, limits=render.Limits(chart_url=chart_service[0], max_rows=layouts.MAX_REPORT_ROWS))
    assert out.warnings == []
    said = set(strings(json.loads(handed[0])))
    words = layouts.texts("en")["tests"]["cmh"]
    assert {"Rows (R_i)", "Rows (C_j)", "V(k, k')", "n − m", "Index (k)", "Expected (m)"} <= said
    assert {words["tables"]["margins_x"], words["tables"]["margins_y"], words["tables"]["covariance"]} <= {
        it["title"] for it in layouts.definition("cmh", params, "en")["sections"]["body"]["items"] if it["type"] == "table"}
    assert any(x.startswith("x = «x», y = «y», strata of «z». R_i, C_j") for x in said)
    assert not any("a, b the first row" in x for x in said)


def test_ks_and_dagostino_write_what_their_statistic_is_made_of():
    """M2: the point where each distance of Kolmogorov–Smirnov is reached (found by the
    engine, with F1 and F2 there), and every number on the way of D'Agostino's K²."""
    rows = {r["key"]: r["value"] for r in rows_of("ks_two_sample")}
    a, b = sorted(A), sorted(B)

    def F(sample, v):
        return sum(x <= v for x in sample) / len(sample)

    points = sorted(set(a + b))
    d_plus = max(F(a, v) - F(b, v) for v in points)
    d_minus = max(F(b, v) - F(a, v) for v in points)
    v_plus = min(v for v in points if math.isclose(F(a, v) - F(b, v), d_plus))
    v_minus = min(v for v in points if math.isclose(F(b, v) - F(a, v), d_minus))
    assert rows["test|||v_plus"] == v_plus and rows["test|||v_minus"] == v_minus
    assert math.isclose(rows["test|||f1_minus"], F(a, v_minus)) and math.isclose(rows["test|||f2_minus"], F(b, v_minus))
    assert math.isclose(abs(rows["test|||f1_d"] - rows["test|||f2_d"]), rows["test|||d"])
    assert rows["test|||v_d"] == (v_plus if d_plus >= d_minus else v_minus)
    dag = {r["key"]: r["value"] for r in rows_of("normality")}
    n, g1, b2 = dag["test|||n"], dag["test|||skewness"], dag["test|||kurtosis"]
    y = g1 * math.sqrt((n + 1) * (n + 3) / (6 * (n - 2)))
    assert math.isclose(dag["test|||dag_y"], y, rel_tol=1e-12)
    w2 = dag["test|||dag_w2"]
    assert math.isclose(w2, math.sqrt(2 * (dag["test|||dag_beta2"] - 1)) - 1, rel_tol=1e-12)
    assert math.isclose(dag["test|||dag_delta"], 1 / math.sqrt(math.log(w2) / 2), rel_tol=1e-12)
    assert math.isclose(dag["test|||dag_alpha"], math.sqrt(2 / (w2 - 1)), rel_tol=1e-12)
    assert math.isclose(dag["test|||z_skewness"], dag["test|||dag_delta"] * math.asinh(y / dag["test|||dag_alpha"]), rel_tol=1e-12)
    x = (b2 - dag["test|||dag_mean_b2"]) / math.sqrt(dag["test|||dag_var_b2"])
    assert math.isclose(dag["test|||dag_x"], x, rel_tol=1e-12)
    A_ = dag["test|||dag_a"]
    z2 = (1 - 2 / (9 * A_) - ((1 - 2 / A_) / (1 + x * math.sqrt(2 / (A_ - 4)))) ** (1 / 3)) / math.sqrt(2 / (9 * A_))
    assert math.isclose(dag["test|||z_kurtosis"], z2, rel_tol=1e-10)
    assert math.isclose(A_, 6 + 8 / dag["test|||dag_sqrt_beta1"] * (2 / dag["test|||dag_sqrt_beta1"]
                                                                  + math.sqrt(1 + 4 / dag["test|||dag_sqrt_beta1"] ** 2)), rel_tol=1e-12)


def test_a_label_with_a_number_is_turned_never_wrapped():
    from app.reports.charts import _x_axis

    bins = [f"[{i},48 – {i + 1},47)" for i in range(6)]
    assert _x_axis(bins, 280)["axisLabel"]["rotate"] == 40  # it would end a line inside a number
    assert "rotate" not in _x_axis(["North", "South", "East"], 280)["axisLabel"]
    assert "rotate" not in _x_axis(["1", "2", "3"], 280)["axisLabel"]  # it fits


def test_the_words_that_introduce_a_table_are_kept_with_it_and_nothing_else():
    """The heading of a section and the words under it (the method, the formula of the comparisons) ask to be kept with
    the table that follows (`keep_with_next`): the report engine makes them one sticky run of bands, so a page never ends
    with the title and the table on the next one, nor the title alone and its formula on the next. Nothing else asks:
    the words under a table, the cards, the charts and their captions stay where they are."""
    from tests.test_reports_model import build

    asked = {**REFERENCE["chi2_independence"], "posthoc": "cells"}
    d = layouts.definition("chi2_independence", asked, "en", None, None)
    items = {i["id"]: i for i in d["sections"]["body"]["items"]}
    for id_ in ("s_pairs", "s_pairs_rule", "pairs_method", "s_pairs_keep", "pairs_formula",  # the comparisons
                "s_computed", "formula", "symbols", "distribution", "critical",  # how it is computed, above its numbers
                "s_groups", "s_groups_rule"):  # a section's heading right above its first table
        assert items[id_].get("keep_with_next"), id_
    for id_ in ("pairs_signif_legend", "signif_legend", "decision", "s_result", "s_charts", "c_observed"):
        assert not items[id_].get("keep_with_next"), id_
    assert not any(i.get("keep_with_next") for i in items.values() if i["type"] not in ("text", "line", "rect"))
    rows = layouts.with_labels("chi2_independence", rows_of("chi2_independence", posthoc="cells"), "en")
    body = build(d, rows=rows).doc["body"]
    at = next(i for i, b in enumerate(body) if b["k"] == "table" and b.get("id") == "pairs_p")
    assert [b.get("sticky") for b in body[at - 2:at]] == [True, True]  # the heading with the method, then the formula
    assert not body[at - 3].get("sticky")


def test_a_model_report_keeps_its_headings_and_the_coefficients_note_with_their_tables():
    """The model reports write with the same page (stats.Page): the Fit and Notes headings ask to be kept with their
    tables, and the note under the coefficients' charts (parametric survival) with the coefficients' tables, not with
    the charts above it; the charts and their captions never ask."""
    from pathlib import Path

    from app.engine.models import layouts as model_layouts

    fixture = json.loads((Path(__file__).parent / "fixtures" / "models" / "parametric_survival-ph.json").read_text())
    d = model_layouts.definition(fixture["model"], fixture["params"], "en", None, None)
    items = {i["id"]: i for i in d["sections"]["body"]["items"]}
    for id_ in ("s_fit", "s_fit_rule", "s_notes", "s_notes_rule", "notes_lines", "coef_note"):
        assert items[id_].get("keep_with_next"), id_
    assert not any(i.get("keep_with_next") for i in items.values() if i["type"] not in ("text", "line", "rect"))
    assert not any(i.get("keep_with_next") for id_, i in items.items() if id_.endswith(("_how", "_caption")))
