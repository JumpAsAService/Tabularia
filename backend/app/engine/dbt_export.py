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
all'unpivot (supertipo comune).

`foreach` non è traducibile (control-flow a runtime) → errore esplicito; `pivot`
solo nel target federato.
"""
from __future__ import annotations

import json
import logging
import re
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
from app.engine.operations import PIVOT_LABEL_SEP

# resolver di una sorgente ({key} | {model} | {seed}) → (ref SQL, colonne, tipi DuckDB per colonna)
logger = logging.getLogger(__name__)
SourceResolver = Callable[[dict], tuple[str, list[str], dict]]
SEED_MAX_ROWS = 50_000          # un seed dbt è un CSV nel repository: oltre, non ha senso


class DbtExportError(EngineError):
    """Un rifiuto dell'export. Il testo (italiano) resta nei log; `code` e `params`
    vanno al gateway, che lo dice nella lingua di chi lo legge."""

    def __init__(self, messaggio: str, code: str = "internal", **params):
        super().__init__(messaggio)
        self.code = code
        self.params = params


# ── tipi ─────────────────────────────────────────────────────────────────────
_POLARS_INT = {"Int8": "TINYINT", "Int16": "SMALLINT", "Int32": "INTEGER", "Int64": "BIGINT",
               "UInt8": "BIGINT", "UInt16": "BIGINT", "UInt32": "BIGINT", "UInt64": "BIGINT"}


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


_TESTO = ("VARCHAR", "STRING", "TEXT", "CHAR")
_NUMERI = ("DOUBLE", "FLOAT", "REAL", "DECIMAL")
_INTERI = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "INT")


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
    """Lo schema corrente di un modello: colonne in ordine e il loro tipo DuckDB (o None)."""

    def __init__(self, cols: list[str], types: Optional[dict] = None):
        self.cols = list(cols)
        self.types = {c: (types or {}).get(c) for c in self.cols}

    def con(self, cols: list[str], types: dict) -> "_S":
        return _S(cols, types)


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
    for a, b in (("*/", "* /"), ("{{", "{ {"), ("}}", "} }"), ("{%", "{ %"), ("%}", "% }"), ("{#", "{ #"), ("#}", "# }")):
        t = t.replace(a, b)
    return t[:200]


class _Model:
    """Accumula le CTE di un modello. Ogni CTE ha un NOME che dice cosa fa
    (`filter_stato`, `aggregate_by_categoria`) e una NOTA in parole: chi rivede il
    progetto in una pull request deve capire il passo senza aprire Tabularia."""

    def __init__(self, namer: _Namer, native: bool = False, dialetto: str | None = None):
        self.namer = namer
        self.native = native
        self.dialetto = dialetto   # il dialetto di arrivo, dove una forma dipende dal warehouse
        self.ctes: list[tuple[str, str, str]] = []  # (nome, sql, nota)
        self.etichette: list[tuple[str, str]] = []   # (nome, nota) del passo che si sta compilando
        self._usati: set[str] = set()

    def _unico(self, base: str) -> str:
        nome, n = _slug_passo(base), 2
        while nome in self._usati:
            nome = f"{_slug_passo(base, 36)}_{n}"
            n += 1
        self._usati.add(nome)
        return nome

    def add(self, sql: str, nome: Optional[str] = None, nota: Optional[str] = None) -> str:
        if nome is None and self.etichette:
            nome, nota_passo = self.etichette[-1]
            nota = nota if nota is not None else nota_passo
        name = self._unico(nome) if nome else self.namer.next()
        self.ctes.append((name, sql, _commento(nota or "")))
        return name


# ── forme portabili per il target nativo ─────────────────────────────────────
_RE_INTERO = r"^[+-]?[0-9]+$"
_RE_NUMERO = r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$"
_RE_DATA = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_RE_ISTANTE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{2}:[0-9]{2}(:[0-9]{2}([.][0-9]+)?)?)?$"


def _cast_portabile(q: str, src: Optional[str], duck_t: str) -> str:
    """Lo stesso cast del motore (fallito → NULL; testo con spazi tagliati; testo →
    intero solo se intero letterale; numero → intero per troncamento) senza
    TRY_CAST, che i warehouse non hanno: un'espressione regolare decide prima se il
    testo è convertibile, così un valore sbagliato dà NULL invece di far fallire il modello."""
    fam = _famiglia(src)
    if fam == "testo":
        t = f"trim({q})"
        guardia = {"BIGINT": _RE_INTERO, "DOUBLE": _RE_NUMERO, "DATE": _RE_DATA, "TIMESTAMP": _RE_ISTANTE}.get(duck_t)
        if guardia:
            return f"CASE WHEN regexp_matches({t}, {_lit(guardia)}) THEN CAST({t} AS {duck_t}) END"
        if duck_t == "BOOLEAN":
            return (f"CASE WHEN lower({t}) IN ('true', 't', '1') THEN TRUE "
                    f"WHEN lower({t}) IN ('false', 'f', '0') THEN FALSE END")
        return f"CAST({t} AS {duck_t})"
    if duck_t == "BIGINT" and fam == "numero":
        return f"CAST(trunc({q}) AS BIGINT)"
    return f"CAST({q} AS {duck_t})"


def _predicato_portabile(column: str, operator: str, value: Any) -> str:
    """Come `duckdb_ops._predicate`, ma senza contains/starts_with/ends_with, che
    non tutti i dialetti hanno."""
    c = _qi(column)
    if operator in ("contains", "starts_with", "ends_with"):
        v, testo = str(value), f"CAST({c} AS VARCHAR)"
        if operator == "contains":
            return f"strpos({testo}, {_lit(v)}) > 0"
        return f"{'left' if operator == 'starts_with' else 'right'}({testo}, {len(v)}) = {_lit(v)}"
    return _predicate(column, operator, value)


def _riscrivi_query(query: str, ref: str) -> str:
    """Il nodo sql legge l'input come `self` o `input`: si puntano quei nomi alla
    CTE del passo precedente. La query resta una sottoquery, quindi può avere il
    suo WITH."""
    import sqlglot
    from sqlglot import exp

    try:
        albero = sqlglot.parse_one(query, read="duckdb")
    except Exception as e:  # noqa: BLE001
        raise DbtExportError(f"sql: la query non si legge ({e})", "sql_unreadable", detail=str(e)[:200])
    cte = {c.alias_or_name.lower() for c in albero.find_all(exp.CTE)}
    for t in albero.find_all(exp.Table):
        nome = t.name.lower()
        if not t.args.get("db") and nome in ("self", "input") and nome not in cte:
            if not t.alias:
                t.set("alias", exp.TableAlias(this=exp.to_identifier(t.name)))
            t.set("this", exp.to_identifier(ref))
    return albero.sql(dialect="duckdb")


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
            f"{cosa}: le colonne non sono note a questo punto del flusso (dopo un nodo sql): "
            "nell'export nativo servono. Usa l'export «federato», oppure fai elencare le colonne al nodo sql.",
            "columns_unknown_after_sql", step=cosa,
        )
    return True


def _colonne_della_query(query: str, ref: str, s: "_S") -> list[str]:
    """Le colonne che un nodo sql restituisce, ricavate dalla query e dalle colonne
    del passo prima (`*` compreso). Servono ai passi successivi, e nel nativo alle
    liste esplicite. Se non si ricavano: [] (i passi dopo useranno `*`)."""
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


# ── le operazioni ────────────────────────────────────────────────────────────
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
        return model.add(f"SELECT {sel} FROM {ref}"), _S(c, types)

    if op_type == "reorder":
        order = _require(params, "columns")
        nuove = list(order) + [c for c in cols if c not in set(order)]
        if _serve_lista(model, s, "reorder"):
            return model.add(f"SELECT {_lista(nuove)} FROM {ref}"), _S(nuove, types)
        qs = ", ".join(q(x) for x in order)
        return model.add(f"SELECT {qs}, * EXCLUDE ({qs}) FROM {ref}"), _S(nuove, types)

    if op_type == "drop":
        drop = set(_require(params, "columns"))
        restano = [x for x in cols if x not in drop]
        if _serve_lista(model, s, "drop"):
            return model.add(f"SELECT {_lista(restano)} FROM {ref}"), _S(restano, types)
        sql = f"SELECT * EXCLUDE ({', '.join(q(x) for x in drop)}) FROM {ref}"
        return model.add(sql), _S(restano, types)

    if op_type == "rename":
        mapping = _require(params, "mapping")
        nuove = [mapping.get(x, x) for x in cols]
        nt = {mapping.get(k, k): v for k, v in types.items()}
        if _serve_lista(model, s, "rename"):
            return model.add(f"SELECT {_lista(cols, nomi=mapping)} FROM {ref}"), _S(nuove, nt)
        pairs = ", ".join(f"{q(o)} AS {q(n)}" for o, n in mapping.items())
        return model.add(f"SELECT * RENAME ({pairs}) FROM {ref}"), _S(nuove, nt)

    if op_type == "cast":
        casts = _require(params, "columns")
        repls, nuovi = [], dict(types)
        for col, dt in casts.items():
            duck_t = _DUCK_DTYPES.get(str(dt))
            if not duck_t:
                raise DbtExportError(f"cast: tipo non supportato '{dt}'", "cast_unsupported_type", type=dt)
            src = types.get(col)
            espr = _cast_portabile(q(col), src, duck_t) if nat else _cast_expr(q(col), src or "", duck_t)
            repls.append((col, espr))
            nuovi[col] = duck_t
        if _serve_lista(model, s, "cast"):
            return model.add(f"SELECT {_lista(cols, espr=dict(repls))} FROM {ref}"), _S(cols, nuovi)
        return model.add(f"SELECT * REPLACE ({', '.join(f'{e} AS {q(c)}' for c, e in repls)}) FROM {ref}"), _S(cols, nuovi)

    if op_type == "filter":
        col, operator, value = _require(params, "column"), params.get("operator", "eq"), params.get("value")
        pred = _predicato_portabile(col, operator, value) if nat else _predicate(col, operator, value)
        return model.add(f"SELECT {tutte} FROM {ref} WHERE {pred}"), s

    if op_type == "sort":
        by = _as_list(_require(params, "by"))
        if params.get("ignore_missing") and cols:
            by = [c for c in by if c in set(cols)]
            if not by:
                return ref, s
        direction = "DESC" if params.get("descending") else "ASC"
        # standard cross-engine: NULL sempre in coda (come il motore)
        order = ", ".join(f"{q(c)} {direction} NULLS LAST" for c in by)
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
            raise DbtExportError("drop_nulls: colonne non note a questo punto del flusso (dopo un nodo sql o un pivot): indicale nel nodo", "drop_nulls_columns_unknown")
        cond = " AND ".join(f"{q(c)} IS NOT NULL" for c in subset)
        return model.add(f"SELECT {tutte} FROM {ref} WHERE {cond}"), s

    if op_type == "group_by":
        by = _as_list(_require(params, "by"))
        aggs = _require(params, "aggregations")
        parts, out_cols, out_t = [", ".join(q(c) for c in by)], list(by), {c: types.get(c) for c in by}
        for agg in aggs:
            col, func = agg["column"], agg.get("func", "sum")
            if func not in _AGG:
                raise DbtExportError(f"group_by: funzione non supportata '{func}'", "groupby_unsupported_func", func=func)
            alias = agg.get("alias") or f"{col}_{func}"
            expr = f"count(DISTINCT {q(col)})" if func == "n_unique" else f"{_AGG[func]}({q(col)})"
            if func in ("std", "var") and model.dialetto == "clickhouse":
                # ClickHouse dà NaN con un solo valore: NULL, come il motore (e come DuckDB/Postgres)
                expr = f"CASE WHEN count({q(col)}) > 1 THEN {expr} END"
            fam = _famiglia(types.get(col))
            if func in ("count", "n_unique"):
                t = "BIGINT"
            elif func in ("mean", "median", "std", "var"):
                t = "DOUBLE"
            elif func == "sum" and fam == "intero":
                # come il motore: la somma di interi torna BIGINT (DuckDB darebbe HUGEINT)
                expr, t = f"CAST({expr} AS BIGINT)", "BIGINT"
            else:
                t = types.get(col)
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
                raise DbtExportError("compute: nome ed espressione sono obbligatori", "compute_missing")
            # niente controllo «solo espressioni scalari» qui: nel motore protegge il
            # NOSTRO processo (l'espressione gira dentro Tabularia); il progetto dbt
            # gira nell'ambiente di chi lo esporta, e deve riprodurre il flusso com'è
            # (quel controllo, a parole, rifiutava anche EXTRACT(YEAR FROM …))
            if nat and not out_cols:
                _serve_lista(model, _S([]), "compute")
            passo = dict(nome=f"compute_{name}", nota=f"{name} = {expr}" + (" (replaces the column, in place)" if name in out_cols else ""))
            if name in out_cols:
                # colonna esistente: sovrascritta NELLA SUA POSIZIONE (come il motore)
                if nat:
                    cur = model.add(f"SELECT {_lista(out_cols, espr={name: f'({expr})'})} FROM {cur}", **passo)
                else:
                    cur = model.add(f"SELECT * REPLACE (({expr}) AS {q(name)}) FROM {cur}", **passo)
            else:
                cur = model.add(f"SELECT {_lista(out_cols) if nat else '*'}, ({expr}) AS {q(name)} FROM {cur}", **passo)
                out_cols.append(name)
            out_t[name] = None
        return cur, _S(out_cols, out_t)

    if op_type == "sql":
        query = _riscrivi_query(str(_require(params, "query")).strip(), ref)
        uscita = _colonne_della_query(query, ref, s)
        sel = _lista(uscita) if (nat and uscita) else "*"
        return model.add(f"SELECT {sel} FROM ({query}) AS _q"), _S(uscita)

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
                return model.add(f"SELECT * FROM {ref} UNION ALL BY NAME SELECT * FROM {r_ref}"), _S(merged, mt)
            if not cols or not rs.cols:
                raise DbtExportError("union per nome: le colonne di uno dei due rami non sono note (dopo un nodo sql o un pivot): nell'export nativo serve saperle",
                                     "union_columns_unknown")
            lato = lambda have: ", ".join(q(c) if c in set(have) else f"NULL AS {q(c)}" for c in merged)
            return model.add(f"SELECT {lato(cols)} FROM {ref} UNION ALL SELECT {lato(rs.cols)} FROM {r_ref}"), _S(merged, mt)
        how = params.get("how", "inner")
        lset = set(cols)
        if how == "cross":
            sel = _join_select(ref, cols, r_ref, rs.cols, set())
            out = cols + [c if c not in lset else c + "_right" for c in rs.cols]
            ot = {**types, **{(c if c not in lset else c + "_right"): rs.types.get(c) for c in rs.cols}}
            return model.add(f"SELECT {sel} FROM {ref} CROSS JOIN {r_ref}"), _S(out, ot)
        clause, kind = _join_condition(ref, r_ref, params)
        if how in ("semi", "anti"):
            sel = ", ".join(f"{ref}.{q(c)} AS {q(c)}" for c in cols) if (nat and cols) else f"{ref}.*"
            if nat and model.dialetto == "clickhouse":
                # ClickHouse non ha EXISTS correlato; ha LEFT SEMI/ANTI JOIN, gli stessi del motore
                return model.add(f"SELECT {sel} FROM {ref} LEFT {how.upper()} JOIN {r_ref} {clause}"), s
            if nat:
                # SEMI/ANTI JOIN non esistono negli altri warehouse: EXISTS / NOT EXISTS
                cond = clause[len("ON "):] if clause.startswith("ON ") else " AND ".join(
                    f"{ref}.{q(k)} = {r_ref}.{q(k)}" for k in _as_list(params["on"]))
                neg = "NOT " if how == "anti" else ""
                return model.add(f"SELECT {sel} FROM {ref} WHERE {neg}EXISTS (SELECT 1 FROM {r_ref} WHERE {cond})"), s
            return model.add(f"SELECT {sel} FROM {ref} {how.upper()} JOIN {r_ref} {clause}"), s
        if how not in _JOIN_KW:
            raise DbtExportError(f"join: tipo non supportato '{how}'", "join_unsupported", how=how)
        skip_right = set(_as_list(params["on"])) if kind == "using" else set()
        # nei right/full join le chiavi USING sono COALESCE(sinistra, destra), come nel motore
        sel = _join_select(ref, cols, r_ref, rs.cols, skip_right, skip_right if how in ("full", "right") else set())
        dest = [c for c in rs.cols if c not in skip_right]
        out = list(cols) + [(c + "_right" if c in lset else c) for c in dest]
        ot = {**types, **{(c + "_right" if c in lset else c): rs.types.get(c) for c in dest}}
        return model.add(f"SELECT {sel} FROM {ref} {_JOIN_KW[how]} {r_ref} {clause}"), _S(out, ot)

    if op_type == "pivot":
        if nat:
            raise DbtExportError(
                "Il nodo «pivot» produce colonne decise dai dati a runtime: in dbt si rende "
                "solo con il PIVOT di DuckDB. Usa l'export «federato» (dbt-duckdb), oppure "
                "sostituisci il pivot con un nodo sql che elenchi le colonne.", "pivot_not_portable"
            )
        index = _as_list(_require(params, "index"))
        on = _as_list(_require(params, "on"))
        values = _require(params, "values")
        func = params.get("func", "sum")
        if func not in _AGG:
            raise DbtExportError(f"pivot: funzione non supportata '{func}'", "pivot_unsupported_func", func=func)
        # etichetta standard cross-engine, come il motore: valore come testo, NULL → 'null',
        # più colonne unite dal separatore
        on_sql = f" || '{PIVOT_LABEL_SEP}' || ".join(f"coalesce(CAST({q(c)} AS VARCHAR), 'null')" for c in on)
        idx_sql = ", ".join(q(c) for c in index)
        return model.add(f"PIVOT {ref} ON {on_sql} USING {_AGG[func]}({q(values)}) GROUP BY {idx_sql}"), _S([])

    if op_type == "unpivot":
        on = params.get("on")
        index = params.get("index") or []
        var = params.get("variable_name") or "variable"
        val = params.get("value_name") or "value"
        melt = on if on else [c for c in cols if c not in set(index)]
        if not melt:
            raise DbtExportError("unpivot: nessuna colonna da sciogliere", "unpivot_nothing")
        tipi = [types.get(c) for c in melt]
        vtype = unpivot_value_type([t for t in tipi]) if all(tipi) else "VARCHAR"
        keep = [c for c in cols if c in set(index)] if cols else list(index)
        # forma portabile, identica nei due target: una SELECT per colonna sciolta, NULL inclusi
        # (come il motore, che usa UNPIVOT INCLUDE NULLS)
        pezzi = [
            "SELECT " + ", ".join([q(c) for c in keep] + [f"{_lit(m)} AS {q(var)}", f"CAST({q(m)} AS {vtype}) AS {q(val)}"]) + f" FROM {ref}"
            for m in melt
        ]
        out = keep + [var, val]
        return model.add(" UNION ALL ".join(pezzi)), _S(out, {**{c: types.get(c) for c in keep}, var: "VARCHAR", val: vtype})

    if op_type == "foreach":
        raise DbtExportError(
            "Il nodo «foreach» itera a runtime su una tabella driver e non ha un "
            "equivalente dbt: escludilo dal flusso o esporta i rami separatamente.", "foreach_not_exportable"
        )

    raise DbtExportError(f"operazione '{op_type}' non esportabile in dbt", "op_not_exportable", op=op_type)


def _risolvi(resolve: SourceResolver, spec: dict) -> tuple[str, list[str], dict, str]:
    """(ref, colonne, tipi, nome leggibile). Il nome viene dal resolver se lo dà,
    altrimenti dal riferimento dbt stesso (`source('…', 'ordini')` → ordini)."""
    r = resolve(spec)
    ref, cols, tipi = r[0], r[1], r[2]
    nome = r[3] if len(r) > 3 and r[3] else None
    if not nome:
        m = re.search(r"source\('[^']*', *'([^']*)'\)|ref\('([^']*)'\)", ref)
        nome = (m.group(1) or m.group(2)) if m else (spec.get("model") or spec.get("seed") or "source")
    return ref, cols, tipi, nome


def _base(model: _Model, src_ref: str, s: _S, nome: str = "source", destra: bool = False) -> str:  # noqa: C901
    """Il primo passo di un modello: la sorgente con i TIPI che Tabularia le ha dato
    all'import, così il progetto vede i dati come il flusso (es. un numeric del
    warehouse che Tabularia legge come testo)."""
    passo = dict(nome=f"{'right_' if destra else ''}source_{nome}",
                 nota=f"{'right side, ' if destra else ''}source: {nome}" + (", with the column types Tabularia reads" if s.cols and all(s.types.get(c) for c in s.cols) else ""))
    if s.cols and all(s.types.get(c) for c in s.cols):
        proj = ", ".join(f"CAST({_qi(c)} AS {s.types[c]}) AS {_qi(c)}" for c in s.cols)
        return model.add(f"SELECT {proj} FROM {src_ref}", **passo)
    if model.native and s.cols:
        return model.add(f"SELECT {_lista(s.cols)} FROM {src_ref}", **passo)
    return model.add(f"SELECT * FROM {src_ref}", **passo)


def _compile_right(model: _Model, ref_spec: dict, resolve: SourceResolver, right_namer: _Namer):
    """Compila il lato destro di un join/union (sotto-flow {source, operations}
    o sorgente semplice) come CTE aggiuntive del modello."""
    spec = ref_spec["source"] if "source" in ref_spec else ref_spec
    src_ref, src_cols, src_types, nome = _risolvi(resolve, spec)
    s = _S(src_cols, src_types)
    base = _base(model, src_ref, s, nome, destra=True)
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


def _compile_ops(model, ref, s, operations, resolve, right_namer):
    for op in operations:
        op_type = op.get("type") if isinstance(op, dict) else op.type
        params = (op.get("params") if isinstance(op, dict) else op.params) or {}
        model.etichette.append(_etichetta(op_type, params, resolve, s.cols))
        try:
            ref, s = _compile_op(model, ref, s, op_type, params, resolve, right_namer)
        finally:
            model.etichette.pop()
    return ref, s


# ── impaginazione (solo spazi, mai il contenuto) ─────────────────────────────
_CLAUSOLA = re.compile(
    r"\s+(FROM|WHERE|GROUP BY|ORDER BY|QUALIFY|HAVING|LIMIT|UNION ALL BY NAME|UNION ALL|"
    r"(?:LEFT SEMI|LEFT ANTI|INNER|LEFT|RIGHT|FULL OUTER|CROSS|SEMI|ANTI) JOIN|USING SAMPLE)\b", re.IGNORECASE)


def _maschera(sql: str) -> str:
    """Il SQL con stringhe, identificatori quotati e parentesi annidate coperti: le
    parole chiave si cercano solo al primo livello e fuori dalle virgolette."""
    out, prof, quota = [], 0, None
    for ch in sql:
        if quota:
            out.append("_")
            if ch == quota:
                quota = None
            continue
        if ch in ("'", '"'):
            quota = ch
            out.append("_")
            continue
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


def compile_model_sql(source: dict, operations: list, resolve: SourceResolver, native: bool = False,
                      dialetto: str | None = None) -> tuple[str, list[str], dict]:
    """Compila (sorgente + operazioni) in un modello dbt completo. Ritorna
    (SQL del modello, colonne di uscita, loro tipi DuckDB)."""
    model = _Model(_Namer("s"), native=native, dialetto=dialetto)
    right_namer = _Namer("r")
    src_ref, src_cols, src_types, nome = _risolvi(resolve, source)
    s = _S(src_cols, src_types)
    base = _base(model, src_ref, s, nome)
    final, out = _compile_ops(model, base, s, operations, resolve, right_namer)
    rientra = lambda t: "\n".join("    " + r for r in t.splitlines())
    body = ",\n".join(
        f"{name} AS (\n" + (f"    -- {nota}\n" if nota else "") + f"{rientra(_formatta(sql))}\n)"
        for name, sql, nota in model.ctes
    )
    finale = _lista(out.cols) if (native and out.cols) else "*"
    return f"WITH\n{body}\nSELECT {finale}\nFROM {final}", out.cols, out.types


def tipizza_query(sql: str, cols: list[str], dtypes: dict) -> str:
    """Una query (datasource da query) con i tipi che Tabularia le ha dato."""
    tipi = {c: duck_type(dtypes.get(c)) for c in cols}
    if not cols or not all(tipi.values()):
        return sql
    proj = ", ".join(f"CAST({_qi(c)} AS {tipi[c]}) AS {_qi(c)}" for c in cols)
    return f"SELECT {proj} FROM ({sql}) AS _q"


# ─────────────────────────────────────────────────────────────────────────────
# test singolari dal data contract (SQL DuckDB; in nativo si traduce come i modelli)
# ─────────────────────────────────────────────────────────────────────────────
TEST_IDENT = "__t"   # il modello sotto test, da sostituire con {{ ref() }}


def singular_test_sql(test: dict) -> str:
    """Una regola di contratto che non è un test generico di dbt → una query che
    SELEZIONA le violazioni (dbt la considera fallita se torna righe)."""
    q, t = _qi, f'"{TEST_IDENT}" AS "{TEST_IDENT}"'
    kind, col = test.get("kind"), test.get("column")
    if kind == "range":
        conds = []
        if test.get("min") is not None:
            conds.append(f"{q(col)} < {_lit(test['min'])}")
        if test.get("max") is not None:
            conds.append(f"{q(col)} > {_lit(test['max'])}")
        if not col or not conds:
            raise DbtExportError("range: colonna e almeno un limite", "contract_rule_invalid", rule="range")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL AND ({' OR '.join(conds)})"
    if kind == "pattern":
        if not col or not test.get("regex"):
            raise DbtExportError("pattern: colonna e regex", "contract_rule_invalid", rule="pattern")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL AND NOT regexp_matches({q(col)}, {_lit(str(test['regex']))})"
    if kind == "row_count":
        conds = []
        if test.get("min") is not None:
            conds.append(f"COUNT(*) < {int(test['min'])}")
        if test.get("max") is not None:
            conds.append(f"COUNT(*) > {int(test['max'])}")
        if not conds:
            raise DbtExportError("row_count: almeno un limite", "contract_rule_invalid", rule="row_count")
        return f"SELECT COUNT(*) AS n FROM {t} HAVING {' OR '.join(conds)}"
    if kind == "unique_combination":
        cols = [c for c in (test.get("columns") or []) if c]
        if len(cols) < 2:
            raise DbtExportError("unique_combination: almeno due colonne", "contract_rule_invalid", rule="unique")
        lista = ", ".join(q(c) for c in cols)
        return f"SELECT {lista}, COUNT(*) AS n FROM {t} GROUP BY {lista} HAVING COUNT(*) > 1"
    if kind == "expression":
        sql = str(test.get("sql") or "").strip()
        if not sql:
            raise DbtExportError("expression: espressione vuota", "contract_rule_invalid", rule="expression")
        # un esito «non lo so» (null) non è un sì: la riga viola — come il valutatore
        return f"SELECT * FROM {t} WHERE NOT COALESCE(({sql}), FALSE)"
    raise DbtExportError(f"regola '{kind}' senza equivalente dbt", "rule_no_equivalent", kind=kind)


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


def _fix_for_dialect(tree, dialect: str):
    """Buchi noti fra i dialetti che sqlglot non copre da solo."""
    from sqlglot import exp

    if dialect == "postgres":
        # Postgres ha round(numeric, int) ma NON round(double precision, int)
        for r in list(tree.find_all(exp.Round)):
            if r.args.get("decimals") is not None and not isinstance(r.this, exp.Cast):
                r.set("this", exp.Cast(this=r.this.copy(), to=exp.DataType.build("DECIMAL")))
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
            sqlglot.parse_one(sql, read="duckdb"),
            schema=MappingSchema(sg_schema, dialect=dialect, normalize=False), dialect=dialect,
            validate_qualify_columns=False, expand_stars=False,
        )
        return _fix_for_dialect(tree, dialect).sql(dialect=dialect, pretty=True)
    except DbtExportError:
        raise
    except Exception as e:
        raise DbtExportError(
            f"traduzione nel dialetto '{dialect}' non riuscita ({e}). "
            "Il flusso è troppo complesso per l'export nativo — usa l'export «federato» (dbt-duckdb).",
            "translation_failed", dialect=dialect, detail=str(e)[:200],
        )


def substitute_source_idents(sql: str, ident_to_macro: dict[str, str]) -> str:
    """Sostituisce `"ident" AS "ident"` → `{{ source(...) }} AS "ident"` (solo il
    riferimento tabella; alias e qualificatori di colonna restano invariati)."""
    for ident, macro in ident_to_macro.items():
        pat = r'(["`])' + re.escape(ident) + r'\1\s+AS\s+(["`])' + re.escape(ident) + r"\2"
        sql = re.sub(pat, macro + r" AS \2" + ident + r"\2", sql)
    return sql


# ─────────────────────────────────────────────────────────────────────────────
# Generatore di progetto dbt
# ─────────────────────────────────────────────────────────────────────────────
# duckdb: DuckDB attacca il DB di origine (postgres/mysql) e legge le tabelle
# LIVE: il nostro SQL DuckDB gira as-is, niente snapshot, niente traduzione.
# clickhouse/trino non hanno uno scanner DuckDB ufficiale → non federabili.
_DUCK_ATTACH_TYPE = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql"}
_FEDERABLE = set(_DUCK_ATTACH_TYPE)
# native: adapter dbt per db_type (dbt-postgres/dbt-mysql/dbt-clickhouse)
_NATIVE_ADAPTER = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "clickhouse": "clickhouse"}


# ── target ClickHouse ──────────────────────────────────────────────────────────
# dbt-clickhouse sul ClickHouse del cliente (di solito quello del motore). Le sorgenti
# Postgres/MySQL si leggono DAL VIVO attraverso un database ClickHouse con il motore
# PostgreSQL / MySQL, uno per (connessione, schema): `tab_src_<id>_<schema>`. Li crea,
# una volta, la macro `create_tabularia_sources` (`dbt run-operation`, password da
# env_var). Da lì in poi è tutto ClickHouse: sources.yml punta a quei database.
_CH_MOTORE_FEDERATO = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mariadb": "MySQL"}


def nome_db_federato(conn_id: int, schema: str) -> str:
    """Il database ClickHouse che mostra uno schema di una connessione Postgres/MySQL."""
    return f"tab_src_{conn_id}_" + (re.sub(r"[^a-z0-9]+", "_", str(schema).lower()).strip("_") or "default")


def _schema_di_default(conn: dict) -> str:
    return "public" if conn["db_type"] == "postgresql" else (conn.get("database") or "")


class TargetClickHouse:
    """Le sorgenti di un export verso ClickHouse: quali database federati servono,
    come si chiamano, e le query delle datasource tradotte in ClickHouse."""

    def __init__(self, connections: list[dict], host_motore: str | None):
        self.conns = {int(c["id"]): c for c in connections}
        self.host_motore = (host_motore or "").strip().lower()
        self.federati: dict[str, dict] = {}     # nome del database → come crearlo

    def database_per(self, conn_id: int, schema: str) -> str:
        conn = self.conns.get(int(conn_id))
        if conn is None:
            raise DbtExportError(f"connessione {conn_id} sconosciuta all'export")
        if conn["db_type"] == "clickhouse":
            # un database del ClickHouse dove gira dbt: si legge così com'è
            if (conn.get("host") or "").strip().lower() != self.host_motore:
                raise DbtExportError(
                    f"la connessione «{conn.get('name')}» è un ALTRO ClickHouse ({conn.get('host')}): il target ClickHouse "
                    "legge le sorgenti Postgres/MySQL e le tabelle del suo stesso server. Usa l'export «nativo» su quel ClickHouse.",
                    "other_clickhouse", connection=conn.get("name"), host=conn.get("host"),
                )
            return schema or conn.get("database") or "default"
        motore = _CH_MOTORE_FEDERATO.get(conn["db_type"])
        if motore is None:
            raise DbtExportError(
                f"la connessione «{conn.get('name')}» è {conn['db_type']}: ClickHouse la legge solo se è "
                "PostgreSQL, MySQL o MariaDB. Usa un altro target per questo flusso.",
                "clickhouse_cannot_read", connection=conn.get("name"), db_type=conn["db_type"],
            )
        nome = nome_db_federato(conn["id"], schema)
        self.federati.setdefault(nome, {"name": nome, "engine": motore, "conn": conn, "schema": schema})
        return nome

    def sources(self, sources: list[dict]) -> list[dict]:
        """sources.yml: lo schema di ogni sorgente diventa il database ClickHouse che la mostra."""
        out = []
        for src in sources:
            if src.get("declared"):
                out.append(src)   # nel loro progetto: la dichiarano loro, nessun database federato da creare
                continue
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
            raise DbtExportError(f"connessione {conn_id} sconosciuta all'export")
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
            raise DbtExportError(f"la query della datasource «{etichetta}» non si traduce in ClickHouse ({str(e)[:200]}): usa il target federato",
                                 "query_not_translatable", datasource=etichetta, detail=str(e)[:200])
        if len(alberi) != 1 or alberi[0] is None:
            raise DbtExportError(f"la query della datasource «{etichetta}» non è una sola SELECT: non si traduce in ClickHouse",
                                 "query_not_single_select", datasource=etichetta)
        albero = alberi[0]
        cte = {c.alias_or_name for c in albero.find_all(exp.CTE)}
        for t in albero.find_all(exp.Table):
            if not t.name or (not t.args.get("db") and t.name in cte):
                continue
            t.set("db", exp.to_identifier(self.database_per(conn_id, t.db or _schema_di_default(conn))))
            t.set("catalog", None)
        try:
            return albero.sql(dialect="clickhouse", pretty=True)
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"la query della datasource «{etichetta}» non si traduce in ClickHouse ({str(e)[:200]}): usa il target federato",
                                 "query_not_translatable", datasource=etichetta, detail=str(e)[:200])


class ControlloSintassiClickHouse:
    """Il SQL ClickHouse di un modello letto da un parser ClickHouse VERO: `EXPLAIN AST`,
    che non esegue niente e non vuole che le tabelle esistano. sqlglot non fallisce
    quasi mai: traduce in silenzio costrutti che ClickHouse non ha (SIMILAR TO,
    LATERAL, TABLESAMPLE…), e l'errore usciva solo al `dbt run` del team data (scelta
    dell'utente, 2026-10-10: controllarlo all'export). Un errore qui è un «non
    tradotto»: col dialogo, la proposta dell'AI; senza, un rifiuto che dice perché.
    Parser: chDB se c'è (sviluppo, locale), altrimenti il ClickHouse del motore; se
    nessuno risponde il controllo si salta, con un log — non blocca un export.
    Non vede le funzioni che non esistono: per quelle servirebbe l'analisi."""

    def __init__(self):
        self._via: Any = None   # "chdb", un client clickhouse-connect, o False (nessun parser)

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
            self._via = clickhouse_connect.get_client(
                host=cfg.host, port=cfg.port, secure=cfg.secure, connect_timeout=cfg.connect_timeout,
                username=cfg.ai_username if usa_ai else cfg.username,
                password=(cfg.ai_password if usa_ai else cfg.password).get_secret_value(),
            )
        except Exception as e:  # noqa: BLE001 — server spento, credenziali: si esporta senza il controllo
            logger.info("export dbt: controllo di sintassi ClickHouse non disponibile (%s)", e)
            self._via = False
        return self._via

    def errore(self, sql: str) -> str | None:
        """None se ClickHouse legge il SQL; altrimenti la prima riga del suo errore."""
        via = self._apri()
        if not via:
            return None
        try:
            if via == "chdb":
                import chdb

                chdb.query(f"EXPLAIN AST {sql}", "TSV")
            else:
                via.query(f"EXPLAIN AST {sql}")
            return None
        except Exception as e:  # noqa: BLE001
            testo = str(e)
            if "Syntax error" not in testo and "SYNTAX_ERROR" not in testo:
                logger.info("export dbt: il controllo di sintassi non ha risposto (%s)", testo[:200])
                return None
            riga = next((r for r in testo.splitlines() if "Syntax error" in r), testo.splitlines()[0])
            riga = re.sub(r"^.*?DB::Exception:\s*", "", riga)
            return re.sub(r"\.\s*Expected one of:.*$", "", riga).strip()[:200]


