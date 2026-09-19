"""
Registry delle operazioni per l'engine BigQuery, parallelo a `chdb_ops.py`:
puro SQL (dialetto GoogleSQL), ogni operazione riceve la SELECT dello step
precedente e la avvolge in una subquery. La catena e' un'unica query che
BigQuery pianifica ed esegue in una volta.

Firma: `(sql, params, ctx)`. `ctx` serve per l'introspezione delle colonne
(`ctx.columns_of` / `ctx.schema_of`, che su BigQuery e' un DRY RUN: gratuito)
e per le operazioni MULTI-INPUT (join/union) che leggono il lato destro
(`ctx.build_right`).

Standard cross-engine (vedi operations.py e test_data_correctness.py): NULL,
cast «try» (fallito → NULL), join con suffisso _right, pivot con etichette
ordinate, datetime naive UTC (BigQuery: DATETIME). Dove BigQuery non ha un
equivalente esatto lo si dice qui accanto all'operazione.

Nomi colonna: BigQuery ammette solo [A-Za-z0-9_] (e normalizza da solo quelli
del parquet: "Ragione Sociale" → Ragione_Sociale). Le operazioni ragionano nei
nomi ORIGINALI e ogni identificatore passa da `ctx.qi(nome)`, che lo traduce
nel nome sicuro registrato nel contesto; il motore ritraduce i risultati.
`ctx.columns_of`/`schema_of` rispondono gia' nei nomi originali.

Sicurezza: le operazioni strutturali usano solo nomi colonna / operatori /
valori. `compute` e `sql` accettano espressioni ma vietano subquery, DDL/DML,
EXTERNAL_QUERY, ML/AI e gli statement di script.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from app.engine.context import MAX_CROSS_JOIN_ROWS
from app.engine.exceptions import EngineError
from app.engine.operations import MAX_PIVOT_COLUMNS, PIVOT_LABEL_SEP, SAMPLE_BUCKETS, pivot_label, sample_threshold
from app.engine.sql_guard import ensure_reads_only_input

BqOpFn = Callable[..., str]

_REGISTRY: dict[str, BqOpFn] = {}


def _register(name: str) -> Callable[[BqOpFn], BqOpFn]:
    def deco(fn: BqOpFn) -> BqOpFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_bigquery_operation(name: str) -> BqOpFn:
    fn = _REGISTRY.get(name)
    if fn is None:
        raise EngineError(
            f"operazione '{name}' non supportata dall'engine BigQuery. "
            "Usa il motore Polars o DuckDB per questo flusso (o questo nodo)."
        )
    return fn


# ── helper SQL (dialetto GoogleSQL) ───────────────────────────────────────────
def _qi(name: str) -> str:
    """Quota un identificatore con i backtick (GoogleSQL)."""
    return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"


def _lit(v: Any) -> str:
    """Letterale SQL da un valore Python (gia' parsato dal frontend). BigQuery
    NON converte implicitamente: un BOOL si confronta solo con TRUE/FALSE."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, "", []):
        raise EngineError(f"parametro mancante: '{key}'")
    return params[key]


def _as_list(v) -> list:
    return v if isinstance(v, list) else [v]


def _sub(sql: str) -> str:
    return f"({sql})"


# tipi BigQuery (come li riporta lo schema di un job) → famiglia
_BQ_INTS = {"INTEGER", "INT64"}
_BQ_FLOATS = {"FLOAT", "FLOAT64"}
_BQ_DECIMALS = {"NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"}
_BQ_TEXT = {"STRING"}
_CAST_TYPES = {"int", "float", "str", "bool", "date", "datetime"}


# ── colonne ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(sql, params, ctx):
    cols = ", ".join(ctx.qi(c) for c in _require(params, "columns"))
    return f"SELECT {cols} FROM {_sub(sql)}"


@_register("reorder")
def op_reorder(sql, params, ctx):
    order = _require(params, "columns")
    cols = ", ".join(ctx.qi(c) for c in order)
    return f"SELECT {cols}, * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("drop")
def op_drop(sql, params, ctx):
    cols = ", ".join(ctx.qi(c) for c in _require(params, "columns"))
    return f"SELECT * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("rename")
def op_rename(sql, params, ctx):
    mapping = _require(params, "mapping")
    cols = ctx.columns_of(sql)
    parts = [f"{ctx.qi(c)} AS {ctx.qi(mapping[c])}" if c in mapping else ctx.qi(c) for c in cols]
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)}"


