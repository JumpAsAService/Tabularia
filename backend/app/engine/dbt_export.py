"""Compiles a flow's IR into dbt models and assembles the project.

NOT an engine: an exporter, an escape hatch. The dbt project must give THE SAME data
Tabularia publishes for the same flow: so in the federated target (dbt-duckdb) every
operation is written with THE SAME SQL functions as the DuckDB engine (`duckdb_ops`:
casts, predicates, joins, pivots…), imported and not copied; in the native target (the
source warehouse, SQL translated with sqlglot) the functions the dialect lacks
(`TRY_CAST`, `contains`, `ends_with`, `UNION ALL BY NAME`, `UNPIVOT`) are written in
equivalent portable forms.

Every model is a chain of CTEs (`WITH s1 AS (...), s2 AS (...) SELECT * FROM sN`), the
right side of a join or union compiled as extra CTEs. Where a model starts the gateway
decides: a `source()`, another model (`ref()`: shared steps, SQL queries, outputs of
other flows) or a seed.

The SCHEMA (the columns' names and types) is tracked at compile time: the sources'
types are those Tabularia read at the import, and the first step of every model
enforces them (`CAST`) — so the project sees the data as the flow does, even where the
warehouse types it otherwise. They serve the cast (the engine's own result: text with
spaces, integer literals, truncation) and the unpivot (a common supertype). Where it would have to guess (a compute's type, a sql
node's columns, an aggregate's type) a probe asks DuckDB itself, over empty tables typed
like the sources (see `_Probe`). The flow's free text (compute, sql) is read in the
dialect of the flow's engine (see `_expression`).

`foreach` cannot be translated (control flow at run time) → an explicit error; `pivot`
in the federated target only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from typing import Any, Callable, Optional

from app.engine.duckdb_ops import (
    _AGG,
    _DUCK_DTYPES,
    _as_list,
    _cast_expr,
    _join_condition,
    _join_select,
    _JOIN_KW,
    _lit,
    _predicate,
    _qi,
    _require,
    unpivot_value_type,
)
from app.engine.exceptions import EngineError
from app.engine.operations import PIVOT_LABEL_SEP, check_filter_value, sort_keys

# a source's resolver ({key} | {model} | {seed}) → (SQL ref, columns, DuckDB type per column)
logger = logging.getLogger(__name__)
SourceResolver = Callable[[dict], tuple[str, list[str], dict]]
SEED_MAX_ROWS = 50_000          # a dbt seed is a CSV in the repository: beyond this it makes no sense


class DbtExportError(EngineError):
    """The export refused. The English text stays in the logs; `code` and `params`
    go to the gateway, which says it in the reader's language."""

    def __init__(self, message: str, code: str = "internal", **params):
        super().__init__(message)
        self.code = code
        self.params = params


# ── types ────────────────────────────────────────────────────────────────────
# UInt64 stays unsigned (UBIGINT: UInt64 on ClickHouse, numeric(20,0) on Postgres): as BIGINT a hash
# key over 2^63 wrapped to a negative on ClickHouse and failed the model on DuckDB
_POLARS_INT = {"Int8": "TINYINT", "Int16": "SMALLINT", "Int32": "INTEGER", "Int64": "BIGINT",
               "UInt8": "BIGINT", "UInt16": "BIGINT", "UInt32": "BIGINT", "UInt64": "UBIGINT"}


def duck_type(dtype: Optional[str]) -> Optional[str]:
    """The Polars dtype Tabularia read a column with (a string, as in
    `Datasource.columns`) → the equivalent DuckDB type. None = unknown or not
    representable (the column is not typed again)."""
    if not dtype:
        return None
    d = str(dtype)
    if d in _POLARS_INT:
        return _POLARS_INT[d]
    if d in ("Float32", "Float64"):
        return "DOUBLE"
    if d in ("String", "Utf8", "Categorical", "Enum") or d.startswith(("Categorical", "Enum")):
        return "VARCHAR"
    if d == "Boolean":
        return "BOOLEAN"
    if d == "Date":
        return "DATE"
    if d.startswith("Datetime"):
        return "TIMESTAMP"
    if d == "Time":
        return "TIME"
    m = re.match(r"Decimal\(precision=(\d+|None), scale=(\d+)\)", d)
    if m:
        return f"DECIMAL({m.group(1) if m.group(1) != 'None' else 38},{m.group(2)})"
    return None


_ZONE = "TIMESTAMPTZ"   # a database column Tabularia read as an instant with a time zone (a Postgres timestamptz)


def source_type(dtype: Optional[str]) -> Optional[str]:
    """The type of a column of a DATABASE table or query that dbt reads live: as `duck_type`,
    except a datetime with a time zone, which is TIMESTAMPTZ. The first step turns it into
    Tabularia's naive UTC instant (see `_typed`): a bare CAST AS TIMESTAMP follows the
    session's time zone, the machine's in dbt-duckdb, the server's in Postgres."""
    if re.match(r"Datetime\(.*time_zone='[^']+'", str(dtype or "")):
        return _ZONE
    return duck_type(dtype)


def _typed(dialect: Optional[str], col: str, kind: str) -> str:
    """A source column with the type Tabularia read it with (see `_base`), in DuckDB SQL
    (`dialect`: the warehouse the native targets translate it for)."""
    if kind == _ZONE:
        if dialect == "clickhouse":
            return f"CAST({_qi(col)} AS TIMESTAMP)"     # a ClickHouse DateTime is already an instant
        return f"CAST({_qi(col)} AS TIMESTAMPTZ) AT TIME ZONE 'UTC'"
    if kind == SEED_TEXT:
        text = f"CAST({_decode_seed(_qi(col))} AS VARCHAR)"
        return f'{text} COLLATE "C"' if dialect == "postgres" else text
    if dialect == "postgres" and _family(kind) == "text":
        # Tabularia compares and orders text by bytes: the column carries the "C" collation from here
        # on (equality, joins, GROUP BY, DISTINCT, min/max, ORDER BY), whatever the database's or the
        # column's own (en_US: 'a' < 'B'; a case-insensitive ICU one: 'A' = 'a')
        return f'CAST({_qi(col)} AS {kind}) COLLATE "C"'
    return f"CAST({_qi(col)} AS {kind})"


def _per_byte(model: "_Model", kind: Optional[str]) -> bool:
    """Whether a text must be given the byte order explicitly (Postgres: the "C" collation). The
    first step gives it to the source columns; text the flow makes (a literal, a cast) and the
    columns of another model carry the database's collation."""
    return model.native and model.dialect == "postgres" and _family(kind) == "text"


def _byte(model: "_Model", expr_: str, kind: Optional[str]) -> str:
    """A text compared or ordered by bytes, as Tabularia does (see `_per_byte`)."""
    return f'{expr_} COLLATE "C"' if _per_byte(model, kind) else expr_


def _typed_projection(dialect: Optional[str], cols: list[str], types_: dict) -> str:
    """The columns with Tabularia's types; one whose type is not known as it is."""
    return ", ".join(f"{_typed(dialect, c, types_[c]) if types_.get(c) else _qi(c)} AS {_qi(c)}" for c in cols)


def _typed_type(kind: Optional[str]) -> Optional[str]:
    """The type a column has after `_typed`."""
    return "TIMESTAMP" if kind == _ZONE else ("VARCHAR" if kind == SEED_TEXT else kind)


# ── seeds: the texts dbt's loaders change ─────────────────────────────────────
# dbt-duckdb's COPY reads "" as NULL; agate (dbt-postgres, dbt-clickhouse) reads '', 'null' in any
# case and blank text as NULL and turns \r\n and \r into \n; ClickHouse's CSV input then cuts the
# spaces and tabs at the ends and reads \N as NULL. Tabularia keeps every one of them, so such a
# value is written `~` + the text with ~ space tab \r escaped (none of which a loader changes),
# and the first step of a model that reads the seed gives it back (`_decode_seed`).
SEED_TEXT = "VARCHAR ~"   # the type of a seed's text column written so (see `_typed`)
_SEED_CODES = {"~": "~0", " ": "~1", "\t": "~2", "\r": "~3"}


def _the_loader_changes_it(v: str) -> bool:
    return (v.strip().lower() in ("", "null") or "\r" in v or v[:1] in (" ", "\t") or v[-1:] in (" ", "\t")
            or v == "\\N" or v.startswith("~"))


def encode_seed(df: Any) -> tuple[Any, list[str]]:
    """A seed's data (Polars) with every text a dbt seed loader would change written as
    `~…`; the other texts, and the columns without any such text, stay as they are (the
    CSV of such a seed is the same as before). Returns the data and the columns written so."""
    import polars as pl

    changed = []
    for c, t in df.schema.items():
        if duck_type(str(t)) != "VARCHAR":
            continue
        texts = df[c].cast(pl.String).to_list()
        if not any(v is not None and _the_loader_changes_it(v) for v in texts):
            continue
        changed.append(c)
        written = [v if v is None or not _the_loader_changes_it(v) else "~" + "".join(_SEED_CODES.get(ch, ch) for ch in v) for v in texts]
        df = df.with_columns(pl.Series(c, written, dtype=pl.String))
    return df, changed


def _decode_seed(q: str) -> str:
    """A seed's text written by `encode_seed`, back as Tabularia reads it. Every `~` of the
    written text starts a pair, so the replacements cannot meet; `~0` goes last."""
    t = f"substr({q}, 2)"
    for code, text in (("~1", "' '"), ("~2", "chr(9)"), ("~3", "chr(13)"), ("~0", "'~'")):
        t = f"replace({t}, '{code}', {text})"
    return f"CASE WHEN left({q}, 1) = '~' THEN {t} ELSE {q} END"


_TEXT = ("VARCHAR", "STRING", "TEXT", "CHAR")
_NUMBERS = ("DOUBLE", "FLOAT", "REAL", "DECIMAL")
_INTEGERS = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "INT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT")


def _family(t: Optional[str]) -> str:
    t = (t or "").upper()
    if t.startswith(_TEXT):
        return "text"
    if t.startswith(_INTEGERS):
        return "integer"
    if t.startswith(_NUMBERS):
        return "number"
    return t.lower() or "?"


class _S:
    """A model's current schema: the columns in order and their DuckDB type (or None).
    No columns = the columns are NOT known (after a pivot, whose columns the data decides,
    or a sql node nobody can read): the steps after it use `*` and DuckDB's forms that need
    no list. Then `types` keeps the types still known (the pivot's index) and `others` is the
    type of every other column when there is one (a pivot's columns: its aggregate's)."""

    def __init__(self, cols: list[str], types: Optional[dict] = None, others: Optional[str] = None):
        self.cols = list(cols)
        self.types = {c: (types or {}).get(c) for c in self.cols} if self.cols else dict(types or {})
        self.others = None if self.cols else others
        self.order: Optional[list[tuple[str, bool]]] = None   # the last sort's keys [(column, descending)], while valid

    def con(self, cols: list[str], types: dict) -> "_S":
        return _S(cols, types)

    def kind(self, col: str) -> Optional[str]:
        if col in self.types:
            return self.types[col]
        return None if self.cols else self.others


def _equal_but_for_case(cols: list[str]) -> list[str]:
    """The names that another name of `cols` repeats but for its case (`Codice`, `code`):
    DuckDB resolves names without case, so it reads them as ONE column."""
    per: dict[str, list[str]] = {}
    for c in cols:
        per.setdefault(c.casefold(), []).append(c)
    return [c for group in per.values() if len(group) > 1 for c in group]


def _refuse_uppercase(model: "_Model", s: "_S", step: str) -> None:
    """Federated target: two columns whose names differ only by case are refused. DuckDB
    resolves both to the first one and renames the second `codice_1` with the first one's
    values, without an error (Polars and ClickHouse keep both, with their own values)."""
    doubles = _equal_but_for_case(s.cols)
    if doubles and not model.native:
        raise DbtExportError(
            f"{step}: the columns {', '.join(f'«{c}»' for c in doubles)} differ only by case: DuckDB, which the federated "
            "target runs on, reads them as one column. Rename one of them in the flow, or use the native or the ClickHouse target.",
            "columns_differ_only_by_case", columns=", ".join(doubles), step=step)


MAX_POSTGRES_NAME = 63   # Postgres keeps the first 63 bytes of a name (NAMEDATALEN - 1), whole characters only


def name_in_postgres(name: str) -> str:
    """The name a column has in Postgres: its first 63 bytes, without a character cut in half."""
    return name.encode()[:MAX_POSTGRES_NAME].decode(errors="ignore")


def _refuse_long(model: "_Model", s: "_S", step: str) -> None:
    """Native Postgres: two names that share their first 63 bytes are ONE column there, and the
    model fails ("column reference … is ambiguous"). A single long name only gets shorter, the
    same everywhere it is used (the README says so)."""
    if not (model.native and model.dialect == "postgres"):
        return
    per: dict[str, list[str]] = {}
    for c in s.cols:
        per.setdefault(name_in_postgres(c), []).append(c)
    doubles = [c for group in per.values() if len(group) > 1 for c in group]
    if doubles:
        raise DbtExportError(
            f"{step}: the columns {', '.join(f'«{c}»' for c in doubles)} are the same column in Postgres, which keeps only the first "
            f"{MAX_POSTGRES_NAME} bytes of a name. Shorten one of them in the flow, or use the federated or the ClickHouse target.",
            "column_names_too_long", columns=", ".join(doubles), limit=MAX_POSTGRES_NAME)


# the types the probe reports and the export keeps (a list, a struct, a map: not known)
_SCALAR_TYPE = re.compile(
    r"(BOOLEAN|U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|FLOAT|DOUBLE|DECIMAL\(\d+,\d+\)|VARCHAR|DATE|TIME|"
    r"TIMESTAMP|TIMESTAMP WITH TIME ZONE|TIMESTAMP_(S|MS|NS)|INTERVAL|BLOB|UUID)")


_UNKNOWN = "__tabularia_unknown_type"
_UNKNOWN_TYPE = f'STRUCT("{_UNKNOWN}" BLOB)'   # in the probe, a column whose type is not known


class _Probe:
    """DuckDB in memory over EMPTY tables typed like the sources: after each step it reads
    the names and the types the model's columns really have (what dbt-duckdb sees) where
    the export would otherwise guess them — a compute's type, a sql node's columns, an
    aggregate's type. It reads no data and no file (external access off, one statement
    at a time). It stops, and the export goes on with what it tracks by itself, when a
    source's type is not known, after a pivot (its columns come from the data) or at the
    first step that does not bind in DuckDB."""

    def __init__(self):
        self._con = None
        self.active = True
        self._done = 0          # the model's CTEs already created (empty tables)
        self.error: Optional[tuple[str, str]] = None   # (step, DuckDB's error) where it stopped
        self.for_unknown = False  # it stopped on a column whose type is not known (`_UNKNOWN_TYPE`)

    def switch_off(self) -> None:
        self.active = False

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def _connection(self):
        if self._con is None:
            import duckdb

            self._con = duckdb.connect(":memory:", config={"enable_external_access": False,
                                                          "autoinstall_known_extensions": False})
            self._con.execute("SET lock_configuration = true")
        return self._con

    def _view(self, con, name: str, sql: str) -> None:
        """The step as an EMPTY TABLE with its columns: the next step binds against it alone. As a
        view, every step re-bound the whole chain before it (150 columns, 120 steps: 10 s)."""
        if len(con.extract_statements(sql)) != 1:
            raise ValueError("not one statement")
        con.execute(f"CREATE TABLE {_qi(name)} AS SELECT * FROM ({sql}) LIMIT 0")

    def align(self, model: "_Model") -> bool:
        """The tables of the steps added since the last call. False (and off) at the first
        that does not bind."""
        if not self.active:
            return False
        try:
            con = self._connection()
            while self._done < len(model.ctes):
                name, sql, _ = model.ctes[self._done]
                sql = model.probes.get(name, sql)
                for placeholder, duck in model.for_probe.items():
                    sql = sql.replace(placeholder, duck)
                self._view(con, name, sql)
                self._done += 1
        except Exception as e:  # noqa: BLE001 — the probe only helps: without it the export goes on
            step = model.ctes[self._done][0] if self._done < len(model.ctes) else "?"
            logger.debug("dbt export: the probe stops at %s (%s)", step, e)
            self.error = (step, str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
            self.for_unknown = _UNKNOWN in str(e)
            self.switch_off()
            return False
        return True

    def columns(self, model: "_Model", sql: str) -> Optional[list[tuple[str, Optional[str]]]]:
        """[(name, type or None)] of a query over the steps so far, or None."""
        if not self.align(model):
            return None
        try:
            con = self._connection()
            if len(con.extract_statements(sql)) != 1:
                return None
            rows = con.execute(f"DESCRIBE {sql}").fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("dbt export: the probe cannot describe a query (%s)", e)
            return None
        return [(r[0], r[1] if _SCALAR_TYPE.fullmatch(str(r[1])) else None) for r in rows]

    def after(self, model: "_Model", ref: str, s: "_S", op_type: str) -> "_S":
        """The schema after a step: the probe's when it can read it, otherwise the tracked one.
        Not with two names that differ only by case: DuckDB would report the second as
        `codice_1`, a column the warehouse does not have."""
        if op_type == "pivot" or _equal_but_for_case(s.cols):
            self.switch_off()
        if not self.active:
            return s
        columns = self.columns(model, f"SELECT * FROM {_qi(ref)}")
        if columns is None:
            self.switch_off()
            return s
        return _S([c for c, _ in columns], dict(columns))


class _Namer:
    """Makes CTE names unique within the model (s1, s2, r1s1, …)."""

    def __init__(self, prefix: str = "s"):
        self.prefix = prefix
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self.prefix}{self._n}"


# words reserved in at least one dialect (DuckDB, Postgres, MySQL, ClickHouse): a step
# cannot be named so without quotes (found: a CTE `unpivot` on DuckDB)
_RESERVED = frozenset("""
all analyse analyze and any array as asc asymmetric between both by case cast check collate column columns constraint
create cross current current_date current_time current_timestamp current_user default deferrable desc describe distinct
do drop else end except exists explain false fetch filter first for foreign from full grant group having ilike in
initially inner intersect interval into is join key last lateral leading left like limit map minus natural not null nulls
offset on only or order over partition pivot placing primary qualify range recursive references returning right row rows
sample select session_user set show some struct symmetric table tablesample then to trailing true union unique unpivot
user using values variadic when where window with within
""".split())


def _step_slug(text: str, maximum: int = 40) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:maximum].strip("_") or "step"
    if out in _RESERVED:
        out = f"{out}_step"
    return out if not out[0].isdigit() else f"s_{out}"


def _comment(text: str) -> str:
    """A note on ONE line, that neither closes the comment nor opens Jinja."""
    t = re.sub(r"\s+", " ", str(text)).strip()
    for a, b in (("*/", "* /"), ("}}", "} }"), ("%}", "% }"), ("#}", "# }")):
        t = t.replace(a, b)
    # a space after EVERY `{` that would open a Jinja block: replacing the pairs left
    # one open in `{{{` (→ `{ {{`)
    t = re.sub(r"\{(?=[{%#])", "{ ", t)
    return t[:200]


# ── Jinja: dbt renders every model, test, macro, profile and YAML value ───────
_JINJA_TO_WRITE = re.compile(r"\{[{%#]|\r")   # what Jinja would not write as it is


def neutralize_jinja(text: Any) -> str:
    """User text (a value, a name, an expression, a query, a description) written into
    a file dbt renders as Jinja: every opener (`{{`, `{%`, `{#`) becomes an expression
    that prints it, so dbt writes the text as it is instead of running it. A value
    `{{ env_var('…') }}` stays that text in the table, as in Tabularia; `price {% 10`
    does not break the project. Text without openers comes out unchanged.
    Only for text that is NOT already inside a Jinja expression (see `_jinja_str`).
    A `\\r` too: Jinja writes every line break of a template as `\\n` (dbt 1.12.5 turned
    'a\\r\\nb' into 'a\\nb' in the table), so it is printed by an expression, `{{ '\\r' }}`."""
    return _JINJA_TO_WRITE.sub(lambda m: "{{ '\\r' }}" if m.group(0) == "\r" else "{{ '" + m.group(0) + "' }}", str(text))


def _jinja_str(value: Any) -> str:
    """A Jinja string expression dbt reads back exactly, for user text written INSIDE an
    expression the exporter writes (`{{ config(alias='…') }}`). A plain text keeps its
    `'…'`; with a quote only, `"…"`. dbt's static parser (on by default for models) takes a
    lone string literal as written, escapes included: `alias='o\\'brien'` made the alias
    `o\\'brien`, another table than the one typed in the flow. So a text that needs an escape
    (a backslash, both quotes, a carriage return) is written as pieces joined with `~`: the
    static parser leaves that to Jinja, which reads `'\\\\'` as one backslash. The gateway's
    `_stringa_jinja` writes the source() names the same way."""
    s = str(value)
    if "\\" not in s and "'" not in s and "\r" not in s:
        return f"'{s}'"
    if "\\" not in s and '"' not in s and "\r" not in s:
        return f'"{s}"'
    # a `\r` as an escape too: Jinja reads a raw one in a literal as `\n`
    pieces = [{"'": '"\'"', "\\": "'\\\\'", "\r": "'\\r'"}.get(x, f"'{x}'") for x in re.split(r"(['\\\r])", s) if x]
    return " ~ ".join(pieces + ([] if len(pieces) > 1 else ["''"]))


