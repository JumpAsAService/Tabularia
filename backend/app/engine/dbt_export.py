"""Compila l'IR di un flusso in modelli dbt e assembla il progetto.

NON è un engine: è un exporter/escape-hatch. Il progetto dbt deve dare GLI STESSI
dati che Tabularia pubblica per lo stesso flusso: per questo, nel target
federato (dbt-duckdb), ogni operazione è resa con le STESSE funzioni SQL del
motore DuckDB (`duckdb_ops`: cast, predicati, join, pivot…), importate e non
ricopiate; nel target nativo (il warehouse di origine, SQL tradotto con sqlglot)
le funzioni che il dialetto non ha (`TRY_CAST`, `contains`, `ends_with`,
`UNION ALL BY NAME`, `UNPIVOT`) sono rese con forme portabili equivalenti.

Ogni modello è una catena di CTE (`WITH s1 AS (...), s2 AS (...) SELECT * FROM
sN`), col lato destro di join/union compilato come CTE aggiuntive. Da dove parte
un modello lo decide il gateway: una `source()`, un altro modello (`ref()`:
passi condivisi, query SQL, uscite di altri flussi) o un seed.

Lo SCHEMA (nomi e tipi delle colonne) è tracciato a compile-time: i tipi delle
sorgenti sono quelli che Tabularia ha letto all'import, e il primo passo di ogni
modello li impone (`CAST`) — così il progetto vede i dati come li vede il flusso,
anche quando il warehouse li tipizza diversamente. Servono al cast (stesso
risultato del motore: testo con spazi, interi letterali, troncamento) e
all'unpivot (supertipo comune). Where it would have to guess (a compute's type, a sql
node's columns, an aggregate's type) a probe asks DuckDB itself, over empty tables typed
like the sources (see `_Sonda`). The flow's free text (compute, sql) is read in the
dialect of the flow's engine (see `_espressione`).

`foreach` non è traducibile (control-flow a runtime) → errore esplicito; `pivot`
solo nel target federato.
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
from app.engine.operations import PIVOT_LABEL_SEP, check_filter_value

# resolver di una sorgente ({key} | {model} | {seed}) → (ref SQL, colonne, tipi DuckDB per colonna)
logger = logging.getLogger(__name__)
SourceResolver = Callable[[dict], tuple[str, list[str], dict]]
SEED_MAX_ROWS = 50_000          # un seed dbt è un CSV nel repository: oltre, non ha senso


class DbtExportError(EngineError):
    """The export refused. The English text stays in the logs; `code` and `params`
    go to the gateway, which says it in the reader's language."""

    def __init__(self, messaggio: str, code: str = "internal", **params):
        super().__init__(messaggio)
        self.code = code
        self.params = params


# ── tipi ─────────────────────────────────────────────────────────────────────
# UInt64 stays unsigned (UBIGINT: UInt64 on ClickHouse, numeric(20,0) on Postgres): as BIGINT a hash
# key over 2^63 wrapped to a negative on ClickHouse and failed the model on DuckDB
_POLARS_INT = {"Int8": "TINYINT", "Int16": "SMALLINT", "Int32": "INTEGER", "Int64": "BIGINT",
               "UInt8": "BIGINT", "UInt16": "BIGINT", "UInt32": "BIGINT", "UInt64": "UBIGINT"}


def duck_type(dtype: Optional[str]) -> Optional[str]:
    """Il dtype di Polars con cui Tabularia ha letto una colonna (stringa, come sta
    in `Datasource.columns`) → il tipo DuckDB equivalente. None = non noto/non
    rappresentabile (la colonna non viene ritipizzata)."""
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


_FUSO = "TIMESTAMPTZ"   # a database column Tabularia read as an instant with a time zone (a Postgres timestamptz)


def tipo_sorgente(dtype: Optional[str]) -> Optional[str]:
    """The type of a column of a DATABASE table or query that dbt reads live: as `duck_type`,
    except a datetime with a time zone, which is TIMESTAMPTZ. The first step turns it into
    Tabularia's naive UTC instant (see `_tipizza`): a bare CAST AS TIMESTAMP follows the
    session's time zone, the machine's in dbt-duckdb, the server's in Postgres."""
    if re.match(r"Datetime\(.*time_zone='[^']+'", str(dtype or "")):
        return _FUSO
    return duck_type(dtype)


def _tipizza(dialetto: Optional[str], col: str, tipo: str) -> str:
    """A source column with the type Tabularia read it with (see `_base`), in DuckDB SQL
    (`dialetto`: the warehouse the native targets translate it for)."""
    if tipo == _FUSO:
        if dialetto == "clickhouse":
            return f"CAST({_qi(col)} AS TIMESTAMP)"     # a ClickHouse DateTime is already an instant
        return f"CAST({_qi(col)} AS TIMESTAMPTZ) AT TIME ZONE 'UTC'"
    if tipo == SEED_TESTO:
        testo = f"CAST({_decodifica_seed(_qi(col))} AS VARCHAR)"
        return f'{testo} COLLATE "C"' if dialetto == "postgres" else testo
    if dialetto == "postgres" and _famiglia(tipo) == "testo":
        # Tabularia compares and orders text by bytes: the column carries the "C" collation from here
        # on (equality, joins, GROUP BY, DISTINCT, min/max, ORDER BY), whatever the database's or the
        # column's own (en_US: 'a' < 'B'; a case-insensitive ICU one: 'A' = 'a')
        return f'CAST({_qi(col)} AS {tipo}) COLLATE "C"'
    return f"CAST({_qi(col)} AS {tipo})"


def _per_byte(model: "_Model", tipo: Optional[str]) -> bool:
    """Whether a text must be given the byte order explicitly (Postgres: the "C" collation). The
    first step gives it to the source columns; text the flow makes (a literal, a cast) and the
    columns of another model carry the database's collation."""
    return model.native and model.dialetto == "postgres" and _famiglia(tipo) == "testo"


def _byte(model: "_Model", espr: str, tipo: Optional[str]) -> str:
    """A text compared or ordered by bytes, as Tabularia does (see `_per_byte`)."""
    return f'{espr} COLLATE "C"' if _per_byte(model, tipo) else espr


def _proiezione_tipizzata(dialetto: Optional[str], cols: list[str], tipi: dict) -> str:
    """The columns with Tabularia's types; one whose type is not known as it is."""
    return ", ".join(f"{_tipizza(dialetto, c, tipi[c]) if tipi.get(c) else _qi(c)} AS {_qi(c)}" for c in cols)


def _tipo_tipizzato(tipo: Optional[str]) -> Optional[str]:
    """The type a column has after `_tipizza`."""
    return "TIMESTAMP" if tipo == _FUSO else ("VARCHAR" if tipo == SEED_TESTO else tipo)


# ── seeds: the texts dbt's loaders change ─────────────────────────────────────
# dbt-duckdb's COPY reads "" as NULL; agate (dbt-postgres, dbt-clickhouse) reads '', 'null' in any
# case and blank text as NULL and turns \r\n and \r into \n; ClickHouse's CSV input then cuts the
# spaces and tabs at the ends and reads \N as NULL. Tabularia keeps every one of them, so such a
# value is written `~` + the text with ~ space tab \r escaped (none of which a loader changes),
# and the first step of a model that reads the seed gives it back (`_decodifica_seed`).
SEED_TESTO = "VARCHAR ~"   # the type of a seed's text column written so (see `_tipizza`)
_SEED_CODICI = {"~": "~0", " ": "~1", "\t": "~2", "\r": "~3"}


def _il_loader_lo_cambia(v: str) -> bool:
    return (v.strip().lower() in ("", "null") or "\r" in v or v[:1] in (" ", "\t") or v[-1:] in (" ", "\t")
            or v == "\\N" or v.startswith("~"))


def codifica_seed(df: Any) -> tuple[Any, list[str]]:
    """A seed's data (Polars) with every text a dbt seed loader would change written as
    `~…`; the other texts, and the columns without any such text, stay as they are (the
    CSV of such a seed is the same as before). Returns the data and the columns written so."""
    import polars as pl

    cambiate = []
    for c, t in df.schema.items():
        if duck_type(str(t)) != "VARCHAR":
            continue
        testi = df[c].cast(pl.String).to_list()
        if not any(v is not None and _il_loader_lo_cambia(v) for v in testi):
            continue
        cambiate.append(c)
        scritti = [v if v is None or not _il_loader_lo_cambia(v) else "~" + "".join(_SEED_CODICI.get(ch, ch) for ch in v) for v in testi]
        df = df.with_columns(pl.Series(c, scritti, dtype=pl.String))
    return df, cambiate


def _decodifica_seed(q: str) -> str:
    """A seed's text written by `codifica_seed`, back as Tabularia reads it. Every `~` of the
    written text starts a pair, so the replacements cannot meet; `~0` goes last."""
    t = f"substr({q}, 2)"
    for codice, testo in (("~1", "' '"), ("~2", "chr(9)"), ("~3", "chr(13)"), ("~0", "'~'")):
        t = f"replace({t}, '{codice}', {testo})"
    return f"CASE WHEN left({q}, 1) = '~' THEN {t} ELSE {q} END"


_TESTO = ("VARCHAR", "STRING", "TEXT", "CHAR")
_NUMERI = ("DOUBLE", "FLOAT", "REAL", "DECIMAL")
_INTERI = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "INT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT")


def _famiglia(t: Optional[str]) -> str:
    t = (t or "").upper()
    if t.startswith(_TESTO):
        return "testo"
    if t.startswith(_INTERI):
        return "intero"
    if t.startswith(_NUMERI):
        return "numero"
    return t.lower() or "?"


class _S:
    """Lo schema corrente di un modello: colonne in ordine e il loro tipo DuckDB (o None).
    No columns = the columns are NOT known (after a pivot, whose columns the data decides,
    or a sql node nobody can read): the steps after it use `*` and DuckDB's forms that need
    no list. Then `types` keeps the types still known (the pivot's index) and `altre` is the
    type of every other column when there is one (a pivot's columns: its aggregate's)."""

    def __init__(self, cols: list[str], types: Optional[dict] = None, altre: Optional[str] = None):
        self.cols = list(cols)
        self.types = {c: (types or {}).get(c) for c in self.cols} if self.cols else dict(types or {})
        self.altre = None if self.cols else altre
        self.ordine: Optional[list[tuple[str, bool]]] = None   # the last sort's keys [(column, descending)], while valid

    def con(self, cols: list[str], types: dict) -> "_S":
        return _S(cols, types)

    def tipo(self, col: str) -> Optional[str]:
        if col in self.types:
            return self.types[col]
        return None if self.cols else self.altre


def _uguali_ma_per_maiuscole(cols: list[str]) -> list[str]:
    """The names that another name of `cols` repeats but for its case (`Codice`, `codice`):
    DuckDB resolves names without case, so it reads them as ONE column."""
    per: dict[str, list[str]] = {}
    for c in cols:
        per.setdefault(c.casefold(), []).append(c)
    return [c for gruppo in per.values() if len(gruppo) > 1 for c in gruppo]


def _rifiuta_maiuscole(model: "_Model", s: "_S", passo: str) -> None:
    """Federated target: two columns whose names differ only by case are refused. DuckDB
    resolves both to the first one and renames the second `codice_1` with the first one's
    values, without an error (Polars and ClickHouse keep both, with their own values)."""
    doppi = _uguali_ma_per_maiuscole(s.cols)
    if doppi and not model.native:
        raise DbtExportError(
            f"{passo}: the columns {', '.join(f'«{c}»' for c in doppi)} differ only by case: DuckDB, which the federated "
            "target runs on, reads them as one column. Rename one of them in the flow, or use the native or the ClickHouse target.",
            "columns_differ_only_by_case", columns=", ".join(doppi), step=passo)


MAX_NOME_POSTGRES = 63   # Postgres keeps the first 63 bytes of a name (NAMEDATALEN - 1), whole characters only


def nome_in_postgres(nome: str) -> str:
    """The name a column has in Postgres: its first 63 bytes, without a character cut in half."""
    return nome.encode()[:MAX_NOME_POSTGRES].decode(errors="ignore")


def _rifiuta_lunghi(model: "_Model", s: "_S", passo: str) -> None:
    """Native Postgres: two names that share their first 63 bytes are ONE column there, and the
    model fails ("column reference … is ambiguous"). A single long name only gets shorter, the
    same everywhere it is used (the README says so)."""
    if not (model.native and model.dialetto == "postgres"):
        return
    per: dict[str, list[str]] = {}
    for c in s.cols:
        per.setdefault(nome_in_postgres(c), []).append(c)
    doppi = [c for gruppo in per.values() if len(gruppo) > 1 for c in gruppo]
    if doppi:
        raise DbtExportError(
            f"{passo}: the columns {', '.join(f'«{c}»' for c in doppi)} are the same column in Postgres, which keeps only the first "
            f"{MAX_NOME_POSTGRES} bytes of a name. Shorten one of them in the flow, or use the federated or the ClickHouse target.",
            "column_names_too_long", columns=", ".join(doppi), limit=MAX_NOME_POSTGRES)


# the types the probe reports and the export keeps (a list, a struct, a map: not known)
_TIPO_SCALARE = re.compile(
    r"(BOOLEAN|U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|FLOAT|DOUBLE|DECIMAL\(\d+,\d+\)|VARCHAR|DATE|TIME|"
    r"TIMESTAMP|TIMESTAMP WITH TIME ZONE|TIMESTAMP_(S|MS|NS)|INTERVAL|BLOB|UUID)")


_IGNOTO = "__tabularia_unknown_type"
_TIPO_IGNOTO = f'STRUCT("{_IGNOTO}" BLOB)'   # in the probe, a column whose type is not known


class _Sonda:
    """DuckDB in memory over EMPTY tables typed like the sources: after each step it reads
    the names and the types the model's columns really have (what dbt-duckdb sees) where
    the export would otherwise guess them — a compute's type, a sql node's columns, an
    aggregate's type. It reads no data and no file (external access off, one statement
    at a time). It stops, and the export goes on with what it tracks by itself, when a
    source's type is not known, after a pivot (its columns come from the data) or at the
    first step that does not bind in DuckDB."""

    def __init__(self):
        self._con = None
        self.attiva = True
        self._fatte = 0          # the model's CTEs already created (empty tables)
        self.errore: Optional[tuple[str, str]] = None   # (step, DuckDB's error) where it stopped
        self.per_ignoto = False  # it stopped on a column whose type is not known (`_TIPO_IGNOTO`)

    def spegni(self) -> None:
        self.attiva = False

    def chiudi(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def _connessione(self):
        if self._con is None:
            import duckdb

            self._con = duckdb.connect(":memory:", config={"enable_external_access": False,
                                                          "autoinstall_known_extensions": False})
            self._con.execute("SET lock_configuration = true")
        return self._con

    def _vista(self, con, nome: str, sql: str) -> None:
        """The step as an EMPTY TABLE with its columns: the next step binds against it alone. As a
        view, every step re-bound the whole chain before it (150 columns, 120 steps: 10 s)."""
        if len(con.extract_statements(sql)) != 1:
            raise ValueError("not one statement")
        con.execute(f"CREATE TABLE {_qi(nome)} AS SELECT * FROM ({sql}) LIMIT 0")

    def allinea(self, model: "_Model") -> bool:
        """The tables of the steps added since the last call. False (and off) at the first
        that does not bind."""
        if not self.attiva:
            return False
        try:
            con = self._connessione()
            while self._fatte < len(model.ctes):
                nome, sql, _ = model.ctes[self._fatte]
                sql = model.sonde.get(nome, sql)
                for segnaposto, duck in model.per_sonda.items():
                    sql = sql.replace(segnaposto, duck)
                self._vista(con, nome, sql)
                self._fatte += 1
        except Exception as e:  # noqa: BLE001 — the probe only helps: without it the export goes on
            passo = model.ctes[self._fatte][0] if self._fatte < len(model.ctes) else "?"
            logger.debug("dbt export: the probe stops at %s (%s)", passo, e)
            self.errore = (passo, str(e).splitlines()[0][:200] if str(e) else type(e).__name__)
            self.per_ignoto = _IGNOTO in str(e)
            self.spegni()
            return False
        return True

    def colonne(self, model: "_Model", sql: str) -> Optional[list[tuple[str, Optional[str]]]]:
        """[(name, type or None)] of a query over the steps so far, or None."""
        if not self.allinea(model):
            return None
        try:
            con = self._connessione()
            if len(con.extract_statements(sql)) != 1:
                return None
            righe = con.execute(f"DESCRIBE {sql}").fetchall()
        except Exception as e:  # noqa: BLE001
            logger.debug("dbt export: the probe cannot describe a query (%s)", e)
            return None
        return [(r[0], r[1] if _TIPO_SCALARE.fullmatch(str(r[1])) else None) for r in righe]

    def dopo(self, model: "_Model", ref: str, s: "_S", op_type: str) -> "_S":
        """The schema after a step: the probe's when it can read it, otherwise the tracked one.
        Not with two names that differ only by case: DuckDB would report the second as
        `codice_1`, a column the warehouse does not have."""
        if op_type == "pivot" or _uguali_ma_per_maiuscole(s.cols):
            self.spegni()
        if not self.attiva:
            return s
        colonne = self.colonne(model, f"SELECT * FROM {_qi(ref)}")
        if colonne is None:
            self.spegni()
            return s
        return _S([c for c, _ in colonne], dict(colonne))


class _Namer:
    """Genera nomi di CTE univoci nel modello (s1, s2, r1s1, …)."""

    def __init__(self, prefix: str = "s"):
        self.prefix = prefix
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self.prefix}{self._n}"


# parole riservate in almeno un dialetto (DuckDB, Postgres, MySQL, ClickHouse): un
# passo non si può chiamare così senza virgolette (trovato: una CTE `unpivot` su DuckDB)
_RISERVATE = frozenset("""
all analyse analyze and any array as asc asymmetric between both by case cast check collate column columns constraint
create cross current current_date current_time current_timestamp current_user default deferrable desc describe distinct
do drop else end except exists explain false fetch filter first for foreign from full grant group having ilike in
initially inner intersect interval into is join key last lateral leading left like limit map minus natural not null nulls
offset on only or order over partition pivot placing primary qualify range recursive references returning right row rows
sample select session_user set show some struct symmetric table tablesample then to trailing true union unique unpivot
user using values variadic when where window with within
""".split())


def _slug_passo(testo: str, massimo: int = 40) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(testo).lower()).strip("_")[:massimo].strip("_") or "step"
    if out in _RISERVATE:
        out = f"{out}_step"
    return out if not out[0].isdigit() else f"s_{out}"


def _commento(testo: str) -> str:
    """Una nota su UNA riga, che non chiude il commento e non apre Jinja."""
    t = re.sub(r"\s+", " ", str(testo)).strip()
    for a, b in (("*/", "* /"), ("}}", "} }"), ("%}", "% }"), ("#}", "# }")):
        t = t.replace(a, b)
    # a space after EVERY `{` that would open a Jinja block: replacing the pairs left
    # one open in `{{{` (→ `{ {{`)
    t = re.sub(r"\{(?=[{%#])", "{ ", t)
    return t[:200]


# ── Jinja: dbt renders every model, test, macro, profile and YAML value ───────
_JINJA_DA_SCRIVERE = re.compile(r"\{[{%#]|\r")   # what Jinja would not write as it is


def neutralize_jinja(text: Any) -> str:
    """User text (a value, a name, an expression, a query, a description) written into
    a file dbt renders as Jinja: every opener (`{{`, `{%`, `{#`) becomes an expression
    that prints it, so dbt writes the text as it is instead of running it. A value
    `{{ env_var('…') }}` stays that text in the table, as in Tabularia; `price {% 10`
    does not break the project. Text without openers comes out unchanged.
    Only for text that is NOT already inside a Jinja expression (see `_jinja_str`).
    A `\\r` too: Jinja writes every line break of a template as `\\n` (dbt 1.12.5 turned
    'a\\r\\nb' into 'a\\nb' in the table), so it is printed by an expression, `{{ '\\r' }}`."""
    return _JINJA_DA_SCRIVERE.sub(lambda m: "{{ '\\r' }}" if m.group(0) == "\r" else "{{ '" + m.group(0) + "' }}", str(text))


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
    pezzi = [{"'": '"\'"', "\\": "'\\\\'", "\r": "'\\r'"}.get(x, f"'{x}'") for x in re.split(r"(['\\\r])", s) if x]
    return " ~ ".join(pezzi + ([] if len(pezzi) > 1 else ["''"]))


