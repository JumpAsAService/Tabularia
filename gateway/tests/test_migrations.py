"""The schema migrations (Alembic), on a REAL Postgres: the gateway's tests run on SQLite
with create_all, but the migrations are Postgres DDL. Every test creates a temporary
database on the configured Postgres (DB__*) and removes it at the end; without a
reachable Postgres the tests are skipped.

What is tested (docs/database-migrations.md):
· a new database, migrated, is the same as the models — if a model changes without its
  revision, this test fails (it replaces the hand rule «every new column wants its
  ALTER»);
· a database from BEFORE Alembic (the real schema of the development database, with
  the drift the old mechanism left) is adopted and becomes the same as the models;
· a difference between database and models stops the step, and leaves no half
  changes;
· without automatic migration the gateway stops on a database behind, not on a newer
  one (rolling update);
· the revisions are one chain."""
import os
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db import migra as m

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "schema_before_alembic.sql")


def _url_base():
    url = make_url(get_settings().db.dsn)
    return url if url.drivername.startswith("postgresql") else None


@pytest.fixture
def temporary_database():
    """An empty database on the configured Postgres; gone at the end."""
    base = _url_base()
    if base is None:
        pytest.skip("serve un Postgres (DB__*)")
    administration = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"zzmig_{uuid.uuid4().hex[:10]}"
    try:
        with administration.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:  # noqa: BLE001 — no reachable Postgres: skipped
        pytest.skip(f"Postgres non raggiungibile ({e.__class__.__name__})")
    eng = create_engine(base.set(database=name))
    try:
        yield eng
    finally:
        eng.dispose()
        with administration.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        administration.dispose()


def test_a_new_migrated_database_is_equal_to_the_models(temporary_database):
    outcome = m.migra(temporary_database)
    assert outcome == {"before": None, "after": m.head(), "adopted": False}
    with temporary_database.connect() as c:
        assert m.differences(c) == []
        assert m.revision(c) == m.head()
    # again: nothing to do
    assert m.migra(temporary_database) == {"before": m.head(), "after": m.head(), "adopted": False}


def test_a_pre_alembic_database_is_adopted_and_becomes_equal_to_the_models(temporary_database):
    with temporary_database.begin() as c:
        c.exec_driver_sql(open(FIXTURE).read())
    with temporary_database.connect() as c:
        assert m.revision(c) is None and m.differences(c)          # the old mechanism's drift is there
    outcome = m.migra(temporary_database)
    assert outcome["adopted"] and outcome["before"] is None and outcome["after"] == m.head()
    with temporary_database.connect() as c:
        assert m.differences(c) == []
        insp = inspect(c)
        assert not insp.has_table("schema_state")                   # the old mechanism's fingerprint goes
        fk = {f["name"] for f in insp.get_foreign_keys("flows")}
        assert "flows_notify_connection_id_fkey" in fk              # the foreign key the ALTER did not add
        assert "uq_users_oidc_identity" in {i["name"] for i in insp.get_indexes("users")}
    assert m.migra(temporary_database)["adopted"] is False        # adopted once only


def test_a_difference_from_the_models_stops_the_step_without_half_changes(temporary_database):
    m.migra(temporary_database)
    with temporary_database.begin() as c:
        c.execute(text("ALTER TABLE users ADD COLUMN zz_a_mano INTEGER"))      # touched by hand, or a model without a revision
    with temporary_database.connect() as c:
        assert any("zz_a_mano" in str(d) for d in m.differences(c))
    with pytest.raises(RuntimeError, match="differs from the models"):
        m.migra(temporary_database)
    # a database from before Alembic that does not add up: the adoption is undone whole.
    # «From before Alembic» = the BASE's schema, without the later revisions' tables or
    # columns: it is built this way, not by removing pieces from today's schema (which at
    # every new revision would leave something behind)
    with temporary_database.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    with temporary_database.begin() as c:
        m.command.upgrade(m._config(c), m.BASE)
        c.execute(text("DROP TABLE alembic_version"))
        c.execute(text("ALTER TABLE users ADD COLUMN zz_a_mano INTEGER"))
    with pytest.raises(RuntimeError, match="differs from the models"):
        m.migra(temporary_database)
    with temporary_database.connect() as c:
        assert m.revision(c) is None and not inspect(c).has_table("alembic_version")
        # nothing halfway: not even the tables of the revisions after the base
        assert not inspect(c).has_table("user_sessions")


