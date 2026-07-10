"""Connessioni esterne: database (PostgreSQL/MySQL/MariaDB/ClickHouse/Trino)
e object storage S3-compatibile (db_type="s3": AWS, MinIO, R2, Wasabi…).

Per le connessioni S3 le colonne sono riusate con questo mapping:
host = endpoint URL (vuoto = AWS), username = access key id,
password_encrypted = secret access key, database = bucket di default,
db_schema = region.

Tutte le operazioni richiedono la capability CONNECT sul progetto della
connessione (ortogonale a VIEW/EDIT: chi gestisce i dati di una cartella non
usa credenziali DB per questo; MANAGE la include). La barriera di sicurezza è
QUI: chi può usare una connessione legge tutto ciò che le sue credenziali
leggono — consigliata un'utenza DB read-only.

La password/secret è cifrata a riposo (Fernet) e non esce mai dalle API: verso
l'engine viaggia ancora cifrata (`password_encrypted`, stessa chiave).
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.crypto import encrypt_secret
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, Datasource, Project, User
from app.models.permission import Capability
from app.schemas.models import ConnectionCreate, ConnectionOut, ConnectionUpdate
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connections"])

SUPPORTED_DB_TYPES = {"postgresql", "mysql", "mariadb", "clickhouse", "trino", "s3"}


def _to_out(conn: Connection) -> ConnectionOut:
    return ConnectionOut(
        **conn.model_dump(exclude={"password_encrypted", "created_at"}),
        has_password=bool(conn.password_encrypted),
    )


def _get_connection(session: Session, conn_id: int) -> Connection:
    conn = session.get(Connection, conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connessione non trovata")
    return conn


def _name_taken(session: Session, project_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(Connection).where(Connection.project_id == project_id, Connection.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Connection.id != exclude_id)
    return session.exec(stmt).first() is not None


def engine_connection_payload(conn: Connection) -> dict:
    """Il payload `connection` per l'engine: password/secret ANCORA cifrata."""
    if conn.db_type == "s3":
        return _s3_payload(conn.host, conn.username, conn.password_encrypted, conn.database, conn.db_schema)
    return {
        "db_type": conn.db_type,
        "host": conn.host,
        "port": conn.port,
        "username": conn.username,
        "password_encrypted": conn.password_encrypted,
        "database": conn.database,
        "db_schema": conn.db_schema,
    }


def _s3_payload(host: str, username: str, secret_encrypted: str, database: str, db_schema: str) -> dict:
    """Mapping colonne→campi S3 (vedi docstring del modulo)."""
    return {
        "db_type": "s3",
        "endpoint_url": host or "",
        "access_key": username or "",
        "secret_key_encrypted": secret_encrypted or "",
        "bucket": database or "",
        "region": db_schema or "",
    }


async def _engine_inspect(payload: dict, action: str) -> dict:
    client = get_engine_client()
    resp = await client.post("/db/inspect", json={"connection": payload, "action": action})
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=str(detail)[:500])
    return resp.json()


# ── CRUD ──────────────────────────────────────────────────────────────────────
@router.get("/connections", response_model=list[ConnectionOut])
def list_all_connections(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Tutte le connessioni USABILI dall'utente (per il picker delle sorgenti DB)."""
    connectable = perm_service.connectable_project_ids(session, user)
    if not connectable:
        return []
    rows = session.exec(
        select(Connection).where(Connection.project_id.in_(connectable)).order_by(Connection.name)
    ).all()
    return [_to_out(c) for c in rows]


@router.get("/projects/{project_id}/connections", response_model=list[ConnectionOut])
def list_project_connections(
    project_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.CONNECT)
    rows = session.exec(
        select(Connection).where(Connection.project_id == project_id).order_by(Connection.name)
    ).all()
    return [_to_out(c) for c in rows]


@router.post(
    "/projects/{project_id}/connections",
    response_model=ConnectionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_connection(
    project_id: int,
    body: ConnectionCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.CONNECT)
    if body.db_type not in SUPPORTED_DB_TYPES:
        raise HTTPException(status_code=422, detail=f"db_type non supportato: {sorted(SUPPORTED_DB_TYPES)}")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Il nome della connessione è vuoto")
    if _name_taken(session, project_id, name):
        raise HTTPException(status_code=409, detail=f"Esiste già una connessione '{name}' nella cartella")

    conn = Connection(
        name=name,
        description=body.description,
        project_id=project_id,
        owner_id=user.id,
        db_type=body.db_type,
        host=body.host,
        port=body.port,
        username=body.username,
        password_encrypted=encrypt_secret(body.password) if body.password else "",
        database=body.database,
        db_schema=body.db_schema,
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return _to_out(conn)


@router.patch("/connections/{conn_id}", response_model=ConnectionOut)
def update_connection(
    conn_id: int,
    body: ConnectionUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)

    target_project = conn.project_id
    if body.project_id is not None and body.project_id != conn.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Progetto di destinazione non trovato")
        ensure_can(session, user, body.project_id, Capability.CONNECT)
        target_project = body.project_id

    new_name = body.name.strip() if body.name is not None else conn.name
    if not new_name:
        raise HTTPException(status_code=422, detail="Il nome non può essere vuoto")
    if _name_taken(session, target_project, new_name, exclude_id=conn.id):
        raise HTTPException(status_code=409, detail=f"Esiste già una connessione '{new_name}' nella cartella")

    conn.name = new_name
    conn.project_id = target_project
    for field in ("description", "host", "port", "username", "database", "db_schema"):
        value = getattr(body, field)
        if value is not None:
            setattr(conn, field, value)
    if body.password is not None:
        # stringa vuota = rimuovi la password; valorizzata = sostituisci
        conn.password_encrypted = encrypt_secret(body.password) if body.password else ""
    conn.updated_at = datetime.now(timezone.utc)
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return _to_out(conn)


@router.delete("/connections/{conn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    conn_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    used_by = session.exec(select(Datasource).where(Datasource.connection_id == conn_id)).first()
    if used_by:
        raise HTTPException(
            status_code=409,
            detail="La connessione è usata da una o più datasource: eliminale o spostale prima",
        )
    session.delete(conn)
    session.commit()


# ── Ispezione (via engine) ────────────────────────────────────────────────────
@router.post("/projects/{project_id}/connections/test")
async def test_draft_connection(
    project_id: int,
    body: ConnectionCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Prova una connessione PRIMA di salvarla (il form della UI)."""
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.CONNECT)
    if body.db_type not in SUPPORTED_DB_TYPES:
        raise HTTPException(status_code=422, detail=f"db_type non supportato: {sorted(SUPPORTED_DB_TYPES)}")
    secret = encrypt_secret(body.password) if body.password else ""
    if body.db_type == "s3":
        payload = _s3_payload(body.host, body.username, secret, body.database, body.db_schema)
    else:
        payload = {
            "db_type": body.db_type,
            "host": body.host,
            "port": body.port,
            "username": body.username,
            "password_encrypted": secret,
            "database": body.database,
            "db_schema": body.db_schema,
        }
    return await _engine_inspect(payload, "test")


@router.post("/connections/{conn_id}/test")
async def test_connection(
    conn_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    return await _engine_inspect(engine_connection_payload(conn), "test")


@router.get("/connections/{conn_id}/tables")
async def list_connection_tables(
    conn_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conn = _get_connection(session, conn_id)
    ensure_can(session, user, conn.project_id, Capability.CONNECT)
    return await _engine_inspect(engine_connection_payload(conn), "tables")