def verifica_proposta(sql: str, dialetto: str, consentite: set[str] | None, colonne: list[str] | None, modello: str):
    """I controlli su una traduzione proposta dall'AI — li fa questo codice, non l'AI:
    una sola SELECT nel dialetto di arrivo, niente istruzioni che scrivono, nessuna
    table function, solo le tabelle che il modello legge (se `consentite`), le stesse
    colonne di uscita nello stesso ordine (se `colonne`). Torna l'albero sqlglot."""
    import sqlglot
    from sqlglot import exp

    def no(motivo: str, testo: str, **dettagli):
        # il motivo è un CODICE (il gateway lo dice nella lingua di chi legge), il testo resta nei log
        raise DbtExportError(f"la proposta dell'AI per «{modello}» non passa i controlli: {testo}",
                             "ai_translation_invalid", model=modello, reason=motivo, **dettagli)

    try:
        alberi = [a for a in sqlglot.parse(sql or "", read=dialetto) if a is not None]
    except Exception as e:  # noqa: BLE001
        no("unparsable", f"non si legge ({str(e)[:120]})")
    if len(alberi) != 1 or not isinstance(alberi[0], exp.Query):
        no("not_select", "non è una sola SELECT")
    albero = alberi[0]
    if any(isinstance(n, (exp.Insert, exp.Create, exp.Drop, exp.Delete, exp.Update, exp.Alter, exp.Command, exp.Merge)) for n in albero.walk()):
        no("writes", "contiene istruzioni che non sono una SELECT")
    cte = {c.alias_or_name for c in albero.find_all(exp.CTE)}
    for t in albero.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            no("table_function", "legge una table function")
        if consentite is not None and t.name not in cte and t.name not in consentite:
            no("unknown_table", f"legge una tabella che il modello non legge ({t.name})", table=t.name)
    if colonne is not None:
        if any(isinstance(x, exp.Star) or (isinstance(x, exp.Column) and isinstance(x.this, exp.Star)) for x in albero.selects):
            no("star", "usa * invece di elencare le colonne")
        uscita = [x.alias_or_name for x in albero.selects]
        if uscita != list(colonne):
            no("columns", f"dà le colonne {uscita} invece di {list(colonne)}", got=", ".join(uscita), expected=", ".join(colonne))
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
    errore = controllo.errore(testo) if controllo else None   # prima delle macro: ClickHouse non legge il Jinja
    if errore:
        raise DbtExportError(f"la proposta dell'AI per «{modello}» non passa i controlli: ClickHouse non la legge ({errore})",
                             "ai_translation_invalid", model=modello, reason="syntax", detail=errore)
    for ident, macro in ident_to_macro.items():
        testo = re.sub(r'[`"]?__TABULARIA_' + re.escape(ident) + r'__[`"]?', lambda _m, macro=macro: macro, testo)
    return testo


