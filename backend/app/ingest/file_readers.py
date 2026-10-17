"""Reading one downloaded file of a file datasource (SFTP, S3): CSV, TSV, parquet,
Excel, NDJSON, with the same strict contract as SharePoint — a header in the
first row, one name per column, no repeats; anything else is refused naming the
file, never guessed at. `error` is the datasource's error class (its prefix).
"""
from __future__ import annotations

import posixpath

import polars as pl

from app.ingest.converters import IngestError
from app.ingest.models import CsvOptions
from app.ingest.sharepoint_source import check_header, read_sheet

FORMAT_BY_EXTENSION = {
    ".csv": "csv", ".txt": "csv", ".tsv": "tsv",
    ".parquet": "parquet", ".pq": "parquet",
    ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel",
    ".ndjson": "ndjson", ".jsonl": "ndjson",
}


def format_of(path: str, chosen: str, error: type[IngestError]) -> str:
    if chosen != "auto":
        return chosen
    fmt = FORMAT_BY_EXTENSION.get(posixpath.splitext(path)[1].lower())
    if fmt is None:
        raise error(
            f"{path}: the extension does not say the format. Narrow the pattern to the data files, or choose "
            f"the format on the datasource (recognised: {', '.join(sorted(FORMAT_BY_EXTENSION))})"
        )
    return fmt


def _strip_names(df: pl.DataFrame) -> pl.DataFrame:
    return df.rename({c: c.strip() for c in df.columns if c != c.strip()})


def _read_csv(local: str, shown_as: str, opts: CsvOptions, separator: str, error: type[IngestError]) -> pl.DataFrame:
    common = dict(separator=separator, encoding=opts.encoding)
    if opts.has_header:
        try:
            head = pl.read_csv(local, has_header=False, n_rows=1, infer_schema_length=0, **common)
        except pl.exceptions.NoDataError as e:
            raise error(f"{shown_as}: the file is empty") from e
        except Exception as e:
            raise error(f"{shown_as}: unreadable CSV file ({e})") from e
        if head.height == 0:
            raise error(f"{shown_as}: the file is empty")
        check_header(list(head.row(0)), shown_as, "the first row", error)
    try:
        df = pl.read_csv(
            local, has_header=opts.has_header, infer_schema_length=opts.infer_schema_length,
            null_values=opts.null_values, try_parse_dates=opts.try_parse_dates, **common,
        )
    except Exception as e:
        raise error(f"{shown_as}: unreadable CSV file ({e})") from e
    return _strip_names(df)


def _read_parquet(local: str, shown_as: str, error: type[IngestError]) -> pl.DataFrame:
    import pyarrow.parquet as pq

    try:
        names = pq.read_schema(local).names
    except Exception as e:
        raise error(f"{shown_as}: unreadable parquet file ({e})") from e
    check_header(names, shown_as, "the schema", error)
    return pl.read_parquet(local)


def _read_ndjson(local: str, shown_as: str, opts: CsvOptions, error: type[IngestError]) -> pl.DataFrame:
    try:
        df = pl.read_ndjson(local, infer_schema_length=opts.infer_schema_length or None)
    except Exception as e:
        raise error(f"{shown_as}: unreadable NDJSON file ({e})") from e
    check_header(df.columns, shown_as, "the keys", error)
    return df


def read_file(local: str, shown_as: str, fmt: str, csv: CsvOptions, sheet: str,
              error: type[IngestError]) -> pl.DataFrame:
    if fmt in ("csv", "tsv"):
        return _read_csv(local, shown_as, csv, "\t" if fmt == "tsv" else csv.separator, error)
    if fmt == "parquet":
        return _read_parquet(local, shown_as, error)
    if fmt == "excel":
        return read_sheet(local, sheet.strip() or None, shown_as, error)
    if fmt == "ndjson":
        return _read_ndjson(local, shown_as, csv, error)
    raise error(f"unsupported format '{fmt}'")  # pragma: no cover
