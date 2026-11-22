"""The explanation of the method in the report of a hypothesis test (docs/specs/report-explanations.md, lot B1): the
sentences the layout chooses from the test's rows (`stats.layouts.readings`), their words in five languages, where
the page places them. The criteria are the spec's: five texts per id with the same names and number forms, no word
of judgement in a measure, a verdict only from the test's own decision (its p-value against α), no «= <»."""
from __future__ import annotations

import json
import re

import pytest

from app.engine.models.layouts import common
from app.engine.stats import layouts
from app.engine.stats.catalog import TESTS
from app.reports import formats, glyphs
from tests.test_stats_layouts import OTHER_SHAPES, REFERENCE, VARIANTS, draw, rows_of, strings
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

LOCALES = layouts.LOCALES
OWN = ("reading", "reading_blocks", "reading_labels", "reading_texts", "measure_readings", "verdict_words")


def _names(template: str) -> list[tuple[str, str]]:
    return sorted((n, f if f in common.NUMBER_FORMS else "") for n, f in re.findall(r"\{(\w+)(?::(\w+))?\}", template))


def _has(word: str, sentence: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence, re.I))


def _shapes() -> list[tuple[str, list[dict]]]:
    """Every variant of every test on the reference data, and the larger tables of OTHER_SHAPES."""
    from app.engine.stats.compute import run_hypothesis_test
    from app.engine.stats.output import OUTPUT_NAMES
    from app.engine.stats.polars_source import PolarsStatSource

    out = [(test, rows_of(test, **params)) for test in sorted(VARIANTS) for params in VARIANTS[test]]
    for test, (frame, params, _) in OTHER_SHAPES.items():
        rows = run_hypothesis_test(PolarsStatSource(frame.lazy()), test, {**params, "detail": "report"}, "polars")
        out.append((test, [dict(zip(OUTPUT_NAMES, r)) for r in rows]))
    return out


SHAPES = _shapes()


def test_every_template_has_its_five_texts_with_the_same_names_and_forms():
    english = layouts.texts("en")
    for code in LOCALES:
        w = layouts.texts(code)
        assert all(k in w for k in OWN) and w["sections"]["how"], code
        for kind in ("reading_texts", "reading_labels"):
            assert list(w[kind]) == list(english[kind]), (code, kind)
        for key, text in english["reading_texts"].items():
            assert _names(text) == _names(w["reading_texts"][key]), (code, key)
            assert not re.search(r"=\s*\{\w+:p\}", w["reading_texts"][key]), (code, key)
        assert glyphs.missing(set("".join(strings({k: w[k] for k in OWN})))) == set(), code
    # every method paragraph has its label; the labels name nothing else
    assert {k for k in english["reading_texts"] if k.startswith("method_")} == set(english["reading_labels"])


def test_every_template_is_chosen_by_some_test_and_every_chosen_one_exists():
    chosen = {id_ for test, rows in SHAPES for _, _, id_, _ in layouts.readings(test, rows)}
    texts = set(layouts.texts("en")["reading_texts"])
    assert chosen <= texts, sorted(chosen - texts)
    # (the ones the reference data do not reach: a «keep» of a test that rejects on them, the cells' and pairs'
    # lines of a post-hoc not in VARIANTS, more than five pairs)
    unreached = texts - chosen
    assert all(k.endswith(("_keep", "_reject", "_more", "_none", "_some")) or k.startswith("method_posthoc_") for k in unreached), \
        sorted(unreached)


@pytest.mark.parametrize("locale", LOCALES)
def test_no_sentence_of_a_measure_holds_a_word_of_judgement(locale):
    """The owner's rule: an estimate, an interval, an effect size are measures — the number and what it measures,
    never «significant», «large», «good». And the words catch a verdict: every rejection says one."""
    w = layouts.texts(locale)
    assert w["measure_readings"] == layouts.texts("en")["measure_readings"] and w["verdict_words"]
    measures = set(w["measure_readings"])
    assert measures == {k for k in w["reading_texts"] if "_estimate" in k or k.startswith("effect_")}
    for key in measures:
        sentence = w["reading_texts"][key]
        assert not [v for v in w["verdict_words"] if _has(v, sentence)], (locale, key, sentence)
    for key, sentence in w["reading_texts"].items():
        if key.endswith("_reject") or key in ("pairs_some", "pairs_some_more", "cells_some", "cells_some_more"):
            assert any(_has(v, sentence) for v in w["verdict_words"]), (locale, key)