class _Model:
    """Accumula le CTE di un modello. Ogni CTE ha un NOME che dice cosa fa
    (`filter_stato`, `aggregate_by_categoria`) e una NOTA in parole: chi rivede il
    progetto in una pull request deve capire il passo senza aprire Tabularia."""

    def __init__(self, namer: _Namer, native: bool = False, dialetto: str | None = None, testo: str = "duckdb",
                 usati: set | None = None):
        self.namer = namer
        self.native = native
        self.dialetto = dialetto   # il dialetto di arrivo, dove una forma dipende dal warehouse
        self.testo = testo         # the dialect the flow's free text is written in (its engine's)
        self.da_rivedere: list[tuple[str, str]] = []   # (step, why): text the team must review
        self.ctes: list[tuple[str, str, str]] = []  # (nome, sql, nota)
        self.etichette: list[tuple[str, str]] = []   # (nome, nota) del passo che si sta compilando
        # the CTE names taken, in the whole project when the caller shares the set: dbt inlines an
        # ephemeral model as a CTE of the model that reads it, and ClickHouse 24 refuses a model
        # whose CTE has the name of one inside the inlined model (UNKNOWN_TABLE)
        self._usati: set[str] = usati if usati is not None else set()
        self.sonda = _Sonda()
        self.sonde: dict[str, str] = {}              # step → the SQL the probe reads instead (a typed empty source, …)
        self.segnaposti: dict[str, str] = {}         # placeholder → text in the warehouse's dialect (see testo_nativo)
        self.per_sonda: dict[str, str] = {}          # placeholder → the same in DuckDB, for the probe
        # random, so no text of the flow can spell a placeholder out; it never reaches the zip
        self._marca = secrets.token_hex(6)

    def testo_nativo(self, finale: str, duck: str) -> str:
        """SQL written directly in the warehouse's dialect (ClickHouse functions sqlglot would
        misread): a placeholder in the model, which sqlglot translates without touching it;
        `rimetti_testi` puts the text back after the translation. `duck`: the same in DuckDB,
        for the probe."""
        segnaposto = f"__tabularia_{self._marca}_t{len(self.segnaposti)}__()"
        self.segnaposti[segnaposto] = finale
        self.per_sonda[segnaposto] = duck
        return segnaposto

    def _unico(self, base: str) -> str:
        nome, n = _slug_passo(base), 2
        while nome in self._usati:
            nome = f"{_slug_passo(base, 36)}_{n}"
            n += 1
        self._usati.add(nome)
        return nome

    def _prossimo(self, base: str) -> str:
        """The name `add` will give the next step called `base` (it does not reserve it)."""
        nome, n = _slug_passo(base), 2
        while nome in self._usati:
            nome = f"{_slug_passo(base, 36)}_{n}"
            n += 1
        return nome

    def add(self, sql: str, nome: Optional[str] = None, nota: Optional[str] = None, sonda: Optional[str] = None) -> str:
        if nome is None and self.etichette:
            nome, nota_passo = self.etichette[-1]
            nota = nota if nota is not None else nota_passo
        name = self._unico(nome or self.namer.next())
        self.ctes.append((name, sql, _commento(nota or "")))
        if sonda is not None:
            self.sonde[name] = sonda
        return name


# ── forme portabili per il target nativo ─────────────────────────────────────
# The guards check the VALUE, not only its shape: '2024-02-30', 25:00 or a 20-digit integer
# passed a shape-only pattern, and then Postgres failed the whole model and ClickHouse made
# up a value (1 March, the next day). Only patterns, never a CAST, decide: no warehouse is
# asked to evaluate a conversion that could fail before the guard has said yes.
def _al_massimo(n: str) -> str:
    """A pattern for the numbers written with len(n) digits that are <= n."""
    parti = [n[:i] + (f"[0-{int(c) - 1}]" if c > "1" else "0") + (f"[0-9]{{{len(n) - i - 1}}}" if i < len(n) - 1 else "")
             for i, c in enumerate(n) if c > "0"]
    return "(" + "|".join(parti + [n]) + ")"


# an integer literal that fits a BIGINT (Postgres fails beyond, the engine gives NULL)
_RE_INTERO = (f"^([+]?0*([0-9]{{1,18}}|{_al_massimo('9223372036854775807')})"
              f"|-0*([0-9]{{1,18}}|{_al_massimo('9223372036854775808')}))$")
_RE_NUMERO = r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$"
# A real day, as both engines (Polars and DuckDB) read one: a month and a day of one or two digits,
# a year of one to four (24 is the year 24, not 2024); months of 30 and 31 days, 29 February only in
# a leap year, no year 0 (Postgres has none). Native gave NULL for '2024-1-5', which both engines read.
_ANNO = r"(0{0,3}[1-9]|0{0,2}[1-9][0-9]|0?[1-9][0-9]{2}|[1-9][0-9]{3})"
_ANNO_BISESTILE = (r"(0{0,3}[48]|0{0,2}([2468][048]|[13579][26])|(0?[1-9]|[1-9][0-9])(0[48]|[2468][048]|[13579][26])"
                   r"|(0?[48]|[2468][048]|[13579][26])00)")
_GIORNO = (rf"({_ANNO}-((0?[13578]|1[02])-(0?[1-9]|[12][0-9]|3[01])|(0?[469]|11)-(0?[1-9]|[12][0-9]|30)|0?2-(0?[1-9]|1[0-9]|2[0-8]))"
           rf"|{_ANNO_BISESTILE}-0?2-29)")
_RE_DATA = rf"^{_GIORNO}$"
# a real instant: a 'T' or white space, hours 0-23, minutes and seconds 0-59, of one or two digits; after
# the seconds a 'Z' or an offset, which both engines DROP (the wall-clock time stays: 10:30+02 is 10:30)
_SPAZI = r"[ \t\n\r\f\v]"   # the white space both engines take between the day and the time
_RE_ISTANTE = (rf"^{_GIORNO}(({_SPAZI}+|T)([01]?[0-9]|2[0-3]):[0-5]?[0-9](:[0-5]?[0-9]([.][0-9]*)?(Z|[+-][0-9]{{2}}(:?[0-9]{{2}})?(:[0-9]{{2}})?)?)?)?$")
# the years ClickHouse's Date32 and DateTime64 hold: it moved any other to 1900 or 2299, a made-up value
_RE_ANNI_CLICKHOUSE = r"^(19|2[0-2])[0-9]{2}-"
# the BIGINT range for a number: NaN and ±inf compare false, so they are outside too
_MIN_BIGINT, _OLTRE_BIGINT = "-9223372036854775808", "9223372036854775808"
_ESPONENTE_SENZA_ZERI = (r"([eE][+-]?)0+([0-9])", r"\1\2")   # 1e00005 → 1e5 (pattern, replacement)


def _sostituisci(t: str, modello: str, con: str) -> str:
    """`regexp_replace` with a pattern anchored at the start: it matches once, so DuckDB and
    Postgres (the first match) and ClickHouse (every match) give the same text."""
    return f"regexp_replace({t}, {_lit(modello)}, {_lit(con)})"


def _data_canonica(t: str) -> str:
    """A day that passed `_GIORNO` (alone or before a time) written `YYYY-MM-DD`, the form every
    warehouse reads as the engines do (Postgres read '24-01-05' as 2024, ClickHouse refused '2024-1-5')."""
    for n, zeri in ((1, "000"), (2, "00"), (3, "0")):
        t = _sostituisci(t, "^([0-9]{%d})-" % n, zeri + r"\1-")
    t = _sostituisci(t, r"^([0-9]{4})-([0-9])-", r"\1-0\2-")
    return _sostituisci(t, r"^([0-9]{4}-[0-9]{2})-([0-9])( |$)", r"\1-0\2\3")


def _istante_canonico(t: str) -> str:
    """An instant that passed `_RE_ISTANTE` written `YYYY-MM-DD HH:MM:SS[.ffffff]`, the form every
    warehouse reads (ClickHouse gave NULL for `10:30` and for a `T`): the offset dropped, as both
    engines drop it; the fraction cut at the microsecond, as they do (Postgres would round it)."""
    t = _sostituisci(f"replace({t}, 'T', ' ')", rf"^([0-9-]+){_SPAZI}+", r"\1 ")
    t = _sostituisci(t, r"^([^ ]+ [^Z+-]+)(Z|[+-].*)$", r"\1")
    t = _sostituisci(t, r"^([^.]*[.][0-9]{6})[0-9]+$", r"\1")
    t = _sostituisci(t, r"^([^.]*)[.]$", r"\1")
    t = _data_canonica(t)
    t = _sostituisci(t, r"^([0-9-]{10}) ([0-9]):", r"\1 0\2:")
    t = _sostituisci(t, r"^([0-9-]{10} [0-9]{2}):([0-9])(:|$)", r"\1:0\2\3")
    t = _sostituisci(t, r"^([0-9-]{10} [0-9]{2}:[0-9]{2}):([0-9])([.]|$)", r"\1:0\2\3")
    t = _sostituisci(t, r"^([0-9-]{10})$", r"\1 00:00:00")
    return _sostituisci(t, r"^([0-9-]{10} [0-9]{2}:[0-9]{2})$", r"\1:00")


# Postgres fails a text beyond the double's range ('1e400', '1e-400'), which both engines read as
# ±inf and ±0: the value is read as a numeric first. From the halfway point to 2^1024 a double is
# infinite; up to the halfway point to the smallest one, zero (the two bounds are a hair inside
# those points, so no text Postgres would refuse reaches its cast)
_OLTRE_DOPPIO, _SOTTO_DOPPIO = "1.797693134862315807937e308", "2.4703282292062328e-324"


def _doppio_postgres(t: str) -> str:
    """A text that passed `_RE_NUMERO` as a double, in Postgres' SQL. An exponent of five digits or
    more is decided by its sign (a numeric would overflow); beyond 1000 characters, NULL."""
    segno = lambda negativo, positivo: f"CAST(CASE WHEN left({t}, 1) = '-' THEN '{negativo}' ELSE '{positivo}' END AS DOUBLE PRECISION)"  # noqa: E731
    infinito, zero, numero = segno("-Infinity", "Infinity"), segno("-0", "0"), f"CAST({t} AS NUMERIC)"
    # five significant digits: 1e00005 is 100000
    grande = f"{t} ~ '[eE][+]?0*[1-9][0-9]{{4,}}$'"
    return (f"(CASE WHEN length({t}) > 1000 THEN NULL "
            f"WHEN {t} ~ '[eE]-0*[1-9][0-9]{{4,}}$' OR ({grande} AND {t} !~ '^[+-]?[0-9.]*[1-9]') THEN {zero} "
            f"WHEN {grande} THEN {infinito} "
            f"WHEN abs({numero}) >= {_OLTRE_DOPPIO} THEN {infinito} "
            f"WHEN {numero} <> 0 AND abs({numero}) < {_SOTTO_DOPPIO} THEN {zero} "
            f"ELSE CAST({t} AS DOUBLE PRECISION) END)")


def _float_in_testo(q: str, dialetto: Optional[str]) -> str:
    """A float as text the way Tabularia writes it: a whole value keeps `.0` (200.0 → '200.0';
    every warehouse prints '200'). On ClickHouse, the ClickHouse engine's own rule (up to 1e15,
    its toString for the rest); elsewhere DuckDB's and Polars': `.0` up to 1e16, 'nan', 'inf',
    '-0.0' (Postgres prints 'NaN', 'Infinity', '-0')."""
    intero = f"CAST(CAST({q} AS BIGINT) AS VARCHAR) || '.0'"
    if dialetto == "clickhouse":
        return f"CASE WHEN {q} = trunc({q}) AND abs({q}) < 1e15 THEN {intero} ELSE CAST({q} AS VARCHAR) END"
    return (f"CASE WHEN {q} = CAST('NaN' AS DOUBLE) THEN 'nan' WHEN {q} = CAST('inf' AS DOUBLE) THEN 'inf' "
            f"WHEN {q} = CAST('-inf' AS DOUBLE) THEN '-inf' WHEN CAST({q} AS VARCHAR) IN ('-0', '-0.0') THEN '-0.0' "
            f"WHEN {q} = trunc({q}) AND abs({q}) < 1e16 THEN {intero} ELSE CAST({q} AS VARCHAR) END")


def _cast_portabile(q: str, src: Optional[str], duck_t: str, dialetto: Optional[str] = None, model: Optional["_Model"] = None) -> str:
    """Lo stesso cast del motore (fallito → NULL; testo con spazi tagliati; testo →
    intero solo se intero letterale; numero → intero per troncamento) senza
    TRY_CAST, che i warehouse non hanno: un'espressione regolare decide prima se il
    testo è convertibile, così un valore sbagliato dà NULL invece di far fallire il modello.
    A text the two engines read alike gives their value: where they disagree, the DuckDB
    engine's (the federated target's) or NULL. `model`: where Postgres needs its own SQL."""
    fam = _famiglia(src)
    if duck_t == "VARCHAR" and str(src or "").upper() in ("DOUBLE", "FLOAT", "REAL"):
        return _float_in_testo(q, dialetto)
    if fam == "testo":
        t = f"trim({q})"
        if duck_t == "BIGINT":
            return f"CASE WHEN regexp_matches({t}, {_lit(_RE_INTERO)}) THEN CAST({t} AS BIGINT) END"
        if duck_t in ("DATE", "TIMESTAMP"):
            guardia = f"regexp_matches({t}, {_lit(_RE_ISTANTE if duck_t == 'TIMESTAMP' else _RE_DATA)})"
            if dialetto == "clickhouse":
                guardia += f" AND regexp_matches({t}, {_lit(_RE_ANNI_CLICKHOUSE)})"
            valore = _istante_canonico(t) if duck_t == "TIMESTAMP" else _data_canonica(t)
            return f"CASE WHEN {guardia} THEN CAST({valore} AS {duck_t}) END"
        if duck_t == "DOUBLE":
            numero, zero = f"CAST({t} AS DOUBLE)", ""
            if dialetto == "postgres" and model is not None:
                numero = model.testo_nativo(_doppio_postgres(t), numero)
            if dialetto == "clickhouse":
                # ClickHouse reads '0e99999' as nan: a mantissa without a nonzero digit is a zero; and
                # '1e00005' as 1: the exponent without its leading zeros (one 'e' in the text: one match)
                zero = (f"WHEN regexp_matches({t}, {_lit(_RE_NUMERO)}) AND regexp_matches({t}, '^[+-]?[0.]*([eE]|$)') "
                        f"THEN CAST(CASE WHEN left({t}, 1) = '-' THEN '-0' ELSE '0' END AS DOUBLE) ")
                numero = f"CAST({_sostituisci(t, *_ESPONENTE_SENZA_ZERI)} AS DOUBLE)"
            return (f"CASE WHEN lower({t}) IN ('inf', '+inf', 'infinity', '+infinity') THEN CAST('Infinity' AS DOUBLE) "
                    f"WHEN lower({t}) IN ('-inf', '-infinity') THEN CAST('-Infinity' AS DOUBLE) "
                    f"WHEN lower({t}) IN ('nan', '+nan', '-nan') THEN CAST('NaN' AS DOUBLE) "
                    f"{zero}WHEN regexp_matches({t}, {_lit(_RE_NUMERO)}) THEN {numero} END")
        if duck_t == "BOOLEAN":
            # the DuckDB engine's words (Polars cannot cast a text to a boolean)
            return (f"CASE WHEN lower({t}) IN ('true', 't', '1', 'yes', 'y') THEN TRUE "
                    f"WHEN lower({t}) IN ('false', 'f', '0', 'no', 'n') THEN FALSE END")
        return f"CAST({t} AS {duck_t})"
    if duck_t == "BIGINT" and fam == "numero":
        return f"CASE WHEN {q} >= {_MIN_BIGINT} AND {q} < {_OLTRE_BIGINT} THEN CAST(trunc({q}) AS BIGINT) END"
    # boolean ↔ number: Postgres has no such cast (only boolean ↔ integer); the engine's values, written out
    if fam == "boolean" and duck_t in ("BIGINT", "DOUBLE"):
        return f"CAST(CASE WHEN {q} THEN 1 WHEN NOT {q} THEN 0 END AS {duck_t})"
    if duck_t == "BOOLEAN" and fam in ("intero", "numero"):
        return f"CASE WHEN {q} <> 0 THEN TRUE WHEN {q} = 0 THEN FALSE END"
    if duck_t == "BIGINT" and str(src or "").upper() in ("UBIGINT", "HUGEINT", "UHUGEINT"):
        # an integer that may not fit: ClickHouse would wrap it, Postgres fail
        return f"CASE WHEN {q} >= {_MIN_BIGINT} AND {q} < {_OLTRE_BIGINT} THEN CAST({q} AS BIGINT) END"
    return f"CAST({q} AS {duck_t})"


def _predicato_portabile(column: str, operator: str, value: Any, byte: bool = False) -> str:
    """Come `duckdb_ops._predicate`, ma senza contains/starts_with/ends_with, che
    non tutti i dialetti hanno. `byte`: a text range in byte order (see `_byte`)."""
    c = _qi(column)
    if byte and operator in ("gt", "ge", "lt", "le", "between"):
        cc = f'{c} COLLATE "C"'
        if operator == "between":
            lo, hi = (_as_list(value) + [None, None])[:2]
            return f"{cc} BETWEEN {_lit(lo)} AND {_lit(hi)}"
        return f"{cc} {dict(gt='>', ge='>=', lt='<', le='<=')[operator]} {_lit(value)}"
    if operator in ("contains", "starts_with", "ends_with"):
        v, testo = str(value), f"CAST({c} AS VARCHAR)"
        if operator == "contains":
            return f"strpos({testo}, {_lit(v)}) > 0"
        return f"{'left' if operator == 'starts_with' else 'right'}({testo}, {len(v)}) = {_lit(v)}"
    return _predicate(column, operator, value)


# DuckDB's escape strings (E'…') follow Postgres' rules: \b \f \n \r \t, an octal or a hex byte, any
# other character after a backslash as it is, '' a quote. DuckDB refuses a byte outside ASCII and \u.
_ESCAPE_E = re.compile(r"''|\\(?:([0-7]{1,3})|x([0-9A-Fa-f]{1,2})|([uU])|(.))", re.DOTALL)
_ESCAPE_UNO = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_FORSE_ESCAPE = re.compile(r"(?<![\w$])[eE]'")


def _valore_escape(corpo: str) -> Optional[str]:
    """The value DuckDB gives the body of an E'…' string, or None where DuckDB refuses it."""
    def uno(m: re.Match) -> str:
        if m.group(0) == "''":
            return "'"
        ottale, esadecimale, unicode, car = m.groups()
        if unicode:
            raise ValueError(m.group(0))
        if ottale or esadecimale:
            n = int(ottale, 8) if ottale else int(esadecimale, 16)
            if not 0 < n < 0x80:
                raise ValueError(m.group(0))
            return chr(n)
        return _ESCAPE_UNO.get(car, car)

    try:
        return _ESCAPE_E.sub(uno, corpo)
    except ValueError:
        return None


def _stringhe_standard(sql: str) -> str:
    """DuckDB text with every escape string (E'…') written as the standard string of the same
    value ('…' with '', its line breaks raw). sqlglot reads the escapes its own way (`\\q` and
    `\\101` stay as written, DuckDB reads `q` and `A`) and writes an escape string back without
    doubling a backslash, so DuckDB and Postgres then read `\\ ` as an escape: 'a\\ b' became
    'a b' on native Postgres and in a sql node of the federated target, `\\q` stayed `\\q` on
    ClickHouse. A standard string has no escapes: every dialect sqlglot writes keeps its bytes.
    One DuckDB refuses is left as it is."""
    if not _FORSE_ESCAPE.search(sql):
        return sql
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import TokenType

    try:
        token = Dialect.get_or_raise("duckdb").tokenize(sql)
    except Exception:  # noqa: BLE001 — the parse that follows says what is wrong
        return sql
    out, prima = [], 0
    for t in token:
        crudo = sql[t.start:t.end + 1]
        if t.token_type != TokenType.BYTE_STRING or len(crudo) < 3 or crudo[:2] not in ("E'", "e'") or crudo[-1] != "'":
            continue
        valore = _valore_escape(crudo[2:-1])
        if valore is not None:
            out += [sql[prima:t.start], "'" + valore.replace("'", "''") + "'"]
            prima = t.end + 1
    return "".join(out) + sql[prima:]