class _Model:
    """Gathers a model's CTEs. Every CTE has a NAME that says what it does
    (`filter_status`, `aggregate_by_category`) and a NOTE in words: whoever reviews
    the project in a pull request must understand the step without opening Tabularia."""

    def __init__(self, namer: _Namer, native: bool = False, dialect: str | None = None, text: str = "duckdb",
                 used: set | None = None):
        self.namer = namer
        self.native = native
        self.dialect = dialect   # the target dialect, where a form depends on the warehouse
        self.text = text         # the dialect the flow's free text is written in (its engine's)
        self.to_review: list[tuple[str, str]] = []   # (step, why): text the team must review
        self.ctes: list[tuple[str, str, str]] = []  # (name, sql, note)
        self.labels: list[tuple[str, str]] = []   # (name, note) of the step being compiled
        # the CTE names taken, in the whole project when the caller shares the set: dbt inlines an
        # ephemeral model as a CTE of the model that reads it, and ClickHouse 24 refuses a model
        # whose CTE has the name of one inside the inlined model (UNKNOWN_TABLE)
        self._used: set[str] = used if used is not None else set()
        self.probe = _Probe()
        self.probes: dict[str, str] = {}              # step → the SQL the probe reads instead (a typed empty source, …)
        self.placeholders: dict[str, str] = {}         # placeholder → text in the warehouse's dialect (see native_text)
        self.for_probe: dict[str, str] = {}          # placeholder → the same in DuckDB, for the probe
        # random, so no text of the flow can spell a placeholder out; it never reaches the zip
        self._mark = secrets.token_hex(6)

    def native_text(self, finale: str, duck: str) -> str:
        """SQL written directly in the warehouse's dialect (ClickHouse functions sqlglot would
        misread): a placeholder in the model, which sqlglot translates without touching it;
        `put_back_texts` puts the text back after the translation. `duck`: the same in DuckDB,
        for the probe."""
        placeholder = f"__tabularia_{self._mark}_t{len(self.placeholders)}__()"
        self.placeholders[placeholder] = finale
        self.for_probe[placeholder] = duck
        return placeholder

    def _unique_name(self, base: str) -> str:
        name, n = _step_slug(base), 2
        while name in self._used:
            name = f"{_step_slug(base, 36)}_{n}"
            n += 1
        self._used.add(name)
        return name

    def _next_name(self, base: str) -> str:
        """The name `add` will give the next step called `base` (it does not reserve it)."""
        name, n = _step_slug(base), 2
        while name in self._used:
            name = f"{_step_slug(base, 36)}_{n}"
            n += 1
        return name

    def add(self, sql: str, step_name: Optional[str] = None, note: Optional[str] = None, probe: Optional[str] = None) -> str:
        if step_name is None and self.labels:
            step_name, step_note = self.labels[-1]
            note = note if note is not None else step_note
        name = self._unique_name(step_name or self.namer.next())
        self.ctes.append((name, sql, _comment(note or "")))
        if probe is not None:
            self.probes[name] = probe
        return name


# ── portable forms for the native target ─────────────────────────────────────
# The guards check the VALUE, not only its shape: '2024-02-30', 25:00 or a 20-digit integer
# passed a shape-only pattern, and then Postgres failed the whole model and ClickHouse made
# up a value (1 March, the next day). Only patterns, never a CAST, decide: no warehouse is
# asked to evaluate a conversion that could fail before the guard has said yes.
def _at_most(n: str) -> str:
    """A pattern for the numbers written with len(n) digits that are <= n."""
    parts = [n[:i] + (f"[0-{int(c) - 1}]" if c > "1" else "0") + (f"[0-9]{{{len(n) - i - 1}}}" if i < len(n) - 1 else "")
             for i, c in enumerate(n) if c > "0"]
    return "(" + "|".join(parts + [n]) + ")"


# an integer literal that fits a BIGINT (Postgres fails beyond, the engine gives NULL)
_RE_INTEGER = (f"^([+]?0*([0-9]{{1,18}}|{_at_most('9223372036854775807')})"
              f"|-0*([0-9]{{1,18}}|{_at_most('9223372036854775808')}))$")
_RE_NUMBER = r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$"
# A real day, as both engines (Polars and DuckDB) read one: a month and a day of one or two digits,
# a year of one to four (24 is the year 24, not 2024); months of 30 and 31 days, 29 February only in
# a leap year, no year 0 (Postgres has none). Native gave NULL for '2024-1-5', which both engines read.
_YEAR = r"(0{0,3}[1-9]|0{0,2}[1-9][0-9]|0?[1-9][0-9]{2}|[1-9][0-9]{3})"
_LEAP_YEAR = (r"(0{0,3}[48]|0{0,2}([2468][048]|[13579][26])|(0?[1-9]|[1-9][0-9])(0[48]|[2468][048]|[13579][26])"
                   r"|(0?[48]|[2468][048]|[13579][26])00)")
_DAY = (rf"({_YEAR}-((0?[13578]|1[02])-(0?[1-9]|[12][0-9]|3[01])|(0?[469]|11)-(0?[1-9]|[12][0-9]|30)|0?2-(0?[1-9]|1[0-9]|2[0-8]))"
           rf"|{_LEAP_YEAR}-0?2-29)")
_RE_DATA = rf"^{_DAY}$"
# a real instant: a 'T' or white space, hours 0-23, minutes and seconds 0-59, of one or two digits; after
# the seconds a 'Z' or an offset, which both engines DROP (the wall-clock time stays: 10:30+02 is 10:30)
_SPACES = r"[ \t\n\r\f\v]"   # the white space both engines take between the day and the time
_RE_INSTANT = (rf"^{_DAY}(({_SPACES}+|T)([01]?[0-9]|2[0-3]):[0-5]?[0-9](:[0-5]?[0-9]([.][0-9]*)?(Z|[+-][0-9]{{2}}(:?[0-9]{{2}})?(:[0-9]{{2}})?)?)?)?$")
# the years ClickHouse's Date32 and DateTime64 hold: it moved any other to 1900 or 2299, a made-up value
_RE_CLICKHOUSE_YEARS = r"^(19|2[0-2])[0-9]{2}-"
# the BIGINT range for a number: NaN and ±inf compare false, so they are outside too
_MIN_BIGINT, _BEYOND_BIGINT = "-9223372036854775808", "9223372036854775808"
_EXPONENT_WITHOUT_ZEROS = (r"([eE][+-]?)0+([0-9])", r"\1\2")   # 1e00005 → 1e5 (pattern, replacement)


def _substitute(t: str, model: str, con: str) -> str:
    """`regexp_replace` with a pattern anchored at the start: it matches once, so DuckDB and
    Postgres (the first match) and ClickHouse (every match) give the same text."""
    return f"regexp_replace({t}, {_lit(model)}, {_lit(con)})"


def _canonical_date(t: str) -> str:
    """A day that passed `_DAY` (alone or before a time) written `YYYY-MM-DD`, the form every
    warehouse reads as the engines do (Postgres read '24-01-05' as 2024, ClickHouse refused '2024-1-5')."""
    for n, zeros in ((1, "000"), (2, "00"), (3, "0")):
        t = _substitute(t, "^([0-9]{%d})-" % n, zeros + r"\1-")
    t = _substitute(t, r"^([0-9]{4})-([0-9])-", r"\1-0\2-")
    return _substitute(t, r"^([0-9]{4}-[0-9]{2})-([0-9])( |$)", r"\1-0\2\3")


def _canonical_instant(t: str) -> str:
    """An instant that passed `_RE_INSTANT` written `YYYY-MM-DD HH:MM:SS[.ffffff]`, the form every
    warehouse reads (ClickHouse gave NULL for `10:30` and for a `T`): the offset dropped, as both
    engines drop it; the fraction cut at the microsecond, as they do (Postgres would round it)."""
    t = _substitute(f"replace({t}, 'T', ' ')", rf"^([0-9-]+){_SPACES}+", r"\1 ")
    t = _substitute(t, r"^([^ ]+ [^Z+-]+)(Z|[+-].*)$", r"\1")
    t = _substitute(t, r"^([^.]*[.][0-9]{6})[0-9]+$", r"\1")
    t = _substitute(t, r"^([^.]*)[.]$", r"\1")
    t = _canonical_date(t)
    t = _substitute(t, r"^([0-9-]{10}) ([0-9]):", r"\1 0\2:")
    t = _substitute(t, r"^([0-9-]{10} [0-9]{2}):([0-9])(:|$)", r"\1:0\2\3")
    t = _substitute(t, r"^([0-9-]{10} [0-9]{2}:[0-9]{2}):([0-9])([.]|$)", r"\1:0\2\3")
    t = _substitute(t, r"^([0-9-]{10})$", r"\1 00:00:00")
    return _substitute(t, r"^([0-9-]{10} [0-9]{2}:[0-9]{2})$", r"\1:00")


# Postgres fails a text beyond the double's range ('1e400', '1e-400'), which both engines read as
# ±inf and ±0: the value is read as a numeric first. From the halfway point to 2^1024 a double is
# infinite; up to the halfway point to the smallest one, zero (the two bounds are a hair inside
# those points, so no text Postgres would refuse reaches its cast)
_BEYOND_DOUBLE, _BELOW_DOUBLE = "1.797693134862315807937e308", "2.4703282292062328e-324"


def _double_postgres(t: str) -> str:
    """A text that passed `_RE_NUMBER` as a double, in Postgres' SQL. An exponent of five digits or
    more is decided by its sign (a numeric would overflow); beyond 1000 characters, NULL."""
    sign = lambda negative, positive: f"CAST(CASE WHEN left({t}, 1) = '-' THEN '{negative}' ELSE '{positive}' END AS DOUBLE PRECISION)"  # noqa: E731
    infinite, zero, number = sign("-Infinity", "Infinity"), sign("-0", "0"), f"CAST({t} AS NUMERIC)"
    # five significant digits: 1e00005 is 100000
    large = f"{t} ~ '[eE][+]?0*[1-9][0-9]{{4,}}$'"
    return (f"(CASE WHEN length({t}) > 1000 THEN NULL "
            f"WHEN {t} ~ '[eE]-0*[1-9][0-9]{{4,}}$' OR ({large} AND {t} !~ '^[+-]?[0-9.]*[1-9]') THEN {zero} "
            f"WHEN {large} THEN {infinite} "
            f"WHEN abs({number}) >= {_BEYOND_DOUBLE} THEN {infinite} "
            f"WHEN {number} <> 0 AND abs({number}) < {_BELOW_DOUBLE} THEN {zero} "
            f"ELSE CAST({t} AS DOUBLE PRECISION) END)")


def _float_as_text(q: str, dialect: Optional[str]) -> str:
    """A float as text the way Tabularia writes it: a whole value keeps `.0` (200.0 → '200.0';
    every warehouse prints '200'). On ClickHouse, the ClickHouse engine's own rule (up to 1e15,
    its toString for the rest); elsewhere DuckDB's and Polars': `.0` up to 1e16, 'nan', 'inf',
    '-0.0' (Postgres prints 'NaN', 'Infinity', '-0')."""
    integer = f"CAST(CAST({q} AS BIGINT) AS VARCHAR) || '.0'"
    if dialect == "clickhouse":
        return f"CASE WHEN {q} = trunc({q}) AND abs({q}) < 1e15 THEN {integer} ELSE CAST({q} AS VARCHAR) END"
    return (f"CASE WHEN {q} = CAST('NaN' AS DOUBLE) THEN 'nan' WHEN {q} = CAST('inf' AS DOUBLE) THEN 'inf' "
            f"WHEN {q} = CAST('-inf' AS DOUBLE) THEN '-inf' WHEN CAST({q} AS VARCHAR) IN ('-0', '-0.0') THEN '-0.0' "
            f"WHEN {q} = trunc({q}) AND abs({q}) < 1e16 THEN {integer} ELSE CAST({q} AS VARCHAR) END")


def _portable_cast(q: str, src: Optional[str], duck_t: str, dialect: Optional[str] = None, model: Optional["_Model"] = None) -> str:
    """The engine's own cast (failed → NULL; text with its spaces trimmed; text → integer
    only for an integer literal; number → integer by truncation) without TRY_CAST, which
    the warehouses lack: a regular expression first decides whether the text converts, so
    a wrong value gives NULL instead of failing the model.
    A text the two engines read alike gives their value: where they disagree, the DuckDB
    engine's (the federated target's) or NULL. `model`: where Postgres needs its own SQL."""
    fam = _family(src)
    if duck_t == "VARCHAR" and str(src or "").upper() in ("DOUBLE", "FLOAT", "REAL"):
        return _float_as_text(q, dialect)
    if fam == "text":
        t = f"trim({q})"
        if duck_t == "BIGINT":
            return f"CASE WHEN regexp_matches({t}, {_lit(_RE_INTEGER)}) THEN CAST({t} AS BIGINT) END"
        if duck_t in ("DATE", "TIMESTAMP"):
            guard = f"regexp_matches({t}, {_lit(_RE_INSTANT if duck_t == 'TIMESTAMP' else _RE_DATA)})"
            if dialect == "clickhouse":
                guard += f" AND regexp_matches({t}, {_lit(_RE_CLICKHOUSE_YEARS)})"
            value = _canonical_instant(t) if duck_t == "TIMESTAMP" else _canonical_date(t)
            return f"CASE WHEN {guard} THEN CAST({value} AS {duck_t}) END"
        if duck_t == "DOUBLE":
            number, zero = f"CAST({t} AS DOUBLE)", ""
            if dialect == "postgres" and model is not None:
                number = model.native_text(_double_postgres(t), number)
            if dialect == "clickhouse":
                # ClickHouse reads '0e99999' as nan: a mantissa without a nonzero digit is a zero; and
                # '1e00005' as 1: the exponent without its leading zeros (one 'e' in the text: one match)
                zero = (f"WHEN regexp_matches({t}, {_lit(_RE_NUMBER)}) AND regexp_matches({t}, '^[+-]?[0.]*([eE]|$)') "
                        f"THEN CAST(CASE WHEN left({t}, 1) = '-' THEN '-0' ELSE '0' END AS DOUBLE) ")
                number = f"CAST({_substitute(t, *_EXPONENT_WITHOUT_ZEROS)} AS DOUBLE)"
            return (f"CASE WHEN lower({t}) IN ('inf', '+inf', 'infinity', '+infinity') THEN CAST('Infinity' AS DOUBLE) "
                    f"WHEN lower({t}) IN ('-inf', '-infinity') THEN CAST('-Infinity' AS DOUBLE) "
                    f"WHEN lower({t}) IN ('nan', '+nan', '-nan') THEN CAST('NaN' AS DOUBLE) "
                    f"{zero}WHEN regexp_matches({t}, {_lit(_RE_NUMBER)}) THEN {number} END")
        if duck_t == "BOOLEAN":
            # the DuckDB engine's words (Polars cannot cast a text to a boolean)
            return (f"CASE WHEN lower({t}) IN ('true', 't', '1', 'yes', 'y') THEN TRUE "
                    f"WHEN lower({t}) IN ('false', 'f', '0', 'no', 'n') THEN FALSE END")
        return f"CAST({t} AS {duck_t})"
    if duck_t == "BIGINT" and fam == "number":
        return f"CASE WHEN {q} >= {_MIN_BIGINT} AND {q} < {_BEYOND_BIGINT} THEN CAST(trunc({q}) AS BIGINT) END"
    # boolean ↔ number: Postgres has no such cast (only boolean ↔ integer); the engine's values, written out
    if fam == "boolean" and duck_t in ("BIGINT", "DOUBLE"):
        return f"CAST(CASE WHEN {q} THEN 1 WHEN NOT {q} THEN 0 END AS {duck_t})"
    if duck_t == "BOOLEAN" and fam in ("integer", "number"):
        return f"CASE WHEN {q} <> 0 THEN TRUE WHEN {q} = 0 THEN FALSE END"
    if duck_t == "BIGINT" and str(src or "").upper() in ("UBIGINT", "HUGEINT", "UHUGEINT"):
        # an integer that may not fit: ClickHouse would wrap it, Postgres fail
        return f"CASE WHEN {q} >= {_MIN_BIGINT} AND {q} < {_BEYOND_BIGINT} THEN CAST({q} AS BIGINT) END"
    return f"CAST({q} AS {duck_t})"


def _portable_predicate(column: str, operator: str, value: Any, byte: bool = False) -> str:
    """As `duckdb_ops._predicate`, without contains/starts_with/ends_with, which not
    every dialect has. `byte`: a text range in byte order (see `_byte`)."""
    c = _qi(column)
    if byte and operator in ("gt", "ge", "lt", "le", "between"):
        cc = f'{c} COLLATE "C"'
        if operator == "between":
            lo, hi = (_as_list(value) + [None, None])[:2]
            return f"{cc} BETWEEN {_lit(lo)} AND {_lit(hi)}"
        return f"{cc} {dict(gt='>', ge='>=', lt='<', le='<=')[operator]} {_lit(value)}"
    if operator in ("contains", "starts_with", "ends_with"):
        v, text = str(value), f"CAST({c} AS VARCHAR)"
        if operator == "contains":
            return f"strpos({text}, {_lit(v)}) > 0"
        return f"{'left' if operator == 'starts_with' else 'right'}({text}, {len(v)}) = {_lit(v)}"
    return _predicate(column, operator, value)


# DuckDB's escape strings (E'…') follow Postgres' rules: \b \f \n \r \t, an octal or a hex byte, any
# other character after a backslash as it is, '' a quote. DuckDB refuses a byte outside ASCII and \u.
_ESCAPE_E = re.compile(r"''|\\(?:([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|([uU])|(.))", re.DOTALL)
_ONE_ESCAPE = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_MAYBE_ESCAPE = re.compile(r"(?<![\w$])[eE]'")


def _escape_value(body: str) -> Optional[str]:
    """The value DuckDB gives the body of an E'…' string, or None where DuckDB refuses it."""
    def one(m: re.Match) -> str:
        if m.group(0) == "''":
            return "'"
        octal, hexadecimal, unicode, char = m.groups()
        if unicode:
            raise ValueError(m.group(0))
        if octal or hexadecimal:
            n = int(octal, 8) if octal else int(hexadecimal, 16)
            if not 0 < n < 0x80:
                raise ValueError(m.group(0))
            return chr(n)
        return _ONE_ESCAPE.get(char, char)

    try:
        return _ESCAPE_E.sub(one, body)
    except ValueError:
        return None


def _standard_strings(sql: str) -> str:
    """DuckDB text with every escape string (E'…') written as the standard string of the same
    value ('…' with '', its line breaks raw). sqlglot reads the escapes its own way (`\\q` and
    `\\101` stay as written, DuckDB reads `q` and `A`) and writes an escape string back without
    doubling a backslash, so DuckDB and Postgres then read `\\ ` as an escape: 'a\\ b' became
    'a b' on native Postgres and in a sql node of the federated target, `\\q` stayed `\\q` on
    ClickHouse. A standard string has no escapes: every dialect sqlglot writes keeps its bytes.
    One DuckDB refuses is left as it is."""
    if not _MAYBE_ESCAPE.search(sql):
        return sql
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import TokenType

    try:
        token = Dialect.get_or_raise("duckdb").tokenize(sql)
    except Exception:  # noqa: BLE001 — the parse that follows says what is wrong
        return sql
    out, before = [], 0
    for t in token:
        raw = sql[t.start:t.end + 1]
        if t.token_type != TokenType.BYTE_STRING or len(raw) < 3 or raw[:2] not in ("E'", "e'") or raw[-1] != "'":
            continue
        value = _escape_value(raw[2:-1])
        if value is not None:
            out += [sql[before:t.start], "'" + value.replace("'", "''") + "'"]
            before = t.end + 1
    return "".join(out) + sql[before:]


def _rewrite_query(query: str, ref: str, read: str = "duckdb", write: str = "duckdb") -> str:
    """The sql node reads its input as `self` or `input`: those names point at the
    previous step's CTE. The query stays a subquery, so it may have its own WITH. `read`: the dialect it is written in; `write`: the one it is written out in."""
    import sqlglot
    from sqlglot import exp

    if read == "duckdb":
        query = _standard_strings(query)   # sqlglot would write its escape strings back wrong
    try:
        tree = sqlglot.parse_one(query, read=read)
    except Exception as e:  # noqa: BLE001
        raise DbtExportError(f"sql: the query cannot be parsed ({e})", "sql_unreadable", detail=str(e)[:200])
    cte = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if not t.args.get("db") and name in ("self", "input") and name not in cte:
            if not t.alias:
                t.set("alias", exp.TableAlias(this=exp.to_identifier(t.name)))
            t.set("this", exp.to_identifier(ref))
    try:
        return tree.sql(dialect=write)
    except Exception as e:  # noqa: BLE001
        raise DbtExportError(f"sql: the query cannot be written for {write} ({e})", "text_not_translatable",
                             step="custom_sql", dialect=read, detail=str(e)[:200])


# ── the flow's free text (compute expressions, sql nodes) in the dialect of its engine ──
_DIALECT_NAME = {"polars": "Polars", "duckdb": "DuckDB", "clickhouse": "ClickHouse", "bigquery": "BigQuery"}
_TO_REVIEW = {
    "polars": "Written for the Polars engine, review",
    "clickhouse": "Translated from ClickHouse SQL by sqlglot, review",
    "bigquery": "Translated from BigQuery SQL by sqlglot, review",
}


def _line_break(text: str) -> str:
    """A text that may end with a `-- comment` on lines of its own: the comment would
    swallow what the export writes after it on the same line."""
    return f"\n{text}\n" if "--" in text else text


def _as_is(model: "_Model") -> bool:
    """ClickHouse text on the ClickHouse target: written as the engine runs it."""
    return model.text == "clickhouse" and model.native and model.dialect == "clickhouse"


def _expression(model: "_Model", expr: str, step: str) -> str:
    """A compute expression, between parentheses, in the model's SQL (DuckDB, which the
    native targets translate). Text of a DuckDB flow: as it is. Of a ClickHouse or
    BigQuery flow: translated by sqlglot (on the ClickHouse target, ClickHouse's text as
    the ClickHouse engine runs it). Of a Polars flow: no sqlglot dialect, kept as it is
    and listed for review — CAST to an integer, ROUND, % and window frames are Polars'."""
    if _as_is(model):
        from app.engine.chdb_ops import _utf8_functions

        return model.native_text(f"({_line_break(_utf8_functions(expr))})", f"({_line_break(_in_duckdb(expr, 'clickhouse') or expr)})")
    if model.text in ("clickhouse", "bigquery"):
        translated_one = _in_duckdb(expr, model.text)
        if translated_one is None:
            raise DbtExportError(f"{step}: the expression, written for the {_DIALECT_NAME[model.text]} engine, cannot be translated",
                                 "text_not_translatable", step=step, dialect=model.text, detail=expr[:200])
        model.to_review.append((step, model.text))
        return f"({_line_break(translated_one)})"
    if model.text == "polars":
        model.to_review.append((step, "polars"))
    return f"({_line_break(expr)})"


