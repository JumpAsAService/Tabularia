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


def _spia(engine):
    """Conta le connessioni uscite dal pool e non rese, e i comandi mandati al database."""
    from sqlalchemy import event

    stato = SimpleNamespace(in_mano=0, prese=0, comandi=[])

    @event.listens_for(engine, "checkout")
    def _presa(*_):
        stato.in_mano += 1
        stato.prese += 1

    @event.listens_for(engine, "checkin")
    def _resa(*_):
        stato.in_mano -= 1

    @event.listens_for(engine, "before_cursor_execute")
    def _comando(conn, cursor, statement, *_):
        stato.comandi.append(statement)

    return stato


def test_l_autenticazione_non_esce_con_una_connessione_in_mano(db_engine, session):
    from datetime import datetime, timezone

    # visto un attimo fa: `last_seen` non va aggiornato, quindi nessun commit
    # rilascia la connessione per caso — è il caso di quasi tutte le richieste
    u = make_user(session, email="a@x.local", last_seen_at=datetime.now(timezone.utc))
    spia = _spia(db_engine)
    utente = auth.get_current_user(_richiesta(), token=create_access_token(u.id), session=session)
    assert spia.prese == 1 and spia.in_mano == 0
    # l'utente resta agganciato alla sessione: chi lo legge dopo lo ritrova
    assert utente in session
    assert utente.email == "a@x.local" and utente.id == u.id


def test_l_autenticazione_costa_una_lettura_e_la_rotta_non_rilegge_l_utente(db_engine):
    """Misurato sullo stack vero prima di questa regola: 4 comandi per leggere
    l'utente (ping, BEGIN, SELECT, ROLLBACK) e una seconda SELECT identica nella
    rotta, perché il rollback di fine passo lo aveva fatto scadere."""
    from datetime import datetime, timezone

    from sqlalchemy import inspect as sa_inspect
    from sqlmodel import Session

    with Session(db_engine) as s:
        uid = make_user(s, email="c@x.local", last_seen_at=datetime.now(timezone.utc)).id
    spia = _spia(db_engine)
    with Session(db_engine) as richiesta:  # la sessione nuova che ogni richiesta riceve
        utente = auth.get_current_user(_richiesta(), token=create_access_token(uid), session=richiesta)
        assert len(spia.comandi) == 1 and "FROM users" in spia.comandi[0]
        # quello che la rotta fa per prima cosa: leggerne i campi
        assert not sa_inspect(utente).expired
        assert (utente.id, utente.email, utente.is_active, utente.is_superuser) == (uid, "c@x.local", True, False)
        assert len(spia.comandi) == 1 and spia.in_mano == 0


def test_un_utente_disattivato_o_sparito_e_respinto_come_prima(db_engine):
    from sqlmodel import Session

    with Session(db_engine) as s:
        spento = make_user(s, email="spento@x.local", is_active=False).id
    for uid in (spento, 99999):
        with Session(db_engine) as richiesta, pytest.raises(HTTPException) as e:
            auth.get_current_user(_richiesta(), token=create_access_token(uid), session=richiesta)
        assert e.value.status_code == 401


def test_l_utente_caricato_si_puo_modificare_e_salvare_dalla_rotta(db_engine):
    """La rotta lo riceve agganciato alla SUA sessione: cambiarlo e fare commit
    scrive, come quando lo leggeva la sessione stessa."""
    from datetime import datetime, timezone

    from sqlmodel import Session

    from app.models import User

    with Session(db_engine) as s:
        uid = make_user(s, email="d@x.local", last_seen_at=datetime.now(timezone.utc)).id
    with Session(db_engine) as richiesta:
        utente = auth.get_current_user(_richiesta(), token=create_access_token(uid), session=richiesta)
        utente.full_name = "Nome Nuovo"
        richiesta.add(utente)
        richiesta.commit()
    with Session(db_engine) as s:
        assert s.get(User, uid).full_name == "Nome Nuovo"


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


