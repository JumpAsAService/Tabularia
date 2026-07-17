"""
Registry delle operazioni del flow.

Ogni operazione è una funzione pura `(lf, params, ctx) -> lf` che mappa un nodo
della IR (un dict `params`) in una trasformazione su `LazyFrame`. Aggiungere una
nuova operazione = registrare una funzione con `@register("nome")`.

Design volutamente dichiarativo: nessuna `eval` di codice arbitrario
(sicurezza + serializzabilità JSON per Celery). Le trasformazioni sono un set
chiuso e verificato. Unica eccezione controllata: `compute` parsa espressioni
SQL con `pl.sql_expr` — un parser di SOLE espressioni (niente statement, niente
I/O, niente Python), quindi il perimetro resta chiuso.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import polars as pl

from app.engine.base import DataSource
from app.engine.context import OperationContext
from app.engine.dtypes import resolve_dtype as _dtype
from app.engine.exceptions import EngineError, UnknownOperationError

# tipo di una funzione-operazione
OperationFn = Callable[[pl.LazyFrame, dict[str, Any], OperationContext], pl.LazyFrame]

_REGISTRY: dict[str, OperationFn] = {}


def register(name: str) -> Callable[[OperationFn], OperationFn]:
    def deco(fn: OperationFn) -> OperationFn:
        _REGISTRY[name] = fn
        return fn

    return deco


def get_operation(name: str) -> OperationFn:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownOperationError(name) from None


def available_operations() -> list[str]:
    return sorted(_REGISTRY)


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise EngineError(f"parametro obbligatorio mancante: '{key}'")
    return params[key]


# ─────────────────────────────────────────────────────────────────────────────
# Selezione / rimozione / rinomina colonne
# ─────────────────────────────────────────────────────────────────────────────
@register("select")
def op_select(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.select(_require(params, "columns"))


@register("drop")
def op_drop(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.drop(_require(params, "columns"))


@register("rename")
def op_rename(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.rename(_require(params, "mapping"))


@register("cast")
def op_cast(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"columns": {"col": "int", "altra": "float"}}
    columns: dict[str, str] = _require(params, "columns")
    return lf.with_columns([pl.col(c).cast(_dtype(t)) for c, t in columns.items()])


# ─────────────────────────────────────────────────────────────────────────────
# Filtro righe
# ─────────────────────────────────────────────────────────────────────────────
def _coerce_value(value: Any, dtype: pl.DataType | None) -> Any:
    """Adatta il valore del filtro al tipo della colonna.

    Il form manda sempre stringhe ISO ("2024-01-15", "2024-01-15T10:30:00"):
    se la colonna è temporale le convertiamo in date/datetime NATIVE, perché
    Polars rifiuta il confronto diretto data↔stringa.
    """
    if not isinstance(value, str) or dtype is None or not dtype.is_temporal():
        return value
    from datetime import date, datetime, time

    try:
        if dtype == pl.Date:
            return date.fromisoformat(value)
        if isinstance(dtype, pl.Datetime):
            return datetime.fromisoformat(value)
        if dtype == pl.Time:
            return time.fromisoformat(value)
    except ValueError:
        raise EngineError(
            f"valore '{value}' non valido per la colonna temporale: "
            f"usa il formato ISO (es. 2024-01-15 o 2024-01-15T10:30:00)"
        )
    return value


def _predicate(column: str, operator: str, value: Any, dtype: pl.DataType | None = None) -> pl.Expr:
    col = pl.col(column)
    match operator:
        case "eq" | "==":
            return col == _coerce_value(value, dtype)
        case "ne" | "!=":
            return col != _coerce_value(value, dtype)
        case "gt" | ">":
            return col > _coerce_value(value, dtype)
        case "ge" | ">=":
            return col >= _coerce_value(value, dtype)
        case "lt" | "<":
            return col < _coerce_value(value, dtype)
        case "le" | "<=":
            return col <= _coerce_value(value, dtype)
        case "in":
            return col.is_in([_coerce_value(v, dtype) for v in value])
        case "not_in":
            return ~col.is_in([_coerce_value(v, dtype) for v in value])
        case "between":
            # pl.lit espliciti: is_between interpreta le stringhe nude come
            # NOMI di colonna, non come valori
            lo, hi = (_coerce_value(v, dtype) for v in (value[0], value[1]))
            return col.is_between(pl.lit(lo), pl.lit(hi))
        case "contains":
            return col.cast(pl.String).str.contains(str(value))
        case "starts_with":
            return col.cast(pl.String).str.starts_with(str(value))
        case "ends_with":
            return col.cast(pl.String).str.ends_with(str(value))
        case "is_null":
            return col.is_null()
        case "is_not_null":
            return col.is_not_null()
        case _:
            raise EngineError(f"operatore di filtro non supportato: '{operator}'")


@register("filter")
def op_filter(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"column": "eta", "operator": "gt", "value": 18}
    #         {"column": "data", "operator": "between", "value": ["2024-01-01", "2024-12-31"]}
    column = _require(params, "column")
    operator = _require(params, "operator")
    value = params.get("value")
    # il dtype serve a convertire le stringhe ISO in date/datetime (solo schema, zero dati)
    dtype = lf.collect_schema().get(column)
    return lf.filter(_predicate(column, operator, value, dtype))


# ─────────────────────────────────────────────────────────────────────────────
# Ordinamento / limit / deduplica / null
# ─────────────────────────────────────────────────────────────────────────────
@register("sort")
def op_sort(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    by = _require(params, "by")
    descending = params.get("descending", False)
    return lf.sort(by, descending=descending)


@register("limit")
def op_limit(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.limit(_require(params, "n"))


@register("unique")
def op_unique(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.unique(subset=params.get("subset"), keep=params.get("keep", "any"))


@register("fill_null")
def op_fill_null(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # o {"value": 0} su tutte, o {"columns": {"col": 0, "altra": "n/d"}}
    if "columns" in params:
        cols: dict[str, Any] = params["columns"]
        return lf.with_columns([pl.col(c).fill_null(v) for c, v in cols.items()])
    return lf.fill_null(_require(params, "value"))


@register("drop_nulls")
def op_drop_nulls(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    return lf.drop_nulls(subset=params.get("subset"))


# ─────────────────────────────────────────────────────────────────────────────
# Aggregazione
# ─────────────────────────────────────────────────────────────────────────────
_AGG: dict[str, Callable[[str], pl.Expr]] = {
    "sum": lambda c: pl.col(c).sum(),
    "mean": lambda c: pl.col(c).mean(),
    "avg": lambda c: pl.col(c).mean(),
    "min": lambda c: pl.col(c).min(),
    "max": lambda c: pl.col(c).max(),
    "count": lambda c: pl.col(c).count(),
    "median": lambda c: pl.col(c).median(),
    "std": lambda c: pl.col(c).std(),
    "var": lambda c: pl.col(c).var(),
    "first": lambda c: pl.col(c).first(),
    "last": lambda c: pl.col(c).last(),
    "n_unique": lambda c: pl.col(c).n_unique(),
}


@register("group_by")
def op_group_by(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"by": ["paese"], "aggregations": [{"column": "vendite", "func": "sum", "alias": "tot"}]}
    by = _require(params, "by")
    aggs: list[dict[str, Any]] = _require(params, "aggregations")
    exprs: list[pl.Expr] = []
    for a in aggs:
        col = _require(a, "column")
        func = _require(a, "func")
        if func not in _AGG:
            raise EngineError(f"funzione di aggregazione non supportata: '{func}'")
        alias = a.get("alias") or f"{col}_{func}"
        exprs.append(_AGG[func](col).alias(alias))
    return lf.group_by(by).agg(exprs)


# ─────────────────────────────────────────────────────────────────────────────
# Reshape: pivot (righe → colonne) e unpivot (colonne → righe)
# ─────────────────────────────────────────────────────────────────────────────
# ogni valore distinto di `on` diventa una colonna nuova: oltre questa soglia è
# quasi certamente la colonna sbagliata, non un reshape voluto
MAX_PIVOT_COLUMNS = 500


@register("pivot")
def op_pivot(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"index": ["paese"], "on": "anno", "values": "vendite", "func": "sum"}
    index = _require(params, "index")
    on = _require(params, "on")
    values = _require(params, "values")
    func = params.get("func") or "sum"
    if isinstance(index, str):
        index = [index]
    if not index:
        raise EngineError("pivot: serve almeno una colonna indice (le chiavi di riga)")
    if func not in _AGG:
        raise EngineError(f"funzione di aggregazione non supportata: '{func}'")

    # 1) il grosso del lavoro resta lazy/STREAMING: l'aggregazione riduce il
    #    dataset a (gruppi indice × valori distinti di `on`) righe — piccolo
    #    per costruzione, qualunque sia la dimensione dell'input
    aggregated = (
        lf.group_by(index + [on]).agg(_AGG[func](values).alias(values)).collect(engine="streaming")
    )

    n_cols = aggregated.get_column(on).n_unique()
    if n_cols > MAX_PIVOT_COLUMNS:
        raise EngineError(
            f"pivot: '{on}' ha {n_cols} valori distinti, cioè {n_cols} colonne nuove "
            f"(massimo {MAX_PIVOT_COLUMNS}). È la colonna giusta?"
        )

    # 2) il reshape vero è eager (pivot non esiste in lazy) ma lavora sul
    #    risultato già aggregato; "first" è un no-op: le coppie (indice, on)
    #    sono uniche dopo la group_by
    out = aggregated.pivot(on, index=index, values=values, aggregate_function="first", sort_columns=True)
    return out.lazy()


@register("unpivot")
def op_unpivot(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"index": ["id"], "on": ["gen", "feb"] (vuoto = tutte le altre),
    #          "variable_name": "mese", "value_name": "valore"}
    index = params.get("index") or None
    on = params.get("on") or None
    return lf.unpivot(
        on=on,
        index=index,
        variable_name=params.get("variable_name") or "variable",
        value_name=params.get("value_name") or "value",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Join (legge una seconda sorgente tramite il context)
# ─────────────────────────────────────────────────────────────────────────────
def _build_right(right_ref: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    Costruisce il LazyFrame del lato destro del join.

    Due formati:
    - sub-flow: {"source": {bucket, key}, "operations": [{type, params}, ...]}
      → scan della sorgente + applicazione della catena (ramo trasformato).
    - legacy:   {"bucket": ..., "key": ...}
      → semplice scan di un parquet.
    """
    if "source" in right_ref:
        rlf = ctx.scan(DataSource(**right_ref["source"]))
        for op in right_ref.get("operations", []):
            fn = get_operation(op["type"])
            rlf = fn(rlf, op.get("params", {}), ctx)
        return rlf
    return ctx.scan(DataSource(**right_ref))


