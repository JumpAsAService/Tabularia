"""The gateway's test harness.

In-memory SQLite DB (StaticPool: one connection shared by all of a test's sessions)
with the schema created by `create_all` — NOT by the ALTER migrations, which are
Postgres-only and serve only to patch tables already in prod.

The engine (data plane) is replaced by an httpx client with `MockTransport`: the
functions that talk to the engine (`_reconcile` through GET /tasks/{id}, the cleanup
through DELETE /files/object) run offline and their calls can be inspected.
"""
import base64
import json
import os

# Connection secrets are encrypted with SECURITY__FERNET_KEY: a random key when none is
# set, so the suite runs on a fresh clone (and in CI) without configuring anything.
os.environ.setdefault("SECURITY__FERNET_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())

import httpx
import pytest
from sqlalchemy import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401 — registers the models on SQLModel.metadata
from app.models import Datasource, Flow, Permission, Project, Run, User


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_deployment_defaults(monkeypatch):
    """The tests must not depend on the deployment's .env (the containers run them with the
    env_file): the automatic development sample stays off unless the test sets it
    itself."""
    monkeypatch.delenv("PREVIEW__DEFAULT_SAMPLE_ROWS", raising=False)
    for name in ("AI__BASE_URL", "AI__ACCESS_KEY", "AI__SECRET_KEY", "AI__DEFAULT_MODEL"):
        monkeypatch.delenv(name, raising=False)  # the AI assistant is off unless the test turns it on
    # the dev stack's own allowances (a fixed admin password, no address purge)
    for name in ("AUTH__ALLOW_KNOWN_ADMIN_PASSWORD", "AUDIT__IP_RETENTION_DAYS", "AUTH__FIRST_RUN_SETUP", "SECURITY__KEYS_DIR"):
        monkeypatch.delenv(name, raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
def fk_session():
    """A session on a database that enforces foreign keys, as Postgres does. The
    other tests run with them off (the factories rely on it), so a delete that
    leaves a reference behind passed here and answered 500 in production."""
    from sqlalchemy import event

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def session(db_engine):
    with Session(db_engine) as s:
        yield s


def _json_body(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


async def _chunks(data: bytes):
    yield data


class FakeEngine:
    """Fake engine: records the blobs' DELETEs and serves task states made to measure."""

    def __init__(self):
        self.deleted: list[tuple[str, str]] = []  # (bucket, key) deleted
        self.revoked: list[str] = []  # task_ids revoked (DELETE /tasks/{id})
        self.transforms: list[dict] = []  # bodies received on POST /tasks/transform-data
        self.ingests: list[tuple[str, dict]] = []  # (route, body) of the queued ingests
        self.inspects: list[dict] = []  # bodies received on POST /sharepoint/inspect
        # SFTP: bodies received on POST /sftp/inspect, and the answers to serve per action
        self.sftp_inspects: list[dict] = []
        # the key the fake SFTP server offers; a «test» answers as the engine does:
        # credentials only if it is the key being accepted, or else the accepted one
        self.sftp_server_key = {"type": "ssh-ed25519", "fingerprint": "SHA256:server-key"}
        self.sftp_responses: dict[str, tuple[int, dict]] = {
            "files": (200, {"files": [], "total": 0}),
            "validate": (200, {"ok": True, "expansions": 1, "first": {}, "last": {}}),
        }
        self.transform_status = 200  # a different answer to test an engine that refuses the run
        # HTTP API connections: bodies received on POST /http-api/inspect, and the answer per action
        self.api_inspects: list[dict] = []
        self.api_responses: dict[str, tuple[int, dict]] = {
            "test": (200, {"ok": True, "status": 200, "url": "https://api.example.com/v1"}),
            "validate": (200, {"ok": True, "calls": 1, "request": {}}),
            "try": (200, {"ok": True, "status": 200, "columns": ["id"], "rows": [{"id": 1}], "records": 1}),
        }
        # placeholders: bodies received on POST /placeholders/preview, and per-template errors to
        # serve ({template name: message}; a "*" key = a general error) — the grammar is the engine's
        self.placeholder_previews: list[dict] = []
        self.placeholder_errors: dict[str, str] = {}
        self.placeholder_values: dict[str, str] = {}  # {template: its value}; others answer with themselves
        # «S3 folder» datasource: bodies on POST /s3/inspect and the answer per action
        self.s3_inspects: list[dict] = []
        self.s3_responses: dict[str, tuple[int, dict]] = {
            "files": (200, {"files": [], "total": 0, "missing": [], "missing_total": 0, "expansions": 1}),
            "validate": (200, {"ok": True, "expansions": 1, "first": {}, "last": {}}),
        }
        # database connections: bodies on POST /db/inspect, the answer per action, and the
        # optional drivers GET /db/drivers reports (None = an engine without that route)
        self.db_inspects: list[dict] = []
        self.db_responses: dict[str, tuple[int, dict]] = {"test": (200, {"ok": True}), "tables": (200, {"tables": []}),
                                                          "columns": (200, {"columns": []})}
        # SQL scripts on a connection: bodies on POST /db/query and the answer to serve
        self.db_queries: list[dict] = []
        self.db_query_response: tuple[int, dict] = (200, {"columns": [], "rows": [], "row_count": 0, "truncated": False})
        # exports started on the engine (POST /tasks/exports, /db/exports): (route, body)
        self.exports: list[tuple[str, dict]] = []
        self.drivers: dict | None = {"sqlserver": {"installed": True}}
        self.dbt_exports: list[dict] = []  # bodies received on POST /dbt/export
        self.dbt_export_response: tuple[int, dict] | None = None  # an error answer to serve (e.g. a 422 with a code)
        self.dbt_export_script: list[tuple[int, dict] | None] = []  # answers in line, one per call (None = the zip)
        self.task_states: dict[str, dict] = {}  # task_id -> {status, result, error}
        # reports (docs/specs/reports.md): renders started, the save's check and the
        # choices read from a dataset. A check or choices answer of None = an engine
        # without that route (404), as before the engine had reports
        self.report_renders: list[dict] = []
        self.report_render_response: tuple[int, dict] | None = None  # an error to serve
        self.report_checks: list[dict] = []
        self.report_check_response: tuple[int, dict] | None = None
        self.report_choices: list[dict] = []
        self.report_choices_response: tuple[int, dict] | None = None
        # a dataset's rows counted (POST /reports/count): None = an engine without the route;
        # an exception is raised as the engine client would (a timeout)
        self.report_counts: list[dict] = []
        self.report_count_response: tuple[int, dict] | Exception | None = None
        # an export followed by its owner: its state per task (default: ready) and the file
        self.export_states: dict[str, list[dict]] = {}  # task_id -> states served in turn (the last one stays)
        self.export_polls: list[tuple[str, str | None]] = []  # (path, owner)
        self.export_file: bytes = b"%PDF-1.7 report"
        self.export_disposition = 'attachment; filename="report.pdf"'  # the file's name, as the engine sends it
        self.pushmails: list[dict] = []  # bodies received on POST /db/pushmail
        self.pushmail_status = 200  # a different answer to test an engine that refuses the email
        self.default_state = {"status": "SUCCESS", "result": {}, "error": None}
        self.delete_status = 200  # forces another outcome to test the sweep's retry
        self.copies: list[tuple[str, str, str]] = []  # (bucket, source, target) copied on the storage
        self.copy_status = 204  # a different answer to test a copy that fails
        # preview (AI assistant): bodies received and the answer to serve
        # data contracts: requests received on /contracts/* and answers to serve
        self.contract_calls: list[tuple[str, dict]] = []
        self.contract_response: tuple[int, dict] = (200, {"outcome": "passed", "rows": 3, "errors": 0, "warnings": 0, "rules": []})
        self.profile_response: tuple[int, dict] = (200, {"profile": {"rows": 3, "columns": []}, "proposal": {"description": "", "rules": []}})
        self.previews: list[dict] = []
        self.preview_response: tuple[int, dict] = (200, {"columns": [], "rows": [], "row_count": 0, "truncated": False})
        # catalog served by GET /engines. The engine says what is technically available; it is
        # the GATEWAY that lays the installation's policy over it (engines disabled by the
        # admin), so here they all arrive "available".
        self.engines: list[dict] = [
            {"id": "polars", "label": "Polars", "available": True, "description": ""},
            {"id": "duckdb", "label": "DuckDB", "available": True, "description": ""},
            {"id": "chdb", "label": "chDB", "available": True, "description": "", "optional": True},
            {"id": "clickhouse", "label": "ClickHouse", "available": True, "description": "", "optional": True},
        ]

    def set_task(self, task_id: str, status: str, result: dict | None = None,
                 error: str | None = None, error_detail: str | None = None):
        self.task_states[task_id] = {
            "status": status, "result": result or {}, "error": error, "error_detail": error_detail,
        }

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/engines":
            return httpx.Response(200, json=self.engines)
        # MUST sit before the "/tasks/" branch, which would otherwise catch it as if
        # "transform-data" were a task_id and answer with a state instead of an id. The
        # payload is recorded: it is the only way to check WHAT the gateway sends the engine —
        # that the secrets go out encrypted, and that the recipients are those validated
        # already.
        if path == "/sftp/inspect" and request.method == "POST":
            body = _json_body(request)
            body.setdefault("expected_fingerprint", "")
            self.sftp_inspects.append(body)
            action = body.get("action", "test")
            if action == "test" and "test" not in self.sftp_responses:
                pinned = body["connection"].get("host_key_fingerprint", "")
                offered = self.sftp_server_key["fingerprint"]
                ok = offered == (body["expected_fingerprint"] or pinned)
                return httpx.Response(200, json={
                    "ok": ok, "needs_acceptance": not ok, "host_key": dict(self.sftp_server_key),
                    "changed": bool(pinned) and pinned != offered, "previous": pinned, **({"home": "/home/tab"} if ok else {})})
            status, payload = self.sftp_responses[action]
            return httpx.Response(status, json=payload)
        if path == "/placeholders/preview" and request.method == "POST":
            body = _json_body(request)
            self.placeholder_previews.append(body)
            fields = {k: ({"error": self.placeholder_errors[k]} if k in self.placeholder_errors
                          else {"value": self.placeholder_values.get(v, v)})
                      for k, v in (body.get("templates") or {}).items()}
            return httpx.Response(200, json={"fields": fields, "expansions": 1, "now": "2026-10-18T00:00:00+00:00",
                                             "error": self.placeholder_errors.get("*")})
        if path == "/db/inspect" and request.method == "POST":
            body = _json_body(request)
            self.db_inspects.append(body)
            status, payload = self.db_responses[body.get("action", "test")]
            return httpx.Response(status, json=payload)
        if path == "/db/query" and request.method == "POST":
            self.db_queries.append(_json_body(request))
            status, payload = self.db_query_response
            return httpx.Response(status, json=payload)
        if path in ("/tasks/exports", "/db/exports") and request.method == "POST":
            self.exports.append((path, _json_body(request)))
            return httpx.Response(200, json={"task_id": f"t-export-{len(self.exports)}", "status": "submitted", "message": ""})
        if path == "/db/drivers" and request.method == "GET":
            if self.drivers is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.drivers)
        if path == "/s3/inspect" and request.method == "POST":
            body = _json_body(request)
            self.s3_inspects.append(body)
            status, payload = self.s3_responses[body.get("action", "files")]
            return httpx.Response(status, json=payload)
        if path == "/http-api/inspect" and request.method == "POST":
            body = _json_body(request)
            self.api_inspects.append(body)
            status, payload = self.api_responses[body.get("action", "test")]
            return httpx.Response(status, json=payload)
        if path in ("/db/ingest", "/sharepoint/ingest", "/sftp/ingest", "/http-api/ingest", "/s3/ingest") and request.method == "POST":
            import json as _json

            self.ingests.append((path, _json.loads(request.content or b"{}")))
            return httpx.Response(200, json={"task_id": f"t-ingest-{len(self.ingests)}"})
        if path == "/sharepoint/inspect" and request.method == "POST":
            import json as _json

            self.inspects.append(_json.loads(request.content or b"{}"))
            return httpx.Response(200, json={"ok": True, "files": [], "total": 0})
        if path == "/tasks/preview" and request.method == "POST":
            import json as _json

            self.previews.append(_json.loads(request.content or b"{}"))
            status, payload = self.preview_response
            return httpx.Response(status, json=payload)
        if path in ("/contracts/evaluate", "/contracts/profile") and request.method == "POST":
            import json as _json

            self.contract_calls.append((path, _json.loads(request.content or b"{}")))
            status, payload = self.contract_response if path.endswith("evaluate") else self.profile_response
            return httpx.Response(status, json=payload)
        if path == "/tasks/transform-data" and request.method == "POST":
            import json as _json

            self.transforms.append(_json.loads(request.content or b"{}"))
            if self.transform_status >= 400:
                return httpx.Response(self.transform_status, json={"detail": "engine refused"})
            return httpx.Response(200, json={"task_id": f"t-transform-{len(self.transforms)}"})
        if path == "/db/pushmail" and request.method == "POST":
            self.pushmails.append(json.loads(request.content or b"{}"))
            if self.pushmail_status >= 400:
                return httpx.Response(self.pushmail_status, json={"detail": "engine refused"})
            return httpx.Response(200, json={"task_id": f"t-pushmail-{len(self.pushmails)}", "status": "submitted", "message": ""})
        if path == "/reports/render" and request.method == "POST":
            self.report_renders.append(_json_body(request))
            if self.report_render_response is not None:
                return httpx.Response(self.report_render_response[0], json=self.report_render_response[1])
            return httpx.Response(200, json={"task_id": f"t-report-{len(self.report_renders)}"})
        if path == "/reports/check" and request.method == "POST" and self.report_check_response is not None:
            self.report_checks.append(_json_body(request))
            return httpx.Response(self.report_check_response[0], json=self.report_check_response[1])
        if path == "/reports/choices" and request.method == "POST" and self.report_choices_response is not None:
            self.report_choices.append(_json_body(request))
            return httpx.Response(self.report_choices_response[0], json=self.report_choices_response[1])
        if path == "/reports/count" and request.method == "POST" and self.report_count_response is not None:
            self.report_counts.append(_json_body(request))
            if isinstance(self.report_count_response, Exception):
                raise self.report_count_response
            return httpx.Response(self.report_count_response[0], json=self.report_count_response[1])
        if path.startswith("/tasks/exports/") and request.method == "GET":
            self.export_polls.append((path, request.url.params.get("owner")))
            tid = path.split("/")[3]
            if path.endswith("/file"):  # streamed, as the engine sends it (the gateway passes it on)
                return httpx.Response(200, content=_chunks(self.export_file), headers={
                    "content-type": "application/pdf", "content-disposition": self.export_disposition})
            states = self.export_states.get(tid) or [{"status": "ready", "filename": "report.pdf", "size": 15}]
            return httpx.Response(200, json=states.pop(0) if len(states) > 1 else states[0])
        if path.startswith("/tasks/"):
            tid = path.rsplit("/", 1)[-1]
            if request.method == "DELETE":  # revoke + terminate of the task
                self.revoked.append(tid)
                return httpx.Response(200, json={"task_id": tid, "status": "revoked"})
            return httpx.Response(200, json=self.task_states.get(tid, self.default_state))
        if path == "/dbt/export" and request.method == "POST":
            self.dbt_exports.append(json.loads(request.content or b"{}"))
            if self.dbt_export_script:
                next_one = self.dbt_export_script.pop(0)
                if next_one is not None:
                    return httpx.Response(next_one[0], json=next_one[1])
            elif self.dbt_export_response is not None:
                return httpx.Response(self.dbt_export_response[0], json=self.dbt_export_response[1])
            return httpx.Response(200, content=b"PK\x05\x06" + b"\x00" * 18, headers={"content-type": "application/zip"})
        if path == "/files/copy" and request.method == "POST":
            p = request.url.params
            if self.copy_status < 400:
                self.copies.append((p.get("bucket"), p.get("source_key"), p.get("target_key")))
            return httpx.Response(self.copy_status)
        if path == "/files/object" and request.method == "DELETE":
            if self.delete_status < 400:  # actual deletion succeeded
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


# ── minimal factories (FKs not enforced by SQLite → only what is needed is created) ──
def make_user(session, **kw):
    kw.setdefault("email", "u@x.local")
    kw.setdefault("hashed_password", "x")
    kw.setdefault("is_superuser", False)
    u = User(**kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def token_of(session, user, request=None, method="password"):
    """A valid token for `user`: opens a real session, as the login does."""
    from app.services import sessions

    return sessions.open_session(session, user, request, method)[1]


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


def make_flow(session, **kw):
    kw.setdefault("name", "flow")
    kw.setdefault("project_id", 1)
    f = Flow(**kw)
    session.add(f)
    session.commit()
    session.refresh(f)
    return f


def make_permission(session, *, user_id, project_id, capability="view"):
    p = Permission(user_id=user_id, project_id=project_id, capability=capability)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


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


@pytest.fixture(autouse=True)
def _openlineage_off(monkeypatch):
    """No test sends lineage to a real collector: the development container may have
    OPENLINEAGE__URL in the environment, and the runs the tests close would end up in
    Marquez. Whoever wants to test the emission turns it on themselves."""
    from app.core.config import get_settings

    s = get_settings().openlineage
    monkeypatch.setattr(s, "url", "")
    monkeypatch.setattr(s, "file", "")
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("OPENLINEAGE_CONFIG", raising=False)