@pytest.mark.parametrize("locale", LOCALES)
def test_every_reading_of_every_test_is_a_sentence_of_the_page_s_language(locale):
    for test, rows in SHAPES:
        said = [r for r in layouts.with_labels(test, rows, locale) if r["section"] == "reading"]
        blocks = {r["level"] for r in said}
        assert {"result", "method"} <= blocks and len(said) <= layouts.READING_ROWS, (test, blocks)
        for r in said:
            assert not re.search(r"@|\{|%2|=\s*[<>]", r["text"]), (test, r["text"])
            assert r["label"] not in ("", r["stat"]) and r["key"] == f'reading|{r["level"]}||{r["stat"]}', (test, r)


def _ids(test: str, rows: list[dict]) -> list[str]:
    return [id_ for _, _, id_, _ in layouts.readings(test, rows)]


def test_the_verdict_is_the_test_s_own_decision_against_alpha():
    """The same data read at an α just above and just below the p-value: rejected, then not rejected."""
    for test in ("t_two_sample", "chi2_independence", "kruskal_wallis", "normality"):
        [p] = [r["value"] for r in rows_of(test) if r["key"] == "test|||p_value"]
        above, below = rows_of(test, alpha=min(0.5, p * 1.5)), rows_of(test, alpha=p * 0.5)
        assert f"{test}_reject" in _ids(test, above) and f"{test}_keep" not in _ids(test, above)
        assert f"{test}_keep" in _ids(test, below) and f"{test}_reject" not in _ids(test, below)


def test_a_one_sided_test_names_its_h1_and_drops_the_two_sided_interval():
    rows = rows_of("t_two_sample", alternative="less")
    lines = {id_: values for _, _, id_, values in layouts.readings("t_two_sample", rows)}
    assert "one_sided_reject" in lines and "t_two_sample_estimate_one_sided" in lines and "method_one_sided" in lines
    said = {r["key"]: r["text"] for r in layouts.with_labels("t_two_sample", rows, "it") if r["section"] == "reading"}
    assert "mean(v | a) − mean(v | b) < 0" in said["reading|result||line_2"] and "rifiutata a favore di H1" in said["reading|result||line_2"]


def test_the_numbers_are_the_rows_in_the_language_s_forms():
    said = {r["key"]: r["text"] for r in layouts.with_labels("t_two_sample", rows_of(), "it") if r["section"] == "reading"}
    assert said["reading|result||line_1"] == ("La media è 5,688 in «a» e 7,129 in «b»: una differenza di −1,441, il cui intervallo di "
                                               "confidenza al 95 % va da −2,118 a −0,7641.")
    assert "probabilità 0,0005, sotto α = 0,05" in said["reading|result||line_2"]
    tiny = {r["key"]: r["text"] for r in layouts.with_labels("pearson", rows_of("pearson"), "de") if r["section"] == "reading"}
    assert "Wahrscheinlichkeit < 0,0001 auf, unter α = 0,05" in tiny["reading|result||line_2"]