def _cross_join(lf: pl.LazyFrame, right_lf: pl.LazyFrame, ctx: OperationContext) -> pl.LazyFrame:
    """Cross join (prodotto cartesiano L×R) con guardia anti-OOM.

    Conta prima le righe dei due lati e, se il prodotto supera il tetto, RIFIUTA
    con un messaggio chiaro invece di far esplodere la RAM (il cross join è la
    causa n.1 di freeze). Niente campionamento: l'anteprima mostra il risultato
    VERO (o l'errore), mai un campione fuorviante. Il conteggio è in streaming:
    sui parquet è quasi gratis (metadati); sul lato sinistro esegue la catena a
    monte una volta (prezzo dell'assicurazione)."""
    cap = ctx.max_cross_join_rows
    if cap and cap > 0:
        left_n = lf.select(pl.len()).collect(engine="streaming").item()
        right_n = right_lf.select(pl.len()).collect(engine="streaming").item()
        if left_n * right_n > cap:
            raise EngineError(
                f"Il cross join produrrebbe {left_n * right_n:,} righe "
                f"({left_n:,} × {right_n:,}), oltre il limite di {cap:,}. "
                "Aggiungi una condizione di join (chiave) o riduci le sorgenti "
                "(filter/limit) prima di incrociarle."
            )
    return lf.join(right_lf, how="cross")


