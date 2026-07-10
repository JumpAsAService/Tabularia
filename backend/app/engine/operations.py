"""
Registry delle operazioni del flow.

Ogni operazione è una funzione pura `(lf, params, ctx) -> lf` che mappa un nodo
della IR (un dict `params`) in una trasformazione su `LazyFrame`. Aggiungere una
nuova operazione = registrare una funzione con `@register("nome")`.

Design volutamente dichiarativo: nessuna `eval` di espressioni arbitrarie
(sicurezza + serializzabilità JSON per Celery). Le trasformazioni sono un set
chiuso e verificato.
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


@register("join")
def op_join(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # params: {"right": <sorgente o sub-flow>,
    #          "on": ["id"]  (oppure "left_on"/"right_on"),
    #          "how": "inner"}
    right_ref = _require(params, "right")
    right_lf = _build_right(right_ref, ctx)
    how = params.get("how", "inner")

    # il cross join non usa colonne chiave
    if how == "cross":
        return lf.join(right_lf, how="cross")

    if "on" in params:
        return lf.join(right_lf, on=params["on"], how=how)
    left_on = _require(params, "left_on")
    right_on = _require(params, "right_on")
    return lf.join(right_lf, left_on=left_on, right_on=right_on, how=how)


# ─────────────────────────────────────────────────────────────────────────────
# Foreach: ciclo con placeholder (stile container SSIS)
# ─────────────────────────────────────────────────────────────────────────────
# guardia: un driver enorme genererebbe migliaia di catene concatenate
MAX_FOREACH_ITERATIONS = 1000

# tollerante: spazi attorno alla chiave ("{{ ProductKey }}") e chiavi con
# spazi/punteggiatura nel nome ("{{Customer ID}}" — capita nei nomi colonna)
_PLACEHOLDER = re.compile(r"\{\{\s*(.+?)\s*\}\}")


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
    if not body:
        raise EngineError("foreach con corpo vuoto: trascina delle operazioni dentro il container")

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
