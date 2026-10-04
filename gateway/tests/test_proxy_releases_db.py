"""Mentre il gateway aspetta l'engine, non deve tenersi una connessione al database.

Ogni rotta inoltrata all'engine passa dall'autenticazione e dai controlli sui
permessi, che leggono dal database e aprono una transazione. Se la transazione
resta aperta durante l'attesa — fino a due minuti per un'anteprima, tutta la
durata per un upload o un export — quella richiesta occupa una connessione del
pool senza usarla. Il pool ne ha quindici: quindici anteprime lente insieme
lasciavano senza database tutte le altre richieste, accesso compreso.

Misurato su Postgres con venti anteprime da 6 secondi: 15 connessioni occupate,
una richiesta qualunque ferma 4,8 secondi, le anteprime finite in 12 secondi
invece di 6.

Qui si guarda la causa, non il sintomo: nel momento in cui parte la chiamata
all'engine, la sessione non deve avere una transazione aperta.
"""
import json

import httpx
import pytest
from starlette.requests import Request

from app.models import User
from app.routes import proxy
from tests.conftest import make_user

pytestmark = pytest.mark.anyio


def _richiesta(body: dict | None = None, method: str = "POST", path: str = "/x") -> Request:
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "method": method, "path": path, "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())],
        "client": ("203.0.113.9", 4242),
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def engine_che_guarda(session, monkeypatch):
    """Un engine finto che, quando viene chiamato, annota se la sessione del
    gateway ha una transazione aperta in quel momento."""
    visto: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visto.append((request.url.path, session.in_transaction()))
        if request.url.path == "/engines":
            return httpx.Response(200, json=[{"id": "polars", "available": True}])
        return httpx.Response(200, json={"dataset_id": "d1", "parquet_key": "datasets/d1.parquet", "raw_key": "raw/d1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)
    return visto


@pytest.fixture
def admin(session) -> User:
    return make_user(session, email="capo@x.local", is_superuser=True)


def _bucket() -> str:
    from app.core.config import get_settings

    return get_settings().engine.bucket


def _corpo(**extra) -> dict:
    return {"bucket": _bucket(), "input_key": "datasets/prova.parquet", "operations": [], "limit": 10, **extra}


def _come_dopo_l_autenticazione(session, user) -> None:
    """Ciò che lascia `get_current_user`: l'utente letto, la transazione aperta."""
    session.get(User, user.id)
    assert session.in_transaction()


async def test_l_anteprima_non_tiene_la_connessione_mentre_aspetta(session, admin, engine_che_guarda):
    await proxy.preview(_richiesta(_corpo()), user=admin, session=session)
    assert engine_che_guarda == [("/tasks/preview", False)]


async def test_l_export_non_tiene_la_connessione_mentre_aspetta(session, admin, engine_che_guarda):
    await proxy.export(_richiesta(_corpo(format="csv", filename="x")), user=admin, session=session)
    assert engine_che_guarda == [("/tasks/export", False)]


async def test_il_run_senza_flusso_non_tiene_la_connessione_mentre_aspetta(session, admin, engine_che_guarda):
    await proxy.transform(_richiesta(_corpo()), user=admin, session=session)
    assert engine_che_guarda == [("/tasks/transform-data", False)]


async def test_l_upload_non_tiene_la_connessione_e_registra_comunque_il_proprietario(session, admin, engine_che_guarda):
    from sqlmodel import select

    from app.models import Upload

    await proxy.upload(_richiesta({}, path="/files"), user=admin, session=session)
    assert engine_che_guarda == [("/files", False)]
    # dopo la risposta dell'engine la sessione serve ancora: deve funzionare
    caricati = session.exec(select(Upload)).all()
    assert [(u.dataset_id, u.owner_id) for u in caricati] == [("d1", admin.id)]


@pytest.mark.parametrize("rotta, percorso", [("operations", "/tasks/operations"), ("engines", "/engines")])
async def test_le_rotte_brevi_rilasciano_anche_loro(session, admin, engine_che_guarda, rotta, percorso):
    _come_dopo_l_autenticazione(session, admin)
    await getattr(proxy, rotta)(_richiesta(method="GET"), session=session)
    assert engine_che_guarda == [(percorso, False)]


async def test_lo_stato_di_un_task_non_tiene_la_connessione(session, admin, engine_che_guarda):
    _come_dopo_l_autenticazione(session, admin)
    await proxy.task_status(_richiesta(method="GET"), task_id="t1", session=session)
    assert engine_che_guarda == [("/tasks/t1", False)]


async def test_l_utente_resta_utilizzabile_dopo_il_rilascio(session, admin, engine_che_guarda):
    """Rilasciare non deve lasciare oggetti inservibili a chi viene dopo: le
    rotte usano ancora l'utente (proprietario dell'upload, audit)."""
    await proxy.preview(_richiesta(_corpo()), user=admin, session=session)
    assert admin.email == "capo@x.local" and admin.id is not None


async def test_anche_se_un_commit_precedente_aveva_fatto_scadere_l_utente(session, admin, engine_che_guarda):
    """È il caso di chi arriva per primo dopo un minuto: l'autenticazione
    aggiorna `last_seen` e fa commit, e gli attributi dell'utente scadono. Dopo
    il rilascio l'utente è staccato dalla sessione: se non fosse stato ricaricato
    prima, leggerne l'id solleverebbe un errore."""
    session.commit()  # ciò che fa `_touch_last_seen`
    await proxy.upload(_richiesta({}, path="/files"), user=admin, session=session)
    assert engine_che_guarda == [("/files", False)]
    assert admin.id is not None and admin.email == "capo@x.local"


@pytest.mark.parametrize("rotta", ["preview", "export", "transform"])
async def test_i_controlli_sul_database_non_girano_sul_ciclo_degli_eventi(session, admin, engine_che_guarda, monkeypatch, rotta):
    """Le query sono sincrone: sul ciclo degli eventi lo bloccherebbero, e con il
    pool esaurito lo fermerebbero ad aspettare una connessione che può liberarsi
    solo se LUI va avanti. Osservato dal vivo: gateway immobile per 30 secondi.
    I controlli devono quindi girare in un thread."""
    import threading

    ciclo = threading.get_ident()
    dove: list[int] = []
    vero = proxy.ensure_reads_pinned

    def spia(*a, **k):
        dove.append(threading.get_ident())
        return vero(*a, **k)

    monkeypatch.setattr(proxy, "ensure_reads_pinned", spia)
    corpo = _corpo(format="csv", filename="x") if rotta == "export" else _corpo()
    await getattr(proxy, rotta)(_richiesta(corpo), user=admin, session=session)
    assert dove and all(t != ciclo for t in dove)


def test_l_autenticazione_del_proxy_non_lascia_la_connessione_in_mano(session, admin):
    """L'autenticazione gira in un thread e i controlli in un altro: una
    richiesta che fra i due si tenesse la connessione aspetterebbe un thread
    tenendo ferma una risorsa che altri thread stanno aspettando. Con 60
    anteprime insieme: gateway fermo 28 secondi. Deve prendere e rendere nello
    stesso passo, e restituire un utente leggibile senza database."""
    from app.core.security import create_access_token

    utente = proxy._utente(_richiesta(method="GET"), token=create_access_token(admin.id), session=session)
    assert not session.in_transaction()
    assert (utente.id, utente.email, utente.is_superuser) == (admin.id, "capo@x.local", True)


def test_ogni_rotta_del_proxy_passa_da_quella_autenticazione():
    """Una rotta aggiunta domani con `get_current_user` riaprirebbe lo stallo
    senza che niente lo dica: l'autenticazione è del router, tutta intera."""
    from app.deps.auth import get_current_user

    for rotta in proxy.router.routes:
        chiamate = {d.call for d in rotta.dependant.dependencies}
        assert proxy._utente in chiamate, rotta.path
        assert get_current_user not in chiamate, rotta.path
