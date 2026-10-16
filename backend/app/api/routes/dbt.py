"""Export dbt: compila i modelli risolti (dal gateway) in un progetto dbt e lo
restituisce come zip. Due modalità:
· **duckdb**: dbt-duckdb federa i DB di origine (ATTACH); SQL DuckDB as-is.
· **native**: dbt gira nel warehouse nativo; il SQL è tradotto nel suo
  dialetto via sqlglot (con espansione degli star sullo schema delle sorgenti).
Il gateway ha già risolto flusso→modelli (uscite, passi condivisi, query,
flussi a monte) e sorgenti→tabelle DB / seed; qui si compila in ORDINE, con un
registro delle colonne di ogni modello per chi lo referenzia, si leggono i
parquet dei seed e si assembla il progetto."""
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
    SEED_TESTO,
    TEST_IDENT,
    DbtExportError,
    ControlloSintassiClickHouse,
    TargetClickHouse,
    _SQLGLOT_DIALECT,
    adapter_nativo,
    usa_proposta,
    build_dbt_project,
    codifica_seed,
    compile_model_sql,
    duck_type,
    neutralize_jinja,
    nome_in_postgres,
    nome_quotabile,
    nomi_delle_cte,
    in_byte,
    oltre_il_limite,
    troppo_grande,
    ref_label,
    rimetti_testi,
    singular_test_sql,
    sorgenti_del_pacchetto,
    test_di_colonna,
    substitute_source_idents,
    tipizza_query,
    tipo_sorgente,
    transpile_model,
    _fix_for_dialect,
    _slug,
    _tempi_clickhouse,
    _tipizza,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dbt", tags=["dbt"])


