"""
Engine di trasformazione dati.

Punto d'ingresso unico: `get_engine()` restituisce l'implementazione attiva
(oggi Polars). Route e task devono usare SOLO l'interfaccia `Engine` e i modelli
di dominio esportati qui, mai Polars direttamente.
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


@lru_cache
def get_engine() -> Engine:
    """Restituisce l'istanza (cached) dell'engine attivo."""
    return PolarsEngine()
