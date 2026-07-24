"""Prepara il payload di export dbt: risolve gli Output del flusso in
{source, operations} (via flow_resolver) e mappa OGNI sorgente (radice + rami
destri di join/union) alla sua TABELLA DB di origine (Datasource + Connection),
così il progetto dbt-duckdb federa i database reali (niente snapshot).

Limiti v1: solo sorgenti kind="database" con source_type="table" su connessioni
FEDERABILI da DuckDB (postgresql/mysql/mariadb). ClickHouse/Trino, sorgenti
file/flow e source_type="sql" → errore chiaro.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlmodel import Session, select

from app.models import Connection, Datasource, Flow
from app.services.flow_resolver import FlowResolveError, build_output_run_requests

# db_type federabili da DuckDB (scanner ufficiali) + porta di default
_FEDERABLE_PORT = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306}
# db_type con un adapter dbt NATIVO (v2, via traduzione sqlglot) + porta default
_NATIVE_PORT = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "clickhouse": 8123}


class DbtExportError(ValueError):
    pass


def _slug(s: str) -> str:
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(s).lower()).strip("_")
    return out or "x"


def _collect_source_keys(operations: list, out: set[str]) -> None:
    """Chiavi parquet di tutte le sorgenti annidate (right di join/union,
    driver e body dei foreach)."""
    for op in operations or []:
        params = op.get("params") or {}
        for nested_key in ("right", "driver"):
            ref = params.get(nested_key)
            if isinstance(ref, dict) and isinstance(ref.get("source"), dict):
                k = ref["source"].get("key")
                if k:
                    out.add(k)
                _collect_source_keys(ref.get("operations") or [], out)
        _collect_source_keys(params.get("body") or [], out)


def _parse_ref(source_ref: str, default_schema: str) -> tuple[str, str]:
    """`schema.table` → (schema, table); `table` → (default_schema, table)."""
    parts = (source_ref or "").split(".")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return default_schema, parts[-1] if parts else source_ref


def build_export_payload(session: Session, flow: Flow, default_bucket: str, target: str = "duckdb") -> dict:
    if target not in ("duckdb", "native"):
        raise DbtExportError(f"target sconosciuto: '{target}'")
    definition = json.loads(flow.definition or "{}")

    def resolve_ds(ds_id: int) -> Optional[tuple[str, str]]:
        ds = session.get(Datasource, ds_id)
        return (ds.bucket, ds.key) if ds and ds.key else None

    try:
        requests = build_output_run_requests(definition, resolve_ds, default_bucket)
    except FlowResolveError as e:
        raise DbtExportError(str(e))

    # tutte le chiavi sorgente (radice di ogni output + annidate)
    keys: set[str] = set()
    models = []
    for i, req in enumerate(requests):
        root_key = req["input_key"]
        keys.add(root_key)
        _collect_source_keys(req.get("operations") or [], keys)
        models.append({
            "name": _model_name(req, i),
            "source": {"bucket": req.get("bucket") or default_bucket, "key": root_key},
            "operations": req.get("operations") or [],
        })

    # risolvi ogni chiave in (datasource, connessione, schema, tabella)
    resolved: list[tuple[str, Datasource, Connection, str, str]] = []
    for key in sorted(keys):
        ds = session.exec(select(Datasource).where(Datasource.key == key)).first()
        if ds is None or ds.kind != "database":
            raise DbtExportError(
                "l'export dbt supporta solo sorgenti da database "
                "(una sorgente del flusso è un file o l'output di un altro flusso)"
            )
        if ds.source_type != "table":
            raise DbtExportError(f"la sorgente «{ds.name}» è una query SQL: supportate solo tabelle")
        conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
        if conn is None:
            raise DbtExportError(f"la sorgente «{ds.name}» non ha una connessione valida")
        # DB senza schemi (clickhouse/mysql): la tabella sta nel database; Postgres → public
        default_schema = conn.database if conn.db_type in ("clickhouse", "mysql", "mariadb") else "public"
        schema, table = _parse_ref(ds.source_ref, default_schema)
        resolved.append((key, ds, conn, schema, table))

    def _cols(ds: Datasource) -> list[str]:
        return [c.get("name") for c in json.loads(ds.columns or "[]") if c.get("name")]

    if target == "native":
        return _native_payload(flow, models, resolved, _cols)
    return _duckdb_payload(flow, models, resolved, _cols)


def _duckdb_payload(flow, models, resolved, cols) -> dict:
    source_map: dict[str, dict] = {}
    attachments: dict[int, dict] = {}
    sources: dict[tuple[str, str], dict] = {}
    for key, ds, conn, schema, table in resolved:
        if conn.db_type not in _FEDERABLE_PORT:
            raise DbtExportError(
                f"la connessione «{conn.name}» è {conn.db_type}: non federabile da DuckDB "
                "(federazione: postgresql/mysql/mariadb). Prova l'export «nativo» per ClickHouse."
            )
        alias = f"db_{conn.id}"
        src_name = _slug(f"{alias}_{schema}")
        attachments[conn.id] = {
            "alias": alias, "db_type": conn.db_type, "pw_env": f"TABULARIA_DB_{conn.id}_PASSWORD",
            "conn": {"host": conn.host, "port": conn.port or _FEDERABLE_PORT[conn.db_type],
                     "database": conn.database, "username": conn.username},
        }
        s = sources.setdefault((alias, schema), {"name": src_name, "database": alias, "schema": schema, "tables": []})
        if table not in s["tables"]:
            s["tables"].append(table)
        source_map[key] = {"ref": "{{ source('%s', '%s') }}" % (src_name, table), "columns": cols(ds)}
    return {
        "flow_name": flow.name, "mode": "duckdb", "models": models, "source_map": source_map,
        "attachments": list(attachments.values()), "sources": list(sources.values()),
    }


def _native_payload(flow, models, resolved, cols) -> dict:
    # un solo warehouse: tutte le sorgenti sulla STESSA connessione
    conn_ids = {conn.id for _, _, conn, _, _ in resolved}
    if len(conn_ids) > 1:
        raise DbtExportError(
            "l'export nativo richiede che tutte le sorgenti siano sulla STESSA connessione "
            "(un solo warehouse). Questo flusso ne usa più di una — usa l'export «federato»."
        )
    _, _, conn, _, _ = resolved[0]
    if conn.db_type not in _NATIVE_PORT:
        raise DbtExportError(f"nessun adapter dbt nativo per '{conn.db_type}'")

    source_map: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    for i, (key, ds, _c, schema, table) in enumerate(resolved):
        src_name = _slug(f"src_{schema}")
        source_map[key] = {
            "ident": f"__s_{i}",
            "macro": "{{ source('%s', '%s') }}" % (src_name, table),
            "columns": cols(ds),
        }
        s = sources.setdefault(schema, {"name": src_name, "database": conn.database, "schema": schema, "tables": []})
        if table not in s["tables"]:
            s["tables"].append(table)
    native = {
        "db_type": conn.db_type,
        "pw_env": f"TABULARIA_DB_{conn.id}_PASSWORD",
        "target_schema": "dbt_tabularia",
        "conn": {"host": conn.host, "port": conn.port or _NATIVE_PORT[conn.db_type],
                 "database": conn.database, "username": conn.username},
    }
    return {
        "flow_name": flow.name, "mode": "native", "models": models,
        "source_map": source_map, "native": native, "sources": list(sources.values()),
    }


def _model_name(req: dict, i: int) -> str:
    if req.get("publish"):
        return req["publish"].get("name") or f"model_{i + 1}"
    dest = req.get("destination") or {}
    if dest.get("type") == "database":
        return dest.get("table") or f"model_{i + 1}"
    if dest.get("type") == "s3":
        return (dest.get("key") or f"model_{i + 1}").rsplit("/", 1)[-1].split(".")[0]
    return f"model_{i + 1}"
