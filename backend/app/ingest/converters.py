"""
Conversione dei formati di input in parquet.

Dove possibile la conversione è in *streaming* (`scan_* -> sink_parquet`), così
un CSV grande non viene mai caricato interamente in RAM — stessa filosofia
dell'engine. JSON (array) ed Excel non sono streamabili: lettura completa.
"""
from __future__ import annotations

import logging

import polars as pl

from app.engine.dtypes import resolve_dtype
from app.ingest.formats import FileFormat
from app.ingest.models import IngestOptions

logger = logging.getLogger(__name__)


class IngestError(Exception):
    """Errore durante la conversione di un file in parquet."""


def _overrides(options: IngestOptions) -> dict[str, object] | None:
    if not options.dtype_overrides:
        return None
    return {col: resolve_dtype(t) for col, t in options.dtype_overrides.items()}


def _cast(lf: pl.LazyFrame, overrides: dict[str, object] | None) -> pl.LazyFrame:
    if not overrides:
        return lf
    return lf.with_columns([pl.col(c).cast(t) for c, t in overrides.items()])


def _sink(lf: pl.LazyFrame, dst: str) -> None:
    """Scrive in streaming; fallback in-memory se un nodo non è supportato."""
    try:
        lf.sink_parquet(dst)
    except Exception as e:  # noqa: BLE001
        logger.warning("sink_parquet streaming fallito (%s), fallback in-memory", e)
        lf.collect(engine='streaming').write_parquet(dst)


def _strip_names(lf: pl.LazyFrame) -> pl.LazyFrame:
    """Headers without the spaces around them («Città »), as the SFTP and S3 datasources
    read them; a name that would then repeat another is left as it is."""
    names = lf.collect_schema().names()
    stripped = {c: c.strip() for c in names if c != c.strip()}
    taken = set(names)
    return lf.rename({c: n for c, n in stripped.items() if n not in taken})


# a number with a decimal comma, with or without thousands grouped by dots: 1.234,50 · 16,125 · 7
_ITALIAN_NUMBER = r"^-?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$"


def _italian_numbers(dst: str, keep: set[str] | frozenset[str] = frozenset()) -> None:
    """The text columns holding nothing but numbers written with a decimal comma become
    numbers: the reader's decimal comma leaves «1.234,50» and «1.500» as text. A column with
    one value that is not such a number stays text, as it was; so does one whose numbers
    have neither a comma nor a thousands dot (codes such as «00144»), and one whose type
    was chosen (`keep`). Read in streaming: a big file is never held in memory whole."""
    import os

    lf = pl.scan_parquet(dst)
    texts = [c for c, t in lf.collect_schema().items() if t == pl.String and c not in keep]
    if not texts:
        return
    checks = lf.select([
        ((pl.col(c).is_null() | pl.col(c).str.strip_chars().str.contains(_ITALIAN_NUMBER)).all()
         & pl.col(c).str.contains(r"[,.]").any()).alias(c)
        for c in texts
    ]).collect(engine="streaming").row(0, named=True)
    numbers = [c for c, ok in checks.items() if ok]
    if not numbers:
        return
    tmp = dst + ".num"
    lf.with_columns([
        pl.col(c).str.strip_chars().str.replace_all(".", "", literal=True).str.replace(",", ".", literal=True)
        .cast(pl.Float64) for c in numbers
    ]).sink_parquet(tmp)
    os.replace(tmp, dst)


def _convert_csv(src: str, dst: str, fmt: FileFormat, options: IngestOptions, overrides) -> None:
    import os
    import tempfile

    from app.ingest import sniff as sniffing

    csv = options.csv
    separator = "\t" if fmt is FileFormat.tsv else csv.separator
    decimal_comma, encoding, path, transcoded = False, csv.encoding, src, None
    if csv.sniff and fmt is FileFormat.csv:
        shape = sniffing.sniff(src)
        separator, decimal_comma = shape.separator, shape.decimal_comma
        if shape.encoding != "utf8":
            fd, transcoded = tempfile.mkstemp(prefix="upload_utf8_", suffix=".csv")
            os.close(fd)
            sniffing.to_utf8(src, transcoded, shape.encoding)
            path, encoding = transcoded, "utf8"
        logger.info("csv read as: separator %r, decimal comma %s, encoding %s", separator, decimal_comma,
                    shape.encoding)

    def scan(infer: int | None) -> pl.LazyFrame:
        return _strip_names(pl.scan_csv(
            path, separator=separator, has_header=csv.has_header, infer_schema_length=infer,
            null_values=csv.null_values, encoding=encoding, try_parse_dates=csv.try_parse_dates,
            schema_overrides=overrides, decimal_comma=decimal_comma,
        ))

    try:
        try:
            _sink(scan(csv.infer_schema_length), dst)
        except pl.exceptions.ComputeError as e:
            if csv.infer_schema_length is None:
                raise
            # a column whose type changes after the rows the types are guessed from (whole
            # numbers for 20,000 rows, then decimals): guessed again from the whole file
            logger.info("csv types guessed again on the whole file (%s)", str(e).splitlines()[0][:200])
            _sink(scan(None), dst)
        if decimal_comma:
            _italian_numbers(dst, set(overrides or {}))
    except IngestError:
        raise
    except Exception as e:  # noqa: BLE001 — what the reader said, as a refusal, not a 500
        from app.engine.query_tag import was_interrupted

        if was_interrupted(e):  # the task's time ran out: not a file that cannot be read
            raise
        first = (str(e).strip().splitlines() or [type(e).__name__])[0][:300]
        raise IngestError(f"The file could not be read as CSV: {first}") from e
    finally:
        if transcoded:
            try:
                os.remove(transcoded)
            except OSError:
                pass


def convert_to_parquet(src: str, dst: str, fmt: FileFormat, options: IngestOptions) -> None:
    """Converte il file locale `src` in un parquet locale `dst`."""
    overrides = _overrides(options)

    if fmt in (FileFormat.csv, FileFormat.tsv):
        _convert_csv(src, dst, fmt, options, overrides)

    elif fmt is FileFormat.ndjson:
        lf = pl.scan_ndjson(
            src,
            infer_schema_length=options.csv.infer_schema_length,
            schema_overrides=overrides,
        )
        _sink(lf, dst)

    elif fmt is FileFormat.json:
        # JSON array: non streamabile
        df = pl.read_json(src)
        df = _cast(df.lazy(), overrides).collect(engine='streaming')
        df.write_parquet(dst)

    elif fmt is FileFormat.excel:
        try:
            df = pl.read_excel(src, sheet_name=options.sheet)
        except (ModuleNotFoundError, ImportError) as e:
            raise IngestError(
                "Reading Excel files requires the 'fastexcel' package. "
                "Add it to the dependencies (e.g. `uv add fastexcel`)."
            ) from e
        except pl.exceptions.NoDataError as e:
            raise IngestError(f"The sheet «{options.sheet}» is empty") from e
        except ValueError as e:
            if "no matching sheet" in str(e):
                raise IngestError(f"The file has no sheet «{options.sheet}»") from e
            raise IngestError(f"The file could not be read as Excel: {e}") from e
        df = _cast(df.lazy(), overrides).collect(engine='streaming')
        df.write_parquet(dst)

    elif fmt is FileFormat.parquet:
        # passthrough (con eventuale cast dei tipi)
        lf = _cast(pl.scan_parquet(src), overrides)
        _sink(lf, dst)

    else:  # pragma: no cover
        raise IngestError(f"Unsupported format: {fmt}")
