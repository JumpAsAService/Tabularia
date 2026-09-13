"""La colonna `connections.extra`: opzioni del tipo che non hanno una colonna.

Serve solo a SMTP (mittente, modalità TLS, domini ammessi) e nasce da un vincolo
preciso: senza un giro completo scrittura→rilettura, una connessione SMTP si può
creare ma non configurare, e il nodo email non parte mai. Quindi qui si prova il
giro, non il singolo pezzo:

  form → ConnectionCreate.extra → colonna → ConnectionOut.extra → form
                                     └→ allowed_email_domains / payload engine

L'altra metà è il momento in cui un JSON rotto viene rifiutato: al salvataggio,
da chi lo sta scrivendo, e non giorni dopo a chi si chiede perché l'email non è
arrivata.
"""
import json

import pytest
from fastapi import HTTPException

from app.models import Connection
from app.routes.connections import (
    allowed_email_domains,
    create_connection,
    engine_connection_payload,
    list_all_connections,
    update_connection,
)
from app.schemas.models import ConnectionCreate, ConnectionUpdate
from tests.conftest import make_project, make_user

OPZIONI = {
    "from_address": "report@azienda.it",
    "from_name": "Report Tabularia",
    "tls": "ssl",
    "allowed_domains": ["azienda.it", "clienti.it"],
}


def _crea(session, user, project_id, *, extra=None, name="posta"):
    body = ConnectionCreate(
        name=name,
        db_type="smtp",
        host="smtp.azienda.it",
        port=465,
        username="report",
        password="segreta",
        **({"extra": json.dumps(extra)} if extra is not None else {}),
    )
    return create_connection(project_id, body, request=None, user=user, session=session)


@pytest.fixture
def scena(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    return admin, make_project(session, name="p")


# ── 1. il giro completo ─────────────────────────────────────────────────────
def test_the_options_survive_the_round_trip_to_the_form(session, scena):
    """Senza questo, il form di modifica si riaprirebbe vuoto e il primo
    salvataggio cancellerebbe mittente, TLS e domini."""
    admin, p = scena
    creata = _crea(session, admin, p.id, extra=OPZIONI)
    assert json.loads(creata.extra) == OPZIONI

    riletta = [c for c in list_all_connections(user=admin, session=session) if c.id == creata.id][0]
    assert json.loads(riletta.extra) == OPZIONI


def test_the_saved_options_are_the_ones_that_limit_the_recipients(session, scena):
    """Il punto della colonna: i domini scritti nel form devono essere quelli che
    il gateway applica al lancio, non una copia che vive altrove."""
    admin, p = scena
    creata = _crea(session, admin, p.id, extra=OPZIONI)

    riga = session.get(Connection, creata.id)
    assert allowed_email_domains(riga) == ["azienda.it", "clienti.it"]

    payload = engine_connection_payload(riga)
    assert payload["from_address"] == "report@azienda.it"
    assert payload["from_name"] == "Report Tabularia"
    assert payload["tls"] == "ssl"


def test_editing_them_changes_what_the_send_will_do(session, scena):
    admin, p = scena
    creata = _crea(session, admin, p.id, extra=OPZIONI)

    nuove = dict(OPZIONI, allowed_domains=["altra.it"], tls="starttls")
    aggiornata = update_connection(
        creata.id, ConnectionUpdate(extra=json.dumps(nuove)), request=None, user=admin, session=session
    )
    assert json.loads(aggiornata.extra)["allowed_domains"] == ["altra.it"]
    assert allowed_email_domains(session.get(Connection, creata.id)) == ["altra.it"]


def test_a_rename_does_not_wipe_the_options(session, scena):
    """`extra` è opzionale in PATCH: ometterlo deve voler dire «non toccarlo»,
    altrimenti rinominare una connessione la disattiverebbe in silenzio."""
    admin, p = scena
    creata = _crea(session, admin, p.id, extra=OPZIONI)

    update_connection(
        creata.id, ConnectionUpdate(name="posta-rinominata"), request=None, user=admin, session=session
    )
    assert allowed_email_domains(session.get(Connection, creata.id)) == ["azienda.it", "clienti.it"]


# ── 2. il rifiuto avviene al salvataggio ────────────────────────────────────
def test_broken_json_is_refused_when_it_is_saved(session, scena):
    admin, p = scena
    body = ConnectionCreate(name="rotta", db_type="smtp", host="h", extra="{non chiuso")
    with pytest.raises(HTTPException) as e:
        create_connection(p.id, body, request=None, user=admin, session=session)
    assert e.value.status_code == 422
    assert session.get(Connection, 1) is None, "niente riga a metà"


def test_json_that_is_not_an_object_is_refused(session, scena):
    """Un elenco passerebbe json.loads ma poi `smtp_options` lo scarterebbe e la
    configurazione sparirebbe senza che nessuno lo dica."""
    admin, p = scena
    with pytest.raises(HTTPException) as e:
        create_connection(
            p.id, ConnectionCreate(name="lista", db_type="smtp", host="h", extra='["azienda.it"]'),
            request=None, user=admin, session=session,
        )
    assert e.value.status_code == 422


def test_broken_json_is_refused_on_update_too(session, scena):
    admin, p = scena
    creata = _crea(session, admin, p.id, extra=OPZIONI)
    with pytest.raises(HTTPException) as e:
        update_connection(creata.id, ConnectionUpdate(extra="{rotto"), request=None, user=admin, session=session)
    assert e.value.status_code == 422
    assert allowed_email_domains(session.get(Connection, creata.id)) == ["azienda.it", "clienti.it"]


# ── 3. gli altri tipi e i segreti ───────────────────────────────────────────
def test_a_connection_without_options_gets_an_empty_object(session, scena):
    """Gli altri tipi non usano `extra`: non devono vedersi né un null né un
    errore, e `smtp_options` deve poterlo leggere sempre."""
    admin, p = scena
    creata = create_connection(
        p.id, ConnectionCreate(name="pg", db_type="postgresql", host="pg", database="shop"),
        request=None, user=admin, session=session,
    )
    assert creata.extra == "{}"
    assert allowed_email_domains(session.get(Connection, creata.id)) == []


def test_the_options_come_back_but_the_password_never_does(session, scena):
    """`extra` può tornare al client proprio perché non contiene segreti: se un
    giorno ne contenesse, questo test è il posto in cui accorgersene."""
    admin, p = scena
    creata = _crea(session, admin, p.id, extra=OPZIONI)
    campi = creata.model_dump()
    assert "password" not in campi and "password_encrypted" not in campi
    assert campi["has_password"] is True
    assert "segreta" not in json.dumps(campi, default=str)  # default=str: c'è updated_at