def _riscrivi_query(query: str, ref: str, read: str = "duckdb", write: str = "duckdb") -> str:
    """Il nodo sql legge l'input come `self` o `input`: si puntano quei nomi alla
    CTE del passo precedente. La query resta una sottoquery, quindi può avere il
    suo WITH. `read`: the dialect it is written in; `write`: the one it is written out in."""
    import sqlglot
    from sqlglot import exp

    if read == "duckdb":
        query = _stringhe_standard(query)   # sqlglot would write its escape strings back wrong
    try:
        albero = sqlglot.parse_one(query, read=read)
    except Exception as e:  # noqa: BLE001
        raise DbtExportError(f"sql: the query cannot be parsed ({e})", "sql_unreadable", detail=str(e)[:200])
    cte = {c.alias_or_name.lower() for c in albero.find_all(exp.CTE)}
    for t in albero.find_all(exp.Table):
        nome = t.name.lower()
        if not t.args.get("db") and nome in ("self", "input") and nome not in cte:
            if not t.alias:
                t.set("alias", exp.TableAlias(this=exp.to_identifier(t.name)))
            t.set("this", exp.to_identifier(ref))
    try:
        return albero.sql(dialect=write)
    except Exception as e:  # noqa: BLE001
        raise DbtExportError(f"sql: the query cannot be written for {write} ({e})", "text_not_translatable",
                             step="custom_sql", dialect=read, detail=str(e)[:200])


# ── the flow's free text (compute expressions, sql nodes) in the dialect of its engine ──
_NOME_DIALETTO = {"polars": "Polars", "duckdb": "DuckDB", "clickhouse": "ClickHouse", "bigquery": "BigQuery"}
_DA_RIVEDERE = {
    "polars": "Written for the Polars engine, review",
    "clickhouse": "Translated from ClickHouse SQL by sqlglot, review",
    "bigquery": "Translated from BigQuery SQL by sqlglot, review",
}


def _a_capo(testo: str) -> str:
    """A text that may end with a `-- comment` on lines of its own: the comment would
    swallow what the export writes after it on the same line."""
    return f"\n{testo}\n" if "--" in testo else testo


def _tal_quale(model: "_Model") -> bool:
    """ClickHouse text on the ClickHouse target: written as the engine runs it."""
    return model.testo == "clickhouse" and model.native and model.dialetto == "clickhouse"


def _espressione(model: "_Model", expr: str, passo: str) -> str:
    """A compute expression, between parentheses, in the model's SQL (DuckDB, which the
    native targets translate). Text of a DuckDB flow: as it is. Of a ClickHouse or
    BigQuery flow: translated by sqlglot (on the ClickHouse target, ClickHouse's text as
    the ClickHouse engine runs it). Of a Polars flow: no sqlglot dialect, kept as it is
    and listed for review — CAST to an integer, ROUND, % and window frames are Polars'."""
    if _tal_quale(model):
        from app.engine.chdb_ops import _utf8_functions

        return model.testo_nativo(f"({_a_capo(_utf8_functions(expr))})", f"({_a_capo(_in_duckdb(expr, 'clickhouse') or expr)})")
    if model.testo in ("clickhouse", "bigquery"):
        tradotto = _in_duckdb(expr, model.testo)
        if tradotto is None:
            raise DbtExportError(f"{passo}: the expression, written for the {_NOME_DIALETTO[model.testo]} engine, cannot be translated",
                                 "text_not_translatable", step=passo, dialect=model.testo, detail=expr[:200])
        model.da_rivedere.append((passo, model.testo))
        return f"({_a_capo(tradotto)})"
    if model.testo == "polars":
        model.da_rivedere.append((passo, "polars"))
    return f"({_a_capo(expr)})"


def _in_duckdb(expr: str, dialetto: str) -> Optional[str]:
    import sqlglot

    try:
        return sqlglot.parse_one(expr, read=dialetto).sql(dialect="duckdb")
    except Exception:  # noqa: BLE001
        return None


def _gira_in_duckdb(model: "_Model", attiva: bool, passo: str) -> None:
    """Translated text DuckDB does not run (a ClickHouse function sqlglot does not know,
    like toYear) is refused here, with DuckDB's reason, instead of at the team's `dbt run`."""
    if model.testo not in ("clickhouse", "bigquery") or _tal_quale(model) or not attiva:
        return
    # text that reads a column of a type the export cannot name does not bind against the probe's
    # placeholder: that says nothing about the warehouse, where the column has its real type
    if not model.sonda.allinea(model) and model.sonda.errore and model.sonda.errore[0] == passo and not model.sonda.per_ignoto:
        raise DbtExportError(f"{passo}: the text, written for the {_NOME_DIALETTO[model.testo]} engine, does not run in DuckDB "
                             f"after the translation ({model.sonda.errore[1]})", "text_not_translatable",
                             step=passo, dialect=model.testo, detail=model.sonda.errore[1])


def _lista(cols: list[str], espr: Optional[dict] = None, nomi: Optional[dict] = None) -> str:
    """Una proiezione esplicita: `espressione AS "nome"` per ogni colonna, nell'ordine.
    Nel target nativo si scrive SEMPRE così, mai `*`: sqlglot, espandendo uno `*`,
    scrive gli alias senza virgolette e il dialetto di arrivo li porta in minuscolo
    («Città» → «città», un'altra colonna nel warehouse). Con la lista scritta da noi
    i nomi restano quelli del flusso."""
    espr, nomi = espr or {}, nomi or {}
    return ", ".join(f"{espr.get(c, _qi(c))} AS {_qi(nomi.get(c, c))}" for c in cols)


def _serve_lista(model: "_Model", s: "_S", cosa: str) -> bool:
    """Nel nativo serve la lista delle colonne; se non è nota (dopo un nodo sql) i
    modificatori di DuckDB (EXCLUDE/REPLACE/RENAME) non hanno equivalente."""
    if not model.native:
        return False
    if not s.cols:
        raise DbtExportError(
            f"{cosa}: the columns are not known at this point of the flow (after a sql node): "
            "the native export needs them. Use the «federated» export, or have the sql node list its columns.",
            "columns_unknown_after_sql", step=cosa,
        )
    return True


_NOME_INVENTATO = re.compile(r"_col_\d+")   # sqlglot's name for an expression without an alias


def _colonne_della_query(query: str, ref: str, s: "_S") -> list[str]:
    """Le colonne che un nodo sql restituisce, ricavate dalla query e dalle colonne
    del passo prima (`*` compreso). Servono ai passi successivi, e nel nativo alle
    liste esplicite. Se non si ricavano: [] (i passi dopo useranno `*`). An expression
    without an alias (`i + 1`, `count(*)`, an unexpanded COLUMNS) has a name sqlglot
    makes up (`_col_1`) that DuckDB does not give it: then [] too."""
    if not s.cols:
        return []
    nomi = _nomi_della_query(query, ref, s)
    return [] if any(_NOME_INVENTATO.fullmatch(n) for n in nomi) else nomi


def _con_i_nomi_di_duckdb(model: "_Model", query: str) -> str:
    """The query with every expression of its SELECT that has no alias named as DuckDB
    names it (`i + 1` → "(i + 1)", `count(*)` → "count_star()"): the probe reads the name
    of each one on its own (the query with only that expression), so a `*` next to them
    does not matter. If a name cannot be read, the query as it was."""
    import sqlglot
    from sqlglot import exp

    try:
        albero = sqlglot.parse_one(query, read="duckdb")
    except Exception:  # noqa: BLE001
        return query
    if not isinstance(albero, exp.Select):
        return query
    senza = [i for i, e in enumerate(albero.expressions)
             if not isinstance(e, (exp.Alias, exp.Column, exp.Star)) and not (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))]
    if not senza:
        return query
    for i in senza:
        una = albero.copy()
        una.set("expressions", [albero.expressions[i].copy()])
        letta = model.sonda.colonne(model, una.sql(dialect="duckdb"))
        if not letta or len(letta) != 1:
            return query
        albero.expressions[i].replace(exp.alias_(albero.expressions[i].copy(), letta[0][0], quoted=True))
    return albero.sql(dialect="duckdb")


def _nomi_della_query(query: str, ref: str, s: "_S") -> list[str]:
    if not s.cols:
        return []
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.schema import MappingSchema

    try:
        albero = qualify(sqlglot.parse_one(query, read="duckdb"),
                         schema=MappingSchema({ref: {c: "VARCHAR" for c in s.cols}}, dialect="postgres", normalize=False),
                         dialect="postgres", validate_qualify_columns=False)
        nomi = []
        for e in albero.selects:
            # uno `*` espanso da sqlglot ha un alias in minuscolo: il nome vero è quello della colonna
            if isinstance(e, exp.Alias) and isinstance(e.this, exp.Column) and e.alias.lower() == e.this.name.lower():
                nomi.append(e.this.name)
            else:
                nomi.append(e.alias_or_name)
        return nomi if all(nomi) and len(set(nomi)) == len(nomi) and "*" not in nomi else []
    except Exception:  # noqa: BLE001
        return []


def _re2(testo: str) -> str:
    """A name as a literal in a regular expression (RE2, DuckDB's): only its
    metacharacters escaped (RE2 refuses an escaped space or letter)."""
    return "".join("\\" + ch if ch in "\\.+*?()|[]{}^$" else ch for ch in str(testo))


_MARCA_DESTRA = "__tabularia_right"   # a right-side column, until it gets its name (see _join_dinamico)


