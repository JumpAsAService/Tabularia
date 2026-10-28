"""The Pushmail cell of a flow group: one email, one .xlsx per datasource, the rows cut
to each one's maximum and said in the body, names from the date placeholders, the
size cap, and the recipients as given (the gateway checks them); the reports rendered
in the same task, attached, one of them summarised in the body with its charts by cid."""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone

import polars as pl
import pytest

from app.engine.cache import StepCache
from app.engine.polars_engine import PolarsEngine
from app.ingest import pushmail as pm
from app.ingest.email_destination import EmailDestinationError, SmtpConnectionSpec
from tests import report_samples as samples
from tests.conftest import upload_df
from tests.fakes import FakeRedis
from tests.test_reports_render import chart_service  # noqa: F401 — the fixture

NOW = datetime(2026, 10, 24, 21, 30, tzinfo=timezone.utc)


@pytest.fixture
def sent(monkeypatch, storage):
    engine = PolarsEngine(storage=storage, cache=StepCache(storage, redis_client=FakeRedis()))
    monkeypatch.setattr("app.engine.get_engine", lambda name: engine)
    out: list = []
    monkeypatch.setattr(pm, "_send", lambda conn, msg, to: out.append((msg, to)))
    return out


@pytest.fixture
def sales(storage):
    return upload_df(storage, pl.DataFrame({"regione": ["Nord", "Sud", "Centro"] * 400, "importo": range(1200)}),
                     "datasets/sales.parquet")


@pytest.fixture
def regions(storage):
    return upload_df(storage, pl.DataFrame({"regione": ["Nord", "Sud"], "manager": ["Anna", "Bruno"]}), "datasets/regions.parquet")


def _conn():
    return SmtpConnectionSpec(host="smtp.test", from_address="data@acme.example")


def _spec(*attachments, **kw):
    base = dict(to=["anna@acme.example"], cc=["bruno@acme.example"], subject="Report {today:yyyy-MM-dd}",
                body="Ciao,\nin allegato i dati.", attachments=list(attachments), flow="Chiusura")
    base.update(kw)
    return pm.PushmailSpec(**base)


def _att(ds, name, rows=10_000, label=""):
    return pm.PushmailAttachment(bucket=ds.bucket, key=ds.key, file_name=name, max_rows=rows, label=label)


def _files(msg):
    return {p.get_filename(): pl.read_excel(io.BytesIO(p.get_content())) for p in msg.iter_attachments()}


def test_one_xlsx_per_datasource_with_names_from_the_dates(sent, sales, regions):
    out = pm.send_pushmail(_conn(), _spec(_att(sales, "vendite_{today:yyyy-MM-dd}"), _att(regions, "regioni.xlsx")), now=NOW)
    (msg, to), = sent
    assert to == ["anna@acme.example", "bruno@acme.example"]
    assert msg["Subject"] == "Report 2026-10-24" and msg["Cc"] == "bruno@acme.example"
    files = _files(msg)
    assert list(files) == ["vendite_2026-10-24.xlsx", "regioni.xlsx"]
    assert files["vendite_2026-10-24.xlsx"].height == 1200 and files["regioni.xlsx"].columns == ["regione", "manager"]
    assert out["rows"] == 1202 and out["cut"] == [] and "Not every row" not in msg.get_body(("plain",)).get_content()


def test_a_datasource_over_its_maximum_is_cut_and_the_body_says_so(sent, sales, regions):
    out = pm.send_pushmail(_conn(), _spec(_att(sales, "vendite", rows=100, label="Vendite"), _att(regions, "regioni"), lang="it"), now=NOW)
    (msg, _), = sent
    assert _files(msg)["vendite.xlsx"].height == 100
    text = msg.get_body(("plain",)).get_content()
    assert text.startswith("Ciao,\nin allegato i dati.")
    assert "Non tutte le righe sono allegate:\nVendite: le prime 100 righe su 1.200." in text
    assert out["cut"] == [{"datasource": "Vendite", "sent": 100, "total": 1200}]


def test_an_html_body_gets_the_note_as_html(sent, sales):
    pm.send_pushmail(_conn(), _spec(_att(sales, "v", rows=10, label="Sales"), body="<p>Hello</p>", body_is_html=True), now=NOW)
    (msg, _), = sent
    html = msg.get_body(("html",)).get_content()
    assert "<p>Hello</p>" in html and "Sales: the first 10 rows of 1,200." in html


def test_the_subject_and_the_names_write_months_in_the_language_of_the_cell(sent, regions):
    pm.send_pushmail(_conn(), _spec(_att(regions, "regioni_{today:MMMM_yyyy}"), subject="Margine {today:MMMM yyyy}",
                                    lang="it"), now=NOW)
    (msg, _), = sent
    assert msg["Subject"] == "Margine ottobre 2026" and list(_files(msg)) == ["regioni_ottobre_2026.xlsx"]


def test_two_files_with_the_same_name_are_told_apart(sent, sales, regions):
    pm.send_pushmail(_conn(), _spec(_att(sales, "report"), _att(regions, "Report.xlsx")), now=NOW)
    (msg, _), = sent
    assert list(_files(msg)) == ["report.xlsx", "Report (2).xlsx"]


def test_a_name_cannot_put_a_folder_in_the_attachment(sent, regions):
    pm.send_pushmail(_conn(), _spec(_att(regions, "../../etc/passwd")), now=NOW)
    (msg, _), = sent
    assert list(_files(msg)) == ["_.._etc_passwd.xlsx"]


def test_over_the_size_cap_nothing_is_sent_and_the_admins_are_named(sent, sales, monkeypatch):
    monkeypatch.setattr(pm, "MAX_ATTACHMENT_BYTES", 1000)
    with pytest.raises(EmailDestinationError, match="administrators"):
        pm.send_pushmail(_conn(), _spec(_att(sales, "v")), now=NOW)
    assert sent == []


