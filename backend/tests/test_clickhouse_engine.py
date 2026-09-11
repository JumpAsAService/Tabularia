"""Engine ClickHouse ESTERNO: stessa IR e stessi builder SQL di chDB, ma eseguiti
su un server remoto. Qui in modalità `push` (staging table + download streaming)
con storage e Redis finti: l'unica cosa vera è il server ClickHouse.

Serve un ClickHouse raggiungibile — i test si SALTANO se `CLICKHOUSE_TEST_HOST`
non è impostata. Es. (porta HTTP 18123, utente tab/tab, db tab):
    docker run -d --rm --name chtest -p 18123:8123 -e CLICKHOUSE_USER=tab \\
      -e CLICKHOUSE_PASSWORD=tab -e CLICKHOUSE_DB=tab clickhouse/clickhouse-server:24.8
    CLICKHOUSE_TEST_HOST=localhost CLICKHOUSE_TEST_PORT=18123 CLICKHOUSE_TEST_USER=tab \\
      CLICKHOUSE_TEST_PASSWORD=tab CLICKHOUSE_TEST_DB=tab pytest tests/test_clickhouse_engine.py

Modalità `s3` (il server legge/scrive sullo storage): stessa suite con
`CLICKHOUSE_TEST_TRANSPORT=s3` + `CLICKHOUSE_TEST_S3_ENDPOINT` (endpoint dello
storage COME LO VEDE il server, es. http://minio:9000 se il container è sulla
rete `dataprep-network`) e le `STORAGE__*` dell'engine puntate allo stesso
storage dall'host (es. STORAGE__ENDPOINT=http://localhost:9002). Qui lo storage
è VERO (MinIO dello stack), non il doppione in memoria.
"""
import datetime as dt
import os

import polars as pl
import pytest

from app.core.config import ClickHouseExternalSettings
from app.engine.cache import StepCache, plan_hashes
from app.engine.exceptions import EngineError, SourceNotFoundError
from app.engine.base import DataSource
from tests.conftest import BUCKET, upload_df
from tests.fakes import FakeRedis

pytestmark = pytest.mark.skipif(
    not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve un ClickHouse (CLICKHOUSE_TEST_HOST)"
)


TRANSPORT = os.getenv("CLICKHOUSE_TEST_TRANSPORT", "push")


def _cfg() -> ClickHouseExternalSettings:
    return ClickHouseExternalSettings(
        host=os.getenv("CLICKHOUSE_TEST_HOST", ""),
        port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_TEST_USER", "default"),
        password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_TEST_DB", "default"),
        transport=TRANSPORT,
        s3_endpoint=os.getenv("CLICKHOUSE_TEST_S3_ENDPOINT", ""),
    )


