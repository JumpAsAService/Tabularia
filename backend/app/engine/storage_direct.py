"""The duckdb and chdb engines read the object storage in place, without downloading.

A parquet on S3 is read by ranges: its footer, then only the columns and row groups
the query needs. The engines used to download every source (and every cached step)
in full before reading it: 6-14 s per preview on a 2.4 GB dataset, even for 100
rows of five columns (review of 2026-10-14).

With `STORAGE__DIRECT_READ=false`, with an endpoint the engines cannot address (one
with a path), with a key they would read as a pattern, or with a storage that is not
the real one (tests use an in-memory one), the file is downloaded as before.

DuckDB reads through its `httpfs` extension, installed in the image at build time
(backend/Dockerfile) and only loaded here, so a worker never downloads code; the
credentials are a temporary secret of each connection. chDB is ClickHouse: its
`s3()` takes the credentials from the process environment, so they never appear in
a query or in the error messages that quote one.

Neither the Execute-SQL node nor a computed column can reach the storage: their
checks refuse table functions (app/engine/sql_guard.py, app/engine/espressioni.py),
and DuckDB's SQL sandbox has no network at all.
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from urllib.parse import urlparse

import duckdb

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# where the image keeps DuckDB's extensions (empty: DuckDB's default, ~/.duckdb)
EXTENSION_DIRECTORY = os.getenv("DUCKDB_EXTENSION_DIRECTORY", "")
# glob characters: in a path both engines would expand them
_GLOB = re.compile(r"[*?\[\]{}]")


def _lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def configured() -> bool:
    """The setting allows it and the endpoint is one the engines can address."""
    settings = get_settings()
    if not settings.storage.direct_read:
        return False
    endpoint = urlparse(settings.storage.endpoint or "")
    if endpoint.path not in ("", "/"):
        logger.warning("engines download their sources: the storage endpoint has a path (%s)", endpoint.path)
        return False
    return True


def _addressable(bucket: str, key: str) -> bool:
    return not (_GLOB.search(bucket) or _GLOB.search(key))


# ── DuckDB ────────────────────────────────────────────────────────────────────
def _load_httpfs(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("SET autoinstall_known_extensions = false")  # nothing downloaded at run time
    if EXTENSION_DIRECTORY:
        con.execute(f"SET extension_directory = {_lit(EXTENSION_DIRECTORY)}")
    con.execute("LOAD httpfs")


@lru_cache(maxsize=1)
def duckdb_available() -> bool:
    """Whether DuckDB reads the storage in place in this process (decided once)."""
    if not configured():
        return False
    try:
        con = duckdb.connect(":memory:")
        try:
            _load_httpfs(con)
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — no extension: the download still works
        logger.warning("DuckDB downloads its sources: the httpfs extension does not load (%s)", e)
        return False
    return True


def duckdb_secret_sql() -> str:
    """The backend's storage credentials as a DuckDB secret for one connection."""
    st = get_settings().storage
    endpoint = urlparse(st.endpoint or "")
    parts = [
        "TYPE s3",
        f"KEY_ID {_lit(st.access_key)}",
        f"SECRET {_lit(st.secret_key.get_secret_value())}",
        f"REGION {_lit(st.region or 'us-east-1')}",
        "URL_STYLE 'path'",  # as the storage service addresses buckets
    ]
    if endpoint.netloc:  # no endpoint: AWS itself
        parts += [f"ENDPOINT {_lit(endpoint.netloc)}", f"USE_SSL {'true' if endpoint.scheme == 'https' else 'false'}"]
    return f"CREATE OR REPLACE TEMPORARY SECRET tabularia_storage ({', '.join(parts)})"


def duckdb_enable(con: duckdb.DuckDBPyConnection) -> None:
    """Lets `con` read the object storage with the backend's credentials."""
    _load_httpfs(con)
    con.execute(duckdb_secret_sql())


def duckdb_url(bucket: str, key: str) -> str | None:
    """`s3://bucket/key`, or None for a name DuckDB would read as a pattern."""
    return f"s3://{bucket}/{key}" if _addressable(bucket, key) else None


# ── chDB ──────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def chdb_available() -> bool:
    """Whether chDB reads the storage in place in this process (decided once). Its
    `s3()` finds the credentials in the environment: they are put there now, the
    same keys the storage service uses, so no query ever carries them."""
    if not configured():
        return False
    st = get_settings().storage
    if st.access_key:
        os.environ["AWS_ACCESS_KEY_ID"] = st.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = st.secret_key.get_secret_value()
    return True


def chdb_url(bucket: str, key: str) -> str | None:
    """The object's https/http URL for `s3()`, or None for a name chDB would read as
    a pattern."""
    if not _addressable(bucket, key):
        return None
    st = get_settings().storage
    endpoint = urlparse(st.endpoint or "")
    base = f"{endpoint.scheme}://{endpoint.netloc}" if endpoint.netloc else f"https://s3.{st.region or 'us-east-1'}.amazonaws.com"
    return f"{base}/{bucket}/{key}"


def chdb_source_sql(url: str) -> str:
    return f"SELECT * FROM s3({_lit(url)}, 'Parquet')"
