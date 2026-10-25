"""
The data transformation engines.

The one entry point: `get_engine(name)` returns the chosen implementation. Routes and
tasks must use ONLY the `Engine` interface and the domain models exported here, never an
engine directly.

The engine is chosen PER FLOW (stored in `Flow.engine` in the gateway and passed to
preview/run). `ENGINE_CATALOG` feeds the frontend's picker.
"""
from functools import lru_cache
from typing import Callable

from app.engine.base import (
    ColumnInfo,
    DataSource,
    Engine,
    Operation,
    PreviewResult,
    RunResult,
)
from app.engine.exceptions import (
    EngineError,
    OperationError,
    SourceNotFoundError,
    UnknownOperationError,
)
from app.engine.cache import StepCache, plan_hashes
from app.engine.dtypes import resolve_dtype
from app.engine.operations import available_operations
from app.engine.polars_engine import PolarsEngine

__all__ = [
    "Engine",
    "PolarsEngine",
    "get_engine",
    "ENGINE_CATALOG",
    "DEFAULT_ENGINE",
    "DataSource",
    "Operation",
    "PreviewResult",
    "RunResult",
    "ColumnInfo",
    "EngineError",
    "OperationError",
    "SourceNotFoundError",
    "UnknownOperationError",
    "available_operations",
    "resolve_dtype",
    "StepCache",
    "plan_hashes",
]


DEFAULT_ENGINE = "polars"

# registered implementations: name → factory (an engine class or a callable → instance,
# see the lazy chDB below).
_ENGINES: dict[str, Callable[[], Engine]] = {
    "polars": PolarsEngine,
}

# DuckDB needs the `duckdb` package: a guarded import, so that when it is missing the
# engine is unavailable without breaking Polars.
try:
    from app.engine.duckdb_engine import DuckDBEngine

    _ENGINES["duckdb"] = DuckDBEngine
    _DUCKDB_AVAILABLE = True
except Exception:  # pragma: no cover
    _DUCKDB_AVAILABLE = False

# chDB (embedded ClickHouse) is FORK-UNSAFE: importing or initializing it starts
# ClickHouse's native threads, and if that happens in Celery's PARENT process
# (prefork) the forked children inherit broken locks → a deadlock on ANY task. So it is
# NOT imported here: only its availability is checked with find_spec (which does not
# run the module, no threads), and it is loaded LAZILY inside get_engine, so chDB's
# init happens in the CHILD after the fork (fork-safe, per process).
import importlib.util

_CHDB_AVAILABLE = importlib.util.find_spec("chdb") is not None


def _make_chdb_engine() -> Engine:
    from app.engine.chdb_engine import ChdbEngine

    return ChdbEngine()


if _CHDB_AVAILABLE:
    _ENGINES["chdb"] = _make_chdb_engine  # a lazy factory (callable → instance)

# EXTERNAL ClickHouse (a remote or cloud server): optional, on only when configured
# (CLICKHOUSE_EXTERNAL__HOST). clickhouse-connect is pure HTTP → fork-safe, but the
# import stays lazy so as not to pay for it where the engine is not used.
from app.core.config import get_settings as _get_settings

_CLICKHOUSE_CFG = _get_settings().clickhouse_external
_CLICKHOUSE_AVAILABLE = _CLICKHOUSE_CFG.enabled and importlib.util.find_spec("clickhouse_connect") is not None


def _make_clickhouse_engine() -> Engine:
    from app.engine.clickhouse_engine import ClickHouseEngine

    return ClickHouseEngine()


if _CLICKHOUSE_AVAILABLE:
    _ENGINES["clickhouse"] = _make_clickhouse_engine

# BigQuery (Google, serverless): optional, on only when configured (BIGQUERY__PROJECT
# + credentials) and the package is installed. A pure HTTP/gRPC client, a lazy import
# as for ClickHouse.
_BIGQUERY_CFG = _get_settings().bigquery
_BIGQUERY_AVAILABLE = _BIGQUERY_CFG.enabled and importlib.util.find_spec("google.cloud.bigquery") is not None


