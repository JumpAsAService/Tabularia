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
import json
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


def test_0035_adds_the_providers_and_leaves_what_was_there(temporary_database):
    """An installation with the environment's provider: its enabled models, conversations and bills
    keep their names, and the previous version's reads still work on the migrated database."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0034")
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) "
                       "VALUES (1, 'ann@example.com', '', true, false, false, now())"))
        c.execute(text("INSERT INTO ai_models (model_id, enabled, updated_at, updated_by) VALUES "
                       "('gpt-oss-120b', true, now(), 1), ('glm-5.2', false, now(), NULL)"))
        c.execute(text("INSERT INTO ai_chats (id, user_id, title, model_id, kind, created_at, updated_at) "
                       "VALUES (5, 1, 'Sales', 'gpt-oss-120b', 'assistant', now(), now())"))
        c.execute(text("INSERT INTO ai_chat_turns (id, chat_id, seq, question, messages, model_id, input_tokens, output_tokens, "
                       "requests, cost_usd, created_at) VALUES (7, 5, 0, 'q', '[]', 'gpt-oss-120b', 10, 2, 1, '0.01', now())"))
        c.execute(text("INSERT INTO ai_spend (ts, user_id, turn_id, model_id, input_tokens, output_tokens, cost_usd) "
                       "VALUES (now(), 1, 7, 'gpt-oss-120b', 10, 2, '0.01')"))
    m.migra(temporary_database)  # stops if the database and the models differ
    with temporary_database.connect() as c:
        assert sorted(c.execute(text("SELECT model_id, enabled FROM ai_models")).all()) == [("glm-5.2", False), ("gpt-oss-120b", True)]
        assert c.execute(text("SELECT model_id FROM ai_chats")).scalar() == "gpt-oss-120b"
        assert c.execute(text("SELECT model_id, cost_usd FROM ai_spend")).one() == ("gpt-oss-120b", "0.01")
        assert c.execute(text("SELECT count(*) FROM ai_providers")).scalar() == 0
    # what the previous version does during a rolling update: inserts a row by its bare name
    with temporary_database.begin() as c:
        c.execute(text("INSERT INTO ai_models (model_id, enabled, updated_at) VALUES ('another', true, now())"))


def test_0036_adds_the_dashboards_and_leaves_what_was_there(temporary_database):
    """An installation with its folders and reports: nothing of it changes, the three tables come
    empty, and what the previous version does during a rolling update still works."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0035")
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) VALUES "
                       "(1, 'ann@example.com', '', true, false, false, now()), (2, 'bob@example.com', '', true, false, false, now())"))
        c.execute(text("INSERT INTO projects (id, name, description, owner_id, created_at) VALUES "
                       "(10, 'Sales', '', 1, now()), (11, 'Old', '', 1, now())"))
        c.execute(text("INSERT INTO reports (id, name, description, project_id, owner_id, current_version, created_at, updated_at, views_total) "
                       "VALUES (5, 'Margin', '', 10, 1, 1, now(), now(), 3)"))
        c.execute(text("INSERT INTO report_versions (id, report_id, version, content, note, created_at) "
                       "VALUES (7, 5, 1, '{\"v\": 1}', 'created', now())"))
    m.migra(temporary_database)  # stops if the database and the models differ
    with temporary_database.connect() as c:
        assert c.execute(text("SELECT name, views_total FROM reports")).one() == ("Margin", 3)
        assert c.execute(text("SELECT content FROM report_versions")).scalar() == '{"v": 1}'
        assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in
                ("dashboards", "dashboard_versions", "dashboard_views")] == [0, 0, 0]
    # the previous version's writes do not know the new tables
    with temporary_database.begin() as c:
        c.execute(text("INSERT INTO reports (name, description, project_id, current_version, created_at, updated_at, views_total) "
                       "VALUES ('Another', '', 10, 1, now(), now(), 0)"))
        c.execute(text("DELETE FROM projects WHERE id = 11"))
        c.execute(text("DELETE FROM report_views"))


