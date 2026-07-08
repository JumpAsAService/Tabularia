"""
Layer di ingest: normalizza qualsiasi input (CSV/TSV/JSON/NDJSON/Excel/parquet)
in parquet al momento dell'upload, così l'engine lavora sempre e solo su parquet.
"""
from app.ingest.converters import IngestError, convert_to_parquet
from app.ingest.formats import FileFormat, UnsupportedFormatError, detect_format
from app.ingest.models import (
    CsvOptions,
    DatasetInfo,
    IngestOptions,
    IngestResult,
)
from app.ingest.service import IngestService, get_ingest_service

__all__ = [
    "FileFormat",
    "UnsupportedFormatError",
    "detect_format",
    "IngestError",
    "convert_to_parquet",
    "IngestOptions",
    "CsvOptions",
    "DatasetInfo",
    "IngestResult",
    "IngestService",
    "get_ingest_service",
]