def _in_duckdb(expr: str, dialect: str) -> Optional[str]:
    import sqlglot

    try:
        return sqlglot.parse_one(expr, read=dialect).sql(dialect="duckdb")
    except Exception:  # noqa: BLE001
        return None


def _runs_in_duckdb(model: "_Model", active: bool, step: str) -> None:
    """Translated text DuckDB does not run (a ClickHouse function sqlglot does not know,
    like toYear) is refused here, with DuckDB's reason, instead of at the team's `dbt run`."""
    if model.text not in ("clickhouse", "bigquery") or _as_is(model) or not active:
        return
    # text that reads a column of a type the export cannot name does not bind against the probe's
    # placeholder: that says nothing about the warehouse, where the column has its real type
    if not model.probe.align(model) and model.probe.error and model.probe.error[0] == step and not model.probe.for_unknown:
        raise DbtExportError(f"{step}: the text, written for the {_DIALECT_NAME[model.text]} engine, does not run in DuckDB "
                             f"after the translation ({model.probe.error[1]})", "text_not_translatable",
                             step=step, dialect=model.text, detail=model.probe.error[1])


def _column_list(cols: list[str], expr_: Optional[dict] = None, names: Optional[dict] = None) -> str:
    """An explicit projection: `expression AS "name"` for every column, in order. The
    native target is ALWAYS written so, never `*`: sqlglot, expanding a `*`, writes the
    aliases without quotes and the target dialect lowercases them («Città» → «città»,
    another column in the warehouse). With the list written here the names stay the
    flow's."""
    expr_, names = expr_ or {}, names or {}
    return ", ".join(f"{expr_.get(c, _qi(c))} AS {_qi(names.get(c, c))}" for c in cols)


def _needs_list(model: "_Model", s: "_S", what: str) -> bool:
    """The native target needs the column list; when it is not known (after a sql node)
    DuckDB's modifiers (EXCLUDE/REPLACE/RENAME) have no equivalent."""
    if not model.native:
        return False
    if not s.cols:
        raise DbtExportError(
            f"{what}: the columns are not known at this point of the flow (after a sql node): "
            "the native export needs them. Use the «federated» export, or have the sql node list its columns.",
            "columns_unknown_after_sql", step=what,
        )
    return True


_MADE_UP_NAME = re.compile(r"_col_\d+")   # sqlglot's name for an expression without an alias


def _query_columns(query: str, ref: str, s: "_S") -> list[str]:
    """The columns a sql node returns, worked out from the query and the previous step's
    columns (`*` included). The next steps need them, and in the native target the
    explicit lists. When they cannot be worked out: [] (the next steps use `*`). An expression
    without an alias (`i + 1`, `count(*)`, an unexpanded COLUMNS) has a name sqlglot
    makes up (`_col_1`) that DuckDB does not give it: then [] too."""
    if not s.cols:
        return []
    names = _query_names(query, ref, s)
    return [] if any(_MADE_UP_NAME.fullmatch(n) for n in names) else names


