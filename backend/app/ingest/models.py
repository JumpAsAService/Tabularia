"""Modelli di dominio dell'ingest: opzioni e risultati."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.engine.base import ColumnInfo
from app.ingest.formats import FileFormat


class CsvOptions(BaseModel):
    separator: str = ","
    has_header: bool = True
    infer_schema_length: int = Field(default=10_000, ge=0)
    null_values: Optional[list[str]] = None
    # "utf8" o "utf8-lossy" (unici accettati da Polars in lazy)
    encoding: str = "utf8"
    try_parse_dates: bool = True


class IngestOptions(BaseModel):
    """Opzioni di conversione. `format=None` => rilevato dall'estensione."""

    format: Optional[FileFormat] = None
    # override dei tipi inferiti: {"colonna": "int"|"float"|"str"|"date"|...}
    dtype_overrides: dict[str, str] = Field(default_factory=dict)
    csv: CsvOptions = Field(default_factory=CsvOptions)
    # per Excel: nome del foglio (None = primo)
    sheet: Optional[str] = None


class DatasetInfo(BaseModel):
    """Metadati di un dataset convertito in parquet."""

    dataset_id: str
    parquet_key: str
    raw_key: str
    format: FileFormat
    rows: int
    columns: list[ColumnInfo]


class IngestResult(BaseModel):
    dataset_id: str
    status: Literal["ready", "processing"]
    parquet_key: str
    raw_key: str
    format: FileFormat
    size_bytes: int
    # presente solo quando status == "ready" (conversione sincrona)
    dataset: Optional[DatasetInfo] = None
    # presente solo quando status == "processing" (conversione async via Celery)
    task_id: Optional[str] = None