def test_past_the_cap_the_next_files_are_not_written(sent, sales, regions, monkeypatch):
    monkeypatch.setattr(pm, "MAX_ATTACHMENT_BYTES", 1)
    with pytest.raises(EmailDestinationError, match="at file 1 of 2"):
        pm.send_pushmail(_conn(), _spec(_att(sales, "v"), _att(regions, "r")), now=NOW)
    assert sent == []


def test_no_recipients_or_a_bad_placeholder_send_nothing(sent, regions):
    with pytest.raises(EmailDestinationError, match="No recipients"):
        pm.send_pushmail(_conn(), _spec(_att(regions, "r"), to=[" "], cc=[]), now=NOW)
    with pytest.raises(EmailDestinationError):
        pm.send_pushmail(_conn(), _spec(_att(regions, "r_{nonsense:zz}")), now=NOW)
    assert sent == []


def test_an_empty_datasource_is_attached_all_the_same(sent, storage):
    empty = upload_df(storage, pl.DataFrame({"a": pl.Series([], dtype=pl.Int64)}), "datasets/empty.parquet")
    pm.send_pushmail(_conn(), _spec(_att(empty, "vuoto")), now=NOW)
    (msg, _), = sent
    assert list(_files(msg)) == ["vuoto.xlsx"]


@pytest.mark.parametrize("bad", [{"attachments": []}, {"attachments": [{"bucket": "b", "key": "k", "file_name": "x", "max_rows": 0}]},
                                 {"attachments": [{"bucket": "b", "key": "k", "file_name": "x", "max_rows": 2_000_000}]}])
def test_the_spec_is_checked(bad):
    with pytest.raises(ValueError):
        pm.PushmailSpec(to=["a@b.c"], **bad)


# ── reports (docs/specs/reports.md, «Sending») ────────────────────────────────────
@pytest.fixture
def charts(monkeypatch, chart_service):  # noqa: F811
    from app.reports import render

    monkeypatch.setattr(render.Limits, "from_settings", classmethod(lambda cls: cls(chart_url=chart_service[0])))
    return chart_service


def _report(name="Vendite", fmt="pdf", in_body=False, file_name="vendite_{today:yyyy-MM}", rows=300):
    return pm.PushmailReport(name=name, definition=samples.definition(), datasets=samples.datasets(rows),
                             params={"since": "2026-01-01"}, format=fmt, file_name=file_name, in_body=in_body,
                             locale="it")


def test_a_report_is_rendered_and_attached_with_its_summary_in_the_body(sent, regions, charts):
    out = pm.send_pushmail(_conn(), _spec(_att(regions, "regioni"), reports=[
        _report(in_body=True), _report(fmt="xlsx", file_name="vendite.xlsx")]), now=NOW)
    (msg, _), = sent
    files = {p.get_filename(): p for p in msg.iter_attachments()}
    assert list(files) == ["regioni.xlsx", "vendite_2026-10.pdf", "vendite.xlsx"]
    assert files["vendite_2026-10.pdf"].get_content_type() == "application/pdf"
    assert files["vendite_2026-10.pdf"].get_content().startswith(b"%PDF-")
    workbook = files["vendite.xlsx"].get_content()
    assert workbook.startswith(b"PK") and len(pl.read_excel(io.BytesIO(workbook), sheet_id=0)) >= 2
    # the cell's text first, then the report's summary, its charts in the HTML part by cid
    html_part = msg.get_body(("html",))
    html = html_part.get_content()
    assert html.index("in allegato i dati.") < html.index("Totale Regione: Centro")
    cids = re.findall(r'src="cid:([^"]+)"', html)
    related = msg.get_body(("related",))
    images = [p for p in related.iter_parts() if p.get_content_maintype() == "image"]
    assert len(cids) == 2 and sorted(cids) == sorted(p["Content-ID"].strip("<>") for p in images)
    assert all(p.get_content().startswith(b"\x89PNG") for p in images)
    assert [p.get_filename() for p in msg.iter_attachments()] == list(files)  # the charts are not attachments
    plain = msg.get_body(("plain",)).get_content()
    assert plain.startswith("Ciao,\nin allegato i dati.") and "Totale Regione: Centro" in plain
    assert [a["name"] for a in out["attachments"]] == list(files)


def test_a_report_without_datasources_and_an_html_body(sent, charts):
    body = "<html><body><p>Buongiorno</p></body></html>"
    pm.send_pushmail(_conn(), _spec(reports=[_report(in_body=True, file_name="v")], body=body, body_is_html=True,
                                    subject=""), now=NOW)
    (msg, _), = sent
    html = msg.get_body(("html",)).get_content()
    assert html.index("<p>Buongiorno</p>") < html.index("Totale Regione") < html.rindex("</body>")
    assert msg["Subject"] == "v.pdf"


def test_a_report_that_cannot_be_rendered_sends_nothing_and_says_which(sent, monkeypatch, chart_service):  # noqa: F811
    from app.reports import render

    monkeypatch.setattr(render.Limits, "from_settings",
                        classmethod(lambda cls: cls(chart_url=chart_service[0], max_rows=100)))
    with pytest.raises(EmailDestinationError, match="the report «Vendite»: the dataset «orders» has more than 100 rows"):
        pm.send_pushmail(_conn(), _spec(reports=[_report()]), now=NOW)
    assert sent == []


@pytest.mark.parametrize("bad", [{}, {"reports": [_report(in_body=True), _report(in_body=True)]}])
def test_a_spec_with_nothing_to_send_or_two_bodies_is_refused(bad):
    with pytest.raises(ValueError):
        pm.PushmailSpec(to=["a@b.c"], **bad)
