"""The moving part of a file datasource (SFTP, SharePoint, S3): placeholders and
variables in its folder and glob, and the append-by-key refresh.

A folder `in/{today:yyyy}` or a glob `sales_{day:yyyyMMdd}.csv` is expanded once
per combination of the datasource's variables (lists and ranges, see
app.core.placeholders), in the datasource's time zone, with {last_refresh} as
for the API. Every file that any expansion matches is read once — two values
that match the same file read it once, with the values of the first — and every
row says the values in `_param_<name>` (one column per variable).

AN EXPANSION THAT MATCHES NOTHING is a day without its file: it is skipped and
listed in the refresh's result (`missing`, shown on the run). If NO expansion
matches anything, the refresh fails saying so: an empty range of files is a
broken feed, not an empty table. A datasource without placeholders behaves as
before (one expansion; no file = the refresh fails).

APPEND BY KEY. In append mode the rows read are merged into the current snapshot.
A key is a GROUP of rows: the old rows of a key that comes again are all REPLACED
by all its new rows, new keys are added — rows read again never pile up on the old
ones (the «every night re-read the last 3 days» import). Rows of one refresh that
share a key are all kept; when the key does not hold `_file` the run counts them
(`duplicate_keys`), since such a key may not identify a row.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TypeVar

import polars as pl
from pydantic import BaseModel, Field

from app.core import placeholders as ph
from app.core.config import get_settings
from app.core.placeholders import Variable
from app.ingest.converters import IngestError

PARAM_PREFIX = "_param_"
# how many missing expansions a result lists by name (the count is always there)
MISSING_SHOWN = 50

F = TypeVar("F")


class FileTemplates(BaseModel):
    """The fields every file datasource shares: the zone of its placeholders, its
    variables, the first value of {last_refresh}, and the refresh mode."""
    timezone: str = Field(default="UTC", max_length=64)
    variables: list[Variable] = Field(default_factory=list, max_length=10)
    last_refresh_initial: str = Field(default="", max_length=100)
    mode: Literal["snapshot", "append"] = "snapshot"
    keys: list[str] = Field(default_factory=list, max_length=20)


@dataclass
class Expansion:
    """One combination of the variables: the templates resolved with it."""
    values: dict[str, str]  # field → resolved template
    params: dict[str, str]  # variable → its value as `_param_<name>` holds it
    label: str = ""  # what to call it in a message: the values, or the resolved templates


@dataclass
class Gathered:
    files: list[Any]
    params: dict[str, dict[str, str]]  # file key → the params of the first expansion that found it
    missing: list[dict[str, Any]] = field(default_factory=list)
    expansions: int = 1


def _err(error: type[IngestError], fn: Callable, *a, **kw):
    try:
        return fn(*a, **kw)
    except ph.PlaceholderError as e:
        raise error(str(e)) from e


def is_dynamic(templates: dict[str, str], spec: FileTemplates) -> bool:
    """Placeholders, variables or escaped braces: the templates go through the
    language. Without any of them they are used exactly as written, as before."""
    return bool(spec.variables) or any("{" in t or "}" in t for t in templates.values())


def static_prefix(template: str) -> str:
    """The leading folders of a template that hold no placeholder: `in/{day}/x` → `in`."""
    try:
        parts = ph.parse(template)
    except ph.PlaceholderError:
        return ""
    text = ""
    for p in parts:
        if not isinstance(p, str):
            if "/" not in text:
                return ""
            head = text.rsplit("/", 1)[0]
            return head or "/"  # `/{day}`: the root, not the login folder
        text += p
    return template


def expand(templates: dict[str, str], spec: FileTemplates, error: type[IngestError], now: datetime | None = None,
           last_refresh: str | None = None) -> list[Expansion]:
    """Every expansion of the templates, checked first (unknown placeholders, an
    unused variable, the keys of the append, the cap on the expansions). Without
    placeholders nor variables: the templates as they are, once."""
    now = now or datetime.now(timezone.utc)
    if spec.mode == "append":
        keys = [k.strip() for k in spec.keys if k.strip()]
        if not keys:
            raise error("the append mode needs the key columns that identify a row (e.g. _file, id)")
        if len(set(keys)) != len(keys):
            raise error("a key column is named twice")
    _err(error, ph.zone, spec.timezone)
    if not is_dynamic(templates, spec):
        return [Expansion(values=dict(templates), params={})]
    _err(error, ph.check_variables, spec.variables)
    lists, ranges = ph.variable_names(spec.variables)
    has_initial = bool(spec.last_refresh_initial.strip())
    if has_initial:
        _err(error, ph.resolve_initial, spec.last_refresh_initial, now, ph.zone(spec.timezone))
    used: set[str] = set()
    for t in templates.values():
        used |= _err(error, ph.check, t, lists, has_initial, ranges=ranges)
    unused = [v.name for v in spec.variables if v.name not in used]
    if unused:
        raise error(f"the variable '{unused[0]}' is not used in the folder or the file pattern")
    cap = get_settings().placeholders.max_values
    base = _err(error, ph.make_context, now, spec.timezone, last_refresh, spec.last_refresh_initial, {})
    combos = _err(error, ph.combinations, spec.variables, base, cap)
    out: list[Expansion] = []
    for combo in combos:
        ctx = ph.with_values(base, combo)
        values = {k: _err(error, ph.render, t, ctx) for k, t in templates.items()}
        params = ph.params_of(spec.variables, combo)
        label = ", ".join(f"{k}={v}" for k, v in params.items())
        out.append(Expansion(values=values, params=params, label=label))
    return out


def gather(expansions: list[Expansion], find: Callable[[Expansion], list[F]], key_of: Callable[[F], str],
           shown: Callable[[Expansion], str], max_files: int, error: type[IngestError]) -> Gathered:
    """The files of every expansion, each once (the first expansion's params), and
    the expansions that found none."""
    files: dict[str, F] = {}
    params: dict[str, dict[str, str]] = {}
    missing: list[dict[str, Any]] = []
    for exp in expansions:
        found = find(exp)
        if not found:
            missing.append({"params": exp.params, "pattern": shown(exp)})
            continue
        for f in found:
            k = key_of(f)
            if k not in files:
                files[k] = f
                params[k] = exp.params
        if len(files) > max_files:
            raise error(f"the expansions match more than {max_files} files: narrow the pattern or the range")
    return Gathered(files=[files[k] for k in sorted(files)], params=params, missing=missing,
                    expansions=len(expansions))


def nothing_found(error: type[IngestError], g: Gathered, what: str) -> IngestError:
    """Every expansion matched nothing: the refresh fails, naming them."""
    names = [m["pattern"] for m in g.missing]
    shown = ", ".join(names[:3]) + (f" … {names[-1]}" if len(names) > 3 else "")
    return error(f"No file matches {what} for any of the {g.expansions} values of the variables ({shown})")


def missing_report(g: Gathered) -> list[dict[str, Any]]:
    return g.missing[:MISSING_SHOWN]


def with_params(df: pl.DataFrame, params: dict[str, str], shown_as: str, error: type[IngestError]) -> pl.DataFrame:
    """The `_param_<name>` columns of a file's rows."""
    if not params:
        return df
    clash = [PARAM_PREFIX + n for n in params if PARAM_PREFIX + n in df.columns]
    if clash:
        raise error(f"{shown_as}: the file has a column '{clash[0]}', the name of the column of a variable: "
                    "rename the variable")
    return df.with_columns(*(pl.lit(v, dtype=pl.String).alias(PARAM_PREFIX + n) for n, v in params.items()))


def stack_files(files: list[F], *, fetch: Callable[[F, str], None], read: Callable[[str, F], pl.DataFrame],
                meta: Callable[[F], tuple[str, datetime | None]], params: dict[str, dict[str, str]],
                key_of: Callable[[F], str], shown_as: Callable[[F], str], report: Callable[..., None], tmp: str,
                error: type[IngestError], file_column: str = "_file",
                modified_column: str = "_file_modified_at") -> tuple[pl.DataFrame, list[str]]:
    """Downloads, reads and stacks the files: every row with its variables'
    values, `_file` and `_file_modified_at` (the two last columns). Returns the
    table and the columns not in every file (united by name, null where missing)."""
    frames: list[pl.DataFrame] = []
    columns_by_file: dict[str, list[str]] = {}
    rows_read = 0
    for i, f in enumerate(files):
        report("files", file=i + 1, files=len(files), rows=rows_read)
        local = os.path.join(tmp, f"{i}{os.path.splitext(shown_as(f))[1]}")
        fetch(f, local)
        df = read(local, f)
        os.remove(local)
        columns_by_file[key_of(f)] = df.columns
        rows_read += df.height
        if df.height == 0:
            # header only: typed all as text, it would drag every numeric
            # column of the other files to text in the relaxed union
            continue
        rel, modified = meta(f)
        df = with_params(df, params.get(key_of(f)) or {}, shown_as(f), error)
        frames.append(df.with_columns(
            # ALWAYS, one file or many: which file a number comes from, and
            # often the month or the branch live only in its name
            pl.lit(rel).alias(file_column),
            pl.lit(modified, dtype=pl.Datetime("us")).alias(modified_column),
        ))
    if not frames:
        raise error("All the matching files are empty (header only)")
    try:
        table = pl.concat(frames, how="diagonal_relaxed")
    except Exception as e:
        raise error(f"The files have columns with the same name but incompatible types: {e}") from e
    table = provenance_last(table, [file_column, modified_column])
    every = set.intersection(*(set(c) for c in columns_by_file.values()))
    drift = sorted({c for cols in columns_by_file.values() for c in cols} - every)
    return table, drift


def provenance_last(table: pl.DataFrame, provenance: list[str]) -> pl.DataFrame:
    """Data, then the variables' columns, then the provenance (`_file`, `_file_modified_at`)."""
    params = [c for c in table.columns if c.startswith(PARAM_PREFIX) and c not in provenance]
    data = [c for c in table.columns if c not in provenance and c not in params]
    return table.select(data + params + [c for c in provenance if c in table.columns])


# ── append by key ─────────────────────────────────────────────────────────────
def read_previous(storage, previous: dict | None, tmp: str, error: type[IngestError]) -> pl.DataFrame | None:
    if not previous or not previous.get("key"):
        return None
    local = os.path.join(tmp, "previous.parquet")
    try:
        storage.download_file(previous.get("bucket") or "", previous["key"], local)
    except Exception as e:  # noqa: BLE001 — named in the message
        raise error(f"the previous snapshot could not be read for the append ({type(e).__name__}): refresh again, "
                    "or switch the datasource to snapshot once") from e
    return pl.read_parquet(local)


def append_by_key(prev: pl.DataFrame | None, new: pl.DataFrame, keys: list[str],
                  error: type[IngestError]) -> tuple[pl.DataFrame, int]:
    """The previous snapshot with the rows of `new` merged in by `keys`: a key
    that comes again REPLACES all its old rows with all its new ones, a new key is
    added. Returns the table and how many old rows were replaced."""
    missing = [k for k in keys if k not in new.columns]
    if missing:
        raise error(f"the key column '{missing[0]}' is not among the columns of the files "
                    f"({', '.join(new.columns[:15])}): fix the keys of the append")
    # a key is a GROUP of rows (`_file`, `_param_day`): every new row is kept, and
    # the old rows of a key that comes again all go — never one row per key
    if prev is None:
        return new, 0
    missing = [k for k in keys if k not in prev.columns]
    if missing:
        raise error(f"the previous snapshot has no column '{missing[0]}': fix the keys, or refresh once in "
                    "snapshot mode")
    try:
        both = pl.concat([prev, new], how="diagonal_relaxed")
    except Exception as e:  # noqa: BLE001
        raise error(f"the new rows do not fit the previous snapshot's types: {e}") from e
    prev_a, new_a = both.head(prev.height), both.tail(new.height)
    kept = prev_a.join(new_a.select(keys).unique(), on=keys, how="anti", nulls_equal=True)
    return pl.concat([kept, new_a], how="vertical"), prev_a.height - kept.height


def repeated_keys(new: pl.DataFrame, keys: list[str]) -> int:
    """How many keys are on more than one of the new rows. Kept (a key is a group),
    but said on the run when the key does not hold `_file` — rows of one file are
    naturally its group; otherwise it may be a key that does not identify a row."""
    if "_file" in keys or not all(k in new.columns for k in keys) or new.is_empty():
        return 0
    return int(new.group_by(keys).len().filter(pl.col("len") > 1).height)


def finish(table: pl.DataFrame, spec: FileTemplates, storage, previous: dict | None, tmp: str,
           error: type[IngestError], provenance: list[str]) -> tuple[pl.DataFrame, int, int]:
    """The table to save: the new rows, or (append) the previous snapshot with them
    merged in. Returns it, the old rows replaced and the repeated keys."""
    if spec.mode != "append":
        return table, 0, 0
    keys = [k.strip() for k in spec.keys if k.strip()]
    prev = read_previous(storage, previous, tmp, error)
    merged, replaced = append_by_key(prev, table, keys, error)
    return provenance_last(merged, provenance), replaced, repeated_keys(table, keys)
