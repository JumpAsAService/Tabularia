"""Compila l'IR di un flusso in modelli dbt e assembla il progetto.

NON è un engine: è un exporter/escape-hatch. Riusa gli stessi helper SQL PURI
dell'engine DuckDB (`duckdb_ops`) per non duplicare la logica non banale
(predicati, condizioni di join, mappe agg/dtype), sostituendo il `FROM <alias>`
dell'esecuzione con riferimenti dbt / CTE. Ogni modello è una catena di
operazioni resa come sequenza di CTE (`WITH s1 AS (...), s2 AS (...) SELECT *
FROM sN`), con il lato destro di join/union compilato come CTE aggiuntive. Da
dove parte un modello lo decide il gateway: una `source()`, un altro modello
(`ref()`: passi condivisi, query SQL, uscite di altri flussi) o un seed.

Lo schema (nomi colonna) è tracciato a compile-time perché alcune operazioni
ne hanno bisogno (compute → EXCLUDE se la colonna esiste; drop_nulls → subset
di default = tutte; join → colonne dei due lati con suffisso _right).

Due modalità: **duckdb** (dbt-duckdb federa i DB di origine; SQL DuckDB as-is)
e **native** (dbt nel warehouse di origine; SQL tradotto via sqlglot, con gli
star espansi sullo schema). `foreach` non è traducibile (control-flow a runtime)
→ errore esplicito; `pivot` lo è solo in DuckDB.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from app.engine.duckdb_ops import (
    _AGG,
    _DUCK_DTYPES,
    _as_list,
    _join_condition,
    _join_select,
    _JOIN_KW,
    _lit,
    _predicate,
    _qi,
    _require,
)
from app.engine.exceptions import EngineError

# resolver di una sorgente ({bucket, key} | {model} | {seed}) → (ref SQL dbt, nomi colonna)
SourceResolver = Callable[[dict], tuple[str, list[str]]]
SEED_MAX_ROWS = 50_000          # un seed dbt è un CSV nel repository: oltre, non ha senso


class DbtExportError(EngineError):
    pass


class _Namer:
    """Genera nomi di CTE univoci nel modello (s1, s2, r1s1, …)."""

    def __init__(self, prefix: str = "s"):
        self.prefix = prefix
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self.prefix}{self._n}"


class _Model:
    """Accumula le CTE di un modello e traccia lo schema corrente."""

    def __init__(self, namer: _Namer, native: bool = False):
        self.namer = namer
        self.native = native
        self.ctes: list[tuple[str, str]] = []  # (nome, sql)

    def add(self, sql: str) -> str:
        name = self.namer.next()
        self.ctes.append((name, sql))
        return name


def _cast_col(col: str, dt: str) -> str:
    duck_t = _DUCK_DTYPES.get(str(dt))
    if not duck_t:
        raise DbtExportError(f"cast: tipo non supportato '{dt}'")
    return f"TRY_CAST({_qi(col)} AS {duck_t}) AS {_qi(col)}"


def _compile_op(
    model: _Model, ref: str, cols: list[str], op_type: str, params: dict,
    resolve: SourceResolver, right_namer: _Namer,
) -> tuple[str, list[str]]:
    """Emette la/le CTE di una operazione. Ritorna (nuovo ref, nuove colonne).

    `ref` è il nome della CTE a monte; `cols` il suo schema (nomi colonna)."""
    q = _qi

    if op_type == "select":
        c = _require(params, "columns")
        sql = f"SELECT {', '.join(q(x) for x in c)} FROM {ref}"
        return model.add(sql), list(c)

    if op_type == "reorder":
        order = _require(params, "columns")
        qs = ", ".join(q(x) for x in order)
        sql = f"SELECT {qs}, * EXCLUDE ({qs}) FROM {ref}"
        return model.add(sql), list(order) + [c for c in cols if c not in set(order)]

    if op_type == "drop":
        drop = set(_require(params, "columns"))
        sql = f"SELECT * EXCLUDE ({', '.join(q(x) for x in drop)}) FROM {ref}"
        return model.add(sql), [x for x in cols if x not in drop]

    if op_type == "rename":
        mapping = _require(params, "mapping")
        pairs = ", ".join(f"{q(o)} AS {q(n)}" for o, n in mapping.items())
        sql = f"SELECT * RENAME ({pairs}) FROM {ref}"
        return model.add(sql), [mapping.get(x, x) for x in cols]

    if op_type == "cast":
        casts = _require(params, "columns")
        repl = ", ".join(_cast_col(c, dt) for c, dt in casts.items())
        return model.add(f"SELECT * REPLACE ({repl}) FROM {ref}"), cols

    if op_type == "filter":
        pred = _predicate(_require(params, "column"), params.get("operator", "eq"), params.get("value"))
        return model.add(f"SELECT * FROM {ref} WHERE {pred}"), cols

    if op_type == "sort":
        by = _as_list(_require(params, "by"))
        if params.get("ignore_missing") and cols:
            by = [c for c in by if c in set(cols)]
            if not by:
                return ref, cols
        direction = "DESC" if params.get("descending") else "ASC"
        order = ", ".join(f"{q(c)} {direction}" for c in by)
        return model.add(f"SELECT * FROM {ref} ORDER BY {order}"), cols

    if op_type == "limit":
        return model.add(f"SELECT * FROM {ref} LIMIT {int(_require(params, 'n'))}"), cols

    if op_type == "unique":
        subset = params.get("subset")
        if subset:
            part = ", ".join(q(c) for c in subset)
            sql = f"SELECT * FROM {ref} QUALIFY row_number() OVER (PARTITION BY {part}) = 1"
        else:
            sql = f"SELECT DISTINCT * FROM {ref}"
        return model.add(sql), cols

    if op_type == "fill_null":
        fills = _require(params, "columns")
        repl = ", ".join(f"COALESCE({q(c)}, {_lit(v)}) AS {q(c)}" for c, v in fills.items())
        return model.add(f"SELECT * REPLACE ({repl}) FROM {ref}"), cols

    if op_type == "drop_nulls":
        subset = params.get("subset") or cols
        cond = " AND ".join(f"{q(c)} IS NOT NULL" for c in subset)
        return model.add(f"SELECT * FROM {ref} WHERE {cond}"), cols

    if op_type == "group_by":
        by = _as_list(_require(params, "by"))
        aggs = _require(params, "aggregations")
        parts = [", ".join(q(c) for c in by)]
        out_cols = list(by)
        for agg in aggs:
            col, func = agg["column"], agg.get("func", "sum")
            if func not in _AGG:
                raise DbtExportError(f"group_by: funzione non supportata '{func}'")
            alias = agg.get("alias") or f"{col}_{func}"
            expr = f"count(DISTINCT {q(col)})" if func == "n_unique" else f"{_AGG[func]}({q(col)})"
            parts.append(f"{expr} AS {q(alias)}")
            out_cols.append(alias)
        by_sql = ", ".join(q(c) for c in by)
        return model.add(f"SELECT {', '.join(parts)} FROM {ref} GROUP BY {by_sql}"), out_cols

    if op_type == "compute":
        columns = _require(params, "columns")  # [{name, expr}]
        cur = ref
        out_cols = list(cols)
        for c in columns:
            name, expr = str(c.get("name") or "").strip(), str(c.get("expr") or "").strip()
            if not name or not expr:
                raise DbtExportError("compute: nome ed espressione sono obbligatori")
            exclude = f" EXCLUDE ({q(name)})" if name in out_cols else ""
            cur = model.add(f"SELECT *{exclude}, ({expr}) AS {q(name)} FROM {cur}")
            if name not in out_cols:
                out_cols.append(name)
        return cur, out_cols

    if op_type == "sql":
        query = str(_require(params, "query")).strip()
        # come l'engine: l'input è disponibile come `self` e `input`
        cte = f"WITH input AS (SELECT * FROM {ref}), self AS (SELECT * FROM {ref}) {query}"
        # non conosciamo lo schema di output di una query arbitraria: ripartiamo "*"
        return model.add(cte), []

    if op_type in ("join", "union"):
        r_ref, r_cols = _compile_right(model, _require(params, "right"), resolve, right_namer)
        if op_type == "union":
            strat = params.get("strategy")
            kw = "UNION ALL" if strat == "strict" else "UNION ALL BY NAME"
            sql = f"SELECT * FROM {ref} {kw} SELECT * FROM {r_ref}"
            merged = cols + [c for c in r_cols if c not in set(cols)]
            return model.add(sql), merged
        # join
        how = params.get("how", "inner")
        if how == "cross":
            sel = _join_select(ref, cols, r_ref, r_cols, set())
            out = cols + [c if c not in set(cols) else c + "_right" for c in r_cols]
            return model.add(f"SELECT {sel} FROM {ref} CROSS JOIN {r_ref}"), out
        clause, kind = _join_condition(ref, r_ref, params)
        if how in ("semi", "anti"):
            return model.add(f"SELECT {ref}.* FROM {ref} {how.upper()} JOIN {r_ref} {clause}"), cols
        if how not in _JOIN_KW:
            raise DbtExportError(f"join: tipo non supportato '{how}'")
        skip_right = set(_as_list(params["on"])) if kind == "using" else set()
        sel = _join_select(ref, cols, r_ref, r_cols, skip_right)
        out = list(cols) + [
            (c + "_right" if c in set(cols) else c) for c in r_cols if c not in skip_right
        ]
        return model.add(f"SELECT {sel} FROM {ref} {_JOIN_KW[how]} {r_ref} {clause}"), out

    if op_type == "pivot":
        if model.native:
            raise DbtExportError(
                "Il nodo «pivot» produce colonne decise dai dati a runtime: in dbt si rende "
                "solo con il PIVOT di DuckDB. Usa l'export «federato» (dbt-duckdb), oppure "
                "sostituisci il pivot con un nodo sql che elenchi le colonne."
            )
        index = _as_list(_require(params, "index"))
        on = _as_list(_require(params, "on"))
        values = _require(params, "values")
        func = params.get("func", "sum")
        if func not in _AGG:
            raise DbtExportError(f"pivot: funzione non supportata '{func}'")
        on_sql = ", ".join(q(c) for c in on)
        idx_sql = ", ".join(q(c) for c in index)
        sql = f"PIVOT {ref} ON {on_sql} USING {_AGG[func]}({q(values)}) GROUP BY {idx_sql}"
        return model.add(sql), []  # colonne dinamiche: schema non noto a compile-time

    if op_type == "unpivot":
        on = params.get("on")
        index = params.get("index") or []
        var = params.get("variable_name") or "variable"
        val = params.get("value_name") or "value"
        melt = on if on else [c for c in cols if c not in set(index)]
        if not melt:
            raise DbtExportError("unpivot: nessuna colonna da sciogliere")
        on_sql = ", ".join(q(c) for c in melt)
        sql = f"UNPIVOT (SELECT * FROM {ref}) ON {on_sql} INTO NAME {q(var)} VALUE {q(val)}"
        return model.add(sql), list(index) + [var, val]

    if op_type == "foreach":
        raise DbtExportError(
            "Il nodo «foreach» itera a runtime su una tabella driver e non ha un "
            "equivalente dbt: escludilo dal flusso o esporta i rami separatamente."
        )

    raise DbtExportError(f"operazione '{op_type}' non esportabile in dbt")


def _compile_right(model: _Model, ref_spec: dict, resolve: SourceResolver, right_namer: _Namer):
    """Compila il lato destro di un join/union (sotto-flow {source, operations}
    o sorgente semplice) come CTE aggiuntive del modello."""
    if "source" in ref_spec:
        src_ref, src_cols = resolve(ref_spec["source"])
        base = model.add(f"SELECT * FROM {src_ref}")
        return _compile_ops(model, base, src_cols, ref_spec.get("operations") or [], resolve, right_namer)
    src_ref, src_cols = resolve(ref_spec)
    return model.add(f"SELECT * FROM {src_ref}"), src_cols


def _compile_ops(model, ref, cols, operations, resolve, right_namer):
    for op in operations:
        op_type = op.get("type") if isinstance(op, dict) else op.type
        params = (op.get("params") if isinstance(op, dict) else op.params) or {}
        ref, cols = _compile_op(model, ref, cols, op_type, params, resolve, right_namer)
    return ref, cols


def compile_model_sql(source: dict, operations: list, resolve: SourceResolver, native: bool = False) -> tuple[str, list[str]]:
    """Compila (sorgente + operazioni) in un modello dbt completo. Ritorna
    (SQL del modello, nomi colonna di output)."""
    model = _Model(_Namer("s"), native=native)
    right_namer = _Namer("r")
    src_ref, src_cols = resolve(source)
    base = model.add(f"SELECT * FROM {src_ref}")
    final, out_cols = _compile_ops(model, base, src_cols, operations, resolve, right_namer)

    body = ",\n".join(f"{name} AS (\n  {sql}\n)" for name, sql in model.ctes)
    return f"WITH {body}\nSELECT * FROM {final}", out_cols


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
            raise DbtExportError("range: colonna e almeno un limite")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL AND ({' OR '.join(conds)})"
    if kind == "pattern":
        if not col or not test.get("regex"):
            raise DbtExportError("pattern: colonna e regex")
        return f"SELECT * FROM {t} WHERE {q(col)} IS NOT NULL AND NOT regexp_matches({q(col)}, {_lit(str(test['regex']))})"
    if kind == "row_count":
        conds = []
        if test.get("min") is not None:
            conds.append(f"COUNT(*) < {int(test['min'])}")
        if test.get("max") is not None:
            conds.append(f"COUNT(*) > {int(test['max'])}")
        if not conds:
            raise DbtExportError("row_count: almeno un limite")
        return f"SELECT COUNT(*) AS n FROM {t} HAVING {' OR '.join(conds)}"
    if kind == "unique_combination":
        cols = [c for c in (test.get("columns") or []) if c]
        if len(cols) < 2:
            raise DbtExportError("unique_combination: almeno due colonne")
        lista = ", ".join(q(c) for c in cols)
        return f"SELECT {lista}, COUNT(*) AS n FROM {t} GROUP BY {lista} HAVING COUNT(*) > 1"
    if kind == "expression":
        sql = str(test.get("sql") or "").strip()
        if not sql:
            raise DbtExportError("expression: espressione vuota")
        # un esito «non lo so» (null) non è un sì: la riga viola — come il valutatore
        return f"SELECT * FROM {t} WHERE NOT COALESCE(({sql}), FALSE)"
    raise DbtExportError(f"regola '{kind}' senza equivalente dbt")


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

    sg_schema = {ident: {c: "VARCHAR" for c in cols} for ident, cols in schema.items()}
    try:
        # validate_qualify_columns=False: espande gli star best-effort senza
        # fallire se un riferimento non è staticamente risolvibile (self-join,
        # colonne derivate) — l'esecuzione reale nel warehouse le risolve comunque.
        tree = qualify(
            sqlglot.parse_one(sql, read="duckdb"), schema=sg_schema, dialect="duckdb",
            validate_qualify_columns=False,
        )
        return _fix_for_dialect(tree, dialect).sql(dialect=dialect)
    except DbtExportError:
        raise
    except Exception as e:
        raise DbtExportError(
            f"traduzione nel dialetto '{dialect}' non riuscita ({e}). "
            "Il flusso è troppo complesso per l'export nativo — usa l'export «federato» (dbt-duckdb)."
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


def _attach_dsn(db_type: str, conn: dict, pw_env: str) -> str:
    """DSN di ATTACH per lo scanner DuckDB, password via env_var (mai in chiaro)."""
    host, port, db, user = conn["host"], conn["port"], conn["database"], conn["username"]
    pw = "{{ env_var('%s') }}" % pw_env
    if db_type == "postgresql":
        return f"dbname={db} host={host} port={port} user={user} password={pw}"
    if db_type in ("mysql", "mariadb"):
        return f"host={host} port={port} database={db} user={user} password={pw}"
    raise DbtExportError(f"federazione DuckDB non disponibile per '{db_type}'")


def _native_profile(project: str, native: dict) -> str:
    """profiles.yml per il warehouse NATIVO. Il dbt gira nel DB di origine; le
    trasformazioni sono già tradotte nel suo dialetto (sqlglot). Password via env_var."""
    db_type = native["db_type"]
    adapter = _NATIVE_ADAPTER.get(db_type)
    if adapter is None:
        raise DbtExportError(f"nessun adapter dbt nativo per '{db_type}'")
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
                            out.append("          - accepted_values:")
                            out.append("              values: [" + ", ".join(_y(v) for v in t.get("values") or []) + "]")
                            out.append(f"              config: {{severity: {t.get('severity', 'error')}}}")
                        else:
                            out.append(f"          - {t['kind']}:")
                            out.append(f"              config: {{severity: {t.get('severity', 'error')}}}")
    return "\n".join(out) + "\n"


def _sources_yaml(sources: list[dict]) -> str:
    out = ["version: 2", "", "sources:"]
    for s in sources:
        out.append(f"  - name: {s['name']}")
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
    return "{{ config(" + ", ".join(parti) + ") }}\n\n"


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
) -> dict[str, str]:
    """Assembla i file di un progetto dbt.

    `models`: [{name, sql, materialized, description, columns, schema?, alias?}].
    `sources`: [{name, database, schema, tables: [{name, description, columns}]}].
    `seeds`: [{name, csv, description, columns}]. `tests`: [{name, sql, severity, description}].
    Modalità:
    · **duckdb** (federazione): `attachments` = [{alias, db_type, conn, pw_env}].
    · **native** (warehouse): `native` = {db_type, conn, pw_env, target_schema}."""
    project = _slug(flow_name)
    files: dict[str, str] = {}
    seeds, tests, notes = seeds or [], tests or [], list(notes or [])

    if native:
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
            + (f"      extensions: [{', '.join(extensions)}]\n" if extensions else "")
            + (f"      attach:\n{attach}\n" if attach else "")
        )

    files["dbt_project.yml"] = (
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

    if sources:
        files["models/sources.yml"] = _sources_yaml(sources)
    files["models/schema.yml"] = _schema_yaml(models)
    for m in models:
        files[f"models/{_slug(m['name'])}.sql"] = _model_header(m) + m["sql"] + "\n"
    if any(m.get("schema") for m in models):
        files["macros/generate_schema_name.sql"] = _SCHEMA_MACRO
    for s in seeds:
        files[f"seeds/{_slug(s['name'])}.csv"] = s["csv"]
    if seeds:
        files["seeds/schema.yml"] = _seeds_yaml(seeds)
    for t in tests:
        files[f"tests/{_slug(t['name'])}.sql"] = (
            f"{{{{ config(severity='{t.get('severity', 'error')}') }}}}\n"
            + (f"-- {t['description']}\n" if t.get("description") else "")
            + t["sql"] + "\n"
        )

    if native:
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
    passi = "".join(f"export {e}=...\n" for e in pw_envs)
    if seeds:
        passi += "DBT_PROFILES_DIR=. dbt seed\n"
    passi += "DBT_PROFILES_DIR=. dbt run\n"
    if tests or any(c.get("tests") for m in models for c in (m.get("columns") or [])):
        passi += "DBT_PROFILES_DIR=. dbt test\n"
    files["README.md"] = (
        f"# dbt project — {flow_name}\n\n{intro}\n"
        + (f"{flow_description}\n\n" if flow_description else "")
        + "## Models\n\n" + "\n".join(cosa) + "\n\n"
        + "## Run\n\n```bash\n" + pip + "\n"
        + "# set the source DB password(s) (referenced via env_var in profiles.yml):\n"
        + passi + "```\n"
        + ("\n## Notes\n\n" + "\n".join(f"- {n}" for n in notes) + "\n" if notes else "")
    )
    return files


def _slug(name: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(name).lower())
    out = out.strip("_") or "model"
    return out if not out[0].isdigit() else f"m_{out}"
