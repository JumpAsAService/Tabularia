"""Harness di test del gateway.

DB SQLite in-memory (StaticPool: una sola connessione condivisa da tutte le
sessioni del test) con lo schema creato da `create_all` — NON dalle migrazioni
ALTER, che sono Postgres-only e servono solo a patchare tabelle già in prod.

L'engine (data plane) è sostituito da un client httpx con `MockTransport`: le
funzioni che parlano con l'engine (`_reconcile` via GET /tasks/{id}, il cleanup
via DELETE /files/object) girano offline e le loro chiamate sono ispezionabili.
"""
import httpx
import pytest
from sqlalchemy import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401 — registra i modelli su SQLModel.metadata
from app.models import Datasource, Flow, Project, Run, User


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_engine):
    with Session(db_engine) as s:
        yield s


class FakeEngine:
    """Engine finto: registra le DELETE dei blob e serve stati di task su misura."""

    def __init__(self):
        self.deleted: list[tuple[str, str]] = []  # (bucket, key) cancellati
        self.task_states: dict[str, dict] = {}  # task_id -> {status, result, error}
        self.default_state = {"status": "SUCCESS", "result": {}, "error": None}
        self.delete_status = 200  # forza un esito diverso per testare il retry dello sweep

    def set_task(self, task_id: str, status: str, result: dict | None = None, error: str | None = None):
        self.task_states[task_id] = {"status": status, "result": result or {}, "error": error}

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/tasks/"):
            tid = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=self.task_states.get(tid, self.default_state))
        if path == "/files/object" and request.method == "DELETE":
            if self.delete_status < 400:  # cancellazione effettiva riuscita
                self.deleted.append(
                    (request.url.params.get("bucket"), request.url.params.get("key"))
                )
            return httpx.Response(self.delete_status, json={"ok": self.delete_status < 400})
        return httpx.Response(404, json={"detail": "not mocked"})


@pytest.fixture
def fake_engine(monkeypatch):
    fake = FakeEngine()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake._handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)
    yield fake


# ── factory minimali (FK non forzate da SQLite → si crea solo ciò che serve) ──
def make_user(session, **kw):
    kw.setdefault("email", "u@x.local")
    kw.setdefault("hashed_password", "x")
    kw.setdefault("is_superuser", False)
    u = User(**kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def make_project(session, **kw):
    kw.setdefault("name", "proj")
    p = Project(**kw)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def make_datasource(session, **kw):
    kw.setdefault("name", "ds")
    kw.setdefault("project_id", 1)
    kw.setdefault("bucket", "data-prep")
    kw.setdefault("key", "datasets/old.parquet")
    kw.setdefault("kind", "flow")
    d = Datasource(**kw)
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def make_run(session, **kw):
    kw.setdefault("task_id", "t-1")
    kw.setdefault("status", "STARTED")
    kw.setdefault("input_key", "datasets/in.parquet")
    kw.setdefault("output_bucket", "data-prep")
    kw.setdefault("output_key", "datasets/new.parquet")
    r = Run(**kw)
    session.add(r)
    session.commit()
    session.refresh(r)
    return r