def _join_dinamico(model: "_Model", ref: str, s: "_S", r_ref: str, rs: "_S", join_sql: str,
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
        sinistra = ", ".join(f"COALESCE({ref}.{q(c)}, {r_ref}.{q(c)}) AS {q(c)}" if c in coalesce_keys else f"{ref}.{q(c)}"
                             for c in s.cols)
    else:
        sinistra = f"{ref}.*" + (" REPLACE (" + ", ".join(f"COALESCE({ref}.{q(k)}, {r_ref}.{q(k)}) AS {q(k)}" for k in sorted(coalesce_keys)) + ")"
                                 if coalesce_keys else "")
    if rs.cols:
        destre = [c for c in rs.cols if c not in skip_right]
        destra = ", ".join(f"{r_ref}.{q(c)} AS {q(c + _MARCA_DESTRA)}" for c in destre)
    else:
        escluse = f" EXCLUDE ({', '.join(q(k) for k in sorted(skip_right))})" if skip_right else ""
        destra = f"COLUMNS({r_ref}.*{escluse}) AS '\\0{_MARCA_DESTRA}'"
    if not destra:
        return model.add(f"SELECT {sinistra} {join_sql}"), _S([])
    unito = model.add(f"SELECT {sinistra}, {destra} {join_sql}")
    a_sinistra = f"COLUMNS(c -> NOT ends_with(c, '{_MARCA_DESTRA}'))"
    marca = _MARCA_DESTRA[: -len("_right")]
    # the left side's names known here: all of them, or (its columns unknown) the pivot's index and
    # the columns made or renamed after it, which a join back on the index meets
    noti = s.cols or list(s.types)
    if noti:
        alternative = "|".join(_re2(c).replace("'", "''") for c in noti)
        nomi = f"COLUMNS('(?s)^({alternative}){marca}(_right)$|^(.*){_MARCA_DESTRA}$') AS '\\1\\2\\3'"
    else:
        nomi = f"COLUMNS('(?s)^(.*){_MARCA_DESTRA}$') AS '\\1'"
    lista = f"SELECT {a_sinistra}, {nomi} FROM {unito}"
    if s.cols:
        return model.add(lista, nota="the right side's columns get their names: _right on those the left side also has"), _S([])
    return model.add(f"({lista}) UNION ALL BY NAME ({lista} LIMIT 0)",
                     nota="the right side's columns get their names: _right on those the left side is known to have; the run "
                          "stops if one is also a column the pivot made (Tabularia would add _right)"), _S([])


# ── le operazioni ────────────────────────────────────────────────────────────
def _primo_ultimo(model: "_Model", func: str, col: str, ordine: Optional[list], tipo_di: Optional[Callable] = None) -> str:
    """first/last as the engine (Polars) gives them: the first/last row of the group in the
    order of the sort before it — DuckDB's parallel aggregate does not keep the order of the
    step before, so the order is written in the aggregate. Without a sort, any row (NULL
    included). Postgres has no first/last: the first/last element of the ordered
    array_agg. ClickHouse has neither ordered aggregates nor arrays that keep NULL: the
    (keys, value) tuples of the group, sorted with NULL keys last; without a sort,
    any/anyLast as the ClickHouse engine."""
    q = _qi
    chiave = lambda k: _byte(model, q(k), tipo_di(k)) if tipo_di else q(k)   # noqa: E731
    chiavi = ", ".join(f"{chiave(k)} {'DESC' if desc else 'ASC'} NULLS LAST" for k, desc in ordine) if ordine else ""
    duck = f"{func}({q(col)}{' ORDER BY ' + chiavi if chiavi else ''})"
    if not model.native:
        return duck
    if model.dialetto == "postgres":
        lista = f"(array_agg({q(col)}{' ORDER BY ' + chiavi if chiavi else ''}))"
        return f"{lista}[1]" if func == "first" else f"{lista}[count(*)]"
    if model.dialetto == "clickhouse":
        if not ordine:
            return model.testo_nativo(f"{'any' if func == 'first' else 'anyLast'}({q(col)})", duck)
        n = len(ordine)
        elem = lambda i: f"tupleElement(t, {i})"   # noqa: E731
        if ordine[0][1]:   # every key has the sort's one direction
            chiave = ", ".join(f"NOT isNull({elem(i + 1)}), {elem(i + 1)}" for i in range(n))
            ordinato = f"arrayReverseSort(t -> ({chiave}), groupArray(tuple({', '.join(q(k) for k, _ in ordine)}, {q(col)})))"
        else:
            chiave = ", ".join(f"isNull({elem(i + 1)}), {elem(i + 1)}" for i in range(n))
            ordinato = f"arraySort(t -> ({chiave}), groupArray(tuple({', '.join(q(k) for k, _ in ordine)}, {q(col)})))"
        return model.testo_nativo(f"tupleElement({ordinato}[{'1' if func == 'first' else '-1'}], {n + 1})", duck)
    raise DbtExportError(f"group_by: «{func}» has no equivalent in the {model.dialetto} dialect", "groupby_unsupported_func", func=func)


def _media_di_istanti(model: "_Model", func: str, c: str, duck: str) -> str:
    """The mean or the median of dates or instants, an instant as the engines give it (Postgres and
    ClickHouse have neither avg nor median of a date: the model failed). The mean is computed over
    the microseconds since 1970 as an exact number, rounded to the microsecond; the median is the
    midpoint of the two middle values. `duck`: DuckDB's own form, for the probe."""
    if model.dialetto == "postgres":
        istante = f"CAST({c} AS TIMESTAMP)"
        if func == "median":
            basso = f"percentile_disc(0.5) WITHIN GROUP (ORDER BY {istante})"
            alto = f"percentile_disc(0.5) WITHIN GROUP (ORDER BY {istante} DESC)"
            return model.testo_nativo(f"({basso} + ({alto} - {basso}) / 2)", duck)
        us, giorno = f"avg(extract(epoch FROM {istante}) * 1000000)", "86400000000"
        # whole days and the microseconds left, each exact as a double (the whole span would not be)
        return model.testo_nativo(f"(TIMESTAMP '1970-01-01 00:00:00' + CAST(floor({us} / {giorno}) AS DOUBLE PRECISION) * INTERVAL '1 day' "
                                  f"+ CAST(round({us} - floor({us} / {giorno}) * {giorno}) AS DOUBLE PRECISION) * INTERVAL '1 microsecond')", duck)
    if model.dialetto == "clickhouse":
        us = f"toUnixTimestamp64Micro(toDateTime64({c}, 6))"
        if func == "median":
            return model.testo_nativo(f"fromUnixTimestamp64Micro(toInt64(round(quantileExactInclusive(0.5)({us}))))", duck)
        # an exact sum (avg of a decimal is a Float64 on ClickHouse); a group of NULLs divides NULL by 1
        return model.testo_nativo(f"fromUnixTimestamp64Micro(toInt64(round(sum(toDecimal128({us}, 6)) / greatest(count({c}), 1))))", duck)
    return duck


def _aggregato(model: "_Model", func: str, col: str, tipo: Optional[str], ordine: Optional[list] = None,
               tipo_di: Optional[Callable] = None) -> tuple[str, Optional[str]]:
    """An aggregate as the engine computes it, and the type it gives (None: not known).
    The engine brings a HUGEINT back to BIGINT (a sum of integers or of booleans, which
    count the true ones); a sum of DECIMAL(p,s) is DECIMAL(38,s); a mean or a median of
    dates is a timestamp. Those types are what a model reading this one is given."""
    q = _qi
    fam = _famiglia(tipo)
    if func in ("first", "last"):
        return _primo_ultimo(model, func, col, ordine, tipo_di), tipo
    expr = f"count(DISTINCT {q(col)})" if func == "n_unique" else f"{_AGG[func]}({q(col)})"
    if func in ("mean", "median") and (fam == "date" or fam.startswith("timestamp")):
        return (_media_di_istanti(model, func, q(col), expr) if model.native else expr), "TIMESTAMP"
    if func in ("mean", "std", "var") and fam == "boolean":
        # a boolean counts as 1 and 0, as in Polars: DuckDB and Postgres have no avg of a boolean
        expr = f"{_AGG[func]}(CAST({q(col)} AS INTEGER))"
    if func in ("min", "max"):
        expr = f"{_AGG[func]}({_byte(model, q(col), tipo)})"     # text: by bytes
    if func in ("std", "var") and model.dialetto == "clickhouse":
        # ClickHouse dà NaN con un solo valore: NULL, come il motore (e come DuckDB/Postgres)
        expr = f"CASE WHEN count({q(col)}) > 1 THEN {expr} END"
    if func in ("mean", "std", "var") and model.native and model.dialetto == "postgres" and fam in ("intero", "numero", "boolean"):
        # Postgres gives a numeric for an integer or a decimal ('1.5000000000000000' as text): Tabularia's double
        expr = f"CAST({expr} AS DOUBLE)"
    if func in ("min", "max") and fam == "boolean" and model.dialetto == "postgres":
        # Postgres has no min/max of a boolean: false < true, so they are bool_and/bool_or
        return f"{'bool_and' if func == 'min' else 'bool_or'}({q(col)})", tipo
    if func in ("count", "n_unique"):
        return expr, "BIGINT"
    if func == "sum" and fam in ("intero", "boolean"):
        # come il motore: la somma di interi torna BIGINT (DuckDB darebbe HUGEINT); a boolean is
        # summed as an integer, which every warehouse can (Postgres has no sum(boolean))
        arg = f"CAST({q(col)} AS INTEGER)" if fam == "boolean" else q(col)
        return f"CAST(sum({arg}) AS BIGINT)", "BIGINT"
    if func == "sum":
        m = re.fullmatch(r"DECIMAL\((\d+),\s*(\d+)\)", str(tipo or "").upper())
        return expr, (f"DECIMAL(38,{m.group(2)})" if m else ("DOUBLE" if fam == "numero" else None))
    if func in ("mean", "std", "var"):
        return expr, "DOUBLE"
    if func == "median":
        return expr, (tipo if str(tipo or "").upper().startswith("DECIMAL") else "DOUBLE")
    return expr, tipo


def _compile_op(model: _Model, ref: str, s: _S, op_type: str, params: dict,
                resolve: SourceResolver, right_namer: _Namer) -> tuple[str, _S]:
    """Emette la/le CTE di una operazione. Ritorna (nuovo ref, nuovo schema)."""
    q, nat = _qi, model.native
    cols, types = s.cols, s.types

    # nel nativo, i passi che non cambiano le colonne le elencano invece di `*`
    tutte = _lista(cols) if (nat and cols) else "*"

    if op_type == "select":
        c = _require(params, "columns")
        sel = _lista(c) if nat else ", ".join(q(x) for x in c)
        return model.add(f"SELECT {sel} FROM {ref}"), _S(c, {x: s.tipo(x) for x in c})

    if op_type == "reorder":
        order = _require(params, "columns")
        nuove = list(order) + [c for c in cols if c not in set(order)]
        if _serve_lista(model, s, "reorder"):
            return model.add(f"SELECT {_lista(nuove)} FROM {ref}"), _S(nuove, types)
        qs = ", ".join(q(x) for x in order)
        # with the columns not known, the listed ones are only the first of them
        dopo = _S(nuove, types) if cols else _S([], types, s.altre)
        return model.add(f"SELECT {qs}, * EXCLUDE ({qs}) FROM {ref}"), dopo

    if op_type == "drop":
        # the node's order, once each: a set's order changes with the process (PYTHONHASHSEED),
        # and with it the file — the same flow must give the same zip on every engine
        drop = list(dict.fromkeys(_require(params, "columns")))
        restano = [x for x in cols if x not in set(drop)]
        if _serve_lista(model, s, "drop"):
            return model.add(f"SELECT {_lista(restano)} FROM {ref}"), _S(restano, types)
        sql = f"SELECT * EXCLUDE ({', '.join(q(x) for x in drop)}) FROM {ref}"
        # with the columns not known, `types` lists the ones known to be there: not the dropped ones
        return model.add(sql), _S(restano, {c: t for c, t in types.items() if c not in set(drop)}, s.altre)

    if op_type == "rename":
        mapping = _require(params, "mapping")
        nuove = [mapping.get(x, x) for x in cols]
        nt = {mapping.get(k, k): v for k, v in types.items()}
        if _serve_lista(model, s, "rename"):
            return model.add(f"SELECT {_lista(cols, nomi=mapping)} FROM {ref}"), _S(nuove, nt)
        pairs = ", ".join(f"{q(o)} AS {q(n)}" for o, n in mapping.items())
        return model.add(f"SELECT * RENAME ({pairs}) FROM {ref}"), _S(nuove, nt, s.altre)

    if op_type == "cast":
        casts = _require(params, "columns")
        repls, nuovi = [], dict(types)
        for col, dt in casts.items():
            duck_t = _DUCK_DTYPES.get(str(dt))
            if not duck_t:
                raise DbtExportError(f"cast: unsupported type '{dt}'", "cast_unsupported_type", type=dt)
            src = s.tipo(col)
            if not src:
                # the engine casts from the column's real type (trunc for a number, trim and the
                # integer guard for a text): without it, a bare cast would round or accept '12.7'
                raise DbtExportError(f"cast: the type of the column «{col}» is not known at this point of the flow, "
                                     "so the cast cannot be written as Tabularia does it", "cast_type_unknown", column=col)
            espr = _cast_portabile(q(col), src, duck_t, model.dialetto, model) if nat else _cast_expr(q(col), src, duck_t)
            repls.append((col, espr))
            nuovi[col] = duck_t
        if _serve_lista(model, s, "cast"):
            return model.add(f"SELECT {_lista(cols, espr=dict(repls))} FROM {ref}"), _S(cols, nuovi)
        return model.add(f"SELECT * REPLACE ({', '.join(f'{e} AS {q(c)}' for c, e in repls)}) FROM {ref}"), _S(cols, nuovi, s.altre)

    if op_type == "filter":
        col, operator, value = _require(params, "column"), params.get("operator", "eq"), params.get("value")
        try:
            # the engines' own check: compared with NULL the filter kept no row, without a word
            check_filter_value(operator, value)
        except EngineError as e:
            raise DbtExportError(f"filter on «{col}»: {e}", "filter_value_missing", column=col, operator=operator) from e
        pred = _predicato_portabile(col, operator, value, _per_byte(model, s.tipo(col))) if nat else _predicate(col, operator, value)
        return model.add(f"SELECT {tutte} FROM {ref} WHERE {pred}"), s

    if op_type == "sort":
        by = _as_list(_require(params, "by"))
        if params.get("ignore_missing") and cols:
            by = [c for c in by if c in set(cols)]
            if not by:
                return ref, s
        direction = "DESC" if params.get("descending") else "ASC"
        # standard cross-engine: NULL sempre in coda (come il motore)
        order = ", ".join(f"{_byte(model, q(c), s.tipo(c))} {direction} NULLS LAST" for c in by)
        return model.add(f"SELECT {tutte} FROM {ref} ORDER BY {order}"), s

    if op_type == "limit":
        return model.add(f"SELECT {tutte} FROM {ref} LIMIT {int(_require(params, 'n'))}"), s

    if op_type == "unique":
        subset = params.get("subset")
        if subset:
            part = ", ".join(q(c) for c in subset)
            return model.add(f"SELECT {tutte} FROM {ref} QUALIFY row_number() OVER (PARTITION BY {part}) = 1"), s
        return model.add(f"SELECT DISTINCT {tutte} FROM {ref}"), s

    if op_type == "fill_null":
        fills = _require(params, "columns")
        if _serve_lista(model, s, "fill_null"):
            return model.add(f"SELECT {_lista(cols, espr={c: f'COALESCE({q(c)}, {_lit(v)})' for c, v in fills.items()})} FROM {ref}"), s
        repl = ", ".join(f"COALESCE({q(c)}, {_lit(v)}) AS {q(c)}" for c, v in fills.items())
        return model.add(f"SELECT * REPLACE ({repl}) FROM {ref}"), s

    if op_type == "drop_nulls":
        subset = params.get("subset") or cols
        if not subset:
            raise DbtExportError("drop_nulls: the columns are not known at this point of the flow (after a sql node or a pivot): list them in the node", "drop_nulls_columns_unknown")
        cond = " AND ".join(f"{q(c)} IS NOT NULL" for c in subset)
        return model.add(f"SELECT {tutte} FROM {ref} WHERE {cond}"), s

    if op_type == "group_by":
        by = _as_list(_require(params, "by"))
        aggs = _require(params, "aggregations")
        parts, out_cols, out_t = [", ".join(q(c) for c in by)], list(by), {c: s.tipo(c) for c in by}
        for agg in aggs:
            col, func = agg["column"], agg.get("func", "sum")
            if func not in _AGG:
                raise DbtExportError(f"group_by: unsupported function '{func}'", "groupby_unsupported_func", func=func)
            alias = agg.get("alias") or f"{col}_{func}"
            expr, t = _aggregato(model, func, col, s.tipo(col), s.ordine, s.tipo)
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
            # niente controllo «solo espressioni scalari» qui: nel motore protegge il
            # NOSTRO processo (l'espressione gira dentro Tabularia); il progetto dbt
            # gira nell'ambiente di chi lo esporta, e deve riprodurre il flusso com'è
            # (quel controllo, a parole, rifiutava anche EXTRACT(YEAR FROM …))
            if nat and not out_cols:
                _serve_lista(model, _S([]), "compute")
            passo = dict(nome=f"compute_{name}", nota=f"{name} = {expr}" + (" (replaces the column, in place)" if name in out_cols else ""))
            attiva = model.sonda.attiva
            nome_passo = model._prossimo(passo["nome"])
            espr = _espressione(model, expr, nome_passo)
            if not out_cols:
                # the columns are not known (after a pivot): the column is replaced where it is if
                # it exists, added at the end otherwise — like the engine, which sees the real ones
                riga = "__tabularia_row"
                cur = model.add(f"SELECT unnest(struct_update({riga}, {q(name)} := {espr})) FROM {cur} AS {riga}", **passo)
            elif name in out_cols:
                # colonna esistente: sovrascritta NELLA SUA POSIZIONE (come il motore)
                if nat:
                    cur = model.add(f"SELECT {_lista(out_cols, espr={name: espr})} FROM {cur}", **passo)
                else:
                    cur = model.add(f"SELECT * REPLACE ({espr} AS {q(name)}) FROM {cur}", **passo)
            else:
                cur = model.add(f"SELECT {_lista(out_cols) if nat else '*'}, {espr} AS {q(name)} FROM {cur}", **passo)
                out_cols.append(name)
            _gira_in_duckdb(model, attiva, cur)
            out_t[name] = None
        return cur, _S(out_cols, out_t, s.altre)

    if op_type == "sql":
        testo = str(_require(params, "query")).strip()
        nome_passo = model._prossimo(model.etichette[-1][0] if model.etichette else "custom_sql")
        if _tal_quale(model):
            # ClickHouse's query on the ClickHouse target, as the engine runs it (only `self` pointed
            # at the step before); its names are ClickHouse's: known only where it gives them
            finale = _riscrivi_query(testo, ref, read="clickhouse", write="clickhouse")
            try:
                query = _riscrivi_query(testo, ref, read="clickhouse")
            except DbtExportError:
                query = finale
            uscita = _colonne_della_query(query, ref, s)
            sorgente = model.testo_nativo(f"(\n{finale}\n)", f"({query})")
            if uscita:
                return model.add(f"SELECT {_lista(uscita)} FROM {sorgente} AS _q"), _S(uscita)
            model.sonda.spegni()
            return model.add(f"SELECT * FROM {sorgente} AS _q"), _S([])
        query = _riscrivi_query(testo, ref, read="duckdb" if model.testo in ("duckdb", "polars") else model.testo)
        if model.testo != "duckdb":
            model.da_rivedere.append((nome_passo, model.testo))
        if nat:
            # the warehouse would name an expression without an alias its own way (Postgres:
            # '?column?') and sqlglot makes one up (`_col_1`): it gets the name DuckDB gives it,
            # as in the federated target and in the DuckDB engine
            query = _con_i_nomi_di_duckdb(model, query)
        uscita = _colonne_della_query(query, ref, s)
        # federated: the names of an expression without an alias are DuckDB's, the probe reads them
        # after the step; native: the list, when it is known
        attiva = model.sonda.attiva
        cur = model.add(f"SELECT {_lista(uscita) if (nat and uscita) else '*'} FROM ({query}) AS _q")
        _gira_in_duckdb(model, attiva, cur)
        if nat and not uscita:
            model.sonda.spegni()   # the warehouse names these columns itself: the probe's names are not its
        return cur, _S(uscita)

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
                    return model.add(f"SELECT {_lista(cols)} FROM {ref} UNION ALL SELECT {_lista(rs.cols, nomi=dict(zip(rs.cols, cols)))} FROM {r_ref}"), _S(cols, mt)
                return model.add(f"SELECT * FROM {ref} UNION ALL SELECT * FROM {r_ref}"), _S(cols, mt)
            if not nat:
                # by name: with one side's columns not known, neither are the result's
                return model.add(f"SELECT * FROM {ref} UNION ALL BY NAME SELECT * FROM {r_ref}"), (_S(merged, mt) if cols and rs.cols else _S([]))
            if not cols or not rs.cols:
                raise DbtExportError("union by name: the columns of one of the two branches are not known (after a sql node or a pivot): the native export needs them",
                                     "union_columns_unknown")
            lato = lambda have: ", ".join(q(c) if c in set(have) else f"NULL AS {q(c)}" for c in merged)
            return model.add(f"SELECT {lato(cols)} FROM {ref} UNION ALL SELECT {lato(rs.cols)} FROM {r_ref}"), _S(merged, mt)
        how = params.get("how", "inner")
        lset = set(cols)
        if nat and how not in ("semi", "anti"):
            _serve_lista(model, s, "join")
            _serve_lista(model, rs, "join")
        dinamico = not nat and (not cols or not rs.cols) and how not in ("semi", "anti")
        if how == "cross":
            if dinamico:
                return _join_dinamico(model, ref, s, r_ref, rs, f"FROM {ref} CROSS JOIN {r_ref}", set(), set())
            sel = _join_select(ref, cols, r_ref, rs.cols, set())
            out = cols + [c if c not in lset else c + "_right" for c in rs.cols]
            ot = {**types, **{(c if c not in lset else c + "_right"): rs.types.get(c) for c in rs.cols}}
            return model.add(f"SELECT {sel} FROM {ref} CROSS JOIN {r_ref}"), _S(out, ot)
        clause, kind = _join_condition(ref, r_ref, params)
        if how in ("semi", "anti"):
            sel = ", ".join(f"{ref}.{q(c)} AS {q(c)}" for c in cols) if (nat and cols) else f"{ref}.*"
            if nat and model.dialetto == "clickhouse":
                # ClickHouse non ha EXISTS correlato; ha LEFT SEMI/ANTI JOIN, gli stessi del motore
                return model.add(f"SELECT {sel} FROM {ref} LEFT {how.upper()} JOIN {r_ref} {clause}", sonda=f"SELECT * FROM {ref}"), s
            if nat:
                # SEMI/ANTI JOIN non esistono negli altri warehouse: EXISTS / NOT EXISTS
                cond = clause[len("ON "):] if clause.startswith("ON ") else " AND ".join(
                    f"{ref}.{q(k)} = {r_ref}.{q(k)}" for k in _as_list(params["on"]))
                neg = "NOT " if how == "anti" else ""
                return model.add(f"SELECT {sel} FROM {ref} WHERE {neg}EXISTS (SELECT 1 FROM {r_ref} WHERE {cond})"), s
            return model.add(f"SELECT {sel} FROM {ref} {how.upper()} JOIN {r_ref} {clause}"), s
        if how not in _JOIN_KW:
            raise DbtExportError(f"join: unsupported type '{how}'", "join_unsupported", how=how)
        skip_right = set(_as_list(params["on"])) if kind == "using" else set()
        # nei right/full join le chiavi USING sono COALESCE(sinistra, destra), come nel motore
        if dinamico:
            return _join_dinamico(model, ref, s, r_ref, rs, f"FROM {ref} {_JOIN_KW[how]} {r_ref} {clause}", skip_right,
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
        # etichetta standard cross-engine, come il motore: valore come testo, NULL → 'null',
        # più colonne unite dal separatore
        on_sql = f" || '{PIVOT_LABEL_SEP}' || ".join(f"coalesce(CAST({q(c)} AS VARCHAR), 'null')" for c in on)
        idx_sql = ", ".join(q(c) for c in index)
        usando = _primo_ultimo(model, func, values, s.ordine) if func in ("first", "last") else f"{_AGG[func]}({q(values)})"
        if func == "sum" and _famiglia(s.tipo(values)) in ("intero", "boolean"):
            usando = f"CAST({usando} AS BIGINT)"      # come il motore: HUGEINT riportato a BIGINT
        # the new columns are not known (the data decides them), their type is: the aggregate's
        return (model.add(f"PIVOT {ref} ON {on_sql} USING {usando} GROUP BY {idx_sql}"),
                _S([], {c: s.tipo(c) for c in index}, _aggregato(model, func, values, s.tipo(values))[1]))

    if op_type == "unpivot":
        on = params.get("on")
        index = params.get("index") or []
        var = params.get("variable_name") or "variable"
        val = params.get("value_name") or "value"
        melt = on if on else [c for c in cols if c not in set(index)]
        if not melt:
            raise DbtExportError("unpivot: no columns to turn into rows", "unpivot_nothing")
        tipi = [s.tipo(c) for c in melt]
        # the engine's supertype of the melted columns; with a type not known, the union of the
        # SELECTs below finds it (never text by default: a number stays a number)
        vtype = unpivot_value_type([t for t in tipi]) if all(tipi) else None
        keep = [c for c in cols if c in set(index)] if cols else list(index)
        # forma portabile, identica nei due target: una SELECT per colonna sciolta, NULL inclusi
        # (come il motore, che usa UNPIVOT INCLUDE NULLS)
        valore = (lambda m: f"CAST({q(m)} AS {vtype})") if vtype else (lambda m: q(m))
        pezzi = [
            "SELECT " + ", ".join([q(c) for c in keep] + [f"{_lit(m)} AS {q(var)}", f"{valore(m)} AS {q(val)}"]) + f" FROM {ref}"
            for m in melt
        ]
        out = keep + [var, val]
        return model.add(" UNION ALL ".join(pezzi)), _S(out, {**{c: s.tipo(c) for c in keep}, var: "VARCHAR", val: vtype})

    if op_type == "foreach":
        raise DbtExportError(
            "The «foreach» node iterates at runtime over a driver table and has no "
            "dbt equivalent: leave it out of the flow or export the branches separately.", "foreach_not_exportable"
        )

    raise DbtExportError(f"operation '{op_type}' cannot be exported to dbt", "op_not_exportable", op=op_type)


def _risolvi(resolve: SourceResolver, spec: dict) -> tuple[str, list[str], dict, str]:
    """(ref, colonne, tipi, nome leggibile). Il nome viene dal resolver se lo dà,
    altrimenti dal riferimento dbt stesso (`source('…', 'ordini')` → ordini)."""
    r = resolve(spec)
    ref, cols, tipi = r[0], r[1], r[2]
    nome = (r[3] if len(r) > 3 and r[3] else None) or ref_label(ref) or spec.get("model") or spec.get("seed") or "source"
    return ref, cols, tipi, nome


def ref_label(ref: str) -> Optional[str]:
    """The table or model a dbt reference names: `source('…', 'ordini')` → ordini."""
    m = re.search(r"source\('[^']*', *'([^']*)'\)|ref\('([^']*)'\)", ref or "")
    return (m.group(1) or m.group(2)) if m else None


def _base(model: _Model, src_ref: str, s: _S, nome: str = "source", destra: bool = False, tipizza: bool = True) -> tuple[str, _S]:  # noqa: C901
    """Il primo passo di un modello: la sorgente con i TIPI che Tabularia le ha dato
    all'import, così il progetto vede i dati come il flusso (es. un numeric del
    warehouse che Tabularia legge come testo). Another model of the project
    (`tipizza=False`) is read as it is: its types are the ones its own SQL gives, and a
    second cast to recorded types could only be wrong. A column of a type the export cannot
    write (an array, a uuid, bytea) is passed through as it is, and only it: it used to switch
    off the casts of all the others. Returns the step and the schema after it."""
    noti = bool(s.cols) and all(s.types.get(c) for c in s.cols)
    tipizzate = tipizza and any(s.types.get(c) for c in s.cols)
    passo = dict(nome=f"{'right_' if destra else ''}source_{nome}",
                 nota=f"{'right side, ' if destra else ''}source: {nome}" + (", with the column types Tabularia reads" if tipizzate else "")
                 + ("" if noti or not tipizzate else " (the columns of other types as they are)")
                 + (", and the texts the seed writes as `~…` read back" if tipizza and SEED_TESTO in s.types.values() else ""))
    if tipizzate:
        sql = f"SELECT {_proiezione_tipizzata(model.dialetto, s.cols, s.types)} FROM {src_ref}"
    elif model.native and s.cols:
        sql = f"SELECT {_lista(s.cols)} FROM {src_ref}"
    else:
        sql = f"SELECT * FROM {src_ref}"
    dopo = _S(s.cols, {c: _tipo_tipizzato(t) for c, t in s.types.items()}) if s.cols else s
    _rifiuta_maiuscole(model, s, passo["nome"])
    _rifiuta_lunghi(model, s, passo["nome"])
    # the probe reads an empty table with those types, never the source; a column of a type the export
    # cannot name is a struct no warehouse type is: passed on, it stays not known (a struct is not a
    # type the probe reports), and a step that reads it as anything else stops the probe there. It
    # used to stop the probe for the whole model, and with it the casts of the other columns
    vuota = "SELECT " + ", ".join(f"CAST(NULL AS {dopo.types.get(c) or _TIPO_IGNOTO}) AS {_qi(c)}" for c in s.cols) + " LIMIT 0" \
        if s.cols and not _uguali_ma_per_maiuscole(s.cols) else None
    if vuota is None:
        model.sonda.spegni()
    return model.add(sql, sonda=vuota, **passo), dopo


def _compile_right(model: _Model, ref_spec: dict, resolve: SourceResolver, right_namer: _Namer):
    """Compila il lato destro di un join/union (sotto-flow {source, operations}
    o sorgente semplice) come CTE aggiuntive del modello."""
    spec = ref_spec["source"] if "source" in ref_spec else ref_spec
    src_ref, src_cols, src_types, nome = _risolvi(resolve, spec)
    base, s = _base(model, src_ref, _S(src_cols, src_types), nome, destra=True, tipizza="model" not in spec)
    return _compile_ops(model, base, s, ref_spec.get("operations") or [] if "source" in ref_spec else [], resolve, right_namer)


def _v(value: Any) -> str:
    return ", ".join(_v(x) for x in value) if isinstance(value, list) else (_lit(value) if not isinstance(value, str) else f"'{value}'")


def _etichetta(op_type: str, params: dict, resolve: SourceResolver, cols: Optional[list] = None) -> tuple[str, str]:
    """Nome e nota di un passo, ricavati da ciò che fa."""
    p = params or {}
    col = p.get("column")
    by = _as_list(p.get("by")) if p.get("by") is not None else []
    if op_type == "sort" and p.get("ignore_missing") and cols:
        by = [c for c in by if c in set(cols)]   # come il passo: le chiavi sparite non contano
    if op_type == "filter":
        ops = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<=", "in": "in", "not_in": "not in",
               "between": "between", "contains": "contains", "starts_with": "starts with", "ends_with": "ends with",
               "is_null": "is null", "is_not_null": "is not null"}
        op = p.get("operator", "eq")
        valore = "" if op in ("is_null", "is_not_null") else f" {_v(p.get('value'))}"
        return f"filter_{col}", f"keep rows where {col} {ops.get(op, op)}{valore}"
    if op_type == "sort":
        return f"sort_by_{'_'.join(by[:2])}", f"sort by {', '.join(by)} {'descending' if p.get('descending') else 'ascending'} (nulls last)"
    if op_type == "group_by":
        aggs = ", ".join(f"{a.get('func', 'sum')}({a.get('column')}) as {a.get('alias') or a.get('column') + '_' + a.get('func', 'sum')}" for a in (p.get("aggregations") or []))
        return f"aggregate_by_{'_'.join(by[:2])}", f"aggregate by {', '.join(by)}: {aggs}"
    if op_type in ("join", "union"):
        destra = p.get("right") or {}
        try:
            nome = _risolvi(resolve, destra["source"] if "source" in destra else destra)[3]
        except Exception:  # noqa: BLE001
            nome = "right"
        if op_type == "union":
            return f"union_{nome}", f"append the rows of {nome} ({'same columns, in order' if p.get('strategy') == 'strict' else 'matched by column name'})"
        how = p.get("how", "inner")
        chiavi = ", ".join(_as_list(p["on"])) if p.get("on") else " and ".join(f"{a} = {b}" for a, b in zip(_as_list(p.get("left_on") or []), _as_list(p.get("right_on") or [])))
        return f"{how}_join_{nome}", f"{how} join with {nome}" + (f" on {chiavi}" if chiavi else "")
    semplici = {
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
    if op_type in semplici:
        nome, nota = semplici[op_type]
        return nome, nota()
    return op_type, op_type


# a node saved half done (a join without keys or without its right input, a filter
# without a column…): the engines refuse it too; the export says which step, with a code
_PASSO_INCOMPLETO = (EngineError, KeyError, TypeError, ValueError, AttributeError, IndexError)


def _forma_sbagliata(op_type: str, p: dict) -> bool:
    """Whether a node's parameters lack the shape the editor writes (a mapping that is not one, an
    aggregation without a column, a limit that is not a number…): then an error compiling it is the
    node's. False: they have it, and the error is the exporter's own."""
    def lista_di(v, *tipi) -> bool:
        return isinstance(v, list) and all(isinstance(x, tipi) and x != "" for x in v)

    def intero(v) -> bool:
        try:
            int(v)
            return not isinstance(v, bool)
        except (TypeError, ValueError):
            return False
    nomi = lambda v: isinstance(v, str) and v != "" or lista_di(v, str) and bool(v)   # noqa: E731
    forme: dict[str, Callable[[], bool]] = {
        "select": lambda: lista_di(p.get("columns"), str), "reorder": lambda: lista_di(p.get("columns"), str),
        "drop": lambda: lista_di(p.get("columns"), str), "rename": lambda: isinstance(p.get("mapping"), dict),
        "cast": lambda: isinstance(p.get("columns"), dict), "fill_null": lambda: isinstance(p.get("columns"), dict),
        "filter": lambda: isinstance(p.get("column"), str), "sort": lambda: nomi(p.get("by")), "limit": lambda: intero(p.get("n")),
        "unique": lambda: not p.get("subset") or lista_di(p.get("subset"), str),
        "drop_nulls": lambda: not p.get("subset") or lista_di(p.get("subset"), str),
        "group_by": lambda: nomi(p.get("by")) and lista_di(p.get("aggregations"), dict)
        and all(isinstance(a.get("column"), str) and a.get("column") for a in p["aggregations"]),
        "compute": lambda: lista_di(p.get("columns"), dict), "sql": lambda: isinstance(p.get("query"), str),
        "join": lambda: isinstance(p.get("right"), dict), "union": lambda: isinstance(p.get("right"), dict),
        "pivot": lambda: nomi(p.get("index")) and nomi(p.get("on")) and isinstance(p.get("values"), str),
        "unpivot": lambda: not p.get("on") or lista_di(p.get("on"), str),
    }
    forma = forme.get(op_type)
    return forma is None or not forma()


def _compile_ops(model, ref, s, operations, resolve, right_namer):
    for op in operations:
        op_type = op.get("type") if isinstance(op, dict) else op.type
        params = (op.get("params") if isinstance(op, dict) else op.params) or {}
        try:
            model.etichette.append(_etichetta(op_type, params, resolve, s.cols))
            try:
                ref, dopo = _compile_op(model, ref, s, op_type, params, resolve, right_namer)
            finally:
                model.etichette.pop()
        except DbtExportError:
            raise
        except _PASSO_INCOMPLETO as e:
            if not isinstance(e, EngineError):
                logger.warning("dbt export: the step %s cannot be compiled", op_type, exc_info=True)
                if not _forma_sbagliata(str(op_type), params if isinstance(params, dict) else {}):
                    # a node of the right shape: the error is the exporter's, not the flow's
                    raise DbtExportError(f"{op_type}: the export cannot compile the step ({type(e).__name__}: {e})", "internal",
                                         detail=f"{op_type}: {type(e).__name__}: {e}"[:200]) from e
            raise DbtExportError(f"{op_type}: the step is incomplete or not valid ({e})",
                                 "step_invalid", step=str(op_type), detail=str(e)[:200]) from e
        _rifiuta_maiuscole(model, dopo, str(op_type))   # the names the step gives, before DuckDB renames one
        _rifiuta_lunghi(model, dopo, str(op_type))
        dopo = model.sonda.dopo(model, ref, dopo, op_type)
        dopo.ordine = _ordine_dopo(op_type, params, s)
        s = dopo
    return ref, s


def _ordine_dopo(op_type: str, params: dict, prima: _S) -> Optional[list[tuple[str, bool]]]:
    """The sort a first/last after this step must follow: set by a sort, kept by the steps
    that keep the rows' order and the keys' values, lost by every other."""
    p, ordine = params or {}, prima.ordine
    if op_type == "sort":
        by = _as_list(p.get("by") or [])
        if p.get("ignore_missing") and prima.cols:
            by = [c for c in by if c in set(prima.cols)]
        return [(c, bool(p.get("descending"))) for c in by] if by else ordine
    if not ordine:
        return None
    chiavi = {k for k, _ in ordine}
    if op_type in ("filter", "limit", "drop_nulls", "reorder"):
        return ordine
    if op_type == "select":
        return ordine if chiavi <= set(p.get("columns") or []) else None
    if op_type == "drop":
        return ordine if not chiavi & set(p.get("columns") or []) else None
    if op_type == "rename":
        mappa = p.get("mapping") or {}
        return [(mappa.get(k, k), desc) for k, desc in ordine]
    if op_type in ("cast", "fill_null"):
        return ordine if not chiavi & set(p.get("columns") or {}) else None
    if op_type == "compute":
        return ordine if not chiavi & {str(c.get("name") or "").strip() for c in (p.get("columns") or [])} else None
    return None


# ── impaginazione (solo spazi, mai il contenuto) ─────────────────────────────
_CLAUSOLA = re.compile(
    r"\s+(FROM|WHERE|GROUP BY|ORDER BY|QUALIFY|HAVING|LIMIT|UNION ALL BY NAME|UNION ALL|"
    r"(?:LEFT SEMI|LEFT ANTI|INNER|LEFT|RIGHT|FULL OUTER|CROSS|SEMI|ANTI) JOIN|USING SAMPLE)\b", re.IGNORECASE)


# the pieces of a DuckDB query that are not code: comments (`c`: -- … ends at a line break, as in
# DuckDB, /* … */) and the text whose bytes are data (`q`: '…' with '', E'…' with backslash
# escapes, $$…$$ and $tag$…$tag$, "…" names with ""). An unclosed one runs to the end.
_PEZZI_SQL = re.compile(
    r"(?P<c>--[^\n\r]*|/\*.*?(?:\*/|\Z))"
    r"|(?P<q>(?<![\w$])[eE]'(?:[^'\\]|\\.|'')*(?:'|\Z)"
    r"|'(?:[^']|'')*(?:'|\Z)"
    r'|"(?:[^"]|"")*(?:"|\Z)'
    r"|(?<![\w$])\$\$.*?(?:\$\$|\Z)"
    r"|(?<![\w$])\$(?P<tag>[A-Za-z_]\w*)\$.*?(?:\$(?P=tag)\$|\Z))", re.DOTALL)


def _maschera(sql: str) -> str:
    """Il SQL con stringhe, identificatori quotati, commenti e parentesi annidate coperti:
    le parole chiave si cercano solo al primo livello, fuori dalle virgolette e dai commenti."""
    coperto = list(sql)
    for m in _PEZZI_SQL.finditer(sql):
        coperto[m.start():m.end()] = "_" * (m.end() - m.start())
    out, prof = [], 0
    for ch in coperto:
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
    """Una clausola per riga e, se la SELECT è lunga, una colonna per riga. Tocca
    SOLO gli spazi fuori da stringhe e parentesi: il SQL resta lo stesso."""
    m = _maschera(sql)
    tagli = [x.start() for x in _CLAUSOLA.finditer(m)]
    if not tagli:
        return sql
    righe, prima = [], 0
    for t in tagli:
        righe.append(sql[prima:t].strip())
        prima = t
    righe.append(sql[prima:].strip())
    testa = righe[0]
    if len(testa) > 80 and re.match(r"(?i)^select\s+(distinct\s+)?", testa):
        avvio = re.match(r"(?i)^select\s+(distinct\s+)?", testa).end()
        mt = _maschera(testa)
        pezzi, inizio = [], avvio
        for i, ch in enumerate(mt):
            if i >= avvio and ch == ",":
                pezzi.append(testa[inizio:i].strip())
                inizio = i + 1
        pezzi.append(testa[inizio:].strip())
        testa = testa[:avvio].strip() + "\n" + ",\n".join("    " + x for x in pezzi)
    return "\n".join([testa] + righe[1:])


def _rientra(sql: str, margine: str = "    ") -> str:
    """Every line of a step indented by `margine`, never inside the text whose bytes are data:
    a string or a quoted name that spans lines keeps them (the indentation went into the value,
    'a⏎  b' became 'a⏎      b' and a filter on it kept no row). Outside of them a `\\r\\n` or a
    `\\r` is a line break like `\\n` (dbt writes them as `\\n` anyway)."""
    if not sql:
        return ""
    codice = lambda t: re.sub(r"\r\n?", "\n", t).replace("\n", "\n" + margine)   # noqa: E731
    pezzi, prima = [], 0
    for m in _PEZZI_SQL.finditer(sql):
        if m.group("q"):
            pezzi += [codice(sql[prima:m.start()]), m.group(0)]
            prima = m.end()
    testo = margine + "".join(pezzi) + codice(sql[prima:])
    # as str.splitlines did: no indented empty line after a final line break
    return testo[:-len(margine) - 1] if testo.endswith("\n" + margine) else testo


def compile_model_sql(source: dict, operations: list, resolve: SourceResolver, native: bool = False,
                      dialetto: str | None = None, info: dict | None = None, testo: str | None = None,
                      usati: set | None = None) -> tuple[str, list[str], dict]:
    """Compila (sorgente + operazioni) in un modello dbt completo. Ritorna
    (SQL del modello, colonne di uscita, loro tipi DuckDB). `testo`: the dialect the
    flow's compute expressions and sql nodes are written in (its engine's: polars,
    duckdb, clickhouse, bigquery; None = duckdb). `info`, if given, receives
    `segnaposti` (the texts written in the warehouse's dialect, which `rimetti_testi`
    puts back in the translated SQL) and `da_rivedere` ([(step, dialect)]: text the team
    must review). `usati`: the CTE names the project's other models already have (shared, and
    filled with this model's): no model repeats one (see `_Model._usati`)."""
    model = _Model(_Namer("s"), native=native, dialetto=dialetto, testo=testo or "duckdb", usati=usati)
    right_namer = _Namer("r")
    src_ref, src_cols, src_types, nome = _risolvi(resolve, source)
    try:
        base, s = _base(model, src_ref, _S(src_cols, src_types), nome, tipizza="model" not in source)
        final, out = _compile_ops(model, base, s, operations, resolve, right_namer)
    finally:
        model.sonda.chiudi()
    body = ",\n".join(
        f"{name} AS (\n" + (f"    -- {nota}\n" if nota else "") + f"{_rientra(_formatta(sql))}\n)"
        for name, sql, nota in model.ctes
    )
    finale = _lista(out.cols) if (native and out.cols) else "*"
    if info is not None:
        info["segnaposti"] = dict(model.segnaposti)
        info["per_sonda"] = dict(model.per_sonda)
        info["da_rivedere"] = list(dict.fromkeys(model.da_rivedere))
    return f"WITH\n{body}\nSELECT {finale}\nFROM {final}", out.cols, out.types


def nomi_delle_cte(sql: str, dialetto: str | None) -> set[str]:
    """The CTE names of a query the export does not write (a datasource query, an AI
    proposal): the models compiled after it must not repeat them (see `_Model._usati`)."""
    import sqlglot
    from sqlglot import exp

    try:
        return {c.alias_or_name for a in sqlglot.parse(sql, read=dialetto or "duckdb") if a is not None for c in a.find_all(exp.CTE)}
    except Exception:  # noqa: BLE001 — a query sqlglot does not read: no name to avoid
        return set()


def rimetti_testi(sql: str, info: dict | None) -> str:
    """The translated SQL with the texts written in the warehouse's dialect in place of
    their placeholders (see `_Model.testo_nativo`)."""
    for segnaposto, testo in ((info or {}).get("segnaposti") or {}).items():
        sql = re.sub(re.escape(segnaposto[:-2]) + r"\s*\(\s*\)", lambda _m, t=testo: t, sql, flags=re.IGNORECASE)
    return sql


def tipizza_query(sql: str, cols: list[str], dtypes: dict) -> str:
    """Una query (datasource da query) con i tipi che Tabularia le ha dato."""
    tipi = {c: tipo_sorgente(dtypes.get(c)) for c in cols}
    if not cols or not any(tipi.values()):
        return sql
    return f"SELECT {_proiezione_tipizzata(None, cols, tipi)} FROM ({sql}) AS _q"


# ─────────────────────────────────────────────────────────────────────────────
# test singolari dal data contract (SQL DuckDB; in nativo si traduce come i modelli)
# ─────────────────────────────────────────────────────────────────────────────
TEST_IDENT = "__t"   # il modello sotto test, da sostituire con {{ ref() }}


def _come_testo(q: str, tipo: Optional[str], dialetto: Optional[str]) -> str:
    """A column as the text Tabularia's contract check reads it (Polars' cast to String):
    a date `2024-01-05`, an instant `2024-01-05 10:30:00.000000` (six digits, always), a
    boolean `true`, a decimal with all its scale digits (`1.50`), a float with `.0` when whole.
    Written in DuckDB SQL for `dialetto`, the warehouse the test is translated for."""
    t = (tipo or "").upper()
    fam = _famiglia(tipo)
    if fam == "testo":
        return q
    if t.startswith("TIMESTAMP") or t == "DATETIME":
        if dialetto == "clickhouse":
            return f"CAST({q} AS VARCHAR)"   # a DateTime64(6) prints its six digits
        # Postgres: to_char(…, 'YYYY-MM-DD HH24:MI:SS.US'); a bare CAST drops the zeros of the fraction
        return f"strftime({q}, '%Y-%m-%d %H:%M:%S.%f')"
    if t == "DATE" and dialetto == "postgres":
        return f"strftime({q}, '%Y-%m-%d')"   # to_char: the same whatever the server's DateStyle
    if t in ("DOUBLE", "FLOAT", "REAL"):
        return _float_in_testo(q, dialetto)
    m = re.fullmatch(r"DECIMAL\(\s*\d+\s*,\s*(\d+)\s*\)", t)
    if m and dialetto == "clickhouse":
        return f"toDecimalString({q}, {m.group(1)})"   # toString cuts the trailing zeros: 1.50 → '1.5'
    return f"CAST({q} AS VARCHAR)"


def _flag_iniziali(regex: str) -> tuple[str, str]:
    """`(?is)abc` → ('is', 'abc'): the inline flags a regex opens with, when they are ones
    Postgres reads the same way at the start of the whole pattern (i, s, x)."""
    m = re.match(r"\(\?([isx]+)\)", regex)
    return (m.group(1), regex[m.end():]) if m else ("", regex)


def _punto_senza_a_capo(regex: str) -> str:
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
                    fine = regex.find(regex[j + 1] + "]", j + 2)
                    j = fine + 2 if fine != -1 else n
                else:
                    j += 1
            out.append(regex[i:j + 1])
            i = j + 1
            continue
        out.append("[^\\n]" if ch == "." else ch)
        i += 1
    return "".join(out)


def _regex_intera(regex: str, dialetto: Optional[str]) -> str:
    """The regex of a pattern rule for the WHOLE value, as Tabularia matches it (`^(?:…)$`,
    with Polars' `.`, which does not match a newline): DuckDB's RE2 already reads it so;
    ClickHouse's `match` lets `.` match a newline unless told `(?-s)`; Postgres reads its
    flags only at the start of the pattern and has no `(?-s)` (see `_punto_senza_a_capo`)."""
    if dialetto == "clickhouse":
        return f"(?-s)^(?:{regex})$"
    if dialetto == "postgres":
        flag, resto = _flag_iniziali(regex)
        if "s" not in flag:
            resto = _punto_senza_a_capo(resto)
        return (f"(?{flag})" if flag else "") + f"^(?:{resto})$"
    return f"^(?:{regex})$"


# a boolean written in a contract, read as Tabularia's contract check reads it (app.contracts.evaluator)
_BOOLEANI = {True: True, False: False, "true": True, "false": False, "1": True, "0": False, 1: True, 0: False}


def _doppio(x: float) -> str:
    return f"CAST('{x}' AS DOUBLE)" if x != x or x in (float("inf"), float("-inf")) else f"CAST({x!r} AS DOUBLE)"


def _tra_gli_ammessi(q: str, valori: list, tipo: Optional[str], dialetto: Optional[str]) -> str:
    """`q` is one of the accepted `valori`, compared as Tabularia's contract check compares them:
    on a number column as numbers («3» = 3 = 3.0, unless a value is not a number), on a boolean
    column as booleans, otherwise by the column's text. dbt's accepted_values wrote every value
    as '{{ value }}': an apostrophe broke the SQL, a 3.0 failed on an integer column."""
    if _famiglia(tipo) in ("intero", "numero"):
        try:
            numeri = [float(v) for v in valori if not isinstance(v, bool)]
        except (TypeError, ValueError):
            numeri = None   # a value that is not a number: compared by the text, as Tabularia does
        if numeri is not None:
            return f"CAST({q} AS DOUBLE) IN ({', '.join(_doppio(x) for x in numeri)})" if numeri else "FALSE"
    elif (tipo or "").upper() == "BOOLEAN":
        try:
            booleani = [_BOOLEANI[v.strip().lower() if isinstance(v, str) else v] for v in valori]
        except (KeyError, TypeError):
            # Tabularia's check cannot evaluate it either: the rule never passes there
            raise DbtExportError("accepted_values: a value of a boolean column is not a boolean", "contract_rule_invalid",
                                 rule="accepted_values")
        return f"{q} IN ({', '.join('TRUE' if b else 'FALSE' for b in dict.fromkeys(booleani))})"
    return f"{_come_testo(q, tipo, dialetto)} IN ({', '.join(_lit(str(v)) for v in valori)})"


def singular_test_sql(test: dict, tipi: Optional[dict] = None, dialetto: Optional[str] = None) -> str:
    """Una regola di contratto che non è un test generico di dbt → una query che
    SELEZIONA le violazioni (dbt la considera fallita se torna righe). `tipi`: the
    model's column types; `dialetto`: the warehouse the test is translated for."""
    q, t = _qi, f'"{TEST_IDENT}" AS "{TEST_IDENT}"'
    kind, col = test.get("kind"), test.get("column")
    tipi = tipi or {}
    if kind == "not_null":
        if not col:
            raise DbtExportError("not_null: a column is required", "contract_rule_invalid", rule="not_null")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NULL"
    if kind == "accepted_values":
        if not col or not isinstance(test.get("values"), list) or not test["values"]:
            raise DbtExportError("accepted_values: a column and a list of values are required", "contract_rule_invalid",
                                 rule="accepted_values")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL AND NOT ({_tra_gli_ammessi(q(col), test['values'], tipi.get(col), dialetto)})"
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
        testo = _come_testo(q(col), tipi.get(col), dialetto)
        return (f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL "
                f"AND NOT regexp_matches({testo}, {_lit(_regex_intera(str(test['regex']), dialetto))})")
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
        lista = ", ".join(q(c) for c in cols)
        return f"SELECT {lista}, COUNT(*) AS n FROM {t} GROUP BY {lista} HAVING COUNT(*) > 1"
    if kind == "expression":
        sql = str(test.get("sql") or "").strip()
        if not sql:
            raise DbtExportError("expression: the expression is empty", "contract_rule_invalid", rule="expression")
        # un esito «non lo so» (null) non è un sì: la riga viola — come il valutatore
        return f"SELECT * FROM {t} WHERE NOT COALESCE(({sql}), FALSE)"
    raise DbtExportError(f"rule '{kind}' has no dbt equivalent", "rule_no_equivalent", kind=kind)


def test_di_colonna(columns: list[dict]) -> tuple[list[dict], list[dict]]:
    """The contract's column tests (on the schema.yml columns, as the gateway sends them) →
    the columns with the generic tests dbt runs as Tabularia's check does — not_null only — and
    the rules that become singular tests (`singular_test_sql`, with their `column`): unique,
    where two NULLs are a duplicate (decision 7), and accepted_values (see `_tra_gli_ammessi`).
    A generic test gets the column name quoted by the adapter (`quote: true`, see `_schema_yaml`),
    which writes `"…"` without escaping a double quote in it: on such a column not_null is a
    singular test too. Two not_null rules on a column (a warning and an error) are ONE test
    with the stricter severity: dbt names both alike and refused the project at parse; the
    error fails exactly where the warning would warn."""
    colonne, singolari = [], []
    for c in columns:
        not_null: Optional[dict] = None
        for t in c.get("tests") or []:
            if t.get("kind") != "not_null" or '"' in str(c.get("name")):
                singolari.append({**t, "column": c["name"]})
            elif not_null is None:
                not_null = t
            elif t.get("severity", "error") == "error":
                not_null = {**not_null, "severity": "error"}
        colonne.append({**c, "tests": [not_null] if not_null else []})
    return colonne, singolari


# ─────────────────────────────────────────────────────────────────────────────
# traduzione dialetto (warehouse nativo) via sqlglot
# ─────────────────────────────────────────────────────────────────────────────
# In modalità nativa la sorgente è un IDENTIFICATORE segnaposto (non una macro
# dbt, che sqlglot non saprebbe parsare). Si compila in SQL DuckDB, si espandono
# gli star con lo schema (qualify) e si traduce nel dialetto target; poi il
# segnaposto viene sostituito con `{{ source() }}` / `{{ ref() }}` SOLO nel
# riferimento tabella (tenendo l'alias, così i qualificatori di colonna restano validi).
_SQLGLOT_DIALECT = {
    "postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "clickhouse": "clickhouse",
}


def _senza_unsigned(tree):
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
    """Buchi noti fra i dialetti che sqlglot non copre da solo."""
    from sqlglot import exp

    if dialect == "postgres":
        # Postgres ha round(numeric, int) ma NON round(double precision, int)
        for r in list(tree.find_all(exp.Round)):
            if r.args.get("decimals") is not None and not isinstance(r.this, exp.Cast):
                r.set("this", exp.Cast(this=r.this.copy(), to=exp.DataType.build("DECIMAL")))
        _senza_unsigned(tree)
        # DuckDB's year(d), month(d)…, which Postgres does not have (sqlglot keeps them): EXTRACT,
        # an integer as in DuckDB (a second without its fraction)
        parti = {exp.Year: "YEAR", exp.Month: "MONTH", exp.Day: "DAY", exp.DayOfMonth: "DAY", exp.DayOfWeek: "DOW",
                 exp.DayOfWeekIso: "ISODOW", exp.DayOfYear: "DOY", exp.Quarter: "QUARTER", exp.Week: "WEEK",
                 exp.WeekOfYear: "WEEK", exp.Hour: "HOUR", exp.Minute: "MINUTE", exp.Second: "SECOND"}
        for nodo in reversed(list(tree.walk(bfs=False))):
            parte = parti.get(type(nodo))
            if parte and not (isinstance(nodo, exp.Week) and nodo.args.get("mode") is not None):
                estratto = exp.Extract(this=exp.var(parte), expression=nodo.this.copy())
                if parte == "SECOND":
                    estratto = exp.Floor(this=estratto)
                nodo.replace(exp.Cast(this=estratto, to=exp.DataType.build("BIGINT")))
    if dialect == "clickhouse":
        _tempi_clickhouse(tree)
        # DuckDB's upper/lower/substring/reverse/left/right/lpad work on characters, ClickHouse's
        # on bytes ('città' → 'CITTà'): their *UTF8 forms, as the ClickHouse engine uses
        utf8 = {exp.Upper: "upperUTF8", exp.Lower: "lowerUTF8", exp.Reverse: "reverseUTF8", exp.Substring: "substringUTF8",
                exp.Left: "leftUTF8", exp.Right: "rightUTF8"}
        for nodo in reversed(list(tree.walk(bfs=False))):   # the inner calls first: the outer copies them
            nome = utf8.get(type(nodo))
            if isinstance(nodo, exp.Pad):
                nome = "leftPadUTF8" if nodo.args.get("is_left") else "rightPadUTF8"
            if isinstance(nodo, exp.StrPosition) and not nodo.args.get("occurrence"):
                # strpos/instr/position count characters; ClickHouse's POSITION counts bytes
                argomenti = [a for a in (nodo.this, nodo.args.get("substr"), nodo.args.get("position")) if a is not None]
                nodo.replace(exp.Anonymous(this="positionUTF8", expressions=[a.copy() for a in argomenti]))
                continue
            if nome:
                argomenti = [a for a in (nodo.this, *(nodo.args.get(k) for k in ("start", "length", "expression", "fill_pattern"))) if a is not None]
                nodo.replace(exp.Anonymous(this=nome, expressions=[a.copy() for a in argomenti]))
    return tree


def transpile_model(sql: str, dialect: str, schema: dict[str, list[str]]) -> str:
    """Traduce il SQL DuckDB del modello nel dialetto target, espandendo gli
    star con lo schema delle sorgenti ({ident: [colonne]})."""
    import sqlglot
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.schema import MappingSchema

    sg_schema = {ident: {c: "VARCHAR" for c in cols} for ident, cols in schema.items()}
    try:
        # validate_qualify_columns=False: espande gli star best-effort senza
        # fallire se un riferimento non è staticamente risolvibile (self-join,
        # colonne derivate) — l'esecuzione reale nel warehouse le risolve comunque.
        # I nomi NON vanno normalizzati con le regole di DuckDB (che non distingue le
        # maiuscole e porterebbe «Nome Strano» a «nome strano», un'altra colonna nel
        # warehouse): schema tal quale e regole del dialetto di arrivo, dove un nome
        # fra virgolette resta com'è.
        # expand_stars=False: le liste le scrive l'exporter (vedi _lista); dove resta
        # uno `*` (dopo un nodo sql) lo si lascia al warehouse
        tree = qualify(
            sqlglot.parse_one(_stringhe_standard(sql), read="duckdb"),
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
    """Sostituisce `"ident" AS "ident"` → `{{ source(...) }} AS "ident"` (solo il
    riferimento tabella; alias e qualificatori di colonna restano invariati)."""
    for ident, macro in ident_to_macro.items():
        pat = r'(["`])' + re.escape(ident) + r'\1\s+AS\s+(["`])' + re.escape(ident) + r"\2"
        # a function, not a template: the macro is written as it is (a `'\\'` in it stays two backslashes)
        sql = re.sub(pat, lambda m, macro=macro, ident=ident: f"{macro} AS {m.group(2)}{ident}{m.group(2)}", sql)
    return sql


# ─────────────────────────────────────────────────────────────────────────────
# Generatore di progetto dbt
# ─────────────────────────────────────────────────────────────────────────────
# duckdb: DuckDB attacca il DB di origine (postgres/mysql) e legge le tabelle
# LIVE: il nostro SQL DuckDB gira as-is, niente snapshot, niente traduzione.
# clickhouse/trino non hanno uno scanner DuckDB ufficiale → non federabili.
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
# dbt-clickhouse sul ClickHouse del cliente (di solito quello del motore). Le sorgenti
# Postgres/MySQL si leggono DAL VIVO attraverso un database ClickHouse per (connessione,
# schema), `tab_src_<id>_<schema>`, with one table per source table (ENGINE = PostgreSQL /
# MySQL) whose types keep every value (see `_struttura_sql`). Li crea la macro
# `create_tabularia_sources` (`dbt run-operation`, password da env_var). Da lì in poi è
# tutto ClickHouse: sources.yml punta a quei database.
_CH_MOTORE_FEDERATO = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MySQL"}


def nome_db_federato(conn_id: int, schema: str) -> str:
    """Il database ClickHouse che mostra uno schema di una connessione Postgres/MySQL: one per
    schema. A schema that is not a plain lowercase name gets a fingerprint of its exact name
    after the readable part: 'Sales' and 'sales', 'vendite_città' and 'vendite_citt', or two
    Cyrillic names were one database, read from the first of them."""
    schema = str(schema)
    leggibile = re.sub(r"[^a-z0-9]+", "_", schema.lower()).strip("_") or "default"
    if leggibile != schema:
        leggibile += "_" + hashlib.sha256(schema.encode()).hexdigest()[:10]
    return f"tab_src_{conn_id}_{leggibile}"


def _date_come_testo(albero) -> None:
    """In a comparison, a date or timestamp written as a typed literal (`DATE '1970-01-01'`,
    `'…'::timestamp`) becomes its text: ClickHouse 24 hands a typed date constant to a
    PostgreSQL/MySQL table as a number of days (`"nato" < 0`, which Postgres refuses) and a
    timestamp as seconds; the text it hands as it is, and compares with the column's own type."""
    from sqlglot import exp

    T = exp.DataType.Type
    for c in list(albero.find_all(exp.Cast)):
        confronto = isinstance(c.parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In))
        if not (confronto and isinstance(c.this, exp.Literal) and c.this.is_string):
            continue
        # an instant with its offset (`… 10:00+02`) stays typed: as text ClickHouse would read it in its own zone
        con_fuso = re.search(r"\d:\d\d(:\d\d(\.\d+)?)?\s*(Z|[+-]\d\d(:?\d\d)?)$", c.this.this.strip())
        if c.to.this in (T.DATE, T.TIMESTAMP, T.TIMESTAMPNTZ, T.DATETIME) or (c.to.this in (T.TIMESTAMPTZ, T.TIMESTAMPLTZ) and not con_fuso):
            altri = [x for x in c.parent.iter_expressions() if x is not c]
            if any(not isinstance(x, (exp.Literal, exp.Cast)) for x in altri):   # never two literals compared as text
                c.replace(c.this.copy())


def _schema_di_default(conn: dict) -> str:
    return "public" if conn["db_type"] == "postgresql" else (conn.get("database") or "")


class TargetClickHouse:
    """Le sorgenti di un export verso ClickHouse: quali database federati servono,
    come si chiamano, e le query delle datasource tradotte in ClickHouse."""

    def __init__(self, connections: list[dict], host_motore: str | None):
        self.conns = {int(c["id"]): c for c in connections}
        self.host_motore = (host_motore or "").strip().lower()
        self.federati: dict[str, dict] = {}     # nome del database → come crearlo

    def database_per(self, conn_id: int, schema: str, tabella: str | None = None) -> str:
        """The ClickHouse database that shows `schema` of a connection; `tabella`: a table
        the project reads there, which the macro creates in it."""
        conn = self.conns.get(int(conn_id))
        if conn is None:
            raise DbtExportError(f"connection {conn_id} is unknown to the export")
        if conn["db_type"] == "clickhouse":
            # un database del ClickHouse dove gira dbt: si legge così com'è
            if (conn.get("host") or "").strip().lower() != self.host_motore:
                raise DbtExportError(
                    f"the connection «{conn.get('name')}» is ANOTHER ClickHouse ({conn.get('host')}): the ClickHouse target "
                    "reads the Postgres/MySQL sources and the tables of its own server. Use the «native» export on that ClickHouse.",
                    "other_clickhouse", connection=conn.get("name"), host=conn.get("host"),
                )
            return schema or conn.get("database") or "default"
        motore = _CH_MOTORE_FEDERATO.get(conn["db_type"])
        if motore is None:
            raise DbtExportError(
                f"the connection «{conn.get('name')}» is {conn['db_type']}: ClickHouse can read it only if it is "
                "PostgreSQL, MySQL or MariaDB. Use another target for this flow.",
                "clickhouse_cannot_read", connection=conn.get("name"), db_type=conn["db_type"],
            )
        nome = nome_db_federato(conn["id"], schema)
        f = self.federati.setdefault(nome, {"name": nome, "engine": motore, "conn": conn, "schema": schema, "tables": set()})
        if f["schema"] != schema:   # two schemas, one database: never read one in place of the other
            raise DbtExportError(f"the schemas «{f['schema']}» and «{schema}» would share the ClickHouse database {nome}")
        if tabella:
            f["tables"].add(tabella)
        return nome

    def sources(self, sources: list[dict]) -> list[dict]:
        """sources.yml: lo schema di ogni sorgente diventa il database ClickHouse che la mostra."""
        out = []
        for src in sources:
            if src.get("declared"):
                out.append(src)   # nel loro progetto: la dichiarano loro, nessun database federato da creare
                continue
            for t in src.get("tables") or []:
                self.database_per(src["connection_id"], src["schema"], t["name"])
            out.append({**src, "database": None, "schema": self.database_per(src["connection_id"], src["schema"])})
        return out

    def traduci_query(self, sql: str, conn_id: int, etichetta: str, proposta: str | None = None,
                      colonne: list[str] | None = None) -> str:
        """La query di una datasource, scritta per Postgres/MySQL, in SQL ClickHouse:
        le sue tabelle diventano quelle dei database federati. Con una `proposta`
        (dell'AI, già in SQL ClickHouse con i NOMI DI TABELLA dell'origine) si usa
        quella, dopo i controlli: le tabelle le ripunta comunque questo codice."""
        import sqlglot
        from sqlglot import exp

        conn = self.conns.get(int(conn_id))
        if conn is None:
            raise DbtExportError(f"connection {conn_id} is unknown to the export")
        if conn["db_type"] == "clickhouse":
            self.database_per(conn_id, conn.get("database") or "")   # stesso server, o rifiuto
            return sql
        dialetto = _SQLGLOT_DIALECT.get(conn["db_type"])
        if proposta is not None:
            albero = verifica_proposta(proposta, "clickhouse", None, colonne, etichetta)
            alberi = [albero]
            dialetto = "clickhouse"
        try:
            alberi = alberi if proposta is not None else sqlglot.parse(sql, read=dialetto)
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"the query of the datasource «{etichetta}» cannot be translated to ClickHouse ({str(e)[:200]}): use the federated target",
                                 "query_not_translatable", datasource=etichetta, detail=str(e)[:200])
        if len(alberi) != 1 or alberi[0] is None:
            raise DbtExportError(f"the query of the datasource «{etichetta}» is not a single SELECT: it cannot be translated to ClickHouse",
                                 "query_not_single_select", datasource=etichetta)
        albero = alberi[0]
        if proposta is None and dialetto == "postgres":
            # Postgres folds a name written without quotes to lowercase (`Prodotti` is the table
            # prodotti), ClickHouse does not: the names as Postgres reads them, quoted ones as they are
            from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

            albero = normalize_identifiers(albero, dialect="postgres")
        for t in list(albero.find_all(exp.Table)):
            # a CTE only where it is visible: any other name is a table of the connection
            if not t.name or legge_una_cte(t):
                continue
            t.set("db", exp.to_identifier(self.database_per(conn_id, t.db or _schema_di_default(conn), t.name)))
            t.set("catalog", None)
        _date_come_testo(albero)
        _fix_for_dialect(albero, "clickhouse")   # Date32/DateTime64, and the *UTF8 functions, as in the models
        try:
            return albero.sql(dialect="clickhouse", pretty=True)
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"the query of the datasource «{etichetta}» cannot be translated to ClickHouse ({str(e)[:200]}): use the federated target",
                                 "query_not_translatable", datasource=etichetta, detail=str(e)[:200])