def _with_duckdb_names(model: "_Model", query: str) -> str:
    """The query with every expression of its SELECT that has no alias named as DuckDB
    names it (`i + 1` → "(i + 1)", `count(*)` → "count_star()"): the probe reads the name
    of each one on its own (the query with only that expression), so a `*` next to them
    does not matter. If a name cannot be read, the query as it was."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(query, read="duckdb")
    except Exception:  # noqa: BLE001
        return query
    if not isinstance(tree, exp.Select):
        return query
    without = [i for i, e in enumerate(tree.expressions)
             if not isinstance(e, (exp.Alias, exp.Column, exp.Star)) and not (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))]
    if not without:
        return query
    for i in without:
        one_ = tree.copy()
        one_.set("expressions", [tree.expressions[i].copy()])
        read_value = model.probe.columns(model, one_.sql(dialect="duckdb"))
        if not read_value or len(read_value) != 1:
            return query
        tree.expressions[i].replace(exp.alias_(tree.expressions[i].copy(), read_value[0][0], quoted=True))
    return tree.sql(dialect="duckdb")


def _query_names(query: str, ref: str, s: "_S") -> list[str]:
    if not s.cols:
        return []
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.schema import MappingSchema

    try:
        tree = qualify(sqlglot.parse_one(query, read="duckdb"),
                         schema=MappingSchema({ref: {c: "VARCHAR" for c in s.cols}}, dialect="postgres", normalize=False),
                         dialect="postgres", validate_qualify_columns=False)
        names = []
        for e in tree.selects:
            # a `*` expanded by sqlglot has a lowercase alias: the true name is the column's
            if isinstance(e, exp.Alias) and isinstance(e.this, exp.Column) and e.alias.lower() == e.this.name.lower():
                names.append(e.this.name)
            else:
                names.append(e.alias_or_name)
        return names if all(names) and len(set(names)) == len(names) and "*" not in names else []
    except Exception:  # noqa: BLE001
        return []


def _re2(text: str) -> str:
    """A name as a literal in a regular expression (RE2, DuckDB's): only its
    metacharacters escaped (RE2 refuses an escaped space or letter)."""
    return "".join("\\" + ch if ch in "\\.+*?()|[]{}^$" else ch for ch in str(text))


_RIGHT_MARK = "__tabularia_right"   # a right-side column, until it gets its name (see _dynamic_join)


def _dynamic_join(model: "_Model", ref: str, s: "_S", r_ref: str, rs: "_S", join_sql: str,
                   skip_right: set, coalesce_keys: set) -> tuple[str, "_S"]:
    """A join where one side's columns are NOT known (after a pivot): the list cannot be
    written, and DuckDB alone would rename a clash `x_1` instead of `x_right`. So the
    right side's columns first carry a mark, then get their names in a second step:
    `x_right` if the left side has an `x`, `x` otherwise — Tabularia's rule. When the left
    side is the unknown one, which names clash is decided by the data: the names are left
    as they are and the run stops if one clashes (UNION BY NAME refuses a name twice),
    instead of renaming it in silence."""
    q = _qi
    if s.cols:
        left = ", ".join(f"COALESCE({ref}.{q(c)}, {r_ref}.{q(c)}) AS {q(c)}" if c in coalesce_keys else f"{ref}.{q(c)}"
                             for c in s.cols)
    else:
        left = f"{ref}.*" + (" REPLACE (" + ", ".join(f"COALESCE({ref}.{q(k)}, {r_ref}.{q(k)}) AS {q(k)}" for k in sorted(coalesce_keys)) + ")"
                                 if coalesce_keys else "")
    if rs.cols:
        rights = [c for c in rs.cols if c not in skip_right]
        right = ", ".join(f"{r_ref}.{q(c)} AS {q(c + _RIGHT_MARK)}" for c in rights)
    else:
        excluded = f" EXCLUDE ({', '.join(q(k) for k in sorted(skip_right))})" if skip_right else ""
        right = f"COLUMNS({r_ref}.*{excluded}) AS '\\0{_RIGHT_MARK}'"
    if not right:
        return model.add(f"SELECT {left} {join_sql}"), _S([])
    joined = model.add(f"SELECT {left}, {right} {join_sql}")
    on_the_left = f"COLUMNS(c -> NOT ends_with(c, '{_RIGHT_MARK}'))"
    mark = _RIGHT_MARK[: -len("_right")]
    # the left side's names known here: all of them, or (its columns unknown) the pivot's index and
    # the columns made or renamed after it, which a join back on the index meets
    known = s.cols or list(s.types)
    if known:
        alternative = "|".join(_re2(c).replace("'", "''") for c in known)
        names = f"COLUMNS('(?s)^({alternative}){mark}(_right)$|^(.*){_RIGHT_MARK}$') AS '\\1\\2\\3'"
    else:
        names = f"COLUMNS('(?s)^(.*){_RIGHT_MARK}$') AS '\\1'"
    items_list = f"SELECT {on_the_left}, {names} FROM {joined}"
    if s.cols:
        return model.add(items_list, note="the right side's columns get their names: _right on those the left side also has"), _S([])
    return model.add(f"({items_list}) UNION ALL BY NAME ({items_list} LIMIT 0)",
                     note="the right side's columns get their names: _right on those the left side is known to have; the run "
                          "stops if one is also a column the pivot made (Tabularia would add _right)"), _S([])


# ── the operations ───────────────────────────────────────────────────────────
def _first_last(model: "_Model", func: str, col: str, order: Optional[list], kind_of_: Optional[Callable] = None) -> str:
    """first/last as the engine (Polars) gives them: the first/last row of the group in the
    order of the sort before it — DuckDB's parallel aggregate does not keep the order of the
    step before, so the order is written in the aggregate. Without a sort, any row (NULL
    included). Postgres has no first/last: the first/last element of the ordered
    array_agg. ClickHouse has neither ordered aggregates nor arrays that keep NULL: the
    (keys, value) tuples of the group, sorted with NULL keys last; without a sort,
    any/anyLast as the ClickHouse engine."""
    q = _qi
    key = lambda k: _byte(model, q(k), kind_of_(k)) if kind_of_ else q(k)   # noqa: E731
    key_names = ", ".join(f"{key(k)} {'DESC' if desc else 'ASC'} NULLS LAST" for k, desc in order) if order else ""
    duck = f"{func}({q(col)}{' ORDER BY ' + key_names if key_names else ''})"
    if not model.native:
        return duck
    if model.dialect == "postgres":
        items_list = f"(array_agg({q(col)}{' ORDER BY ' + key_names if key_names else ''}))"
        return f"{items_list}[1]" if func == "first" else f"{items_list}[count(*)]"
    if model.dialect == "clickhouse":
        if not order:
            return model.native_text(f"{'any' if func == 'first' else 'anyLast'}({q(col)})", duck)
        n = len(order)
        elem = lambda i: f"tupleElement(t, {i})"   # noqa: E731
        if order[0][1]:   # every key has the sort's one direction
            key = ", ".join(f"NOT isNull({elem(i + 1)}), {elem(i + 1)}" for i in range(n))
            sorted_ = f"arrayReverseSort(t -> ({key}), groupArray(tuple({', '.join(q(k) for k, _ in order)}, {q(col)})))"
        else:
            key = ", ".join(f"isNull({elem(i + 1)}), {elem(i + 1)}" for i in range(n))
            sorted_ = f"arraySort(t -> ({key}), groupArray(tuple({', '.join(q(k) for k, _ in order)}, {q(col)})))"
        return model.native_text(f"tupleElement({sorted_}[{'1' if func == 'first' else '-1'}], {n + 1})", duck)
    raise DbtExportError(f"group_by: «{func}» has no equivalent in the {model.dialect} dialect", "groupby_unsupported_func", func=func)


def _mean_of_instants(model: "_Model", func: str, c: str, duck: str) -> str:
    """The mean or the median of dates or instants, an instant as the engines give it (Postgres and
    ClickHouse have neither avg nor median of a date: the model failed). The mean is computed over
    the microseconds since 1970 as an exact number, rounded to the microsecond; the median is the
    midpoint of the two middle values. `duck`: DuckDB's own form, for the probe."""
    if model.dialect == "postgres":
        instant = f"CAST({c} AS TIMESTAMP)"
        if func == "median":
            basso = f"percentile_disc(0.5) WITHIN GROUP (ORDER BY {instant})"
            alto = f"percentile_disc(0.5) WITHIN GROUP (ORDER BY {instant} DESC)"
            return model.native_text(f"({basso} + ({alto} - {basso}) / 2)", duck)
        us, day = f"avg(extract(epoch FROM {instant}) * 1000000)", "86400000000"
        # whole days and the microseconds left, each exact as a double (the whole span would not be)
        return model.native_text(f"(TIMESTAMP '1970-01-01 00:00:00' + CAST(floor({us} / {day}) AS DOUBLE PRECISION) * INTERVAL '1 day' "
                                  f"+ CAST(round({us} - floor({us} / {day}) * {day}) AS DOUBLE PRECISION) * INTERVAL '1 microsecond')", duck)
    if model.dialect == "clickhouse":
        us = f"toUnixTimestamp64Micro(toDateTime64({c}, 6))"
        if func == "median":
            return model.native_text(f"fromUnixTimestamp64Micro(toInt64(round(quantileExactInclusive(0.5)({us}))))", duck)
        # an exact sum (avg of a decimal is a Float64 on ClickHouse); a group of NULLs divides NULL by 1
        return model.native_text(f"fromUnixTimestamp64Micro(toInt64(round(sum(toDecimal128({us}, 6)) / greatest(count({c}), 1))))", duck)
    return duck


def _aggregato(model: "_Model", func: str, col: str, kind: Optional[str], order: Optional[list] = None,
               kind_of_: Optional[Callable] = None) -> tuple[str, Optional[str]]:
    """An aggregate as the engine computes it, and the type it gives (None: not known).
    The engine brings a HUGEINT back to BIGINT (a sum of integers or of booleans, which
    count the true ones); a sum of DECIMAL(p,s) is DECIMAL(38,s); a mean or a median of
    dates is a timestamp. Those types are what a model reading this one is given."""
    q = _qi
    fam = _family(kind)
    if func in ("first", "last"):
        return _first_last(model, func, col, order, kind_of_), kind
    expr = f"count(DISTINCT {q(col)})" if func == "n_unique" else f"{_AGG[func]}({q(col)})"
    if func in ("mean", "median") and (fam == "date" or fam.startswith("timestamp")):
        return (_mean_of_instants(model, func, q(col), expr) if model.native else expr), "TIMESTAMP"
    if func in ("mean", "std", "var") and fam == "boolean":
        # a boolean counts as 1 and 0, as in Polars: DuckDB and Postgres have no avg of a boolean
        expr = f"{_AGG[func]}(CAST({q(col)} AS INTEGER))"
    if func in ("min", "max"):
        expr = f"{_AGG[func]}({_byte(model, q(col), kind)})"     # text: by bytes
    if func in ("std", "var") and model.dialect == "clickhouse":
        # ClickHouse gives NaN for a single value: NULL, as the engine (and DuckDB, Postgres)
        expr = f"CASE WHEN count({q(col)}) > 1 THEN {expr} END"
    if func in ("mean", "std", "var") and model.native and model.dialect == "postgres" and fam in ("integer", "number", "boolean"):
        # Postgres gives a numeric for an integer or a decimal ('1.5000000000000000' as text): Tabularia's double
        expr = f"CAST({expr} AS DOUBLE)"
    if func in ("min", "max") and fam == "boolean" and model.dialect == "postgres":
        # Postgres has no min/max of a boolean: false < true, so they are bool_and/bool_or
        return f"{'bool_and' if func == 'min' else 'bool_or'}({q(col)})", kind
    if func in ("count", "n_unique"):
        return expr, "BIGINT"
    if func == "sum" and fam in ("integer", "boolean"):
        # as the engine: a sum of integers is BIGINT (DuckDB would give HUGEINT); a boolean is
        # summed as an integer, which every warehouse can (Postgres has no sum(boolean))
        arg = f"CAST({q(col)} AS INTEGER)" if fam == "boolean" else q(col)
        return f"CAST(sum({arg}) AS BIGINT)", "BIGINT"
    if func == "sum":
        m = re.fullmatch(r"DECIMAL\((\d+),\s*(\d+)\)", str(kind or "").upper())
        return expr, (f"DECIMAL(38,{m.group(2)})" if m else ("DOUBLE" if fam == "number" else None))
    if func in ("mean", "std", "var"):
        return expr, "DOUBLE"
    if func == "median":
        return expr, (kind if str(kind or "").upper().startswith("DECIMAL") else "DOUBLE")
    return expr, kind


def _compile_op(model: _Model, ref: str, s: _S, op_type: str, params: dict,
                resolve: SourceResolver, right_namer: _Namer) -> tuple[str, _S]:
    """Writes the CTE(s) of an operation. Returns (new ref, new schema)."""
    q, nat = _qi, model.native
    cols, types = s.cols, s.types

    # in the native target, the steps that do not change the columns list them instead of `*`
    all_ = _column_list(cols) if (nat and cols) else "*"

    if op_type == "select":
        c = _require(params, "columns")
        sel = _column_list(c) if nat else ", ".join(q(x) for x in c)
        return model.add(f"SELECT {sel} FROM {ref}"), _S(c, {x: s.kind(x) for x in c})

    if op_type == "reorder":
        order = _require(params, "columns")
        new_ones = list(order) + [c for c in cols if c not in set(order)]
        if _needs_list(model, s, "reorder"):
            return model.add(f"SELECT {_column_list(new_ones)} FROM {ref}"), _S(new_ones, types)
        qs = ", ".join(q(x) for x in order)
        # with the columns not known, the listed ones are only the first of them
        after = _S(new_ones, types) if cols else _S([], types, s.others)
        return model.add(f"SELECT {qs}, * EXCLUDE ({qs}) FROM {ref}"), after

    if op_type == "drop":
        # the node's order, once each: a set's order changes with the process (PYTHONHASHSEED),
        # and with it the file — the same flow must give the same zip on every engine
        drop = list(dict.fromkeys(_require(params, "columns")))
        remaining = [x for x in cols if x not in set(drop)]
        if _needs_list(model, s, "drop"):
            return model.add(f"SELECT {_column_list(remaining)} FROM {ref}"), _S(remaining, types)
        sql = f"SELECT * EXCLUDE ({', '.join(q(x) for x in drop)}) FROM {ref}"
        # with the columns not known, `types` lists the ones known to be there: not the dropped ones
        return model.add(sql), _S(remaining, {c: t for c, t in types.items() if c not in set(drop)}, s.others)

    if op_type == "rename":
        mapping = _require(params, "mapping")
        new_ones = [mapping.get(x, x) for x in cols]
        nt = {mapping.get(k, k): v for k, v in types.items()}
        if _needs_list(model, s, "rename"):
            return model.add(f"SELECT {_column_list(cols, names=mapping)} FROM {ref}"), _S(new_ones, nt)
        pairs = ", ".join(f"{q(o)} AS {q(n)}" for o, n in mapping.items())
        return model.add(f"SELECT * RENAME ({pairs}) FROM {ref}"), _S(new_ones, nt, s.others)

    if op_type == "cast":
        casts = _require(params, "columns")
        repls, new_items = [], dict(types)
        for col, dt in casts.items():
            duck_t = _DUCK_DTYPES.get(str(dt))
            if not duck_t:
                raise DbtExportError(f"cast: unsupported type '{dt}'", "cast_unsupported_type", type=dt)
            src = s.kind(col)
            if not src:
                # the engine casts from the column's real type (trunc for a number, trim and the
                # integer guard for a text): without it, a bare cast would round or accept '12.7'
                raise DbtExportError(f"cast: the type of the column «{col}» is not known at this point of the flow, "
                                     "so the cast cannot be written as Tabularia does it", "cast_type_unknown", column=col)
            expr_ = _portable_cast(q(col), src, duck_t, model.dialect, model) if nat else _cast_expr(q(col), src, duck_t)
            repls.append((col, expr_))
            new_items[col] = duck_t
        if _needs_list(model, s, "cast"):
            return model.add(f"SELECT {_column_list(cols, expr_=dict(repls))} FROM {ref}"), _S(cols, new_items)
        return model.add(f"SELECT * REPLACE ({', '.join(f'{e} AS {q(c)}' for c, e in repls)}) FROM {ref}"), _S(cols, new_items, s.others)

    if op_type == "filter":
        col, operator, value = _require(params, "column"), params.get("operator", "eq"), params.get("value")
        try:
            # the engines' own check: compared with NULL the filter kept no row, without a word
            check_filter_value(operator, value)
        except EngineError as e:
            raise DbtExportError(f"filter on «{col}»: {e}", "filter_value_missing", column=col, operator=operator) from e
        pred = _portable_predicate(col, operator, value, _per_byte(model, s.kind(col))) if nat else _predicate(col, operator, value)
        return model.add(f"SELECT {all_} FROM {ref} WHERE {pred}"), s

    if op_type == "sort":
        _require(params, "by")
        # the keys and a direction each, as the engines (operations.sort_keys)
        keys = sort_keys(params, cols or None)
        if not keys:
            return ref, s
        # the cross-engine standard: NULL always last (as the engine)
        order = ", ".join(f"{_byte(model, q(c), s.kind(c))} {'DESC' if d else 'ASC'} NULLS LAST" for c, d in keys)
        return model.add(f"SELECT {all_} FROM {ref} ORDER BY {order}"), s

    if op_type == "limit":
        return model.add(f"SELECT {all_} FROM {ref} LIMIT {int(_require(params, 'n'))}"), s

    if op_type == "unique":
        subset = params.get("subset")
        if subset:
            part = ", ".join(q(c) for c in subset)
            return model.add(f"SELECT {all_} FROM {ref} QUALIFY row_number() OVER (PARTITION BY {part}) = 1"), s
        return model.add(f"SELECT DISTINCT {all_} FROM {ref}"), s

    if op_type == "fill_null":
        fills = _require(params, "columns")
        if _needs_list(model, s, "fill_null"):
            return model.add(f"SELECT {_column_list(cols, expr_={c: f'COALESCE({q(c)}, {_lit(v)})' for c, v in fills.items()})} FROM {ref}"), s
        repl = ", ".join(f"COALESCE({q(c)}, {_lit(v)}) AS {q(c)}" for c, v in fills.items())
        return model.add(f"SELECT * REPLACE ({repl}) FROM {ref}"), s

    if op_type == "drop_nulls":
        subset = params.get("subset") or cols
        if not subset:
            raise DbtExportError("drop_nulls: the columns are not known at this point of the flow (after a sql node or a pivot): list them in the node", "drop_nulls_columns_unknown")
        cond = " AND ".join(f"{q(c)} IS NOT NULL" for c in subset)
        return model.add(f"SELECT {all_} FROM {ref} WHERE {cond}"), s

    if op_type == "group_by":
        by = _as_list(_require(params, "by"))
        aggs = _require(params, "aggregations")
        parts, out_cols, out_t = [", ".join(q(c) for c in by)], list(by), {c: s.kind(c) for c in by}
        for agg in aggs:
            col, func = agg["column"], agg.get("func", "sum")
            if func not in _AGG:
                raise DbtExportError(f"group_by: unsupported function '{func}'", "groupby_unsupported_func", func=func)
            alias = agg.get("alias") or f"{col}_{func}"
            expr, t = _aggregato(model, func, col, s.kind(col), s.order, s.kind)
            parts.append(f"{expr} AS {q(alias)}")
            out_cols.append(alias)
            out_t[alias] = t
        by_sql = ", ".join(q(c) for c in by)
        return model.add(f"SELECT {', '.join(parts)} FROM {ref} GROUP BY {by_sql}"), _S(out_cols, out_t)

    if op_type == "compute":
        columns = _require(params, "columns")  # [{name, expr}]
        cur, out_cols, out_t = ref, list(cols), dict(types)
        for c in columns:
            name, expr = str(c.get("name") or "").strip(), str(c.get("expr") or "").strip()
            if not name or not expr:
                raise DbtExportError("compute: the name and the expression are required", "compute_missing")
            # no «scalar expressions only» check here: in the engine it protects OUR
            # process (the expression runs inside Tabularia); the dbt project runs in
            # the exporter's environment, and must reproduce the flow as it is (that
            # check, matching words, refused EXTRACT(YEAR FROM …) too)
            if nat and not out_cols:
                _needs_list(model, _S([]), "compute")
            step = dict(step_name=f"compute_{name}", note=f"{name} = {expr}" + (" (replaces the column, in place)" if name in out_cols else ""))
            active = model.probe.active
            step_name = model._next_name(step["step_name"])
            expr_ = _expression(model, expr, step_name)
            if not out_cols:
                # the columns are not known (after a pivot): the column is replaced where it is if
                # it exists, added at the end otherwise — like the engine, which sees the real ones
                line = "__tabularia_row"
                cur = model.add(f"SELECT unnest(struct_update({line}, {q(name)} := {expr_})) FROM {cur} AS {line}", **step)
            elif name in out_cols:
                # an existing column: overwritten IN ITS PLACE (as the engine)
                if nat:
                    cur = model.add(f"SELECT {_column_list(out_cols, expr_={name: expr_})} FROM {cur}", **step)
                else:
                    cur = model.add(f"SELECT * REPLACE ({expr_} AS {q(name)}) FROM {cur}", **step)
            else:
                cur = model.add(f"SELECT {_column_list(out_cols) if nat else '*'}, {expr_} AS {q(name)} FROM {cur}", **step)
                out_cols.append(name)
            _runs_in_duckdb(model, active, cur)
            out_t[name] = None
        return cur, _S(out_cols, out_t, s.others)

    if op_type == "sql":
        text = str(_require(params, "query")).strip()
        step_name = model._next_name(model.labels[-1][0] if model.labels else "custom_sql")
        if _as_is(model):
            # ClickHouse's query on the ClickHouse target, as the engine runs it (only `self` pointed
            # at the step before); its names are ClickHouse's: known only where it gives them
            finale = _rewrite_query(text, ref, read="clickhouse", write="clickhouse")
            try:
                query = _rewrite_query(text, ref, read="clickhouse")
            except DbtExportError:
                query = finale
            output = _query_columns(query, ref, s)
            source = model.native_text(f"(\n{finale}\n)", f"({query})")
            if output:
                return model.add(f"SELECT {_column_list(output)} FROM {source} AS _q"), _S(output)
            model.probe.switch_off()
            return model.add(f"SELECT * FROM {source} AS _q"), _S([])
        query = _rewrite_query(text, ref, read="duckdb" if model.text in ("duckdb", "polars") else model.text)
        if model.text != "duckdb":
            model.to_review.append((step_name, model.text))
        if nat:
            # the warehouse would name an expression without an alias its own way (Postgres:
            # '?column?') and sqlglot makes one up (`_col_1`): it gets the name DuckDB gives it,
            # as in the federated target and in the DuckDB engine
            query = _with_duckdb_names(model, query)
        output = _query_columns(query, ref, s)
        # federated: the names of an expression without an alias are DuckDB's, the probe reads them
        # after the step; native: the list, when it is known
        active = model.probe.active
        cur = model.add(f"SELECT {_column_list(output) if (nat and output) else '*'} FROM ({query}) AS _q")
        _runs_in_duckdb(model, active, cur)
        if nat and not output:
            model.probe.switch_off()   # the warehouse names these columns itself: the probe's names are not its
        return cur, _S(output)

    if op_type in ("join", "union"):
        r_ref, rs = _compile_right(model, _require(params, "right"), resolve, right_namer)
        if op_type == "union":
            merged = cols + [c for c in rs.cols if c not in set(cols)]
            mt = {c: (types.get(c) if c in types else rs.types.get(c)) for c in merged}
            for c in merged:
                if c in types and c in rs.types and types[c] != rs.types[c]:
                    mt[c] = None
            if params.get("strategy") == "strict":
                if nat and cols and rs.cols:
                    return model.add(f"SELECT {_column_list(cols)} FROM {ref} UNION ALL SELECT {_column_list(rs.cols, names=dict(zip(rs.cols, cols)))} FROM {r_ref}"), _S(cols, mt)
                return model.add(f"SELECT * FROM {ref} UNION ALL SELECT * FROM {r_ref}"), _S(cols, mt)
            if not nat:
                # by name: with one side's columns not known, neither are the result's
                return model.add(f"SELECT * FROM {ref} UNION ALL BY NAME SELECT * FROM {r_ref}"), (_S(merged, mt) if cols and rs.cols else _S([]))
            if not cols or not rs.cols:
                raise DbtExportError("union by name: the columns of one of the two branches are not known (after a sql node or a pivot): the native export needs them",
                                     "union_columns_unknown")
            side = lambda have: ", ".join(q(c) if c in set(have) else f"NULL AS {q(c)}" for c in merged)
            return model.add(f"SELECT {side(cols)} FROM {ref} UNION ALL SELECT {side(rs.cols)} FROM {r_ref}"), _S(merged, mt)
        how = params.get("how", "inner")
        lset = set(cols)
        if nat and how not in ("semi", "anti"):
            _needs_list(model, s, "join")
            _needs_list(model, rs, "join")
        dynamic = not nat and (not cols or not rs.cols) and how not in ("semi", "anti")
        if how == "cross":
            if dynamic:
                return _dynamic_join(model, ref, s, r_ref, rs, f"FROM {ref} CROSS JOIN {r_ref}", set(), set())
            sel = _join_select(ref, cols, r_ref, rs.cols, set())
            out = cols + [c if c not in lset else c + "_right" for c in rs.cols]
            ot = {**types, **{(c if c not in lset else c + "_right"): rs.types.get(c) for c in rs.cols}}
            return model.add(f"SELECT {sel} FROM {ref} CROSS JOIN {r_ref}"), _S(out, ot)
        clause, kind = _join_condition(ref, r_ref, params)
        if how in ("semi", "anti"):
            sel = ", ".join(f"{ref}.{q(c)} AS {q(c)}" for c in cols) if (nat and cols) else f"{ref}.*"
            if nat and model.dialect == "clickhouse":
                # ClickHouse has no correlated EXISTS; it has LEFT SEMI/ANTI JOIN, the engine's own
                return model.add(f"SELECT {sel} FROM {ref} LEFT {how.upper()} JOIN {r_ref} {clause}", probe=f"SELECT * FROM {ref}"), s
            if nat:
                # SEMI/ANTI JOIN do not exist in the other warehouses: EXISTS / NOT EXISTS
                cond = clause[len("ON "):] if clause.startswith("ON ") else " AND ".join(
                    f"{ref}.{q(k)} = {r_ref}.{q(k)}" for k in _as_list(params["on"]))
                neg = "NOT " if how == "anti" else ""
                return model.add(f"SELECT {sel} FROM {ref} WHERE {neg}EXISTS (SELECT 1 FROM {r_ref} WHERE {cond})"), s
            return model.add(f"SELECT {sel} FROM {ref} {how.upper()} JOIN {r_ref} {clause}"), s
        if how not in _JOIN_KW:
            raise DbtExportError(f"join: unsupported type '{how}'", "join_unsupported", how=how)
        skip_right = set(_as_list(params["on"])) if kind == "using" else set()
        # in right/full joins the USING keys are COALESCE(left, right), as in the engine
        if dynamic:
            return _dynamic_join(model, ref, s, r_ref, rs, f"FROM {ref} {_JOIN_KW[how]} {r_ref} {clause}", skip_right,
                                  skip_right if how in ("full", "right") else set())
        sel = _join_select(ref, cols, r_ref, rs.cols, skip_right, skip_right if how in ("full", "right") else set())
        dest = [c for c in rs.cols if c not in skip_right]
        out = list(cols) + [(c + "_right" if c in lset else c) for c in dest]
        ot = {**types, **{(c + "_right" if c in lset else c): rs.types.get(c) for c in dest}}
        return model.add(f"SELECT {sel} FROM {ref} {_JOIN_KW[how]} {r_ref} {clause}"), _S(out, ot)

    if op_type == "pivot":
        if nat:
            raise DbtExportError(
                "The «pivot» node produces columns decided by the data at runtime: in dbt it can only be written "
                "with DuckDB's PIVOT. Use the «federated» export (dbt-duckdb), or "
                "replace the pivot with a sql node that lists the columns.", "pivot_not_portable"
            )
        index = _as_list(_require(params, "index"))
        on = _as_list(_require(params, "on"))
        values = _require(params, "values")
        func = params.get("func", "sum")
        if func not in _AGG:
            raise DbtExportError(f"pivot: unsupported function '{func}'", "pivot_unsupported_func", func=func)
        # the cross-engine label, as the engine: the value as text, NULL → 'null',
        # several columns joined by the separator
        on_sql = f" || '{PIVOT_LABEL_SEP}' || ".join(f"coalesce(CAST({q(c)} AS VARCHAR), 'null')" for c in on)
        idx_sql = ", ".join(q(c) for c in index)
        using = _first_last(model, func, values, s.order) if func in ("first", "last") else f"{_AGG[func]}({q(values)})"
        if func == "sum" and _family(s.kind(values)) in ("integer", "boolean"):
            using = f"CAST({using} AS BIGINT)"      # as the engine: HUGEINT back to BIGINT
        # the new columns are not known (the data decides them), their type is: the aggregate's
        return (model.add(f"PIVOT {ref} ON {on_sql} USING {using} GROUP BY {idx_sql}"),
                _S([], {c: s.kind(c) for c in index}, _aggregato(model, func, values, s.kind(values))[1]))

    if op_type == "unpivot":
        on = params.get("on")
        index = params.get("index") or []
        var = params.get("variable_name") or "variable"
        val = params.get("value_name") or "value"
        melt = on if on else [c for c in cols if c not in set(index)]
        if not melt:
            raise DbtExportError("unpivot: no columns to turn into rows", "unpivot_nothing")
        types_ = [s.kind(c) for c in melt]
        # the engine's supertype of the melted columns; with a type not known, the union of the
        # SELECTs below finds it (never text by default: a number stays a number)
        vtype = unpivot_value_type([t for t in types_]) if all(types_) else None
        keep = [c for c in cols if c in set(index)] if cols else list(index)
        # a portable form, the same in both targets: one SELECT per unpivoted column, NULLs
        # included (as the engine, which uses UNPIVOT INCLUDE NULLS)
        value_sql = (lambda m: f"CAST({q(m)} AS {vtype})") if vtype else (lambda m: q(m))
        pieces = [
            "SELECT " + ", ".join([q(c) for c in keep] + [f"{_lit(m)} AS {q(var)}", f"{value_sql(m)} AS {q(val)}"]) + f" FROM {ref}"
            for m in melt
        ]
        out = keep + [var, val]
        return model.add(" UNION ALL ".join(pieces)), _S(out, {**{c: s.kind(c) for c in keep}, var: "VARCHAR", val: vtype})

    if op_type == "foreach":
        raise DbtExportError(
            "The «foreach» node iterates at runtime over a driver table and has no "
            "dbt equivalent: leave it out of the flow or export the branches separately.", "foreach_not_exportable"
        )

    raise DbtExportError(f"operation '{op_type}' cannot be exported to dbt", "op_not_exportable", op=op_type)


def _resolve(resolve: SourceResolver, spec: dict) -> tuple[str, list[str], dict, str]:
    """(ref, columns, types, readable name). The name comes from the resolver when it
    gives one, otherwise from the dbt reference itself (`source('…', 'orders')` → orders)."""
    r = resolve(spec)
    ref, cols, types_ = r[0], r[1], r[2]
    name = (r[3] if len(r) > 3 and r[3] else None) or ref_label(ref) or spec.get("model") or spec.get("seed") or "source"
    return ref, cols, types_, name


def ref_label(ref: str) -> Optional[str]:
    """The table or model a dbt reference names: `source('…', 'ordini')` → ordini."""
    m = re.search(r"source\('[^']*', *'([^']*)'\)|ref\('([^']*)'\)", ref or "")
    return (m.group(1) or m.group(2)) if m else None


def _base(model: _Model, src_ref: str, s: _S, name: str = "source", right: bool = False, typed: bool = True) -> tuple[str, _S]:  # noqa: C901
    """A model's first step: the source with the TYPES Tabularia gave it at the import,
    so the project sees the data as the flow does (a warehouse numeric Tabularia reads
    as text). Another model of the project
    (`tipizza=False`) is read as it is: its types are the ones its own SQL gives, and a
    second cast to recorded types could only be wrong. A column of a type the export cannot
    write (an array, a uuid, bytea) is passed through as it is, and only it: it used to switch
    off the casts of all the others. Returns the step and the schema after it."""
    known = bool(s.cols) and all(s.types.get(c) for c in s.cols)
    typed_items = typed and any(s.types.get(c) for c in s.cols)
    step = dict(step_name=f"{'right_' if right else ''}source_{name}",
                 note=f"{'right side, ' if right else ''}source: {name}" + (", with the column types Tabularia reads" if typed_items else "")
                 + ("" if known or not typed_items else " (the columns of other types as they are)")
                 + (", and the texts the seed writes as `~…` read back" if typed and SEED_TEXT in s.types.values() else ""))
    if typed_items:
        sql = f"SELECT {_typed_projection(model.dialect, s.cols, s.types)} FROM {src_ref}"
    elif model.native and s.cols:
        sql = f"SELECT {_column_list(s.cols)} FROM {src_ref}"
    else:
        sql = f"SELECT * FROM {src_ref}"
    after = _S(s.cols, {c: _typed_type(t) for c, t in s.types.items()}) if s.cols else s
    _refuse_uppercase(model, s, step["step_name"])
    _refuse_long(model, s, step["step_name"])
    # the probe reads an empty table with those types, never the source; a column of a type the export
    # cannot name is a struct no warehouse type is: passed on, it stays not known (a struct is not a
    # type the probe reports), and a step that reads it as anything else stops the probe there. It
    # used to stop the probe for the whole model, and with it the casts of the other columns
    empty = "SELECT " + ", ".join(f"CAST(NULL AS {after.types.get(c) or _UNKNOWN_TYPE}) AS {_qi(c)}" for c in s.cols) + " LIMIT 0" \
        if s.cols and not _equal_but_for_case(s.cols) else None
    if empty is None:
        model.probe.switch_off()
    return model.add(sql, probe=empty, **step), after


def _compile_right(model: _Model, ref_spec: dict, resolve: SourceResolver, right_namer: _Namer):
    """Compiles the right side of a join or union (a sub-flow {source, operations} or a
    plain source) as extra CTEs of the model."""
    spec = ref_spec["source"] if "source" in ref_spec else ref_spec
    src_ref, src_cols, src_types, name = _resolve(resolve, spec)
    base, s = _base(model, src_ref, _S(src_cols, src_types), name, right=True, typed="model" not in spec)
    return _compile_ops(model, base, s, ref_spec.get("operations") or [] if "source" in ref_spec else [], resolve, right_namer)


def _v(value: Any) -> str:
    return ", ".join(_v(x) for x in value) if isinstance(value, list) else (_lit(value) if not isinstance(value, str) else f"'{value}'")


def _label(op_type: str, params: dict, resolve: SourceResolver, cols: Optional[list] = None) -> tuple[str, str]:
    """A step's name and note, from what it does."""
    p = params or {}
    col = p.get("column")
    by = _as_list(p.get("by")) if p.get("by") is not None else []
    if op_type == "sort" and p.get("ignore_missing") and cols:
        by = [c for c in by if c in set(cols)]   # as the step: the keys gone do not count
    if op_type == "filter":
        ops = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<=", "in": "in", "not_in": "not in",
               "between": "between", "contains": "contains", "starts_with": "starts with", "ends_with": "ends with",
               "is_null": "is null", "is_not_null": "is not null"}
        op = p.get("operator", "eq")
        value = "" if op in ("is_null", "is_not_null") else f" {_v(p.get('value'))}"
        return f"filter_{col}", f"keep rows where {col} {ops.get(op, op)}{value}"
    if op_type == "sort":
        keys = sort_keys(p, cols or None) if by else []
        return f"sort_by_{'_'.join(by[:2])}", "sort by " + ", ".join(
            f"{c} {'descending' if d else 'ascending'}" for c, d in keys) + " (nulls last)"
    if op_type == "group_by":
        aggs = ", ".join(f"{a.get('func', 'sum')}({a.get('column')}) as {a.get('alias') or a.get('column') + '_' + a.get('func', 'sum')}" for a in (p.get("aggregations") or []))
        return f"aggregate_by_{'_'.join(by[:2])}", f"aggregate by {', '.join(by)}: {aggs}"
    if op_type in ("join", "union"):
        right = p.get("right") or {}
        try:
            name = _resolve(resolve, right["source"] if "source" in right else right)[3]
        except Exception:  # noqa: BLE001
            name = "right"
        if op_type == "union":
            return f"union_{name}", f"append the rows of {name} ({'same columns, in order' if p.get('strategy') == 'strict' else 'matched by column name'})"
        how = p.get("how", "inner")
        key_names = ", ".join(_as_list(p["on"])) if p.get("on") else " and ".join(f"{a} = {b}" for a, b in zip(_as_list(p.get("left_on") or []), _as_list(p.get("right_on") or [])))
        return f"{how}_join_{name}", f"{how} join with {name}" + (f" on {key_names}" if key_names else "")
    simple = {
        "select": ("select_columns", lambda: f"keep the columns {', '.join(p.get('columns') or [])}"),
        "reorder": ("reorder_columns", lambda: f"put first {', '.join(p.get('columns') or [])}"),
        "drop": ("drop_columns", lambda: f"drop {', '.join(p.get('columns') or [])}"),
        "rename": ("rename_columns", lambda: "rename " + ", ".join(f"{a} to {b}" for a, b in (p.get("mapping") or {}).items())),
        "cast": ("cast_columns", lambda: "cast " + ", ".join(f"{a} to {b}" for a, b in (p.get("columns") or {}).items()) + " (a value that does not convert becomes NULL)"),
        "limit": (f"limit_{p.get('n')}", lambda: f"keep the first {p.get('n')} rows"),
        "unique": ("deduplicate", lambda: f"one row per {', '.join(p.get('subset'))}" if p.get("subset") else "drop duplicate rows"),
        "fill_null": ("fill_nulls", lambda: "replace NULL: " + ", ".join(f"{a} with {_v(b)}" for a, b in (p.get("columns") or {}).items())),
        "drop_nulls": ("drop_null_rows", lambda: f"drop rows with NULL in {', '.join(p.get('subset'))}" if p.get("subset") else "drop rows with any NULL"),
        "sql": ("custom_sql", lambda: "SQL written in the flow: " + str(p.get("query") or "")),
        "pivot": (f"pivot_{'_'.join(_as_list(p.get('on') or [])[:2])}", lambda: f"one column per value of {', '.join(_as_list(p.get('on') or []))}, {p.get('func', 'sum')}({p.get('values')}) by {', '.join(_as_list(p.get('index') or []))}"),
        "unpivot": (f"unpivot_{'_'.join((p.get('on') or [])[:2])}", lambda: f"turn {', '.join(p.get('on') or []) or 'the columns'} into rows ({p.get('variable_name') or 'variable'}, {p.get('value_name') or 'value'})"),
    }
    if op_type in simple:
        name, note = simple[op_type]
        return name, note()
    return op_type, op_type


# a node saved half done (a join without keys or without its right input, a filter
# without a column…): the engines refuse it too; the export says which step, with a code
_INCOMPLETE_STEP = (EngineError, KeyError, TypeError, ValueError, AttributeError, IndexError)


def _wrong_shape(op_type: str, p: dict) -> bool:
    """Whether a node's parameters lack the shape the editor writes (a mapping that is not one, an
    aggregation without a column, a limit that is not a number…): then an error compiling it is the
    node's. False: they have it, and the error is the exporter's own."""
    def list_of(v, *types_) -> bool:
        return isinstance(v, list) and all(isinstance(x, types_) and x != "" for x in v)

    def integer(v) -> bool:
        try:
            int(v)
            return not isinstance(v, bool)
        except (TypeError, ValueError):
            return False
    names = lambda v: isinstance(v, str) and v != "" or list_of(v, str) and bool(v)   # noqa: E731
    shapes: dict[str, Callable[[], bool]] = {
        "select": lambda: list_of(p.get("columns"), str), "reorder": lambda: list_of(p.get("columns"), str),
        "drop": lambda: list_of(p.get("columns"), str), "rename": lambda: isinstance(p.get("mapping"), dict),
        "cast": lambda: isinstance(p.get("columns"), dict), "fill_null": lambda: isinstance(p.get("columns"), dict),
        "filter": lambda: isinstance(p.get("column"), str), "sort": lambda: names(p.get("by")), "limit": lambda: integer(p.get("n")),
        "unique": lambda: not p.get("subset") or list_of(p.get("subset"), str),
        "drop_nulls": lambda: not p.get("subset") or list_of(p.get("subset"), str),
        "group_by": lambda: names(p.get("by")) and list_of(p.get("aggregations"), dict)
        and all(isinstance(a.get("column"), str) and a.get("column") for a in p["aggregations"]),
        "compute": lambda: list_of(p.get("columns"), dict), "sql": lambda: isinstance(p.get("query"), str),
        "join": lambda: isinstance(p.get("right"), dict), "union": lambda: isinstance(p.get("right"), dict),
        "pivot": lambda: names(p.get("index")) and names(p.get("on")) and isinstance(p.get("values"), str),
        "unpivot": lambda: not p.get("on") or list_of(p.get("on"), str),
    }
    shape = shapes.get(op_type)
    return shape is None or not shape()


