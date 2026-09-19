"""
Registry delle operazioni per l'engine chDB (ClickHouse embedded), parallelo a
`duckdb_ops.py`. A differenza di DuckDB (oggetti relation lazy) chDB è PURO SQL:
ogni operazione riceve la SELECT dello step precedente e ne restituisce una nuova
che la avvolge in subquery — `SELECT … FROM (prev)`. La catena resta un unico
piano che ClickHouse ottimizza ed esegue in streaming (spill su disco).

Firma: `(sql, params, ctx)`. `ctx` serve per l'introspezione delle colonne
(`ctx.columns_of`) e per le operazioni MULTI-INPUT (join/union) che leggono il
lato destro (`ctx.build_right`).

Sicurezza: le operazioni strutturali usano solo nomi colonna / operatori /
valori (niente SQL libero). `compute` accetta espressioni scalari ma VIETA
subquery e table function di lettura (file/url/s3/…) → niente filesystem/rete.
`sql` (query intera) e `foreach` non sono ancora supportate su chDB.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from app.engine.context import MAX_CROSS_JOIN_ROWS
from app.engine.exceptions import EngineError
from app.engine.operations import MAX_PIVOT_COLUMNS, PIVOT_LABEL_SEP, SAMPLE_BUCKETS, pivot_label, sample_threshold
from app.engine.sql_guard import ensure_reads_only_input

ChdbOpFn = Callable[..., str]

_REGISTRY: dict[str, ChdbOpFn] = {}


def _register(name: str) -> Callable[[ChdbOpFn], ChdbOpFn]:
    def deco(fn: ChdbOpFn) -> ChdbOpFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_chdb_operation(name: str) -> ChdbOpFn:
    fn = _REGISTRY.get(name)
    if fn is None:
        raise EngineError(
            f"operazione '{name}' non ancora supportata dall'engine chDB. "
            "Usa il motore Polars o DuckDB per questo flusso (o questo nodo)."
        )
    return fn


# ── helper SQL (dialetto ClickHouse) ──────────────────────────────────────────
def _qi(name: str) -> str:
    """Quota un identificatore ClickHouse con i backtick."""
    return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"


def _lit(v: Any) -> str:
    """Letterale SQL da un valore Python (già parsato dal frontend)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
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
    """Avvolge una SELECT come sorgente di subquery."""
    return f"({sql})"


# cast "try": funzioni *OrNull di ClickHouse (NULL invece di errore)
_CH_CAST = {
    "int": "toInt64OrNull(toString({c}))",
    "float": "toFloat64OrNull(toString({c}))",
    "str": "CAST({c} AS Nullable(String))",
    "bool": "toUInt8OrNull(toString({c}))",
    "date": "toDateOrNull(toString({c}))",
    "datetime": "parseDateTimeBestEffortOrNull(toString({c}))",
}


# ── colonne ───────────────────────────────────────────────────────────────────
@_register("select")
def op_select(sql, params, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return f"SELECT {cols} FROM {_sub(sql)}"


@_register("reorder")
def op_reorder(sql, params, ctx):
    # colonne elencate prima (nell'ordine), poi le altre (EXCEPT degli elencati)
    order = _require(params, "columns")
    cols = ", ".join(_qi(c) for c in order)
    return f"SELECT {cols}, * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("drop")
def op_drop(sql, params, ctx):
    cols = ", ".join(_qi(c) for c in _require(params, "columns"))
    return f"SELECT * EXCEPT ({cols}) FROM {_sub(sql)}"


@_register("rename")
def op_rename(sql, params, ctx):
    mapping = _require(params, "mapping")
    # ClickHouse non ha `* RENAME`: ricostruiamo la lista mantenendo l'ordine
    cols = ctx.columns_of(sql)
    parts = [
        f"{_qi(c)} AS {_qi(mapping[c])}" if c in mapping else _qi(c) for c in cols
    ]
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)}"