def _cast_expr(c: str, base: str, dt: str) -> str:
    """Stesso STANDARD del cast di Polars/DuckDB/ClickHouse: fallito → NULL
    (SAFE_CAST); testo con spazi tagliati; testo → intero solo se intero
    letterale; numero → intero per troncamento (CAST arrotonderebbe); float →
    testo con ".0" sui valori interi (BigQuery stamperebbe 200.0 come "200")."""
    is_text = base in _BQ_TEXT
    is_float = base in _BQ_FLOATS or base in _BQ_DECIMALS
    if dt == "int":
        if is_text:
            return f"SAFE_CAST(TRIM({c}) AS INT64)"
        if is_float:
            return f"SAFE_CAST(TRUNC({c}) AS INT64)"
        return f"SAFE_CAST({c} AS INT64)"
    if dt == "float":
        if is_text:
            return f"SAFE_CAST(TRIM({c}) AS FLOAT64)"
        return f"SAFE_CAST({c} AS FLOAT64)"
    if dt == "str":
        if base in _BQ_FLOATS:
            return (f"IF({c} = TRUNC({c}) AND ABS({c}) < 1e15, "
                    f"CONCAT(CAST(CAST({c} AS INT64) AS STRING), '.0'), CAST({c} AS STRING))")
        return f"SAFE_CAST({c} AS STRING)"
    if dt == "date":
        if is_text:
            # SAFE_CAST accetta solo YYYY-MM-DD e rifiuta le date impossibili
            # ("2024-02-30" → NULL), come Polars/DuckDB
            return f"SAFE_CAST(TRIM({c}) AS DATE)"
        if base == "TIMESTAMP":
            return f"DATE({c}, 'UTC')"
        return f"SAFE_CAST({c} AS DATE)"
    if dt == "datetime":
        if is_text:
            # ISO con 'T' o spazio; il resto → NULL (Polars/DuckDB: solo l'ISO)
            return f"SAFE_CAST(TRIM({c}) AS DATETIME)"
        if base == "TIMESTAMP":
            return f"DATETIME({c}, 'UTC')"
        return f"SAFE_CAST({c} AS DATETIME)"
    if dt == "bool":
        if is_text:
            return f"SAFE_CAST(TRIM({c}) AS BOOL)"
        if base in _BQ_INTS or is_float:
            return f"SAFE_CAST(SAFE_CAST({c} AS INT64) AS BOOL)"
        return f"SAFE_CAST({c} AS BOOL)"
    raise EngineError(f"cast: tipo non supportato '{dt}'")


@_register("cast")
def op_cast(sql, params, ctx):
    cols = _require(params, "columns")
    schema = dict(ctx.schema_of(sql))
    repls = []
    for col, dt in cols.items():
        if str(dt) not in _CAST_TYPES:
            raise EngineError(f"cast: tipo non supportato '{dt}'")
        base = str(schema.get(col, "")).upper()
        repls.append(f"{_cast_expr(ctx.qi(col), base, str(dt))} AS {ctx.qi(col)}")
    return f"SELECT * REPLACE ({', '.join(repls)}) FROM {_sub(sql)}"


# ── filtri ────────────────────────────────────────────────────────────────────
def _predicate(ctx, column: str, operator: str, value: Any) -> str:
    c = ctx.qi(column)
    if operator == "is_null":
        return f"{c} IS NULL"
    if operator == "is_not_null":
        return f"{c} IS NOT NULL"
    if operator in ("in", "not_in"):
        joined = ", ".join(_lit(v) for v in _as_list(value))
        return f"{c} {'NOT IN' if operator == 'not_in' else 'IN'} ({joined})"
    if operator == "between":
        lo, hi = (_as_list(value) + [None, None])[:2]
        return f"{c} BETWEEN {_lit(lo)} AND {_lit(hi)}"
    if operator == "contains":
        return f"STRPOS(CAST({c} AS STRING), {_lit(value)}) > 0"
    if operator == "starts_with":
        return f"STARTS_WITH(CAST({c} AS STRING), {_lit(value)})"
    if operator == "ends_with":
        return f"ENDS_WITH(CAST({c} AS STRING), {_lit(value)})"
    ops = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
    if operator not in ops:
        raise EngineError(f"filter: operatore non supportato '{operator}'")
    return f"{c} {ops[operator]} {_lit(value)}"


