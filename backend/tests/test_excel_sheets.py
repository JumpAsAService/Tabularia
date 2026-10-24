"""The sheets of an uploaded workbook: listed in the answer, the first visible one read
when none is chosen, a chosen one read, a missing or empty one refused saying so."""
from __future__ import annotations

import polars as pl
import pytest
import xlsxwriter

from app.ingest.converters import IngestError
from app.ingest.models import IngestOptions
from app.ingest.service import IngestService
from app.ingest.sheets import Sheet, default_sheet, sheets_of_first, workbook_sheets
from tests.fake_graph import FakeGraph, workbook


def _workbook(path, hidden_first=False):
    wb = xlsxwriter.Workbook(str(path))
    if hidden_first:
        h = wb.add_worksheet("Lookup")
        h.write_row(0, 0, ["code", "label"])
        h.write_row(1, 0, [1, "uno"])
        h.hide()
    a = wb.add_worksheet("Istruzioni")
    a.write(0, 0, "Leggimi prima")
    a.activate()  # the sheet open in Excel cannot be the hidden one
    d = wb.add_worksheet("Dati")
    d.write_row(0, 0, ["regione", "importo"])
    d.write_row(1, 0, ["Nord", 10])
    d.write_row(2, 0, ["Sud", 5])
    wb.add_worksheet("Vuoto")
    wb.close()
    return str(path)


@pytest.fixture
def service(storage):
    return IngestService(storage=storage)


def test_the_sheets_are_listed_and_the_first_visible_is_read(tmp_path, service):
    path = _workbook(tmp_path / "budget.xlsx", hidden_first=True)
    assert workbook_sheets(path) == [Sheet("Lookup", True), Sheet("Istruzioni"), Sheet("Dati"), Sheet("Vuoto")]
    res = service.ingest_local(path, "budget.xlsx", IngestOptions())
    assert res.sheets == ["Lookup", "Istruzioni", "Dati", "Vuoto"]
    assert res.hidden_sheets == ["Lookup"]
    assert res.sheet == "Istruzioni"
    assert [c.name for c in res.dataset.columns] == ["Leggimi prima"]


def test_a_chosen_sheet_is_read(tmp_path, service):
    path = _workbook(tmp_path / "budget.xlsx")
    res = service.ingest_local(path, "budget.xlsx", IngestOptions(sheet="Dati"))
    assert res.sheet == "Dati" and res.dataset.rows == 2
    assert [c.name for c in res.dataset.columns] == ["regione", "importo"]


def test_a_hidden_sheet_can_be_chosen(tmp_path, service):
    path = _workbook(tmp_path / "budget.xlsx", hidden_first=True)
    res = service.ingest_local(path, "budget.xlsx", IngestOptions(sheet="Lookup"))
    assert res.sheet == "Lookup" and res.dataset.rows == 1


@pytest.mark.parametrize("sheet, message", [("Manca", "no sheet «Manca»"), ("Vuoto", "«Vuoto» is empty")])
def test_a_missing_or_empty_sheet_is_refused_saying_so(tmp_path, service, sheet, message):
    path = _workbook(tmp_path / "budget.xlsx")
    with pytest.raises(IngestError, match=message):
        service.ingest_local(path, "budget.xlsx", IngestOptions(sheet=sheet))


def test_other_files_carry_no_sheets(tmp_path, service):
    p = tmp_path / "a.csv"
    p.write_text("x,y\n1,2\n")
    res = service.ingest_local(str(p), "a.csv", IngestOptions())
    assert res.sheets is None and res.sheet is None


def test_a_file_that_is_not_a_workbook_lists_nothing(tmp_path):
    p = tmp_path / "finto.xlsx"
    p.write_text("not a workbook")
    assert workbook_sheets(str(p)) == []
    assert default_sheet([]) is None
    assert default_sheet([Sheet("A", True)]) == "A"  # all hidden: the first one still


# ── «Check the files» of SFTP, S3 folders and SharePoint ─────────────────────
BOOK = workbook({"Lookup": [["k"], [1]], "Dati": [["id"], [1]], "Note": [["x"], ["y"]]}, hidden=("Lookup",))
SHEETS = {"sheets": ["Lookup", "Dati", "Note"], "hidden": ["Lookup"]}


def test_the_sheets_of_the_first_workbook_taken(tmp_path):
    files = [("a.csv", b"id\n1\n"), ("big.xlsx", b"x" * 10), ("m/budget.xlsx", BOOK)]

    def fetch(f, local):
        with open(local, "wb") as out:
            out.write(f[1])

    # the first workbook, unless it is too big to download for a list
    assert sheets_of_first(files[2:], lambda f: f[0], lambda f: len(f[1]), fetch, lambda f: f[0]) == {"file": "m/budget.xlsx", **SHEETS}
    import app.ingest.sheets as sh

    out = sheets_of_first(files, lambda f: f[0], lambda f: len(f[1]) if f[0] != "big.xlsx" else sh.PEEK_BYTES + 1, fetch, lambda f: f[0])
    assert out == {"file": "m/budget.xlsx", **SHEETS}
    # no workbook, or one that cannot be read or fetched: no list, and no error
    assert sheets_of_first(files[:1], lambda f: f[0], lambda f: 1, fetch, lambda f: f[0]) is None
    assert sheets_of_first(files[1:2], lambda f: f[0], lambda f: 1, fetch, lambda f: f[0]) is None

    def broken(f, local):
        raise OSError("gone")

    assert sheets_of_first(files[2:], lambda f: f[0], lambda f: 1, broken, lambda f: f[0]) is None


def test_sftp_check_the_files_lists_the_sheets(tmp_path):
    from app.ingest import sftp
    from tests.fake_sftp import FakeSftpServer
    from tests.test_sftp import _conn, _write

    root = str(tmp_path)
    _write(root, "in/budget_2026.xlsx", BOOK)
    _write(root, "in/note.csv", "id\n1\n")
    with FakeSftpServer(root) as srv:
        g = sftp.check_files(_conn(srv), sftp.SftpSourceSpec(folder="in", pattern="*"))
        assert g.sheets == {"file": "budget_2026.xlsx", **SHEETS}
        g = sftp.check_files(_conn(srv), sftp.SftpSourceSpec(folder="in", pattern="*", format="csv"))
        assert g.sheets is None  # read as CSV: no sheets to choose


def test_s3_check_the_files_lists_the_sheets(monkeypatch):
    from app.ingest import s3_source as s3
    from tests.test_s3_source import FakeS3, conn

    fake = FakeS3({"in/budget.xlsx": BOOK, "in/a.csv": b"id\n1\n"})
    monkeypatch.setattr(type(conn()), "client", lambda self: fake)
    g = s3.check_files(conn(), s3.S3SourceSpec(folder="in", pattern="*"))
    assert g.sheets == {"file": "budget.xlsx", **SHEETS}


def test_sharepoint_check_the_files_lists_the_sheets():
    from app.ingest import sharepoint_source as sp
    from tests.test_sharepoint_source import _client

    client = _client(FakeGraph({"Budget/2026/gen.xlsx": BOOK}))
    g, _ = sp.gather_files(client, sp.SharePointSourceSpec(path="Budget/2026/*.xlsx"))
    out = sheets_of_first(g.files, lambda f: f.path, lambda f: f.size, lambda f, local: client.download(f.id, local), lambda f: f.path)
    assert out == {"file": "Budget/2026/gen.xlsx", **SHEETS}
