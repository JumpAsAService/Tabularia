"""Client addresses are personal data: they are kept for a declared time, and they go
with the account.

The audit log kept every row's address and browser for ever, and deleting an account
left them there (`actor_id` became NULL, the row and its address stayed). Now:
- AUDIT__IP_RETENTION_DAYS (90 by default) empties the address and the browser of
  every audit row, token and session older than that; the row itself stays, so the
  log still says who did what and when. 0 keeps them for ever.
- deleting an account empties the address and the browser of its rows at once,
  failed sign-ins with its email included.

Every test runs on the throwaway SQLite database of the test harness.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.core.config import Settings
from app.models import ApiToken, AuditLog, LoginAttempt, User, UserSession
from app.services import ip_retention
from tests.conftest import make_user
from tests.test_migrazioni import database_temporaneo  # noqa: F401 — the fixture

ORA = datetime(2026, 10, 18, 12, 0, 0)


def _voce(session, giorni_fa: float, ip="203.0.113.7", agent="Firefox", **kw) -> AuditLog:
    kw.setdefault("actor_label", "tizio@x.it")
    v = AuditLog(action="flow.run", outcome="success", ts=ORA - timedelta(days=giorni_fa),
                 ip=ip, user_agent=agent, **kw)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def _token(session, user, giorni_fa: float, ip="203.0.113.8") -> ApiToken:
    t = ApiToken(user_id=user.id, name=f"t{giorni_fa}", prefix="tab_x", token_hash=f"h{giorni_fa}{user.id}",
                 created_at=ORA - timedelta(days=400), expires_at=ORA + timedelta(days=30),
                 last_used_at=ORA - timedelta(days=giorni_fa), last_used_ip=ip)
    session.add(t)
    session.commit()
    return t


# ── the setting ────────────────────────────────────────────────────────────────
def test_addresses_are_kept_ninety_days_by_default(monkeypatch):
    monkeypatch.delenv("AUDIT__IP_RETENTION_DAYS", raising=False)  # the dev stack sets 0
    assert Settings().audit.ip_retention_days == 90


def test_zero_keeps_them_for_ever(session):
    v = _voce(session, 4000)
    assert ip_retention.delete_addresses(session, 0, at=ORA) == 0
    session.refresh(v)
    assert v.ip == "203.0.113.7" and v.user_agent == "Firefox"


# ── the purge ──────────────────────────────────────────────────────────────────
def test_only_rows_past_the_window_lose_their_address(session):
    vecchia, limit, recente = _voce(session, 91), _voce(session, 90), _voce(session, 1)
    assert ip_retention.delete_addresses(session, 90, at=ORA) >= 1
    for v in (vecchia, limit, recente):
        session.refresh(v)
    assert (vecchia.ip, vecchia.user_agent) == (None, None)
    # the row itself stays: who did what, and when
    assert vecchia.action == "flow.run" and vecchia.actor_label == "tizio@x.it"
    assert limit.ip == "203.0.113.7" and recente.ip == "203.0.113.7"


def test_it_empties_a_backlog_bigger_than_one_batch(session, monkeypatch):
    monkeypatch.setattr(ip_retention, "BATCH", 7)
    for _ in range(30):
        _voce(session, 200)
    ip_retention.delete_addresses(session, 90, at=ORA)
    assert session.exec(select(AuditLog).where(AuditLog.ip.is_not(None))).all() == []


def test_tokens_and_sessions_lose_an_old_address(session):
    u = make_user(session, email="u@x.it")
    old, new = _token(session, u, 120), _token(session, u, 2)
    stop = UserSession(id="s-old", user_id=u.id, created_at=ORA - timedelta(days=200),
                        last_seen_at=ORA - timedelta(days=100), expires_at=ORA + timedelta(days=1),
                        ip="198.51.100.1", user_agent="Chrome")
    viva = UserSession(id="s-new", user_id=u.id, created_at=ORA - timedelta(hours=1),
                       last_seen_at=ORA, expires_at=ORA + timedelta(hours=11), ip="198.51.100.2", user_agent="Chrome")
    session.add_all([stop, viva])
    session.commit()
    ip_retention.delete_addresses(session, 90, at=ORA)
    for x in (old, new, stop, viva):
        session.refresh(x)
    assert old.last_used_ip is None and new.last_used_ip == "203.0.113.8"
    assert (stop.ip, stop.user_agent) == (None, None) and viva.ip == "198.51.100.2"


def test_the_old_last_seen_address_of_users_goes(session):
    """`users.last_seen_ip` is no longer written (the address lives in the session):
    what an older release wrote there has no date of its own and nobody reads it."""
    u = make_user(session, email="u@x.it", last_seen_ip="192.0.2.4")
    ip_retention.delete_addresses(session, 90, at=ORA)
    session.refresh(u)
    assert u.last_seen_ip is None


def test_one_round_runs_the_address_purge_even_with_the_whole_log_kept(session, monkeypatch):
    """AUDIT__RETENTION_MINUTES=0 (rows for ever, the default) must not switch the
    address purge off: they are two settings."""
    from app.services import audit_retention

    v = _voce(session, 100)
    s = Settings(audit={"retention_minutes": 0, "ip_retention_days": 90})
    audit_retention.one_round(session, s, at=ORA, addresses=True)
    session.refresh(v)
    assert v.ip is None and len(session.exec(select(AuditLog)).all()) == 1


def test_the_loop_runs_when_only_the_address_purge_is_on():
    from app.services import audit_retention

    assert audit_retention.to_do(Settings(audit={"retention_minutes": 0, "ip_retention_days": 90}))
    assert not audit_retention.to_do(Settings(audit={"retention_minutes": 0, "ip_retention_days": 0}))


# ── deleting an account ────────────────────────────────────────────────────────
def test_deleting_an_account_empties_its_addresses_at_once(session):
    from app.routes.users import remove_user

    admin = make_user(session, email="admin@x.it", is_superuser=True)
    lei = make_user(session, email="Anna.Bianchi@x.it")
    sua = _voce(session, 1, actor_id=lei.id, actor_label="Anna.Bianchi@x.it")
    # failed sign-ins: no actor, only the email as typed
    tentativo = _voce(session, 1, actor_label="anna.bianchi@X.it")
    altrui = _voce(session, 1, actor_id=admin.id, actor_label="admin@x.it")
    session.add(LoginAttempt(ip="203.0.113.7", email="anna.bianchi@x.it", failures=2, last_at=ORA))
    session.commit()

    remove_user(session, lei.id)

    for v in (sua, tentativo, altrui):
        session.refresh(v)
    assert (sua.ip, sua.user_agent, tentativo.ip, tentativo.user_agent) == (None, None, None, None)
    assert altrui.ip == "203.0.113.7"
    # the log still reads: the action, its time and the email of the time stay
    assert sua.action == "flow.run" and sua.actor_label == "Anna.Bianchi@x.it" and sua.actor_id is None
    assert session.exec(select(LoginAttempt)).all() == []
    assert session.get(User, lei.id) is None


def test_forgetting_an_account_on_a_database_that_enforces_foreign_keys(fk_session):
    from app.routes.users import remove_user

    lei = make_user(fk_session, email="anna@x.it")
    v = _voce(fk_session, 1, actor_id=lei.id, actor_label="anna@x.it")
    remove_user(fk_session, lei.id)
    fk_session.refresh(v)
    assert v.ip is None and v.actor_id is None


# ── the same on a real Postgres (a throwaway database, never the application's) ──
def test_purge_and_forgetting_on_postgres(database_temporaneo):  # noqa: F811
    from sqlmodel import Session

    from app.db import migra as m
    from app.routes.users import remove_user

    m.migra(database_temporaneo)
    with Session(database_temporaneo) as s:
        lei = make_user(s, email="Lei@x.it", last_seen_ip="192.0.2.9")
        vecchia = _voce(s, 120, actor_id=lei.id, actor_label="Lei@x.it")
        recente = _voce(s, 1)
        tentativo = _voce(s, 1, actor_label="lei@x.it")
        ip_retention.delete_addresses(s, 90, at=ORA)
        for v in (vecchia, recente):
            s.refresh(v)
        assert vecchia.ip is None and recente.ip == "203.0.113.7"
        remove_user(s, lei.id)
        s.refresh(tentativo)
        assert tentativo.ip is None and tentativo.actor_label == "lei@x.it"
