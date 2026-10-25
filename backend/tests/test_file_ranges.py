"""Placeholders and variables in the folder and glob of the file datasources
(SFTP, SharePoint), and their append-by-key refresh (app.ingest.file_templates).

Fixed here: one expansion per value (and per combination with lists), each file
read once, `_param_<name>` on every row, `_file` below the folder's fixed part;
a value without its file is skipped and LISTED; no file at all across the range
fails the refresh; append mode replaces the rows of keys that come again.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timezone

import polars as pl
import pytest

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.ingest import sftp
from app.ingest import sharepoint_source as sp
from tests.fake_graph import GRAPH, LOGIN, FakeGraph, xlsx
from tests.fake_sftp import FakeSftpServer
from tests.fakes import FakeStorage

NOW = datetime(2026, 10, 17, 22, 30, 5, tzinfo=timezone.utc)  # already the 18th in Rome
DAYS = {"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}


def _write(root, rel, data: str):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(data)


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "srv"
    d.mkdir()
    return str(d)


@pytest.fixture
def srv(root):
    with FakeSftpServer(root) as s:
        yield s


def _conn(srv):
    return sftp.SftpConnectionSpec(host="127.0.0.1", port=srv.port, username="tab", password_encrypted=encrypt_secret("pw"),
                                   host_key_type=srv.host_key.get_name(),
                                   host_key_fingerprint=sftp.fingerprint(srv.host_key), timeout_seconds=10)


def _ingest(srv, storage=None, previous=None, last_refresh=None, **source):
    st = storage or FakeStorage()
    res = sftp.ingest_sftp_to_parquet(_conn(srv), sftp.SftpSourceSpec(**source), "b", "out.parquet", st, now=NOW,
                                      previous=previous, last_refresh=last_refresh)
    return res, pl.read_parquet(io.BytesIO(st.blobs[("b", "out.parquet")]))


# ── SFTP ──────────────────────────────────────────────────────────────────────
def test_a_range_reads_each_day_and_lists_the_missing_one(srv, root):
    _write(root, "in/sales_20261014.csv", "id,v\n1,10\n")
    _write(root, "in/sales_20261016.csv", "id,v\n2,20\n3,30\n")
    _write(root, "in/sales_20261017.csv", "id,v\n9,90\n")  # today: outside the range (the end is excluded)
    res, df = _ingest(srv, folder="in", pattern="sales_{day:yyyyMMdd}.csv", variables=[DAYS])
    assert df.columns == ["id", "v", "_param_day", "_file", "_file_modified_at"]
    assert df.select("id", "_param_day", "_file").rows() == [
        (1, "2026-10-14", "sales_20261014.csv"), (2, "2026-10-16", "sales_20261016.csv"),
        (3, "2026-10-16", "sales_20261016.csv")]
    assert res["missing"] == [{"params": {"day": "2026-10-15"}, "pattern": "in/sales_20261015.csv"}]
    assert res["expansions"] == 3


def test_no_file_at_all_across_the_range_fails(srv, root):
    os.makedirs(os.path.join(root, "in"))
    with pytest.raises(sftp.SftpError, match=r"No file matches .* any of the 3 values .*sales_20261014\.csv"):
        _ingest(srv, folder="in", pattern="sales_{day:yyyyMMdd}.csv", variables=[DAYS])


def test_the_zone_of_the_datasource_says_which_day_it_is(srv, root):
    _write(root, "in/d_2026-10-18.csv", "id\n1\n")
    res, df = _ingest(srv, folder="in", pattern="d_{today}.csv", timezone="Europe/Rome")
    assert df["_file"].to_list() == ["d_2026-10-18.csv"] and res["missing"] == []


def test_a_dated_folder_missing_is_a_missing_value_and_file_is_below_the_fixed_part(srv, root):
    _write(root, "in/2026-10-14/a.csv", "id\n1\n")
    _write(root, "in/2026-10-16/b.csv", "id\n2\n")
    res, df = _ingest(srv, folder="in/{day}", pattern="*.csv", variables=[DAYS])
    assert df["_file"].to_list() == ["2026-10-14/a.csv", "2026-10-16/b.csv"]
    assert [m["params"]["day"] for m in res["missing"]] == ["2026-10-15"]


def test_a_static_folder_that_does_not_exist_is_still_an_error(srv, root):
    with pytest.raises(sftp.SftpError, match="not found"):
        _ingest(srv, folder="nope", pattern="sales_{day:yyyyMMdd}.csv", variables=[DAYS])


def test_two_values_matching_the_same_file_read_it_once(srv, root):
    _write(root, "in/sales_1.csv", "id\n1\n")
    res, df = _ingest(srv, folder="in", pattern="{p}*.csv",
                      variables=[{"name": "p", "values": ["sales", "sal", "nothing"]}])
    assert df.select("id", "_param_p").rows() == [(1, "sales")]
    assert res["missing"] == [{"params": {"p": "nothing"}, "pattern": "in/nothing*.csv"}]


def test_a_list_and_a_range_make_every_combination(srv, root):
    _write(root, "in/MI_2026-10-15.csv", "id\n1\n")
    _write(root, "in/RM_2026-10-16.csv", "id\n2\n")
    res, df = _ingest(srv, folder="in", pattern="{branch}_{day}.csv",
                      variables=[{"name": "branch", "values": ["MI", "RM"]},
                                 {"name": "day", "type": "range", "start": "today-2", "end": "today", "step": "1d"}])
    assert df.select("_param_branch", "_param_day").rows() == [("MI", "2026-10-15"), ("RM", "2026-10-16")]
    assert len(res["missing"]) == 2


def test_last_refresh_in_a_pattern(srv, root):
    _write(root, "in/since_2026-10-12.csv", "id\n1\n")
    _, df = _ingest(srv, folder="in", pattern="since_{last_refresh:yyyy-MM-dd}.csv", last_refresh_initial="today-5")
    assert df.height == 1
    _, df = _ingest(srv, folder="in", pattern="since_{last_refresh:yyyy-MM-dd}.csv", last_refresh_initial="today-1",
                    last_refresh="2026-10-12T04:00:00Z")
    assert df.height == 1


def test_append_by_key_replaces_the_days_read_again(srv, root):
    st = FakeStorage()
    _write(root, "in/s_2026-10-14.csv", "id,v\n1,10\n")
    _write(root, "in/s_2026-10-15.csv", "id,v\n2,20\n")
    spec = dict(folder="in", pattern="s_{day}.csv", mode="append", keys=["_param_day", "id"],
                variables=[{"name": "day", "type": "range", "start": "today-3", "end": "today-1", "step": "1d"}])
    first, df1 = _ingest(srv, storage=st, **spec)
    assert sorted(df1["id"].to_list()) == [1, 2]
    st.blobs[("b", "prev.parquet")] = st.blobs[("b", "out.parquet")]
    # the next night: the 15th corrected, the 16th new
    _write(root, "in/s_2026-10-15.csv", "id,v\n2,21\n")
    _write(root, "in/s_2026-10-16.csv", "id,v\n3,30\n")
    spec["variables"] = [{"name": "day", "type": "range", "start": "today-2", "end": "today", "step": "1d"}]
    res, df = _ingest(srv, storage=st, previous={"bucket": "b", "key": "prev.parquet"}, **spec)
    assert sorted(df.select("id", "v").rows()) == [(1, 10), (2, 21), (3, 30)]
    assert res["rows_replaced"] == 1 and res["mode"] == "append"
    assert df.columns[-2:] == ["_file", "_file_modified_at"]


def test_append_without_keys_is_refused(srv, root):
    with pytest.raises(sftp.SftpError, match="key"):
        sftp.validate_source(sftp.SftpSourceSpec(folder="in", pattern="*.csv", mode="append"), now=NOW)


@pytest.mark.parametrize("kw, message", [
    (dict(pattern="s_{tomorrow}.csv"), "unknown placeholder"),
    (dict(pattern="*.csv", variables=[DAYS]), "not used"),
    (dict(pattern="{day}.csv", variables=[{**DAYS, "end": "today-5"}]), "empty"),
    (dict(pattern="s_{last_refresh}.csv"), "initial"),
    (dict(pattern="s.csv", timezone="Mars/Base"), "time zone"),
    (dict(folder="in/{today", pattern="*.csv"), "not closed"),
])
def test_validation_refuses_what_cannot_work(kw, message):
    with pytest.raises(sftp.SftpError, match=message):
        sftp.validate_source(sftp.SftpSourceSpec(**kw), now=NOW)


def test_the_values_are_capped(monkeypatch):
    monkeypatch.setattr(get_settings().placeholders, "max_values", 2)
    with pytest.raises(sftp.SftpError, match="more than 2 values"):
        sftp.validate_source(sftp.SftpSourceSpec(pattern="{day}.csv", variables=[DAYS]), now=NOW)


def test_validation_shows_the_first_expansion():
    out = sftp.validate_source(sftp.SftpSourceSpec(folder="in/{day:yyyy}", pattern="s_{day}.csv", variables=[DAYS]),
                               now=NOW)
    assert out == {"ok": True, "expansions": 3, "first": {"folder": "in/2026", "pattern": "s_2026-10-14.csv"},
                   "last": {"folder": "in/2026", "pattern": "s_2026-10-16.csv"}}


def test_check_the_files_lists_them_with_the_missing_values(srv, root):
    _write(root, "in/sales_20261014.csv", "id\n1\n")
    out = sftp.check_files(_conn(srv), sftp.SftpSourceSpec(folder="in", pattern="sales_{day:yyyyMMdd}.csv",
                                                           variables=[DAYS]), now=NOW)
    assert [f.rel for f in out.files] == ["sales_20261014.csv"] and len(out.missing) == 2


def test_without_placeholders_nothing_changes(srv, root):
    _write(root, "in/{x}.csv", "id\n1\n")  # braces in a real name: a literal file name is written {{x}}
    _, df = _ingest(srv, folder="in", pattern="{{x}}.csv")
    assert df["_file"].to_list() == ["{x}.csv"]


# ── SharePoint ────────────────────────────────────────────────────────────────
DATA = xlsx([["id", "importo"], [1, 10.5]])


def _sp_ingest(files, previous=None, storage=None, **source):
    graph = FakeGraph(files)
    conn = sp.SharePointConnectionSpec(tenant_id="T", client_id="C", client_secret_encrypted=encrypt_secret("s3gr3t0"),
                                       site_url="https://contoso.sharepoint.com/sites/Finance", graph_base=GRAPH,
                                       login_base=LOGIN)
    st = storage or FakeStorage()
    res = sp.ingest_sharepoint_to_parquet(conn, sp.SharePointSourceSpec(**source), "b", "x.parquet", st,
                                          client=sp.GraphClient(conn, http=graph.request, sleep=lambda s: None),
                                          now=NOW, previous=previous)
    return res, pl.read_parquet(io.BytesIO(st.blobs[("b", "x.parquet")]))


def test_sharepoint_a_range_of_monthly_files_with_one_missing():
    files = {"Chiusure/2026-07.xlsx": DATA, "Chiusure/2026-09.xlsx": DATA}
    res, df = _sp_ingest(files, path="Chiusure/{month:yyyy-MM}.xlsx", sheet="Dati",
                         variables=[{"name": "month", "type": "range", "start": "start_of_month-3m",
                                     "end": "start_of_month", "step": "1m"}])
    assert df.select("_param_month", "_file").rows() == [("2026-07-01", "Chiusure/2026-07.xlsx"),
                                                         ("2026-09-01", "Chiusure/2026-09.xlsx")]
    assert res["missing"] == [{"params": {"month": "2026-08-01"}, "pattern": "Chiusure/2026-08.xlsx"}]


def test_sharepoint_no_file_across_the_range_fails():
    with pytest.raises(sp.SharePointError, match="any of the 3 values"):
        _sp_ingest({"Altro/x.xlsx": DATA}, path="Chiusure/{day}.xlsx", sheet="Dati", variables=[DAYS])


def test_sharepoint_append_by_key():
    st = FakeStorage()
    _sp_ingest({"C/a.xlsx": xlsx([["id", "v"], [1, 10], [2, 20]])}, storage=st, path="C/*.xlsx", sheet="Dati",
               mode="append", keys=["id"])
    st.blobs[("b", "p.parquet")] = st.blobs[("b", "x.parquet")]
    res, df = _sp_ingest({"C/a.xlsx": xlsx([["id", "v"], [2, 21], [3, 30]])}, storage=st, path="C/*.xlsx",
                         sheet="Dati", mode="append", keys=["id"], previous={"bucket": "b", "key": "p.parquet"})
    assert sorted(df.select("id", "v").rows()) == [(1, 10), (2, 21), (3, 30)] and res["rows_replaced"] == 1


def test_sharepoint_validation():
    with pytest.raises(sp.SharePointError, match="not used"):
        sp.validate_source(sp.SharePointSourceSpec(path="C/*.xlsx", sheet="Dati", variables=[DAYS]), now=NOW)
    assert sp.validate_source(sp.SharePointSourceSpec(path="C/{today}.xlsx", sheet="Dati"), now=NOW)["first"] == {
        "path": "C/2026-10-17.xlsx"}


def test_append_keyed_by_the_file_replaces_all_its_rows_and_keeps_them_all(srv, root):
    """Keyed by `_file` (or by `_param_day`): a file read again replaces ALL the old
    rows of that file with ALL its new rows — never one row per key."""
    st = FakeStorage()
    _write(root, "in/a.csv", "id\n1\n2\n")
    _write(root, "in/b.csv", "id\n3\n")
    _ingest(srv, storage=st, folder="in", pattern="*.csv", mode="append", keys=["_file"])
    st.blobs[("b", "prev.parquet")] = st.blobs[("b", "out.parquet")]
    os.remove(os.path.join(root, "in/a.csv"))
    _write(root, "in/b.csv", "id\n10\n11\n12\n")
    res, df = _ingest(srv, storage=st, previous={"bucket": "b", "key": "prev.parquet"}, folder="in", pattern="*.csv",
                      mode="append", keys=["_file"])
    assert sorted(df.select("id", "_file").rows()) == [(1, "a.csv"), (2, "a.csv"), (10, "b.csv"), (11, "b.csv"),
                                                       (12, "b.csv")]
    assert res["rows_replaced"] == 1


def test_append_says_how_many_keys_repeat_in_the_new_rows(srv, root):
    """Keyed without `_file`, a key on several new rows is kept (a key is a group) but
    counted: the run says it, since it may be a key that does not identify a row."""
    _write(root, "in/a.csv", "id,v\n1,10\n1,11\n2,20\n")
    _write(root, "in/b.csv", "id,v\n2,21\n3,30\n")
    res, df = _ingest(srv, folder="in", pattern="*.csv", mode="append", keys=["id"])
    assert df.height == 5 and res["duplicate_keys"] == 2
    res, _ = _ingest(srv, folder="in", pattern="*.csv", mode="append", keys=["_file", "id"])
    assert res["duplicate_keys"] == 0  # with `_file` in the key, rows of one file are its group: not reported
