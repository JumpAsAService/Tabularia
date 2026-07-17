"""
Engine di trasformazione dati.

Punto d'ingresso unico: `get_engine(name)` restituisce l'implementazione scelta
(oggi Polars; DuckDB in arrivo). Route e task devono usare SOLO l'interfaccia
`Engine` e i modelli di dominio esportati qui, mai Polars direttamente.

L'engine è scelto PER FLUSSO (persistito su `Flow.engine` nel gateway e passato
in preview/run). `ENGINE_CATALOG` alimenta il picker del frontend.
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

# implementazioni registrate: name → factory (classe engine o callable → istanza,
# vedi chDB pigro più sotto).
_ENGINES: dict[str, Callable[[], Engine]] = {
    "polars": PolarsEngine,
}

# DuckDB richiede il pacchetto `duckdb`: import guardato così, se manca,
# l'engine risulta non disponibile senza rompere Polars.
try:
    from app.engine.duckdb_engine import DuckDBEngine

    _ENGINES["duckdb"] = DuckDBEngine
    _DUCKDB_AVAILABLE = True
except Exception:  # pragma: no cover
    _DUCKDB_AVAILABLE = False

# chDB (ClickHouse embedded) è FORK-UNSAFE: importarlo/inizializzarlo avvia i
# thread nativi di ClickHouse, e se ciò avviene nel processo PADRE di Celery
# (prefork) i figli forkati ereditano lock rotti → deadlock su QUALSIASI task.
# Quindi NON lo importiamo qui: controlliamo solo la disponibilità con find_spec
# (non esegue il modulo, niente thread) e lo carichiamo PIGRO dentro get_engine,
# così l'init di chDB avviene nel FIGLIO dopo il fork (fork-safe, per-processo).
import importlib.util

_CHDB_AVAILABLE = importlib.util.find_spec("chdb") is not None


def _make_chdb_engine() -> Engine:
    from app.engine.chdb_engine import ChdbEngine

    return ChdbEngine()


if _CHDB_AVAILABLE:
    _ENGINES["chdb"] = _make_chdb_engine  # factory pigra (callable → istanza)

# metadati per il picker del frontend (creazione flusso). `available=False` =
# opzione mostrata ma non ancora selezionabile.
ENGINE_CATALOG = [
    {
        "id": "polars",
        "label": "Polars",
        "available": True,
        "description": "Motore in-process, lazy e in streaming. Predefinito.",
    },
    {
        "id": "duckdb",
        "label": "DuckDB",
        "available": _DUCKDB_AVAILABLE,
        "description": "Motore SQL out-of-core (spill su disco): adatto ad aggregazioni e join molto grandi. "
        "v1: operazioni di base (le trasformazioni avanzate usano Polars).",
    },
    {
        "id": "chdb",
        "label": "chDB (ClickHouse)",
        "available": _CHDB_AVAILABLE,
        "description": "Motore SQL out-of-core con dialetto ClickHouse (spill su disco). "
        "v1: operazioni strutturali (sql/foreach usano Polars o DuckDB).",
    },
]


@lru_cache
def get_engine(name: str | None = None) -> Engine:
    """Restituisce l'istanza (cached per nome) dell'engine scelto. `None` = default.

    Le chiamate senza argomento (es. eviction cache) usano il default e restano
    valide; preview/run passano il nome dal flusso."""
    key = (name or DEFAULT_ENGINE).lower()
    factory = _ENGINES.get(key)
    if factory is None:
        raise EngineError(
            f"engine sconosciuto: '{name}'. Disponibili: {', '.join(sorted(_ENGINES))}."
        )
    return factory()
