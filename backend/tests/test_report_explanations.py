"""The explanation of the method in every report (docs/specs/report-explanations.md): the pieces every model's layout
shares (`common.py`: `READINGS`, `reading_rows`, `page(explained=…)`, `reading_under`, `how_computed`) and the criteria
each lot's explanation must meet. The criteria run over every model whose file registers its readings in
`common.READINGS`, so a lot that explains a model is checked by adding it there, without touching this file."""
from __future__ import annotations

import copy
import re

import pytest

from app.engine.models import catalog, layouts
from app.engine.models.layouts import common
from app.engine.stats.layouts import LOCALES
from app.reports import definition as rd
from app.reports import formats, glyphs
from tests.test_models_layouts import CASES, fixture, strings

TEMPLATES = ("reading_texts", "formula_texts")
WORDS_FILES = sorted(p.stem for p in common.TEXTS.glob("*.json"))
# the items the explanation adds to a page: the readings under the blocks, «How this report was computed»
EXPLANATION_IDS = re.compile(r"^(reading(_.+)?|s_how(_rule|_keep)?|how_lead)$")
PAGE = common.page  # the page as `common.py` draws it, before a test wraps it


def explained() -> list[str]:
    """The models whose layout chooses its readings from the rows (the A/B test and the decomposition, whose runners
    write theirs, have their own tests in test_models_layouts.py)."""
    return sorted(common.READINGS)


def explained_cases() -> list[str]:
    return [c for c in CASES if c.split("-")[0] in common.READINGS]


def _sentences(text) -> list[str]:
    """A template's sentences: one, or one for each setting it depends on (`by_params`)."""
    return [text] if isinstance(text, str) else list(text.values())


def _names(template: str) -> list[tuple[str, str]]:
    """The names of a template with their number forms (a language's grammatical form, «{unit:dative}», is its own)."""
    return sorted((n, f if f in common.NUMBER_FORMS else "") for n, f in re.findall(r"\{(\w+)(?::(\w+))?\}", template))


def _has(word: str, sentence: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence, re.I))


# ── the shared pieces ───────────────────────────────────────────────────────
def test_a_line_s_values_are_written_as_the_runners_write_them_and_said_back_once():
    assert common.encoded(12.0) == "12" and common.encoded(0.05) == "0.05" and common.encoded(1e-300) == "1e-300"
    assert common.encoded(True) == "true" and common.encoded("North Side, 5%") == "North%20Side%2C%205%25"
    assert common.encoded(["a,b", 0.5]) == "a%2Cb,0.5"
    rows = [{"ord": 0}, {"ord": 7}]
    out = common.reading_rows([("result", None, "x", {"p": 0.0051, "group": "a b"}), ("result", None, "y", {"n": None}),
                               ("pairs", "a – b", "z", {})], rows)
    assert [(r["key"], r["ord"], r["text"]) for r in out] == [
        ("reading|result||line_1", 8, "@x p=0.0051 group=a%20b"), ("reading|result||line_2", 9, "@y"),
        ("reading|pairs|a – b|line_1", 10, "@z")]
    w = {"reading_texts": {"x": "p = {p:p} in «{group}»"}, "reading_labels": {}, "reading_blocks": {"result": "Result"}}
    for locale, said in (("en", "p = 0.0051 in «a b»"), ("it", "p = 0,0051 in «a b»")):
        assert common.reading_said(w, "result", out[0]["text"], formats.locale(locale), {}, "line_1") == (said, "Result")


def test_a_page_with_nothing_explained_is_the_page_as_before_and_an_unknown_place_is_refused(monkeypatch):
    """Every model now explains its method: the linear regression stands for one that does not, its readings taken out
    and its page asked for no reading place."""
    model, params, _ = fixture("linear-unpenalized")
    monkeypatch.delitem(common.READINGS, model)
    monkeypatch.setattr(common, "page", lambda *a, **k: PAGE(*a, **{**k, "explained": ()}))
    assert not [it for it in layouts.definition(model, params, "en")["sections"]["body"]["items"] if EXPLANATION_IDS.match(it["id"])]
    with pytest.raises(ValueError, match="no reading place"):
        PAGE(model, params, "en", None, None, name="x", about="", cards=(), explained=("charts",))