def test_the_comparisons_count_and_name_what_their_own_decisions_reject():
    rows = rows_of("anova")
    [line] = [(id_, v) for b, _, id_, v in layouts.readings("anova", rows) if b == "pairs"]
    rejected = sorted(f'{r["level"]} – {r["level_2"]}' for r in rows if r["section"] == "pair" and r["stat"] == "decision" and r["text"] == "reject_h0")
    assert line == ("pairs_some", {"k": len(rejected), "total": 3, "pairs": rejected, "alpha": 0.05})
    # more than five: five named, «and k more»
    many = [{**r, "level": f'g{i}', "ord": 10_000 + i} for i, r in enumerate(r for r in rows if r["section"] == "pair" and r["stat"] == "decision")]
    many = [*rows, *({**m, "text": "reject_h0", "level_2": f"h{j}", "ord": m["ord"] * 10 + j} for m in many for j in range(3))]
    [(id_, values)] = [(id_, v) for b, _, id_, v in layouts.readings("anova", many) if b == "pairs"]
    assert id_ == "pairs_some_more" and len(values["pairs"]) == layouts.MAX_NAMED and values["more"] == values["k"] - layouts.MAX_NAMED
    cells = rows_of("chi2_independence", posthoc="cells")
    assert [id_ for b, _, id_, _ in layouts.readings("chi2_independence", cells) if b == "pairs"][0].startswith("cells_")


def test_a_name_that_looks_like_a_template_stays_as_it_is():
    rows = [{**r, "level": "{p:p} 2.5" if r["level"] == "a" else r["level"]} for r in rows_of()]
    rows = [{**r, "key": r["key"].replace("|a|", "|{p:p} 2.5|")} for r in rows]
    said = [r["text"] for r in layouts.with_labels("t_two_sample", rows, "en") if r["section"] == "reading"]
    assert any("«{p:p} 2.5»" in s for s in said), said


def test_the_page_places_the_reading_under_the_decision_and_the_method_before_the_notes(tmp_path, chart_service, monkeypatch):  # noqa: F811
    d = layouts.definition("anova", {**REFERENCE["anova"]}, "fr")
    ids = [it["id"] for it in d["sections"]["body"]["items"]]
    assert ids.index("decision") < ids.index("reading_result") < ids.index("s_hypotheses")
    assert ids.index("pairs_signif_legend") < ids.index("reading_pairs") < ids.index("s_charts")
    assert ids.index("s_charts") < ids.index("s_how") < ids.index("reading") < ids.index("s_notes")
    out, page = draw(tmp_path, chart_service, monkeypatch, locale="fr", test="anova")
    said = set(strings(json.loads(page)))
    assert out.warnings == [] and layouts.texts("fr")["sections"]["how"] in said
    assert any(s.startswith("Lecture : ") and "Si les 3 groupes avaient la même moyenne" in s for s in said)
    for test in TESTS:  # every test's page has its places
        body = [it["id"] for it in layouts.definition(test, REFERENCE[test], "en")["sections"]["body"]["items"]]
        assert {"reading_result", "s_how", "reading"} <= set(body), test


def test_no_header_of_the_reading_table_runs_into_the_next_column():
    from app.reports.model import CELL_PAD

    for locale in LOCALES:
        [table] = [it for it in layouts.definition("t_two_sample", REFERENCE["t_two_sample"], locale)["sections"]["body"]["items"]
                   if it["id"] == "reading"]
        left = table["w"] - sum(c.get("width") or 0 for c in table["columns"])
        for c in table["columns"]:
            room = (c.get("width") or left) - 2 * CELL_PAD + 0.2
            assert not [w for w in c["header"].split() if glyphs.width_mm(w, 8, 700) > room], (locale, c["header"])
        # and every label of the column «Part» (semibold, the theme's size): no word runs into the explanation
        part = table["columns"][0]
        room = part["width"] - 2 * CELL_PAD + 0.2
        labels = [*layouts.texts(locale)["reading_labels"].values(), *layouts.texts(locale)["reading_blocks"].values()]
        assert not [w for label in labels for w in label.split() if glyphs.width_mm(w, 9, 600) > room], locale


def test_the_rows_a_report_reads_leave_room_for_the_explanation():
    assert max(len([r for r in layouts.with_labels(t, rows, "en") if r["section"] == "reading"]) for t, rows in SHAPES) <= layouts.READING_ROWS
    assert common.number_form(0.05, "num", formats.locale("en")) == "0.05"
