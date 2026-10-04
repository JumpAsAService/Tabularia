"""Una connessione al database si prende e si rende nello stesso passo.

FastAPI esegue ogni dipendenza sincrona e ogni rotta sincrona in un thread, uno
per passo, presi da un insieme limitato (40). Se un passo esce tenendosi la
connessione, la richiesta la tiene mentre aspetta un thread per il passo dopo —
e sotto una raffica quei thread sono tutti occupati da altre richieste che
aspettano una connessione (il pool ne ha 15). Nessuno dei due gruppi avanza
finché non scatta il timeout del pool.

Misurato su Postgres, rotte normali (`/flows`, `/projects`, `/datasources`,
`/auth/me`), richieste tutte insieme:

    20  → tutte 200 in 0,2 s
    60  → 32 errori 500, 30 s
    100 → 68 errori 500, 60 s
    200 → 160 errori 500, 120 s

Sessanta richieste insieme sono una decina di persone che aprono la stessa
pagina. Due regole lo tolgono, e questi test le tengono ferme:

  - le dipendenze di autenticazione chiudono la loro transazione prima di
    restituire l'utente;
  - la sessione della richiesta si chiude sul ciclo degli eventi, non in un
    thread: il rilascio finale non deve aspettare un thread libero.
"""
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.security import create_access_token
from app.db import session as db_session
from app.deps import auth
from tests.conftest import make_user


def _richiesta():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"))


def test_l_autenticazione_non_esce_con_una_transazione_aperta(session):
    from datetime import datetime, timezone

    # visto un attimo fa: `last_seen` non va aggiornato, quindi nessun commit
    # chiude la transazione per caso — è il caso di quasi tutte le richieste
    u = make_user(session, email="a@x.local", last_seen_at=datetime.now(timezone.utc))
    utente = auth.get_current_user(_richiesta(), token=create_access_token(u.id), session=session)
    assert not session.in_transaction()
    # l'utente resta agganciato alla sessione: chi lo legge dopo lo ritrova
    assert utente.email == "a@x.local" and utente.id == u.id


def test_nemmeno_quando_ha_appena_aggiornato_last_seen(session):
    u = make_user(session, email="b@x.local")  # last_seen_at nullo: l'aggiorna e fa commit
    auth.get_current_user(_richiesta(), token=create_access_token(u.id), session=session)
    assert not session.in_transaction()
    session.refresh(u)
    assert u.last_seen_at is not None


@pytest.mark.parametrize("guardia", ["require_superuser", "require_observer"])
def test_le_guardie_amministrative_non_escono_con_una_transazione_aperta(session, guardia):
    admin = make_user(session, email="capo@x.local", is_superuser=True)
    assert getattr(auth, guardia)(user=admin, session=session) is admin
    assert not session.in_transaction()


@pytest.mark.parametrize("guardia", ["require_superuser", "require_observer"])
def test_le_guardie_rifiutano_come_prima(session, guardia):
    normale = make_user(session, email="n@x.local")
    with pytest.raises(HTTPException) as e:
        getattr(auth, guardia)(user=normale, session=session)
    assert e.value.status_code == 403


def test_un_token_non_valido_e_respinto_come_prima(session):
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(_richiesta(), token="non-un-token", session=session)
    assert e.value.status_code == 401


@pytest.mark.anyio
async def test_la_sessione_della_richiesta_si_chiude_sul_ciclo_degli_eventi(db_engine, monkeypatch):
    """Generatore ASINCRONO: l'ingresso e l'uscita non passano da un thread."""
    assert inspect.isasyncgenfunction(db_session.get_session)
    monkeypatch.setattr(db_session, "engine", db_engine)
    gen = db_session.get_session()
    sessione = await gen.__anext__()
    from sqlmodel import select

    from app.models import User

    sessione.exec(select(User)).all()
    assert sessione.in_transaction()
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert not sessione.in_transaction()


# ── La rotta sincrona rilascia prima di uscire dal suo thread ────────────────
# Dopo la rotta FastAPI valida la risposta in UN ALTRO thread: se la rotta esce
# con la transazione aperta, la richiesta tiene la connessione mentre aspetta
# quel thread. Misurato dopo aver corretto l'autenticazione: 60 insieme a posto,
# 100 insieme → 37 errori e 30 secondi di blocco.
def test_a_fine_rotta_la_connessione_e_resa_e_il_risultato_resta_leggibile(session):
    from app.db.session import a_fine_rotta
    from app.models import Project

    @a_fine_rotta
    def rotta(session):
        p = Project(name="cartella")
        session.add(p)
        session.commit()  # l'oggetto è «scaduto»: leggerlo dopo richiede il database
        return p

    p = rotta(session=session)
    assert not session.in_transaction()
    # FastAPI lo leggerà nel thread successivo: deve bastare ciò che è in memoria
    assert p.name == "cartella" and p.id is not None
    assert not session.in_transaction()


def test_vale_anche_per_liste_e_dizionari_di_oggetti(session):
    from app.db.session import a_fine_rotta
    from app.models import Project

    @a_fine_rotta
    def rotta(session):
        a, b = Project(name="a"), Project(name="b")
        session.add(a); session.add(b)
        session.commit()
        return {"items": [a, b], "total": 2}

    r = rotta(session=session)
    assert [x.name for x in r["items"]] == ["a", "b"] and not session.in_transaction()


def test_un_errore_nella_rotta_rilascia_lo_stesso(session):
    from sqlmodel import select

    from app.db.session import a_fine_rotta
    from app.models import Project

    @a_fine_rotta
    def rotta(session):
        session.exec(select(Project)).all()
        raise HTTPException(status_code=404, detail="non c'è")

    with pytest.raises(HTTPException):
        rotta(session=session)
    assert not session.in_transaction()


def test_ogni_router_del_gateway_rilascia_a_fine_rotta():
    """Un router nuovo non deve potersene dimenticare: ogni `APIRouter` dichiara
    la classe di rotta che avvolge le rotte sincrone. Le asincrone restano fuori
    (non passano da un thread per rotta)."""
    import asyncio
    import importlib
    import pkgutil

    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    import app.main  # noqa: F401 — carica tutti i router
    import app.routes as pacchetto
    from app.core.routing import RottaCheRilascia

    sincrone = asincrone = 0
    for info in pkgutil.iter_modules(pacchetto.__path__):
        modulo = importlib.import_module(f"app.routes.{info.name}")
        router = getattr(modulo, "router", None)
        if not isinstance(router, APIRouter):
            continue
        assert router.route_class is RottaCheRilascia, info.name
        for rotta in router.routes:
            if not isinstance(rotta, APIRoute):
                continue
            avvolta = getattr(rotta.endpoint, "rilascia_a_fine_rotta", False)
            if asyncio.iscoroutinefunction(rotta.endpoint):
                asincrone += 1
                assert not avvolta, rotta.path
            else:
                sincrone += 1
                assert avvolta, rotta.path
    assert sincrone > 50 and asincrone > 10