def _letterale_ch(v: Any) -> str:
    """Un letterale stringa ClickHouse scritto in un file Jinja: niente delimitatori Jinja."""
    t = str(v if v is not None else "")
    if re.search(r"[{}%#]", t):
        raise DbtExportError(f"il valore «{t}» contiene caratteri che il progetto dbt non può scrivere in una macro", "macro_value_invalid", value=t)
    return "'" + t.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _macro_sorgenti_clickhouse(federati: list[dict]) -> str:
    """La macro che crea, una volta, i database federati verso le sorgenti."""
    righe = [
        "{% macro create_tabularia_sources() %}",
        "{#- Tabularia: one ClickHouse database per source (connection, schema), with the",
        "    PostgreSQL / MySQL engine: ClickHouse reads the source tables live from there.",
        "    Run once, before the first `dbt run`:",
        "      dbt run-operation create_tabularia_sources --log-level-file none",
        "    The source passwords come from env vars and are part of the SQL this runs:",
        "    `--log-level-file none` keeps that SQL out of logs/dbt.log. -#}",
    ]
    for f in federati:
        c = f["conn"]
        pw = f"pw_{_slug(f['name'])}"
        righe.append("{%- set " + pw + " = env_var('" + c["pw_env"] + "') | replace('\\\\', '\\\\\\\\') | replace(\"'\", \"\\\\'\") %}")
        indirizzo = f"{c['host']}:{c.get('port') or (5432 if f['engine'] == 'PostgreSQL' else 3306)}"
        if f["engine"] == "PostgreSQL":
            argomenti = [_letterale_ch(indirizzo), _letterale_ch(c.get("database")), _letterale_ch(c.get("username")), "'{{ " + pw + " }}'", _letterale_ch(f["schema"])]
        else:   # MySQL: lo schema è il database
            argomenti = [_letterale_ch(indirizzo), _letterale_ch(f["schema"]), _letterale_ch(c.get("username")), "'{{ " + pw + " }}'"]
        righe += [
            "{% call statement('" + f["name"] + "') -%}",
            f"CREATE DATABASE IF NOT EXISTS `{f['name']}` ENGINE = {f['engine']}({', '.join(argomenti)})",
            "{%- endcall %}",
            "{{ log('Tabularia source ready: " + f["name"] + "', info=True) }}",
        ]
    righe.append("{% endmacro %}")
    return "\n".join(righe) + "\n"