@_register("filter")
def op_filter(sql, params, ctx):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    pred = _predicate(ctx, column, operator, params.get("value"))
    return f"SELECT * FROM {_sub(sql)} WHERE {pred}"


# ── riordino / righe ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(sql, params, ctx):
    cols = _as_list(_require(params, "by"))
    if params.get("ignore_missing"):  # vedi chdb_ops.op_sort
        have = set(ctx.columns_of(sql))
        cols = [c for c in cols if c in have]
        if not cols:
            return sql
    direction = "DESC" if params.get("descending") else "ASC"
    order = ", ".join(f"{ctx.qi(c)} {direction} NULLS LAST" for c in cols)
    return f"SELECT * FROM {_sub(sql)} ORDER BY {order}"


@_register("limit")
def op_limit(sql, params, ctx):
    return f"SELECT * FROM {_sub(sql)} LIMIT {int(_require(params, 'n'))}"


@_register("sample")
def op_sample(sql, params, ctx):
    # campione casuale DETERMINISTICO: hash del contenuto della riga (+ seme).
    # Righe diverse dagli altri engine (hash diverso), ma le stesse a ogni run.
    threshold, seed = sample_threshold(params)
    return (
        f"SELECT * FROM {_sub(sql)} AS _r WHERE "
        f"MOD(ABS(FARM_FINGERPRINT(CONCAT({_lit(str(seed))}, TO_JSON_STRING(_r)))), {SAMPLE_BUCKETS}) < {threshold}"
    )


@_register("unique")
def op_unique(sql, params, ctx):
    subset = params.get("subset")
    if subset:
        part = ", ".join(ctx.qi(c) for c in subset)
        # una riga per combinazione (quale, come su ClickHouse, non e' garantito)
        return f"SELECT * FROM {_sub(sql)} QUALIFY ROW_NUMBER() OVER (PARTITION BY {part}) = 1"
    return f"SELECT DISTINCT * FROM {_sub(sql)}"


@_register("fill_null")
def op_fill_null(sql, params, ctx):
    cols = _require(params, "columns")
    repls = ", ".join(f"COALESCE({ctx.qi(c)}, {_lit(v)}) AS {ctx.qi(c)}" for c, v in cols.items())
    return f"SELECT * REPLACE ({repls}) FROM {_sub(sql)}"


@_register("drop_nulls")
def op_drop_nulls(sql, params, ctx):
    subset = params.get("subset") or ctx.columns_of(sql)
    cond = " AND ".join(f"{ctx.qi(c)} IS NOT NULL" for c in subset)
    return f"SELECT * FROM {_sub(sql)} WHERE {cond}"


# ── aggregazione ──────────────────────────────────────────────────────────────
# `median` non ha un aggregato ESATTO in BigQuery: e' calcolata a parte con
# PERCENTILE_CONT (analitica) e agganciata per chiave (vedi op_group_by).
_AGG = {
    "sum": "SUM", "mean": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT",
    "std": "STDDEV_SAMP", "var": "VAR_SAMP",
    "first": "ANY_VALUE", "last": "ANY_VALUE", "n_unique": "COUNT_DISTINCT", "median": "MEDIAN",
}


def _agg_expr(func: str, col_sql: str) -> str:
    if func == "n_unique":
        return f"COUNT(DISTINCT {col_sql})"
    return f"{_AGG[func]}({col_sql})"


@_register("group_by")
def op_group_by(sql, params, ctx):
    by = _as_list(_require(params, "by"))
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(ctx.qi(c) for c in by)
    base = _sub(sql)
    out: list[str] = [f"a.{ctx.qi(c)}" for c in by]
    parts = [by_sql]
    medians: list[tuple[str, str]] = []  # (colonna, alias)
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: funzione non supportata '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        if func == "median":
            medians.append((col, alias))
            out.append(f"m.{ctx.qi(alias)}")
            continue
        parts.append(f"{_agg_expr(func, ctx.qi(col))} AS {ctx.qi(alias)}")
        out.append(f"a.{ctx.qi(alias)}")
    agg_sql = f"SELECT {', '.join(parts)} FROM {base} GROUP BY {by_sql}"
    if not medians:
        return agg_sql
    # mediana ESATTA: PERCENTILE_CONT e' solo analitica → una riga per chiave
    # (DISTINCT) e join sulle chiavi con semantica NULL-safe
    med_cols = ", ".join(f"PERCENTILE_CONT({ctx.qi(c)}, 0.5) OVER (PARTITION BY {by_sql}) AS {ctx.qi(a)}" for c, a in medians)
    med_sql = f"SELECT DISTINCT {by_sql}, {med_cols} FROM {base}"
    on = " AND ".join(f"a.{ctx.qi(c)} IS NOT DISTINCT FROM m.{ctx.qi(c)}" for c in by)
    return f"SELECT {', '.join(out)} FROM ({agg_sql}) AS a LEFT JOIN ({med_sql}) AS m ON {on}"