# ClickHouse reads at most max_query_size bytes of a query (256 KiB by default): the profiles raise
# it to this, the syntax check reads the models with it, and a model bigger than it, with the
# ephemeral models dbt inlines into it, is refused (see `troppo_grande`). Far below it the planning
# already takes long: about 100 s for 150 columns × 40 steps on ClickHouse 24.8.
MAX_QUERY_SIZE = 16 * 1024 * 1024
_INVOLUCRO = 8 * 1024   # what dbt-clickhouse writes around a model (CREATE TABLE … AS, SETTINGS)
ATTESA_CONTROLLO = 10   # seconds the syntax check waits for the engine's ClickHouse (EXPLAIN AST only parses)


def troppo_grande(modello: str, dimensione: int) -> DbtExportError:
    """The refusal of a model ClickHouse would not read: not a «not translatable» (that asks the
    AI for a translation, which can only be as big)."""
    return DbtExportError(
        f"the model «{modello}» is {dimensione:,} bytes of SQL together with the ephemeral models dbt puts inside it: ClickHouse reads "
        f"at most {MAX_QUERY_SIZE:,} (max_query_size). Split the flow in two: a flow that reads another flow's output exports it as a model of its own.",
        "model_too_large", model=modello, size=f"{dimensione:,}", max=f"{MAX_QUERY_SIZE:,}")