def _profilo_clickhouse(project: str, ch: dict) -> str:
    """profiles.yml del target ClickHouse: host e porta del ClickHouse del motore come
    valori di partenza, l'utente e la password di dbt SOLO da env_var. `join_use_nulls`:
    senza, il lato mancante di un join esterno è 0 / '' invece di NULL (il motore non lo
    vede perché legge parquet, dove tutto è Nullable)."""
    host = ch.get("host") or ""
    porta = ch.get("port") or (8443 if ch.get("secure") else 8123)
    sicuro = "true" if ch.get("secure") else "false"
    return "\n".join([
        f"{project}:", "  target: dev", "  outputs:", "    dev:", "      type: clickhouse",
        "      host: \"{{ env_var('CLICKHOUSE_HOST'" + (f", '{host}'" if host else "") + ") }}\"",
        "      port: \"{{ env_var('CLICKHOUSE_PORT', '" + str(porta) + "') | as_number }}\"",
        # as_bool vuole True/False alla Python: si accetta anche come lo scrive una persona (true, 1, yes)
        "      secure: \"{{ (env_var('CLICKHOUSE_SECURE', '" + sicuro + "') | lower in ['true', '1', 'yes']) | as_bool }}\"",
        "      user: \"{{ env_var('CLICKHOUSE_USER') }}\"",
        "      password: \"{{ env_var('CLICKHOUSE_PASSWORD') }}\"",
        f"      schema: {ch.get('target_schema') or 'dbt_tabularia'}",
        "      custom_settings:",
        "        join_use_nulls: 1",
    ]) + "\n"


