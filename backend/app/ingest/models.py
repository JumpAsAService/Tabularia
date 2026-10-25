"""The ingest's domain models: options and results."""
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
    # "utf8" or "utf8-lossy" (the only ones Polars takes lazily)
    encoding: str = "utf8"
    try_parse_dates: bool = True
    # the separator, the decimal comma and the encoding read from the file itself (an
    # upload from the editor, which sends the file alone): ingest/sniff.py
    sniff: bool = False


class IngestOptions(BaseModel):
    """Conversion options. `format=None` => detected from the extension."""

    format: Optional[FileFormat] = None
    # overrides of the inferred types: {"column": "int"|"float"|"str"|"date"|...}
    dtype_overrides: dict[str, str] = Field(default_factory=dict)
    csv: CsvOptions = Field(default_factory=CsvOptions)
    # per Excel: nome del foglio (None = the first visible one, ingest/sheets.py)
    sheet: Optional[str] = None


class DatasetInfo(BaseModel):
    """The metadata of a dataset converted to parquet."""

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
    # present only when status == "ready" (synchronous conversion)
    dataset: Optional[DatasetInfo] = None
    # present only when status == "processing" (asynchronous conversion through Celery)
    task_id: Optional[str] = None
    # an Excel file: its sheets in order, the hidden ones, and the one read (the editor
    # offers the others after the upload)
    sheets: Optional[list[str]] = None
    hidden_sheets: Optional[list[str]] = None
    sheet: Optional[str] = None
