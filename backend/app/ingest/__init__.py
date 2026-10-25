"""
The ingest layer: normalizes any input (CSV/TSV/JSON/NDJSON/Excel/parquet) to parquet
at upload time, so the engine always works on parquet only.
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