# ── compute (espressioni scalari, SENZA subquery né accesso esterno) ──────────
_FORBIDDEN_IN_EXPR = re.compile(
    r"\b(?:select|from|with|insert|create|external_query|execute|call|declare|"
    r"ml\.|ai\.|session_user|export)\b",
    re.IGNORECASE,
)


def to_bigquery_expr(expr: str) -> str:
    """Traduce un'espressione scalare dal dialetto degli altri motori (DuckDB/
    Polars SQL: `%`, strftime, date_part…) in GoogleSQL con sqlglot. Best-effort:
    se non si lascia analizzare resta com'e' (magari e' gia' GoogleSQL)."""
    try:
        import sqlglot

        out = sqlglot.transpile(f"SELECT {expr}", read="duckdb", write="bigquery")[0]
    except Exception:  # noqa: BLE001
        return expr
    return out[7:] if out.upper().startswith("SELECT ") else expr


@_register("compute")
def op_compute(sql, params, ctx):
    columns = _require(params, "columns")  # [{name, expr}]
    if not isinstance(columns, list):
        raise EngineError("compute: definisci almeno una colonna calcolata")
    existing = set(ctx.columns_of(sql))
    for c in columns:
        name = str(c.get("name") or "").strip()
        expr = str(c.get("expr") or "").strip()
        if not name or not expr:
            raise EngineError("compute: nome ed espressione sono obbligatori")
        if _FORBIDDEN_IN_EXPR.search(expr):
            raise EngineError(
                f"compute: espressione di '{name}' non consentita (niente subquery, "
                "FROM o funzioni esterne — solo espressioni scalari)."
            )
        expr = ctx.map_quoted(to_bigquery_expr(expr))
        if name in existing:
            sql = f"SELECT * REPLACE (({expr}) AS {ctx.qi(name)}) FROM {_sub(sql)}"
        else:
            sql = f"SELECT *, ({expr}) AS {ctx.qi(name)} FROM {_sub(sql)}"
        existing.add(name)
    return sql


# ── Execute SQL (query libera sull'input, dialetto GoogleSQL) ─────────────────
_SQL_FORBIDDEN = re.compile(
    r"\b(?:insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|"
    r"export|load|call|execute|declare|begin|set|external_query|ml\.|ai\.|"
    r"information_schema|session_user)\b",
    re.IGNORECASE,
)


@_register("sql")
def op_sql(sql, params, ctx):
    """Query libera sull'input del nodo, esposto come CTE `self` (alias `input`),
    stessa convenzione degli altri motori. Composabile come ogni altra op."""
    query = str(_require(params, "query")).strip().rstrip(";").strip()
    if not query:
        raise EngineError("sql: la query è vuota")
    if not re.search(r"\bfrom\s+(?:self|input)\b", query, re.IGNORECASE):
        raise EngineError("sql: la query deve leggere dall'input del nodo — usa `FROM self` (o `FROM input`).")
    if "--" in query or "/*" in query:
        raise EngineError("sql: i commenti (-- e /* */) non sono ammessi nel nodo SQL.")
    if ";" in query:
        raise EngineError("sql: è ammessa una sola istruzione SELECT (niente ';').")
    if _SQL_FORBIDDEN.search(query) or re.search(r"`[^`]*\.[^`]*`", query):
        raise EngineError(
            "sql: consentito solo interrogare l'input del nodo. Vietati DDL/DML, "
            "script, EXTERNAL_QUERY, ML/AI e i riferimenti a tabelle del progetto."
        )
    # lista BIANCA: `dataset.tabella` si scrive anche SENZA backtick, e la
    # step-cache degli altri utenti vive in un dataset dello stesso progetto
    ensure_reads_only_input(query, "bigquery")
    return f"WITH input AS ({sql}), self AS (SELECT * FROM input) {ctx.map_quoted(query)}"