def test_without_automatic_migration_the_gateway_stops_only_when_the_database_is_behind(temporary_database):
    with pytest.raises(m.DatabaseBehind, match="python -m app.db.migra"):
        m.check_revision(temporary_database)                 # empty: behind
    m.migra(temporary_database)
    assert m.check_revision(temporary_database) == m.head()
    with temporary_database.begin() as c:                          # a revision this code does not know: newer
        c.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    assert m.check_revision(temporary_database) == "9999"


def test_the_revisions_are_one_numbered_chain():
    script = m._script()
    assert len(script.get_heads()) == 1
    revisions = list(script.walk_revisions())
    numbers = sorted(int(r.revision) for r in revisions)
    assert numbers == list(range(1, len(numbers) + 1)) and all(len(r.revision) == 4 for r in revisions)
    assert m.BASE == "0001" and script.get_revision(m.BASE).down_revision is None


def test_a_database_newer_than_the_code_does_not_stop_the_step(temporary_database, monkeypatch):
    """The previous release after a `helm rollback`: its `migrate` step found a revision it
    does not know and stopped every new pod (Init:CrashLoopBackOff), so the rollback the
    upgrade guide promises never completed (tried on kind, 1.2.2 → 1.2.1, 2026-10-23)."""
    m.migra(temporary_database)
    with temporary_database.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    assert m.migra(temporary_database) == {"before": "9999", "after": "9999", "adopted": False}
    assert m.check_revision(temporary_database) == "9999"


def test_0034_takes_manage_off_the_personal_folders_and_off_them_only(temporary_database):
    """A personal folder is told by the line its creation left in the audit, not by its
    shape: this revision also runs on installations that never had one, where the shape
    can be a company folder and its grants are somebody's decision."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0033")
        for uid, email, admin in ((1, "leo@example.com", False), (2, "boss@example.com", True),
                                  (3, "other@example.com", False), (4, "ann@example.com", False)):
            c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) "
                           "VALUES (:i, :e, '', true, :a, false, now())"), {"i": uid, "e": email, "a": admin})
        c.execute(text("INSERT INTO groups (id, name, description, is_admin, is_observer, created_at) "
                       "VALUES (1, 'finance-leads', '', false, false, now())"))
        for pid, name, owner in ((10, "leo", 1), (11, "Finance", 2), (12, "ann", 4), (13, "boss", 2)):
            c.execute(text("INSERT INTO projects (id, name, description, owner_id, created_at) "
                           "VALUES (:i, :n, '', :o, now())"), {"i": pid, "n": name, "o": owner})
        for pid, uid, gid, capability in (
            (10, 1, None, "manage"),                                    # the person's own
            (10, 3, None, "manage"), (10, None, 1, "connect"),          # what the person shared
            (10, 3, None, "view"),
            (11, 2, None, "manage"), (11, None, 1, "manage"),           # a company folder of the same shape
            (12, 4, None, "edit"), (12, 4, None, "manage"),
            # folder 13: an administrator took the person's grant away, and it stays away
        ):
            c.execute(text("INSERT INTO permissions (project_id, user_id, group_id, capability) "
                           "VALUES (:p, :u, :g, :c)"), {"p": pid, "u": uid, "g": gid, "c": capability})
        for pid, detail in ((10, '{"parent_id": null, "personal": true, "user_id": 1}'),
                            (11, '{"parent_id": null}'),
                            (12, '{"parent_id": null, "personal": true, "user_id": 4, "via": "sign-in"}'),
                            (13, '{"parent_id": null, "personal": true, "user_id": 2}')):
            c.execute(text("INSERT INTO audit_logs (ts, actor_label, action, outcome, target_type, target_id, detail) "
                           "VALUES (now(), '', 'folder.create', 'success', 'folder', :p, :d)"), {"p": pid, "d": detail})
    m.migra(temporary_database)
    with temporary_database.connect() as c:
        rows = c.execute(text("SELECT project_id, user_id, group_id, capability FROM permissions")).all()
    assert sorted(rows, key=str) == sorted([
        (10, 1, None, "edit"), (10, 3, None, "view"),                   # no more MANAGE or CONNECT on it
        (11, 2, None, "manage"), (11, None, 1, "manage"),               # untouched
        (12, 4, None, "edit"),                                          # no second EDIT
    ], key=str)