def _compile_ops(model, ref, s, operations, resolve, right_namer):
    for op in operations:
        op_type = op.get("type") if isinstance(op, dict) else op.type
        params = (op.get("params") if isinstance(op, dict) else op.params) or {}
        try:
            model.labels.append(_label(op_type, params, resolve, s.cols))
            try:
                ref, after = _compile_op(model, ref, s, op_type, params, resolve, right_namer)
            finally:
                model.labels.pop()
        except DbtExportError:
            raise
        except _INCOMPLETE_STEP as e:
            if not isinstance(e, EngineError):
                logger.warning("dbt export: the step %s cannot be compiled", op_type, exc_info=True)
                if not _wrong_shape(str(op_type), params if isinstance(params, dict) else {}):
                    # a node of the right shape: the error is the exporter's, not the flow's
                    raise DbtExportError(f"{op_type}: the export cannot compile the step ({type(e).__name__}: {e})", "internal",
                                         detail=f"{op_type}: {type(e).__name__}: {e}"[:200]) from e
            raise DbtExportError(f"{op_type}: the step is incomplete or not valid ({e})",
                                 "step_invalid", step=str(op_type), detail=str(e)[:200]) from e
        _refuse_uppercase(model, after, str(op_type))   # the names the step gives, before DuckDB renames one
        _refuse_long(model, after, str(op_type))
        after = model.probe.after(model, ref, after, op_type)
        after.order = _order_after(op_type, params, s)
        s = after
    return ref, s


def _order_after(op_type: str, params: dict, before: _S) -> Optional[list[tuple[str, bool]]]:
    """The sort a first/last after this step must follow: set by a sort, kept by the steps
    that keep the rows' order and the keys' values, lost by every other."""
    p, order = params or {}, before.order
    if op_type == "sort":
        keys = sort_keys(p, before.cols or None) if p.get("by") else []
        return keys or order
    if not order:
        return None
    key_names = {k for k, _ in order}
    if op_type in ("filter", "limit", "drop_nulls", "reorder"):
        return order
    if op_type == "select":
        return order if key_names <= set(p.get("columns") or []) else None
    if op_type == "drop":
        return order if not key_names & set(p.get("columns") or []) else None
    if op_type == "rename":
        mapping = p.get("mapping") or {}
        return [(mapping.get(k, k), desc) for k, desc in order]
    if op_type in ("cast", "fill_null"):
        return order if not key_names & set(p.get("columns") or {}) else None
    if op_type == "compute":
        return order if not key_names & {str(c.get("name") or "").strip() for c in (p.get("columns") or [])} else None
    return None


# ── layout (spaces only, never the content) ──────────────────────────────────
_CLAUSE = re.compile(
    r"\s+(FROM|WHERE|GROUP BY|ORDER BY|QUALIFY|HAVING|LIMIT|UNION ALL BY NAME|UNION ALL|"
    r"(?:LEFT SEMI|LEFT ANTI|INNER|LEFT|RIGHT|FULL OUTER|CROSS|SEMI|ANTI) JOIN|USING SAMPLE)\b", re.IGNORECASE)


# the pieces of a DuckDB query that are not code: comments (`c`: -- … ends at a line break, as in
# DuckDB, /* … */) and the text whose bytes are data (`q`: '…' with '', E'…' with backslash
# escapes, $$…$$ and $tag$…$tag$, "…" names with ""). An unclosed one runs to the end.
_SQL_PIECES = re.compile(
    r"(?P<c>--[^\n\r]*|/\*.*?(?:\*/|\Z))"
    r"|(?P<q>(?<![\w$])[eE]'(?:[^'\\]|\\.|'')*(?:'|\Z)"
    r"|'(?:[^']|'')*(?:'|\Z)"
    r'|"(?:[^"]|"")*(?:"|\Z)'
    r"|(?<![\w$])\$\$.*?(?:\$\$|\Z)"
    r"|(?<![\w$])\$(?P<tag>[A-Za-z_]\w*)\$.*?(?:\$(?P=tag)\$|\Z))", re.DOTALL)


def _mask(sql: str) -> str:
    """The SQL with its strings, quoted identifiers, comments and nested parentheses masked:
    keywords are looked for at the top level only, outside quotes and comments."""
    masked = list(sql)
    for m in _SQL_PIECES.finditer(sql):
        masked[m.start():m.end()] = "_" * (m.end() - m.start())
    out, prof = [], 0
    for ch in masked:
        if ch == "(":
            prof += 1
            out.append("(" if prof == 1 else "_")
            continue
        if ch == ")":
            prof -= 1
            out.append(")" if prof == 0 else "_")
            continue
        out.append(ch if prof == 0 else "_")
    return "".join(out)


def _formatta(sql: str) -> str:
    """One clause per line and, for a long SELECT, one column per line. Touches ONLY the
    spaces outside strings and parentheses: the SQL stays the same."""
    m = _mask(sql)
    cuts = [x.start() for x in _CLAUSE.finditer(m)]
    if not cuts:
        return sql
    rows, before = [], 0
    for t in cuts:
        rows.append(sql[before:t].strip())
        before = t
    rows.append(sql[before:].strip())
    head = rows[0]
    if len(head) > 80 and re.match(r"(?i)^select\s+(distinct\s+)?", head):
        start_values = re.match(r"(?i)^select\s+(distinct\s+)?", head).end()
        mt = _mask(head)
        pieces, start = [], start_values
        for i, ch in enumerate(mt):
            if i >= start_values and ch == ",":
                pieces.append(head[start:i].strip())
                start = i + 1
        pieces.append(head[start:].strip())
        head = head[:start_values].strip() + "\n" + ",\n".join("    " + x for x in pieces)
    return "\n".join([head] + rows[1:])


def _indent(sql: str, margin: str = "    ") -> str:
    """Every line of a step indented by `margin`, never inside the text whose bytes are data:
    a string or a quoted name that spans lines keeps them (the indentation went into the value,
    'a⏎  b' became 'a⏎      b' and a filter on it kept no row). Outside of them a `\\r\\n` or a
    `\\r` is a line break like `\\n` (dbt writes them as `\\n` anyway)."""
    if not sql:
        return ""
    code = lambda t: re.sub(r"\r\n?", "\n", t).replace("\n", "\n" + margin)   # noqa: E731
    pieces, before = [], 0
    for m in _SQL_PIECES.finditer(sql):
        if m.group("q"):
            pieces += [code(sql[before:m.start()]), m.group(0)]
            before = m.end()
    text = margin + "".join(pieces) + code(sql[before:])
    # as str.splitlines did: no indented empty line after a final line break
    return text[:-len(margin) - 1] if text.endswith("\n" + margin) else text


def compile_model_sql(source: dict, operations: list, resolve: SourceResolver, native: bool = False,
                      dialect: str | None = None, info: dict | None = None, text: str | None = None,
                      used: set | None = None) -> tuple[str, list[str], dict]:
    """Compiles (source + operations) into a complete dbt model. Returns (the model's
    SQL, the output columns, their DuckDB types). `text`: the dialect the
    flow's compute expressions and sql nodes are written in (its engine's: polars,
    duckdb, clickhouse, bigquery; None = duckdb). `info`, if given, receives
    `segnaposti` (the texts written in the warehouse's dialect, which `put_back_texts`
    puts back in the translated SQL) and `da_rivedere` ([(step, dialect)]: text the team
    must review). `used`: the CTE names the project's other models already have (shared, and
    filled with this model's): no model repeats one (see `_Model._used`)."""
    model = _Model(_Namer("s"), native=native, dialect=dialect, text=text or "duckdb", used=used)
    right_namer = _Namer("r")
    src_ref, src_cols, src_types, name = _resolve(resolve, source)
    try:
        base, s = _base(model, src_ref, _S(src_cols, src_types), name, typed="model" not in source)
        final, out = _compile_ops(model, base, s, operations, resolve, right_namer)
    finally:
        model.probe.close()
    body = ",\n".join(
        f"{name} AS (\n" + (f"    -- {note}\n" if note else "") + f"{_indent(_formatta(sql))}\n)"
        for name, sql, note in model.ctes
    )
    finale = _column_list(out.cols) if (native and out.cols) else "*"
    if info is not None:
        info["segnaposti"] = dict(model.placeholders)
        info["per_sonda"] = dict(model.for_probe)
        info["da_rivedere"] = list(dict.fromkeys(model.to_review))
    return f"WITH\n{body}\nSELECT {finale}\nFROM {final}", out.cols, out.types


def cte_names(sql: str, dialect: str | None) -> set[str]:
    """The CTE names of a query the export does not write (a datasource query, an AI
    proposal): the models compiled after it must not repeat them (see `_Model._used`)."""
    import sqlglot
    from sqlglot import exp

    try:
        return {c.alias_or_name for a in sqlglot.parse(sql, read=dialect or "duckdb") if a is not None for c in a.find_all(exp.CTE)}
    except Exception:  # noqa: BLE001 — a query sqlglot does not read: no name to avoid
        return set()


def put_back_texts(sql: str, info: dict | None) -> str:
    """The translated SQL with the texts written in the warehouse's dialect in place of
    their placeholders (see `_Model.native_text`)."""
    for placeholder, text in ((info or {}).get("segnaposti") or {}).items():
        sql = re.sub(re.escape(placeholder[:-2]) + r"\s*\(\s*\)", lambda _m, t=text: t, sql, flags=re.IGNORECASE)
    return sql


def typed_query(sql: str, cols: list[str], dtypes: dict) -> str:
    """A query (a datasource from a query) with the types Tabularia gave it."""
    types_ = {c: source_type(dtypes.get(c)) for c in cols}
    if not cols or not any(types_.values()):
        return sql
    return f"SELECT {_typed_projection(None, cols, types_)} FROM ({sql}) AS _q"


# ─────────────────────────────────────────────────────────────────────────────
# singular tests from the data contract (DuckDB SQL; in native, translated as the models)
# ─────────────────────────────────────────────────────────────────────────────
TEST_IDENT = "__t"   # the model under test, replaced with {{ ref() }}


def _as_text(q: str, kind: Optional[str], dialect: Optional[str]) -> str:
    """A column as the text Tabularia's contract check reads it (Polars' cast to String):
    a date `2024-01-05`, an instant `2024-01-05 10:30:00.000000` (six digits, always), a
    boolean `true`, a decimal with all its scale digits (`1.50`), a float with `.0` when whole.
    Written in DuckDB SQL for `dialect`, the warehouse the test is translated for."""
    t = (kind or "").upper()
    fam = _family(kind)
    if fam == "text":
        return q
    if t.startswith("TIMESTAMP") or t == "DATETIME":
        if dialect == "clickhouse":
            return f"CAST({q} AS VARCHAR)"   # a DateTime64(6) prints its six digits
        # Postgres: to_char(…, 'YYYY-MM-DD HH24:MI:SS.US'); a bare CAST drops the zeros of the fraction
        return f"strftime({q}, '%Y-%m-%d %H:%M:%S.%f')"
    if t == "DATE" and dialect == "postgres":
        return f"strftime({q}, '%Y-%m-%d')"   # to_char: the same whatever the server's DateStyle
    if t in ("DOUBLE", "FLOAT", "REAL"):
        return _float_as_text(q, dialect)
    m = re.fullmatch(r"DECIMAL\(\s*\d+\s*,\s*(\d+)\s*\)", t)
    if m and dialect == "clickhouse":
        return f"toDecimalString({q}, {m.group(1)})"   # toString cuts the trailing zeros: 1.50 → '1.5'
    return f"CAST({q} AS VARCHAR)"


def _leading_flags(regex: str) -> tuple[str, str]:
    """`(?is)abc` → ('is', 'abc'): the inline flags a regex opens with, when they are ones
    Postgres reads the same way at the start of the whole pattern (i, s, x)."""
    m = re.match(r"\(\?([isx]+)\)", regex)
    return (m.group(1), regex[m.end():]) if m else ("", regex)


def _dot_without_line_break(regex: str) -> str:
    """Every `.` outside a bracket expression → `[^\\n]`: Postgres' `.` matches a newline,
    Tabularia's (Polars) does not. A `[^x]` keeps matching it in both, which `(?p)` would break."""
    out, i, n = [], 0, len(regex)
    while i < n:
        ch = regex[i]
        if ch == "\\":
            out.append(regex[i:i + 2])
            i += 2
            continue
        if ch == "[":
            j = i + 1
            if j < n and regex[j] == "^":
                j += 1
            if j < n and regex[j] == "]":
                j += 1   # a `]` first in the brackets is a character
            while j < n and regex[j] != "]":
                if regex[j] == "\\":
                    j += 2
                elif regex[j] == "[" and j + 1 < n and regex[j + 1] in ":.=":
                    deadline = regex.find(regex[j + 1] + "]", j + 2)
                    j = deadline + 2 if deadline != -1 else n
                else:
                    j += 1
            out.append(regex[i:j + 1])
            i = j + 1
            continue
        out.append("[^\\n]" if ch == "." else ch)
        i += 1
    return "".join(out)


def _whole_regex(regex: str, dialect: Optional[str]) -> str:
    """The regex of a pattern rule for the WHOLE value, as Tabularia matches it (`^(?:…)$`,
    with Polars' `.`, which does not match a newline): DuckDB's RE2 already reads it so;
    ClickHouse's `match` lets `.` match a newline unless told `(?-s)`; Postgres reads its
    flags only at the start of the pattern and has no `(?-s)` (see `_dot_without_line_break`)."""
    if dialect == "clickhouse":
        return f"(?-s)^(?:{regex})$"
    if dialect == "postgres":
        flag, rest = _leading_flags(regex)
        if "s" not in flag:
            rest = _dot_without_line_break(rest)
        return (f"(?{flag})" if flag else "") + f"^(?:{rest})$"
    return f"^(?:{regex})$"


# a boolean written in a contract, read as Tabularia's contract check reads it (app.contracts.evaluator)
_BOOLEANS = {True: True, False: False, "true": True, "false": False, "1": True, "0": False, 1: True, 0: False}


def _double(x: float) -> str:
    return f"CAST('{x}' AS DOUBLE)" if x != x or x in (float("inf"), float("-inf")) else f"CAST({x!r} AS DOUBLE)"


def _among_accepted(q: str, values: list, kind: Optional[str], dialect: Optional[str]) -> str:
    """`q` is one of the accepted `values`, compared as Tabularia's contract check compares them:
    on a number column as numbers («3» = 3 = 3.0, unless a value is not a number), on a boolean
    column as booleans, otherwise by the column's text. dbt's accepted_values wrote every value
    as '{{ value }}': an apostrophe broke the SQL, a 3.0 failed on an integer column."""
    if _family(kind) in ("integer", "number"):
        try:
            numbers = [float(v) for v in values if not isinstance(v, bool)]
        except (TypeError, ValueError):
            numbers = None   # a value that is not a number: compared by the text, as Tabularia does
        if numbers is not None:
            return f"CAST({q} AS DOUBLE) IN ({', '.join(_double(x) for x in numbers)})" if numbers else "FALSE"
    elif (kind or "").upper() == "BOOLEAN":
        try:
            booleans = [_BOOLEANS[v.strip().lower() if isinstance(v, str) else v] for v in values]
        except (KeyError, TypeError):
            # Tabularia's check cannot evaluate it either: the rule never passes there
            raise DbtExportError("accepted_values: a value of a boolean column is not a boolean", "contract_rule_invalid",
                                 rule="accepted_values")
        return f"{q} IN ({', '.join('TRUE' if b else 'FALSE' for b in dict.fromkeys(booleans))})"
    return f"{_as_text(q, kind, dialect)} IN ({', '.join(_lit(str(v)) for v in values)})"


def singular_test_sql(test: dict, types_: Optional[dict] = None, dialect: Optional[str] = None) -> str:
    """A contract rule that is not a generic dbt test → a query that SELECTS the
    violations (dbt fails it when it returns rows). `types_`: the
    model's column types; `dialect`: the warehouse the test is translated for."""
    q, t = _qi, f'"{TEST_IDENT}" AS "{TEST_IDENT}"'
    kind, col = test.get("kind"), test.get("column")
    types_ = types_ or {}
    if kind == "not_null":
        if not col:
            raise DbtExportError("not_null: a column is required", "contract_rule_invalid", rule="not_null")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NULL"
    if kind == "accepted_values":
        if not col or not isinstance(test.get("values"), list) or not test["values"]:
            raise DbtExportError("accepted_values: a column and a list of values are required", "contract_rule_invalid",
                                 rule="accepted_values")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL AND NOT ({_among_accepted(q(col), test['values'], types_.get(col), dialect)})"
    if kind == "range":
        conds = []
        if test.get("min") is not None:
            conds.append(f"{q(col)} < {_lit(test['min'])}")
        if test.get("max") is not None:
            conds.append(f"{q(col)} > {_lit(test['max'])}")
        if not col or not conds:
            raise DbtExportError("range: a column and at least one bound are required", "contract_rule_invalid", rule="range")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL AND ({' OR '.join(conds)})"
    if kind == "pattern":
        if not col or not test.get("regex"):
            raise DbtExportError("pattern: a column and a regex are required", "contract_rule_invalid", rule="pattern")
        # the whole value, read as text (a pattern on an integer column made the test ERROR)
        text = _as_text(q(col), types_.get(col), dialect)
        return (f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL "
                f"AND NOT regexp_matches({text}, {_lit(_whole_regex(str(test['regex']), dialect))})")
    if kind == "row_count":
        conds = []
        if test.get("min") is not None:
            conds.append(f"COUNT(*) < {int(test['min'])}")
        if test.get("max") is not None:
            conds.append(f"COUNT(*) > {int(test['max'])}")
        if not conds:
            raise DbtExportError("row_count: at least one bound is required", "contract_rule_invalid", rule="row_count")
        return f"SELECT COUNT(*) AS n FROM {t} HAVING {' OR '.join(conds)}"
    if kind in ("unique", "unique_combination"):
        # GROUP BY keeps the NULLs in one group: two NULLs are a duplicate, as in Tabularia's check
        # (dbt's generic `unique` drops them, decision 7)
        cols = [c for c in (test.get("columns") or ([col] if kind == "unique" else [])) if c]
        if len(cols) < (1 if kind == "unique" else 2):
            raise DbtExportError(f"{kind}: {'a column is' if kind == 'unique' else 'at least two columns are'} required",
                                 "contract_rule_invalid", rule="unique")
        items_list = ", ".join(q(c) for c in cols)
        return f"SELECT {items_list}, COUNT(*) AS n FROM {t} GROUP BY {items_list} HAVING COUNT(*) > 1"
    if kind == "expression":
        sql = str(test.get("sql") or "").strip()
        if not sql:
            raise DbtExportError("expression: the expression is empty", "contract_rule_invalid", rule="expression")
        # an «I don't know» outcome (null) is not a yes: the row violates — as the evaluator
        return f"SELECT * FROM {t} WHERE NOT COALESCE(({sql}), FALSE)"
    raise DbtExportError(f"rule '{kind}' has no dbt equivalent", "rule_no_equivalent", kind=kind)


def column_tests(columns: list[dict]) -> tuple[list[dict], list[dict]]:
    """The contract's column tests (on the schema.yml columns, as the gateway sends them) →
    the columns with the generic tests dbt runs as Tabularia's check does — not_null only — and
    the rules that become singular tests (`singular_test_sql`, with their `column`): unique,
    where two NULLs are a duplicate (decision 7), and accepted_values (see `_among_accepted`).
    A generic test gets the column name quoted by the adapter (`quote: true`, see `_schema_yaml`),
    which writes `"…"` without escaping a double quote in it: on such a column not_null is a
    singular test too. Two not_null rules on a column (a warning and an error) are ONE test
    with the stricter severity: dbt names both alike and refused the project at parse; the
    error fails exactly where the warning would warn."""
    cols_out, singular = [], []
    for c in columns:
        not_null: Optional[dict] = None
        for t in c.get("tests") or []:
            if t.get("kind") != "not_null" or '"' in str(c.get("name")):
                singular.append({**t, "column": c["name"]})
            elif not_null is None:
                not_null = t
            elif t.get("severity", "error") == "error":
                not_null = {**not_null, "severity": "error"}
        cols_out.append({**c, "tests": [not_null] if not_null else []})
    return cols_out, singular


# ─────────────────────────────────────────────────────────────────────────────
# dialect translation (native warehouse) with sqlglot
# ─────────────────────────────────────────────────────────────────────────────
# In native mode the source is a placeholder IDENTIFIER (not a dbt macro, which sqlglot
# could not parse). The model is compiled to DuckDB SQL, the stars are expanded with
# the schema (qualify) and the SQL is translated into the target dialect; then the
# placeholder is replaced with `{{ source() }}` / `{{ ref() }}` ONLY as the table
# reference (keeping the alias, so the column qualifiers stay valid).
_SQLGLOT_DIALECT = {
    "postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "clickhouse": "clickhouse",
}


def _without_unsigned(tree):
    """Postgres has no unsigned integer: a UBIGINT (Tabularia's UInt64) is a numeric(20,0)."""
    from sqlglot import exp

    for t in list(tree.find_all(exp.DataType)):
        if t.this == exp.DataType.Type.UBIGINT:
            t.replace(exp.DataType.build("DECIMAL(20, 0)"))
    return tree


