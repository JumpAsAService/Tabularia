"""POST /placeholders/preview: the live preview of the forms and the check the
gateway makes before a run — each template's value now, or its message."""
from __future__ import annotations

from datetime import date

from app.api.routes import placeholders as route


def _preview(**kw):
    return route.preview(route.PreviewRequest(**kw))


def test_an_input_template_with_a_range_shows_its_first_and_last_value():
    today = date.today()
    out = _preview(templates={"pattern": "s_{day:yyyyMMdd}.csv", "folder": "in"},
                   variables=[{"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}])
    assert out.error is None and out.expansions == 3
    assert out.fields["pattern"].value.startswith("s_") and out.fields["pattern"].last.endswith(".csv")
    assert out.fields["folder"].value == "in" and out.fields["folder"].last is None
    assert today.strftime("%Y%m%d") not in out.fields["pattern"].last  # the end is excluded


def test_each_template_gets_its_own_message():
    out = _preview(templates={"a": "{tomorrow}", "b": "{today}"})
    assert "unknown placeholder {tomorrow}" in out.fields["a"].error and out.fields["b"].error is None


def test_a_broken_range_is_a_general_error():
    out = _preview(templates={"p": "{day}"},
                   variables=[{"name": "day", "type": "range", "start": "today", "end": "today-1", "step": "1d"}])
    assert "empty" in out.error


def test_an_output_preview_in_its_zone_with_run_id_left_as_it_is():
    out = _preview(kind="output", templates={"file_name": "x_{run_id}_{flow}.csv", "subject": "R {flow}"},
                   targets={"file_name": "file", "subject": "text"}, flow="Vendite mese", timezone="Europe/Rome")
    assert out.fields["file_name"].value == "x_{run_id}_Vendite_mese.csv"
    assert out.fields["subject"].value == "R Vendite mese"


def test_an_output_preview_refuses_what_the_run_would_refuse():
    out = _preview(kind="output", templates={"file_name": "{today:yyyy/MM}.csv", "key": "x/{last_refresh}"},
                   targets={"file_name": "file", "key": "path"})
    assert "'/'" in out.fields["file_name"].error and "Output" in out.fields["key"].error


def test_a_preview_writes_names_in_the_language_asked():
    month = date.today().month
    out = _preview(kind="output", templates={"subject": "{today:MMMM}"}, targets={"subject": "text"}, lang="it")
    assert out.fields["subject"].error is None
    assert out.fields["subject"].value == ("gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre "
                                           "novembre dicembre").split()[month - 1]
    assert _preview(templates={"p": "{today:MMM}"}).fields["p"].value == date.today().strftime("%b")
