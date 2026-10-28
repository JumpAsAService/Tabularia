"""The Google Sheets source against a fake Google (tests/fake_google.py): the token
asked with the service account's signed JWT, the list of the spreadsheets shared with
it, the reading of a tab (types, dates, empty cells, pages), and every refusal saying
what to do. No call reaches the real Google: there are no credentials in development.
"""
import io
import json
import logging

import polars as pl
import pytest
from fastapi import HTTPException

from app.api.routes import gsheets as route
from app.core.crypto import encrypt_secret
from app.ingest import gsheets as gs
from tests.fake_google import EMAIL, PRIVATE_PEM, PROJECT, TOKEN_URL, Book, FakeGoogle, Tab, service_account_json
from tests.fakes import FakeStorage

SID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcd"
OTHER = "1ZzYyXxWwVvUuTtSsRrQqPpOoNnMmLlKkJjIiHhGgFf"

ORDERS = Tab("Orders", sheet_id=0, rows=[
    ["order_id", "customer_code", "amount", "paid", "ordered_on", "shipped_at", "slot", "discount", "note"],
    [1, "00123", 1250.5, True, 46313, 46313.5, 0.375, 0.15, "first"],
    [2, "00456", 99, False, 46314, 46314.75, 0.5, 0, None],
    [],  # an empty row inside the data: dropped
    [3, "00789", 10, True, None, None, None, 0.05, "#N/A"],
], formats={4: "DATE", 5: "DATE_TIME", 6: "TIME", 7: "PERCENT", 2: "CURRENCY"})


def _books(**tabs):
    return {SID: Book("Sales 2026", [ORDERS, Tab("Notes", sheet_id=77, rows=[["a"], ["x"]])]),
            OTHER: Book("Budget", [Tab("Q1", rows=[["k", "v"], ["a", 1]])]), **tabs}


def _conn(key: str | None = None):
    return gs.GsheetsConnectionSpec(key_encrypted=encrypt_secret(key or service_account_json()),
                                    client_email=EMAIL, project_id=PROJECT)


