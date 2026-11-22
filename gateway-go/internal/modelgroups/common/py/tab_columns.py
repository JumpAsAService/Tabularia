"""Columns as data (the twin of common/R/columns.R): the input read, its columns' kinds, their
names renamed to positional ones (v1, v2…) before a library sees them, the labels of values as
the engine writes them (catalog.label_of, operations.pivot_label: «true», «false», a date as
2026-11-19, a float as Python's repr), the typed CSVs of the reference cases, the rows left out.

A frame read here carries its columns' kinds in `df.attrs["kinds"]` (column → the catalogue's
kind: integer, number, text, boolean, date, datetime, other), taken from the Parquet file's own
types: pandas turns an integer column with a NULL into floats, and a kind must not follow that.

pandas is imported where a function needs it, never when the module is, so that the library runs
in an image without pandas (a runner that reads its input with pyarrow alone, tab_series). Where
pandas is installed, pyarrow imports it by itself on its first array or Parquet read.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import numpy as np

from tab_protocol import ENV_INPUT, Refusal

if TYPE_CHECKING:
    import pandas as pd

KINDS_ATTR = "kinds"


# ── kinds ─────────────────────────────────────────────────────────────────────
def kind_of_arrow(t: Any) -> str:
    """The kind of a pyarrow type, as the catalogue names a column's kind."""
    import pyarrow.types as pt

    if pt.is_boolean(t):
        return "boolean"
    if pt.is_integer(t):
        return "integer"
    if pt.is_floating(t) or pt.is_decimal(t):
        return "number"
    if pt.is_string(t) or pt.is_large_string(t) or pt.is_dictionary(t) or getattr(pt, "is_string_view", lambda _: False)(t):
        return "text"
    if pt.is_date(t):
        return "date"
    if pt.is_timestamp(t):
        return "datetime"
    return "other"


def kind(df: pd.DataFrame, name: str) -> str:
    """The kind of a column of a frame: the one it was read with, else from its dtype."""
    import pandas as pd

    kinds = df.attrs.get(KINDS_ATTR) or {}
    if name in kinds:
        return kinds[name]
    x = df[name]
    dtype = x.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "integer"
    if pd.api.types.is_float_dtype(dtype):
        return "number"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    if isinstance(dtype, pd.CategoricalDtype) or pd.api.types.is_string_dtype(dtype):
        values = [v for v in x if v is not None and not (isinstance(v, float) and math.isnan(v))]
        if values and all(isinstance(v, bool) for v in values):
            return "boolean"
        if values and all(isinstance(v, dt.date) and not isinstance(v, dt.datetime) for v in values):
            return "date"
        if values and all(isinstance(v, dt.datetime) for v in values):
            return "datetime"
        return "text"
    return "other"


# ── labels ────────────────────────────────────────────────────────────────────
def is_missing(v: Any) -> bool:
    """NULL or NaN (pandas' NA and NaT included: they exist only once pandas is imported)."""
    if v is None:
        return True
    pd = sys.modules.get("pandas")
    if pd is not None and (v is pd.NA or v is pd.NaT):
        return True
    try:
        return bool(isinstance(v, (float, np.floating)) and math.isnan(v))
    except TypeError:
        return False


def label(v: Any) -> str | None:
    """A value's label (tab_label): what a level of a categorical column is called in the results
    and the card, the same as the engine's for the same value; None for a missing value."""
    if is_missing(v):
        return None
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    pd = sys.modules.get("pandas")
    if pd is not None and isinstance(v, pd.Timestamp):
        v = v.to_pydatetime()
    if isinstance(v, np.datetime64):
        import pandas as pd

        v = pd.Timestamp(v).to_pydatetime()
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(dt.UTC).replace(tzinfo=None)
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (float, np.floating)):
        v = float(v)
        return repr(0.0 if v == 0.0 else v)
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    return str(v)


def labels(values: Iterable[Any]) -> list[str | None]:
    return [label(v) for v in values]


def sort_labels(values: Iterable[str | None]) -> list[str]:
    """Distinct labels in text order, the engine's: by code point (Python's own order of str)."""
    return sorted({v for v in values if v is not None})