class ModelSpec(BaseModel):
    name: str
    kind: str = "output"              # output | intermediate | raw
    source: dict | None = None        # {key} | {model} | {seed}
    operations: list = []
    materialized: str = "table"
    raw_sql: str | None = None        # kind=raw: il SQL com'è (query di una datasource)
    description: str = ""
    columns: list = []                # [{name, description, tests:[{kind, severity, values?}]}]
    tests: list = []                  # [{id, kind, severity, column?, min?, max?, regex?, sql?}]
    schema_: str | None = None
    alias: str | None = None
    meta: dict = {}                   # da dove viene: flusso, versione, uscita, autore, export
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
    schema_: str | None = Field(None, alias="schema", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")   # duckdb: schema dei modelli
    exported_by: str | None = None  # chi esporta: solo nel README
    ai: dict | None = None          # dialogo: cosa ha scritto l'AI {model, summaries, translated}
    overrides: dict[str, str] = {}  # modello → traduzione proposta dall'AI (verificata qui prima dell'uso)

    model_config = {"populate_by_name": True}
    sources: list = []            # [{name, database, schema, tables}]
    seeds: list[SeedSpec] = []
    notes: list[str] = []


@router.post("/export")
def export_dbt(req: ExportRequest) -> StreamingResponse:
    try:
        if req.mode == "native":
            adapter_nativo((req.native or {}).get("db_type"))   # MySQL/MariaDB: no native target (decision of 2026-10-16)
        ch = _target_clickhouse(req) if req.mode == "clickhouse" else None
        dialect = "clickhouse" if ch else (_SQLGLOT_DIALECT.get((req.native or {}).get("db_type")) if req.mode == "native" else None)
        seeds = _read_seeds(req.seeds, dialect)
        sources = sorgenti_del_pacchetto(req.sources, req.layout)   # «already declared»: the folder package only
        sources = ch.sources(sources) if ch else sources
        _nomi_quotabili(req, sources)
        notes = list(req.notes) + [_nota_seed(s) for s in seeds if s["cambiate"]]
        models, tests = _compile(req, seeds, ch, notes)
        if ch:
            notes.extend(_nota_timestamptz(req, ch))
        clickhouse = None
        if ch:
            cfg = get_settings().clickhouse_external
            clickhouse = {"host": cfg.host, "port": cfg.port, "secure": cfg.secure, "target_schema": (req.clickhouse or {}).get("target_schema"),
                          "federati": list(ch.federati.values())}
        files = build_dbt_project(
            req.flow_name, models, req.attachments if req.mode == "duckdb" else None, sources,
            native=req.native if req.mode == "native" else None, seeds=seeds, tests=tests,
            notes=notes, flow_description=req.flow_description, clickhouse=clickhouse,
            layout=req.layout, schema=req.schema_ if req.mode == "duckdb" else None, esportato_da=req.exported_by,
            ai=_ai_del_progetto(req, models),
        )
    except DbtExportError as e:
        # al gateway: il codice e i parametri (lo dice nella lingua di chi lo legge), il testo per i log
        raise HTTPException(status_code=422, detail={"code": e.code, "params": e.params, "message": str(e)})

    # DETERMINISTICO: stesso flusso, stessa versione, stesse opzioni → stesso zip, byte per
    # byte (file in ordine, data fissa: altrimenti ogni voce porta l'ora dell'export)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            voce = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            voce.compress_type = zipfile.ZIP_DEFLATED
            voce.external_attr = 0o644 << 16
            # a lone surrogate (half an emoji from a browser) cannot be written in UTF-8: U+FFFD, not a 500
            zf.writestr(voce, re.sub("[\ud800-\udfff]", "�", files[path]) if isinstance(files[path], str) else files[path])
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip")


def _nomi_quotabili(req: "ExportRequest", sources: list[dict]) -> None:
    """Every name the project's adapter must write — a source's schema and tables (the team's
    «already declared» ones are theirs), a model's table and schema, the project's schema — is
    one it can write (`name_not_quotable`, as the gateway says before the download)."""
    if req.mode == "native":
        adapter, schema = "dbt-" + adapter_nativo((req.native or {}).get("db_type")), (req.native or {}).get("target_schema")
    elif req.mode == "clickhouse":
        adapter, schema = "dbt-clickhouse", (req.clickhouse or {}).get("target_schema")
    else:
        adapter, schema = "dbt-duckdb", req.schema_
    nome_quotabile(schema, adapter, "schema")
    for s in sources:
        if s.get("declared"):
            continue
        nome_quotabile(s.get("schema"), adapter, "source")
        for t in s.get("tables") or []:
            nome_quotabile(t["name"] if isinstance(t, dict) else t, adapter, "source")
    for m in req.models:
        nome_quotabile(m.schema_, adapter, "schema")
        nome_quotabile(m.alias, adapter, "relation")


def _ai_del_progetto(req: "ExportRequest", models: list[dict]) -> dict | None:
    """Per il README: cosa ha scritto l'AI (riassunti dal gateway, modelli tradotti
    dall'engine) e se ci sono descrizioni marcate."""
    if not req.ai and not any(m.get("ai_translated") for m in models):
        return None
    ai = dict(req.ai or {})
    ai["translated"] = [m["name"] for m in models if m.get("ai_translated")]
    ai["described"] = any(str(c.get("description") or "").startswith("(AI) ") for m in models for c in (m.get("columns") or []))
    return ai


def _tipo_nel_dialetto(duck_t: str, dialect: str | None) -> str:
    """Un tipo DuckDB scritto come lo vuole il warehouse (per `column_types` dei seed)."""
    import sqlglot

    tipo = sqlglot.exp.DataType.build(duck_t, dialect="duckdb")
    if dialect == "postgres" and tipo.this == sqlglot.exp.DataType.Type.UBIGINT:
        tipo = sqlglot.exp.DataType.build("DECIMAL(20, 0)")     # Postgres has no unsigned integer (UInt64)
    if dialect == "clickhouse":
        tipo = _tempi_clickhouse(tipo)                          # Date32, DateTime64(6), a time as text
    return tipo.sql(dialect=dialect or "duckdb")


def _read_seeds(seeds: list[SeedSpec], dialect: str | None = None) -> list[dict]:
    """I parquet dei seed → CSV (con un tetto: un seed è un file nel repository).
    Ogni seed porta i TIPI delle sue colonne: dbt li dichiara (`column_types`)
    invece di indovinarli dal CSV — un codice «007» resta testo, non diventa 7."""
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
            df, cambiate = codifica_seed(lf.collect())
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
        colonne = s.columns or [{"name": c, "description": ""} for c in df.columns]
        tipi = {c: duck_type(str(t)) for c, t in df.schema.items()}
        out.append({"name": s.name, "csv": csv, "description": s.description, "columns": colonne, "_cols": list(df.columns),
                    "_types": {c: SEED_TESTO if c in cambiate else t for c, t in tipi.items()}, "cambiate": cambiate,
                    "column_types": {c: _tipo_nel_dialetto(t, dialect) for c, t in tipi.items() if t}})
    return out


def _nota_seed(seed: dict) -> str:
    """README: the seed whose texts are written as `~…`, and why."""
    colonne = ", ".join(f"`{c}`" for c in seed["cambiate"])
    return (f"In the seed `{seed['name']}`, the texts of {colonne} that dbt would load changed (an empty text, `null`, a blank "
            "text or `\\N` as NULL, the spaces at the ends cut, `\\r` as a newline) are written `~` + the text in the CSV, with `~`, "
            "space, tab and `\\r` written `~0`, `~1`, `~2`, `~3`. The first step of every model that reads the seed gives them "
            "back as Tabularia reads them: read the seed through those models.")


def _target_clickhouse(req: ExportRequest) -> TargetClickHouse:

    if not req.clickhouse:
        raise DbtExportError("clickhouse mode without the source connections")
    return TargetClickHouse(req.clickhouse.get("connections") or [], get_settings().clickhouse_external.host)


def _compile(req: ExportRequest, seeds: list[dict], ch: TargetClickHouse | None = None,
             notes: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """I modelli in ordine, con un registro delle colonne di ciascuno; i test
    singolari dai contratti. In nativo (e su ClickHouse) ogni riferimento è un
    identificatore segnaposto finché sqlglot non ha tradotto."""
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

    registro: dict[str, tuple[list[str], dict]] = {}   # modello → (colonne di uscita, tipi)
    seed_cols = {s["name"]: (s["_cols"], s["_types"]) for s in seeds}
    # a database table read live: a timestamptz stays one until the first step (see tipo_sorgente)
    tipi_di = lambda e: {c: tipo_sorgente(t) for c, t in (e.get("types") or {}).items()}
    ident_to_macro: dict[str, str] = {e["ident"]: e["macro"] for e in req.source_map.values()} if native else {}
    schema: dict[str, list[str]] = {e["ident"]: e.get("columns") or [] for e in req.source_map.values()} if native else {}
    ident_di: dict[str, str] = {}                  # modello/seed → identificatore (nativo)
    # Federated: the SQL is not translated, so the macros would sit in it next to the
    # user's text, and the Jinja neutralisation must not touch OURS. Each macro is a
    # placeholder until the text is neutralised; the marker is random (nobody can write
    # it in a flow) and never reaches the zip, which stays the same byte for byte.
    marcatore = f"__tabularia_{secrets.token_hex(8)}_"
    segnaposti: dict[str, str] = {}                # macro → segnaposto (federato)

    def segnaposto(macro: str) -> str:
        if macro not in segnaposti:
            segnaposti[macro] = f"{marcatore}{len(segnaposti)}__"
        return segnaposti[macro]

    def ident_per(nome: str, cols: list[str]) -> str:
        if nome not in ident_di:
            ident_di[nome] = f"__m_{len(ident_di)}"
            ident_to_macro[ident_di[nome]] = "{{ ref('%s') }}" % nome
            schema[ident_di[nome]] = cols
        return ident_di[nome]

    def resolve(src: dict):
        if src.get("model") is not None:
            nome = src["model"]
            if nome not in registro:
                raise DbtExportError(f"model referenced before it was compiled: {nome}")
            cols, tipi = registro[nome]
            return (ident_per(nome, cols) if native else segnaposto("{{ ref('%s') }}" % nome)), cols, tipi, nome
        if src.get("seed") is not None:
            nome = src["seed"]
            if nome not in seed_cols:
                raise DbtExportError(f"seed not read: {nome}")
            cols, tipi = seed_cols[nome]
            return (ident_per(nome, cols) if native else segnaposto("{{ ref('%s') }}" % nome)), cols, tipi, nome
        e = req.source_map.get(src.get("key"))
        if e is None:
            raise DbtExportError(f"unmapped source: {src.get('key')}")
        if native:
            return e["ident"], e.get("columns") or [], tipi_di(e), e.get("label")
        return segnaposto(e["ref"]), e.get("columns") or [], tipi_di(e), e.get("label") or ref_label(e["ref"])

    def con_le_macro(sql: str) -> str:
        """Federated: the user's text neutralised, then the exporter's macros in place."""
        sql = neutralize_jinja(sql)
        for macro, s in segnaposti.items():
            sql = sql.replace(s, macro)
        return sql

    models, tests = [], []
    usati: set[str] = set()         # the CTE names of the whole project: no model repeats one (#62)
    file_dei_test: set[str] = set()   # a singular test is the file tests/<slug of its name>.sql: one name, one file

    def nome_del_test(nome: str) -> str:
        """The name itself, or with `_2`, `_3`… when another test has its file (two columns
        `Codice` and `codice` give the same slug)."""
        scelto, n = nome, 2
        while _slug(scelto) in file_dei_test:
            scelto, n = f"{nome}_{n}", n + 1
        file_dei_test.add(_slug(scelto))
        return scelto
    dimensioni: dict[str, tuple[int, bool, set]] = {}   # ClickHouse: model → (bytes, ephemeral?, models it refs)
    tradotti: dict[str, str] = {}   # modello → dialetto da cui l'AI l'ha tradotto
    # il SQL ClickHouse si fa leggere a un parser ClickHouse vero (sqlglot sbaglia in silenzio)
    controllo = ControlloSintassiClickHouse() if dialect == "clickhouse" else None
    try:
        for m in req.models:
            info: dict = {}     # what compile_model_sql tells besides the SQL
            if m.kind == "raw":
                cols = [c["name"] for c in m.columns if c.get("name")]
                dtypes = {c["name"]: c.get("dtype") for c in m.columns if c.get("name")}
                tipi = {c: duck_type(dtypes.get(c)) for c in cols}   # after _tipizza_raw: a timestamptz is a naive UTC TIMESTAMP
                sql = (m.raw_sql or "").strip()
                if ch and m.translate:
                    # target ClickHouse: la query, scritta per l'origine, gira in ClickHouse sui database federati
                    origine = ch.conns.get(int(m.translate.get("connection_id")), {}).get("db_type", "")
                    proposta = req.overrides.get(m.name)
                    originale = sql
                    try:
                        sql = ch.traduci_query(sql, m.translate.get("connection_id"), m.translate.get("label") or m.name,
                                               proposta=proposta, colonne=cols if proposta is not None else None)
                        if oltre_il_limite(in_byte(sql)):
                            raise troppo_grande(m.name, in_byte(sql))
                        errore = controllo.errore(sql) if controllo else None
                        if errore and proposta is not None:
                            raise DbtExportError(f"the AI's proposal for «{m.name}» does not pass the checks: ClickHouse cannot parse it ({errore})",
                                                 "ai_translation_invalid", model=m.name, reason="syntax", detail=errore)
                        if errore:
                            etichetta = m.translate.get("label") or m.name
                            raise DbtExportError(f"the translated query of the datasource «{etichetta}» cannot be parsed by ClickHouse ({errore})",
                                                 "query_not_translatable", datasource=etichetta, detail=errore)
                    except DbtExportError as e:
                        if e.code in ("query_not_translatable", "query_not_single_select") and proposta is None:
                            # al gateway quello che serve per chiedere una proposta all'AI (se l'ha scelto)
                            e.params.update(model=m.name, sql=originale, source_dialect=_SQLGLOT_DIALECT.get(origine, origine),
                                            target_dialect="clickhouse", columns=cols)
                        raise
                    if proposta is not None:
                        tradotti[m.name] = _SQLGLOT_DIALECT.get(origine, origine)
                    if origine != "clickhouse" and notes is not None:
                        notes.append(f"The model `{m.name}` is the SQL query of a datasource, translated from {origine} to ClickHouse "
                                     "by sqlglot, with its tables read from the `tab_src_*` databases: review it.")
                usati.update(nomi_delle_cte(sql, "clickhouse" if (ch and m.translate) else (dialect or "duckdb")))
                # the query is the user's text: no macro of ours in it, all of it neutralised
                sql = neutralize_jinja(_tipizza_raw(sql, cols, dtypes, dialect))
            else:
                if not m.source:
                    raise DbtExportError(f"the model «{m.name}» has no source")
                raw, cols, tipi = compile_model_sql(m.source, m.operations, resolve, native=native, dialetto=dialect, info=info,
                                                    testo=m.dialect, usati=usati)
                da_rivedere = info.get("da_rivedere") or []
                if da_rivedere and notes is not None:
                    notes.extend(_nota_da_rivedere(m.name, da_rivedere))
                if native and m.name in req.overrides:
                    # the proposal may read only what THIS model reads, not every source of the project
                    del_modello = {i: mac for i, mac in ident_to_macro.items() if re.search(r"(?<!\w)" + re.escape(i) + r"(?!\w)", raw)}
                    sql = usa_proposta(req.overrides[m.name], dialect, del_modello, cols, m.name, controllo)
                    usati.update(nomi_delle_cte(req.overrides[m.name], dialect))
                    tradotti[m.name] = "duckdb"
                elif native:
                    try:
                        # the texts written in the warehouse's dialect go back in after sqlglot
                        tradotto = rimetti_testi(transpile_model(raw, dialect, schema), info)
                        if controllo and oltre_il_limite(in_byte(tradotto)):
                            raise troppo_grande(m.name, in_byte(tradotto))   # not «not translatable»: no AI asked for it
                        errore = controllo.errore(tradotto) if controllo else None   # prima delle macro: niente Jinja
                        if errore:
                            raise DbtExportError(f"translation to the '{dialect}' dialect failed: ClickHouse cannot parse it ({errore})",
                                                 "translation_failed", dialect=dialect, detail=errore)
                        sql = substitute_source_idents(neutralize_jinja(tradotto), ident_to_macro)
                    except DbtExportError as e:
                        if e.code == "translation_failed":
                            # to whoever translates it (the AI): the model in DuckDB, without our placeholders
                            e.params.update(model=m.name, sql=rimetti_testi(raw, {"segnaposti": info.get("per_sonda")}), source_dialect="duckdb",
                                            target_dialect=dialect, columns=cols)
                        raise
                else:
                    sql = con_le_macro(raw)
            registro[m.name] = (cols, tipi)
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
                dimensioni[m.name] = (in_byte(sql), m.materialized == "ephemeral", set(re.findall(r"\{\{ ref\('([^']*)'\) \}\}", sql)))
                totale = _con_gli_effimeri(m.name, dimensioni)
                if m.materialized != "ephemeral" and oltre_il_limite(totale):
                    raise troppo_grande(m.name, totale)
            if dialect == "postgres" and notes is not None:
                notes.extend(f"In `{m.name}`, the column `{c}` is `{nome_in_postgres(c)}` in Postgres, which keeps only the first 63 "
                             "bytes of a name." for c in cols if nome_in_postgres(c) != c)
            # the column rules dbt's generic tests do not run as Tabularia does become singular tests
            colonne, di_colonna = test_di_colonna(m.columns)
            models.append({"name": m.name, "kind": m.kind, "sql": sql, "materialized": m.materialized, "description": m.description,
                           "da_rivedere": info.get("da_rivedere"),
                           "columns": colonne, "schema": m.schema_, "alias": m.alias, "tags": m.tags,
                           "meta": {**m.meta, "ai_translated": True} if m.name in tradotti else m.meta,
                           "ai_translated": m.name in tradotti, "ai_translated_from": tradotti.get(m.name)})
            for t in [*m.tests, *di_colonna]:
                tsql = singular_test_sql(t, tipi, dialect)
                if native:
                    tsql = transpile_model(tsql, dialect, {TEST_IDENT: cols})
                # the rule's text (an expression, a regex, a bound) neutralised, then the ref() to the model
                tsql = substitute_source_idents(neutralize_jinja(tsql), {TEST_IDENT: "{{ ref('%s') }}" % m.name})
                if t in di_colonna:
                    nome, descrizione = f"{m.name}__{t['kind']}_{t.get('id') or t['column']}", \
                        f"Contract rule {t['kind']} on {m.name}, column «{t['column']}»"
                else:
                    nome, descrizione = f"{m.name}__{t.get('kind')}_{t.get('id') or len(tests) + 1}", f"Contract rule {t.get('kind')} on {m.name}"
                tests.append({"name": nome_del_test(nome), "sql": tsql, "severity": t.get("severity", "error"), "description": descrizione})
    finally:
        if controllo:
            controllo.chiudi()
    if controllo and controllo.saltato and notes is not None:
        notes.append("The ClickHouse SQL of this project was not read by a ClickHouse parser at export time (none answered): "
                     "an error Tabularia would have caught shows at the first `dbt run`.")
    return models, tests


def _nota_timestamptz(req: ExportRequest, ch: TargetClickHouse) -> list[str]:
    """README (ClickHouse target): ClickHouse's PostgreSQL engine reads a timestamptz as the
    wall-clock time of its Postgres session and drops the offset — on a server in Europe/Rome
    10:30 UTC came out 12:30. Tabularia reads the instant in UTC; a session in UTC gives the same."""
    fuso = lambda t: tipo_sorgente(t) == "TIMESTAMPTZ"   # noqa: E731
    pg = {int(i): c for i, c in ch.conns.items() if c.get("db_type") == "postgresql"}
    utenti: set[str] = set()
    if any(fuso(t) for e in req.source_map.values() for t in (e.get("types") or {}).values()):
        utenti |= {str(c.get("username") or "") for c in pg.values()}
    for m in req.models:
        if m.kind == "raw" and m.translate and any(fuso(c.get("dtype")) for c in m.columns):
            c = pg.get(int(m.translate.get("connection_id") or 0))
            if c:
                utenti.add(str(c.get("username") or ""))
    utenti.discard("")
    if not utenti:
        return []
    ruolo = lambda u: u if re.fullmatch(r"[a-z_][a-z0-9_]*", u) else '"' + u.replace('"', '""') + '"'   # noqa: E731
    return ["ClickHouse reads a Postgres `timestamptz` as the wall-clock time of its Postgres session and drops the offset: unless "
            "the Postgres server works in UTC, give the user ClickHouse connects with the UTC time zone ("
            + ", ".join(f"`ALTER ROLE {ruolo(u)} SET timezone = 'UTC'`" for u in sorted(utenti))
            + "), or every instant shifts by the zone's offset. Tabularia reads them in UTC."]


def _con_gli_effimeri(nome: str, dimensioni: dict[str, tuple[int, bool, set]]) -> int:
    """The bytes of a model with every ephemeral model dbt inlines into it (those it reads,
    and theirs, once each)."""
    visti, da_vedere, totale = {nome}, list(dimensioni[nome][2]), dimensioni[nome][0]
    while da_vedere:
        r = da_vedere.pop()
        if r in visti or r not in dimensioni or not dimensioni[r][1]:
            continue
        visti.add(r)
        totale += dimensioni[r][0]
        da_vedere.extend(dimensioni[r][2])
    return totale


_PERCHE_RIVEDERE = {
    "polars": ("written for the Polars engine: their SQL is kept as it is, and where Polars and the warehouse differ "
               "(CAST of a decimal to an integer, ROUND of .5, % of a negative, window frames with ties, the name "
               "of an expression without an alias) the results differ"),
    "clickhouse": "written for the ClickHouse engine and translated by sqlglot",
    "bigquery": "written for the BigQuery engine and translated by sqlglot",
}


def _nota_da_rivedere(modello: str, da_rivedere: list) -> list[str]:
    """README: the steps of a model whose text the team must review, and why."""
    per: dict[str, list[str]] = {}
    for passo, perche in da_rivedere:
        per.setdefault(perche, []).append(passo)
    return [f"In `{modello}`, " + (f"the step `{passi[0]}` is" if len(passi) == 1 else "the steps " + ", ".join(f"`{p}`" for p in passi) + " are")
            + f" {_PERCHE_RIVEDERE.get(perche, perche)}: review {'it' if len(passi) == 1 else 'them'}."
            for perche, passi in per.items()]


def _tipizza_raw(sql: str, cols: list[str], dtypes: dict, dialect: str | None) -> str:
    """La query di una datasource da query, con i tipi che Tabularia le ha dato.
    In federato è SQL DuckDB; in nativo è nel dialetto del warehouse e non si
    traduce: si scrive nel suo dialetto solo la proiezione che la avvolge."""
    if dialect is None:
        return tipizza_query(sql, cols, dtypes)
    import sqlglot
    from sqlglot import exp

    tipi = {c: tipo_sorgente(dtypes.get(c)) for c in cols}
    if not cols or not any(tipi.values()):
        return sql
    # a column of a type the export cannot write passes as it is (only it)
    proj = ", ".join(
        _fix_for_dialect(exp.alias_(sqlglot.parse_one(_tipizza(dialect, c, tipi[c]), read="duckdb") if tipi[c] else exp.column(c, quoted=True),
                                    c, quoted=True), dialect).sql(dialect=dialect)
        for c in cols
    )
    return f"SELECT {proj} FROM ({sql}) AS _q"