# ── join / union ──────────────────────────────────────────────────────────────
def _join_condition(ctx, params: dict) -> tuple[str, str, list[tuple[str, str]]]:
    """Torna (clausola SQL, kind, coppie chiave) con kind='using' o 'on'."""
    if "on" in params and params["on"]:
        keys = _as_list(params["on"])
        return f"USING ({', '.join(ctx.qi(c) for c in keys)})", "using", [(k, k) for k in keys]
    lo = _as_list(_require(params, "left_on"))
    ro = _as_list(_require(params, "right_on"))
    if len(lo) != len(ro):
        raise EngineError("join: left_on e right_on devono avere lo stesso numero di colonne")
    cond = " AND ".join(f"l.{ctx.qi(l)} = r.{ctx.qi(r)}" for l, r in zip(lo, ro))
    return f"ON {cond}", "on", list(zip(lo, ro))


def _join_select(ctx, lcols, rcols, skip_right: set, coalesce_keys: set = frozenset()) -> str:
    lset = set(lcols)
    parts = [
        f"COALESCE(l.{ctx.qi(c)}, r.{ctx.qi(c)}) AS {ctx.qi(c)}" if c in coalesce_keys else f"l.{ctx.qi(c)}"
        for c in lcols
    ]
    for c in rcols:
        if c in skip_right:
            continue
        parts.append(f"r.{ctx.qi(c)} AS {ctx.qi(c + '_right')}" if c in lset else f"r.{ctx.qi(c)}")
    return ", ".join(parts)


_JOIN_KW = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}