def oltre_il_limite(dimensione: int) -> bool:
    return dimensione + _INVOLUCRO > MAX_QUERY_SIZE


def in_byte(sql: str) -> int:
    """The size ClickHouse's max_query_size measures: bytes, not characters (an accented letter is two)."""
    return len(sql.encode("utf-8", "surrogatepass"))


class ControlloSintassiClickHouse:
    """Il SQL ClickHouse di un modello letto da un parser ClickHouse VERO: `EXPLAIN AST`,
    che non esegue niente e non vuole che le tabelle esistano. sqlglot non fallisce
    quasi mai: traduce in silenzio costrutti che ClickHouse non ha (SIMILAR TO,
    LATERAL, TABLESAMPLE…), e l'errore usciva solo al `dbt run` del team data (scelta
    dell'utente, 2026-10-10: controllarlo all'export). Un errore qui è un «non
    tradotto»: col dialogo, la proposta dell'AI; senza, un rifiuto che dice perché.
    Parser: chDB se c'è (sviluppo, locale), altrimenti il ClickHouse del motore; se
    nessuno risponde il controllo si salta, con un log — non blocca un export.
    Non vede le funzioni che non esistono: per quelle servirebbe l'analisi.
    A ClickHouse that accepts the connection and never answers waits ATTESA_CONTROLLO seconds,
    once: then the check is off for the rest of the export (`saltato`), and the README says so."""

    def __init__(self):
        self._via: Any = None   # "chdb", un client clickhouse-connect, o False (nessun parser)
        self.saltato = False    # a parser was configured and did not answer: the SQL was not read by it

    def chiudi(self) -> None:
        if self._via not in (None, False, "chdb"):
            try:
                self._via.close()
            except Exception:  # noqa: BLE001
                pass
            self._via = False

    def _apri(self):
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
            usa_ai = bool(cfg.ai_username and cfg.ai_password.get_secret_value())   # l'utenza di sola lettura, se c'è
            # a read timeout of its own (the client's default is 300 s), no session: one request each
            self._via = clickhouse_connect.get_client(
                host=cfg.host, port=cfg.port, secure=cfg.secure, connect_timeout=min(cfg.connect_timeout, ATTESA_CONTROLLO),
                send_receive_timeout=ATTESA_CONTROLLO, autogenerate_session_id=False,
                username=cfg.ai_username if usa_ai else cfg.username,
                password=(cfg.ai_password if usa_ai else cfg.password).get_secret_value(),
            )
        except Exception as e:  # noqa: BLE001 — server spento, credenziali: si esporta senza il controllo
            logger.warning("dbt export: the ClickHouse syntax check is not available (%s)", str(e)[:200])
            self._via, self.saltato = False, True
        return self._via

    def errore(self, sql: str) -> str | None:
        """None se ClickHouse legge il SQL; altrimenti la prima riga del suo errore."""
        via = self._apri()
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
            testo = str(e)
            if "Syntax error" not in testo and "SYNTAX_ERROR" not in testo:
                # no answer (a timeout, a dropped connection): off for the rest of the export, not one wait per model
                logger.warning("dbt export: the ClickHouse syntax check did not answer (%s)", testo[:200])
                self.chiudi()
                self._via, self.saltato = False, True
                return None
            riga = next((r for r in testo.splitlines() if "Syntax error" in r), testo.splitlines()[0])
            riga = re.sub(r"^.*?DB::Exception:\s*", "", riga)
            return re.sub(r"\.\s*Expected one of:.*$", "", riga).strip()[:200]


