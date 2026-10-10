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
import tempfile
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings

from app.engine.dbt_export import (
    SEED_MAX_ROWS,
    TEST_IDENT,
    DbtExportError,
    ControlloSintassiClickHouse,
    TargetClickHouse,
    _SQLGLOT_DIALECT,
    usa_proposta,
    build_dbt_project,
    compile_model_sql,
    duck_type,
    singular_test_sql,
    substitute_source_idents,
    tipizza_query,
    transpile_model,
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
        ch = _target_clickhouse(req) if req.mode == "clickhouse" else None
        dialect = "clickhouse" if ch else (_SQLGLOT_DIALECT.get((req.native or {}).get("db_type")) if req.mode == "native" else None)
        seeds = _read_seeds(req.seeds, dialect)
        sources = ch.sources(req.sources) if ch else req.sources
        notes = list(req.notes)
        models, tests = _compile(req, seeds, ch, notes)
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
            zf.writestr(voce, files[path])
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip")


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

    return sqlglot.exp.DataType.build(duck_t, dialect="duckdb").sql(dialect=dialect or "duckdb")


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
                    f"la sorgente «{s.name}» ha {n:,} righe: troppe per un seed dbt (al più {SEED_MAX_ROWS:,}). "
                    "Importala in un database e usa quella come sorgente.",
                    "seed_too_large", source=s.name, rows=f"{n:,}", max=f"{SEED_MAX_ROWS:,}",
                )
            df = lf.collect()
            csv = df.write_csv()
        except DbtExportError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"seed «{s.name}»: impossibile leggere il file ({e})", "seed_unreadable", seed=s.name, detail=str(e)[:200])
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        colonne = s.columns or [{"name": c, "description": ""} for c in df.columns]
        tipi = {c: duck_type(str(t)) for c, t in df.schema.items()}
        out.append({"name": s.name, "csv": csv, "description": s.description, "columns": colonne, "_cols": list(df.columns),
                    "_types": tipi, "column_types": {c: _tipo_nel_dialetto(t, dialect) for c, t in tipi.items() if t}})
    return out


