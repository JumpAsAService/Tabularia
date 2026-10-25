"""The automatic backup of this database (services/db_backup.py, Administration → Backups).

Fixed here: the schedule is planned and a due backup is taken by one process only; off,
nothing is planned but «Back up now» still runs; a backup left by a dead process is taken
again once stale; pg_dump gets the password from its environment, never its command line;
a backup is stored and the oldest beyond the number to keep go; a failure says why, on
the row and in the audit log; the administrators change the settings (and put them
back), the observer reads without errors and downloads nothing; a download streams the
file and is in the audit log.
"""
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.session import get_session
from app.models import AuditLog, DatabaseBackup
from app.routes import backups as backup_routes
from app.services import db_backup
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
NOW = datetime(2026, 10, 22, 12, 0)
OLD = [f"tabularia-2026-10-{d:02d}T03-00-00Z.dump" for d in (18, 19, 20, 21)]


class FakeStore:
    """The engine's /backups, over a dict."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.fail_put = ""

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/backups" and req.method == "GET":
            items = [{"name": n, "size": len(b), "modified": None} for n, b in sorted(self.objects.items(), reverse=True)]
            return httpx.Response(200, json={"bucket": "tabularia", "prefix": "backups/postgres/", "endpoint": "",
                                             "same_as_data": True, "items": items, "error": None})
        name = path.rsplit("/", 1)[-1]
        if req.method == "PUT":
            if self.fail_put:
                return httpx.Response(502, json={"detail": self.fail_put})
            self.objects[name] = req.read()
            return httpx.Response(200, json={"size": len(self.objects[name])})
        if req.method == "GET":
            if name not in self.objects:
                return httpx.Response(404, json={"detail": "No such backup"})
            return httpx.Response(200, content=self.objects[name])
        if req.method == "DELETE":
            self.objects.pop(name, None)
            return httpx.Response(204)
        return httpx.Response(405)


@pytest.fixture
def store(monkeypatch, db_engine):
    fake = FakeStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)
    monkeypatch.setattr("app.db.session.engine", db_engine)

    async def dump():  # a pg_dump that works
        yield b"PGDMP"
        yield b"x" * 1000
    monkeypatch.setattr(db_backup, "_pg_dump", dump)
    return fake


def _row(session) -> DatabaseBackup:
    session.expire_all()
    return session.get(DatabaseBackup, 1)


def test_the_schedule_is_planned_and_a_due_backup_is_taken_once(session):
    assert db_backup.claim(session, NOW) is None  # planned, not due
    r = _row(session)
    assert r.next_at > NOW and r.next_for == "0 3 * * *"
    due = r.next_at + timedelta(seconds=1)
    assert db_backup.claim(session, due) == {"keep": 14, "who": None}
    assert db_backup.claim(session, due) is None  # running, and the next one is tomorrow
    r = _row(session)
    assert r.running_since == due and r.next_at > due


def test_off_plans_nothing_but_back_up_now_still_runs(session):
    db_backup.claim(session, NOW)
    r = _row(session)
    r.enabled = False
    r.keep = 3
    session.add(r)
    session.commit()
    assert db_backup.claim(session, NOW + timedelta(days=2)) is None and _row(session).next_at is None
    r = _row(session)
    r.requested_at, r.requested_by = NOW, "capo@x.local"
    session.add(r)
    session.commit()
    assert db_backup.claim(session, NOW) == {"keep": 3, "who": "capo@x.local"}
    assert _row(session).requested_at is None


def test_a_backup_left_by_a_dead_process_is_taken_again_later(session):
    db_backup.claim(session, NOW)
    r = _row(session)
    # running for an hour, its heartbeat 30 s ago: alive
    r.requested_at, r.running_since, r.alive_at = NOW, NOW - timedelta(hours=1), NOW - timedelta(seconds=30)
    session.add(r)
    session.commit()
    assert db_backup.claim(session, NOW) is None
    # no heartbeat for three minutes: its process is gone
    assert db_backup.claim(session, NOW + timedelta(minutes=3)) is not None


def test_a_database_restored_from_a_backup_does_not_block_the_backups(session):
    """The dump is taken while its own backup is marked running: restored, that claim of a
    process long gone blocked «Back up now» and the nightly backup for over an hour
    (found on kind, 2026-10-23)."""
    r = db_backup.row(session)
    r.running_since, r.alive_at, r.claimed_by = NOW - timedelta(minutes=10), NOW - timedelta(minutes=10), "gone:1:x"
    session.add(r)
    session.commit()
    assert not db_backup.running(_row(session), NOW)
    r = _row(session)
    r.requested_at = NOW
    session.add(r)
    session.commit()
    assert db_backup.claim(session, NOW) is not None


def test_pg_dump_gets_the_password_from_the_environment(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().db, "url", "postgresql+psycopg2://tab:s3cret@db.internal:6432/meta?sslmode=require")
    env, args = db_backup._pg_env_and_args()
    assert "s3cret" not in " ".join(args) and env["PGPASSWORD"] == "s3cret" and env["PGSSLMODE"] == "require"
    assert args[-1] == "meta" and "db.internal" in args and "6432" in args and "--format=custom" in args


def test_the_dump_is_made_with_the_server_s_own_version(tmp_path, monkeypatch):
    for v in (16, 18):
        (tmp_path / str(v) / "bin").mkdir(parents=True)
        exe = tmp_path / str(v) / "bin" / "pg_dump"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    monkeypatch.setattr(db_backup, "VERSIONS_DIR", str(tmp_path))
    assert db_backup.pg_dump_for(16) == str(tmp_path / "16" / "bin" / "pg_dump")
    assert db_backup.pg_dump_for(19) == "pg_dump" and db_backup.pg_dump_for(None) == "pg_dump"  # the default, newest


@pytest.mark.anyio
async def test_a_backup_is_stored_and_the_oldest_beyond_keep_go(session, store):
    for name in OLD:
        store.objects[name] = b"old"
    db_backup.row(session)
    out = await db_backup.make(keep=3, who="capo@x.local")
    assert out["ok"] and out["removed"] == 2
    assert sorted(store.objects) == [OLD[2], OLD[3], out["name"]]
    assert store.objects[out["name"]].startswith(b"PGDMP")
    r = _row(session)
    assert r.last_name == out["name"] and r.last_size == 1005 and r.running_since is None and r.last_ok_at
    item = session.exec(select(AuditLog).where(AuditLog.action == "backup.run")).one()
    assert item.outcome == "success" and item.actor_label == "capo@x.local"


@pytest.mark.anyio
async def test_a_failed_backup_says_why(session, store, monkeypatch):
    async def broken():
        yield b"PGDMP"  # halfway, then it fails: the upload breaks, nothing is stored
        raise db_backup.BackupFailed("pg_dump failed: could not connect to server")
    monkeypatch.setattr(db_backup, "_pg_dump", broken)
    db_backup.row(session)
    out = await db_backup.make(keep=3)
    assert not out["ok"] and "could not connect" in out["error"] and store.objects == {}
    r = _row(session)
    assert "could not connect" in r.last_error and r.last_error_at and r.last_ok_at is None
    store.fail_put = "AccessDenied: Access Denied."
    monkeypatch.setattr(db_backup, "_pg_dump", _works)
    out = await db_backup.make(keep=3)
    assert not out["ok"] and "AccessDenied" in _row(session).last_error
    items = session.exec(select(AuditLog).where(AuditLog.action == "backup.run")).all()
    assert [v.outcome for v in items] == ["failure", "failure"] and items[0].actor_label == db_backup.SCHEDULE_ACTOR


@pytest.mark.anyio
async def test_a_backup_stopped_with_its_process_says_so(session, store, monkeypatch):
    # a drain or an upgrade at 03:00 stops the orchestrator mid-backup: the next time has
    # already moved to tomorrow, and nothing said that tonight's was lost
    import asyncio

    started = asyncio.Event()

    async def slow():
        yield b"PGDMP"
        started.set()
        await asyncio.sleep(60)
    monkeypatch.setattr(db_backup, "_pg_dump", slow)
    db_backup.row(session)
    task = asyncio.create_task(db_backup.make(keep=3))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    r = _row(session)
    assert "Stopped before the end" in r.last_error and r.running_since is None
    item = session.exec(select(AuditLog).where(AuditLog.action == "backup.run")).one()
    assert item.outcome == "failure"


async def _works():
    yield b"PGDMP"


# ── the panel ────────────────────────────────────────────────────────────────

@pytest.fixture
def app(session, store, monkeypatch):
    from app.routes import auth as auth_routes

    monkeypatch.setattr(db_backup, "disabled_reason", lambda s: None)  # SQLite here
    app = FastAPI()
    for r in (auth_routes.router, backup_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _sign_in(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def test_the_administrator_changes_the_settings_and_puts_them_back(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    st = boss.get("/admin/backups").json()
    assert st["enabled"] and st["schedule"] == "0 3 * * *" and st["keep"] == 14 and st["editable"]
    assert st["bucket"] == "tabularia" and st["prefix"] == "backups/postgres/" and st["same_as_data"] is True
    assert boss.put("/admin/backups", json={"schedule": "every night"}, headers=CSRF).status_code == 422
    st = boss.put("/admin/backups", json={"schedule": "30 1 * * 0", "keep": 4}, headers=CSRF).json()
    assert st["schedule"] == "30 1 * * 0" and st["keep"] == 4 and st["changed"] == {"enabled": False, "schedule": True, "keep": True}
    assert datetime.fromisoformat(st["next_at"]).weekday() == 6  # the next one follows the new schedule at once
    assert boss.put("/admin/backups", json={"enabled": False}, headers=CSRF).json()["next_at"] is None
    boss.put("/admin/backups", json={"reset": ["enabled"]}, headers=CSRF)
    st = boss.put("/admin/backups", json={"reset": ["schedule"]}, headers=CSRF).json()
    assert st["schedule"] == "0 3 * * *" and st["changed"]["schedule"] is False and st["keep"] == 4
    assert session.exec(select(AuditLog).where(AuditLog.action == "backup.update")).all()


def test_back_up_now_once_at_a_time(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.post("/admin/backups/run", headers=CSRF)
    assert r.status_code == 200 and r.json()["requested_at"]
    assert boss.post("/admin/backups/run", headers=CSRF).status_code == 409


def test_the_observer_reads_without_errors_and_downloads_nothing(app, session, store):
    store.objects[OLD[0]] = b"PGDMP"
    r = db_backup.row(session)
    r.last_error = "pg_dump failed: password authentication failed for user tab on db.internal"
    session.add(r)
    session.commit()
    eye = _sign_in(app, session, "occhio@x.local", is_observer=True)
    st = eye.get("/admin/backups").json()
    assert st["last_error"] is None and not st["editable"] and [i["name"] for i in st["items"]] == [OLD[0]]
    assert eye.get(f"/admin/backups/{OLD[0]}/download").status_code == 403
    assert eye.put("/admin/backups", json={"keep": 1}, headers=CSRF).status_code == 403
    assert eye.post("/admin/backups/run", headers=CSRF).status_code == 403
    assert _sign_in(app, session, "anna@x.local").get("/admin/backups").status_code == 403


def test_a_download_streams_the_file_and_is_in_the_audit_log(app, session, store):
    store.objects[OLD[0]] = b"PGDMP" + b"y" * 5000
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.get(f"/admin/backups/{OLD[0]}/download")
    assert r.status_code == 200 and r.content == store.objects[OLD[0]]
    assert OLD[0] in r.headers["content-disposition"]
    assert boss.get("/admin/backups/notes.txt/download").status_code == 404
    assert boss.get(f"/admin/backups/{OLD[1]}/download").status_code == 404
    item = session.exec(select(AuditLog).where(AuditLog.action == "backup.download")).one()
    assert item.target_label == OLD[0]


def test_off_kubernetes_the_backups_are_greyed_out(session, tmp_path, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().kubernetes, "token_file", str(tmp_path / "no-token"))
    assert db_backup.disabled_reason(session) == "kubernetes"  # Docker Compose
    token = tmp_path / "token"
    token.write_text("t")
    monkeypatch.setattr(get_settings().kubernetes, "token_file", str(token))
    assert db_backup.disabled_reason(session) == "postgres"  # on a cluster, SQLite here
