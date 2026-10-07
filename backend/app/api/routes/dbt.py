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
from pydantic import BaseModel

from app.engine.dbt_export import (
    SEED_MAX_ROWS,
    TEST_IDENT,
    DbtExportError,
    _SQLGLOT_DIALECT,
    build_dbt_project,
    compile_model_sql,
    singular_test_sql,
    substitute_source_idents,
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
    mode: str = "duckdb"          # duckdb | native
    attachments: list = []        # duckdb: [{alias, db_type, conn, pw_env}]
    native: dict | None = None    # native: {db_type, conn, pw_env, target_schema}
    sources: list = []            # [{name, database, schema, tables}]
    seeds: list[SeedSpec] = []
    notes: list[str] = []


@router.post("/export")
def export_dbt(req: ExportRequest) -> StreamingResponse:
    try:
        seeds = _read_seeds(req.seeds)
        models, tests = _compile(req, seeds)
        files = build_dbt_project(
            req.flow_name, models, req.attachments if req.mode != "native" else None, req.sources,
            native=req.native if req.mode == "native" else None, seeds=seeds, tests=tests,
            notes=req.notes, flow_description=req.flow_description,
        )
    except DbtExportError as e:
        raise HTTPException(status_code=422, detail=str(e))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip")


def _read_seeds(seeds: list[SeedSpec]) -> list[dict]:
    """I parquet dei seed → CSV (con un tetto: un seed è un file nel repository)."""
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
                    "Importala in un database e usa quella come sorgente."
                )
            df = lf.collect()
            csv = df.write_csv()
        except DbtExportError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DbtExportError(f"seed «{s.name}»: impossibile leggere il file ({e})")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        colonne = s.columns or [{"name": c, "description": ""} for c in df.columns]
        out.append({"name": s.name, "csv": csv, "description": s.description, "columns": colonne, "_cols": list(df.columns)})
    return out


def _compile(req: ExportRequest, seeds: list[dict]) -> tuple[list[dict], list[dict]]:
    """I modelli in ordine, con un registro delle colonne di ciascuno; i test
    singolari dai contratti. In nativo ogni riferimento è un identificatore
    segnaposto finché sqlglot non ha tradotto."""
    native = req.mode == "native"
    dialect = None
    if native:
        if not req.native:
            raise DbtExportError("modalità native senza configurazione del warehouse")
        dialect = _SQLGLOT_DIALECT.get(req.native.get("db_type"))
        if dialect is None:
            raise DbtExportError(f"nessun dialetto sqlglot per '{req.native.get('db_type')}'")

    registro: dict[str, list[str]] = {}            # modello → colonne di uscita
    seed_cols = {s["name"]: s["_cols"] for s in seeds}
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
            cols = registro[nome]
            return (ident_per(nome, cols) if native else "{{ ref('%s') }}" % nome), cols
        if src.get("seed") is not None:
            nome = src["seed"]
            if nome not in seed_cols:
                raise DbtExportError(f"seed non letto: {nome}")
            cols = seed_cols[nome]
            return (ident_per(nome, cols) if native else "{{ ref('%s') }}" % nome), cols
        e = req.source_map.get(src.get("key"))
        if e is None:
            raise DbtExportError(f"sorgente non mappata: {src.get('key')}")
        return (e["ident"] if native else e["ref"]), e.get("columns") or []

    models, tests = [], []
    for m in req.models:
        if m.kind == "raw":
            sql = (m.raw_sql or "").strip()
            cols = [c["name"] for c in m.columns if c.get("name")]
        else:
            if not m.source:
                raise DbtExportError(f"il modello «{m.name}» non ha una sorgente")
            raw, cols = compile_model_sql(m.source, m.operations, resolve, native=native)
            sql = substitute_source_idents(transpile_model(raw, dialect, schema), ident_to_macro) if native else raw
        registro[m.name] = cols
        models.append({"name": m.name, "sql": sql, "materialized": m.materialized, "description": m.description,
                       "columns": m.columns, "schema": m.schema_, "alias": m.alias})
        for t in m.tests:
            tsql = singular_test_sql(t)
            if native:
                tsql = substitute_source_idents(transpile_model(tsql, dialect, {TEST_IDENT: cols}), {TEST_IDENT: "{{ ref('%s') }}" % m.name})
            else:
                tsql = substitute_source_idents(tsql, {TEST_IDENT: "{{ ref('%s') }}" % m.name})
            tests.append({"name": f"{m.name}__{t.get('kind')}_{t.get('id') or len(tests) + 1}", "sql": tsql,
                          "severity": t.get("severity", "error"), "description": f"Contract rule {t.get('kind')} on {m.name}"})
    return models, tests