@register("join")
def op_join(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"right": <sorgente o sub-flow>,
    #          "on": ["id"]  (oppure "left_on"/"right_on"),
    #          "how": "inner"}
    right_ref = _require(params, "right")
    right_lf = _build_right(right_ref, ctx)
    how = params.get("how", "inner")

    # il cross join non usa colonne chiave — e può esplodere: protetto a parte
    if how == "cross":
        return _cross_join(lf, right_lf, ctx)

    if "on" in params:
        return lf.join(right_lf, on=params["on"], how=how)
    left_on = _require(params, "left_on")
    right_on = _require(params, "right_on")
    return lf.join(right_lf, left_on=left_on, right_on=right_on, how=how)


# ─────────────────────────────────────────────────────────────────────────────
# Union: accoda le righe di un altro ramo (stile Tableau Prep)
# ─────────────────────────────────────────────────────────────────────────────
@register("union")
def op_union(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    params: {"right": <sorgente o sub-flow, come il join>, "strategy": "relaxed"}

    Accoda le righe del ramo destro sotto quelle della catena. "relaxed"
    (default) allinea le colonne per nome: le mancanti da un lato diventano
    null, i tipi si riconciliano al supertipo. "strict" richiede schemi
    identici. Union di N sorgenti = nodi union in catena. Tutto lazy.
    """
    right_ref = _require(params, "right")
    right_lf = _build_right(right_ref, ctx)
    strategy = params.get("strategy") or "relaxed"
    if strategy not in ("relaxed", "strict"):
        raise EngineError(f"union: strategia non supportata: '{strategy}' (usa relaxed o strict)")
    how = "diagonal_relaxed" if strategy == "relaxed" else "vertical"
    return pl.concat([lf, right_lf], how=how)


# ─────────────────────────────────────────────────────────────────────────────
# Colonne calcolate: espressioni SQL (dialetto Polars) → espressioni lazy
# ─────────────────────────────────────────────────────────────────────────────
@register("compute")
def op_compute(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    params: {"columns": [{"name": "margine", "expr": "prezzo - costo"}, ...]}

    Le espressioni sono SQL nel dialetto di Polars: aritmetica, CASE WHEN,
    funzioni stringa/data e window functions con OVER (PARTITION BY /
    ORDER BY → semantica running per righe). `pl.sql_expr` parsa SOLO
    espressioni: niente statement né accesso a tabelle. Le colonne vengono
    applicate IN SEQUENZA: un'espressione può usare quelle definite prima;
    un nome già esistente sovrascrive la colonna. Tutto resta lazy.
    """
    columns = _require(params, "columns")
    if not isinstance(columns, list) or not columns:
        raise EngineError("compute: definisci almeno una colonna calcolata")
    for c in columns:
        name = str(c.get("name") or "").strip()
        expr_s = str(c.get("expr") or "").strip()
        if not name:
            raise EngineError("compute: ogni colonna calcolata deve avere un nome")
        if not expr_s:
            raise EngineError(f"compute: l'espressione di '{name}' è vuota")
        try:
            expr = pl.sql_expr(expr_s)
        except Exception as e:
            raise EngineError(
                f"compute: espressione di '{name}' non valida: {e}"
            ) from e
        lf = lf.with_columns(expr.alias(name))
    return lf


# ─────────────────────────────────────────────────────────────────────────────
# Execute SQL: una query SQL (dialetto Polars) sul frame in ingresso
# ─────────────────────────────────────────────────────────────────────────────
# Table function che leggono FILE dal disco del worker: Polars SQL le supporta,
# ma qui sono VIETATE — una query potrebbe leggere secret o i parquet di altri
# (es. `SELECT * FROM read_csv('/app/.../secrets.toml')`). L'unica tabella
# disponibile è l'input del nodo.
_SQL_FILE_FUNCS = re.compile(r"\b(?:read|scan)_(?:csv|parquet|ipc|ndjson|json)\s*\(", re.IGNORECASE)


@register("sql")
def op_sql(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    params: {"query": "SELECT ... FROM self ..."}

    Esegue una query SQL nel dialetto di Polars sul frame in ingresso, esposto
    come tabella `self` (alias `input`). Polars SQL è un motore CHIUSO sui frame
    in memoria — niente Python, nessun I/O — e resta LAZY (`SQLContext.execute`
    torna un LazyFrame → streamabile e cacheable), coerente con l'engine
    dichiarativo. Le table function che leggono file sono bloccate a monte.
    """
    query = str(_require(params, "query") or "").strip()
    if not query:
        raise EngineError("sql: la query è vuota")
    if _SQL_FILE_FUNCS.search(query):
        raise EngineError(
            "sql: le funzioni di lettura file (read_csv/read_parquet/…) non sono "
            "permesse. L'unica tabella disponibile è l'input del nodo (`self`)."
        )
    try:
        result = pl.SQLContext(frames={"self": lf, "input": lf}).execute(query)
        result.collect_schema()  # valida sintassi/colonne SUBITO (metadati, senza dati)
        return result
    except EngineError:
        raise
    except Exception as e:
        raise EngineError(f"sql: query non valida: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Foreach: ciclo con placeholder (stile container SSIS)
# ─────────────────────────────────────────────────────────────────────────────
# guardia per-livello: un driver/items enorme genererebbe migliaia di catene
MAX_FOREACH_ITERATIONS = 1000
# guardia cumulativa su TUTTA la catena di un run: i foreach annidati moltiplicano
# (1000^profondità), quindi il solo limite per-livello non basta. Conta le
# iterazioni totali costruite (outer × inner) e taglia l'esplosione.
MAX_FOREACH_TOTAL = 100_000

# tollerante: spazi attorno alla chiave ("{{ ProductKey }}") e chiavi con
# spazi/punteggiatura nel nome ("{{Customer ID}}" — capita nei nomi colonna)
_PLACEHOLDER = re.compile(r"\{\{\s*(.+?)\s*\}\}")


def _reject_dynamic_source_keys(obj: Any) -> None:
    """Vieta i placeholder `{{...}}` nei campi che INDIRIZZANO lo storage
    (`key`/`bucket`). Una sorgente il cui percorso viene risolto dai DATI di
    riga sfuggirebbe all'autorizzazione statica del gateway (che scandisce solo
    il payload): potresti leggere il parquet di un altro progetto mettendone la
    chiave in una cella del driver. I percorsi delle sorgenti devono venire dal
    grafo del flusso (concreti e autorizzati), mai dai dati.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("key", "bucket") and isinstance(v, str) and _PLACEHOLDER.search(v):
                raise EngineError(
                    "placeholder non ammessi nei percorsi sorgente (key/bucket): "
                    "una sorgente non può essere scelta dai dati dell'iterazione"
                )
            _reject_dynamic_source_keys(v)
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            _reject_dynamic_source_keys(x)


def _substitute(obj: Any, item: dict[str, Any]) -> Any:
    """Sostituisce ricorsivamente i placeholder {{chiave}} nei params del corpo.

    - stringa che è SOLO un placeholder ("{{soglia}}") → valore originale,
      tipo preservato (numero resta numero: utile per filter/limit);
    - placeholder immerso in testo ("vendite_{{paese}}") → sostituzione testuale;
    - dict/list → discesa ricorsiva.
    """
    if isinstance(obj, str):
        m = _PLACEHOLDER.fullmatch(obj.strip())
        if m:
            key = m.group(1)
            if key not in item:
                raise EngineError(
                    f"placeholder '{{{{{key}}}}}' non trovato: disponibili {sorted(item)}"
                )
            return item[key]
        def _repl(mm):
            key = mm.group(1)
            if key not in item:
                raise EngineError(
                    f"placeholder '{{{{{key}}}}}' non trovato: disponibili {sorted(item)}"
                )
            return str(item[key])
        return _PLACEHOLDER.sub(_repl, obj)
    if isinstance(obj, list):
        return [_substitute(x, item) for x in obj]
    if isinstance(obj, dict):
        # anche le CHIAVI possono essere placeholder (es. rename/cast/fill_null:
        # {"{{colonna}}": ...}); una chiave deve restare stringa
        out = {}
        for k, v in obj.items():
            nk = _substitute(k, item) if isinstance(k, str) else k
            out[nk if isinstance(nk, str) else str(nk)] = _substitute(v, item)
        return out
    return obj


@register("foreach")
def op_foreach(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    """
    Esegue il corpo per ogni iterazione e APPENDE i risultati (concat).

    params:
    - "body": [{type, params}, ...] — le operazioni del corpo; nei valori si
      usano i placeholder {{chiave}};
    - le iterazioni arrivano da UNO dei due:
      "driver": sub-flow ({"source": {...}, "operations": [...]}) — ogni RIGA
        è un'iterazione, le COLONNE sono i placeholder disponibili;
      "items": lista statica di dict [{chiave: valore, ...}, ...];
    - "add_keys_as_columns": true → aggiunge le chiavi dell'iterazione come
      colonne letterali all'output (per distinguere i blocchi appesi).

    Il risultato resta LAZY: N catene + concat, lo streaming è preservato.
    `diagonal_relaxed` tollera piccole differenze di schema tra iterazioni.
    """
    body = _require(params, "body")
    items = params.get("items")
    if not items and "driver" in params:
        driver_df = _build_right(params["driver"], ctx).collect(engine="streaming")
        if driver_df.height > MAX_FOREACH_ITERATIONS:
            raise EngineError(
                f"il driver ha {driver_df.height} righe: oltre il limite di "
                f"{MAX_FOREACH_ITERATIONS} iterazioni. Riduci il driver (unique/filter)."
            )
        items = driver_df.to_dicts()
    if not items:
        raise EngineError(
            "foreach senza iterazioni: collega un driver (input in alto) "
            "o definisci 'items' nei parametri"
        )
    # cap per-livello anche sugli items STATICI (il ramo driver è già limitato sopra)
    if len(items) > MAX_FOREACH_ITERATIONS:
        raise EngineError(
            f"foreach: {len(items)} iterazioni oltre il limite di {MAX_FOREACH_ITERATIONS}"
        )
    if not body:
        raise EngineError("foreach con corpo vuoto: trascina delle operazioni dentro il container")
    # una sorgente nel corpo non può avere il percorso deciso dai dati (bypass RBAC)
    _reject_dynamic_source_keys(body)

    # budget cumulativo: i foreach annidati vengono ricostruiti a ogni iterazione
    # esterna, quindi outer×inner passa da qui e viene tagliato prima dell'OOM.
    # (in esecuzione reale ctx c'è sempre; è None solo nei test delle op pure)
    if ctx is not None:
        ctx.foreach_iterations += len(items)
        if ctx.foreach_iterations > MAX_FOREACH_TOTAL:
            raise EngineError(
                f"foreach: troppe iterazioni totali ({ctx.foreach_iterations}) — "
                f"annidamento eccessivo (limite {MAX_FOREACH_TOTAL})"
            )

    add_keys = params.get("add_keys_as_columns", False)
    parts: list[pl.LazyFrame] = []
    for idx, item in enumerate(items):
        sub = lf
        try:
            for op in _substitute(body, item):
                fn = get_operation(op["type"])
                sub = fn(sub, op.get("params", {}), ctx)
        except EngineError as e:
            raise EngineError(f"iterazione {idx + 1}/{len(items)}: {e}") from e
        if add_keys:
            sub = sub.with_columns([pl.lit(v).alias(str(k)) for k, v in item.items()])
        parts.append(sub)
    return pl.concat(parts, how="diagonal_relaxed")