def _cast_expr(c: str, base: str, dt: str) -> str:
    """Stesso STANDARD del cast di Polars/DuckDB (vedi operations.op_cast):
    fallito → NULL; testo con spazi tagliati; testo → intero solo se intero
    letterale; numero → intero per troncamento; float → testo con ".0" sui
    valori interi (ClickHouse stamperebbe 200.0 come "200")."""
    is_text = base == "String" or base.startswith("FixedString")
    is_num = base in _CH_FLOATS or base.startswith("Decimal")
    if dt == "int":
        if is_text:
            return f"toInt64OrNull(trimBoth({c}))"
        if is_num:
            return f"toInt64OrNull(toString(trunc({c})))"
        return f"accurateCastOrNull({c}, 'Int64')"
    if dt == "float":
        if is_text:
            return f"toFloat64OrNull(trimBoth({c}))"
        return f"accurateCastOrNull({c}, 'Float64')"
    if dt == "str":
        if base in _CH_FLOATS:
            return (f"if({c} = trunc({c}) AND abs({c}) < 1e15, "
                    f"concat(toString(toInt64({c})), '.0'), toString({c}))")
        return f"CAST({c} AS Nullable(String))"
    if dt == "date":
        if is_text:
            # ClickHouse è permissivo ("2024-02-30" → 1° marzo): valido solo se
            # la data riletta coincide col testo, come Polars/DuckDB
            return f"if(toString(toDateOrNull(trimBoth({c}))) = trimBoth({c}), toDateOrNull(trimBoth({c})), NULL)"
        return f"toDateOrNull(toString({c}))"
    if dt == "datetime":
        return f"parseDateTimeBestEffortOrNull(trimBoth(toString({c})))"
    if dt == "bool":
        return f"toUInt8OrNull(trimBoth(toString({c})))"
    raise EngineError(f"cast: tipo non supportato '{dt}'")


@_register("cast")
def op_cast(sql, params, ctx):
    cols = _require(params, "columns")
    schema = dict(ctx.schema_of(sql))
    repls = []
    for col, dt in cols.items():
        if str(dt) not in _CH_CAST:
            raise EngineError(f"cast: tipo non supportato '{dt}'")
        base = _ch_base_type(schema.get(col, ""))
        repls.append(f"{_cast_expr(_qi(col), base, str(dt))} AS {_qi(col)}")
    return f"SELECT * REPLACE ({', '.join(repls)}) FROM {_sub(sql)}"


# ── filtri ────────────────────────────────────────────────────────────────────
def _predicate(column: str, operator: str, value: Any) -> str:
    c = _qi(column)
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
        return f"position(toString({c}), {_lit(value)}) > 0"
    if operator == "starts_with":
        return f"startsWith(toString({c}), {_lit(value)})"
    if operator == "ends_with":
        return f"endsWith(toString({c}), {_lit(value)})"
    ops = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
    if operator not in ops:
        raise EngineError(f"filter: operatore non supportato '{operator}'")
    return f"{c} {ops[operator]} {_lit(value)}"


@_register("filter")
def op_filter(sql, params, ctx):
    column = _require(params, "column")
    operator = params.get("operator", "eq")
    pred = _predicate(column, operator, params.get("value"))
    return f"SELECT * FROM {_sub(sql)} WHERE {pred}"


# ── riordino / righe ──────────────────────────────────────────────────────────
@_register("sort")
def op_sort(sql, params, ctx):
    cols = _as_list(_require(params, "by"))
    # `ignore_missing`: lo mette SOLO l'iniezione del publish (gateway), mai
    # l'utente. Una chiave di ordinamento dichiarata sull'Output e poi sparita
    # dalla catena (colonna rinominata o tolta) non deve far fallire OGNI run
    # del flusso — e non è nemmeno togliibile dalla checklist, che mostra solo
    # le colonne esistenti. Un `sort` chiesto esplicitamente resta severo.
    if params.get("ignore_missing"):
        have = set(ctx.columns_of(sql))
        cols = [c for c in cols if c in have]
        if not cols:
            return sql
    direction = "DESC" if params.get("descending") else "ASC"
    # standard cross-engine: NULL sempre in coda (vedi operations.op_sort)
    order = ", ".join(f"{_qi(c)} {direction} NULLS LAST" for c in cols)
    return f"SELECT * FROM {_sub(sql)} ORDER BY {order}"


@_register("limit")
def op_limit(sql, params, ctx):
    return f"SELECT * FROM {_sub(sql)} LIMIT {int(_require(params, 'n'))}"


@_register("sample")
def op_sample(sql, params, ctx):
    # campione casuale deterministico: hash del contenuto della riga (+ seme)
    threshold, seed = sample_threshold(params)
    return f"SELECT * FROM {_sub(sql)} WHERE cityHash64({seed}, *) % {SAMPLE_BUCKETS} < {threshold}"


@_register("unique")
def op_unique(sql, params, ctx):
    subset = params.get("subset")
    if subset:
        part = ", ".join(_qi(c) for c in subset)
        return f"SELECT * FROM {_sub(sql)} LIMIT 1 BY {part}"
    return f"SELECT DISTINCT * FROM {_sub(sql)}"