def _client(google: FakeGoogle, key: str | None = None, sleeps: list | None = None):
    return gs.GoogleClient(_conn(key), http=google.request, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def _read(google, **source):
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return gs.read_tab(_client(google), gs.GsheetsSourceSpec(spreadsheet_id=SID, **source), tmp)


# ── the token ─────────────────────────────────────────────────────────────────────
def test_the_token_is_asked_with_a_signed_jwt_and_read_only_scopes():
    g = FakeGoogle(_books())
    c = _client(g)
    c.metadata(SID)
    c.metadata(SID)
    assert g.tokens == 1  # reused until it is about to expire
    (claims,) = g.assertions
    assert claims["iss"] == EMAIL and claims["aud"] == TOKEN_URL
    assert set(claims["scope"].split()) == {"https://www.googleapis.com/auth/spreadsheets.readonly",
                                            "https://www.googleapis.com/auth/drive.metadata.readonly"}
    assert claims["exp"] - claims["iat"] == 3600


def test_the_endpoints_never_come_from_the_key():
    """`token_uri` in the JSON is the person's: the signed assertion must not go there."""
    g = FakeGoogle(_books())
    _client(g, service_account_json(token_uri="https://attacker.example/token")).metadata(SID)
    assert all(not url.startswith("https://attacker.example") for _, url in g.calls)
    assert {url.split("?")[0].split("/")[2] for _, url in g.calls} <= {
        "oauth2.googleapis.com", "sheets.googleapis.com", "www.googleapis.com"}


def test_a_revoked_key_says_how_to_make_a_new_one():
    with pytest.raises(gs.GsheetsError, match="deleted or revoked.*Keys, add a new JSON key") as e:
        _client(FakeGoogle(_books(), revoked=True)).metadata(SID)
    assert e.value.code == "key" and EMAIL in str(e.value)


def test_a_key_that_is_not_the_accounts_is_refused_by_google():
    from cryptography.hazmat.primitives.asymmetric import rsa

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    with pytest.raises(gs.GsheetsError, match="deleted or revoked"):
        _client(FakeGoogle(_books(), public_key=other)).metadata(SID)


@pytest.mark.parametrize("text, message", [
    ("{not json", "not valid JSON"),
    (json.dumps({"installed": {"client_id": "x"}}), "not a service account key"),
    (service_account_json(private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"), "cannot be read"),
])
def test_a_broken_key_is_explained(text, message):
    with pytest.raises(gs.GsheetsError, match=message):
        _client(FakeGoogle(_books()), text).metadata(SID)


def test_the_private_key_is_never_in_a_message_nor_in_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    g = FakeGoogle(_books(), shared=set())
    with pytest.raises(gs.GsheetsError) as e:
        _client(g).metadata(SID)
    body = PRIVATE_PEM.splitlines()[1]
    assert body not in str(e.value) and body not in caplog.text and "PRIVATE KEY" not in caplog.text


# ── what the user pastes ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [
    (f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=77", (SID, 77)),
    (f"https://docs.google.com/spreadsheets/u/1/d/{SID}/edit?usp=sharing", (SID, None)),
    (f"https://docs.google.com/spreadsheets/d/{SID}/edit?gid=0#gid=0", (SID, 0)),
    (f"  {SID} ", (SID, None)),
])
def test_a_link_or_an_id(text, expected):
    assert gs.spreadsheet_ref(text) == expected


@pytest.mark.parametrize("text, message", [
    ("", "paste the spreadsheet's link"),
    (f"https://drive.google.com/file/d/{SID}/view", "Save as Google Sheets"),
    ("budget 2026", "neither a spreadsheet's link nor its ID"),
])
def test_what_is_not_a_spreadsheet_is_explained(text, message):
    with pytest.raises(gs.GsheetsError, match=message):
        gs.spreadsheet_ref(text)


@pytest.mark.parametrize("text, expected", [
    ("", None), ("A1:F500", (1, 1, 6, 500)), ("b3:h", (2, 3, 8, None)), ("A:F", (1, 1, 6, None)),
    ("AA10:AB20", (27, 10, 28, 20)),
])
def test_ranges(text, expected):
    assert gs.parse_range(text) == expected


@pytest.mark.parametrize("text, message", [
    ("Orders!A1:B2", "names a tab"), ("A1", "not a range"), ("F1:A9", "ends before it starts"), ("A10:B2", "ends before"),
])
def test_bad_ranges(text, message):
    with pytest.raises(gs.GsheetsError, match=message):
        gs.parse_range(text)


# ── the table ─────────────────────────────────────────────────────────────────────
def test_cells_become_typed_columns():
    df, info = _read(FakeGoogle(_books()), sheet_id=0)
    assert df.columns == ORDERS.rows[0]
    s = df.schema
    assert s["order_id"] == pl.Int64 and s["amount"] == pl.Float64 and s["paid"] == pl.Boolean
    # text in Sheets stays text: the leading zeros of a code are not lost
    assert s["customer_code"] == pl.String and df["customer_code"].to_list() == ["00123", "00456", "00789"]
    # cells formatted as dates come back as dates, not as serial numbers
    assert s["ordered_on"] == pl.Date and str(df["ordered_on"][0]) == "2026-10-18"
    assert s["shipped_at"] == pl.Datetime and str(df["shipped_at"][1]) == "2026-10-19 18:00:00"
    assert s["slot"] == pl.Time and str(df["slot"][0]) == "09:00:00"
    # a percentage is its fraction, as Sheets holds it
    assert df["discount"].to_list() == [0.15, 0.0, 0.05]
    # the empty row is dropped, empty cells are NULL, a formula error is NULL and counted
    assert df.height == 3 and df["note"].to_list() == ["first", None, None]
    assert df["ordered_on"][2] is None and info["formula_errors"] == 1
    assert info["spreadsheet"] == {"id": SID, "title": "Sales 2026"} and info["sheet"] == {"id": 0, "title": "Orders"}


def test_a_mixed_column_goes_through_the_csv_inference():
    tab = Tab("Mix", rows=[["n", "m", "flag"], [1, "n/a", True], ["2", 3, "yes"], [4.5, 5, False]])
    df, _ = _read(FakeGoogle({SID: Book("B", [tab])}), sheet="Mix")
    # numbers and a number typed as text: a number column; numbers and words: text
    assert df.schema["n"] == pl.Float64 and df["n"].to_list() == [1.0, 2.0, 4.5]
    assert df.schema["m"] == pl.String and df["m"].to_list() == ["n/a", "3", "5"]
    assert df.schema["flag"] == pl.String


def test_a_duration_over_a_day_stays_a_number_of_days():
    tab = Tab("T", rows=[["took"], [1.5], [0.25]], formats={0: "TIME"})
    df, _ = _read(FakeGoogle({SID: Book("B", [tab])}))
    assert df["took"].to_list() == [1.5, 0.25]


def test_a_number_formatted_as_a_date_out_of_range_stays_a_number():
    tab = Tab("T", rows=[["d"], [46313], [99_999_999]], formats={0: "DATE"})
    df, _ = _read(FakeGoogle({SID: Book("B", [tab])}))
    assert df.schema["d"] == pl.String and df["d"].to_list() == ["2026-10-18", "99999999"]


def test_a_range_reads_only_its_cells_and_its_first_row_is_the_header():
    tab = Tab("R", rows=[["REPORT 2026"], [], ["x", "code", "value", "y"], ["-", "A", 1, "-"], ["-", "B", 2, "-"]])
    df, info = _read(FakeGoogle({SID: Book("B", [tab])}), range="B3:C")
    assert df.columns == ["code", "value"] and df.rows() == [("A", 1), ("B", 2)]
    assert info["range"].startswith("B3:C")


def test_a_renamed_tab_is_found_by_its_id():
    books = _books()
    books[SID].tabs[1].title = "Notes (old)"
    df, info = _read(FakeGoogle(books), sheet_id=77, sheet="Notes")
    assert info["sheet"]["title"] == "Notes (old)" and df.columns == ["a"]


def test_without_a_tab_the_first_one_is_read_and_a_title_is_matched():
    assert _read(FakeGoogle(_books()))[1]["sheet"]["title"] == "Orders"
    assert _read(FakeGoogle(_books()), sheet="notes")[1]["sheet"]["id"] == 77


def test_a_missing_tab_lists_the_tabs():
    with pytest.raises(gs.GsheetsError, match="'Gone' is not in the spreadsheet 'Sales 2026'.*'Orders', 'Notes'"):
        _read(FakeGoogle(_books()), sheet_id=5, sheet="Gone")


def test_a_chart_tab_is_refused():
    with pytest.raises(gs.GsheetsError, match="is a chart"):
        _read(FakeGoogle({SID: Book("B", [Tab("Chart1", chart=True)])}))


@pytest.mark.parametrize("rows, message", [
    ([[], ["a", "b"], [1, 2]], "first row of the tab 'T' is empty"),
    ([["a", "", "c"], [1, 2, 3]], "incomplete header"),
    ([["a", "A"], [1, 2]], "repeated column names"),
    ([["a", "b"], [1, 2, "stray"]], "row 2 of the tab 'T' has a value in column C"),
    ([], "is empty"),
])
def test_what_is_not_machine_readable_is_refused(rows, message):
    with pytest.raises(gs.GsheetsError, match=message):
        _read(FakeGoogle({SID: Book("B", [Tab("T", rows=rows)])}))


def test_a_big_tab_is_read_in_pages(monkeypatch):
    monkeypatch.setattr(gs, "CELLS_PER_PAGE", 300)  # 3 columns → pages of 100 rows
    rows = [["id", "name", "value"]] + [[i, f"n{i}", i * 1.5] for i in range(1, 451)]
    g = FakeGoogle({SID: Book("Big", [Tab("Data", rows=rows, grid_rows=1000, grid_columns=3)])})
    df, _ = _read(g)
    reads = [u for _, u in g.calls if "/values/" in u]
    assert len(reads) == 10  # 1000 grid rows / 100 (the empty pages at the end included)
    assert df.height == 450 and df["id"].to_list() == list(range(1, 451))
    assert df.schema["value"] == pl.Float64


# ── Google's refusals ─────────────────────────────────────────────────────────────
def test_a_spreadsheet_not_shared_says_with_whom_to_share_it():
    with pytest.raises(gs.GsheetsError, match=f"share the spreadsheet with {EMAIL}") as e:
        _read(FakeGoogle(_books(), shared={OTHER}))
    assert e.value.code == "no_access"


def test_a_spreadsheet_that_does_not_exist():
    with pytest.raises(gs.GsheetsError, match="was not found: check the link") as e:
        _client(FakeGoogle(_books())).metadata("1" * 44)
    assert e.value.code == "not_found"


def test_the_sheets_api_not_enabled_says_where_to_enable_it():
    with pytest.raises(gs.GsheetsError, match=f"enable the Google Sheets API in the Google Cloud project {PROJECT}") as e:
        _read(FakeGoogle(_books(), sheets_api=False))
    assert f"sheets.googleapis.com?project={PROJECT}" in str(e.value)


def test_an_excel_file_in_drive_is_explained():
    with pytest.raises(gs.GsheetsError, match="Excel file stored in Drive.*Save as Google Sheets"):
        _read(FakeGoogle({SID: Book("old.xlsx", [Tab("S")], excel=True)}))


def test_a_quota_is_waited_out_as_google_asks():
    g = FakeGoogle(_books())
    g.throttle_next = 2
    sleeps: list[float] = []
    assert _client(g, sleeps=sleeps).metadata(SID)["title"] == "Sales 2026"
    assert sleeps == [1.0, 1.0]  # Retry-After


def test_a_quota_that_does_not_end_says_so():
    g = FakeGoogle(_books())
    g.throttle_next = 99
    with pytest.raises(gs.GsheetsError, match="quota of read requests") as e:
        _client(g).metadata(SID)
    assert e.value.code == "quota"


def test_errors_carry_the_prefix_for_the_run():
    with pytest.raises(gs.GsheetsError) as e:
        _read(FakeGoogle(_books(), shared=set()))
    assert str(e.value).startswith("Google Sheets: ")


# ── the connection test and the list ─────────────────────────────────────────────
def test_the_test_counts_the_spreadsheets_shared_with_the_account():
    out = gs.test_connection(_client(FakeGoogle(_books())))
    assert out == {"ok": True, "client_email": EMAIL, "project_id": PROJECT, "drive": True, "spreadsheets": 2, "more": False}


def test_the_test_fails_when_the_sheets_api_is_off():
    with pytest.raises(gs.GsheetsError, match="enable the Google Sheets API"):
        gs.test_connection(_client(FakeGoogle(_books(), sheets_api=False)))


def test_the_test_passes_without_drive_and_says_the_list_is_missing():
    out = gs.test_connection(_client(FakeGoogle(_books(), drive_api=False)))
    assert out["ok"] and out["drive"] is False and out["spreadsheets"] is None


def test_the_test_fails_with_a_revoked_key():
    with pytest.raises(gs.GsheetsError, match="revoked"):
        gs.test_connection(_client(FakeGoogle(_books(), revoked=True)))


def test_the_list_follows_drives_pages_and_is_in_name_order():
    books = {f"{i:044d}": Book(f"Sheet {chr(90 - i)}", [Tab("A")]) for i in range(7)}
    files, more = _client(FakeGoogle(books, drive_page_size=3)).spreadsheets()
    assert [f["name"] for f in files] == sorted(f"Sheet {chr(90 - i)}" for i in range(7)) and not more
    assert files[0]["owner"] == "Anna Rossi"


def test_the_tabs_of_a_link_preselect_its_tab():
    out = gs.tabs(_client(FakeGoogle(_books())), f"https://docs.google.com/spreadsheets/d/{SID}/edit#gid=77")
    assert out["title"] == "Sales 2026" and [t["title"] for t in out["tabs"]] == ["Orders", "Notes"]
    assert out["selected_sheet_id"] == 77


def test_the_preview_shows_the_types_of_the_first_rows():
    out = gs.preview(_client(FakeGoogle(_books())), gs.GsheetsSourceSpec(spreadsheet_id=SID, sheet_id=0), limit=2)
    assert [c["dtype"] for c in out["columns"]][:4] == ["Int64", "String", "Float64", "Boolean"]
    assert len(out["rows"]) == 2 and out["rows"][0][4] == "2026-10-18"


# ── the import ────────────────────────────────────────────────────────────────────
def test_the_import_writes_the_parquet():
    st = FakeStorage()
    res = gs.ingest_gsheets_to_parquet(_conn(), gs.GsheetsSourceSpec(spreadsheet_id=SID, sheet_id=0), "b",
                                       "datasets/x.parquet", st, client=_client(FakeGoogle(_books())))
    df = pl.read_parquet(io.BytesIO(st.blobs[("b", "datasets/x.parquet")]))
    assert res["status"] == "success" and res["rows_written"] == 3 == df.height
    assert {c["name"]: c["dtype"] for c in res["columns"]}["ordered_on"] == "Date"
    assert res["sheet"]["title"] == "Orders" and res["formula_errors"] == 1


def test_the_progress_hears_the_rows_and_the_saving():
    heard = []
    gs.ingest_gsheets_to_parquet(_conn(), gs.GsheetsSourceSpec(spreadsheet_id=SID), "b", "k", FakeStorage(),
                                 client=_client(FakeGoogle(_books())), progress=lambda p, **c: heard.append((p, c)))
    assert ("reading", {"rows": 3}) in heard and heard[-1] == ("saving", {"rows": 3})


# ── the engine's routes ───────────────────────────────────────────────────────────
@pytest.fixture
def google(monkeypatch):
    g = FakeGoogle(_books())
    real = gs.GoogleClient
    monkeypatch.setattr(gs, "GoogleClient", lambda conn: real(conn, http=g.request, sleep=lambda s: None))
    return g


def _conn_body():
    return {"key_encrypted": encrypt_secret(service_account_json()), "client_email": EMAIL, "project_id": PROJECT}


def test_the_inspect_route_answers_every_action(google):
    req = route.GsheetsInspectRequest
    assert route.inspect(req(connection=_conn_body(), action="test"))["spreadsheets"] == 2
    assert len(route.inspect(req(connection=_conn_body(), action="spreadsheets"))["spreadsheets"]) == 2
    assert route.inspect(req(connection=_conn_body(), action="tabs", spreadsheet=SID))["tabs"][1]["sheet_id"] == 77
    out = route.inspect(req(connection=_conn_body(), action="preview", source={"spreadsheet_id": SID, "sheet": "Notes"}))
    assert out["columns"] == [{"name": "a", "dtype": "String"}]


def test_the_inspect_route_refuses_with_a_400_and_never_the_key(google):
    google.shared = set()
    with pytest.raises(HTTPException) as e:
        route.inspect(route.GsheetsInspectRequest(connection=_conn_body(), action="tabs", spreadsheet=SID))
    assert e.value.status_code == 400 and e.value.detail.startswith("Google Sheets: ") and EMAIL in e.value.detail
    assert "PRIVATE KEY" not in e.value.detail


def test_the_ingest_route_queues_the_task(monkeypatch):
    from app.tasks import jobs

    sent = {}
    monkeypatch.setattr(jobs, "ingest_gsheets_task", type("T", (), {"delay": staticmethod(
        lambda **kw: sent.update(kw) or type("R", (), {"id": "t-1"})())}))
    out = route.ingest(route.GsheetsIngestRequest(connection=_conn_body(), source={"spreadsheet_id": SID, "sheet_id": 0},
                                                  bucket="b", output_key="datasets/1.parquet"))
    assert out.task_id == "t-1" and sent["source"]["sheet_id"] == 0 and "contract" not in sent
    assert "PRIVATE KEY" not in json.dumps(sent)  # the key travels encrypted