def _tempi_clickhouse(tree):
    """DuckDB's dates and times as the ClickHouse engine types them: DATE is a Date32 and a
    TIMESTAMP a DateTime64(6). sqlglot writes Date (1970-2149: a birth date of 1955 wrapped to
    2134) and DateTime (whole seconds, 1970-2106). ClickHouse 24 has no time of day (its TIME
    is an Int64, a time became NULL): a time is its text, as ClickHouse reads a Postgres time."""
    from sqlglot import exp

    T = exp.DataType.Type
    scala = {T.TIMESTAMP_S: 0, T.TIMESTAMP_MS: 3, T.TIMESTAMP_NS: 9}
    for t in list(tree.find_all(exp.DataType)):
        if t.this == T.DATE:
            t.set("this", T.DATE32)
        elif t.this in (T.TIMESTAMP, T.TIMESTAMPNTZ, T.TIMESTAMPTZ, T.TIMESTAMPLTZ, T.DATETIME, *scala):
            t.set("expressions", [exp.DataTypeParam(this=exp.Literal.number(scala.get(t.this, 6)))])
            t.set("this", T.DATETIME64)
        elif t.this in (T.TIME, T.TIMETZ):
            t.set("this", T.TEXT)
    return tree


def _fix_for_dialect(tree, dialect: str):
    """Known gaps between the dialects that sqlglot does not cover by itself."""
    from sqlglot import exp

    if dialect == "postgres":
        # Postgres has round(numeric, int) but NOT round(double precision, int)
        for r in list(tree.find_all(exp.Round)):
            if r.args.get("decimals") is not None and not isinstance(r.this, exp.Cast):
                r.set("this", exp.Cast(this=r.this.copy(), to=exp.DataType.build("DECIMAL")))
        _without_unsigned(tree)
        # DuckDB's year(d), month(d)…, which Postgres does not have (sqlglot keeps them): EXTRACT,
        # an integer as in DuckDB (a second without its fraction)
        parts = {exp.Year: "YEAR", exp.Month: "MONTH", exp.Day: "DAY", exp.DayOfMonth: "DAY", exp.DayOfWeek: "DOW",
                 exp.DayOfWeekIso: "ISODOW", exp.DayOfYear: "DOY", exp.Quarter: "QUARTER", exp.Week: "WEEK",
                 exp.WeekOfYear: "WEEK", exp.Hour: "HOUR", exp.Minute: "MINUTE", exp.Second: "SECOND"}
        for node in reversed(list(tree.walk(bfs=False))):
            part = parts.get(type(node))
            if part and not (isinstance(node, exp.Week) and node.args.get("mode") is not None):
                excerpt = exp.Extract(this=exp.var(part), expression=node.this.copy())
                if part == "SECOND":
                    excerpt = exp.Floor(this=excerpt)
                node.replace(exp.Cast(this=excerpt, to=exp.DataType.build("BIGINT")))
    if dialect == "clickhouse":
        _tempi_clickhouse(tree)
        # DuckDB's upper/lower/substring/reverse/left/right/lpad work on characters, ClickHouse's
        # on bytes ('città' → 'CITTà'): their *UTF8 forms, as the ClickHouse engine uses
        utf8 = {exp.Upper: "upperUTF8", exp.Lower: "lowerUTF8", exp.Reverse: "reverseUTF8", exp.Substring: "substringUTF8",
                exp.Left: "leftUTF8", exp.Right: "rightUTF8"}
        for node in reversed(list(tree.walk(bfs=False))):   # the inner calls first: the outer copies them
            name = utf8.get(type(node))
            if isinstance(node, exp.Pad):
                name = "leftPadUTF8" if node.args.get("is_left") else "rightPadUTF8"
            if isinstance(node, exp.StrPosition) and not node.args.get("occurrence"):
                # strpos/instr/position count characters; ClickHouse's POSITION counts bytes
                arguments = [a for a in (node.this, node.args.get("substr"), node.args.get("position")) if a is not None]
                node.replace(exp.Anonymous(this="positionUTF8", expressions=[a.copy() for a in arguments]))
                continue
            if name:
                arguments = [a for a in (node.this, *(node.args.get(k) for k in ("start", "length", "expression", "fill_pattern"))) if a is not None]
                node.replace(exp.Anonymous(this=name, expressions=[a.copy() for a in arguments]))
    return tree


def transpile_model(sql: str, dialect: str, schema: dict[str, list[str]]) -> str:
    """Translates the model's DuckDB SQL into the target dialect, expanding the stars
    with the sources' schema ({ident: [columns]})."""
    import sqlglot
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.schema import MappingSchema

    sg_schema = {ident: {c: "VARCHAR" for c in cols} for ident, cols in schema.items()}
    try:
        # validate_qualify_columns=False: expands the stars best effort, without failing
        # when a reference cannot be resolved statically (self-joins, derived columns) —
        # running in the warehouse resolves them anyway.
        # The names must NOT be normalized with DuckDB's rules (which ignore case and
        # would turn «Odd Name» into «odd name», another column in the warehouse): the
        # schema as it is, and the target dialect's rules, where a quoted name stays.
        # expand_stars=False: the exporter writes the lists (see _lista); where a `*`
        # remains (after a sql node) it is left to the warehouse
        tree = qualify(
            sqlglot.parse_one(_standard_strings(sql), read="duckdb"),
            schema=MappingSchema(sg_schema, dialect=dialect, normalize=False), dialect=dialect,
            validate_qualify_columns=False, expand_stars=False,
        )
        return _fix_for_dialect(tree, dialect).sql(dialect=dialect, pretty=True)
    except DbtExportError:
        raise
    except Exception as e:
        raise DbtExportError(
            f"translation to the '{dialect}' dialect failed ({e}). "
            "The flow is too complex for the native export — use the «federated» export (dbt-duckdb).",
            "translation_failed", dialect=dialect, detail=str(e)[:200],
        )


def substitute_source_idents(sql: str, ident_to_macro: dict[str, str]) -> str:
    """Replaces `"ident" AS "ident"` → `{{ source(...) }} AS "ident"` (the table
    reference only; aliases and column qualifiers stay as they are)."""
    for ident, macro in ident_to_macro.items():
        pat = r'(["`])' + re.escape(ident) + r'\1\s+AS\s+(["`])' + re.escape(ident) + r"\2"
        # a function, not a template: the macro is written as it is (a `'\\'` in it stays two backslashes)
        sql = re.sub(pat, lambda m, macro=macro, ident=ident: f"{macro} AS {m.group(2)}{ident}{m.group(2)}", sql)
    return sql


# ─────────────────────────────────────────────────────────────────────────────
# dbt project generator
# ─────────────────────────────────────────────────────────────────────────────
# duckdb: DuckDB attaches the source database (postgres/mysql) and reads the tables
# LIVE: our DuckDB SQL runs as it is, no snapshot, no translation.
# clickhouse/trino have no official DuckDB scanner → they cannot be federated.
_DUCK_ATTACH_TYPE = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql"}
_FEDERABLE = set(_DUCK_ATTACH_TYPE)
# native: adapter dbt per db_type (dbt-postgres/dbt-clickhouse). Not MySQL/MariaDB (decision of
# 2026-10-16): the only dbt-mysql release needs dbt-core 1.7, which silently drops the project's
# tests; the federated and the ClickHouse targets read MySQL live
_NATIVE_ADAPTER = {"postgresql": "postgres", "clickhouse": "clickhouse"}


# the session of the project's DuckDB in UTC, as the engine's (`SET TimeZone = 'UTC'`): what the
# flow does with time zones (a cast to TIMESTAMPTZ, a date of an instant) does not depend on the
# machine that runs dbt
_DUCK_SETTINGS = "      settings:\n        TimeZone: UTC\n"


# ── target ClickHouse ──────────────────────────────────────────────────────────
# dbt-clickhouse on the customer's ClickHouse (usually the engine's). The Postgres/MySQL
# sources are read LIVE through one ClickHouse database per (connection, schema),
# `tab_src_<id>_<schema>`, with one table per source table (ENGINE = PostgreSQL /
# MySQL) whose types keep every value (see `_structure_sql`). The macro
# `create_tabularia_sources` creates them (`dbt run-operation`, password from env_var).
# From there on it is all ClickHouse: sources.yml points at those databases.
_CH_FEDERATED_ENGINE = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MySQL"}


def federated_db_name(conn_id: int, schema: str) -> str:
    """The ClickHouse database that shows a schema of a Postgres/MySQL connection: one per
    schema. A schema that is not a plain lowercase name gets a fingerprint of its exact name
    after the readable part: 'Sales' and 'sales', 'vendite_città' and 'vendite_citt', or two
    Cyrillic names were one database, read from the first of them."""
    schema = str(schema)
    readable = re.sub(r"[^a-z0-9]+", "_", schema.lower()).strip("_") or "default"
    if readable != schema:
        readable += "_" + hashlib.sha256(schema.encode()).hexdigest()[:10]
    return f"tab_src_{conn_id}_{readable}"


def _dates_as_text(tree) -> None:
    """In a comparison, a date or timestamp written as a typed literal (`DATE '1970-01-01'`,
    `'…'::timestamp`) becomes its text: ClickHouse 24 hands a typed date constant to a
    PostgreSQL/MySQL table as a number of days (`"nato" < 0`, which Postgres refuses) and a
    timestamp as seconds; the text it hands as it is, and compares with the column's own type."""
    from sqlglot import exp

    T = exp.DataType.Type
    for c in list(tree.find_all(exp.Cast)):
        comparison = isinstance(c.parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In))
        if not (comparison and isinstance(c.this, exp.Literal) and c.this.is_string):
            continue
        # an instant with its offset (`… 10:00+02`) stays typed: as text ClickHouse would read it in its own zone
        with_zone = re.search(r"\d:\d\d(:\d\d(\.\d+)?)?\s*(Z|[+-]\d\d(:?\d\d)?)$", c.this.this.strip())
        if c.to.this in (T.DATE, T.TIMESTAMP, T.TIMESTAMPNTZ, T.DATETIME) or (c.to.this in (T.TIMESTAMPTZ, T.TIMESTAMPLTZ) and not with_zone):
            others_ = [x for x in c.parent.iter_expressions() if x is not c]
            if any(not isinstance(x, (exp.Literal, exp.Cast)) for x in others_):   # never two literals compared as text
                c.replace(c.this.copy())


def _default_schema(conn: dict) -> str:
    return "public" if conn["db_type"] == "postgresql" else (conn.get("database") or "")


class TargetClickHouse:
    """The sources of an export to ClickHouse: which federated databases are needed,
    their names, and the datasources' queries translated into ClickHouse."""

    def __init__(self, connections: list[dict], engine_host: str | None):
        self.conns = {int(c["id"]): c for c in connections}
        self.engine_host = (engine_host or "").strip().lower()
        self.federated: dict[str, dict] = {}     # database name → how to create it

    def database_for(self, conn_id: int, schema: str, table: str | None = None) -> str:
        """The ClickHouse database that shows `schema` of a connection; `table`: a table
        the project reads there, which the macro creates in it."""
        conn = self.conns.get(int(conn_id))
        if conn is None:
            raise DbtExportError(f"connection {conn_id} is unknown to the export")
        if conn["db_type"] == "clickhouse":
            # a database of the ClickHouse dbt runs on: read as it is
            if (conn.get("host") or "").strip().lower() != self.engine_host:
                raise DbtExportError(
                    f"the connection «{conn.get('name')}» is ANOTHER ClickHouse ({conn.get('host')}): the ClickHouse target "
                    "reads the Postgres/MySQL sources and the tables of its own server. Use the «native» export on that ClickHouse.",
                    "other_clickhouse", connection=conn.get("name"), host=conn.get("host"),
                )
            return schema or conn.get("database") or "default"
        engine_name = _CH_FEDERATED_ENGINE.get(conn["db_type"])
        if engine_name is None:
            raise DbtExportError(
                f"the connection «{conn.get('name')}» is {conn['db_type']}: ClickHouse can read it only if it is "
                "PostgreSQL, MySQL or MariaDB. Use another target for this flow.",
                "clickhouse_cannot_read", connection=conn.get("name"), db_type=conn["db_type"],
            )
        name = federated_db_name(conn["id"], schema)
        f = self.federated.setdefault(name, {"name": name, "engine": engine_name, "conn": conn, "schema": schema, "tables": set()})
        if f["schema"] != schema:   # two schemas, one database: never read one in place of the other
            raise DbtExportError(f"the schemas «{f['schema']}» and «{schema}» would share the ClickHouse database {name}")
        if table:
            f["tables"].add(table)
        return name

    def sources(self, sources: list[dict]) -> list[dict]:
        """sources.yml: every source's schema becomes the ClickHouse database that shows it."""
        out = []
        for src in sources:
            if src.get("declared"):
                out.append(src)   # in their project: they declare it, no federated database to create
                continue
            for t in src.get("tables") or []:
                self.database_for(src["connection_id"], src["schema"], t["name"])
            out.append({**src, "database": None, "schema": self.database_for(src["connection_id"], src["schema"])})
        return out

    def translate_query(self, sql: str, conn_id: int, label: str, proposal: str | None = None,
                      columns: list[str] | None = None) -> str:
        """A datasource's query, written for Postgres/MySQL, in ClickHouse SQL: its tables
        become those of the federated databases. With a `proposal` (the AI's, already
        ClickHouse SQL with the source's TABLE NAMES) that one is used, after the
        checks: this code points the tables anyway."""
        import sqlglot
        from sqlglot import exp

        conn = self.conns.get(int(conn_id))
        if conn is None:
            raise DbtExportError(f"connection {conn_id} is unknown to the export")
        if conn["db_type"] == "clickhouse":
            self.database_for(conn_id, conn.get("database") or "")   # the same server, or a refusal
            return sql
        dialect = _SQLGLOT_DIALECT.get(conn["db_type"])
        if proposal is not None:
            tree = check_proposal(proposal, "clickhouse", None, columns, label)
            trees = [tree]
            dialect = "clickhouse"
        try:
            trees = trees if proposal is not None else sqlglot.parse(sql, read=dialect)
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"the query of the datasource «{label}» cannot be translated to ClickHouse ({str(e)[:200]}): use the federated target",
                                 "query_not_translatable", datasource=label, detail=str(e)[:200])
        if len(trees) != 1 or trees[0] is None:
            raise DbtExportError(f"the query of the datasource «{label}» is not a single SELECT: it cannot be translated to ClickHouse",
                                 "query_not_single_select", datasource=label)
        tree = trees[0]
        if proposal is None and dialect == "postgres":
            # Postgres folds a name written without quotes to lowercase (`Prodotti` is the table
            # prodotti), ClickHouse does not: the names as Postgres reads them, quoted ones as they are
            from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

            tree = normalize_identifiers(tree, dialect="postgres")
        for t in list(tree.find_all(exp.Table)):
            # a CTE only where it is visible: any other name is a table of the connection
            if not t.name or reads_a_cte(t):
                continue
            t.set("db", exp.to_identifier(self.database_for(conn_id, t.db or _default_schema(conn), t.name)))
            t.set("catalog", None)
        _dates_as_text(tree)
        _fix_for_dialect(tree, "clickhouse")   # Date32/DateTime64, and the *UTF8 functions, as in the models
        try:
            return tree.sql(dialect="clickhouse", pretty=True)
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"the query of the datasource «{label}» cannot be translated to ClickHouse ({str(e)[:200]}): use the federated target",
                                 "query_not_translatable", datasource=label, detail=str(e)[:200])


# ClickHouse reads at most max_query_size bytes of a query (256 KiB by default): the profiles raise
# it to this, the syntax check reads the models with it, and a model bigger than it, with the
# ephemeral models dbt inlines into it, is refused (see `too_large`). Far below it the planning
# already takes long: about 100 s for 150 columns × 40 steps on ClickHouse 24.8.
MAX_QUERY_SIZE = 16 * 1024 * 1024
_WRAPPER = 8 * 1024   # what dbt-clickhouse writes around a model (CREATE TABLE … AS, SETTINGS)
CHECK_WAIT = 10   # seconds the syntax check waits for the engine's ClickHouse (EXPLAIN AST only parses)


def too_large(model: str, size: int) -> DbtExportError:
    """The refusal of a model ClickHouse would not read: not a «not translatable» (that asks the
    AI for a translation, which can only be as big)."""
    return DbtExportError(
        f"the model «{model}» is {size:,} bytes of SQL together with the ephemeral models dbt puts inside it: ClickHouse reads "
        f"at most {MAX_QUERY_SIZE:,} (max_query_size). Split the flow in two: a flow that reads another flow's output exports it as a model of its own.",
        "model_too_large", model=model, size=f"{size:,}", max=f"{MAX_QUERY_SIZE:,}")


def over_the_limit(size: int) -> bool:
    return size + _WRAPPER > MAX_QUERY_SIZE


def in_byte(sql: str) -> int:
    """The size ClickHouse's max_query_size measures: bytes, not characters (an accented letter is two)."""
    return len(sql.encode("utf-8", "surrogatepass"))


class ClickHouseSyntaxCheck:
    """A model's ClickHouse SQL read by a REAL ClickHouse parser: `EXPLAIN AST`, which
    runs nothing and does not need the tables to exist. sqlglot almost never fails: it
    silently translates constructs ClickHouse lacks (SIMILAR TO, LATERAL, TABLESAMPLE…),
    and the error came out only at the data team's `dbt run` (the user's choice,
    2026-10-10: check it at the export). An error here is a «not translated»: with the
    dialog, the AI's proposal; without it, a refusal that says why.
    Parser: chDB when present (development, local), otherwise the engine's ClickHouse;
    when none answers the check is skipped, with a log — it does not block an export.
    It does not see functions that do not exist: those would need the analysis.
    A ClickHouse that accepts the connection and never answers waits CHECK_WAIT seconds,
    once: then the check is off for the rest of the export (`skipped`), and the README says so."""

    def __init__(self):
        self._via: Any = None   # "chdb", a clickhouse-connect client, or False (no parser)
        self.skipped = False    # a parser was configured and did not answer: the SQL was not read by it

    def close(self) -> None:
        if self._via not in (None, False, "chdb"):
            try:
                self._via.close()
            except Exception:  # noqa: BLE001
                pass
            self._via = False

    def _open(self):
        if self._via is not None:
            return self._via
        try:
            import chdb  # noqa: F401

            self._via = "chdb"
            return self._via
        except ImportError:
            pass
        try:
            import clickhouse_connect

            from app.core.config import get_settings

            cfg = get_settings().clickhouse_external
            if not cfg.host:
                self._via = False
                return self._via
            usa_ai = bool(cfg.ai_username and cfg.ai_password.get_secret_value())   # the read-only user, when there is one
            # a read timeout of its own (the client's default is 300 s), no session: one request each
            self._via = clickhouse_connect.get_client(
                host=cfg.host, port=cfg.port, secure=cfg.secure, connect_timeout=min(cfg.connect_timeout, CHECK_WAIT),
                send_receive_timeout=CHECK_WAIT, autogenerate_session_id=False,
                username=cfg.ai_username if usa_ai else cfg.username,
                password=(cfg.ai_password if usa_ai else cfg.password).get_secret_value(),
            )
        except Exception as e:  # noqa: BLE001 — server down, credentials: the export goes on without the check
            logger.warning("dbt export: the ClickHouse syntax check is not available (%s)", str(e)[:200])
            self._via, self.skipped = False, True
        return self._via

    def error(self, sql: str) -> str | None:
        """None when ClickHouse reads the SQL; otherwise the first line of its error."""
        via = self._open()
        if not via:
            return None
        try:
            # the limit of the profiles: ClickHouse's default 256 KiB refused models dbt runs
            if via == "chdb":
                import chdb

                chdb.query(f"SET max_query_size = {MAX_QUERY_SIZE}; EXPLAIN AST {sql}", "TSV")
            else:
                via.query(f"EXPLAIN AST {sql}", settings={"max_query_size": MAX_QUERY_SIZE})
            return None
        except Exception as e:  # noqa: BLE001
            text = str(e)
            if "Syntax error" not in text and "SYNTAX_ERROR" not in text:
                # no answer (a timeout, a dropped connection): off for the rest of the export, not one wait per model
                logger.warning("dbt export: the ClickHouse syntax check did not answer (%s)", text[:200])
                self.close()
                self._via, self.skipped = False, True
                return None
            line = next((r for r in text.splitlines() if "Syntax error" in r), text.splitlines()[0])
            line = re.sub(r"^.*?DB::Exception:\s*", "", line)
            return re.sub(r"\.\s*Expected one of:.*$", "", line).strip()[:200]


def reads_a_cte(t) -> bool:
    """True if a table reference names a CTE that is VISIBLE where it stands: never a
    qualified name (`system.users` is read for real even next to a CTE `users`), only the
    CTEs of an enclosing query, and inside a CTE's own body only the CTEs before it (itself
    only if RECURSIVE) — elsewhere the same name is a real table of the database."""
    from sqlglot import exp

    if t.args.get("db") or t.args.get("catalog"):
        return False
    child, n = t, t.parent
    while n is not None:
        visible: list = []
        if isinstance(n, exp.With):
            ctes = list(n.expressions)
            i = next((k for k, c in enumerate(ctes) if c is child), None)
            if i is not None:
                visible = ctes[:i] + ([ctes[i]] if n.args.get("recursive") else [])
        else:
            # the query's WITH (its arg is `with` or `with_` depending on the sqlglot version)
            w = next((v for v in n.args.values() if isinstance(v, exp.With)), None)
            if w is not None and w is not child:
                visible = list(w.expressions)
        if any(c.alias_or_name == t.name for c in visible):
            return True
        child, n = n, n.parent
    return False


# functions that read data or settings outside the model, or change state: a translation
# never needs them (ClickHouse: dictionaries, joins, files, URLs, other servers, settings;
# Postgres: dblink, server files, large objects, settings, sequences, XML of whole tables)
_FORBIDDEN_FUNCTIONS = re.compile(
    r"(?i)(dict(get|has|isin)\w*|joinget\w*|file|url|urlcluster|s3|s3cluster|hdfs\w*|azureblobstorage\w*|remote\w*|cluster\w*|"
    r"mysql|postgresql|mongodb|redis|jdbc|odbc|getsetting\w*|getmacro|getserverport|input|"
    r"dblink\w*|pg_\w+|lo_\w+|set_config|current_setting|nextval|setval|currval|lastval|"
    r"(query|table|cursor|schema|database)_to_xml\w*)"
)