@_register("fill_null")
def op_fill_null(sql, params, ctx):
    cols = _require(params, "columns")
    repls = ", ".join(f"coalesce({_qi(c)}, {_lit(v)}) AS {_qi(c)}" for c, v in cols.items())
    return f"SELECT * REPLACE ({repls}) FROM {_sub(sql)}"


@_register("drop_nulls")
def op_drop_nulls(sql, params, ctx):
    subset = params.get("subset") or ctx.columns_of(sql)
    cond = " AND ".join(f"{_qi(c)} IS NOT NULL" for c in subset)
    return f"SELECT * FROM {_sub(sql)} WHERE {cond}"


# ── aggregazione ──────────────────────────────────────────────────────────────
_AGG = {
    "sum": "sum", "mean": "avg", "min": "min", "max": "max", "count": "count",
    "median": "median", "std": "stddevSamp", "var": "varSamp",
    "first": "any", "last": "anyLast", "n_unique": "uniqExact",
}


@_register("group_by")
def op_group_by(sql, params, ctx):
    by = _as_list(_require(params, "by"))
    aggs = _require(params, "aggregations")
    by_sql = ", ".join(_qi(c) for c in by)
    parts = [by_sql]
    for agg in aggs:
        col, func = agg["column"], agg.get("func", "sum")
        if func not in _AGG:
            raise EngineError(f"group_by: funzione non supportata '{func}'")
        alias = agg.get("alias") or f"{col}_{func}"
        expr = f"uniqExact({_qi(col)})" if func == "n_unique" else f"{_AGG[func]}({_qi(col)})"
        parts.append(f"{expr} AS {_qi(alias)}")
    return f"SELECT {', '.join(parts)} FROM {_sub(sql)} GROUP BY {by_sql}"


# ── compute (espressioni scalari, SENZA subquery né lettura file/rete) ─────────
_FORBIDDEN_IN_EXPR = re.compile(
    r"\b(?:select|from|with|insert|attach|create|"
    r"file|url|s3|hdfs|remote|remoteSecure|mysql|postgresql|jdbc|odbc|"
    r"clusterAllReplicas|cluster|dictionary|merge|numbers|zeros)\b",
    re.IGNORECASE,
)


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
                "FROM o table function di lettura — solo espressioni scalari)."
            )
        expr = _utf8_functions(expr)
        if name in existing:
            # colonna esistente: sovrascritta NELLA SUA POSIZIONE (come Polars/DuckDB)
            sql = f"SELECT * REPLACE (({expr}) AS {_qi(name)}) FROM {_sub(sql)}"
        else:
            sql = f"SELECT *, ({expr}) AS {_qi(name)} FROM {_sub(sql)}"
        existing.add(name)
    return sql


# In ClickHouse upper/lower/length/substring/reverse lavorano sui BYTE: "Città"
# → "CITTà", length = 6. Le varianti *UTF8 danno lo stesso risultato di
# Polars/DuckDB (caratteri): la stessa espressione del compute vale ovunque.
_CH_UTF8_FUNCS = {
    "upper": "upperUTF8", "ucase": "upperUTF8", "lower": "lowerUTF8", "lcase": "lowerUTF8",
    "length": "lengthUTF8", "char_length": "lengthUTF8", "character_length": "lengthUTF8",
    "substring": "substringUTF8", "substr": "substringUTF8", "mid": "substringUTF8",
    "reverse": "reverseUTF8",
}
_CH_UTF8_RE = re.compile(r"\b(" + "|".join(_CH_UTF8_FUNCS) + r")\s*\(", re.IGNORECASE)


def _utf8_functions(expr: str) -> str:
    return _CH_UTF8_RE.sub(lambda m: _CH_UTF8_FUNCS[m.group(1).lower()] + "(", expr)


# ── Execute SQL (query libera sull'input, dialetto ClickHouse) ────────────────
# Table function di ACCESSO ESTERNO / ESECUZIONE: vietate. Non limitano lo SQL
# analitico (SELECT/JOIN/GROUP BY/window/subquery/CTE restano liberi) — bloccano
# solo lettura file / SSRF / RCE sul server, che romperebbero l'RBAC per tutti.
_SQL_FORBIDDEN_FUNCS = re.compile(
    r"\b(?:file|url|s3|s3Cluster|fileCluster|urlCluster|hdfs|hdfsCluster|"
    r"remote|remoteSecure|cluster|clusterAllReplicas|"
    r"mysql|postgresql|jdbc|odbc|mongodb|redis|sqlite|"
    r"azureBlobStorage|gcs|deltaLake|iceberg|hudi|executable|dictionary)\s*\(",
    re.IGNORECASE,
)
# statement/keyword pericolosi (DDL/DML, scrittura file, cambio settings, system)
_SQL_FORBIDDEN_KW = re.compile(
    r"\b(?:insert|attach|detach|create|alter|drop|truncate|optimize|rename|"
    r"grant|revoke|system|use|kill|set|into\s+outfile|into\s+dumpfile)\b",
    re.IGNORECASE,
)


