"""The «S3 folder» datasource: the files of a bucket's folder read and stacked
(app.ingest.s3_source), against an in-memory S3 client.

Fixed here: the glob over keys (`*`, `?`, `**`, fixed leading folders narrowing
the listing), the formats of the SFTP datasource, `_file` and
`_file_modified_at` on every row, the caps checked on the listing BEFORE any
download, nothing outside the configured bucket and folder, placeholders and
ranges (a missing day listed, none at all fails), append by key, and messages
that never hold a secret.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import polars as pl
import pytest
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.ingest import s3_source as s3
from app.ingest.s3_destination import S3ConnectionSpec
from tests.fake_graph import xlsx
from tests.fakes import FakeStorage

NOW = datetime(2026, 10, 17, 22, 30, 5, tzinfo=timezone.utc)
SECRET = "S3-very-s3cret-key"
MODIFIED = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


class FakeS3:
    """The calls the datasource makes: list_objects_v2 (paginated) and get_object."""

    def __init__(self, objects: dict[str, bytes], bucket: str = "data", page_size: int = 1000):
        self.bucket = bucket
        self.objects = dict(objects)
        self.page_size = page_size
        self.prefixes: list[str] = []
        self.gets: list[str] = []

    def _error(self, code: str, op: str):
        return ClientError({"Error": {"Code": code, "Message": code}}, op)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix="", **kw):
                if Bucket != outer.bucket:
                    raise outer._error("NoSuchBucket", "ListObjectsV2")
                outer.prefixes.append(Prefix)
                keys = sorted(k for k in outer.objects if k.startswith(Prefix))
                for i in range(0, max(len(keys), 1), outer.page_size):
                    yield {"Contents": [{"Key": k, "Size": len(outer.objects[k]), "LastModified": MODIFIED}
                                        for k in keys[i:i + outer.page_size]]}

        return _P()

    def get_object(self, Bucket, Key):
        if Bucket != self.bucket:
            raise self._error("NoSuchBucket", "GetObject")
        if Key not in self.objects:
            raise self._error("NoSuchKey", "GetObject")
        self.gets.append(Key)
        return {"Body": io.BytesIO(self.objects[Key]), "ContentLength": len(self.objects[Key])}


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(objects, **kw):
        holder["s3"] = FakeS3(objects, **kw)
        monkeypatch.setattr(S3ConnectionSpec, "client", lambda self: holder["s3"])
        return holder["s3"]

    return install


def conn(**kw) -> S3ConnectionSpec:
    base = dict(endpoint_url="http://minio:9000", access_key="AK", secret_key_encrypted=encrypt_secret(SECRET),
                bucket="data")
    base.update(kw)
    return S3ConnectionSpec(**base)


def ingest(storage=None, previous=None, c=None, **source):
    st = storage or FakeStorage()
    res = s3.ingest_s3_to_parquet(c or conn(), s3.S3SourceSpec(**source), "b", "out.parquet", st, now=NOW,
                                  previous=previous)
    return res, pl.read_parquet(io.BytesIO(st.blobs[("b", "out.parquet")]))


# ── the glob and the formats ──────────────────────────────────────────────────
def test_a_glob_over_csv_parquet_excel_and_ndjson(fake):
    fake({"in/a.csv": b"id,v\n1,10\n", "in/b.parquet": _parquet(pl.DataFrame({"id": [2], "v": [20]})),
          "in/c.xlsx": xlsx([["id", "v"], [3, 30]]), "in/d.ndjson": b'{"id": 4, "v": 40}\n',
          "other/e.csv": b"id,v\n9,9\n"})
    res, df = ingest(folder="in", pattern="*.*")
    assert sorted(df["id"].to_list()) == [1, 2, 3, 4]
    assert df.columns[-2:] == ["_file", "_file_modified_at"]
    assert sorted(df["_file"].to_list()) == ["a.csv", "b.parquet", "c.xlsx", "d.ndjson"]
    assert df["_file_modified_at"].to_list()[0] == datetime(2026, 10, 1, 8, 0)
    assert len(res["files"]) == 4


def test_unknown_extensions_say_so(fake):
    fake({"in/x.bak": b"1"})
    with pytest.raises(s3.S3SourceError, match="extension"):
        ingest(folder="in", pattern="*")


def test_double_star_and_the_listing_starts_at_the_fixed_folders(fake):
    s = fake({"in/2026/q1/a.csv": b"id\n1\n", "in/2026/b.csv": b"id\n2\n", "in/2025/c.csv": b"id\n3\n"})
    _, df = ingest(folder="in", pattern="2026/**/*.csv")
    assert sorted(df["_file"].to_list()) == ["2026/b.csv", "2026/q1/a.csv"]
    assert s.prefixes == ["in/2026/"]


def test_the_folder_is_a_whole_folder_not_a_prefix_of_names(fake):
    fake({"in/a.csv": b"id\n1\n", "inbox/b.csv": b"id\n2\n"})
    _, df = ingest(folder="in", pattern="*.csv")
    assert df["_file"].to_list() == ["a.csv"]


def test_hidden_files_and_folder_markers_are_left_out(fake):
    fake({"in/a.csv": b"id\n1\n", "in/.part.csv": b"id\n2\n", "in/sub/": b"", "in/.tmp/c.csv": b"id\n3\n"})
    _, df = ingest(folder="in", pattern="**/*.csv")
    assert df["_file"].to_list() == ["a.csv"]


def test_listing_follows_pages(fake):
    fake({f"in/f{i:03}.csv": b"id\n1\n" for i in range(25)}, page_size=10)
    res, df = ingest(folder="in", pattern="*.csv")
    assert df.height == 25


def test_the_bucket_of_the_datasource_or_of_the_connection(fake):
    fake({"in/a.csv": b"id\n1\n"}, bucket="other")
    _, df = ingest(bucket="other", folder="in", pattern="*.csv")
    assert df.height == 1
    with pytest.raises(s3.S3SourceError, match="no bucket"):
        ingest(c=conn(bucket=""), folder="in", pattern="*.csv")


# ── what is refused ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("kw", [dict(folder="in/..", pattern="*.csv"), dict(folder="in", pattern="../x/*.csv"),
                                dict(folder="in", pattern="x/../../*.csv")])
def test_dot_dot_is_refused(fake, kw):
    fake({"in/a.csv": b"id\n1\n"})
    with pytest.raises(s3.S3SourceError, match=r"'\.\.'"):
        ingest(**kw)


def test_a_value_cannot_climb_out_of_the_folder(fake):
    fake({"a.csv": b"id\n1\n"})
    with pytest.raises(s3.S3SourceError, match=r"'\.\.'"):
        ingest(folder="in/{p}", pattern="*.csv", variables=[{"name": "p", "values": [".."]}])


def test_a_file_over_the_cap_is_refused_before_any_download(fake, monkeypatch):
    s = fake({"in/a.csv": b"id\n1\n", "in/big.csv": b"id\n" + b"1\n" * 600})
    monkeypatch.setattr(get_settings().s3_source, "max_file_bytes", 1000)
    with pytest.raises(s3.S3SourceError, match="big.csv .* over the limit"):
        ingest(folder="in", pattern="*.csv")
    assert s.gets == []


def test_all_the_files_over_the_total_cap(fake, monkeypatch):
    s = fake({f"in/{i}.csv": b"id\n1\n" for i in range(5)})
    monkeypatch.setattr(get_settings().s3_source, "max_total_bytes", 10)
    with pytest.raises(s3.S3SourceError, match="per refresh"):
        ingest(folder="in", pattern="*.csv")
    assert s.gets == []


def test_too_many_files(fake, monkeypatch):
    fake({f"in/{i}.csv": b"id\n1\n" for i in range(5)})
    monkeypatch.setattr(get_settings().s3_source, "max_files", 3)
    with pytest.raises(s3.S3SourceError, match="more than 3 files"):
        ingest(folder="in", pattern="*.csv")


def test_a_missing_bucket_and_no_secret_in_the_messages(fake):
    fake({"in/a.csv": b"id\n1\n"}, bucket="data")
    with pytest.raises(s3.S3SourceError) as e:
        ingest(bucket="nope", folder="in", pattern="*.csv")
    assert "nope" in str(e.value) and "does not exist" in str(e.value) and SECRET not in str(e.value)
    assert str(e.value).startswith("S3: ")


def test_nothing_matching(fake):
    fake({"in/a.csv": b"id\n1\n"})
    with pytest.raises(s3.S3SourceError, match=r"No file matches '\*\.parquet' in 's3://data/in'"):
        ingest(folder="in", pattern="*.parquet")


def test_not_machine_readable_is_refused_naming_the_file(fake):
    fake({"in/a.csv": b"id,id\n1,2\n"})
    with pytest.raises(s3.S3SourceError, match="a.csv.*repeated"):
        ingest(folder="in", pattern="*.csv")


# ── placeholders, ranges, append ──────────────────────────────────────────────
DAYS = {"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}


def test_a_range_with_a_missing_day(fake):
    fake({"in/2026-10-14/s.csv": b"id\n1\n", "in/2026-10-16/s.csv": b"id\n2\n"})
    res, df = ingest(folder="in/{day}", pattern="s.csv", variables=[DAYS])
    assert df.select("_param_day", "_file").rows() == [("2026-10-14", "2026-10-14/s.csv"),
                                                        ("2026-10-16", "2026-10-16/s.csv")]
    assert res["missing"] == [{"params": {"day": "2026-10-15"}, "pattern": "in/2026-10-15/s.csv"}]


def test_no_file_across_the_range_fails(fake):
    fake({"in/x.csv": b"id\n1\n"})
    with pytest.raises(s3.S3SourceError, match="any of the 3 values"):
        ingest(folder="in", pattern="s_{day:yyyyMMdd}.csv", variables=[DAYS])


def test_append_by_key(fake):
    st = FakeStorage()
    fake({"in/a.csv": b"id,v\n1,10\n2,20\n"})
    ingest(storage=st, folder="in", pattern="*.csv", mode="append", keys=["id"])
    st.blobs[("b", "p.parquet")] = st.blobs[("b", "out.parquet")]
    fake({"in/a.csv": b"id,v\n2,21\n3,30\n"})
    res, df = ingest(storage=st, previous={"bucket": "b", "key": "p.parquet"}, folder="in", pattern="*.csv",
                     mode="append", keys=["id"])
    assert sorted(df.select("id", "v").rows()) == [(1, 10), (2, 21), (3, 30)] and res["rows_replaced"] == 1


def test_check_the_files_and_validate(fake):
    fake({"in/s_20261014.csv": b"id\n1\n"})
    g = s3.check_files(conn(), s3.S3SourceSpec(folder="in", pattern="s_{day:yyyyMMdd}.csv", variables=[DAYS]), now=NOW)
    assert [f.rel for f in g.files] == ["s_20261014.csv"] and len(g.missing) == 2
    out = s3.validate_source(s3.S3SourceSpec(folder="in", pattern="s_{day:yyyyMMdd}.csv", variables=[DAYS]), now=NOW)
    assert out["expansions"] == 3 and out["first"] == {"folder": "in", "pattern": "s_20261014.csv"}
    with pytest.raises(s3.S3SourceError, match="not used"):
        s3.validate_source(s3.S3SourceSpec(folder="in", pattern="*.csv", variables=[DAYS]), now=NOW)



# ── second review ─────────────────────────────────────────────────────────────
def test_the_listing_cap_is_for_the_whole_refresh_not_per_expansion(fake, monkeypatch):
    fake({f"in/{d}/f{i}.csv": b"id\n1\n" for d in ("2026-10-14", "2026-10-15", "2026-10-16") for i in range(4)})
    monkeypatch.setattr(get_settings().s3_source, "max_listed", 10)
    with pytest.raises(s3.S3SourceError, match="more than 10 objects"):
        ingest(folder="in/{day}", pattern="*.csv", variables=[DAYS])


def test_a_download_that_takes_too_long_is_stopped(fake, monkeypatch):
    fake({"in/a.csv": b"id\n" + b"1\n" * 2_000_000})
    monkeypatch.setattr(get_settings().s3_source, "max_request_seconds", 0.0001)
    with pytest.raises(s3.S3SourceError, match="S3_SOURCE__MAX_REQUEST_SECONDS"):
        ingest(folder="in", pattern="*.csv")