def check_proposal(sql: str, dialect: str, allowed: set[str] | None, columns: list[str] | None, model: str):
    """The checks on a translation the AI proposes — this code makes them, not the AI:
    one SELECT in the target dialect, no statements that write, no table function, no
    function that reads outside the model (dictionaries, files, settings…), no
    SETTINGS, only the tables the model reads (with `allowed`: its placeholders,
    never qualified, and the visible CTEs), the same output columns in the same order
    (with `columns`). Returns the sqlglot tree."""
    import sqlglot
    from sqlglot import exp

    def no(reason: str, text: str, **details):
        # the reason is a CODE (the gateway says it in the reader's language), the text stays in the logs
        raise DbtExportError(f"the AI's proposal for «{model}» does not pass the checks: {text}",
                             "ai_translation_invalid", model=model, reason=reason, **details)

    try:
        trees = [a for a in sqlglot.parse(sql or "", read=dialect) if a is not None]
    except Exception as e:  # noqa: BLE001
        no("unparsable", f"it cannot be parsed ({str(e)[:120]})")
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        no("not_select", "it is not a single SELECT")
    tree = trees[0]
    if any(isinstance(n, (exp.Insert, exp.Create, exp.Drop, exp.Delete, exp.Update, exp.Alter, exp.Command, exp.Merge)) for n in tree.walk()):
        no("writes", "it contains statements that are not a SELECT")
    if any(q.args.get("settings") for q in tree.find_all(exp.Query)):
        no("settings", "it changes the server's settings (SETTINGS)")
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            no("table_function", "it reads a table function")
    for f in tree.find_all(exp.Func):
        name = f.name if isinstance(f, exp.Anonymous) else f.sql_name()
        if name and _FORBIDDEN_FUNCTIONS.fullmatch(str(name)):
            no("function_not_allowed", f"it calls {name}, which reads data or settings outside the model", function=str(name))
    for t in tree.find_all(exp.Table):
        if allowed is None or reads_a_cte(t):
            continue
        if t.args.get("db") or t.args.get("catalog") or t.name not in allowed:
            name = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            no("unknown_table", f"it reads a table the model does not read ({name})", table=name)
    for i in tree.find_all(exp.In):
        if i.args.get("field") is not None:
            # ClickHouse `x IN db.table` reads that table, and it is not a FROM
            name = i.args["field"].sql(dialect=dialect)
            no("unknown_table", f"it reads a table the model does not read ({name})", table=name)
    if columns is not None:
        if any(isinstance(x, exp.Star) or (isinstance(x, exp.Column) and isinstance(x.this, exp.Star)) for x in tree.selects):
            no("star", "it uses * instead of listing the columns")
        output = [x.alias_or_name for x in tree.selects]
        if output != list(columns):
            no("columns", f"it returns the columns {output} instead of {list(columns)}", got=", ".join(output), expected=", ".join(columns))
    return tree


def use_proposal(sql: str, dialect: str, ident_to_macro: dict[str, str], columns: list[str], model: str,
                 check: "ControlloSintassiClickHouse | None" = None) -> str:
    """A translation proposed for a compiled model: checked, then with the sources'
    placeholders (__s_0, __m_1…) in place of the dbt macros. The placeholders are
    renamed on the tree, not in the text: the AI may write them with or without alias."""
    from sqlglot import exp

    tree = check_proposal(sql, dialect, set(ident_to_macro), columns, model)
    for t in list(tree.find_all(exp.Table)):
        if t.name in ident_to_macro:
            alias = t.alias or t.name
            t.set("this", exp.to_identifier(f"__TABULARIA_{t.name}__"))
            t.set("alias", exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
    text = tree.sql(dialect=dialect, pretty=True)
    if check and over_the_limit(in_byte(text)):
        raise too_large(model, in_byte(text))
    error = check.error(text) if check else None   # before the macros: ClickHouse does not read Jinja
    if error:
        raise DbtExportError(f"the AI's proposal for «{model}» does not pass the checks: ClickHouse cannot parse it ({error})",
                             "ai_translation_invalid", model=model, reason="syntax", detail=error)
    text = neutralize_jinja(text)   # Jinja in the proposal is text, never run by dbt; then our macros
    for ident, macro in ident_to_macro.items():
        text = re.sub(r'[`"]?__TABULARIA_' + re.escape(ident) + r'__[`"]?', lambda _m, macro=macro: macro, text)
    return text


def _ch_literal(v: Any) -> str:
    """A ClickHouse string literal written in a Jinja file: the Jinja delimiters stay
    text (neutralized, not refused)."""
    t = str(v if v is not None else "")
    return neutralize_jinja("'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'")


def _ident_ch(name: str) -> str:
    """A ClickHouse identifier between backticks, written in a Jinja file."""
    return neutralize_jinja("`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`")


def _structure_sql(database: str, table: str, engine_name: str) -> str:
    """The columns of a source table as ClickHouse inferred them (the table made `AS
    postgresql(…)`/`mysql(…)`), each `` `name` type ``, with the types that would lose
    values replaced: a Date (1970-2149) is a Date32 and a DateTime (whole seconds,
    1970-2106) a DateTime64(6), also inside an Array or a Tuple; a Postgres numeric without
    precision, which ClickHouse makes a Decimal(38, 19) (a value over 10^19 failed every
    model), is Tabularia's double. A MySQL decimal stays exact, as Tabularia reads it."""
    t = "concat('(', type, ')')"
    if engine_name == "PostgreSQL":
        t = f"replaceAll({t}, 'Decimal(38, 19)', 'Float64')"
    for old, new in (("Date)", "Date32)"), ("Date,", "Date32,"), ("DateTime)", "DateTime64(6))"), ("DateTime,", "DateTime64(6),")):
        t = f"replaceAll({t}, '{old}', '{new}')"
    return (r"SELECT concat('`', replaceAll(replaceAll(name, '\\', '\\\\'), '`', '\\`'), '` ', substring(t, 2, length(t) - 2)) "
            f"FROM (SELECT name, position, {t} AS t FROM system.columns WHERE database = {_ch_literal(database)} "
            f"AND table = {_ch_literal(table)}) ORDER BY position")


def _clickhouse_sources_macro(federated: list[dict]) -> str:
    """The macro that creates the databases federated to the sources: for every table the
    project reads, a ClickHouse table with the PostgreSQL / MySQL engine. Its types are the
    ones ClickHouse infers (the table made `AS postgresql(…)` first) where they keep every
    value, and the wider ones where they do not (see `_structure_sql`): a database engine
    infers them on every read, and a date of 1955 became 2134 before any cast of the model."""
    rows = [
        "{% macro create_tabularia_sources() %}",
        "{#- Tabularia: one ClickHouse database per source (connection, schema), with one table per",
        "    source table (PostgreSQL / MySQL engine): ClickHouse reads the source tables live from there.",
        "    Run it before the first `dbt run`, and again when a source table changes its columns:",
        "      dbt run-operation create_tabularia_sources --log-level-file none",
        "    The source passwords come from env vars and are part of the SQL this runs:",
        "    `--log-level-file none` keeps that SQL out of logs/dbt.log. -#}",
    ]
    for f in federated:
        c = f["conn"]
        pw = f"pw_{_slug(f['name'])}"
        rows.append("{%- set " + pw + " = env_var('" + c["pw_env"] + "') | replace('\\\\', '\\\\\\\\') | replace(\"'\", \"\\\\'\") %}")
        address = f"{c['host']}:{c.get('port') or (5432 if f['engine'] == 'PostgreSQL' else 3306)}"
        db = f["name"]
        rows += [
            # a database an earlier export created with the PostgreSQL/MySQL engine: it holds no data, it is replaced
            "{%- if run_query(\"SELECT count() FROM system.databases WHERE name = '" + db + "' AND engine IN ('PostgreSQL', 'MySQL')\")"
            ".columns[0].values()[0] > 0 %}",
            "{% call statement('" + db + "') -%}",
            f"DROP DATABASE `{db}`",
            "{%- endcall %}",
            "{%- endif %}",
            "{% call statement('" + db + "') -%}",
            f"CREATE DATABASE IF NOT EXISTS `{db}`",
            "{%- endcall %}",
        ]
        for table in sorted(f.get("tables") or ()):
            if f["engine"] == "PostgreSQL":
                arguments = [_ch_literal(address), _ch_literal(c.get("database")), _ch_literal(table), _ch_literal(c.get("username")),
                             "'{{ " + pw + " }}'", _ch_literal(f["schema"])]
            else:   # MySQL: the schema is the database
                arguments = [_ch_literal(address), _ch_literal(f["schema"]), _ch_literal(table), _ch_literal(c.get("username")),
                             "'{{ " + pw + " }}'"]
            name = f"`{db}`.{_ident_ch(table)}"
            rows += [
                "{% call statement('" + db + "') -%}",
                f"CREATE OR REPLACE TABLE {name} AS {f['engine'].lower()}({', '.join(arguments)})",
                "{%- endcall %}",
                "{%- set structure %}" + _structure_sql(db, table, f["engine"]) + "{% endset %}",
                "{%- set columns = run_query(structure).columns[0].values() %}",
                "{% call statement('" + db + "') -%}",
                f"CREATE OR REPLACE TABLE {name} ({{{{ columns | join(', ') }}}}) ENGINE = {f['engine']}({', '.join(arguments)})",
                "{%- endcall %}",
            ]
        rows.append("{{ log('Tabularia source ready: " + db + "', info=True) }}")
    rows.append("{% endmacro %}")
    return "\n".join(rows) + "\n"


def _clickhouse_settings() -> list[str]:
    """The settings every dbt-clickhouse profile of the export sends with each query. Without
    join_use_nulls the missing side of an outer join is 0 / '' instead of NULL (the engine does
    not see it: it reads parquet, where everything is Nullable). max_query_size: every step lists
    and qualifies every column, so a wide, long flow passes ClickHouse's default 256 KiB."""
    return ["        join_use_nulls: 1", f"        max_query_size: {MAX_QUERY_SIZE}"]


def _secure_line(default: str) -> str:
    """`secure:` of a dbt-clickhouse profile: `default` ('true'/'false') unless CLICKHOUSE_SECURE
    says otherwise. as_bool wants Python's True/False: what a person writes (true, 1, yes)
    is taken too."""
    return "      secure: \"{{ (env_var('CLICKHOUSE_SECURE', '" + default + "') | lower in ['true', '1', 'yes']) | as_bool }}\""


def tls_for_port(port: Any) -> bool:
    """Whether Tabularia reaches a ClickHouse connection over TLS: its client (clickhouse-connect,
    given no scheme) uses https on the ports 8443 and 443, plain http on the others."""
    return str(port) in ("8443", "443")


def _clickhouse_profile(project: str, ch: dict) -> str:
    """profiles.yml of the ClickHouse target: the engine's ClickHouse host and port as
    starting values, dbt's user and password ONLY from env_var. `join_use_nulls`: without
    it the missing side of an outer join is 0 / '' instead of NULL (the engine does not
    see it, as it reads parquet, where everything is Nullable)."""
    host = ch.get("host") or ""
    port = ch.get("port") or (8443 if ch.get("secure") else 8123)
    safe = "true" if ch.get("secure") else "false"
    return "\n".join([
        f"{_y_name(project, key=True)}:", "  target: dev", "  outputs:", "    dev:", "      type: clickhouse",
        # YAML first (a double-quoted scalar), then Jinja: a quote or a backslash in the host survives both
        "      host: " + _y_key("{{ env_var('CLICKHOUSE_HOST'" + (f", {_jinja_str(host)}" if host else "") + ") }}"),
        "      port: \"{{ env_var('CLICKHOUSE_PORT', '" + str(port) + "') | as_number }}\"",
        _secure_line(safe),
        "      user: \"{{ env_var('CLICKHOUSE_USER') }}\"",
        "      password: \"{{ env_var('CLICKHOUSE_PASSWORD') }}\"",
        f"      schema: {_y_name(ch.get('target_schema') or 'dbt_tabularia')}",
        "      custom_settings:", *_clickhouse_settings(),
    ]) + "\n"


def _secret_name(a: dict) -> str:
    return f"tabularia_{_slug(a['alias'])}"


def _duck_secret(a: dict) -> str:
    """The DuckDB secret of a source database (dbt-duckdb `secrets:`), password via
    env_var. NOT a DSN in the ATTACH path: DuckDB prints that path on every failed
    connection (wrong password, source down) and dbt writes it to the console and to
    logs/dbt.log; an unquoted DSN also broke on a password with a space or a backslash.
    With a secret the path is empty and the error says `at ""` (proved with real dbt)."""
    db_type = a["db_type"]
    if db_type not in _DUCK_ATTACH_TYPE:
        raise DbtExportError(f"DuckDB federation is not available for '{db_type}'", "duckdb_federation_unavailable", db_type=db_type)
    c = a["conn"]
    port = c.get("port")
    return "\n".join([
        f"        - type: {_DUCK_ATTACH_TYPE[db_type]}",
        f"          name: {_secret_name(a)}",
        f"          host: {_y(c['host'])}",
        f"          port: {port if isinstance(port, int) else _y(port)}",
        f"          database: {_y(c['database'])}",
        f"          user: {_y(c['username'])}",
        "          password: \"{{ env_var('%s') }}\"" % a["pw_env"],
    ])


def _duck_attach(a: dict) -> str:
    """The ATTACH of a source database: empty path, everything from its secret."""
    return "\n".join([
        "        - path: \"\"",
        f"          type: {_DUCK_ATTACH_TYPE[a['db_type']]}",
        f"          alias: {a['alias']}",
        f"          secret: {_secret_name(a)}",
    ])


# The characters a dbt adapter cannot have in a name, by where the name is (dbt 1.12.5): it writes
# names unescaped into its own SQL, between `"` (dbt-postgres, dbt-duckdb) or backticks (dbt-clickhouse,
# where a backslash starts an escape) around a relation, and between `'` in the queries that list a
# schema's relations and a table's columns. For a model's own table and schema a quote broke the first
# `dbt run` on ClickHouse, an incremental's second run and `dbt docs generate` on Postgres; for a
# source's schema or table (read through source(), so `dbt build` passed) it broke `dbt docs generate`
# on all three adapters, whose catalog query writes them between '…' («upper('o'tab')»).
# dbt-postgres also compares a model's schema with ILIKE, where a backslash escapes: the second
# `dbt run` did not find the table and failed with «already exists».
# The gateway refuses them before the download, with the same code; this is the engine's own check.
_NOT_QUOTABLE = {
    "dbt-postgres": {"source": "\"'", "relation": "\"'", "schema": "\"'\\"},
    "dbt-duckdb": {"source": "\"'", "relation": '"', "schema": '"'},
    "dbt-clickhouse": {"source": "`\\'", "relation": "`\\'", "schema": "`\\'"},
}


def quotable_name(name: Optional[str], adapter: str, where: str = "relation") -> None:
    """Refuses a name `adapter` cannot write (see `_NOT_QUOTABLE`): `where` is source (a source's
    schema or table), relation (a model's table) or schema (a model's or the project's schema)."""
    for char in _NOT_QUOTABLE[adapter][where]:
        if name and char in name:
            raise DbtExportError(f"the name «{name}» has the character {char}, which {adapter} cannot quote in a relation name",
                                 "name_not_quotable", name=name, character=char, adapter=adapter)


def native_adapter(db_type: Optional[str]) -> str:
    """The dbt adapter of the native target on a connection of this type, or the refusal."""
    adapter = _NATIVE_ADAPTER.get(str(db_type or ""))
    if adapter is None:
        raise DbtExportError(f"no native dbt adapter for '{db_type}': use the federated or the ClickHouse target",
                             "no_native_adapter", db_type=db_type)
    return adapter


def _native_profile(project: str, native: dict) -> str:
    """profiles.yml for the NATIVE warehouse. dbt runs in the source database; the
    transformations are translated into its dialect already (sqlglot). Password by env_var."""
    db_type = native["db_type"]
    adapter = native_adapter(db_type)
    c = native["conn"]
    pw = "{{ env_var('%s') }}" % native["pw_env"]
    schema = native.get("target_schema") or "dbt_tabularia"
    lines = [f"{_y_name(project, key=True)}:", "  target: dev", "  outputs:", "    dev:", f"      type: {adapter}"]
    # connection values are text the admin typed: quoted (YAML) and neutralised (dbt renders the profile)
    if adapter == "postgres":
        lines += [f"      host: {_y(c['host'])}", f"      port: {c['port']}", f"      user: {_y(c['username'])}",
                  f"      password: \"{pw}\"", f"      dbname: {_y(c['database'])}", f"      schema: {_y_name(schema)}"]
    elif adapter == "clickhouse":
        lines += [f"      host: {_y(c['host'])}", f"      port: {c['port']}", f"      user: {_y(c['username'])}",
                  # TLS where Tabularia uses it (dbt-clickhouse does not infer it from the port: `secure: false`
                  # spoke plain HTTP to a TLS port 8443)
                  f"      password: \"{pw}\"", f"      schema: {_y_name(schema)}", _secure_line(str(tls_for_port(c["port"])).lower()),
                  # as the ClickHouse target: the missing side of an outer join is NULL, not 0 / ''
                  "      custom_settings:", *_clickhouse_settings()]
    return "\n".join(lines) + "\n"


def _y(s: Any) -> str:
    """A YAML string in double quotes (JSON escapes are valid YAML), for a VALUE: dbt
    renders it as Jinja, so the user's text is neutralized.
    Jinja also drops a template's final line break (keep_trailing_newline is off: a column
    «Total⏎» was «Total» in schema.yml and its not_null test failed), so a final `\\n` is
    printed by an expression, `{{ '\\n' }}`, whose output is never trimmed."""
    t = neutralize_jinja(s)
    return _y_key(t[:-1] + "{{ '\\n' }}" if t.endswith("\n") else t)


# what JSON leaves raw but YAML refuses in a file (DEL, C1 controls, U+FFFE/FFFF, lone
# surrogates) or folds into a space inside quotes (NEL, LS, PS): written as \u escapes,
# which YAML reads back as the same character
_YAML_TO_ESCAPE = re.compile("[\x7f-\x9f\u2028\u2029\ud800-\udfff\ufffe\uffff]")


def _y_key(s: Any) -> str:
    """A double-quoted YAML string for a mapping KEY: dbt renders values, not keys, so
    the text is written as it is (a seed's column named `a{{b` stays `a{{b`). A character
    YAML does not allow raw (U+0096 from cp1252 text read as latin-1 made `dbt parse`
    fail) is escaped, not dropped: a column name or an accepted value stays exact."""
    return _YAML_TO_ESCAPE.sub(lambda m: "\\u%04x" % ord(m.group(0)), json.dumps(str(s), ensure_ascii=False))


# YAML 1.1 (PyYAML, so dbt) reads these plain scalars as booleans or null, in any case
_YAML_PAROLE = frozenset("y n yes no on off true false null".split())


def _y_name(s: Any, key: bool = False) -> str:
    """A name (model, source, schema, table, project) in YAML: as it is when YAML can only
    read it as that string (an identifier that is not `on`, `yes`, `null`…), otherwise
    quoted — a flow «On» became the profile `True`, an output «Yes» a model named True."""
    t = str(s)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t) and t.lower() not in _YAML_PAROLE:
        return t
    return _y_key(t) if key else _y(t)


def _project_name(flow_name: str) -> str:
    """The dbt project (and profile) name: the flow's slug, prefixed when it is the name
    of a package dbt installs itself (`dbt`, `dbt_<adapter>`): a flow «DBT» or «dbt duckdb»
    failed with «dbt found more than one package with the name …»."""
    p = _slug(flow_name)
    return f"tabularia_{p}" if p == "dbt" or p.startswith("dbt_") else p


def _schema_yaml(models: list[dict]) -> str:
    """models/schema.yml: descriptions, columns and generic tests (from the data contract)."""
    out = ["version: 2", "", "models:"]
    for m in models:
        out.append(f"  - name: {_y_name(m['name'])}")
        if m.get("description"):
            out.append(f"    description: {_y(m['description'])}")
        if m.get("tags") or m.get("meta"):
            # where the model comes from: the data team finds it (and selects it with tag:tabularia)
            out.append("    config:")
            if m.get("tags"):
                out.append("      tags: [" + ", ".join(_y(t) for t in m["tags"]) + "]")
            if m.get("meta"):
                out.append("      meta:")
                out.append("        tabularia:")
                for k, v in m["meta"].items():
                    if v is not None and v != "":
                        value = ("true" if v else "false") if isinstance(v, bool) else (v if isinstance(v, (int, float)) else _y(v))
                        out.append(f"          {k}: {value}")
        cols = m.get("columns") or []
        if cols:
            out.append("    columns:")
            for c in cols:
                out.append(f"      - name: {_y(c['name'])}")
                if c.get("description"):
                    out.append(f"        description: {_y(c['description'])}")
                tests = c.get("tests") or []
                if tests:
                    # dbt's generic tests write the name in the SQL as it is: «Nome Cliente» or «select»
                    # was a syntax error, and Postgres read «Canale» as canale. The models quote every name
                    out.append("        quote: true")
                    out.append("        data_tests:")
                    for t in tests:
                        if t["kind"] != "not_null":
                            # unique and accepted_values are singular tests (see column_tests): dbt's own
                            # ignore NULLs and write the values as quoted text
                            raise DbtExportError(f"the column test '{t['kind']}' is written as a singular test", "internal")
                        out.append("          - not_null:")
                        out.append(f"              config: {{severity: {t.get('severity', 'error')}}}")
    return "\n".join(out) + "\n"