def _make_bigquery_engine() -> Engine:
    from app.engine.bigquery_engine import BigQueryEngine

    return BigQueryEngine()


if _BIGQUERY_AVAILABLE:
    _ENGINES["bigquery"] = _make_bigquery_engine

# metadata for the frontend's picker (creating a flow). `available=False` = an option
# shown but not selectable yet.
ENGINE_CATALOG = [
    {
        "id": "polars",
        "label": "Polars",
        "available": True,
        "description": "In-process, lazy, streaming engine. Default.",
    },
    {
        "id": "duckdb",
        "label": "DuckDB",
        "available": _DUCKDB_AVAILABLE,
        "description": "Out-of-core SQL engine (spills to disk): suited to very large aggregations and joins. "
        "v1: basic operations (advanced transforms use Polars).",
    },
    {
        "id": "chdb",
        "label": "chDB (ClickHouse)",
        "available": _CHDB_AVAILABLE,
        # optional: the light production image does not include it (WITH_CHDB); when it
        # is missing the picker shows it «not configured», not «coming»
        "optional": True,
        "description": (
            "Out-of-core SQL engine with ClickHouse dialect (spills to disk). "
            "v1: structural operations (sql/foreach use Polars or DuckDB)."
            if _CHDB_AVAILABLE
            else "Embedded ClickHouse engine, not included in this image: "
            "rebuild the backend with WITH_CHDB=true to enable it."
        ),
    },
    {
        "id": "clickhouse",
        "label": "ClickHouse (external)",
        "available": _CLICKHOUSE_AVAILABLE,
        # optional: when not available it is because it is NOT CONFIGURED (not "coming")
        "optional": True,
        "description": (
            f"External ClickHouse server ({_CLICKHOUSE_CFG.host}:{_CLICKHOUSE_CFG.port}, transport {_CLICKHOUSE_CFG.transport}): "
            "transforms run on the server, not in the worker."
            if _CLICKHOUSE_AVAILABLE
            else "External ClickHouse server (cloud or self-hosted). Not configured: set CLICKHOUSE_EXTERNAL__HOST."
        ),
    },
    {
        "id": "bigquery",
        "label": "BigQuery",
        "available": _BIGQUERY_AVAILABLE,
        "optional": True,
        "description": (
            f"Google BigQuery (project {_BIGQUERY_CFG.project}): reads the parquet files of the GCS bucket as external "
            "tables and computes on the service, pay per use. "
            + (f"Native step cache in the dataset {_BIGQUERY_CFG.cache_dataset}." if _BIGQUERY_CFG.cache_dataset else "No step cache: every preview reads the source again.")
            if _BIGQUERY_AVAILABLE
            else "Google BigQuery (serverless). Not configured: set BIGQUERY__PROJECT and the credentials."
        ),
    },
]


@lru_cache
def get_engine(name: str | None = None) -> Engine:
    """Returns the chosen engine's instance (cached by name). `None` = the default.

    The calls without an argument (the cache eviction) use the default and stay valid;
    preview/run pass the name from the flow."""
    key = (name or DEFAULT_ENGINE).lower()
    factory = _ENGINES.get(key)
    if factory is None:
        # an engine KNOWN but missing in this deployment (chDB left out of the light
        # image, the external ClickHouse not configured): a message for the user, in
        # English, that does not call it «unknown»
        known = next((e for e in ENGINE_CATALOG if e["id"] == key), None)
        if known is not None:
            raise EngineError(
                f"The {known['label']} engine is not available in this deployment. "
                "Choose another engine for this flow, or ask the administrator to enable it."
            )
        raise EngineError(
            f"unknown engine: '{name}'. Available: {', '.join(sorted(_ENGINES))}."
        )
    return factory()