def test_a_model_that_registers_its_readings_gets_their_rows_and_their_places(monkeypatch):
    """A toy explanation of the linear regression: the rows `READINGS` chooses are said in the page's words, and
    `page(explained=…)` places them under its blocks and on the last page, before the notes."""
    model, params, rows = fixture("linear-unpenalized")
    words = common.words

    def with_texts(m, variant, locale):
        w = copy.deepcopy(words(m, variant, locale))
        w["reading_texts"] = {"fit_r2": "R² is {r2:num}.", "method_ols": "Least squares."}
        w["reading_blocks"] = {"fit": "Fit", "method": "Method"}
        w["reading_labels"] = {}  # the model's own labels (`method_ols`) would name the toy's rows
        return w

    monkeypatch.setattr(common, "words", with_texts)
    monkeypatch.setitem(common.READINGS, model, lambda rows_, checked: [
        ("fit", None, "fit_r2", {"r2": next(r["value"] for r in rows_ if r["key"] == "model|||r_squared")}),
        ("method", None, "method_ols", {})])
    said = {r["key"]: r for r in layouts.with_labels(model, params, rows, "it") if r["section"] == "reading"}
    assert said["reading|fit||line_1"]["text"].startswith("R² is 0,") and said["reading|method||line_1"]["label"] == "Method"
    monkeypatch.setattr(common, "page", lambda *a, **k: PAGE(*a, **{**k, "explained": ("fit",)}))
    body = [it["id"] for it in layouts.definition(model, params, "en")["sections"]["body"]["items"]]
    assert body.index("fit") < body.index("reading_fit") < body.index("s_how") < body.index("reading") < body.index("s_notes")
    assert "reading_summary" not in body
    only_additions("linear-unpenalized", monkeypatch)  # and the test of «only additions» sees this one


# ── the criteria of every lot (the spec's «Criteria») ───────────────────────
@pytest.mark.parametrize("name", WORDS_FILES)
def test_every_template_has_its_five_texts_with_the_same_names_and_forms(name):
    words = common._file(name)
    for kind in TEMPLATES:
        english = words["en"].get(kind, {})
        for code in LOCALES:
            other = words[code].get(kind, {})
            assert set(other) == set(english), (name, kind, code, sorted(set(other) ^ set(english)))
            for key, text in english.items():
                assert isinstance(text, str) == isinstance(other[key], str) and (isinstance(text, str) or set(text) == set(other[key]))
                pairs = ((text, other[key]),) if isinstance(text, str) else ((text[k], other[key][k]) for k in text)
                for one, two in pairs:
                    assert _names(one) == _names(two), (name, code, key)
                    # a p-value's form may say «< 0.0001»: never after «=» («p = < 0.0001»)
                    assert not re.search(r"=\s*\{\w+:p\}", two), (name, code, key)


@pytest.mark.parametrize("locale", LOCALES)
def test_no_sentence_of_a_measure_holds_a_word_of_judgement_in_any_explained_model(locale):
    """The owner's rule: a verdict only from a test with a p-value against alpha; a measure is a number without a word
    of judgement. Every explained model declares its measures' sentences (`measure_readings`); none holds a word of
    the language's `verdict_words` (common.json's, or the model's own)."""
    for model in explained():
        w = common.words(model, catalog.variant(model, catalog.check_params(model, fixture(next(
            c for c in CASES if c.split("-")[0] == model))[1])), locale)
        assert w.get("verdict_words") and "measure_readings" in w, model
        assert set(w["measure_readings"]) <= set(w["reading_texts"]), (model, sorted(set(w["measure_readings"]) - set(w["reading_texts"])))
        for key in w["measure_readings"]:
            for sentence in _sentences(w["reading_texts"][key]):
                assert not [v for v in w["verdict_words"] if _has(v, sentence)], (model, locale, key, sentence)