def legge_una_cte(t) -> bool:
    """True if a table reference names a CTE that is VISIBLE where it stands: never a
    qualified name (`system.users` is read for real even next to a CTE `users`), only the
    CTEs of an enclosing query, and inside a CTE's own body only the CTEs before it (itself
    only if RECURSIVE) — elsewhere the same name is a real table of the database."""
    from sqlglot import exp

    if t.args.get("db") or t.args.get("catalog"):
        return False
    figlio, n = t, t.parent
    while n is not None:
        visibili: list = []
        if isinstance(n, exp.With):
            ctes = list(n.expressions)
            i = next((k for k, c in enumerate(ctes) if c is figlio), None)
            if i is not None:
                visibili = ctes[:i] + ([ctes[i]] if n.args.get("recursive") else [])
        else:
            # the query's WITH (its arg is `with` or `with_` depending on the sqlglot version)
            w = next((v for v in n.args.values() if isinstance(v, exp.With)), None)
            if w is not None and w is not figlio:
                visibili = list(w.expressions)
        if any(c.alias_or_name == t.name for c in visibili):
            return True
        figlio, n = n, n.parent
    return False


# functions that read data or settings outside the model, or change state: a translation
# never needs them (ClickHouse: dictionaries, joins, files, URLs, other servers, settings;
# Postgres: dblink, server files, large objects, settings, sequences, XML of whole tables)
_FUNZIONI_VIETATE = re.compile(
    r"(?i)(dict(get|has|isin)\w*|joinget\w*|file|url|urlcluster|s3|s3cluster|hdfs\w*|azureblobstorage\w*|remote\w*|cluster\w*|"
    r"mysql|postgresql|mongodb|redis|jdbc|odbc|getsetting\w*|getmacro|getserverport|input|"
    r"dblink\w*|pg_\w+|lo_\w+|set_config|current_setting|nextval|setval|currval|lastval|"
    r"(query|table|cursor|schema|database)_to_xml\w*)"
)


def verifica_proposta(sql: str, dialetto: str, consentite: set[str] | None, colonne: list[str] | None, modello: str):
    """I controlli su una traduzione proposta dall'AI — li fa questo codice, non l'AI:
    una sola SELECT nel dialetto di arrivo, niente istruzioni che scrivono, nessuna
    table function, nessuna funzione che legge fuori dal modello (dizionari, file,
    impostazioni…), niente SETTINGS, solo le tabelle che il modello legge (se
    `consentite`: i suoi segnaposto, mai qualificati, e le CTE visibili), le stesse
    colonne di uscita nello stesso ordine (se `colonne`). Torna l'albero sqlglot."""
    import sqlglot
    from sqlglot import exp

    def no(motivo: str, testo: str, **dettagli):
        # il motivo è un CODICE (il gateway lo dice nella lingua di chi legge), il testo resta nei log
        raise DbtExportError(f"the AI's proposal for «{modello}» does not pass the checks: {testo}",
                             "ai_translation_invalid", model=modello, reason=motivo, **dettagli)

    try:
        alberi = [a for a in sqlglot.parse(sql or "", read=dialetto) if a is not None]
    except Exception as e:  # noqa: BLE001
        no("unparsable", f"it cannot be parsed ({str(e)[:120]})")
    if len(alberi) != 1 or not isinstance(alberi[0], exp.Query):
        no("not_select", "it is not a single SELECT")
    albero = alberi[0]
    if any(isinstance(n, (exp.Insert, exp.Create, exp.Drop, exp.Delete, exp.Update, exp.Alter, exp.Command, exp.Merge)) for n in albero.walk()):
        no("writes", "it contains statements that are not a SELECT")
    if any(q.args.get("settings") for q in albero.find_all(exp.Query)):
        no("settings", "it changes the server's settings (SETTINGS)")
    for t in albero.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            no("table_function", "it reads a table function")
    for f in albero.find_all(exp.Func):
        nome = f.name if isinstance(f, exp.Anonymous) else f.sql_name()
        if nome and _FUNZIONI_VIETATE.fullmatch(str(nome)):
            no("function_not_allowed", f"it calls {nome}, which reads data or settings outside the model", function=str(nome))
    for t in albero.find_all(exp.Table):
        if consentite is None or legge_una_cte(t):
            continue
        if t.args.get("db") or t.args.get("catalog") or t.name not in consentite:
            nome = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            no("unknown_table", f"it reads a table the model does not read ({nome})", table=nome)
    for i in albero.find_all(exp.In):
        if i.args.get("field") is not None:
            # ClickHouse `x IN db.table` reads that table, and it is not a FROM
            nome = i.args["field"].sql(dialect=dialetto)
            no("unknown_table", f"it reads a table the model does not read ({nome})", table=nome)
    if colonne is not None:
        if any(isinstance(x, exp.Star) or (isinstance(x, exp.Column) and isinstance(x.this, exp.Star)) for x in albero.selects):
            no("star", "it uses * instead of listing the columns")
        uscita = [x.alias_or_name for x in albero.selects]
        if uscita != list(colonne):
            no("columns", f"it returns the columns {uscita} instead of {list(colonne)}", got=", ".join(uscita), expected=", ".join(colonne))
    return albero


def usa_proposta(sql: str, dialetto: str, ident_to_macro: dict[str, str], colonne: list[str], modello: str,
                 controllo: "ControlloSintassiClickHouse | None" = None) -> str:
    """Una traduzione proposta per un modello compilato: controllata, poi con i
    segnaposto delle sorgenti (__s_0, __m_1…) al posto delle macro dbt. I segnaposto
    si rinominano sull'albero, non nel testo: l'AI può scriverli con o senza alias."""
    from sqlglot import exp

    albero = verifica_proposta(sql, dialetto, set(ident_to_macro), colonne, modello)
    for t in list(albero.find_all(exp.Table)):
        if t.name in ident_to_macro:
            alias = t.alias or t.name
            t.set("this", exp.to_identifier(f"__TABULARIA_{t.name}__"))
            t.set("alias", exp.TableAlias(this=exp.to_identifier(alias, quoted=True)))
    testo = albero.sql(dialect=dialetto, pretty=True)
    if controllo and oltre_il_limite(in_byte(testo)):
        raise troppo_grande(modello, in_byte(testo))
    errore = controllo.errore(testo) if controllo else None   # prima delle macro: ClickHouse non legge il Jinja
    if errore:
        raise DbtExportError(f"the AI's proposal for «{modello}» does not pass the checks: ClickHouse cannot parse it ({errore})",
                             "ai_translation_invalid", model=modello, reason="syntax", detail=errore)
    testo = neutralize_jinja(testo)   # Jinja in the proposal is text, never run by dbt; then our macros
    for ident, macro in ident_to_macro.items():
        testo = re.sub(r'[`"]?__TABULARIA_' + re.escape(ident) + r'__[`"]?', lambda _m, macro=macro: macro, testo)
    return testo


def _letterale_ch(v: Any) -> str:
    """Un letterale stringa ClickHouse scritto in un file Jinja: i delimitatori Jinja
    restano testo (neutralizzati, non rifiutati)."""
    t = str(v if v is not None else "")
    return neutralize_jinja("'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'")


def _ident_ch(nome: str) -> str:
    """A ClickHouse identifier between backticks, written in a Jinja file."""
    return neutralize_jinja("`" + str(nome).replace("\\", "\\\\").replace("`", "\\`") + "`")


def _struttura_sql(database: str, tabella: str, motore: str) -> str:
    """The columns of a source table as ClickHouse inferred them (the table made `AS
    postgresql(…)`/`mysql(…)`), each `` `name` type ``, with the types that would lose
    values replaced: a Date (1970-2149) is a Date32 and a DateTime (whole seconds,
    1970-2106) a DateTime64(6), also inside an Array or a Tuple; a Postgres numeric without
    precision, which ClickHouse makes a Decimal(38, 19) (a value over 10^19 failed every
    model), is Tabularia's double. A MySQL decimal stays exact, as Tabularia reads it."""
    t = "concat('(', type, ')')"
    if motore == "PostgreSQL":
        t = f"replaceAll({t}, 'Decimal(38, 19)', 'Float64')"
    for vecchio, nuovo in (("Date)", "Date32)"), ("Date,", "Date32,"), ("DateTime)", "DateTime64(6))"), ("DateTime,", "DateTime64(6),")):
        t = f"replaceAll({t}, '{vecchio}', '{nuovo}')"
    return (r"SELECT concat('`', replaceAll(replaceAll(name, '\\', '\\\\'), '`', '\\`'), '` ', substring(t, 2, length(t) - 2)) "
            f"FROM (SELECT name, position, {t} AS t FROM system.columns WHERE database = {_letterale_ch(database)} "
            f"AND table = {_letterale_ch(tabella)}) ORDER BY position")