@_register("sql")
def op_sql(sql, params, ctx):
    """Query SQL libera (dialetto ClickHouse) sull'input del nodo, esposto come
    CTE `self` (alias `input`) — stessa convenzione di Polars/DuckDB. Composabile
    come ogni altra op (resta una SELECT annidabile).

    Sicurezza: lo SQL analitico è libero, ma sono vietate le table function di
    accesso esterno/esecuzione (file/url/s3/remote/executable/…), i commenti e gli
    statement multipli — così non si trasforma in lettura file/SSRF/RCE sul server.
    """
    query = str(_require(params, "query")).strip().rstrip(";").strip()
    if not query:
        raise EngineError("sql: la query è vuota")
    # il nodo TRASFORMA il suo input: dev'essere referenziato come `self` o `input`
    if not re.search(r"\bfrom\s+(?:self|input)\b", query, re.IGNORECASE):
        raise EngineError("sql: la query deve leggere dall'input del nodo — usa `FROM self` (o `FROM input`).")
    # niente commenti (offuscherebbero i controlli) né più istruzioni
    if "--" in query or "/*" in query:
        raise EngineError("sql: i commenti (-- e /* */) non sono ammessi nel nodo SQL.")
    if ";" in query:
        raise EngineError("sql: è ammessa una sola istruzione SELECT (niente ';').")
    if _SQL_FORBIDDEN_FUNCS.search(query) or _SQL_FORBIDDEN_KW.search(query):
        raise EngineError(
            "sql: consentito solo interrogare l'input. Vietate le table function di "
            "accesso esterno (file/url/s3/remote/…), l'esecuzione (executable) e DDL/DML."
        )
    # lista BIANCA delle tabelle: la lista nera qui sopra resta come prima linea,
    # ma da sola non bastava (`merge()`, `information_schema`: vedi sql_guard)
    ensure_reads_only_input(query, "clickhouse")
    # espone l'input come `self` (e alias `input`); resta una SELECT annidabile
    return f"WITH input AS ({sql}), self AS (SELECT * FROM input) {query}"


# ── join / union (leggono il lato destro dalla sorgente annidata) ─────────────
def _join_condition(params: dict) -> tuple[str, str]:
    """Torna (clausola SQL, kind) dove kind='using' o 'on'."""
    if "on" in params and params["on"]:
        keys = ", ".join(_qi(c) for c in _as_list(params["on"]))
        return f"USING ({keys})", "using"
    lo = _as_list(_require(params, "left_on"))
    ro = _as_list(_require(params, "right_on"))
    if len(lo) != len(ro):
        raise EngineError("join: left_on e right_on devono avere lo stesso numero di colonne")
    cond = " AND ".join(f"l.{_qi(l)} = r.{_qi(r)}" for l, r in zip(lo, ro))
    return f"ON {cond}", "on"


def _join_select(lcols, rcols, skip_right: set, coalesce_keys: set = frozenset()) -> str:
    """Colonne del risultato: tutte da sinistra + quelle di destra (chiavi USING
    escluse), con suffisso _right sulle omonime — come Polars/DuckDB. Nei
    full/right join le chiavi USING sono coalesce(sinistra, destra): valorizzate
    anche nelle righe che esistono solo a destra."""
    lset = set(lcols)
    parts = [
        f"coalesce(l.{_qi(c)}, r.{_qi(c)}) AS {_qi(c)}" if c in coalesce_keys else f"l.{_qi(c)}"
        for c in lcols
    ]
    for c in rcols:
        if c in skip_right:
            continue
        parts.append(f"r.{_qi(c)} AS {_qi(c + '_right')}" if c in lset else f"r.{_qi(c)}")
    return ", ".join(parts)


_JOIN_KW = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL OUTER JOIN"}


