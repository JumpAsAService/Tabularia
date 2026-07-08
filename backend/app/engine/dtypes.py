"""Mappatura dtype (stringa -> tipo Polars), condivisa da engine e ingest."""
from __future__ import annotations

from typing import Any

import polars as pl

from app.engine.exceptions import EngineError

DTYPES: dict[str, Any] = {
    "int": pl.Int64, "int64": pl.Int64, "int32": pl.Int32,
    "float": pl.Float64, "float64": pl.Float64, "float32": pl.Float32,
    "str": pl.String, "string": pl.String, "utf8": pl.String,
    "bool": pl.Boolean, "boolean": pl.Boolean,
    "date": pl.Date, "datetime": pl.Datetime,
}

def resolve_dtype(name: str) -> Any:
    try:
        return DTYPES[name.lower()]
    except KeyError:
        raise EngineError(f"dtype non supportato: '{name}'") from None