def _macro_sorgenti_clickhouse(federati: list[dict]) -> str:
    """La macro che crea i database federati verso le sorgenti: per ogni tabella che il
    progetto legge, una tabella ClickHouse col motore PostgreSQL / MySQL. Its types are the
    ones ClickHouse infers (the table made `AS postgresql(…)` first) where they keep every
    value, and the wider ones where they do not (see `_struttura_sql`): a database engine
    infers them on every read, and a date of 1955 became 2134 before any cast of the model."""
    righe = [
        "{% macro create_tabularia_sources() %}",
        "{#- Tabularia: one ClickHouse database per source (connection, schema), with one table per",
        "    source table (PostgreSQL / MySQL engine): ClickHouse reads the source tables live from there.",
        "    Run it before the first `dbt run`, and again when a source table changes its columns:",
        "      dbt run-operation create_tabularia_sources --log-level-file none",
        "    The source passwords come from env vars and are part of the SQL this runs:",
        "    `--log-level-file none` keeps that SQL out of logs/dbt.log. -#}",
    ]
    for f in federati:
        c = f["conn"]
        pw = f"pw_{_slug(f['name'])}"
        righe.append("{%- set " + pw + " = env_var('" + c["pw_env"] + "') | replace('\\\\', '\\\\\\\\') | replace(\"'\", \"\\\\'\") %}")
        indirizzo = f"{c['host']}:{c.get('port') or (5432 if f['engine'] == 'PostgreSQL' else 3306)}"
        db = f["name"]
        righe += [
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
        for tabella in sorted(f.get("tables") or ()):
            if f["engine"] == "PostgreSQL":
                argomenti = [_letterale_ch(indirizzo), _letterale_ch(c.get("database")), _letterale_ch(tabella), _letterale_ch(c.get("username")),
                             "'{{ " + pw + " }}'", _letterale_ch(f["schema"])]
            else:   # MySQL: lo schema è il database
                argomenti = [_letterale_ch(indirizzo), _letterale_ch(f["schema"]), _letterale_ch(tabella), _letterale_ch(c.get("username")),
                             "'{{ " + pw + " }}'"]
            nome = f"`{db}`.{_ident_ch(tabella)}"
            righe += [
                "{% call statement('" + db + "') -%}",
                f"CREATE OR REPLACE TABLE {nome} AS {f['engine'].lower()}({', '.join(argomenti)})",
                "{%- endcall %}",
                "{%- set struttura %}" + _struttura_sql(db, tabella, f["engine"]) + "{% endset %}",
                "{%- set colonne = run_query(struttura).columns[0].values() %}",
                "{% call statement('" + db + "') -%}",
                f"CREATE OR REPLACE TABLE {nome} ({{{{ colonne | join(', ') }}}}) ENGINE = {f['engine']}({', '.join(argomenti)})",
                "{%- endcall %}",
            ]
        righe.append("{{ log('Tabularia source ready: " + db + "', info=True) }}")
    righe.append("{% endmacro %}")
    return "\n".join(righe) + "\n"


def _impostazioni_clickhouse() -> list[str]:
    """The settings every dbt-clickhouse profile of the export sends with each query. Without
    join_use_nulls the missing side of an outer join is 0 / '' instead of NULL (the engine does
    not see it: it reads parquet, where everything is Nullable). max_query_size: every step lists
    and qualifies every column, so a wide, long flow passes ClickHouse's default 256 KiB."""
    return ["        join_use_nulls: 1", f"        max_query_size: {MAX_QUERY_SIZE}"]


def _riga_secure(predefinito: str) -> str:
    """`secure:` of a dbt-clickhouse profile: `predefinito` ('true'/'false') unless CLICKHOUSE_SECURE
    says otherwise. as_bool vuole True/False alla Python: si accetta anche come lo scrive una
    persona (true, 1, yes)."""
    return "      secure: \"{{ (env_var('CLICKHOUSE_SECURE', '" + predefinito + "') | lower in ['true', '1', 'yes']) | as_bool }}\""


def tls_per_porta(porta: Any) -> bool:
    """Whether Tabularia reaches a ClickHouse connection over TLS: its client (clickhouse-connect,
    given no scheme) uses https on the ports 8443 and 443, plain http on the others."""
    return str(porta) in ("8443", "443")


def _profilo_clickhouse(project: str, ch: dict) -> str:
    """profiles.yml del target ClickHouse: host e porta del ClickHouse del motore come
    valori di partenza, l'utente e la password di dbt SOLO da env_var. `join_use_nulls`:
    senza, il lato mancante di un join esterno è 0 / '' invece di NULL (il motore non lo
    vede perché legge parquet, dove tutto è Nullable)."""
    host = ch.get("host") or ""
    porta = ch.get("port") or (8443 if ch.get("secure") else 8123)
    sicuro = "true" if ch.get("secure") else "false"
    return "\n".join([
        f"{_y_nome(project, chiave=True)}:", "  target: dev", "  outputs:", "    dev:", "      type: clickhouse",
        # YAML first (a double-quoted scalar), then Jinja: a quote or a backslash in the host survives both
        "      host: " + _y_key("{{ env_var('CLICKHOUSE_HOST'" + (f", {_jinja_str(host)}" if host else "") + ") }}"),
        "      port: \"{{ env_var('CLICKHOUSE_PORT', '" + str(porta) + "') | as_number }}\"",
        _riga_secure(sicuro),
        "      user: \"{{ env_var('CLICKHOUSE_USER') }}\"",
        "      password: \"{{ env_var('CLICKHOUSE_PASSWORD') }}\"",
        f"      schema: {_y_nome(ch.get('target_schema') or 'dbt_tabularia')}",
        "      custom_settings:", *_impostazioni_clickhouse(),
    ]) + "\n"


def _nome_secret(a: dict) -> str:
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
    porta = c.get("port")
    return "\n".join([
        f"        - type: {_DUCK_ATTACH_TYPE[db_type]}",
        f"          name: {_nome_secret(a)}",
        f"          host: {_y(c['host'])}",
        f"          port: {porta if isinstance(porta, int) else _y(porta)}",
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
        f"          secret: {_nome_secret(a)}",
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
_NON_QUOTABILI = {
    "dbt-postgres": {"source": "\"'", "relation": "\"'", "schema": "\"'\\"},
    "dbt-duckdb": {"source": "\"'", "relation": '"', "schema": '"'},
    "dbt-clickhouse": {"source": "`\\'", "relation": "`\\'", "schema": "`\\'"},
}


def nome_quotabile(nome: Optional[str], adapter: str, dove: str = "relation") -> None:
    """Refuses a name `adapter` cannot write (see `_NON_QUOTABILI`): `dove` is source (a source's
    schema or table), relation (a model's table) or schema (a model's or the project's schema)."""
    for car in _NON_QUOTABILI[adapter][dove]:
        if nome and car in nome:
            raise DbtExportError(f"the name «{nome}» has the character {car}, which {adapter} cannot quote in a relation name",
                                 "name_not_quotable", name=nome, character=car, adapter=adapter)


def adapter_nativo(db_type: Optional[str]) -> str:
    """The dbt adapter of the native target on a connection of this type, or the refusal."""
    adapter = _NATIVE_ADAPTER.get(str(db_type or ""))
    if adapter is None:
        raise DbtExportError(f"no native dbt adapter for '{db_type}': use the federated or the ClickHouse target",
                             "no_native_adapter", db_type=db_type)
    return adapter


def _native_profile(project: str, native: dict) -> str:
    """profiles.yml per il warehouse NATIVO. Il dbt gira nel DB di origine; le
    trasformazioni sono già tradotte nel suo dialetto (sqlglot). Password via env_var."""
    db_type = native["db_type"]
    adapter = adapter_nativo(db_type)
    c = native["conn"]
    pw = "{{ env_var('%s') }}" % native["pw_env"]
    schema = native.get("target_schema") or "dbt_tabularia"
    lines = [f"{_y_nome(project, chiave=True)}:", "  target: dev", "  outputs:", "    dev:", f"      type: {adapter}"]
    # connection values are text the admin typed: quoted (YAML) and neutralised (dbt renders the profile)
    if adapter == "postgres":
        lines += [f"      host: {_y(c['host'])}", f"      port: {c['port']}", f"      user: {_y(c['username'])}",
                  f"      password: \"{pw}\"", f"      dbname: {_y(c['database'])}", f"      schema: {_y_nome(schema)}"]
    elif adapter == "clickhouse":
        lines += [f"      host: {_y(c['host'])}", f"      port: {c['port']}", f"      user: {_y(c['username'])}",
                  # TLS where Tabularia uses it (dbt-clickhouse does not infer it from the port: `secure: false`
                  # spoke plain HTTP to a TLS port 8443)
                  f"      password: \"{pw}\"", f"      schema: {_y_nome(schema)}", _riga_secure(str(tls_per_porta(c["port"])).lower()),
                  # as the ClickHouse target: the missing side of an outer join is NULL, not 0 / ''
                  "      custom_settings:", *_impostazioni_clickhouse()]
    return "\n".join(lines) + "\n"


def _y(s: Any) -> str:
    """Una stringa YAML fra virgolette doppie (le sequenze JSON sono YAML valido), per
    un VALORE: dbt lo rende come Jinja, quindi il testo dell'utente è neutralizzato.
    Jinja also drops a template's final line break (keep_trailing_newline is off: a column
    «Total⏎» was «Total» in schema.yml and its not_null test failed), so a final `\\n` is
    printed by an expression, `{{ '\\n' }}`, whose output is never trimmed."""
    t = neutralize_jinja(s)
    return _y_key(t[:-1] + "{{ '\\n' }}" if t.endswith("\n") else t)


# what JSON leaves raw but YAML refuses in a file (DEL, C1 controls, U+FFFE/FFFF, lone
# surrogates) or folds into a space inside quotes (NEL, LS, PS): written as \u escapes,
# which YAML reads back as the same character
_YAML_DA_ESCAPARE = re.compile("[\x7f-\x9f\u2028\u2029\ud800-\udfff\ufffe\uffff]")


def _y_key(s: Any) -> str:
    """A double-quoted YAML string for a mapping KEY: dbt renders values, not keys, so
    the text is written as it is (a seed's column named `a{{b` stays `a{{b`). A character
    YAML does not allow raw (U+0096 from cp1252 text read as latin-1 made `dbt parse`
    fail) is escaped, not dropped: a column name or an accepted value stays exact."""
    return _YAML_DA_ESCAPARE.sub(lambda m: "\\u%04x" % ord(m.group(0)), json.dumps(str(s), ensure_ascii=False))


# YAML 1.1 (PyYAML, so dbt) reads these plain scalars as booleans or null, in any case
_YAML_PAROLE = frozenset("y n yes no on off true false null".split())


def _y_nome(s: Any, chiave: bool = False) -> str:
    """A name (model, source, schema, table, project) in YAML: as it is when YAML can only
    read it as that string (an identifier that is not `on`, `yes`, `null`…), otherwise
    quoted — a flow «On» became the profile `True`, an output «Yes» a model named True."""
    t = str(s)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t) and t.lower() not in _YAML_PAROLE:
        return t
    return _y_key(t) if chiave else _y(t)


def _nome_progetto(flow_name: str) -> str:
    """The dbt project (and profile) name: the flow's slug, prefixed when it is the name
    of a package dbt installs itself (`dbt`, `dbt_<adapter>`): a flow «DBT» or «dbt duckdb»
    failed with «dbt found more than one package with the name …»."""
    p = _slug(flow_name)
    return f"tabularia_{p}" if p == "dbt" or p.startswith("dbt_") else p


def _schema_yaml(models: list[dict]) -> str:
    """models/schema.yml: descrizioni, colonne e test generici (dal data contract)."""
    out = ["version: 2", "", "models:"]
    for m in models:
        out.append(f"  - name: {_y_nome(m['name'])}")
        if m.get("description"):
            out.append(f"    description: {_y(m['description'])}")
        if m.get("tags") or m.get("meta"):
            # da dove viene il modello: il team data lo ritrova (e lo seleziona con tag:tabularia)
            out.append("    config:")
            if m.get("tags"):
                out.append("      tags: [" + ", ".join(_y(t) for t in m["tags"]) + "]")
            if m.get("meta"):
                out.append("      meta:")
                out.append("        tabularia:")
                for k, v in m["meta"].items():
                    if v is not None and v != "":
                        valore = ("true" if v else "false") if isinstance(v, bool) else (v if isinstance(v, (int, float)) else _y(v))
                        out.append(f"          {k}: {valore}")
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
                            # unique and accepted_values are singular tests (see test_di_colonna): dbt's own
                            # ignore NULLs and write the values as quoted text
                            raise DbtExportError(f"the column test '{t['kind']}' is written as a singular test", "internal")
                        out.append("          - not_null:")
                        out.append(f"              config: {{severity: {t.get('severity', 'error')}}}")
    return "\n".join(out) + "\n"


def _sources_yaml(sources: list[dict]) -> str:
    out = ["version: 2", "", "sources:"]
    for s in sources:
        out.append(f"  - name: {_y_nome(s['name'])}")
        if s.get("database"):
            out.append(f"    database: {_y_nome(s['database'])}")
        out.append(f"    schema: {_y_nome(s['schema'])}")
        out.append("    tables:")
        for t in sorted(s["tables"], key=lambda t: t["name"] if isinstance(t, dict) else t):
            if not isinstance(t, dict):
                out.append(f"      - name: {_y_nome(t)}")
                continue
            out.append(f"      - name: {_y_nome(t['name'])}")
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
        out.append(f"  - name: {_y_nome(s['name'])}")
        if s.get("description"):
            out.append(f"    description: {_y(s['description'])}")
        # tagged as the models: the folder package's `+tag:<flow>,tag:tabularia` still loads it
        out.append("    config:")
        out.append('      tags: ["tabularia"]')
        if s.get("column_types"):
            # i tipi DICHIARATI, non indovinati dal CSV: sono quelli con cui Tabularia legge il file
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
    parti = [f"materialized={_jinja_str(mat)}"]
    if mat == "incremental":
        parti.append("incremental_strategy='append'")
    if m.get("schema"):
        parti.append(f"schema={_jinja_str(m['schema'])}")
    if m.get("alias"):
        parti.append(f"alias={_jinja_str(m['alias'])}")
    testa = "{{ config(" + ", ".join(parti) + ") }}\n\n"
    if m.get("ai_translated"):
        testa += f"-- Translated by AI from {_commento(m.get('ai_translated_from') or 'another dialect')}: review it before use.\n\n"
    rivedere: dict[str, list[str]] = {}
    for passo, perche in m.get("da_rivedere") or []:
        rivedere.setdefault(perche, []).append(passo)
    for perche, passi in rivedere.items():
        testa += f"-- {_DA_RIVEDERE.get(perche, 'Review')}: {_commento(', '.join(passi))}.\n"
    if rivedere:
        testa += "\n"
    meta = m.get("meta") or {}
    if meta.get("flow") or meta.get("datasource"):
        # da dove viene, in testa al file: chi lo apre nel repository risale all'app. Solo
        # fatti del flusso: riesportare la stessa versione dà lo stesso file (diff pulito)
        if meta.get("flow"):
            versione = f", version {meta['flow_version']}" if meta.get("flow_version") else ""
            if meta.get("version_saved_at"):
                versione += f" saved {meta['version_saved_at']}"
            origine = f"Exported from Tabularia: flow «{meta['flow']}» (id {meta.get('flow_id')}{versione})"
            if meta.get("output"):
                origine += f", {meta['output']}"
        else:
            origine = f"Exported from Tabularia: the SQL query of the datasource «{meta['datasource']}» (id {meta.get('datasource_id')})"
        testa += f"-- {_commento(origine)}.\n"
        testa += (f"-- Built by {_commento(meta['owner'])}. " if meta.get("owner") else "-- ") + \
            "Re-exporting the same version gives this same file.\n\n"
    return testa


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
    esportato_da: str | None = None, ai: dict | None = None,
) -> dict[str, str]:
    """Assembla i file di un progetto dbt.

    `models`: [{name, sql, materialized, description, columns, schema?, alias?}].
    `sources`: [{name, database, schema, tables: [{name, description, columns}]}].
    `seeds`: [{name, csv, description, columns}]. `tests`: [{name, sql, severity, description}].
    Modalità:
    · **duckdb** (federazione): `attachments` = [{alias, db_type, conn, pw_env}].
    · **native** (warehouse): `native` = {db_type, conn, pw_env, target_schema}.
    · **clickhouse** (il ClickHouse del cliente, sorgenti federate): `clickhouse` =
      {host, port, secure, target_schema, federati: [{name, engine, conn, schema}]}.
    `layout` (dialogo di download): {package: project|folder, folder, layers}. Una
    CARTELLA per un progetto esistente non ha profilo né dbt_project.yml: ha un
    INTEGRATION.md che dice cosa copiare e cosa aggiungere al loro. `schema`: lo
    schema dei modelli nel profilo dbt-duckdb (gli altri target lo hanno già)."""
    project = _nome_progetto(flow_name)
    files: dict[str, str] = {}
    seeds, tests, notes = seeds or [], tests or [], list(notes or [])
    dove = _Disposizione(layout)

    if clickhouse is not None:
        files["profiles.yml"] = _profilo_clickhouse(project, clickhouse)
        if clickhouse.get("federati"):
            files[dove.macro("create_tabularia_sources")] = _macro_sorgenti_clickhouse(clickhouse["federati"])
    elif native:
        files["profiles.yml"] = _native_profile(project, native)
    else:
        extensions = sorted({_DUCK_ATTACH_TYPE[a["db_type"]] for a in (attachments or []) if a["db_type"] in _FEDERABLE})
        segreti = "\n".join(_duck_secret(a) for a in (attachments or []))
        attach = "\n".join(_duck_attach(a) for a in (attachments or []))
        files["profiles.yml"] = (
            f"{_y_nome(project, chiave=True)}:\n"
            f"  target: dev\n"
            f"  outputs:\n"
            f"    dev:\n"
            f"      type: duckdb\n"
            f"      path: {project}.duckdb\n"
            + (f"      schema: {_y_nome(schema)}\n" if schema else "")
            + _DUCK_SETTINGS
            + (f"      extensions: [{', '.join(extensions)}]\n" if extensions else "")
            + (f"      secrets:\n{segreti}\n      attach:\n{attach}\n" if attach else "")
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
        f"models:\n  {_y_nome(project, chiave=True)}:\n    +materialized: table\n"
    )

    # quelle «già dichiarate» le ha il loro sources.yml: only a folder copied into their project has one
    nostre = sorgenti_del_pacchetto(sources, layout)
    nostre = [x for x in nostre if not x.get("declared")]
    if nostre:
        files[dove.modelli("sources.yml")] = _sources_yaml(nostre)
    files[dove.modelli("schema.yml")] = _schema_yaml(models)
    for m in models:
        # m["sql"] is final dbt SQL: user text already neutralised, the exporter's macros in place
        files[dove.modello(m)] = _model_header(m) + m["sql"] + "\n"
    if any(m.get("schema") for m in models) and not dove.cartella:
        # nella cartella per un progetto esistente no: cambierebbe i nomi degli schemi di TUTTO il loro progetto
        files[dove.macro("generate_schema_name")] = _SCHEMA_MACRO
    for s in seeds:
        files[dove.seed(f"{_slug(s['name'])}.csv")] = s["csv"]
    if seeds:
        files[dove.seed("schema.yml")] = _seeds_yaml(seeds)
    for t in tests:
        files[dove.test(f"{_slug(t['name'])}.sql")] = (
            f"{{{{ config(severity={_jinja_str(t.get('severity', 'error'))}) }}}}\n"
            + (f"-- {_commento(t['description'])}\n" if t.get("description") else "")
            + t["sql"] + "\n"
        )

    preparazione = dove_finiscono = ""
    if clickhouse is not None:
        federati = clickhouse.get("federati") or []
        intro = (
            f"Generated by Tabularia from the flow **{flow_name}**. Targets **dbt-clickhouse** on your\n"
            "ClickHouse: the source databases are read live through ClickHouse's PostgreSQL/MySQL table\n"
            "engines (one database `tab_src_<connection>_<schema>` per source schema, one table per source\n"
            "table), and from there on everything runs in ClickHouse.\n"
        )
        pip = "pip install dbt-clickhouse"
        pw_envs = ["CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"] + list(dict.fromkeys(f["conn"]["pw_env"] for f in federati))
        if federati:
            preparazione = ("DBT_PROFILES_DIR=. dbt run-operation create_tabularia_sources --log-level-file none"
                            "   # once, and again when a source table changes its columns\n")
            raggiunge = ", ".join(sorted({f"`{f['conn']['host']}:{f['conn'].get('port') or (5432 if f['engine'] == 'PostgreSQL' else 3306)}`" for f in federati}))
            notes = [
                f"ClickHouse must reach the source databases ({raggiunge}): it reads them itself, dbt does not.",
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
            porta = (native.get("conn") or {}).get("port")
            notes = [f"The profile reaches ClickHouse on port {porta} " + ("over TLS (`secure: true`), as Tabularia does on 8443 and 443"
                     if tls_per_porta(porta) else "without TLS, as Tabularia does on a port other than 8443 and 443")
                     + ": `CLICKHOUSE_SECURE=true|false` changes it."] + notes
    else:
        intro = (
            f"Generated by Tabularia from the flow **{flow_name}**. Targets **dbt-duckdb**, which\n"
            "attaches the origin database(s) and reads the tables live (no snapshots, no dialect translation).\n"
        )
        pip = "pip install dbt-duckdb"
        pw_envs = [a["pw_env"] for a in (attachments or [])]
        # where they land, said: whoever queried dbt_tabularia.<model> in the file found no such schema
        dove_finiscono = (f"The models are built in `{project}.duckdb`, in the schema `{schema or 'main'}`"
                          + ("" if schema else " (dbt-duckdb's default: the profile names no schema)") + ".\n\n")
    cosa = []
    for m in models:
        cosa.append(f"- `{m['name']}` ({m.get('materialized', 'table')}): {m.get('description') or ''}".rstrip(": "))
    passi = "".join(f"export {e}=...\n" for e in pw_envs) + preparazione
    if seeds:
        passi += "DBT_PROFILES_DIR=. dbt seed\n"
    passi += "DBT_PROFILES_DIR=. dbt run\n"
    if tests or any(c.get("tests") for m in models for c in (m.get("columns") or [])):
        passi += "DBT_PROFILES_DIR=. dbt test\n"
    provenienza = _provenienza(models, esportato_da) + _sezione_ai(ai)
    if dove.cartella:
        files.pop("profiles.yml", None)
        files["INTEGRATION.md"] = _integrazione(
            flow_name, intro, provenienza, flow_description, cosa, models, sources, seeds, tests, notes, dove,
            attachments=attachments, native=native, clickhouse=clickhouse, pip=pip, pw_envs=pw_envs, preparazione=preparazione,
        )
        return files
    files["dbt_project.yml"] = dbt_project_yml
    files["README.md"] = (
        f"# dbt project — {flow_name}\n\n{intro}\n" + dove_finiscono + provenienza
        + (f"{flow_description}\n\n" if flow_description else "")
        + "## Models\n\n" + "\n".join(cosa) + "\n\n"
        + "## Run\n\n```bash\n" + pip + "\n"
        + "# set the source DB password(s) (referenced via env_var in profiles.yml):\n"
        + passi + "```\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n" if notes else "")
    )
    return files


def sorgenti_del_pacchetto(sources: list[dict], layout: dict | None) -> list[dict]:
    """«Already declared» means the team's own sources.yml declares the source: only the folder
    package goes into their project. A complete project ignores it (decision 10): it left the
    source out of its sources.yml and `dbt parse` failed («depends on a source … not found»)."""
    if _Disposizione(layout).cartella:
        return sources
    return [{k: v for k, v in x.items() if k != "declared"} for x in sources]


class _Disposizione:
    """Dove va ogni file. Il progetto completo senza opzioni: come sempre (models/,
    seeds/, tests/, macros/). Con una cartella: models/<cartella>/… (e così seeds,
    tests, macros), che si copia dentro il progetto del team. Con i livelli: i
    modelli delle query in staging/, i passi condivisi in intermediate/, le uscite in marts/."""

    _LIVELLO = {"raw": "staging", "intermediate": "intermediate", "output": "marts"}

    def __init__(self, layout: dict | None):
        layout = layout or {}
        cartella = layout.get("folder") or ""
        if not re.fullmatch(r"[a-z0-9_]{0,40}", cartella):
            raise DbtExportError(f"invalid folder name: «{cartella}»", "folder_invalid", folder=cartella)
        self.pacchetto = layout.get("package") or "project"
        self.cartella = self.pacchetto == "folder"
        self.sotto = f"/{cartella}" if cartella else ""
        self.livelli = bool(layout.get("layers"))

    def modelli(self, nome: str) -> str:
        return f"models{self.sotto}/{nome}"

    def modello(self, m: dict) -> str:
        livello = f"{self._LIVELLO.get(m.get('kind') or 'output', 'marts')}/" if self.livelli else ""
        return self.modelli(f"{livello}{_slug(m['name'])}.sql")

    def seed(self, nome: str) -> str:
        return f"seeds{self.sotto}/{nome}"

    def test(self, nome: str) -> str:
        return f"tests{self.sotto}/{nome}"

    def macro(self, nome: str) -> str:
        return f"macros{self.sotto}/{nome}.sql"


def _provenienza(models: list[dict], esportato_da: str | None = None) -> str:
    origini = {}
    for m in models:
        mt = m.get("meta") or {}
        if mt.get("flow_id") is not None:
            origini.setdefault(mt["flow_id"], mt)
    if not origini:
        return ""
    return "## Where it comes from\n\n" + "\n".join(
        f"- Tabularia flow «{mt['flow']}» (id {mt['flow_id']}, version {mt.get('flow_version')}"
        + (f", saved {mt['version_saved_at']}" if mt.get("version_saved_at") else "") + ")"
        + (f", built by {mt['owner']}" if mt.get("owner") else "")
        for mt in origini.values()
    ) + "\n\n" + (f"Exported by {esportato_da}. " if esportato_da else "") + \
        "Every model carries the same in `schema.yml` (`config.meta.tabularia`) and is tagged `tabularia`: `dbt run -s tag:tabularia` runs them all.\n\n"


def _sezione_ai(ai: dict | None) -> str:
    """Cosa ha scritto l'AI (dialogo di download): il paragrafo di ogni flusso e
    l'avvertenza sulle descrizioni marcate «(AI)» e sui modelli tradotti."""
    if not ai:
        return ""
    parti = []
    riassunti = ai.get("summaries") or []
    if riassunti:
        parti.append("## What the flow does\n\n" + "\n\n".join(
            (f"**{r['flow']}** — " if len(riassunti) > 1 else "") + r["text"] for r in riassunti) + "\n")
    tradotti = ai.get("translated") or []
    avvisi = []
    if riassunti or ai.get("described"):
        avvisi.append(f"Descriptions that start with `(AI)` were written by an AI model (`{ai.get('model')}`) from the structure "
                      "of the flow and a few sample values: review them before relying on them.")
    if tradotti:
        avvisi.append("These models were translated by an AI model and passed Tabularia's checks (one SELECT, the same "
                      "output columns, only the tables they may read), but nobody has run them yet: "
                      + ", ".join(f"`{m}`" for m in tradotti) + ". Review them.")
    if avvisi:
        parti.append("\n".join(f"- {a}" for a in avvisi) + "\n")
    return "\n".join(parti) + ("\n" if parti else "")


def _tag_del_flusso(flow_name: str, models: list[dict]) -> str:
    for m in models:
        if (m.get("meta") or {}).get("flow") == flow_name and len(m.get("tags") or []) > 1:
            return m["tags"][1]
    return _slug(flow_name)


def _integrazione(flow_name, intro, provenienza, flow_description, cosa, models, sources, seeds, tests, notes, dove, *,
                  attachments, native, clickhouse, pip, pw_envs, preparazione) -> str:
    """INTEGRATION.md della cartella per un progetto esistente: cosa copiare, cosa
    serve nel loro profilo, quali sorgenti devono già dichiarare, come lanciarla."""
    tag = _tag_del_flusso(flow_name, models)
    copia = [f"- `models{dove.sotto}/` — {len(models)} models, `schema.yml`" + (", `sources.yml`" if any(not x.get("declared") for x in sources) else "")]
    if seeds:
        copia.append(f"- `seeds{dove.sotto}/` — {len(seeds)} seeds (with their column types)")
    if tests:
        copia.append(f"- `tests{dove.sotto}/` — {len(tests)} singular tests from the data contract")
    if clickhouse is not None and clickhouse.get("federati"):
        copia.append(f"- `macros{dove.sotto}/` — `create_tabularia_sources`, which creates the source databases in ClickHouse")
    impostazioni = ("```yaml\n      custom_settings:\n" + "\n".join(_impostazioni_clickhouse()) + "\n```\n")
    if clickhouse is not None:
        profilo = ("Your **dbt-clickhouse** output needs `join_use_nulls` (without it the missing side of an outer join is "
                   "0 or `''` instead of NULL):\n\n" + impostazioni)
    elif native:
        c = native.get("conn") or {}
        profilo = (f"Your **dbt-{_NATIVE_ADAPTER[native['db_type']]}** profile on the {native['db_type']} database "
                   f"`{c.get('database')}` at `{c.get('host')}:{c.get('port')}`: the models run there, next to the data.\n")
        if native["db_type"] == "clickhouse":
            profilo += ("\nIt needs `join_use_nulls` (without it the missing side of an outer join is 0 or `''` instead of "
                        "NULL):\n\n" + impostazioni)
    else:
        segreti = "\n".join(_duck_secret(a) for a in (attachments or []))
        attach = "\n".join(_duck_attach(a) for a in (attachments or []))
        profilo = ("Your **dbt-duckdb** profile must attach the source databases with these aliases "
                   "(the sources read `<alias>.<schema>.<table>`). The passwords go through DuckDB secrets, not the "
                   "ATTACH path, so a failed connection does not print them:\n\n```yaml\n      secrets:\n" + segreti
                   + "\n      attach:\n" + attach + "\n```\n") if attach else \
            "Your **dbt-duckdb** profile: this flow reads no database.\n"
        profilo += ("\nIts session must be in UTC, as Tabularia's engine is: otherwise what the flow does with time zones "
                    "follows the time zone of the machine that runs dbt.\n\n```yaml\n" + _DUCK_SETTINGS + "```\n")
    dichiarate = [x for x in sources if x.get("declared")]
    righe_sorgenti = [f"- `{x['name']}` is **declared in your project**: it must list the tables "
                      + ", ".join(f"`{t['name']}`" for t in sorted(x["tables"], key=lambda t: t["name"])) + "."
                      for x in dichiarate]
    if any(not x.get("declared") for x in sources):
        righe_sorgenti.append(f"- The others are declared in `models{dove.sotto}/sources.yml`.")
    con_schema = [m for m in models if m.get("schema")]
    note = list(notes) + [
        f"The model `{m['name']}` has `schema='{m['schema']}'`: with dbt's default naming it lands in "
        f"`<your schema>_{m['schema']}`. The complete project ships a `generate_schema_name` macro that writes into "
        f"`{m['schema']}` itself; this folder does not, because it would rename the schemas of your whole project."
        for m in con_schema
    ]
    return (
        f"# Add «{flow_name}» to your dbt project\n\n{intro}\n" + provenienza
        + (f"{flow_description}\n\n" if flow_description else "")
        + "## 1. Copy\n\nCopy these folders into your project as they are (dbt reads every subfolder of its "
        "model, seed, test and macro paths):\n\n" + "\n".join(copia) + "\n\n"
        + "## 2. Your profile\n\n" + profilo + "\n"
        + ("## 3. Sources\n\n" + "\n".join(righe_sorgenti) + "\n\n" if righe_sorgenti else "")
        + "## Run\n\n```bash\n" + pip + "\n" + "".join(f"export {e}=...\n" for e in pw_envs)
        + preparazione.replace("DBT_PROFILES_DIR=. ", "")
        + f"dbt build -s +tag:{tag},tag:tabularia\n```\n\n"
        f"`+tag:{tag},tag:tabularia` selects the models of the flow and everything they read (upstream flows, seeds), with "
        f"their tests: only what came from Tabularia, so a model of yours tagged `{tag}` is not built, nor what it reads.\n\n"
        + "## Models\n\n" + "\n".join(cosa) + "\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in note) + "\n" if note else "")
    )


def _slug(name: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(name).lower())
    out = out.strip("_") or "model"
    return out if not out[0].isdigit() else f"m_{out}"