@pytest.mark.parametrize("locale", LOCALES)
def test_every_reading_an_explained_model_chooses_is_a_sentence_of_the_page_s_language(locale):
    for case in explained_cases():
        model, params, rows = fixture(case)
        said = [r for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading"]
        assert said, case
        for r in said:
            assert not re.search(r"@|\{|%2", r["text"]), (case, r["text"])
            assert not re.search(r"=\s*[<>]", r["text"]), (case, r["text"])
            assert r["label"] not in ("", r["stat"]), (case, r)
        assert {r["level"] for r in said} >= {"method"}, case


@pytest.mark.parametrize("locale", LOCALES)
def test_an_explained_report_is_a_definition_the_engine_accepts_and_no_header_runs_into_the_next_column(locale):
    from app.reports.model import CELL_PAD

    columns = {"result": {c["name"]: c["type"] for c in layouts.columns()}}
    for case in explained_cases():
        model, params, _ = fixture(case)
        d = layouts.definition(model, params, locale, None, None)
        assert [str(p) for p in rd.check(d, columns)] == [], case
        for item in d["sections"]["body"]["items"]:
            if item["type"] != "table" or not EXPLANATION_IDS.match(item["id"]):
                continue
            cols = item["columns"]
            left = item["w"] - sum(c.get("width") or 0 for c in cols)
            free = sum(1 for c in cols if not c.get("width")) or 1
            size = item.get("style", {}).get("size", 9)
            for c in cols:
                room = (c.get("width") or left / free) - 2 * CELL_PAD + 0.2
                assert not [w for w in c["header"].split() if glyphs.width_mm(w, size, 700) > room], (case, item["id"], c["header"])


def _content(items: list[dict]) -> list[dict]:
    """The items of a body without where they are: what changes when items are added above them (a row of charts'
    invisible rectangle is named by its place among the items, `common.grid`)."""
    out = []
    for it in items:
        it = {k: v for k, v in it.items() if k not in ("y", "keep_with_next")}
        if it["type"] == "rect":
            it.pop("h", None)
            it["id"] = re.sub(r"^row_\d+_keep$", "row_keep", it["id"])
        out.append(it)
    return out


def only_additions(case: str, monkeypatch) -> None:
    """«The other reports change only by what is added»: every item of the page without its explanation is on the
    page with it, in the same order and the same in all but its place; every labelled row is there as before."""
    model, params, rows = fixture(case)
    with_it = layouts.definition(model, params, "en", None, None)
    labelled = layouts.with_labels(model, params, rows, "en")
    with monkeypatch.context() as m:
        m.delitem(common.READINGS, model)
        for fn in ("reading_under", "reading_table", "how_computed"):
            m.setattr(common, fn, lambda *a, **k: None)
        m.setattr(common, "page", lambda *a, **k: PAGE(*a, **{**k, "explained": ()}))
        without = layouts.definition(model, params, "en", None, None)
        plain = layouts.with_labels(model, params, rows, "en")
    added = [it for it in with_it["sections"]["body"]["items"] if EXPLANATION_IDS.match(it["id"])]
    kept = [it for it in with_it["sections"]["body"]["items"] if not EXPLANATION_IDS.match(it["id"])]
    assert added and _content(kept) == _content(without["sections"]["body"]["items"]), case
    assert {k: v for k, v in with_it.items() if k != "sections"} == {k: v for k, v in without.items() if k != "sections"}, case
    assert [r for r in labelled if r["section"] != "reading"] == plain, case
    assert set(strings(added)) and all(s for s in strings(added)), case


def test_an_explained_report_is_the_report_as_before_with_the_explanation_added(monkeypatch):
    for case in explained_cases():
        only_additions(case, monkeypatch)


# ── lot B4: the names a reading chooses, the lines every model shares ───────
# the sentences several models say, written once in common.json (the spec's Open question 4); a model keeps its own only
# where its noun differs (ARIMA's observations, the customer lifetime's customers)
SHARED_LINES = ("assumptions_rejected", "assumptions_none_rejected", "assumptions_many_rows", "coef_some", "coef_all",
                "coef_none", "more_in_table", "method_p_value", "method_se_cluster", "threshold_vif", "threshold_epv",
                "threshold_cooks_distance", "threshold_leverage", "coef_hazard", "coef_hazard_level")
OWN_NOUN = {("arima", "assumptions_many_rows"), ("customer_lifetime", "assumptions_many_rows")}


@pytest.mark.parametrize("locale", LOCALES)
def test_a_check_a_reading_names_is_said_as_the_table_names_it(locale):
    """`check:<id>` in a reading's list is the check's name in the table, in the page's language and in guillemets (a
    name may hold a comma); a check of each term has its term after it; any other name stays as it is."""
    model, params, _ = fixture("cox-default")
    w = common.words(model, catalog.variant(model, catalog.check_params(model, params)), locale)
    names = w["values"]["reading_names"]
    assert names["check:schoenfeld_global"] == f"«{w['checks']['schoenfeld_global']['name']}»"
    note = {**w, "note_texts": {"x": "{names}"}}
    said = common.structured_note(note, "@x names=check:schoenfeld_global,check:schoenfeld|North%20Side,check:2.5,price",
                                  formats.locale(locale))
    assert said == ", ".join([names["check:schoenfeld_global"], names["check:schoenfeld"] + " (North Side)", "check:2.5", "price"])
    # the words' own `values.reading_names` come first
    own = {**w, "values": {**w["values"], "reading_names": {**names, "check:vif": "VIF"}}, "note_texts": {"x": "{names}"}}
    assert common.structured_note(own, "@x names=check:vif", formats.locale(locale)) == "VIF"


@pytest.mark.parametrize("locale", LOCALES)
def test_the_lines_several_models_say_are_written_once_in_common_json(locale):
    shared = common._file("common")[locale]["reading_texts"]
    assert set(SHARED_LINES) <= set(shared)
    for name in WORDS_FILES:
        if name == "common":
            continue
        own = set(common._file(name)[locale].get("reading_texts", {})) & set(SHARED_LINES)
        assert own <= {k for m, k in OWN_NOUN if m == name}, (name, sorted(own))
    for model in explained():  # and a model says common.json's sentence
        w = common.words(model, catalog.variant(model, catalog.check_params(model, fixture(next(
            c for c in CASES if c.split("-")[0] == model))[1])), locale)
        for key in SHARED_LINES:
            if (model, key) not in OWN_NOUN and (catalog.MODELS[model].get("family"), key) not in OWN_NOUN:
                assert w["reading_texts"][key] == shared[key], (model, key)