def test_0036_on_postgres_a_name_is_unique_in_its_folder_and_the_openings_go_with_their_person(temporary_database):
    """What SQLite does not hold in the other tests: the constraints, the cascade, the JSON of
    `sizes_of` and the housekeeping's windows are Postgres's."""
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session

    from app.services import dashboards, housekeeping

    m.migra(temporary_database)
    content = {"v": 1, "parameters": [{"name": "p", "type": "text"}], "datasets": [], "tiles": [{"id": "a"}, {"id": "b"}]}
    with temporary_database.begin() as c:
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) "
                       "VALUES (1, 'ann@example.com', '', true, false, false, now())"))
        c.execute(text("INSERT INTO projects (id, name, description, owner_id, created_at) VALUES (10, 'Sales', '', 1, now())"))
        c.execute(text("INSERT INTO dashboards (id, name, description, project_id, owner_id, current_version, created_at, updated_at) "
                       "VALUES (5, 'Live', '', 10, 1, 3, now(), now())"))
        for v in range(1, 6):
            c.execute(text("INSERT INTO dashboard_versions (dashboard_id, version, content, note, created_at) "
                           "VALUES (5, :v, :content, 'edited', now())"), {"v": v, "content": json.dumps(content)})
        c.execute(text("INSERT INTO dashboard_views (dashboard_id, user_id, day, at) VALUES (5, 1, '2026-10-30', now())"))
    with pytest.raises(IntegrityError):
        with temporary_database.begin() as c:
            c.execute(text("INSERT INTO dashboards (name, description, project_id, current_version, created_at, updated_at) "
                           "VALUES ('Live', '', 10, 1, now(), now())"))
    with Session(temporary_database) as session:
        assert dashboards.sizes_of(session, [5]) == {5: (0, 1, 2)}
        assert housekeeping.dashboard_versions(session, 2) == 2  # v1, v2: v3 is the current one
    with temporary_database.begin() as c:
        assert [r[0] for r in c.execute(text("SELECT version FROM dashboard_versions ORDER BY version"))] == [3, 4, 5]
        c.execute(text("UPDATE dashboards SET owner_id = NULL"))
        c.execute(text("UPDATE projects SET owner_id = NULL"))
        c.execute(text("DELETE FROM users WHERE id = 1"))
        assert c.execute(text("SELECT count(*) FROM dashboard_views")).scalar() == 0


def test_0037_adds_the_ai_comments_and_leaves_what_was_there(temporary_database):
    """Two tables, empty, that the previous version does not know, during a rolling update too."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0036")
        c.execute(text("INSERT INTO ai_models (model_id, enabled, updated_at) VALUES ('a-model', true, now())"))
    m.migra(temporary_database)  # stops if the database and the models differ
    with temporary_database.connect() as c:
        assert c.execute(text("SELECT model_id FROM ai_models")).scalar() == "a-model"
        assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("ai_comments", "ai_comment_threads")] == [0, 0]
    with temporary_database.begin() as c:
        c.execute(text("INSERT INTO ai_models (model_id, enabled, updated_at) VALUES ('another', true, now())"))


def test_0038_adds_the_flow_shortcuts_and_leaves_what_was_there(temporary_database):
    """One nullable column on users: the accounts keep everything, nobody has chosen a menu,
    and the previous version's insert (without the column) still works."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0037")
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, locale, created_at) "
                       "VALUES (1, 'ann@example.com', 'Ann', true, false, false, 'it', now())"))
    m.migra(temporary_database)  # stops if the database and the models differ
    with temporary_database.connect() as c:
        assert c.execute(text("SELECT email, locale, flow_shortcuts FROM users")).one() == ("ann@example.com", "it", None)
    with temporary_database.begin() as c:
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) "
                       "VALUES (2, 'bob@example.com', '', true, false, false, now())"))