def _sources_yaml(sources: list[dict]) -> str:
    out = ["version: 2", "", "sources:"]
    for s in sources:
        out.append(f"  - name: {_y_name(s['name'])}")
        if s.get("database"):
            out.append(f"    database: {_y_name(s['database'])}")
        out.append(f"    schema: {_y_name(s['schema'])}")
        out.append("    tables:")
        for t in sorted(s["tables"], key=lambda t: t["name"] if isinstance(t, dict) else t):
            if not isinstance(t, dict):
                out.append(f"      - name: {_y_name(t)}")
                continue
            out.append(f"      - name: {_y_name(t['name'])}")
            if t.get("description"):
                out.append(f"        description: {_y(t['description'])}")
            if t.get("columns"):
                out.append("        columns:")
                for c in t["columns"]:
                    out.append(f"          - name: {_y(c['name'])}")
                    if c.get("description"):
                        out.append(f"            description: {_y(c['description'])}")
    return "\n".join(out) + "\n"


def _seeds_yaml(seeds: list[dict]) -> str:
    out = ["version: 2", "", "seeds:"]
    for s in seeds:
        out.append(f"  - name: {_y_name(s['name'])}")
        if s.get("description"):
            out.append(f"    description: {_y(s['description'])}")
        # tagged as the models: the folder package's `+tag:<flow>,tag:tabularia` still loads it
        out.append("    config:")
        out.append('      tags: ["tabularia"]')
        if s.get("column_types"):
            # the DECLARED types, not guessed from the CSV: those Tabularia reads the file with
            out.append("      column_types:")
            for c, t in s["column_types"].items():
                out.append(f"        {_y_key(c)}: {_y(t)}")
        if s.get("columns"):
            out.append("    columns:")
            for c in s["columns"]:
                out.append(f"      - name: {_y(c['name'])}")
                if c.get("description"):
                    out.append(f"        description: {_y(c['description'])}")
    return "\n".join(out) + "\n"


def _model_header(m: dict) -> str:
    mat = m.get("materialized", "table")
    parts = [f"materialized={_jinja_str(mat)}"]
    if mat == "incremental":
        parts.append("incremental_strategy='append'")
    if m.get("schema"):
        parts.append(f"schema={_jinja_str(m['schema'])}")
    if m.get("alias"):
        parts.append(f"alias={_jinja_str(m['alias'])}")
    head = "{{ config(" + ", ".join(parts) + ") }}\n\n"
    if m.get("ai_translated"):
        head += f"-- Translated by AI from {_comment(m.get('ai_translated_from') or 'another dialect')}: review it before use.\n\n"
    review: dict[str, list[str]] = {}
    for step, why in m.get("da_rivedere") or []:
        review.setdefault(why, []).append(step)
    for why, steps in review.items():
        head += f"-- {_TO_REVIEW.get(why, 'Review')}: {_comment(', '.join(steps))}.\n"
    if review:
        head += "\n"
    meta = m.get("meta") or {}
    if meta.get("flow") or meta.get("datasource"):
        # where it comes from, at the top of the file: whoever opens it in the repository
        # finds the app. Facts of the flow only: exporting the same version again gives
        # the same file (a clean diff)
        if meta.get("flow"):
            version = f", version {meta['flow_version']}" if meta.get("flow_version") else ""
            if meta.get("version_saved_at"):
                version += f" saved {meta['version_saved_at']}"
            origin = f"Exported from Tabularia: flow «{meta['flow']}» (id {meta.get('flow_id')}{version})"
            if meta.get("output"):
                origin += f", {meta['output']}"
        else:
            origin = f"Exported from Tabularia: the SQL query of the datasource «{meta['datasource']}» (id {meta.get('datasource_id')})"
        head += f"-- {_comment(origin)}.\n"
        head += (f"-- Built by {_comment(meta['owner'])}. " if meta.get("owner") else "-- ") + \
            "Re-exporting the same version gives this same file.\n\n"
    return head


_SCHEMA_MACRO = """{% macro generate_schema_name(custom_schema_name, node) -%}
    {#- Tabularia: a model with a `schema` config lands in THAT schema (the output
        table of the flow), not in `<target schema>_<schema>` as dbt does by default. -#}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
"""


def build_dbt_project(
    flow_name: str, models: list[dict], attachments: list[dict] | None,
    sources: list[dict], native: dict | None = None, seeds: list[dict] | None = None,
    tests: list[dict] | None = None, notes: list[str] | None = None, flow_description: str = "",
    clickhouse: dict | None = None, layout: dict | None = None, schema: str | None = None,
    exported_by: str | None = None, ai: dict | None = None,
) -> dict[str, str]:
    """Assembles the files of a dbt project.

    `models`: [{name, sql, materialized, description, columns, schema?, alias?}].
    `sources`: [{name, database, schema, tables: [{name, description, columns}]}].
    `seeds`: [{name, csv, description, columns}]. `tests`: [{name, sql, severity, description}].
    Modes:
    · **duckdb** (federation): `attachments` = [{alias, db_type, conn, pw_env}].
    · **native** (warehouse): `native` = {db_type, conn, pw_env, target_schema}.
    · **clickhouse** (the customer's ClickHouse, federated sources): `clickhouse` =
      {host, port, secure, target_schema, federati: [{name, engine, conn, schema}]}.
    `layout` (the download dialog): {package: project|folder, folder, layers}. A FOLDER
    for an existing project has no profile nor dbt_project.yml: it has an INTEGRATION.md
    that says what to copy and what to add to theirs. `schema`: the models' schema in
    the dbt-duckdb profile (the other targets have it already)."""
    project = _project_name(flow_name)
    files: dict[str, str] = {}
    seeds, tests, notes = seeds or [], tests or [], list(notes or [])
    where = _Layout(layout)

    if clickhouse is not None:
        files["profiles.yml"] = _clickhouse_profile(project, clickhouse)
        if clickhouse.get("federati"):
            files[where.macro("create_tabularia_sources")] = _clickhouse_sources_macro(clickhouse["federati"])
    elif native:
        files["profiles.yml"] = _native_profile(project, native)
    else:
        extensions = sorted({_DUCK_ATTACH_TYPE[a["db_type"]] for a in (attachments or []) if a["db_type"] in _FEDERABLE})
        secrets = "\n".join(_duck_secret(a) for a in (attachments or []))
        attach = "\n".join(_duck_attach(a) for a in (attachments or []))
        files["profiles.yml"] = (
            f"{_y_name(project, key=True)}:\n"
            f"  target: dev\n"
            f"  outputs:\n"
            f"    dev:\n"
            f"      type: duckdb\n"
            f"      path: {project}.duckdb\n"
            + (f"      schema: {_y_name(schema)}\n" if schema else "")
            + _DUCK_SETTINGS
            + (f"      extensions: [{', '.join(extensions)}]\n" if extensions else "")
            + (f"      secrets:\n{secrets}\n      attach:\n{attach}\n" if attach else "")
        )

    dbt_project_yml = (
        f"name: '{project}'\n"
        f"version: '1.0.0'\n"
        f"config-version: 2\n"
        f"profile: '{project}'\n"
        f"model-paths: [\"models\"]\n"
        f"seed-paths: [\"seeds\"]\n"
        f"test-paths: [\"tests\"]\n"
        f"macro-paths: [\"macros\"]\n"
        f"models:\n  {_y_name(project, key=True)}:\n    +materialized: table\n"
    )

    # the «already declared» ones are in their sources.yml: only a folder copied into their project has one
    ours = package_sources(sources, layout)
    ours = [x for x in ours if not x.get("declared")]
    if ours:
        files[where.models("sources.yml")] = _sources_yaml(ours)
    files[where.models("schema.yml")] = _schema_yaml(models)
    for m in models:
        # m["sql"] is final dbt SQL: user text already neutralised, the exporter's macros in place
        files[where.model(m)] = _model_header(m) + m["sql"] + "\n"
    if any(m.get("schema") for m in models) and not where.folder:
        # not in the folder for an existing project: it would change the schema names of their WHOLE project
        files[where.macro("generate_schema_name")] = _SCHEMA_MACRO
    for s in seeds:
        files[where.seed(f"{_slug(s['name'])}.csv")] = s["csv"]
    if seeds:
        files[where.seed("schema.yml")] = _seeds_yaml(seeds)
    for t in tests:
        files[where.test(f"{_slug(t['name'])}.sql")] = (
            f"{{{{ config(severity={_jinja_str(t.get('severity', 'error'))}) }}}}\n"
            + (f"-- {_comment(t['description'])}\n" if t.get("description") else "")
            + t["sql"] + "\n"
        )

    preparation = where_they_go = ""
    if clickhouse is not None:
        federated = clickhouse.get("federati") or []
        intro = (
            f"Generated by Tabularia from the flow **{flow_name}**. Targets **dbt-clickhouse** on your\n"
            "ClickHouse: the source databases are read live through ClickHouse's PostgreSQL/MySQL table\n"
            "engines (one database `tab_src_<connection>_<schema>` per source schema, one table per source\n"
            "table), and from there on everything runs in ClickHouse.\n"
        )
        pip = "pip install dbt-clickhouse"
        pw_envs = ["CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"] + list(dict.fromkeys(f["conn"]["pw_env"] for f in federated))
        if federated:
            preparation = ("DBT_PROFILES_DIR=. dbt run-operation create_tabularia_sources --log-level-file none"
                            "   # once, and again when a source table changes its columns\n")
            reaches = ", ".join(sorted({f"`{f['conn']['host']}:{f['conn'].get('port') or (5432 if f['engine'] == 'PostgreSQL' else 3306)}`" for f in federated}))
            notes = [
                f"ClickHouse must reach the source databases ({reaches}): it reads them itself, dbt does not.",
                "`create_tabularia_sources` creates the `tab_src_*` databases and, in them, one table per source table "
                "(`ENGINE = PostgreSQL/MySQL`) with types that keep every value: a date is a `Date32`, an instant a "
                "`DateTime64(6)`, a Postgres `numeric` a `Float64` as Tabularia reads it (ClickHouse's own `Date`, `DateTime` "
                "and `Decimal(38, 19)` would wrap a date before 1970, drop the microseconds or overflow). The ClickHouse user "
                "needs `CREATE DATABASE`, `CREATE TABLE` and `DROP` on them, and ClickHouse keeps the source password in each "
                "table's definition (hidden in `SHOW CREATE`). Give it a read-only user on the sources.",
            ] + notes
        notes = ["`CLICKHOUSE_HOST`, `CLICKHOUSE_PORT` and `CLICKHOUSE_SECURE` point the profile at another ClickHouse; "
                 "the defaults are the one Tabularia computes on."] + notes
    elif native:
        adapter = _NATIVE_ADAPTER[native["db_type"]]
        intro = (
            f"Generated by Tabularia from the flow **{flow_name}**. Targets **dbt-{adapter}**: the\n"
            f"transformations run natively in the source warehouse (SQL translated to its dialect).\n"
        )
        pip = f"pip install dbt-{adapter}"
        pw_envs = [native["pw_env"]]
        if native["db_type"] == "postgresql":
            notes = ["The models' SQL was translated by sqlglot and not run on Postgres at export time: run `dbt build` "
                     "once on a copy before relying on it (a function sqlglot does not translate fails there)."] + notes
        if native["db_type"] == "clickhouse":
            port = (native.get("conn") or {}).get("port")
            notes = [f"The profile reaches ClickHouse on port {port} " + ("over TLS (`secure: true`), as Tabularia does on 8443 and 443"
                     if tls_for_port(port) else "without TLS, as Tabularia does on a port other than 8443 and 443")
                     + ": `CLICKHOUSE_SECURE=true|false` changes it."] + notes
    else:
        intro = (
            f"Generated by Tabularia from the flow **{flow_name}**. Targets **dbt-duckdb**, which\n"
            "attaches the origin database(s) and reads the tables live (no snapshots, no dialect translation).\n"
        )
        pip = "pip install dbt-duckdb"
        pw_envs = [a["pw_env"] for a in (attachments or [])]
        # where they land, said: whoever queried dbt_tabularia.<model> in the file found no such schema
        where_they_go = (f"The models are built in `{project}.duckdb`, in the schema `{schema or 'main'}`"
                          + ("" if schema else " (dbt-duckdb's default: the profile names no schema)") + ".\n\n")
    what = []
    for m in models:
        what.append(f"- `{m['name']}` ({m.get('materialized', 'table')}): {m.get('description') or ''}".rstrip(": "))
    steps = "".join(f"export {e}=...\n" for e in pw_envs) + preparation
    if seeds:
        steps += "DBT_PROFILES_DIR=. dbt seed\n"
    steps += "DBT_PROFILES_DIR=. dbt run\n"
    if tests or any(c.get("tests") for m in models for c in (m.get("columns") or [])):
        steps += "DBT_PROFILES_DIR=. dbt test\n"
    provenance = _provenance(models, exported_by) + _ai_section(ai)
    if where.folder:
        files.pop("profiles.yml", None)
        files["INTEGRATION.md"] = _integration(
            flow_name, intro, provenance, flow_description, what, models, sources, seeds, tests, notes, where,
            attachments=attachments, native=native, clickhouse=clickhouse, pip=pip, pw_envs=pw_envs, preparation=preparation,
        )
        return files
    files["dbt_project.yml"] = dbt_project_yml
    files["README.md"] = (
        f"# dbt project — {flow_name}\n\n{intro}\n" + where_they_go + provenance
        + (f"{flow_description}\n\n" if flow_description else "")
        + "## Models\n\n" + "\n".join(what) + "\n\n"
        + "## Run\n\n```bash\n" + pip + "\n"
        + "# set the source DB password(s) (referenced via env_var in profiles.yml):\n"
        + steps + "```\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n" if notes else "")
    )
    return files


def package_sources(sources: list[dict], layout: dict | None) -> list[dict]:
    """«Already declared» means the team's own sources.yml declares the source: only the folder
    package goes into their project. A complete project ignores it (decision 10): it left the
    source out of its sources.yml and `dbt parse` failed («depends on a source … not found»)."""
    if _Layout(layout).folder:
        return sources
    return [{k: v for k, v in x.items() if k != "declared"} for x in sources]


class _Layout:
    """Where every file goes. The complete project without options: as always (models/,
    seeds/, tests/, macros/). With a folder: models/<folder>/… (and so seeds, tests,
    macros), copied into the team's project. With the layers: the queries' models in
    staging/, the shared steps in intermediate/, the outputs in marts/."""

    _LAYER = {"raw": "staging", "intermediate": "intermediate", "output": "marts"}

    def __init__(self, layout: dict | None):
        layout = layout or {}
        folder = layout.get("folder") or ""
        if not re.fullmatch(r"[a-z0-9_]{0,40}", folder):
            raise DbtExportError(f"invalid folder name: «{folder}»", "folder_invalid", folder=folder)
        self.package = layout.get("package") or "project"
        self.folder = self.package == "folder"
        self.below = f"/{folder}" if folder else ""
        self.layers = bool(layout.get("layers"))

    def models(self, name: str) -> str:
        return f"models{self.below}/{name}"

    def model(self, m: dict) -> str:
        level = f"{self._LAYER.get(m.get('kind') or 'output', 'marts')}/" if self.layers else ""
        return self.models(f"{level}{_slug(m['name'])}.sql")

    def seed(self, name: str) -> str:
        return f"seeds{self.below}/{name}"

    def test(self, name: str) -> str:
        return f"tests{self.below}/{name}"

    def macro(self, name: str) -> str:
        return f"macros{self.below}/{name}.sql"


def _provenance(models: list[dict], exported_by: str | None = None) -> str:
    origins = {}
    for m in models:
        mt = m.get("meta") or {}
        if mt.get("flow_id") is not None:
            origins.setdefault(mt["flow_id"], mt)
    if not origins:
        return ""
    return "## Where it comes from\n\n" + "\n".join(
        f"- Tabularia flow «{mt['flow']}» (id {mt['flow_id']}, version {mt.get('flow_version')}"
        + (f", saved {mt['version_saved_at']}" if mt.get("version_saved_at") else "") + ")"
        + (f", built by {mt['owner']}" if mt.get("owner") else "")
        for mt in origins.values()
    ) + "\n\n" + (f"Exported by {exported_by}. " if exported_by else "") + \
        "Every model carries the same in `schema.yml` (`config.meta.tabularia`) and is tagged `tabularia`: `dbt run -s tag:tabularia` runs them all.\n\n"


def _ai_section(ai: dict | None) -> str:
    """What the AI wrote (download dialog): every flow's paragraph and the warning about
    the descriptions marked «(AI)» and the translated models."""
    if not ai:
        return ""
    parts = []
    summaries = ai.get("summaries") or []
    if summaries:
        parts.append("## What the flow does\n\n" + "\n\n".join(
            (f"**{r['flow']}** — " if len(summaries) > 1 else "") + r["text"] for r in summaries) + "\n")
    translated = ai.get("translated") or []
    warnings = []
    if summaries or ai.get("described"):
        warnings.append(f"Descriptions that start with `(AI)` were written by an AI model (`{ai.get('model')}`) from the structure "
                      "of the flow and a few sample values: review them before relying on them.")
    if translated:
        warnings.append("These models were translated by an AI model and passed Tabularia's checks (one SELECT, the same "
                      "output columns, only the tables they may read), but nobody has run them yet: "
                      + ", ".join(f"`{m}`" for m in translated) + ". Review them.")
    if warnings:
        parts.append("\n".join(f"- {a}" for a in warnings) + "\n")
    return "\n".join(parts) + ("\n" if parts else "")


def _flow_tag(flow_name: str, models: list[dict]) -> str:
    for m in models:
        if (m.get("meta") or {}).get("flow") == flow_name and len(m.get("tags") or []) > 1:
            return m["tags"][1]
    return _slug(flow_name)


def _integration(flow_name, intro, provenance, flow_description, what, models, sources, seeds, tests, notes, where, *,
                  attachments, native, clickhouse, pip, pw_envs, preparation) -> str:
    """INTEGRATION.md of the folder for an existing project: what to copy, what their
    profile needs, which sources they must declare already, how to run it."""
    tag = _flow_tag(flow_name, models)
    copy = [f"- `models{where.below}/` — {len(models)} models, `schema.yml`" + (", `sources.yml`" if any(not x.get("declared") for x in sources) else "")]
    if seeds:
        copy.append(f"- `seeds{where.below}/` — {len(seeds)} seeds (with their column types)")
    if tests:
        copy.append(f"- `tests{where.below}/` — {len(tests)} singular tests from the data contract")
    if clickhouse is not None and clickhouse.get("federati"):
        copy.append(f"- `macros{where.below}/` — `create_tabularia_sources`, which creates the source databases in ClickHouse")
    settings_ = ("```yaml\n      custom_settings:\n" + "\n".join(_clickhouse_settings()) + "\n```\n")
    if clickhouse is not None:
        profile = ("Your **dbt-clickhouse** output needs `join_use_nulls` (without it the missing side of an outer join is "
                   "0 or `''` instead of NULL):\n\n" + settings_)
    elif native:
        c = native.get("conn") or {}
        profile = (f"Your **dbt-{_NATIVE_ADAPTER[native['db_type']]}** profile on the {native['db_type']} database "
                   f"`{c.get('database')}` at `{c.get('host')}:{c.get('port')}`: the models run there, next to the data.\n")
        if native["db_type"] == "clickhouse":
            profile += ("\nIt needs `join_use_nulls` (without it the missing side of an outer join is 0 or `''` instead of "
                        "NULL):\n\n" + settings_)
    else:
        secrets = "\n".join(_duck_secret(a) for a in (attachments or []))
        attach = "\n".join(_duck_attach(a) for a in (attachments or []))
        profile = ("Your **dbt-duckdb** profile must attach the source databases with these aliases "
                   "(the sources read `<alias>.<schema>.<table>`). The passwords go through DuckDB secrets, not the "
                   "ATTACH path, so a failed connection does not print them:\n\n```yaml\n      secrets:\n" + secrets
                   + "\n      attach:\n" + attach + "\n```\n") if attach else \
            "Your **dbt-duckdb** profile: this flow reads no database.\n"
        profile += ("\nIts session must be in UTC, as Tabularia's engine is: otherwise what the flow does with time zones "
                    "follows the time zone of the machine that runs dbt.\n\n```yaml\n" + _DUCK_SETTINGS + "```\n")
    declared = [x for x in sources if x.get("declared")]
    source_rows = [f"- `{x['name']}` is **declared in your project**: it must list the tables "
                      + ", ".join(f"`{t['name']}`" for t in sorted(x["tables"], key=lambda t: t["name"])) + "."
                      for x in declared]
    if any(not x.get("declared") for x in sources):
        source_rows.append(f"- The others are declared in `models{where.below}/sources.yml`.")
    with_schema = [m for m in models if m.get("schema")]
    note = list(notes) + [
        f"The model `{m['name']}` has `schema='{m['schema']}'`: with dbt's default naming it lands in "
        f"`<your schema>_{m['schema']}`. The complete project ships a `generate_schema_name` macro that writes into "
        f"`{m['schema']}` itself; this folder does not, because it would rename the schemas of your whole project."
        for m in with_schema
    ]
    return (
        f"# Add «{flow_name}» to your dbt project\n\n{intro}\n" + provenance
        + (f"{flow_description}\n\n" if flow_description else "")
        + "## 1. Copy\n\nCopy these folders into your project as they are (dbt reads every subfolder of its "
        "model, seed, test and macro paths):\n\n" + "\n".join(copy) + "\n\n"
        + "## 2. Your profile\n\n" + profile + "\n"
        + ("## 3. Sources\n\n" + "\n".join(source_rows) + "\n\n" if source_rows else "")
        + "## Run\n\n```bash\n" + pip + "\n" + "".join(f"export {e}=...\n" for e in pw_envs)
        + preparation.replace("DBT_PROFILES_DIR=. ", "")
        + f"dbt build -s +tag:{tag},tag:tabularia\n```\n\n"
        f"`+tag:{tag},tag:tabularia` selects the models of the flow and everything they read (upstream flows, seeds), with "
        f"their tests: only what came from Tabularia, so a model of yours tagged `{tag}` is not built, nor what it reads.\n\n"
        + "## Models\n\n" + "\n".join(what) + "\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in note) + "\n" if note else "")
    )


def _slug(name: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(name).lower())
    out = out.strip("_") or "model"
    return out if not out[0].isdigit() else f"m_{out}"
