"""The gateway's settings changed from the panel (app/core/overrides.py).

The rules these tests hold: only the settings in the catalogue can be changed; a value
passes the panel's range AND the setting's own validation; a pair of settings that
breaks a constraint refuses the one just changed; the environment stays the base and
comes back when an override goes; what the code reads at each use sees a change at once;
a database that cannot be read leaves the values in force.
"""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.core import overrides as ov
from app.core.config import get_settings, settings_from_env
from app.models import GatewaySetting

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clean():
    ov.forget()
    yield
    ov.forget()


def _err(values, changed=None):
    good, errors = ov.check(values, settings_from_env(), changed)
    return good, {e["key"]: e["code"] for e in errors}


# ── one value ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key,value,code", [
    ("orchestrator.max_concurrent", True, "integer"),        # a bool is not a number here
    ("orchestrator.max_concurrent", "5", "integer"),
    ("orchestrator.max_concurrent", 2.5, "integer"),
    ("orchestrator.poll_interval_seconds", "1", "number"),
    ("auth.personal_tokens", 1, "boolean"),
    ("ai.default_model", 3, "text"),
    ("orchestrator.max_concurrent", 0, "min"),                # the panel's range
    ("orchestrator.max_concurrent", 201, "max"),
    ("app.timezone", "Mars/Olympus_Mons", "invalid"),         # the setting's own validator
    ("app.timezone", "   ", "invalid"),
    ("scripts.build_cpu", "two", "invalid"),
    ("containers.max_memory", "16 gigabytes", "invalid"),
    ("oidc.button_label", "x" * 61, "too_long"),
    ("db.url", "postgresql://elsewhere", "unknown"),           # not in the catalogue: never
    ("audit.retention_minutes", 1, "unknown"),                 # the audit trail is the installer's
    ("oidc.superuser_group", "everyone", "unknown"),
])
def test_a_wrong_value_is_refused_with_its_reason(key, value, code):
    good, errors = _err({key: value})
    assert good == {} and errors == {key: code}


def test_good_values_come_back_as_the_setting_holds_them():
    good, errors = _err({"orchestrator.poll_interval_seconds": 2, "app.timezone": " Europe/Rome ",
                         "scripts.build_memory": "8Gi", "auth.personal_tokens": False})
    assert errors == {}
    assert good == {"orchestrator.poll_interval_seconds": 2.0, "app.timezone": "Europe/Rome",
                    "scripts.build_memory": "8Gi", "auth.personal_tokens": False}
    assert isinstance(good["orchestrator.poll_interval_seconds"], float)


def test_not_a_dict_is_refused():
    assert ov.check(["orchestrator.max_concurrent"], settings_from_env()) == ({}, [{"key": "", "code": "unknown"}])


def test_every_entry_of_the_catalogue_names_a_real_setting():
    base = settings_from_env()
    for voce in ov.VOCI:
        assert hasattr(getattr(base, voce.section), voce.field), voce.key
        if voce.rollout:
            assert set(voce.rollout) <= {"gateway", "orchestrator"}


# ── constraints between two settings ─────────────────────────────────────────
def test_a_heartbeat_too_slow_for_the_dead_after_refuses_the_one_changed():
    # defaults: heartbeat 10, dead after 60
    good, errors = _err({"orchestrator.heartbeat_seconds": 30})
    assert errors == {"orchestrator.heartbeat_seconds": "heartbeat_dead_after"}
    good, errors = _err({"orchestrator.heartbeat_seconds": 10, "orchestrator.dead_after_seconds": 20},
                        changed={"orchestrator.dead_after_seconds"})
    assert errors == {"orchestrator.dead_after_seconds": "heartbeat_dead_after"}
    assert good == {"orchestrator.heartbeat_seconds": 10.0}
    good, errors = _err({"orchestrator.heartbeat_seconds": 20, "orchestrator.dead_after_seconds": 60})
    assert errors == {}  # exactly three beats: allowed


def test_executions_beyond_the_pool_are_refused():
    # defaults: no pool size → the gateway's threads (40) + overflow 10 = 50
    good, errors = _err({"orchestrator.max_concurrent": 50})
    assert errors == {}
    good, errors = _err({"orchestrator.max_concurrent": 51})
    assert errors == {"orchestrator.max_concurrent": "concurrent_pool"}
    # shrinking the pool under the executions already allowed: the pool is the one refused
    good, errors = _err({"orchestrator.max_concurrent": 20, "db.pool_size": 5, "db.max_overflow": 0},
                        changed={"db.pool_size"})
    assert errors == {"db.pool_size": "concurrent_pool"} and "db.max_overflow" in good