def test_0039_adds_the_share_links_and_leaves_what_was_there(temporary_database):
    """Three tables the previous version does not know, and the settings' one row with the
    feature on; a deleted creator takes their links and the links' sessions with them."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0038")
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) "
                       "VALUES (1, 'ann@example.com', 'Ann', true, false, false, now())"))
    m.migra(temporary_database)  # stops if the database and the models differ
    with temporary_database.connect() as c:
        assert c.execute(text("SELECT email FROM users")).scalar() == "ann@example.com"
        assert c.execute(text("SELECT enabled, max_days, ai_comments_per_hour FROM share_link_settings")).one() == (True, 90, 20)
    with temporary_database.begin() as c:
        c.execute(text(
            "INSERT INTO share_links (id, token_hash, token_hint, kind, object_id, label, creator_id, embeddable, origins, "
            "password_temporary, failures, params, downloads, expires_at, created_at, openings, ai_calls) "
            "VALUES (1, 'h', 'abcd', 'report', 7, '', 1, false, '[]', true, 0, '{}', '[]', now(), now(), 0, 0)"))
        c.execute(text("INSERT INTO share_link_sessions (id, link_id, purpose, created_at, last_seen_at, expires_at) "
                       "VALUES ('s', 1, 'view', now(), now(), now())"))
    with temporary_database.begin() as c:
        c.execute(text("DELETE FROM users WHERE id = 1"))
    with temporary_database.connect() as c:
        assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("share_links", "share_link_sessions")] == [0, 0]


def test_0040_adds_the_data_apis_and_leaves_what_was_there(temporary_database):
    """Five tables the previous version does not know. A datasource a resource publishes cannot
    be deleted; a deleted creator takes their data APIs, and with them the resources, versions,
    keys and usage; who saved a version or made a key is forgotten with the account."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0039")
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) "
                       "VALUES (1, 'ann@example.com', 'Ann', true, false, false, now()), "
                       "(2, 'bob@example.com', 'Bob', true, false, false, now())"))
    m.migra(temporary_database)  # stops if the database and the models differ
    with temporary_database.connect() as c:
        assert c.execute(text("SELECT email FROM users WHERE id = 1")).scalar() == "ann@example.com"
        assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                for t in ("data_apis", "data_api_versions", "data_api_resources", "data_api_keys", "data_api_usage")] == [0] * 5
    with temporary_database.begin() as c:
        c.execute(text("INSERT INTO projects (id, name, description, created_at) VALUES (1, 'p', '', now())"))
        c.execute(text("INSERT INTO datasources (id, name, description, project_id, bucket, key, columns, sort_keys, "
                       "column_descriptions, kind, created_at, updated_at) "
                       "VALUES (1, 'customers', '', 1, 'b', 'k', '[]', '[]', '{}', 'flow', now(), now())"))
        c.execute(text(
            "INSERT INTO data_apis (id, name, slug, description, project_id, creator_id, state, runtime, current_version, "
            "created_at, updated_at) VALUES (1, 'CRM', 'crm', '', 1, 1, 'draft', '{}', 1, now(), now())"))
        c.execute(text("INSERT INTO data_api_versions (data_api_id, version, content, note, created_at, created_by) "
                       "VALUES (1, 1, '{}', 'created', now(), 2)"))
        c.execute(text("INSERT INTO data_api_resources (data_api_id, position, name, source_kind, datasource_id, columns, "
                       "filters, default_sort, max_page) VALUES (1, 0, 'customers', 'datasource', 1, '[]', '[]', '[]', 1000)"))
        c.execute(text("INSERT INTO data_api_keys (id, data_api_id, name, prefix, hash, expires_at, rate_per_minute, "
                       "resources, cidrs, created_by, created_at) VALUES (1, 1, 'CRM', 'abcdefgh', 'h', now(), 600, '[]', '[]', 2, now())"))
        c.execute(text("INSERT INTO data_api_usage (data_api_id, key_id, minute, requests, rows, errors, throttled) "
                       "VALUES (1, 1, now(), 1, 1, 0, 0)"))
    with pytest.raises(Exception, match="data_api_resources"):
        with temporary_database.begin() as c:
            c.execute(text("DELETE FROM datasources WHERE id = 1"))
    with temporary_database.begin() as c:
        c.execute(text("DELETE FROM users WHERE id = 2"))
    with temporary_database.connect() as c:
        assert c.execute(text("SELECT created_by FROM data_api_versions")).scalar() is None
        assert c.execute(text("SELECT created_by FROM data_api_keys")).scalar() is None
    with temporary_database.begin() as c:
        c.execute(text("DELETE FROM users WHERE id = 1"))
    with temporary_database.connect() as c:
        assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                for t in ("data_apis", "data_api_versions", "data_api_resources", "data_api_keys", "data_api_usage")] == [0] * 5
        assert c.execute(text("SELECT count(*) FROM datasources")).scalar() == 1


def test_0041_adds_the_data_api_pods_and_leaves_what_was_there(temporary_database):
    """Two tables the previous version does not know; both go with their data API."""
    with temporary_database.begin() as c:
        command.upgrade(m._config(c), "0040")
        c.execute(text("INSERT INTO users (id, email, full_name, is_active, is_superuser, is_observer, created_at) "
                       "VALUES (1, 'ann@example.com', 'Ann', true, false, false, now())"))
        c.execute(text("INSERT INTO projects (id, name, description, created_at) VALUES (1, 'p', '', now())"))
        c.execute(text(
            "INSERT INTO data_apis (id, name, slug, description, project_id, creator_id, state, runtime, current_version, "
            "created_at, updated_at) VALUES (1, 'CRM', 'crm', '', 1, 1, 'active', '{}', 1, now(), now())"))
    m.migra(temporary_database)  # stops if the database and the models differ
    with temporary_database.connect() as c:
        assert c.execute(text("SELECT slug, state FROM data_apis")).one() == ("crm", "active")
        assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("data_api_status", "data_api_pods")] == [0, 0]
    with temporary_database.begin() as c:
        c.execute(text("INSERT INTO data_api_status (data_api_id, cluster, checked_at) VALUES (1, '{}', now())"))
        c.execute(text("INSERT INTO data_api_pods (data_api_id, pod, cluster, report, reported_at) "
                       "VALUES (1, 'tabularia-data-api-1-abc', '{}', '{}', now())"))
    with pytest.raises(Exception, match="uq_data_api_pod"):
        with temporary_database.begin() as c:
            c.execute(text("INSERT INTO data_api_pods (data_api_id, pod, cluster, report) "
                           "VALUES (1, 'tabularia-data-api-1-abc', '{}', '{}')"))
    with temporary_database.begin() as c:
        c.execute(text("DELETE FROM data_apis WHERE id = 1"))
    with temporary_database.connect() as c:
        assert [c.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in ("data_api_status", "data_api_pods")] == [0, 0]