@_register("join")
def op_join(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    how = params.get("how", "inner")
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    L, R = _sub(sql), _sub(right)

    if how == "cross":
        ln = ctx.scalar(f"SELECT count() FROM {L}")
        rn = ctx.scalar(f"SELECT count() FROM {R}")
        if MAX_CROSS_JOIN_ROWS and ln * rn > MAX_CROSS_JOIN_ROWS:
            raise EngineError(
                f"Il cross join produrrebbe {ln * rn:,} righe ({ln:,} × {rn:,}), "
                f"oltre il limite di {MAX_CROSS_JOIN_ROWS:,}. Aggiungi una condizione di join "
                "o riduci le sorgenti."
            )
        sel = _join_select(lcols, rcols, set())
        return f"SELECT {sel} FROM {L} AS l CROSS JOIN {R} AS r"

    clause, kind = _join_condition(params)
    if how in ("semi", "anti"):
        kw = "LEFT SEMI JOIN" if how == "semi" else "LEFT ANTI JOIN"
        return f"SELECT l.* FROM {L} AS l {kw} {R} AS r {clause}"
    if how not in _JOIN_KW:
        raise EngineError(f"join: tipo non supportato '{how}'")
    skip_right = set(_as_list(params["on"])) if kind == "using" else set()
    sel = _join_select(lcols, rcols, skip_right, skip_right if how in ("full", "right") else set())
    return f"SELECT {sel} FROM {L} AS l {_JOIN_KW[how]} {R} AS r {clause}"


@_register("union")
def op_union(sql, params, ctx):
    right = ctx.build_right(_require(params, "right"))
    if params.get("strategy") == "strict":
        # schema identico: UNION ALL posizionale
        return f"SELECT * FROM {_sub(sql)} UNION ALL SELECT * FROM {_sub(right)}"
    # relaxed: allinea per NOME (ClickHouse non ha UNION ALL BY NAME) → per ogni
    # colonna dell'unione, ciascun lato la seleziona o NULL se assente
    lcols, rcols = ctx.columns_of(sql), ctx.columns_of(right)
    allcols = lcols + [c for c in rcols if c not in set(lcols)]
    lset, rset = set(lcols), set(rcols)
    lsel = ", ".join((_qi(c) if c in lset else "NULL") + f" AS {_qi(c)}" for c in allcols)
    rsel = ", ".join((_qi(c) if c in rset else "NULL") + f" AS {_qi(c)}" for c in allcols)
    return f"SELECT {lsel} FROM {_sub(sql)} UNION ALL SELECT {rsel} FROM {_sub(right)}"


# ── pivot / unpivot (righe↔colonne) ───────────────────────────────────────────
# Stesso standard cross-engine di operations.py (`pivot_label`): nomi colonna,
# semantica NULL e tipi coincidono con Polars/DuckDB, così un flusso progettato
# su un engine gira identico su ClickHouse in produzione.
@_register("pivot")
def op_pivot(sql, params, ctx):
    index = _as_list(_require(params, "index"))
    on = _as_list(_require(params, "on"))  # più colonne = combinazioni
    values = _require(params, "values")
    func = params.get("func", "sum")
    if func not in _AGG:
        raise EngineError(f"pivot: funzione non supportata '{func}'")
    base = _sub(sql)
    on_sql = ", ".join(_qi(c) for c in on)
    n_cols = ctx.scalar(f"SELECT count() FROM (SELECT DISTINCT {on_sql} FROM {base})")
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: le colonne scelte hanno {n_cols} combinazioni distinte ({n_cols} colonne "
            f"nuove, massimo {MAX_PIVOT_COLUMNS}). Sono le colonne giuste?"
        )
    combos = ctx.distinct_rows(base, on)  # ClickHouse non ha PIVOT: colonne condizionali
    # ordine delle colonne nuove = ordine TESTUALE delle etichette (come Polars e
    # DuckDB, che ordinano la chiave stringa): "10" < "2", "null" tra "false" e "true"
    combos = sorted(combos, key=lambda combo: tuple(pivot_label(v) for v in combo))
    idx_sql = ", ".join(_qi(c) for c in index)
    cols = [idx_sql] if idx_sql else []
    for combo in combos:
        cond = " AND ".join(
            f"{_qi(c)} IS NULL" if v is None else f"{_qi(c)} = {_lit(v)}" for c, v in zip(on, combo)
        )
        colname = PIVOT_LABEL_SEP.join(pivot_label(v) for v in combo)
        # combinatore -If: aggrega solo le righe della combinazione; nessuna riga
        # (o soli NULL) → NULL, tranne count/n_unique → 0 (Int64 come altrove)
        expr = f"{_AGG[func]}If({_qi(values)}, {cond})"
        if func in ("count", "n_unique"):
            expr = f"toInt64({expr})"
        cols.append(f"{expr} AS {_qi(colname)}")
    return f"SELECT {', '.join(cols)} FROM {base} GROUP BY {idx_sql}"


# tipi ClickHouse → famiglia, per il supertipo del valore in unpivot
_CH_INTS = {f"Int{b}" for b in (8, 16, 32, 64, 128, 256)} | {f"UInt{b}" for b in (8, 16, 32, 64, 128, 256)}
_CH_FLOATS = {"Float32", "Float64"}


def temporal_safe_sql(ctx, sql: str) -> str:
    """ClickHouse esporta `Date` come UInt16 e `DateTime` (32 bit) come UInt32 —
    INTERI — in Arrow/Parquet (19727 al posto di 2024-01-05, 1704450600 al
    posto di 2024-01-05 10:30:00): ogni colonna di quei tipi, prodotta da
    cast/compute/funzioni di data, viene promossa a Date32/DateTime64, che
    escono come DATE/TIMESTAMP veri. Va applicato a ogni SQL FINALE (preview,
    run, cache): le sorgenti lette da parquet sono già Date32/DateTime64."""
    repls = []
    for name, t in ctx.schema_of(sql):
        base = _ch_base_type(t)
        if base == "Date":
            repls.append(f"toDate32({_qi(name)}) AS {_qi(name)}")
        elif base == "DateTime" or base.startswith("DateTime("):
            repls.append(f"toDateTime64({_qi(name)}, 0) AS {_qi(name)}")
    if not repls:
        return sql
    return f"SELECT * REPLACE ({', '.join(repls)}) FROM {_sub(sql)}"


def _ch_base_type(t: str) -> str:
    """Toglie i wrapper Nullable(...) / LowCardinality(...)."""
    t = t.strip()
    for w in ("Nullable(", "LowCardinality("):
        while t.startswith(w) and t.endswith(")"):
            t = t[len(w):-1].strip()
    return t


def unpivot_value_type(types: list[str]) -> str:
    """Supertipo del valore sciolto (stessa regola di Polars/DuckDB): tutti
    interi → Int64; tutti numerici → Float64; tutti dello stesso tipo → quello;
    altrimenti String."""
    bases = [_ch_base_type(t) for t in types]
    if all(b in _CH_INTS for b in bases):
        return "Int64"
    if all(b in _CH_INTS or b in _CH_FLOATS or b.startswith("Decimal") for b in bases):
        return "Float64"
    if len(set(bases)) == 1:
        return bases[0]
    return "String"


@_register("unpivot")
def op_unpivot(sql, params, ctx):
    on = params.get("on")
    index = params.get("index") or []
    var = params.get("variable_name") or "variable"
    val = params.get("value_name") or "value"
    schema = dict(ctx.schema_of(sql))  # nome → tipo ClickHouse
    allcols = list(schema)
    melt = on if on else [c for c in allcols if c not in set(index)]
    if not melt:
        raise EngineError("unpivot: nessuna colonna da sciogliere")
    missing = [c for c in melt if c not in schema]
    if missing:
        raise EngineError(f"unpivot: colonne inesistenti: {', '.join(missing)}")
    # come Polars: restano SOLO le colonne indice (le altre non sciolte si perdono)
    keep = [c for c in allcols if c in set(index)]
    keep_sql = "".join(f"{_qi(c)}, " for c in keep)
    # arrayJoin su tuple (nome, valore al supertipo comune): una riga per colonna
    vtype = unpivot_value_type([schema[c] for c in melt])

    def _val(c: str) -> str:
        if vtype == "String" and _ch_base_type(schema[c]) in _CH_FLOATS:
            # ClickHouse stampa 7.0 come "7": allineato a Polars/DuckDB ("7.0")
            q = _qi(c)
            return (f"if({q} = trunc({q}) AND abs({q}) < 1e15, "
                    f"concat(toString(toInt64({q})), '.0'), toString({q}))")
        return f"CAST({_qi(c)} AS Nullable({vtype}))"

    tuples = ", ".join(f"({_lit(c)}, {_val(c)})" for c in melt)
    base = _sub(sql)
    return (
        f"SELECT {keep_sql}tup.1 AS {_qi(var)}, tup.2 AS {_qi(val)} "
        f"FROM {base} ARRAY JOIN [{tuples}] AS tup"
    )