def test_a_constraint_the_environment_already_breaks_refuses_nothing_unrelated(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR__HEARTBEAT_SECONDS", "50")  # 150 > 60, the installer's choice
    get_settings.cache_clear()
    good, errors = _err({"ai.max_requests": 10})
    assert good == {"ai.max_requests": 10} and errors == {}


# ── the values in force ──────────────────────────────────────────────────────
def test_the_overlay_changes_only_the_sections_touched_and_never_the_environment():
    base = settings_from_env()
    ov.set_values({"orchestrator.max_concurrent": 7})
    s = get_settings()
    assert s.orchestrator.max_concurrent == 7
    assert base.orchestrator.max_concurrent == 20          # the environment untouched
    assert s.ai is base.ai and s.db is base.db              # the other sections are the same objects
    assert get_settings() is s                              # nothing rebuilt while nothing changes
    ov.set_values({})
    assert get_settings() is base                           # back to the installation's value


def test_what_the_code_reads_at_each_use_sees_the_change():
    from app.routes import runs
    from app.services import blobgc
    from app.services import orchestrator as orch

    before = blobgc.blob_deletion_grace_seconds()
    ov.set_values({"orchestrator.max_concurrent": 3, "engine.run_stale_timeout_seconds": 1800})
    assert orch._orc().max_concurrent == 3
    assert runs.stale_after_seconds() == 1800
    assert blobgc.blob_deletion_grace_seconds() != before  # it follows the run's lifetime
    ov.set_values({})
    assert orch._orc().max_concurrent == 20
    assert blobgc.blob_deletion_grace_seconds() == before


def _save(session, key, value, raw=None):
    session.add(GatewaySetting(key=key, value=raw if raw is not None else json.dumps(value),
                               updated_at=datetime.now(timezone.utc), updated_by="admin@example.com"))
    session.commit()


def test_reload_puts_the_good_ones_in_force_and_drops_the_rest(session, db_engine):
    _save(session, "orchestrator.max_concurrent", 4)
    _save(session, "ai.max_requests", 999)                  # out of range
    _save(session, "db.url", "sqlite://")                   # not in the catalogue (a hand-written row)
    _save(session, "app.timezone", None, raw="{not json")
    assert ov.reload(db_engine) == {"orchestrator.max_concurrent": 4}
    assert get_settings().orchestrator.max_concurrent == 4
    assert get_settings().ai.max_requests == settings_from_env().ai.max_requests


def test_an_override_removed_gives_the_environment_back(session, db_engine):
    _save(session, "orchestrator.max_concurrent", 4)
    ov.reload(db_engine)
    session.exec(text("DELETE FROM gateway_settings"))
    session.commit()
    ov.reload(db_engine)
    assert get_settings().orchestrator.max_concurrent == 20


def test_a_database_that_cannot_be_read_leaves_the_values_in_force(db_engine):
    ov.set_values({"orchestrator.max_concurrent": 4})

    class Down:
        def connect(self):
            raise OSError("connection refused")

    assert ov.reload(Down()) == {"orchestrator.max_concurrent": 4}
    assert get_settings().orchestrator.max_concurrent == 4


def test_loading_at_start_never_stops_the_gateway():
    ov.load_at_start("postgresql+psycopg://nobody@127.0.0.1:1/none")  # nothing listens there
    assert ov.current() == {}


def test_loading_at_start_reads_the_values(tmp_path):
    from sqlmodel import SQLModel, create_engine

    dsn = f"sqlite:///{tmp_path / 'g.db'}"
    eng = create_engine(dsn)
    SQLModel.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO gateway_settings (key, value, updated_at) VALUES ('db.pool_size', '12', '2026-10-23')"))
    eng.dispose()
    ov.load_at_start(dsn)
    assert get_settings().db.pool_size == 12


def test_cache_clear_forgets_the_panel():
    ov.set_values({"orchestrator.max_concurrent": 4})
    get_settings.cache_clear()
    assert get_settings().orchestrator.max_concurrent == 20


# ── clients built with a setting ─────────────────────────────────────────────
def test_the_jwks_client_follows_the_timeout():
    from app.services import sso

    sso._jwks_clients.clear()
    a = sso._jwks_client("https://idp.example/jwks", 5)
    assert sso._jwks_client("https://idp.example/jwks", 5) is a
    assert sso._jwks_client("https://idp.example/jwks", 9) is not a
    sso._jwks_clients.clear()


def test_the_openlineage_client_is_rebuilt_when_its_timeout_changes(monkeypatch, tmp_path):
    from app.services import openlineage as ol

    monkeypatch.setenv("OPENLINEAGE__FILE", str(tmp_path / "events.jsonl"))
    get_settings.cache_clear()
    monkeypatch.setattr(ol, "_client", None)
    monkeypatch.setattr(ol, "_client_timeout", None)
    a = ol._get_client()
    assert ol._get_client() is a
    ov.set_values({"openlineage.timeout_seconds": 3})
    assert ol._get_client() is not a
    injected = object()
    ol.set_client(injected)                                 # the tests' client stays
    ov.set_values({"openlineage.timeout_seconds": 4})
    assert ol._get_client() is injected


# ── from the bug hunt of 2026-10-23 ───────────────────────────────────────────
@pytest.mark.parametrize("value,code", [(float("nan"), "number"), (float("inf"), "number"), (10 ** 400, "max")])
def test_a_decimal_that_is_not_a_finite_number_is_refused(value, code):
    """NaN passed every comparison: a poll interval of NaN left the waits without a ceiling."""
    good, errors = _err({"orchestrator.poll_interval_seconds": value})
    assert good == {} and errors == {"orchestrator.poll_interval_seconds": code}


def test_the_claim_with_the_groups_cannot_be_emptied():
    """Empty, every sign-in would take every group away (and the administrators' rights)."""
    assert _err({"oidc.groups_claim": "   "})[1] == {"oidc.groups_claim": "invalid"}


def test_admitting_only_the_groups_taken_needs_a_list_of_them(monkeypatch):
    good, errors = _err({"oidc.require_allowlisted_group": True})
    assert errors == {"oidc.require_allowlisted_group": "allowlist_needed"}
    assert _err({"oidc.require_allowlisted_group": True, "oidc.group_allowlist": "analysts"})[1] == {}
    monkeypatch.setenv("OIDC__SUPERUSER_GROUP", "tabularia-admins")
    get_settings.cache_clear()
    assert _err({"oidc.require_allowlisted_group": True})[1] == {}


def test_a_change_during_an_overlay_is_not_lost(monkeypatch):
    """A value saved while another thread was laying the previous ones over the settings
    stayed out for good: the old result was cached under the new version."""
    ov.set_values({"orchestrator.max_concurrent": 3})
    real = ov.apply

    def meanwhile(base, values):
        out = real(base, values)
        ov.set_values({"orchestrator.max_concurrent": 4})
        return out

    monkeypatch.setattr(ov, "apply", meanwhile)
    get_settings()
    monkeypatch.setattr(ov, "apply", real)
    assert get_settings().orchestrator.max_concurrent == 4


def test_the_executions_follow_the_pool_the_process_started_with(monkeypatch):
    """Raised together from the panel, the executions applied at once and the pool only
    after the restart: they could take more connections than it had."""
    from app.services import orchestrator as orch

    class Pool:
        _max_overflow = 2

        def size(self):
            return 5

    monkeypatch.setattr(orch, "engine", type("E", (), {"pool": Pool()})())
    ov.set_values({"orchestrator.max_concurrent": 50})
    assert orch.concorrenza() == 7
    ov.set_values({"orchestrator.max_concurrent": 3})
    assert orch.concorrenza() == 3


def test_a_shorter_heartbeat_applies_without_waiting_for_the_old_one(monkeypatch):
    import threading
    import time as t

    from app.services import orchestrator as orch

    beats = []
    monkeypatch.setattr(orch, "batti", lambda: beats.append(t.monotonic()))
    ov.set_values({"orchestrator.heartbeat_seconds": 60.0, "orchestrator.dead_after_seconds": 600.0})
    stop = threading.Event()
    orch.avvia_battito(stop)
    t.sleep(0.5)
    ov.set_values({"orchestrator.heartbeat_seconds": 2.0})
    t.sleep(3.6)
    stop.set()
    assert beats, "with the beat at 2 s it beats within 3.6 s, not after the 60 s already started"


@pytest.mark.parametrize("key,value", [("scripts.build_memory", "0"), ("scripts.build_memory", "0.5"), ("containers.max_memory", "10Mi"),
                                       ("containers.max_cpu", "0"), ("scripts.build_cpu", "0m")])
def test_a_quantity_too_small_to_run_anything_is_refused(key, value):
    good, errors = _err({key: value})
    assert good == {} and errors[key] in ("too_small", "invalid")


def test_a_real_quantity_passes():
    assert _err({"scripts.build_memory": "512Mi", "containers.max_cpu": "500m"})[1] == {}