# ── names ─────────────────────────────────────────────────────────────────────
def rename(names: Iterable[str]) -> tuple[dict[str, str], dict[str, str]]:
    """The positional names of the columns (v1, v2… in their order) and the way back."""
    to = {name: f"v{i}" for i, name in enumerate(names, start=1)}
    return to, {v: k for k, v in to.items()}


# ── the input ─────────────────────────────────────────────────────────────────
def read_input(path: str | None = None, columns: list[str] | None = None) -> pd.DataFrame:
    """The input as a frame with the input's own names (a name is data, never an attribute):
    TABULARIA_INPUT, a path or an s3:// address. Every column when `columns` is None."""
    import pyarrow.parquet as pq

    path = path if path is not None else os.environ.get(ENV_INPUT, "")
    if not path:
        raise Refusal("no_input", "the step has no input (TABULARIA_INPUT is empty)")
    table = pq.read_table(path, columns=columns)
    return frame_of(table)


def frame_of(table: Any) -> pd.DataFrame:
    """A pyarrow table as a frame, its kinds kept: dates as datetime.date, texts as Python's str."""
    import pandas as pd

    kinds = {f.name: kind_of_arrow(f.type) for f in table.schema}
    df = table.to_pandas(date_as_object=True)
    for name, k in kinds.items():
        if k == "text":
            df[name] = pd.Series([None if is_missing(v) else str(v) for v in df[name]], index=df.index, dtype=object)
        elif k == "boolean":
            df[name] = pd.Series([None if is_missing(v) else bool(v) for v in df[name]], index=df.index, dtype=object)
    df.attrs[KINDS_ATTR] = kinds
    return df


def read_csv(path: str, types: Mapping[str, str]) -> pd.DataFrame:
    """A CSV of the reference cases or of the self-test, typed by `types` (column → kind: number,
    integer, text, boolean, date, datetime): a CSV says no type. An empty cell is NULL."""
    import pandas as pd

    raw = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8")
    df = pd.DataFrame(index=raw.index)
    for name in raw.columns:
        df[name] = pd.Series([None if is_missing(v) else str(v) for v in raw[name]], index=raw.index, dtype=object)
    kinds = {name: "text" for name in raw.columns}
    for name, k in types.items():
        if name not in raw.columns:
            raise ValueError(f"the reference table {path} has no column {name}")
        x = [None if is_missing(v) else str(v) for v in raw[name]]
        if k in ("number", "integer"):
            values = np.array([np.nan if v is None else float(v) for v in x], dtype=float)
            if k == "integer" and not np.isnan(values).any():
                df[name] = values.astype(np.int64)
            else:
                df[name] = values
        elif k == "text":
            pass
        elif k == "boolean":
            df[name] = pd.Series([None if v is None else v.lower() in ("true", "1") for v in x], index=raw.index, dtype=object)
        elif k == "date":
            df[name] = pd.Series([None if v is None else dt.date.fromisoformat(v) for v in x], index=raw.index, dtype=object)
        elif k == "datetime":
            df[name] = pd.to_datetime(pd.Series(x, index=raw.index), utc=True).dt.tz_localize(None).astype("datetime64[us]")
        else:
            raise ValueError(f"unknown type {k}")
        kinds[name] = k
    df.attrs[KINDS_ATTR] = kinds
    return df


# ── rows left out ─────────────────────────────────────────────────────────────
def complete_rows(df: pd.DataFrame, columns: Iterable[str]) -> dict[str, Any]:
    """Rows that the model can use: no NULL or NaN in `columns` (the count is n_missing); an
    infinite number stops the node and names the column."""
    import pandas as pd

    keep = np.ones(len(df), dtype=bool)
    for name in columns:
        x = df[name]
        if pd.api.types.is_numeric_dtype(x.dtype) and not pd.api.types.is_bool_dtype(x.dtype):
            values = x.to_numpy(dtype=float, na_value=np.nan)
            if np.isinf(values).any():
                raise Refusal("infinite", f"the column «{name}» holds an infinite value: filter it out or replace it before the node",
                              {"column": name})
            keep &= ~np.isnan(values)
        else:
            keep &= ~np.array([is_missing(v) for v in x], dtype=bool)
    return {"keep": keep, "n": int(keep.sum()), "n_missing": int((~keep).sum()), "rows": np.flatnonzero(keep) + 1}
