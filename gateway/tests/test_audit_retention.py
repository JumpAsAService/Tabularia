"""La scadenza del registro: spenta di default, precisa quando è accesa."""
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.models import AuditLog
from app.services.audit_retention import purga


def _voce(session, minuti_fa: int, azione: str = "flow.run") -> AuditLog:
    ora = datetime.now(timezone.utc).replace(tzinfo=None)
    v = AuditLog(action=azione, actor_label="tizio@x.it", outcome="success",
                 ts=ora - timedelta(minutes=minuti_fa))
    session.add(v)
    session.commit()
    return v


def test_without_a_window_nothing_is_deleted(session):
    """Il default è «per sempre»: cancellare un registro di sicurezza non può
    succedere a qualcuno senza che l'abbia chiesto."""
    _voce(session, 60 * 24 * 365)
    assert purga(session, 0) == 0
    assert len(session.exec(select(AuditLog)).all()) == 1


def test_only_what_is_past_the_window_goes(session):
    _voce(session, 120, "vecchia")
    _voce(session, 61, "appena oltre")
    _voce(session, 59, "appena dentro")
    _voce(session, 1, "recente")

    assert purga(session, 60) == 2
    rimaste = sorted(a.action for a in session.exec(select(AuditLog)).all())
    assert rimaste == ["appena dentro", "recente"]


def test_the_edge_is_kept_not_dropped(session):
    """Alla soglia esatta la voce RESTA: si cancella ciò che è più vecchio della
    finestra, non ciò che la tocca."""
    ora = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add(AuditLog(action="al limite", actor_label="x", outcome="success",
                         ts=ora - timedelta(minutes=30)))
    session.commit()
    assert purga(session, 30, ora=ora) == 0
    assert len(session.exec(select(AuditLog)).all()) == 1


def test_it_empties_a_backlog_bigger_than_one_batch(session):
    """Un primo passaggio su un registro vecchio non deve fermarsi al primo lotto."""
    ora = datetime.now(timezone.utc).replace(tzinfo=None)
    session.add_all([
        AuditLog(action="vecchia", actor_label="x", outcome="success",
                 ts=ora - timedelta(minutes=120))
        for _ in range(120)
    ])
    session.commit()
    assert purga(session, 60) == 120
    assert session.exec(select(AuditLog)).all() == []