@_register("join")
def op_join(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    how = params.get("how", "inner")
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    L, R = _sub(sql), _sub(right)

    if how == "cross":
        ln = ctx.scalar(f"SELECT COUNT(*) FROM {L}")
        rn = ctx.scalar(f"SELECT COUNT(*) FROM {R}")
        if MAX_CROSS_JOIN_ROWS and ln * rn > MAX_CROSS_JOIN_ROWS:
            raise EngineError(
                f"Il cross join produrrebbe {ln * rn:,} righe ({ln:,} × {rn:,}), "
                f"oltre il limite di {MAX_CROSS_JOIN_ROWS:,}. Aggiungi una condizione di join "
                "o riduci le sorgenti."
            )
        sel = _join_select(ctx, lcols, rcols, set())
        return f"SELECT {sel} FROM {L} AS l CROSS JOIN {R} AS r"

    clause, kind, pairs = _join_condition(ctx, params)
    if how in ("semi", "anti"):
        # BigQuery non ha SEMI/ANTI JOIN: EXISTS con le stesse chiavi
        cond = " AND ".join(f"l.{ctx.qi(l)} = r.{ctx.qi(r)}" for l, r in pairs)
        kw = "EXISTS" if how == "semi" else "NOT EXISTS"
        return f"SELECT l.* FROM {L} AS l WHERE {kw} (SELECT 1 FROM {R} AS r WHERE {cond})"
    if how not in _JOIN_KW:
        raise EngineError(f"join: tipo non supportato '{how}'")
    skip_right = {k for k, _ in pairs} if kind == "using" else set()
    sel = _join_select(ctx, lcols, rcols, skip_right, skip_right if how in ("full", "right") else set())
    return f"SELECT {sel} FROM {L} AS l {_JOIN_KW[how]} {R} AS r {clause}"


@_register("union")
def op_union(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    if params.get("strategy") == "strict":
        return f"SELECT * FROM {_sub(sql)} UNION ALL SELECT * FROM {_sub(right)}"
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    allcols = lcols + [c for c in rcols if c not in set(lcols)]
    lset, rset = set(lcols), set(rcols)
    lsel = ", ".join((ctx.qi(c) if c in lset else "NULL") + f" AS {ctx.qi(c)}" for c in allcols)
    rsel = ", ".join((ctx.qi(c) if c in rset else "NULL") + f" AS {ctx.qi(c)}" for c in allcols)
    return f"SELECT {lsel} FROM {_sub(sql)} UNION ALL SELECT {rsel} FROM {_sub(right)}"


# ── pivot / unpivot ───────────────────────────────────────────────────────────
@_register("pivot")
def op_pivot(sql, params, ctx):
    index = _as_list(_require(params, "index"))
    on = _as_list(_require(params, "on"))
    values = _require(params, "values")
    func = params.get("func", "sum")
    if func not in _AGG:
        raise EngineError(f"pivot: funzione non supportata '{func}'")
    if func == "median":
        raise EngineError("pivot: la mediana non e' disponibile nel pivot su BigQuery (usa un altro motore).")
    base = _sub(sql)
    on_sql = ", ".join(ctx.qi(c) for c in on)
    n_cols = ctx.scalar(f"SELECT COUNT(*) FROM (SELECT DISTINCT {on_sql} FROM {base})")
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: le colonne scelte hanno {n_cols} combinazioni distinte ({n_cols} colonne "
            f"nuove, massimo {MAX_PIVOT_COLUMNS}). Sono le colonne giuste?"
        )
    combos = ctx.distinct_rows(base, on)
    combos = sorted(combos, key=lambda combo: tuple(pivot_label(v) for v in combo))
    idx_sql = ", ".join(ctx.qi(c) for c in index)
    cols = [idx_sql] if idx_sql else []
    v = ctx.qi(values)
    for combo in combos:
        cond = " AND ".join(
            f"{ctx.qi(c)} IS NULL" if val is None else f"{ctx.qi(c)} = {_lit(val)}" for c, val in zip(on, combo)
        )
        colname = PIVOT_LABEL_SEP.join(pivot_label(val) for val in combo)
        # aggregazione condizionale: nessuna riga (o soli NULL) → NULL, tranne
        # count/n_unique → 0, come negli altri engine
        expr = _agg_expr(func, f"IF({cond}, {v}, NULL)")
        cols.append(f"{expr} AS {ctx.qi(colname)}")
    group = f" GROUP BY {idx_sql}" if idx_sql else ""
    return f"SELECT {', '.join(cols)} FROM {base}{group}"


def unpivot_value_type(types: list[str]) -> str:
    """Supertipo del valore sciolto (stessa regola degli altri engine): tutti
    interi → INT64; tutti numerici → FLOAT64; tutti uguali → quello; altrimenti STRING."""
    bases = [str(t).upper() for t in types]
    if all(b in _BQ_INTS for b in bases):
        return "INT64"
    if all(b in _BQ_INTS or b in _BQ_FLOATS or b in _BQ_DECIMALS for b in bases):
        return "FLOAT64"
    if len(set(bases)) == 1:
        return bases[0]
    return "STRING"


@_register("unpivot")
def op_unpivot(sql, params, ctx):
    on = params.get("on")
    index = params.get("index") or []
    var = params.get("variable_name") or "variable"
    val = params.get("value_name") or "value"
    schema = dict(ctx.schema_of(sql))
    allcols = list(schema)
    melt = on if on else [c for c in allcols if c not in set(index)]
    if not melt:
        raise EngineError("unpivot: nessuna colonna da sciogliere")
    missing = [c for c in melt if c not in schema]
    if missing:
        raise EngineError(f"unpivot: colonne inesistenti: {', '.join(missing)}")
    keep = [c for c in allcols if c in set(index)]
    vtype = unpivot_value_type([schema[c] for c in melt])

    def _val(c: str) -> str:
        q = ctx.qi(c)
        if vtype == "STRING" and str(schema[c]).upper() in _BQ_FLOATS:
            return (f"IF({q} = TRUNC({q}) AND ABS({q}) < 1e15, "
                    f"CONCAT(CAST(CAST({q} AS INT64) AS STRING), '.0'), CAST({q} AS STRING)) AS {q}")
        return f"CAST({q} AS {vtype}) AS {q}"

    # l'UNPIVOT nativo vuole colonne dello stesso tipo: prima le si porta al supertipo
    inner_cols = [ctx.qi(c) for c in keep] + [_val(c) for c in melt]
    inner = f"SELECT {', '.join(inner_cols)} FROM {_sub(sql)}"
    keep_sql = "".join(f"{ctx.qi(c)}, " for c in keep)
    return (
        f"SELECT {keep_sql}{ctx.qi(var)}, {ctx.qi(val)} FROM ({inner}) "
        f"UNPIVOT INCLUDE NULLS ({ctx.qi(val)} FOR {ctx.qi(var)} IN ({', '.join(ctx.qi(c) for c in melt)}))"
    )