class _RealStorage:
    """Storage VERO (per la modalità s3) con le comodità del FakeStorage usate
    negli assert (`exists`, `read`)."""

    def __init__(self):
        from app.utils import get_storage_service

        self._svc = get_storage_service()

    def __getattr__(self, name):
        return getattr(self._svc, name)

    def exists(self, bucket: str, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._svc.head_object(bucket, key)
            return True
        except ClientError:
            return False

    def read(self, bucket: str, key: str) -> bytes:
        import io

        buf = io.BytesIO()
        self._svc.download_fileobj(bucket, key, buf)
        return buf.getvalue()


def _read_blob(storage, bucket: str, key: str) -> bytes:
    return storage.read(bucket, key) if hasattr(storage, "read") else storage.blobs[(bucket, key)]


@pytest.fixture
def storage():
    if TRANSPORT == "s3":
        return _RealStorage()
    from tests.fakes import FakeStorage

    return FakeStorage()


def _engine(storage, cache=None):
    from app.engine.clickhouse_engine import ClickHouseEngine

    return ClickHouseEngine(storage=storage, cache=cache or StepCache(storage, redis_client=FakeRedis()), cfg=_cfg())


@pytest.fixture
def src(storage):
    df = pl.DataFrame({
        "paese": ["IT", "FR", "DE", "IT"],
        "vendite": [100, 50, 250, 300],
        "citta": ["Roma", "Parigi", None, "Milano"],
        "giorno": [dt.date(2024, 1, 1), dt.date(2024, 2, 1), dt.date(2024, 3, 1), dt.date(2024, 4, 1)],
        "quando": [dt.datetime(2024, 1, 1, 10, 30), dt.datetime(2024, 2, 1, 11), None, dt.datetime(2024, 4, 1, 12)],
    })
    return upload_df(storage, df, "datasets/vendite_ch.parquet")


@pytest.fixture
def anagrafica(storage):
    df = pl.DataFrame({"paese": ["IT", "DE"], "nome": ["Italia", "Germania"]})
    return upload_df(storage, df, "datasets/anagrafica_ch.parquet")


def test_preview_passthrough_types(storage, src):
    res = _engine(storage).preview(src, [], limit=10)
    assert res.row_count == 4
    types = {c.name: c.dtype for c in res.columns}
    assert types["paese"] == "String"
    assert types["vendite"] == "Int64"
    assert types["giorno"] == "Date"
    assert types["quando"].startswith("Datetime")
    by_paese = {r["paese"]: r for r in res.rows}
    assert by_paese["DE"]["citta"] is None and by_paese["DE"]["quando"] is None
    assert by_paese["FR"]["giorno"] == dt.date(2024, 2, 1)


def test_filter_and_sort(storage, src):
    ops = [
        {"type": "filter", "params": {"column": "vendite", "operator": "gt", "value": 100}},
        {"type": "sort", "params": {"by": ["vendite"], "descending": [True]}},
    ]
    res = _engine(storage).preview(src, ops, limit=10)
    assert [r["vendite"] for r in res.rows] == [300, 250]


def test_group_by(storage, src):
    ops = [{"type": "group_by", "params": {
        "by": ["paese"],
        "aggregations": [{"column": "vendite", "func": "sum", "alias": "tot"}],
    }}]
    res = _engine(storage).preview(src, ops, limit=10)
    tot = {r["paese"]: r["tot"] for r in res.rows}
    assert tot == {"IT": 400, "FR": 50, "DE": 250}


def test_join(storage, src, anagrafica):
    ops = [{"type": "join", "params": {"right": anagrafica.model_dump(), "on": ["paese"], "how": "left"}}]
    res = _engine(storage).preview(src, ops, limit=10)
    assert res.row_count == 4
    nomi = {r["paese"]: r["nome"] for r in res.rows}
    assert nomi["IT"] == "Italia" and nomi["FR"] is None


def test_compute_and_rename(storage, src):
    ops = [
        {"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "vendite * 2"}]}},
        {"type": "rename", "params": {"mapping": {"doppio": "x2"}}},
    ]
    res = _engine(storage).preview(src, ops, limit=10)
    assert {r["paese"]: r["x2"] for r in res.rows}["DE"] == 500


def test_preview_truncated(storage, src):
    res = _engine(storage).preview(src, [], limit=2)
    assert res.row_count == 2 and res.truncated


def test_run_writes_parquet_and_cache(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(storage, cache)
    ops = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}]
    dest = DataSource(bucket=BUCKET, key="out/vendite_it.parquet")
    res = eng.run(src, ops, dest)
    assert res.rows_written == 2
    assert {c.name for c in res.columns} >= {"paese", "vendite", "giorno", "quando"}
    assert storage.exists(BUCKET, dest.key)
    # il risultato finale finisce anche nella step-cache (namespace dell'engine)
    hashes = plan_hashes(eng._source_id(src), ops)
    assert cache.has(hashes[-1])
    assert storage.exists(cache.bucket, cache.object_key(hashes[-1]))
    # rilettura del parquet scritto: tipi temporali preservati
    import io
    df = pl.read_parquet(io.BytesIO(_read_blob(storage, BUCKET, dest.key)))
    assert df["giorno"].dtype == pl.Date and df["quando"].dtype.is_temporal()
    assert df["paese"].dtype == pl.String


def test_preview_uses_cache_prefix(storage, src):
    cache = StepCache(storage, redis_client=FakeRedis())
    eng = _engine(storage, cache)
    ops = [
        {"type": "filter", "params": {"column": "vendite", "operator": "gt", "value": 60}},
        {"type": "select", "params": {"columns": ["paese", "vendite"]}},
    ]
    res = eng.preview(src, ops, limit=10)
    assert res.row_count == 3
    # la preview materializza il prefisso (tutti gli step tranne l'ultimo)
    hashes = plan_hashes(eng._source_id(src), ops)
    assert cache.has(hashes[0]) and not cache.has(hashes[1])
    # seconda preview: riparte dalla cache e dà lo stesso risultato
    res2 = eng.preview(src, ops, limit=10)
    assert sorted(r["paese"] for r in res2.rows) == sorted(r["paese"] for r in res.rows)


@pytest.mark.skipif(TRANSPORT != "push", reason="le tabelle di staging esistono solo in modalità push")
def test_staging_tables_are_dropped(storage, src):
    eng = _engine(storage)
    eng.preview(src, [], limit=5)
    import clickhouse_connect

    c = _cfg()
    client = clickhouse_connect.get_client(host=c.host, port=c.port, username=c.username,
                                           password=c.password.get_secret_value(), database=c.database)
    leftovers = client.query(
        "SELECT name FROM system.tables WHERE database = %(db)s AND name LIKE '_tabularia_%%'",
        parameters={"db": c.database},
    ).result_rows
    assert leftovers == []


def test_missing_source(storage):
    with pytest.raises(SourceNotFoundError):
        _engine(storage).preview(DataSource(bucket=BUCKET, key="datasets/nope.parquet"), [], limit=5)


def test_sql_op_forbids_external_access(storage, src):
    ops = [{"type": "sql", "params": {"query": "SELECT * FROM self, s3('http://x/y', 'a', 'b', 'Parquet')"}}]
    with pytest.raises(EngineError):
        _engine(storage).preview(src, ops, limit=5)


def test_not_configured():
    from app.engine.clickhouse_engine import ClickHouseEngine

    with pytest.raises(EngineError):
        ClickHouseEngine(storage=object(), cache=object(), cfg=ClickHouseExternalSettings(host=""))
