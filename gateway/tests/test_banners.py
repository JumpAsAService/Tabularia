"""Banner di avvertimento dell'Explore: creazione, elenco, eliminazione.

Il contratto che conta: li LEGGONO tutti (servono a essere visti), li SCRIVE solo
un superuser, e ogni modifica lascia traccia nell'audit — perché cambia ciò che
vede l'intera installazione.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.deps.auth import require_superuser
from app.models import AuditLog, Banner
from app.routes.banners import create_banner, delete_banner, list_banners
from app.schemas.models import BannerCreate
from app.services import audit
from tests.conftest import make_user


def _admin(session):
    return make_user(session, email="admin@x.local", is_superuser=True)


def _azioni(session) -> list[str]:
    return [r.action for r in session.exec(select(AuditLog)).all()]


# ── creazione ───────────────────────────────────────────────────────────────
def test_create_shows_up_in_the_list(session):
    admin = _admin(session)
    create_banner(body=BannerCreate(message="Manutenzione alle 18:00"), user=admin, session=session)

    banners = list_banners(session=session)
    assert len(banners) == 1
    assert banners[0].message == "Manutenzione alle 18:00"
    assert banners[0].level == "warning"  # default: è un avvertimento
    assert banners[0].created_by == admin.id


def test_level_is_kept(session):
    admin = _admin(session)
    for livello in ("info", "warning", "danger"):
        create_banner(body=BannerCreate(message=f"m-{livello}", level=livello), user=admin, session=session)
    assert {b.level for b in list_banners(session=session)} == {"info", "warning", "danger"}


def test_message_is_trimmed_and_blank_is_refused(session):
    admin = _admin(session)
    create_banner(body=BannerCreate(message="   con spazi   "), user=admin, session=session)
    assert list_banners(session=session)[0].message == "con spazi"

    with pytest.raises(HTTPException) as e:
        create_banner(body=BannerCreate(message="    "), user=admin, session=session)
    assert e.value.status_code == 422


def test_list_is_newest_first(session):
    admin = _admin(session)
    for m in ("primo", "secondo", "terzo"):
        create_banner(body=BannerCreate(message=m), user=admin, session=session)
    assert [b.message for b in list_banners(session=session)] == ["terzo", "secondo", "primo"]


# ── eliminazione ────────────────────────────────────────────────────────────
def test_delete_removes_it(session):
    admin = _admin(session)
    b = create_banner(body=BannerCreate(message="da togliere"), user=admin, session=session)
    delete_banner(banner_id=b.id, user=admin, session=session)
    assert list_banners(session=session) == []
    assert session.exec(select(Banner)).all() == []


def test_delete_missing_is_404(session):
    admin = _admin(session)
    with pytest.raises(HTTPException) as e:
        delete_banner(banner_id=999, user=admin, session=session)
    assert e.value.status_code == 404


# ── chi può scrivere ────────────────────────────────────────────────────────
def test_only_superusers_may_write(session):
    normale = make_user(session, email="tizio@x.local", is_superuser=False)
    with pytest.raises(HTTPException) as e:
        require_superuser(user=normale)
    assert e.value.status_code == 403
    # il superuser passa la stessa guardia
    assert require_superuser(user=_admin(session)) is not None


# ── audit ───────────────────────────────────────────────────────────────────
def test_create_and_delete_are_audited(session):
    admin = _admin(session)
    b = create_banner(body=BannerCreate(message="tracciami"), user=admin, session=session)
    assert audit.BANNER_CREATE in _azioni(session)

    delete_banner(banner_id=b.id, user=admin, session=session)
    assert audit.BANNER_DELETE in _azioni(session)