def _attach_dsn(db_type: str, conn: dict, pw_env: str) -> str:
    """DSN di ATTACH per lo scanner DuckDB, password via env_var (mai in chiaro)."""
    host, port, db, user = conn["host"], conn["port"], conn["database"], conn["username"]
    pw = "{{ env_var('%s') }}" % pw_env
    if db_type == "postgresql":
        return f"dbname={db} host={host} port={port} user={user} password={pw}"
    if db_type in ("mysql", "mariadb"):
        return f"host={host} port={port} database={db} user={user} password={pw}"
    raise DbtExportError(f"federazione DuckDB non disponibile per '{db_type}'", "duckdb_federation_unavailable", db_type=db_type)


def _native_profile(project: str, native: dict) -> str:
    """profiles.yml per il warehouse NATIVO. Il dbt gira nel DB di origine; le
    trasformazioni sono già tradotte nel suo dialetto (sqlglot). Password via env_var."""
    db_type = native["db_type"]
    adapter = _NATIVE_ADAPTER.get(db_type)
    if adapter is None:
        raise DbtExportError(f"nessun adapter dbt nativo per '{db_type}'", "no_native_adapter", db_type=db_type)
    c = native["conn"]
    pw = "{{ env_var('%s') }}" % native["pw_env"]
    schema = native.get("target_schema") or "dbt_tabularia"
    lines = [f"{project}:", "  target: dev", "  outputs:", "    dev:", f"      type: {adapter}"]
    if adapter == "postgres":
        lines += [f"      host: {c['host']}", f"      port: {c['port']}", f"      user: {c['username']}",
                  f"      password: \"{pw}\"", f"      dbname: {c['database']}", f"      schema: {schema}"]
    elif adapter == "mysql":
        lines += [f"      server: {c['host']}", f"      port: {c['port']}", f"      username: {c['username']}",
                  f"      password: \"{pw}\"", f"      schema: {schema}"]
    elif adapter == "clickhouse":
        lines += [f"      host: {c['host']}", f"      port: {c['port']}", f"      user: {c['username']}",
                  f"      password: \"{pw}\"", f"      schema: {schema}", "      secure: false"]
    return "\n".join(lines) + "\n"