# ── Meno giri verso Postgres per richiesta ───────────────────────────────────
# Il profilo sotto carico (250 utenti): quasi metà del tempo in cui il gateway
# lavorava era attesa su Postgres per cose evitabili — il ping prima di ogni uso
# di una connessione, connessioni aperte e chiuse a ogni richiesta perché il pool
# era di 5, la stessa tabella riletta tre volte.
def test_il_pool_e_a_misura_dei_thread():
    from app.core.config import get_settings

    s = get_settings()
    assert s.db.pool_size is None  # vuoto = uno per thread
    assert db_session.engine.pool.size() == s.app.gateway_threads


def test_le_letture_dell_utente_hanno_connessioni_loro_sempre_in_autocommit(db_engine):
    letture = db_session.engine_di_lettura(db_session.engine)
    assert letture is not db_session.engine and letture.pool is not db_session.engine.pool
    assert letture.url == db_session.engine.url
    assert letture.dialect._on_connect_isolation_level == "AUTOCOMMIT"
    # poche, e mai una in più: chi non ne trova aspetta la SELECT di un altro
    assert 2 <= letture.pool.size() <= db_session.engine.pool.size() // 2
    assert letture.pool._max_overflow == 0
    # un engine qualunque (quello dei test): stesso pool, autocommit per l'occasione
    assert db_session.engine_di_lettura(db_engine).pool is db_engine.pool


def _engine_su_file(tmp_path, ferma_da_s):
    from sqlalchemy import QueuePool
    from sqlmodel import create_engine

    eng = create_engine(f"sqlite:///{tmp_path}/ping.db", poolclass=QueuePool)
    db_session.ping_solo_se_ferma(eng, ferma_da_s=ferma_da_s)
    ping = []
    vero = eng.dialect.do_ping
    eng.dialect.do_ping = lambda c: (ping.append(c), vero(c))[1]
    return eng, ping


def test_una_connessione_appena_usata_non_viene_provata(tmp_path):
    eng, ping = _engine_su_file(tmp_path, ferma_da_s=60)
    for _ in range(5):
        with eng.connect() as c:
            c.exec_driver_sql("select 1")
    assert ping == []


def test_una_connessione_rimasta_ferma_viene_provata(tmp_path):
    eng, ping = _engine_su_file(tmp_path, ferma_da_s=0)
    for _ in range(3):
        with eng.connect() as c:
            c.exec_driver_sql("select 1")
    assert len(ping) == 3


def test_se_la_connessione_ferma_e_morta_chi_la_chiede_ne_riceve_una_nuova(tmp_path):
    from sqlalchemy import event

    eng, _ = _engine_su_file(tmp_path, ferma_da_s=0)
    aperte = []
    event.listen(eng, "connect", lambda dbapi, record: aperte.append(dbapi))
    with eng.connect() as c:
        c.exec_driver_sql("select 1")
    assert len(aperte) == 1
    morte = {id(aperte[0])}

    def do_ping(dbapi_conn):  # la prima connessione non risponde più
        if id(dbapi_conn) in morte:
            raise OSError("server closed the connection unexpectedly")
        return True

    eng.dialect.do_ping = do_ping
    with eng.connect() as c:
        assert c.exec_driver_sql("select 41 + 1").scalar() == 42
    assert len(aperte) == 2


def test_l_elenco_delle_cartelle_legge_l_albero_una_volta_sola(db_engine, session):
    from app.models import Permission, Project
    from app.services import permissions

    u = make_user(session, email="e@x.local")
    radice = Project(name="radice", owner_id=u.id)
    altra = Project(name="altra", owner_id=u.id)
    session.add(radice); session.add(altra); session.commit()
    figlia = Project(name="figlia", parent_id=radice.id, owner_id=u.id)
    session.add(figlia); session.commit()
    session.add(Permission(project_id=figlia.id, user_id=u.id, capability="view")); session.commit()

    attese = permissions.visible_project_ids(session, u)
    assert attese == {radice.id, figlia.id}  # la figlia, e la radice per arrivarci
    spia = _spia(db_engine)
    viste = permissions.visible_projects(session, u)
    assert {p.id for p in viste} == attese
    assert sum("FROM projects" in c for c in spia.comandi) == 1
