"""dbt export: compiles the models the gateway resolved into a dbt project and returns
it as a zip. Two modes:
· **duckdb**: dbt-duckdb federates the source databases (ATTACH); DuckDB SQL as it is.
· **native**: dbt runs in the native warehouse; the SQL is translated into its dialect
  with sqlglot (the stars expanded with the sources' schema).
The gateway has resolved flow→models (outputs, shared steps, queries, upstream flows)
and sources→database tables / seeds already; here the models are compiled IN ORDER,
with a registry of every model's columns for whoever refers to it, the seeds' parquet
files are read and the project is assembled."""
from __future__ import annotations

import io
import logging
import os
import re
import secrets
import tempfile
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import get_settings

from app.engine.dbt_export import (
    SEED_MAX_ROWS,
    SEED_TEXT,
    TEST_IDENT,
    DbtExportError,
    ClickHouseSyntaxCheck,
    TargetClickHouse,
    _SQLGLOT_DIALECT,
    native_adapter,
    use_proposal,
    build_dbt_project,
    encode_seed,
    compile_model_sql,
    duck_type,
    neutralize_jinja,
    nome_in_postgres,
    quotable_name,
    cte_names,
    in_byte,
    over_the_limit,
    too_large,
    ref_label,
    put_back_texts,
    singular_test_sql,
    package_sources,
    column_tests,
    substitute_source_idents,
    typed_query,
    source_type,
    transpile_model,
    _fix_for_dialect,
    _slug,
    _tempi_clickhouse,
    _typed,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dbt", tags=["dbt"])


class ModelSpec(BaseModel):
    name: str
    kind: str = "output"              # output | intermediate | raw
    source: dict | None = None        # {key} | {model} | {seed}
    operations: list = []
    materialized: str = "table"
    raw_sql: str | None = None        # kind=raw: the SQL as it is (a datasource's query)
    description: str = ""
    columns: list = []                # [{name, description, tests:[{kind, severity, values?}]}]
    tests: list = []                  # [{id, kind, severity, column?, min?, max?, regex?, sql?}]
    schema_: str | None = None
    alias: str | None = None
    meta: dict = {}                   # where it comes from: flow, version, output, author, export
    tags: list = []
    translate: dict | None = None     # target clickhouse: la query (raw) va tradotta {connection_id}
    # the dialect its compute expressions and sql nodes are written in: its flow's engine's
    # (None: duckdb, as before this field existed)
    dialect: Literal["polars", "duckdb", "clickhouse", "bigquery"] | None = None

    model_config = {"populate_by_name": True}

    def __init__(self, **data):
        if "schema" in data and "schema_" not in data:
            data["schema_"] = data.pop("schema")
        super().__init__(**data)


class SeedSpec(BaseModel):
    name: str
    bucket: str
    key: str
    description: str = ""
    columns: list = []


class ExportRequest(BaseModel):
    flow_name: str
    flow_description: str = ""
    models: list[ModelSpec]
    # duckdb: entries {ref, columns}; native: entries {ident, macro, columns}
    source_map: dict
    mode: str = "duckdb"          # duckdb | native | clickhouse
    attachments: list = []        # duckdb: [{alias, db_type, conn, pw_env}]
    native: dict | None = None    # native: {db_type, conn, pw_env, target_schema}
    clickhouse: dict | None = None  # clickhouse: {connections: [{id, name, db_type, host, port, database, username, pw_env}], target_schema}
    layout: dict | None = None    # dialogo di download: {package: project|folder, folder, layers}
    schema_: str | None = Field(None, alias="schema", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")   # duckdb: the models' schema
    exported_by: str | None = None  # who exports: in the README only
    ai: dict | None = None          # dialog: what the AI wrote {model, summaries, translated}
    overrides: dict[str, str] = {}  # model → the translation the AI proposes (checked here before use)

    model_config = {"populate_by_name": True}
    sources: list = []            # [{name, database, schema, tables}]
    seeds: list[SeedSpec] = []
    notes: list[str] = []


@router.post("/export")
def export_dbt(req: ExportRequest) -> StreamingResponse:
    try:
        if req.mode == "native":
            native_adapter((req.native or {}).get("db_type"))   # MySQL/MariaDB: no native target (decision of 2026-10-16)
        ch = _target_clickhouse(req) if req.mode == "clickhouse" else None
        dialect = "clickhouse" if ch else (_SQLGLOT_DIALECT.get((req.native or {}).get("db_type")) if req.mode == "native" else None)
        seeds = _read_seeds(req.seeds, dialect)
        sources = package_sources(req.sources, req.layout)   # «already declared»: the folder package only
        sources = ch.sources(sources) if ch else sources
        _quotable_names(req, sources)
        notes = list(req.notes) + [_seed_note(s) for s in seeds if s["cambiate"]]
        models, tests = _compile(req, seeds, ch, notes)
        if ch:
            notes.extend(_timestamptz_note(req, ch))
        clickhouse = None
        if ch:
            cfg = get_settings().clickhouse_external
            clickhouse = {"host": cfg.host, "port": cfg.port, "secure": cfg.secure, "target_schema": (req.clickhouse or {}).get("target_schema"),
                          "federati": list(ch.federated.values())}
        files = build_dbt_project(
            req.flow_name, models, req.attachments if req.mode == "duckdb" else None, sources,
            native=req.native if req.mode == "native" else None, seeds=seeds, tests=tests,
            notes=notes, flow_description=req.flow_description, clickhouse=clickhouse,
            layout=req.layout, schema=req.schema_ if req.mode == "duckdb" else None, exported_by=req.exported_by,
            ai=_project_ai(req, models),
        )
    except DbtExportError as e:
        # to the gateway: the code and the parameters (it says it in the reader's language), the text for the logs
        raise HTTPException(status_code=422, detail={"code": e.code, "params": e.params, "message": str(e)})

    # DETERMINISTIC: the same flow, version and options → the same zip, byte for byte
    # (files in order, a fixed date: otherwise every entry carries the export's time)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            item = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            # a lone surrogate (half an emoji from a browser) cannot be written in UTF-8: U+FFFD, not a 500
            zf.writestr(item, re.sub("[\ud800-\udfff]", "�", files[path]) if isinstance(files[path], str) else files[path])
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip")


def _quotable_names(req: "ExportRequest", sources: list[dict]) -> None:
    """Every name the project's adapter must write — a source's schema and tables (the team's
    «already declared» ones are theirs), a model's table and schema, the project's schema — is
    one it can write (`name_not_quotable`, as the gateway says before the download)."""
    if req.mode == "native":
        adapter, schema = "dbt-" + native_adapter((req.native or {}).get("db_type")), (req.native or {}).get("target_schema")
    elif req.mode == "clickhouse":
        adapter, schema = "dbt-clickhouse", (req.clickhouse or {}).get("target_schema")
    else:
        adapter, schema = "dbt-duckdb", req.schema_
    quotable_name(schema, adapter, "schema")
    for s in sources:
        if s.get("declared"):
            continue
        quotable_name(s.get("schema"), adapter, "source")
        for t in s.get("tables") or []:
            quotable_name(t["name"] if isinstance(t, dict) else t, adapter, "source")
    for m in req.models:
        quotable_name(m.schema_, adapter, "schema")
        quotable_name(m.alias, adapter, "relation")


def _project_ai(req: "ExportRequest", models: list[dict]) -> dict | None:
    """For the README: what the AI wrote (summaries from the gateway, models translated by
    the engine) and whether there are marked descriptions."""
    if not req.ai and not any(m.get("ai_translated") for m in models):
        return None
    ai = dict(req.ai or {})
    ai["translated"] = [m["name"] for m in models if m.get("ai_translated")]
    ai["described"] = any(str(c.get("description") or "").startswith("(AI) ") for m in models for c in (m.get("columns") or []))
    return ai


def _type_in_dialect(duck_t: str, dialect: str | None) -> str:
    """A DuckDB type written as the warehouse wants it (for the seeds' `column_types`)."""
    import sqlglot

    kind = sqlglot.exp.DataType.build(duck_t, dialect="duckdb")
    if dialect == "postgres" and kind.this == sqlglot.exp.DataType.Type.UBIGINT:
        kind = sqlglot.exp.DataType.build("DECIMAL(20, 0)")     # Postgres has no unsigned integer (UInt64)
    if dialect == "clickhouse":
        kind = _tempi_clickhouse(kind)                          # Date32, DateTime64(6), a time as text
    return kind.sql(dialect=dialect or "duckdb")


def _read_seeds(seeds: list[SeedSpec], dialect: str | None = None) -> list[dict]:
    """The seeds' parquet → CSV (with a cap: a seed is a file in the repository). Every seed
    carries its columns' TYPES: dbt declares them (`column_types`) instead of guessing
    them from the CSV — a code «007» stays text, it does not become 7."""
    if not seeds:
        return []
    import polars as pl

    from app.utils import get_storage_service

    out = []
    storage = get_storage_service()
    for s in seeds:
        fd, path = tempfile.mkstemp(suffix=".parquet", prefix="dbt_seed_")
        os.close(fd)
        try:
            storage.download_file(s.bucket, s.key, path)
            lf = pl.scan_parquet(path)
            n = lf.select(pl.len()).collect().item()
            if n > SEED_MAX_ROWS:
                raise DbtExportError(
                    f"the source «{s.name}» has {n:,} rows: too many for a dbt seed (at most {SEED_MAX_ROWS:,}). "
                    "Import it into a database and use that as the source.",
                    "seed_too_large", source=s.name, rows=f"{n:,}", max=f"{SEED_MAX_ROWS:,}",
                )
            # the texts dbt's seed loaders would change ('' and 'null' as NULL, spaces cut…) written as `~…`
            df, changed = encode_seed(lf.collect())
            csv = df.write_csv()
        except DbtExportError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"seed «{s.name}»: the file cannot be read ({e})", "seed_unreadable", seed=s.name, detail=str(e)[:200])
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        columns = s.columns or [{"name": c, "description": ""} for c in df.columns]
        tipi = {c: duck_type(str(t)) for c, t in df.schema.items()}
        out.append({"name": s.name, "csv": csv, "description": s.description, "columns": columns, "_cols": list(df.columns),
                    "_types": {c: SEED_TEXT if c in changed else t for c, t in tipi.items()}, "cambiate": changed,
                    "column_types": {c: _type_in_dialect(t, dialect) for c, t in tipi.items() if t}})
    return out


def _seed_note(seed: dict) -> str:
    """README: the seed whose texts are written as `~…`, and why."""
    columns = ", ".join(f"`{c}`" for c in seed["cambiate"])
    return (f"In the seed `{seed['name']}`, the texts of {columns} that dbt would load changed (an empty text, `null`, a blank "
            "text or `\\N` as NULL, the spaces at the ends cut, `\\r` as a newline) are written `~` + the text in the CSV, with `~`, "
            "space, tab and `\\r` written `~0`, `~1`, `~2`, `~3`. The first step of every model that reads the seed gives them "
            "back as Tabularia reads them: read the seed through those models.")


def _target_clickhouse(req: ExportRequest) -> TargetClickHouse:

    if not req.clickhouse:
        raise DbtExportError("clickhouse mode without the source connections")
    return TargetClickHouse(req.clickhouse.get("connections") or [], get_settings().clickhouse_external.host)


def _compile(req: ExportRequest, seeds: list[dict], ch: TargetClickHouse | None = None,
             notes: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """The models in order, with a registry of each one's columns; the singular tests from
    the contracts. In native (and on ClickHouse) every reference is a placeholder
    identifier until sqlglot has translated it."""
    native = req.mode in ("native", "clickhouse")
    dialect = "clickhouse" if ch else None
    if req.mode == "native":
        if not req.native:
            raise DbtExportError("native mode without the warehouse configuration")
        dialect = _SQLGLOT_DIALECT.get(req.native.get("db_type"))
        if dialect is None:
            raise DbtExportError(f"no sqlglot dialect for '{req.native.get('db_type')}'")
    elif req.mode == "clickhouse" and ch is None:
        raise DbtExportError("clickhouse mode without the source connections")

    registry: dict[str, tuple[list[str], dict]] = {}   # model → (output columns, types)
    seed_cols = {s["name"]: (s["_cols"], s["_types"]) for s in seeds}
    # a database table read live: a timestamptz stays one until the first step (see source_type)
    tipi_di = lambda e: {c: source_type(t) for c, t in (e.get("types") or {}).items()}
    ident_to_macro: dict[str, str] = {e["ident"]: e["macro"] for e in req.source_map.values()} if native else {}
    schema: dict[str, list[str]] = {e["ident"]: e.get("columns") or [] for e in req.source_map.values()} if native else {}
    ident_di: dict[str, str] = {}                  # model/seed → identifier (native)
    # Federated: the SQL is not translated, so the macros would sit in it next to the
    # user's text, and the Jinja neutralisation must not touch OURS. Each macro is a
    # placeholder until the text is neutralised; the marker is random (nobody can write
    # it in a flow) and never reaches the zip, which stays the same byte for byte.
    marker = f"__tabularia_{secrets.token_hex(8)}_"
    placeholders: dict[str, str] = {}                # macro → segnaposto (federato)

    def placeholder(macro: str) -> str:
        if macro not in placeholders:
            placeholders[macro] = f"{marker}{len(placeholders)}__"
        return placeholders[macro]

    def ident_per(nome: str, cols: list[str]) -> str:
        if nome not in ident_di:
            ident_di[nome] = f"__m_{len(ident_di)}"
            ident_to_macro[ident_di[nome]] = "{{ ref('%s') }}" % nome
            schema[ident_di[nome]] = cols
        return ident_di[nome]

    def resolve(src: dict):
        if src.get("model") is not None:
            nome = src["model"]
            if nome not in registry:
                raise DbtExportError(f"model referenced before it was compiled: {nome}")
            cols, tipi = registry[nome]
            return (ident_per(nome, cols) if native else placeholder("{{ ref('%s') }}" % nome)), cols, tipi, nome
        if src.get("seed") is not None:
            nome = src["seed"]
            if nome not in seed_cols:
                raise DbtExportError(f"seed not read: {nome}")
            cols, tipi = seed_cols[nome]
            return (ident_per(nome, cols) if native else placeholder("{{ ref('%s') }}" % nome)), cols, tipi, nome
        e = req.source_map.get(src.get("key"))
        if e is None:
            raise DbtExportError(f"unmapped source: {src.get('key')}")
        if native:
            return e["ident"], e.get("columns") or [], tipi_di(e), e.get("label")
        return placeholder(e["ref"]), e.get("columns") or [], tipi_di(e), e.get("label") or ref_label(e["ref"])

    def con_le_macro(sql: str) -> str:
        """Federated: the user's text neutralised, then the exporter's macros in place."""
        sql = neutralize_jinja(sql)
        for macro, s in placeholders.items():
            sql = sql.replace(s, macro)
        return sql

    models, tests = [], []
    used: set[str] = set()         # the CTE names of the whole project: no model repeats one (#62)
    test_files: set[str] = set()   # a singular test is the file tests/<slug of its name>.sql: one name, one file

    def nome_del_test(nome: str) -> str:
        """The name itself, or with `_2`, `_3`… when another test has its file (two columns
        `Codice` and `code` give the same slug)."""
        chosen, n = nome, 2
        while _slug(chosen) in test_files:
            chosen, n = f"{nome}_{n}", n + 1
        test_files.add(_slug(chosen))
        return chosen
    sizes: dict[str, tuple[int, bool, set]] = {}   # ClickHouse: model → (bytes, ephemeral?, models it refs)
    translated: dict[str, str] = {}   # model → the dialect the AI translated it from
    # the ClickHouse SQL is read by a real ClickHouse parser (sqlglot is wrong silently)
    check = ClickHouseSyntaxCheck() if dialect == "clickhouse" else None
    try:
        for m in req.models:
            info: dict = {}     # what compile_model_sql tells besides the SQL
            if m.kind == "raw":
                cols = [c["name"] for c in m.columns if c.get("name")]
                dtypes = {c["name"]: c.get("dtype") for c in m.columns if c.get("name")}
                tipi = {c: duck_type(dtypes.get(c)) for c in cols}   # after _typed_raw: a timestamptz is a naive UTC TIMESTAMP
                sql = (m.raw_sql or "").strip()
                if ch and m.translate:
                    # ClickHouse target: the query, written for the source, runs in ClickHouse on the federated databases
                    origin = ch.conns.get(int(m.translate.get("connection_id")), {}).get("db_type", "")
                    proposal = req.overrides.get(m.name)
                    original = sql
                    try:
                        sql = ch.translate_query(sql, m.translate.get("connection_id"), m.translate.get("label") or m.name,
                                               proposal=proposal, columns=cols if proposal is not None else None)
                        if over_the_limit(in_byte(sql)):
                            raise too_large(m.name, in_byte(sql))
                        error = check.error(sql) if check else None
                        if error and proposal is not None:
                            raise DbtExportError(f"the AI's proposal for «{m.name}» does not pass the checks: ClickHouse cannot parse it ({error})",
                                                 "ai_translation_invalid", model=m.name, reason="syntax", detail=error)
                        if error:
                            label = m.translate.get("label") or m.name
                            raise DbtExportError(f"the translated query of the datasource «{label}» cannot be parsed by ClickHouse ({error})",
                                                 "query_not_translatable", datasource=label, detail=error)
                    except DbtExportError as e:
                        if e.code in ("query_not_translatable", "query_not_single_select") and proposal is None:
                            # to the gateway what it needs to ask the AI for a proposal (if it chose so)
                            e.params.update(model=m.name, sql=original, source_dialect=_SQLGLOT_DIALECT.get(origin, origin),
                                            target_dialect="clickhouse", columns=cols)
                        raise
                    if proposal is not None:
                        translated[m.name] = _SQLGLOT_DIALECT.get(origin, origin)
                    if origin != "clickhouse" and notes is not None:
                        notes.append(f"The model `{m.name}` is the SQL query of a datasource, translated from {origin} to ClickHouse "
                                     "by sqlglot, with its tables read from the `tab_src_*` databases: review it.")
                used.update(cte_names(sql, "clickhouse" if (ch and m.translate) else (dialect or "duckdb")))
                # the query is the user's text: no macro of ours in it, all of it neutralised
                sql = neutralize_jinja(_typed_raw(sql, cols, dtypes, dialect))
            else:
                if not m.source:
                    raise DbtExportError(f"the model «{m.name}» has no source")
                raw, cols, tipi = compile_model_sql(m.source, m.operations, resolve, native=native, dialect=dialect, info=info,
                                                    text=m.dialect, used=used)
                to_review = info.get("da_rivedere") or []
                if to_review and notes is not None:
                    notes.extend(_review_note(m.name, to_review))
                if native and m.name in req.overrides:
                    # the proposal may read only what THIS model reads, not every source of the project
                    of_model = {i: mac for i, mac in ident_to_macro.items() if re.search(r"(?<!\w)" + re.escape(i) + r"(?!\w)", raw)}
                    sql = use_proposal(req.overrides[m.name], dialect, of_model, cols, m.name, check)
                    used.update(cte_names(req.overrides[m.name], dialect))
                    translated[m.name] = "duckdb"
                elif native:
                    try:
                        # the texts written in the warehouse's dialect go back in after sqlglot
                        translated_one = put_back_texts(transpile_model(raw, dialect, schema), info)
                        if check and over_the_limit(in_byte(translated_one)):
                            raise too_large(m.name, in_byte(translated_one))   # not «not translatable»: no AI asked for it
                        error = check.error(translated_one) if check else None   # before the macros: no Jinja
                        if error:
                            raise DbtExportError(f"translation to the '{dialect}' dialect failed: ClickHouse cannot parse it ({error})",
                                                 "translation_failed", dialect=dialect, detail=error)
                        sql = substitute_source_idents(neutralize_jinja(translated_one), ident_to_macro)
                    except DbtExportError as e:
                        if e.code == "translation_failed":
                            # to whoever translates it (the AI): the model in DuckDB, without our placeholders
                            e.params.update(model=m.name, sql=put_back_texts(raw, {"segnaposti": info.get("per_sonda")}), source_dialect="duckdb",
                                            target_dialect=dialect, columns=cols)
                        raise
                else:
                    sql = con_le_macro(raw)
            registry[m.name] = (cols, tipi)
            if dialect == "clickhouse" and m.materialized not in ("view", "ephemeral"):
                # dbt-clickhouse writes a table's column list as "…" without escaping a double quote
                # in it: the model failed at `dbt run` (a view, an ephemeral model and the other adapters build it)
                for c in cols:
                    if '"' in c:
                        raise DbtExportError(f"the column «{c}» of «{m.name}» has a double quote, which dbt-clickhouse cannot write in a "
                                             "table's column list: rename it in the flow, or materialize the model as a view",
                                             "column_name_unsupported", model=m.name, column=c, character='"', adapter="dbt-clickhouse")
            if dialect == "clickhouse":
                # dbt runs a model with the ephemeral models it reads inlined: ClickHouse reads them as ONE query
                sizes[m.name] = (in_byte(sql), m.materialized == "ephemeral", set(re.findall(r"\{\{ ref\('([^']*)'\) \}\}", sql)))
                total = _with_ephemerals(m.name, sizes)
                if m.materialized != "ephemeral" and over_the_limit(total):
                    raise too_large(m.name, total)
            if dialect == "postgres" and notes is not None:
                notes.extend(f"In `{m.name}`, the column `{c}` is `{nome_in_postgres(c)}` in Postgres, which keeps only the first 63 "
                             "bytes of a name." for c in cols if nome_in_postgres(c) != c)
            # the column rules dbt's generic tests do not run as Tabularia does become singular tests
            columns, of_column = column_tests(m.columns)
            models.append({"name": m.name, "kind": m.kind, "sql": sql, "materialized": m.materialized, "description": m.description,
                           "da_rivedere": info.get("da_rivedere"),
                           "columns": columns, "schema": m.schema_, "alias": m.alias, "tags": m.tags,
                           "meta": {**m.meta, "ai_translated": True} if m.name in translated else m.meta,
                           "ai_translated": m.name in translated, "ai_translated_from": translated.get(m.name)})
            for t in [*m.tests, *of_column]:
                tsql = singular_test_sql(t, tipi, dialect)
                if native:
                    tsql = transpile_model(tsql, dialect, {TEST_IDENT: cols})
                # the rule's text (an expression, a regex, a bound) neutralised, then the ref() to the model
                tsql = substitute_source_idents(neutralize_jinja(tsql), {TEST_IDENT: "{{ ref('%s') }}" % m.name})
                if t in of_column:
                    nome, description = f"{m.name}__{t['kind']}_{t.get('id') or t['column']}", \
                        f"Contract rule {t['kind']} on {m.name}, column «{t['column']}»"
                else:
                    nome, description = f"{m.name}__{t.get('kind')}_{t.get('id') or len(tests) + 1}", f"Contract rule {t.get('kind')} on {m.name}"
                tests.append({"name": nome_del_test(nome), "sql": tsql, "severity": t.get("severity", "error"), "description": description})
    finally:
        if check:
            check.close()
    if check and check.skipped and notes is not None:
        notes.append("The ClickHouse SQL of this project was not read by a ClickHouse parser at export time (none answered): "
                     "an error Tabularia would have caught shows at the first `dbt run`.")
    return models, tests


def _timestamptz_note(req: ExportRequest, ch: TargetClickHouse) -> list[str]:
    """README (ClickHouse target): ClickHouse's PostgreSQL engine reads a timestamptz as the
    wall-clock time of its Postgres session and drops the offset — on a server in Europe/Rome
    10:30 UTC came out 12:30. Tabularia reads the instant in UTC; a session in UTC gives the same."""
    zone = lambda t: source_type(t) == "TIMESTAMPTZ"   # noqa: E731
    pg = {int(i): c for i, c in ch.conns.items() if c.get("db_type") == "postgresql"}
    users: set[str] = set()
    if any(zone(t) for e in req.source_map.values() for t in (e.get("types") or {}).values()):
        users |= {str(c.get("username") or "") for c in pg.values()}
    for m in req.models:
        if m.kind == "raw" and m.translate and any(zone(c.get("dtype")) for c in m.columns):
            c = pg.get(int(m.translate.get("connection_id") or 0))
            if c:
                users.add(str(c.get("username") or ""))
    users.discard("")
    if not users:
        return []
    role = lambda u: u if re.fullmatch(r"[a-z_][a-z0-9_]*", u) else '"' + u.replace('"', '""') + '"'   # noqa: E731
    return ["ClickHouse reads a Postgres `timestamptz` as the wall-clock time of its Postgres session and drops the offset: unless "
            "the Postgres server works in UTC, give the user ClickHouse connects with the UTC time zone ("
            + ", ".join(f"`ALTER ROLE {role(u)} SET timezone = 'UTC'`" for u in sorted(users))
            + "), or every instant shifts by the zone's offset. Tabularia reads them in UTC."]


def _with_ephemerals(nome: str, sizes: dict[str, tuple[int, bool, set]]) -> int:
    """The bytes of a model with every ephemeral model dbt inlines into it (those it reads,
    and theirs, once each)."""
    seen, to_see, total = {nome}, list(sizes[nome][2]), sizes[nome][0]
    while to_see:
        r = to_see.pop()
        if r in seen or r not in sizes or not sizes[r][1]:
            continue
        seen.add(r)
        total += sizes[r][0]
        to_see.extend(sizes[r][2])
    return total


_WHY_REVIEW = {
    "polars": ("written for the Polars engine: their SQL is kept as it is, and where Polars and the warehouse differ "
               "(CAST of a decimal to an integer, ROUND of .5, % of a negative, window frames with ties, the name "
               "of an expression without an alias) the results differ"),
    "clickhouse": "written for the ClickHouse engine and translated by sqlglot",
    "bigquery": "written for the BigQuery engine and translated by sqlglot",
}


def _review_note(model: str, to_review: list) -> list[str]:
    """README: the steps of a model whose text the team must review, and why."""
    per: dict[str, list[str]] = {}
    for step, why in to_review:
        per.setdefault(why, []).append(step)
    return [f"In `{model}`, " + (f"the step `{steps[0]}` is" if len(steps) == 1 else "the steps " + ", ".join(f"`{p}`" for p in steps) + " are")
            + f" {_WHY_REVIEW.get(why, why)}: review {'it' if len(steps) == 1 else 'them'}."
            for why, steps in per.items()]


def _typed_raw(sql: str, cols: list[str], dtypes: dict, dialect: str | None) -> str:
    """The query of a datasource from a query, with the types Tabularia gave it. In
    federated mode it is DuckDB SQL; in native mode it is in the warehouse's dialect and
    is not translated: only the projection wrapping it is written in its dialect."""
    if dialect is None:
        return typed_query(sql, cols, dtypes)
    import sqlglot
    from sqlglot import exp

    tipi = {c: source_type(dtypes.get(c)) for c in cols}
    if not cols or not any(tipi.values()):
        return sql
    # a column of a type the export cannot write passes as it is (only it)
    proj = ", ".join(
        _fix_for_dialect(exp.alias_(sqlglot.parse_one(_typed(dialect, c, tipi[c]), read="duckdb") if tipi[c] else exp.column(c, quoted=True),
                                    c, quoted=True), dialect).sql(dialect=dialect)
        for c in cols
    )
    return f"SELECT {proj} FROM ({sql}) AS _q"