def _y(s: Any) -> str:
    """Una stringa YAML fra virgolette doppie (le sequenze JSON sono YAML valido)."""
    return json.dumps(str(s), ensure_ascii=False)


def _schema_yaml(models: list[dict]) -> str:
    """models/schema.yml: descrizioni, colonne e test generici (dal data contract)."""
    out = ["version: 2", "", "models:"]
    for m in models:
        out.append(f"  - name: {m['name']}")
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
                    out.append("        data_tests:")
                    for t in tests:
                        if t["kind"] == "accepted_values":
                            # dbt ≥ 1.10: i parametri dei test generici stanno sotto `arguments`
                            out.append("          - accepted_values:")
                            out.append("              arguments:")
                            out.append("                values: [" + ", ".join(_y(v) for v in t.get("values") or []) + "]")
                            out.append(f"              config: {{severity: {t.get('severity', 'error')}}}")
                        else:
                            out.append(f"          - {t['kind']}:")
                            out.append(f"              config: {{severity: {t.get('severity', 'error')}}}")
    return "\n".join(out) + "\n"


def _sources_yaml(sources: list[dict]) -> str:
    out = ["version: 2", "", "sources:"]
    for s in sources:
        out.append(f"  - name: {s['name']}")
        if s.get("database"):
            out.append(f"    database: {s['database']}")
        out.append(f"    schema: {s['schema']}")
        out.append("    tables:")
        for t in sorted(s["tables"], key=lambda t: t["name"] if isinstance(t, dict) else t):
            if not isinstance(t, dict):
                out.append(f"      - name: {t}")
                continue
            out.append(f"      - name: {t['name']}")
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
        out.append(f"  - name: {s['name']}")
        if s.get("description"):
            out.append(f"    description: {_y(s['description'])}")
        if s.get("column_types"):
            # i tipi DICHIARATI, non indovinati dal CSV: sono quelli con cui Tabularia legge il file
            out.append("    config:")
            out.append("      column_types:")
            for c, t in s["column_types"].items():
                out.append(f"        {_y(c)}: {_y(t)}")
        if s.get("columns"):
            out.append("    columns:")
            for c in s["columns"]:
                out.append(f"      - name: {_y(c['name'])}")
                if c.get("description"):
                    out.append(f"        description: {_y(c['description'])}")
    return "\n".join(out) + "\n"


