"""The report of the pre-trained forecast, «Forecast (Timestral)» (docs/specs/foundation-forecast.md, lot F4): the words of
every sentence the runner may say in the five languages, the owner's rule on verdicts (only Wilcoxon's test across the
series; no word of judgement in a measure), the slots of the series drawn, the holdout, the comparison's verdict in its own
words, and the licence of the weights: on the first page in a line, on the last page verbatim, the vendored file's bytes.

The fixture (`timestral-default.json`, lot F0) holds three imaginary series and a few readings; `full_rows` adds a line of
every template the spec names, with the names the runner (lot F3) writes, so that each is said and drawn."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from app.engine.models import catalog, layouts
from app.engine.models.layouts import common
from app.engine.models.layouts import timestral as layout
from app.engine.stats.layouts import LOCALES
from app.reports import definition as rd
from app.reports import formats, glyphs
from tests.test_models_layouts import draw, fixture, strings
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

CASE = "timestral-default"
# the licence of the weights: the sha256 of the file the group vendors (licence/LICENSE.txt, lot F0; its recipe pins it)
LICENCE_SHA256 = "648194727930df730890e9f9affdbcc5c930d406d6829559b25ca3d5a973fc8c"
VENDORED = (Path(__file__).resolve().parents[2] / "gateway-go" / "internal" / "modelgroups" / "groups" / "foundation_forecast"
            / "licence" / "LICENSE.txt")

# every template the runner may write, with the names it sends (the contract with lot F3; docs/specs/foundation-forecast.md,
# «Explaining the method in the report» and «As built / F4»)
READINGS: dict[str, dict] = {
    "summary": {"size": "Timestral-28M", "n_series": 3, "value": "orders", "series": "channel", "horizon": 14, "last": 20632.0,
                "context": 120},
    "summary_one": {"size": "Timestral-28M", "value": "orders", "horizon": 14, "last": 20632.0, "context": 120},
    "summary_steps": {"size": "Timestral-28M", "n_series": 3, "value": "orders", "series": "channel", "horizon": 14, "last": 120,
                      "context": 120},
    "summary_one_steps": {"size": "Timestral-28M", "value": "orders", "horizon": 14, "last": 120, "context": 120},
    "summary_known": {"covariate": ["festivo", "promo"]},
    "summary_past": {"covariate": ["price"]},
    "filled_linear": {"count": 3},
    "missing_model": {"count": 2},
    "forecast_bands": {"median": 71.906, "low80": 69.856, "high80": 73.957, "low90": 69.274, "high90": 74.538, "at": 20633.0},
    "forecast_bands_steps": {"median": 71.906, "low80": 69.856, "high80": 73.957, "low90": 69.274, "high90": 74.538, "at": 121},
    "holdout_mase": {"holdout": 14, "mase": 0.4756, "baseline_mase": 1.4443},
    "holdout_not_measured": {"count": 1, "n_series": 3},
    "holdout_none": {},
    "coverage": {"in80": 12, "in90": 13, "steps": 14, "share80": 0.857, "share90": 0.929},
    "comparison_supported": {"better": 9, "n_series": 10, "p_value": 0.0039, "alpha": 0.05},
    "comparison_opposite": {"better": 1, "n_series": 10, "p_value": 0.0039, "alpha": 0.05},
    "comparison_not_supported": {"better": 3, "n_series": 3, "p_value": 0.25, "alpha": 0.05},
    "comparison_few_series": {"n_series": 3, "min_p": 0.25, "alpha": 0.05},
    "comparison_one_series": {"mase": 0.62, "baseline_mase": 1.0},
    "comparison_not_computed": {"n_measured": 1},
    "limits": {},
    "limits_context": {"used": 32768, "n": 100000, "series": "site A"},
    "limits_clip": {},
    "licence": {"licence": "Modified MIT License (Mistral AI)"},
    "licence_accepted": {"by": "ada@example.com", "on": 20778.0},
    **{f"method_{k}": {} for k in ("zero_shot", "quantiles", "holdout", "baseline", "coverage", "wilcoxon", "limits", "missing")},
}
FORMULAS = ("formula_naive", "formula_mase", "formula_smape", "formula_pinball", "formula_coverage", "formula_wilcoxon")
NOTES: dict[str, dict] = {
    "ends_early": {"count": 4, "last": 20649.0}, "context_cut": {"used": 32768, "n": 100000, "series": "site A"},
    "history_shrunk": {"points": 1500}, "future_left_out": {"count": 28},
    "covariate_missing": {"count": 2, "covariate": "promo"}, "time_budget": {"seconds": 41.2, "limit": 900},
    "ends_early_steps": {"count": 1, "last": 39999}, "series_kept": {"kept": 5000, "total": 6123, "most": 100000},
}
BLOCK = {"summary": "summary", "filled": "summary", "missing": "summary", "forecast": "forecast", "holdout": "holdout",
         "coverage": "coverage", "comparison": "comparison", "limits": "limits", "licence": "licence", "method": "method"}
VERDICTS = ("comparison_supported", "comparison_opposite", "comparison_not_supported")
# words of judgement besides common.json's `verdict_words`, for the measures of this report (lot F9)
JUDGEMENT = {"en": ("advantage", "too narrow", "too wide", "clear", "helps", "worse"),
             "it": ("vantaggio", "netto", "troppo strette", "troppo larghe", "aiuta", "peggiore"),
             "de": ("Vorsprung", "zu schmal", "zu breit", "klarer", "hilft", "schlechter"),
             "fr": ("avance nette", "trop étroites", "trop larges", "nette", "aide", "pire"),
             "es": ("ventaja", "demasiado estrechas", "demasiado anchas", "clara", "ayuda", "peor")}


def _block(id_: str) -> str:
    return "formulas" if id_.startswith("formula_") else BLOCK[id_.split("_")[0]]


def full_rows(rows: list[dict], frequency_free: bool = True) -> list[dict]:
    """The fixture's rows with a line of every template in place of its readings (the forecast's under its first series),
    and a note of every id."""
    kept = [r for r in rows if r["section"] != "reading" and not (r["section"] == "model" and r["stat"].startswith("note_"))]
    lines = [(_block(i), "Online" if i.startswith("forecast_") else None, i, v) for i, v in READINGS.items()]
    lines += [("formulas", None, i, {}) for i in FORMULAS]
    out = kept + common.reading_rows(lines, kept)
    last = max(r["ord"] for r in out)
    for k, (id_, values) in enumerate(NOTES.items(), start=1):
        text = " ".join(["@" + id_, *(f"{n}={common.encoded(v)}" for n, v in values.items())])
        out.append({"section": "model", "level": None, "level_2": None, "stat": f"note_{k}", "value": None, "text": text,
                    "x0": None, "x1": None, "ord": last + k, "key": f"model|||note_{k}"})
    return out


def _words(locale: str) -> dict:
    return common.words("timestral", "default", locale)


def _said(text) -> list[str]:
    return [text] if isinstance(text, str) else list(text.values())


def _has(word: str, sentence: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence, re.I))


def _with(monkeypatch, params: dict, rows: list[dict]) -> None:
    """`draw` and `fixture` of test_models_layouts on these parameters and rows."""
    import tests.test_models_layouts as me

    monkeypatch.setattr(me, "fixture", lambda case: ("timestral", params, rows))


# ── the words ───────────────────────────────────────────────────────────────
def test_every_template_the_runner_may_write_has_its_sentence_its_names_and_a_label():
    words = common._file("timestral")
    english = words["en"]
    assert set(english["reading_texts"]) == set(READINGS), sorted(set(english["reading_texts"]) ^ set(READINGS))
    assert set(english["formula_texts"]) == set(FORMULAS) and set(english["note_texts"]) == set(NOTES)
    for kind, ids in (("reading_texts", READINGS), ("note_texts", NOTES)):
        for id_, values in ids.items():
            for sentence in _said(english[kind][id_]):
                names = set(re.findall(r"\{(\w+)(?::\w+)?\}", sentence))
                assert names <= set(values), (id_, sentence)
    for id_ in (*FORMULAS, *(i for i in READINGS if i.startswith("method_"))):
        assert all(words[code]["reading_labels"].get(id_) for code in LOCALES), id_
    # a sentence that names a unit of time says it in the frequency's words, the steps for a series in steps
    days = english["reading_texts"]["holdout_mase"]
    assert "14 days" in days["frequency=day"].replace("{holdout:int}", "14") and "steps" in days[""]
    assert "{last:date}" not in english["reading_texts"]["summary"]["frequency=hour"]


@pytest.mark.parametrize("locale", LOCALES)
def test_no_sentence_of_a_measure_holds_a_word_of_judgement_and_each_verdict_holds_one(locale):
    """The owner's rule: the only verdict is Wilcoxon's test across the series, against alpha. The forecast's bands, the
    errors on the holdout, the coverage, the limits are measures: their sentences never say «better», «good», «supported»,
    «real»… (`verdict_words`, common.json's), in any language; and the three verdicts of the test each hold one."""
    w = _words(locale)
    assert set(w["measure_readings"]) <= set(w["reading_texts"]) and not set(w["measure_readings"]) & set(VERDICTS)
    assert set(READINGS) - set(w["measure_readings"]) == {*VERDICTS, "comparison_few_series",
                                                          *(i for i in READINGS if i.startswith("method_"))}
    measures = [s for key in w["measure_readings"] for s in _said(w["reading_texts"][key])]
    measures += [*w["measure_texts"].values(), *w["across_texts"].values(), *w["captions"].values(), *w["kpi"].values(),
                 *w["note_texts"].values()]
    for sentence in measures:
        assert not [v for v in w["verdict_words"] if _has(v, sentence)], (locale, sentence)
    for key in VERDICTS:
        assert any(_has(v, w["reading_texts"][key]) for v in w["verdict_words"]), (locale, key)
    # lot F9: the explanations of the measures on the last page and their formulas are measures too, and a few
    # words of judgement common.json does not list (a coverage «too narrow», a «clear advantage», a covariate that «helps»)
    tests = ("method_wilcoxon", "method_p_value")  # the paragraphs of the test itself
    explained = [s for key, said in w["reading_texts"].items() if key.startswith("method_") and key not in tests
                 for s in _said(said)]
    explained += [v for k, v in w["formula_texts"].items() if k != "formula_wilcoxon"]  # the alpha line explains the test
    for sentence in explained + measures:
        assert not [v for v in (*w["verdict_words"], *JUDGEMENT[locale]) if _has(v, sentence)], (locale, sentence)
    # no p-value after «=» («p = < 0.0001»)
    assert not [s for k in w["reading_texts"] for s in _said(w["reading_texts"][k]) if re.search(r"=\s*\{\w+:p\}", s)]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("frequency", ("day", "hour", "month", "index"))
def test_every_reading_and_note_is_a_sentence_of_the_page_s_language(locale, frequency):
    model, params, rows = fixture(CASE)
    params = {**params, "frequency": frequency, "season": 7 if frequency == "day" else 1}
    said = layouts.with_labels(model, params, full_rows(rows), locale)
    readings = [r for r in said if r["section"] == "reading"]
    assert len(readings) == len(READINGS) + len(FORMULAS)
    for r in readings + [r for r in said if r["section"] == "model" and r["stat"].startswith("note_")]:
        assert not re.search(r"^@|\{|%2|§", r["text"]), r["text"]  # an e-mail address holds its own «@»
        assert not re.search(r"=\s*[<>]", r["text"]), r["text"]
        assert r["label"] not in ("", r["stat"]), r
    decimal = formats.locale(locale).decimal
    bands = [r["text"] for r in readings if r["level"] == "forecast"]
    assert len(bands) == 2 and all(f"71{decimal}91" in b and f"69{decimal}86" in b for b in bands), bands
    # a series' or a column's name stays as it is, never read as a number
    assert any("«site A»" in r["text"] for r in readings) and any("«channel»" in r["text"] for r in readings)


# ── the page ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("locale", LOCALES)
def test_the_report_with_every_reading_renders_and_the_engine_accepts_it(tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    model, params, rows = fixture(CASE)
    columns = {"result": {c["name"]: c["type"] for c in layouts.columns()}}
    for p in (params, {**params, "holdout": 0}, {**params, "series": None}, {**params, "known_covariates": ["festivo"],
                                                                                 "horizon": None}):
        assert [str(x) for x in rd.check(layouts.definition(model, {k: v for k, v in p.items() if v is not None}, locale), columns)] == []
    _with(monkeypatch, params, full_rows(rows))
    out, page = draw(tmp_path, chart_service, monkeypatch, CASE, locale=locale)
    assert out.path.read_bytes().startswith(b"%PDF-") and 5 <= out.pages <= 12 and out.warnings == [], (out.pages, out.warnings)
    said = set(strings(json.loads(page)))
    assert not [s for s in said if re.search(r"=\s*[<>]", s)]


def test_the_first_page_holds_the_cards_the_check_the_measures_and_the_licence_line(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The summary's items come before the first chart and the checks' table, so they are on the first page: the cards,
    the formula line in the page's words, the licence line, the check in its own words, the measures."""
    _, page = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="svg")
    said = " ".join(strings(json.loads(page)))
    w = _words("en")
    for text in ("Forecast (Timestral-28M)", "Holdout MASE", "against 1.44 for the seasonal naïve",
                 "orders ~ Timestral-28M · 3 series by «channel» · daily series · 14 days ahead",
                 "Timestral against the seasonal naïve: Wilcoxon", "– not supported",
                 "Forecast by Timestral-28M, weights by Mistral AI under the Modified MIT License"):
        assert text in said, text
    body = layouts.definition("timestral", fixture(CASE)[1], "en")["sections"]["body"]["items"]
    ids = [it["id"] for it in body]
    assert ids.index("k_mase") < ids.index("licence_line") < ids.index("assumptions") < ids.index("measures") < ids.index("forecast_1")
    assert min(i for i, it in enumerate(body) if it["type"] == "chart") == ids.index("forecast_1") < ids.index("holdout_1")
    assert ids[-1] == "licence_text" and ids.index("notes") < ids.index("weights") < ids.index("licence_text")
    assert "many_rows" not in ids and w["sections"]["assumptions"] in strings(body)


def test_each_series_drawn_has_its_slot_and_the_others_take_no_room(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The layout does not know the labels: the k-th slot shows the k-th series the runner drew, found by the calculated
    fields `drawn_<k>`; a slot without a series is not drawn (`collapse_hidden`)."""
    del chart_service[1][:]
    draw(tmp_path, chart_service, monkeypatch, CASE)
    w = _words("en")
    asked = [spec["option"] for spec in chart_service[1] if "title" in spec["option"]]
    forecasts = [o for o in asked if o["title"]["text"] == w["charts"]["forecast"]]
    holdouts = [o for o in asked if o["title"]["text"] == w["charts"]["holdout"]]
    assert len(forecasts) == len(holdouts) == 3
    for o in forecasts:
        assert o["xAxis"]["type"] == "time"
        assert o["legend"]["data"] == ["90 % band", "80 % band", "Observed", "Seasonal naïve", "Median"]
        marks = next(s for s in o["series"] if "markLine" in s)["markLine"]["data"]
        assert marks == [{"xAxis": 20633 * 86_400_000}]  # the first instant forecast
    for o in holdouts:
        marks = next(s for s in o["series"] if "markLine" in s)["markLine"]["data"]
        assert marks == [{"xAxis": 20619 * 86_400_000}]  # the first value kept out
    [relative] = [o for o in asked if o["title"]["text"] == w["charts"]["relative"]]
    assert relative["xAxis"]["data"] == ["Online", "Phone", "Shop"] and relative["yAxis"]["type"] == "log"
    body = layouts.definition("timestral", fixture(CASE)[1], "en")["sections"]["body"]["items"]
    slots = [it for it in body if re.match(r"^(forecast|holdout)_(name_|keep_)?\d+(_steps)?$", it["id"])]
    assert len(slots) == 12 * 4 + 12 * 3 + 6 and all(it.get("collapse_hidden") for it in slots)


def test_a_time_in_steps_draws_the_charts_along_integers(tmp_path, chart_service, monkeypatch):  # noqa: F811
    model, params, rows = fixture(CASE)
    rows = [{**r, "text": "integer"} if r["key"] == "model|||time_kind" else r for r in rows]
    _with(monkeypatch, params, rows)
    del chart_service[1][:]
    draw(tmp_path, chart_service, monkeypatch, CASE)
    titled = [s["option"] for s in chart_service[1] if s["option"].get("title", {}).get("text") == _words("en")["charts"]["forecast"]]
    assert len(titled) == 3 and all(o["xAxis"]["type"] != "time" for o in titled)


def test_one_series_without_a_holdout_has_one_slot_no_holdout_page_and_no_measures(tmp_path, chart_service, monkeypatch):  # noqa: F811
    model, params, rows = fixture(CASE)
    one = {k: v for k, v in params.items() if k != "series"} | {"holdout": 0}
    rows = [r for r in rows if r["section"] not in ("series", "series_curve") or r["level"] == "Online"]
    rows = [r for r in rows if not (r["section"] == "series_curve" and r["level_2"].startswith("holdout_"))]
    _with(monkeypatch, one, rows)
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="svg")
    ids = {it["id"] for it in layouts.definition(model, one, "en")["sections"]["body"]["items"]}
    assert not {"holdout_1", "series", "measures", "across", "relative_mase"} & ids
    asked = [s["option"]["title"]["text"] for s in chart_service[1] if "title" in s["option"]]
    assert asked.count(_words("en")["charts"]["forecast"]) == 1
    said = " ".join(strings(json.loads(page)))
    assert "orders ~ Timestral-28M · daily series · 14 days ahead" in said and "no holdout" in said


@pytest.mark.parametrize(("verdict", "median", "lean", "words"), (("ok", -1.1, None, "ok"), ("failed", -1.1, None, "ahead"),
                                                            ("failed", 0.4, None, "behind"), ("failed", 0.01, -0.45, "ahead"),
                                                            ("failed", -0.2, 0.3, "behind")))
@pytest.mark.parametrize("locale", ("en", "it"))
def test_the_comparison_says_its_verdict_in_its_own_words(tmp_path, chart_service, monkeypatch, verdict, median, lean, words, locale):  # noqa: F811
    """Not rejecting H0 is «not supported», in the muted colour (never a broken assumption's red); a rejection names the
    forecast ahead by the side of the test, the sign of the rank-biserial correlation the runner writes since lot F9 (26
    differences of +0.01 and 24 near −1: the median above 0, the ranks on Timestral's side), else of the median."""
    model, params, rows = fixture(CASE)
    rows = [{**r, "text": verdict} if r["key"] == "assumption|baseline_comparison||verdict" else
            {**r, "value": median} if r["key"] == "assumption|baseline_comparison||value" else r for r in rows]
    if lean is not None:
        value = next(r for r in rows if r["key"] == "assumption|baseline_comparison||value")
        rows.append({**value, "stat": "rank_biserial", "value": lean, "ord": len(rows) + 100,
                     "key": "assumption|baseline_comparison||rank_biserial"})
    rows.append({**next(r for r in rows if r["key"] == "assumption|baseline_comparison||h0"), "stat": "advice",
                 "text": "the runner's advice", "ord": len(rows), "key": "assumption|baseline_comparison||advice"})
    _with(monkeypatch, params, rows)
    _, page = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="svg", locale=locale)
    said = set(strings(json.loads(page)))
    own = _words(locale)["comparison_verdicts"]
    assert own[words] in said and not ({v for k, v in own.items() if k != words} & said)
    w = _words(locale)
    if verdict == "failed":  # what to do: the check's advice, under its own words
        assert w["checks"]["baseline_comparison"]["advice"] in said


# ── the licence ─────────────────────────────────────────────────────────────
def test_the_licence_is_the_vendored_file_byte_for_byte_and_never_translated():
    """The words hold the licence of the weights once per language, the same bytes in the five: the file the group vendors
    (licence/LICENSE.txt, its sha256 pinned by the recipe and written by the runner as `licence_sha256`). The engine's
    suite holds only the backend: there the sha256 says the bytes; with the repository beside it, the file says them too."""
    texts = {code: common._file("timestral")[code]["licence_text"] for code in LOCALES}
    assert len(set(texts.values())) == 1
    text = texts["en"]
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == LICENCE_SHA256
    assert text.startswith("Modified MIT License \n\nAttribution notice: 2026 - Mistral AI\n")
    assert "global consolidated monthly revenue of your company (or that of your employer) exceeds $20 million" in text
    if VENDORED.exists():
        assert VENDORED.read_bytes() == text.encode("utf-8")
    _, _, rows = fixture(CASE)
    assert next(r["text"] for r in rows if r["key"] == "model|||licence_sha256") == LICENCE_SHA256
    assert glyphs.missing(set(text)) == set()


@pytest.mark.parametrize("locale", LOCALES)
def test_the_last_page_holds_the_licence_verbatim_with_the_weights(tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    text = common._file("timestral")["en"]["licence_text"]
    body = layouts.definition("timestral", fixture(CASE)[1], locale)["sections"]["body"]["items"]
    [cell] = body[-1]["columns"]
    assert body[-1]["id"] == "licence_text" and cell["value"] == common.q(text)
    _, page = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="svg", locale=locale)
    handed = strings(json.loads(page))
    assert text in handed, "the licence is handed whole to the page, every byte"
    for line in ("mistralai/Timestral-28M@49b26409", "dfc83c6220937ce5fc6687b35a4df94cd25e28a2cfe6fb92a23dad6301b2015d",
                 LICENCE_SHA256, "Modified MIT License (Mistral AI)"):
        assert line in handed, line


def test_excel_keeps_the_check_the_series_the_reading_and_the_licence(tmp_path, chart_service, monkeypatch):  # noqa: F811
    out, _ = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        sheets = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
        xml = " ".join(z.read(n).decode() for n in z.namelist() if n == "xl/sharedStrings.xml" or n.startswith("xl/worksheets/"))
    w = _words("en")
    for title in (w["assumptions"]["title"], w["tables"]["title"], "Reading", w["licence"]["weights_title"], w["licence"]["text_title"]):
        assert title[:31] in sheets, (title, sheets)
    assert "Attribution notice: 2026 - Mistral AI" in xml


@pytest.mark.parametrize("locale", LOCALES)
def test_no_header_of_the_forecast_s_tables_runs_into_the_next_column(locale):
    from app.reports.model import CELL_PAD

    model, params, _ = fixture(CASE)
    for item in layouts.definition(model, params, locale, None, None)["sections"]["body"]["items"]:
        if item["type"] != "table" or item["id"] in ("assumptions", "advice", "fit", "settings", "notes"):
            continue
        cols = item["columns"]
        left = item["w"] - sum(c.get("width") or 0 for c in cols)
        free = sum(1 for c in cols if not c.get("width")) or 1
        size = item.get("style", {}).get("size", 9)
        for c in cols:
            room = (c.get("width") or left / free) - 2 * CELL_PAD + 0.2
            wide = [word for word in c["header"].split() if glyphs.width_mm(word, size, 700) > room]
            assert not wide, (locale, item["id"], c["header"], round(room, 1))


def test_the_report_s_name_says_the_model_s_size_and_the_series(monkeypatch):
    params = fixture(CASE)[1]
    assert layouts.title("timestral", params, "en") == "Forecast (Timestral-28M) — «orders» over «day» by «channel», daily series"
    assert layouts.title("timestral", {**params, "size": "209m", "series": None}, "it") == \
        "Previsione (Timestral-209M) — «orders» nel tempo «day», serie giornaliera"
    assert catalog.LIMITS["max_series_charts"] == layout.SLOTS == 12


def test_the_runner_s_notes_by_their_rows_and_the_holdout_s_seasonal_naive(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """What lot F3's runner writes: a note per cut context and per covariate with empty values (`note_context_cut_<k>`,
    `note_covariate_missing_<k>`), each with its name; the series' end as a position for a time in steps
    (`ends_early_steps`); the table of the series cut to the results' ceiling (`series_kept`); the seasonal naïve on the
    holdout (`holdout_baseline`), drawn in the holdout's charts as on the horizon."""
    model, params, rows = fixture(CASE)
    notes = {"note_context_cut_1": "@context_cut used=32768 n=40000 series=long", "note_covariate_missing_1":
             "@covariate_missing count=18 covariate=promo", "note_ends_early": "@ends_early_steps count=1 last=39999",
             "note_series_left_out": "@series_kept kept=5000 total=6123 most=100000"}
    last = max(r["ord"] for r in rows)
    extra = [{"section": "model", "level": None, "level_2": None, "stat": k, "value": None, "text": v, "x0": None, "x1": None,
              "ord": last + i + 1, "key": f"model|||{k}"} for i, (k, v) in enumerate(notes.items())]
    naive = [{**r, "level_2": "holdout_baseline", "value": r["value"] + 1, "ord": last + 10 + i,
              "key": r["key"].replace("|holdout_median|", "|holdout_baseline|")}
             for i, r in enumerate(x for x in rows if x["section"] == "series_curve" and x["level_2"] == "holdout_median")]
    rows = rows + extra + naive
    for locale in LOCALES:
        said = {r["stat"]: r for r in layouts.with_labels(model, params, rows, locale) if r["stat"] in notes}
        assert len(said) == 4 and all(not re.search(r"^@|\{|%2", r["text"]) and r["label"] != r["stat"] for r in said.values()), said
        assert "39999" in re.sub(r"\D", "", said["note_ends_early"]["text"])  # a position, never a date
    _with(monkeypatch, params, rows)
    del chart_service[1][:]
    draw(tmp_path, chart_service, monkeypatch, CASE)
    holdouts = [s["option"] for s in chart_service[1] if s["option"].get("title", {}).get("text") == _words("en")["charts"]["holdout"]]
    assert len(holdouts) == 3
    for o in holdouts:
        assert any(x.get("name") == "Seasonal naïve" and x.get("data") for x in o["series"]), [x.get("name") for x in o["series"]]


# ── lot F9 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("locale", LOCALES)
def test_the_first_page_s_licence_line_says_the_revenue_clause(locale):
    """Whoever works for a large company is told on the first page: the clause of point 2 in one line, word for word in
    English (a part of the vendored text), its threshold, the currency and the month in every language."""
    line = _words(locale)["licence_line"]
    text = common._file("timestral")["en"]["licence_text"]
    assert "Modified MIT License" in line and "{model}" in line and "20" in line
    if locale == "en":
        quoted = re.search(r"«([^»]+)»", line).group(1)
        assert quoted in text and "exceeds $20 million (or its equivalent in another currency) for the preceding month" in quoted
    else:
        assert re.search(r"20 (millions? de dollars|milioni di dollari|Millionen US-Dollar|millones de dólares)", line), line
    model, params, _ = fixture(CASE)
    [item] = [it for it in layouts.definition(model, params, locale)["sections"]["body"]["items"] if it["id"] == "licence_line"]
    assert "Timestral-28M" in item["value"] and "20" in item["value"]


def test_the_series_table_keeps_the_smape_and_its_sign_together_and_a_lone_holdout_chart_takes_the_width():
    model, params, _ = fixture(CASE)
    body = layouts.definition(model, params, "it")["sections"]["body"]["items"]
    [table] = [it for it in body if it["id"] == "series"]
    smape = next(col for col in table["columns"] if col["header"] == _words("it")["tables"]["smape"])
    assert '"\u00a0%"' in smape["value"] and smape["width"] >= 19
    assert sum(col.get("width") or 0 for col in table["columns"]) <= table["w"] - 15  # room left for the series' names
    by = {it["id"]: it for it in body}
    for k in range(1, layout.SLOTS + 1, 2):  # the left slot of each row has its form alone, across the width
        for tail in ("", "_steps"):
            alone, paired = by[f"holdout_{k}_alone{tail}"], by[f"holdout_{k}{tail}"]
            assert alone["w"] == common.WIDTH and paired["w"] < common.WIDTH / 2 and alone["y"] == paired["y"]
            assert alone.get("collapse_hidden") and f"NOT (COUNTIF(" in alone["visible"]
        assert f"holdout_{k + 1}_alone" not in by
    [relative] = [it for it in body if it["id"] == "relative_mase"]
    assert relative["options"]["labels_above"] is True


def test_a_lone_holdout_chart_is_drawn_once_across_the_width(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """Three series: the third holdout chart is alone on its row, drawn in its full-width form only; the relative MASE's
    values above their dots, clear of the line at 1 (lot F9, seen on kind: «1,01» on the line)."""
    del chart_service[1][:]
    draw(tmp_path, chart_service, monkeypatch, CASE)
    w = _words("en")
    asked = [s for s in chart_service[1] if s["option"].get("title", {}).get("text") == w["charts"]["holdout"]]
    assert len(asked) == 3
    assert len({s.get("width") for s in asked}) == 2, [s.get("width") for s in asked]  # two halves and one whole
    [relative] = [s["option"] for s in chart_service[1] if s["option"].get("title", {}).get("text") == w["charts"]["relative"]]
    dots = next(x for x in relative["series"] if x.get("type") == "scatter")
    assert dots["label"]["position"] == "top"


@pytest.mark.parametrize("hostile", ("=HYPERLINK(\"https://example.invalid\",\"x\")", "@SUM(1+1)", "+1-1", "-2+3"))
def test_a_series_named_like_a_formula_stays_text_in_the_pdf_and_the_workbook(tmp_path, chart_service, monkeypatch, hostile):  # noqa: F811
    model, params, rows = fixture(CASE)

    def renamed(r: dict) -> dict:
        r = dict(r)
        for f in ("level", "level_2"):
            if r.get(f) == "Online":
                r[f] = hostile
        r["key"] = r["key"].replace("|Online|", f"|{hostile}|")
        return r

    rows = [renamed(r) for r in rows]
    _with(monkeypatch, params, rows)
    out, page = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="svg")
    assert hostile in " ".join(strings(json.loads(page)))
    out, _ = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        sheets = [z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/")]
        shared = " ".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xml"))
    assert not [x for x in sheets if re.search(r"<f>(?!SUBTOTAL)", x)], "a cell became a formula"
    from xml.sax.saxutils import escape
    assert escape(hostile) in shared


@pytest.mark.parametrize("locale", LOCALES)
def test_wql_is_twice_the_pinball_and_what_the_means_leave_out_is_said(tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    """Lot F9 (the owner's choice): WQL as usually defined, 2 Σ pinball / Σ|y|, in the formula box and the words; the steps
    sMAPE leaves out (the value and the forecast both 0) and the series the geometric mean of the relative MASE leaves out
    are counted by the runner and said under the mean errors, each with what it is, only when there is one."""
    w = _words(locale)
    assert "WQL = 2 · " in w["formula_texts"]["formula_pinball"]
    assert "2" in w["across_texts"]["wql"] or re.search(r"(?i)twice|doppio|doppelte|doble|double", w["across_texts"]["wql"])
    for stat in layout.LEFT_OUT:
        assert w["stats"]["model"][stat] and w["across_texts"][stat], stat
    model, params, rows = fixture(CASE)
    body = layouts.definition(model, params, locale)["sections"]["body"]["items"]
    [across] = [it for it in body if it["id"] == "across"]
    assert all(f'[stat] = "{s}"' in across["filter"] for s in layout.LEFT_OUT) and "[value] > 0" in across["filter"]
    last = max(r["ord"] for r in rows)
    n_measured = next(r for r in rows if r["key"] == "model|||n_measured")
    counts = {"n_smape_zero": 3, "n_baseline_smape_zero": 0, "n_relative_left_out": 1}
    extra = [{**n_measured, "stat": k, "value": float(v), "ord": last + i + 1, "key": f"model|||{k}"} for i, (k, v) in enumerate(counts.items())]
    _with(monkeypatch, params, rows + extra)
    _, page = draw(tmp_path, chart_service, monkeypatch, CASE, fmt="svg", locale=locale)
    said = set(strings(json.loads(page)))
    assert w["stats"]["model"]["n_smape_zero"] in said and w["across_texts"]["n_smape_zero"] in said
    assert w["stats"]["model"]["n_relative_left_out"] in said and w["across_texts"]["n_relative_left_out"] in said
    assert w["stats"]["model"]["n_baseline_smape_zero"] not in said, "a count of 0 is not a row"
    # without the counts (a result of before), the table is as it was
    _with(monkeypatch, params, rows)
    _, page = draw(tmp_path / "before", chart_service, monkeypatch, CASE, fmt="svg", locale=locale)
    assert not {w["stats"]["model"][s] for s in layout.LEFT_OUT} & set(strings(json.loads(page)))
