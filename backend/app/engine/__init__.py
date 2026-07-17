"""
Engine di trasformazione dati.

Punto d'ingresso unico: `get_engine(name)` restituisce l'implementazione scelta
(oggi Polars; DuckDB in arrivo). Route e task devono usare SOLO l'interfaccia
`Engine` e i modelli di dominio esportati qui, mai Polars direttamente.

L'engine è scelto PER FLUSSO (persistito su `Flow.engine` nel gateway e passato
in preview/run). `ENGINE_CATALOG` alimenta il picker del frontend.
"""
from functools import lru_cache

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

# implementazioni registrate: name → factory. DuckDB si aggiungerà qui.
_ENGINES: dict[str, type[Engine]] = {
    "polars": PolarsEngine,
}

# metadati per il picker del frontend (creazione flusso). `available=False` =
# opzione mostrata ma non ancora selezionabile (in arrivo).
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
        "available": False,
        "description": "Motore SQL out-of-core (spill su disco): adatto ad aggregazioni e join molto grandi. In arrivo.",
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