def _model_header(m: dict) -> str:
    mat = m.get("materialized", "table")
    parti = [f"materialized='{mat}'"]
    if mat == "incremental":
        parti.append("incremental_strategy='append'")
    if m.get("schema"):
        parti.append(f"schema='{m['schema']}'")
    if m.get("alias"):
        parti.append(f"alias='{m['alias']}'")
    testa = "{{ config(" + ", ".join(parti) + ") }}\n\n"
    if m.get("ai_translated"):
        testa += f"-- Translated by AI from {_commento(m.get('ai_translated_from') or 'another dialect')}: review it before use.\n\n"
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
    project = _slug(flow_name)
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
        attach = "\n".join(
            f'        - path: \"{_attach_dsn(a["db_type"], a["conn"], a["pw_env"])}\"\n'
            f'          type: {_DUCK_ATTACH_TYPE[a["db_type"]]}\n'
            f'          alias: {a["alias"]}'
            for a in (attachments or [])
        )
        files["profiles.yml"] = (
            f"{project}:\n"
            f"  target: dev\n"
            f"  outputs:\n"
            f"    dev:\n"
            f"      type: duckdb\n"
            f"      path: {project}.duckdb\n"
            + (f"      schema: {schema}\n" if schema else "")
            + (f"      extensions: [{', '.join(extensions)}]\n" if extensions else "")
            + (f"      attach:\n{attach}\n" if attach else "")
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
        f"models:\n  {project}:\n    +materialized: table\n"
    )

    nostre = [x for x in sources if not x.get("declared")]   # quelle «già dichiarate» le ha il loro sources.yml
    if nostre:
        files[dove.modelli("sources.yml")] = _sources_yaml(nostre)
    files[dove.modelli("schema.yml")] = _schema_yaml(models)
    for m in models:
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
            f"{{{{ config(severity='{t.get('severity', 'error')}') }}}}\n"
            + (f"-- {t['description']}\n" if t.get("description") else "")
            + t["sql"] + "\n"
        )

    preparazione = ""
    if clickhouse is not None:
        federati = clickhouse.get("federati") or []
        intro = (
            f"Generated by Tabularia from the flow **{flow_name}**. Targets **dbt-clickhouse** on your\n"
            "ClickHouse: the source databases are read live through ClickHouse database engines (one\n"
            "database `tab_src_<connection>_<schema>` per source schema), and from there on everything\n"
            "runs in ClickHouse.\n"
        )
        pip = "pip install dbt-clickhouse"
        pw_envs = ["CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"] + list(dict.fromkeys(f["conn"]["pw_env"] for f in federati))
        if federati:
            preparazione = "DBT_PROFILES_DIR=. dbt run-operation create_tabularia_sources --log-level-file none   # once\n"
            raggiunge = ", ".join(sorted({f"`{f['conn']['host']}:{f['conn'].get('port') or (5432 if f['engine'] == 'PostgreSQL' else 3306)}`" for f in federati}))
            notes = [
                f"ClickHouse must reach the source databases ({raggiunge}): it reads them itself, dbt does not.",
                "`create_tabularia_sources` creates the `tab_src_*` databases (`CREATE DATABASE … ENGINE = PostgreSQL/MySQL`): "
                "the ClickHouse user needs `CREATE DATABASE`, and ClickHouse keeps the source password in the database "
                "definition (hidden in `SHOW CREATE`). Give it a read-only user on the sources.",
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
    else:
        intro = (
            f"Generated by Tabularia from the flow **{flow_name}**. Targets **dbt-duckdb**, which\n"
            "attaches the origin database(s) and reads the tables live (no snapshots, no dialect translation).\n"
        )
        pip = "pip install dbt-duckdb"
        pw_envs = [a["pw_env"] for a in (attachments or [])]
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
        f"# dbt project — {flow_name}\n\n{intro}\n" + provenienza
        + (f"{flow_description}\n\n" if flow_description else "")
        + "## Models\n\n" + "\n".join(cosa) + "\n\n"
        + "## Run\n\n```bash\n" + pip + "\n"
        + "# set the source DB password(s) (referenced via env_var in profiles.yml):\n"
        + passi + "```\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n" if notes else "")
    )
    return files


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
            raise DbtExportError(f"nome di cartella non valido: «{cartella}»", "folder_invalid", folder=cartella)
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
    if clickhouse is not None:
        profilo = ("Your **dbt-clickhouse** output needs `join_use_nulls` (without it the missing side of an outer join is "
                   "0 or `''` instead of NULL):\n\n```yaml\n      custom_settings:\n        join_use_nulls: 1\n```\n")
    elif native:
        c = native.get("conn") or {}
        profilo = (f"Your **dbt-{_NATIVE_ADAPTER[native['db_type']]}** profile on the {native['db_type']} database "
                   f"`{c.get('database')}` at `{c.get('host')}:{c.get('port')}`: the models run there, next to the data.\n")
    else:
        attach = "\n".join(
            f'        - path: "{_attach_dsn(a["db_type"], a["conn"], a["pw_env"])}"\n'
            f'          type: {_DUCK_ATTACH_TYPE[a["db_type"]]}\n'
            f'          alias: {a["alias"]}'
            for a in (attachments or [])
        )
        profilo = ("Your **dbt-duckdb** profile must attach the source databases with these aliases "
                   "(the sources read `<alias>.<schema>.<table>`):\n\n```yaml\n      attach:\n" + attach + "\n```\n") if attach else \
            "Your **dbt-duckdb** profile: this flow reads no database.\n"
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
        + f"dbt build -s +tag:{tag}\n```\n\n"
        f"`+tag:{tag}` selects the models of the flow and everything they read (upstream flows, seeds), with their tests.\n\n"
        + "## Models\n\n" + "\n".join(cosa) + "\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in note) + "\n" if note else "")
    )


def _slug(name: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(name).lower())
    out = out.strip("_") or "model"
    return out if not out[0].isdigit() else f"m_{out}"
