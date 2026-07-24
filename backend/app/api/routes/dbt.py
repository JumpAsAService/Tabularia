"""Export dbt: compila i modelli risolti (dal gateway) in un progetto
dbt-duckdb e lo restituisce come zip. Il gateway ha già risolto flusso→operazioni
e sorgenti→tabelle DB; qui si fa solo la compilazione SQL + lo zip."""
from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.engine.dbt_export import DbtExportError, build_dbt_project, compile_model_sql

router = APIRouter(prefix="/dbt", tags=["dbt"])


class ModelSpec(BaseModel):
    name: str
    source: dict            # {bucket, key} della radice
    operations: list = []
    materialized: str = "table"


class ExportRequest(BaseModel):
    flow_name: str
    models: list[ModelSpec]
    source_map: dict         # key parquet → {ref, columns}
    attachments: list = []   # [{alias, db_type, conn, pw_env}]
    sources: list = []       # [{name, database, schema, tables}]


@router.post("/export")
def export_dbt(req: ExportRequest) -> StreamingResponse:
    def resolve(src: dict):
        entry = req.source_map.get(src.get("key"))
        if entry is None:
            raise DbtExportError(f"sorgente non mappata a una tabella DB: {src.get('key')}")
        return entry["ref"], entry.get("columns") or []

    try:
        models = [
            {
                "name": m.name,
                "sql": compile_model_sql(m.source, m.operations, resolve)[0],
                "materialized": m.materialized,
            }
            for m in req.models
        ]
        files = build_dbt_project(
            req.flow_name, models, req.attachments, req.sources
        )
    except DbtExportError as e:
        raise HTTPException(status_code=422, detail=str(e))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip")
