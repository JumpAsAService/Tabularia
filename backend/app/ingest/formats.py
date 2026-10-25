"""The input formats supported, and their detection from the extension."""
from __future__ import annotations

from enum import Enum
from pathlib import Path


class FileFormat(str, Enum):
    csv = "csv"
    tsv = "tsv"
    json = "json"
    ndjson = "ndjson"
    excel = "excel"
    parquet = "parquet"


_EXT_MAP: dict[str, FileFormat] = {
    ".csv": FileFormat.csv,
    ".txt": FileFormat.csv,
    ".tsv": FileFormat.tsv,
    ".json": FileFormat.json,
    ".ndjson": FileFormat.ndjson,
    ".jsonl": FileFormat.ndjson,
    ".xlsx": FileFormat.excel,
    ".xls": FileFormat.excel,
    ".parquet": FileFormat.parquet,
    ".pq": FileFormat.parquet,
}

# the "canonical" extension to save the raw file with, per format
_CANONICAL_EXT: dict[FileFormat, str] = {
    FileFormat.csv: ".csv",
    FileFormat.tsv: ".tsv",
    FileFormat.json: ".json",
    FileFormat.ndjson: ".ndjson",
    FileFormat.excel: ".xlsx",
    FileFormat.parquet: ".parquet",
}


class UnsupportedFormatError(Exception):
    def __init__(self, hint: str):
        super().__init__(hint)


def detect_format(filename: str) -> FileFormat:
    ext = Path(filename).suffix.lower()
    fmt = _EXT_MAP.get(ext)
    if fmt is None:
        raise UnsupportedFormatError(
            f"Unrecognized format for '{filename}'. Supported extensions: "
            f"{', '.join(sorted(_EXT_MAP))}. Pass 'format' explicitly."
        )
    return fmt


def canonical_ext(fmt: FileFormat) -> str:
    return _CANONICAL_EXT[fmt]
