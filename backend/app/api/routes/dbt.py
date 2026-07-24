"""Export dbt: compila i modelli risolti (dal gateway) in un progetto dbt e lo
restituisce come zip. Due modalità:
· **duckdb** (v1): dbt-duckdb federa i DB di origine (ATTACH); SQL DuckDB as-is.
· **native** (v2): dbt gira nel warehouse nativo; il SQL è tradotto nel suo
  dialetto via sqlglot (con espansione degli star sullo schema delle sorgenti).
Il gateway ha già risolto flusso→operazioni e sorgenti→tabelle DB."""
from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.engine.dbt_export import (
    DbtExportError,
    _SQLGLOT_DIALECT,
    build_dbt_project,
    compile_model_sql,
    substitute_source_idents,
    transpile_model,
)

router = APIRouter(prefix="/dbt", tags=["dbt"])


class ModelSpec(BaseModel):
    name: str
    source: dict
    operations: list = []
    materialized: str = "table"


class ExportRequest(BaseModel):
    flow_name: str
    models: list[ModelSpec]
    # duckdb: entries {ref, columns}; native: entries {ident, macro, columns}
    source_map: dict
    mode: str = "duckdb"          # duckdb | native
    attachments: list = []        # duckdb: [{alias, db_type, conn, pw_env}]
    native: dict | None = None    # native: {db_type, conn, pw_env, target_schema}
    sources: list = []            # [{name, database, schema, tables}]


@router.post("/export")
def export_dbt(req: ExportRequest) -> StreamingResponse:
    try:
        if req.mode == "native":
            models = _native_models(req)
            files = build_dbt_project(req.flow_name, models, None, req.sources, native=req.native)
        else:
            models = _duckdb_models(req)
            files = build_dbt_project(req.flow_name, models, req.attachments, req.sources)
    except DbtExportError as e:
        raise HTTPException(status_code=422, detail=str(e))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip")


def _duckdb_models(req: ExportRequest) -> list[dict]:
    def resolve(src: dict):
        e = req.source_map.get(src.get("key"))
        if e is None:
            raise DbtExportError(f"sorgente non mappata: {src.get('key')}")
        return e["ref"], e.get("columns") or []

    return [
        {"name": m.name, "sql": compile_model_sql(m.source, m.operations, resolve)[0],
         "materialized": m.materialized}
        for m in req.models
    ]


def _native_models(req: ExportRequest) -> list[dict]:
    if not req.native:
        raise DbtExportError("modalità native senza configurazione del warehouse")
    dialect = _SQLGLOT_DIALECT.get(req.native.get("db_type"))
    if dialect is None:
        raise DbtExportError(f"nessun dialetto sqlglot per '{req.native.get('db_type')}'")

    def resolve(src: dict):
        e = req.source_map.get(src.get("key"))
        if e is None:
            raise DbtExportError(f"sorgente non mappata: {src.get('key')}")
        return e["ident"], e.get("columns") or []

    schema = {e["ident"]: e.get("columns") or [] for e in req.source_map.values()}
    ident_to_macro = {e["ident"]: e["macro"] for e in req.source_map.values()}

    models = []
    for m in req.models:
        raw = compile_model_sql(m.source, m.operations, resolve)[0]
        sql = substitute_source_idents(transpile_model(raw, dialect, schema), ident_to_macro)
        models.append({"name": m.name, "sql": sql, "materialized": m.materialized})
    return models