def _target_clickhouse(req: ExportRequest) -> TargetClickHouse:

    if not req.clickhouse:
        raise DbtExportError("modalità clickhouse senza le connessioni delle sorgenti")
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
            raise DbtExportError("modalità native senza configurazione del warehouse")
        dialect = _SQLGLOT_DIALECT.get(req.native.get("db_type"))
        if dialect is None:
            raise DbtExportError(f"nessun dialetto sqlglot per '{req.native.get('db_type')}'")
    elif req.mode == "clickhouse" and ch is None:
        raise DbtExportError("modalità clickhouse senza le connessioni delle sorgenti")

    registro: dict[str, tuple[list[str], dict]] = {}   # modello → (colonne di uscita, tipi)
    seed_cols = {s["name"]: (s["_cols"], s["_types"]) for s in seeds}
    tipi_di = lambda e: {c: duck_type(t) for c, t in (e.get("types") or {}).items()}
    ident_to_macro: dict[str, str] = {e["ident"]: e["macro"] for e in req.source_map.values()} if native else {}
    schema: dict[str, list[str]] = {e["ident"]: e.get("columns") or [] for e in req.source_map.values()} if native else {}
    ident_di: dict[str, str] = {}                  # modello/seed → identificatore (nativo)

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
                raise DbtExportError(f"modello referenziato prima di essere compilato: {nome}")
            cols, tipi = registro[nome]
            return (ident_per(nome, cols) if native else "{{ ref('%s') }}" % nome), cols, tipi, nome
        if src.get("seed") is not None:
            nome = src["seed"]
            if nome not in seed_cols:
                raise DbtExportError(f"seed non letto: {nome}")
            cols, tipi = seed_cols[nome]
            return (ident_per(nome, cols) if native else "{{ ref('%s') }}" % nome), cols, tipi, nome
        e = req.source_map.get(src.get("key"))
        if e is None:
            raise DbtExportError(f"sorgente non mappata: {src.get('key')}")
        return (e["ident"] if native else e["ref"]), e.get("columns") or [], tipi_di(e), e.get("label")

    models, tests = [], []
    tradotti: dict[str, str] = {}   # modello → dialetto da cui l'AI l'ha tradotto
    # il SQL ClickHouse si fa leggere a un parser ClickHouse vero (sqlglot sbaglia in silenzio)
    controllo = ControlloSintassiClickHouse() if dialect == "clickhouse" else None
    for m in req.models:
        if m.kind == "raw":
            cols = [c["name"] for c in m.columns if c.get("name")]
            dtypes = {c["name"]: c.get("dtype") for c in m.columns if c.get("name")}
            tipi = {c: duck_type(dtypes.get(c)) for c in cols}
            sql = (m.raw_sql or "").strip()
            if ch and m.translate:
                # target ClickHouse: la query, scritta per l'origine, gira in ClickHouse sui database federati
                origine = ch.conns.get(int(m.translate.get("connection_id")), {}).get("db_type", "")
                proposta = req.overrides.get(m.name)
                originale = sql
                try:
                    sql = ch.traduci_query(sql, m.translate.get("connection_id"), m.translate.get("label") or m.name,
                                           proposta=proposta, colonne=cols if proposta is not None else None)
                    errore = controllo.errore(sql) if controllo else None
                    if errore and proposta is not None:
                        raise DbtExportError(f"la proposta dell'AI per «{m.name}» non passa i controlli: ClickHouse non la legge ({errore})",
                                             "ai_translation_invalid", model=m.name, reason="syntax", detail=errore)
                    if errore:
                        etichetta = m.translate.get("label") or m.name
                        raise DbtExportError(f"la query della datasource «{etichetta}» tradotta non si legge in ClickHouse ({errore})",
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
            sql = _tipizza_raw(sql, cols, dtypes, dialect)
        else:
            if not m.source:
                raise DbtExportError(f"il modello «{m.name}» non ha una sorgente")
            raw, cols, tipi = compile_model_sql(m.source, m.operations, resolve, native=native, dialetto=dialect)
            if native and m.name in req.overrides:
                sql = usa_proposta(req.overrides[m.name], dialect, ident_to_macro, cols, m.name, controllo)
                tradotti[m.name] = "duckdb"
            elif native:
                try:
                    tradotto = transpile_model(raw, dialect, schema)
                    errore = controllo.errore(tradotto) if controllo else None   # prima delle macro: niente Jinja
                    if errore:
                        raise DbtExportError(f"traduzione nel dialetto '{dialect}' non riuscita: ClickHouse non la legge ({errore})",
                                             "translation_failed", dialect=dialect, detail=errore)
                    sql = substitute_source_idents(tradotto, ident_to_macro)
                except DbtExportError as e:
                    if e.code == "translation_failed":
                        e.params.update(model=m.name, sql=raw, source_dialect="duckdb", target_dialect=dialect, columns=cols)
                    raise
            else:
                sql = raw
        registro[m.name] = (cols, tipi)
        models.append({"name": m.name, "kind": m.kind, "sql": sql, "materialized": m.materialized, "description": m.description,
                       "columns": m.columns, "schema": m.schema_, "alias": m.alias, "tags": m.tags,
                       "meta": {**m.meta, "ai_translated": True} if m.name in tradotti else m.meta,
                       "ai_translated": m.name in tradotti, "ai_translated_from": tradotti.get(m.name)})
        for t in m.tests:
            tsql = singular_test_sql(t)
            if native:
                tsql = substitute_source_idents(transpile_model(tsql, dialect, {TEST_IDENT: cols}), {TEST_IDENT: "{{ ref('%s') }}" % m.name})
            else:
                tsql = substitute_source_idents(tsql, {TEST_IDENT: "{{ ref('%s') }}" % m.name})
            tests.append({"name": f"{m.name}__{t.get('kind')}_{t.get('id') or len(tests) + 1}", "sql": tsql,
                          "severity": t.get("severity", "error"), "description": f"Contract rule {t.get('kind')} on {m.name}"})
    return models, tests


def _tipizza_raw(sql: str, cols: list[str], dtypes: dict, dialect: str | None) -> str:
    """La query di una datasource da query, con i tipi che Tabularia le ha dato.
    In federato è SQL DuckDB; in nativo è nel dialetto del warehouse e non si
    traduce: si scrive nel suo dialetto solo la proiezione che la avvolge."""
    if dialect is None:
        return tipizza_query(sql, cols, dtypes)
    import sqlglot
    from sqlglot import exp

    tipi = {c: duck_type(dtypes.get(c)) for c in cols}
    if not cols or not all(tipi.values()):
        return sql
    proj = ", ".join(
        exp.Cast(this=exp.column(c, quoted=True), to=exp.DataType.build(tipi[c], dialect="duckdb")).as_(c, quoted=True).sql(dialect=dialect)
        for c in cols
    )
    return f"SELECT {proj} FROM ({sql}) AS _q"
