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
    created = _crea(session, admin, p.id, extra=OPZIONI)
    assert json.loads(created.extra) == OPZIONI

    riletta = [c for c in list_all_connections(user=admin, session=session) if c.id == created.id][0]
    assert json.loads(riletta.extra) == OPZIONI


def test_the_saved_options_are_the_ones_that_limit_the_recipients(session, scena):
    """Il punto della colonna: i domini scritti nel form devono essere quelli che
    il gateway applica al lancio, non una copia che vive altrove."""
    admin, p = scena
    created = _crea(session, admin, p.id, extra=OPZIONI)

    row = session.get(Connection, created.id)
    assert allowed_email_domains(row) == ["azienda.it", "clienti.it"]

    payload = engine_connection_payload(row)
    assert payload["from_address"] == "report@azienda.it"
    assert payload["from_name"] == "Report Tabularia"
    assert payload["tls"] == "ssl"


def test_editing_them_changes_what_the_send_will_do(session, scena):
    admin, p = scena
    created = _crea(session, admin, p.id, extra=OPZIONI)

    new_ones = dict(OPZIONI, allowed_domains=["altra.it"], tls="starttls")
    aggiornata = update_connection(
        created.id, ConnectionUpdate(extra=json.dumps(new_ones)), request=None, user=admin, session=session
    )
    assert json.loads(aggiornata.extra)["allowed_domains"] == ["altra.it"]
    assert allowed_email_domains(session.get(Connection, created.id)) == ["altra.it"]


def test_a_rename_does_not_wipe_the_options(session, scena):
    """`extra` è opzionale in PATCH: ometterlo deve voler dire «non toccarlo»,
    altrimenti rinominare una connessione la disattiverebbe in silenzio."""
    admin, p = scena
    created = _crea(session, admin, p.id, extra=OPZIONI)

    update_connection(
        created.id, ConnectionUpdate(name="posta-rinominata"), request=None, user=admin, session=session
    )
    assert allowed_email_domains(session.get(Connection, created.id)) == ["azienda.it", "clienti.it"]


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
    created = _crea(session, admin, p.id, extra=OPZIONI)
    with pytest.raises(HTTPException) as e:
        update_connection(created.id, ConnectionUpdate(extra="{rotto"), request=None, user=admin, session=session)
    assert e.value.status_code == 422
    assert allowed_email_domains(session.get(Connection, created.id)) == ["azienda.it", "clienti.it"]


# ── 3. gli altri tipi e i segreti ───────────────────────────────────────────
def test_a_connection_without_options_gets_an_empty_object(session, scena):
    """Gli altri tipi non usano `extra`: non devono vedersi né un null né un
    errore, e `smtp_options` deve poterlo leggere sempre."""
    admin, p = scena
    created = create_connection(
        p.id, ConnectionCreate(name="pg", db_type="postgresql", host="pg", database="shop"),
        request=None, user=admin, session=session,
    )
    assert created.extra == "{}"
    assert allowed_email_domains(session.get(Connection, created.id)) == []


def test_the_options_come_back_but_the_password_never_does(session, scena):
    """`extra` può tornare al client proprio perché non contiene segreti: se un
    giorno ne contenesse, questo test è il posto in cui accorgersene."""
    admin, p = scena
    created = _crea(session, admin, p.id, extra=OPZIONI)
    fields = created.model_dump()
    assert "password" not in fields and "password_encrypted" not in fields
    assert fields["has_password"] is True
    assert "segreta" not in json.dumps(fields, default=str)  # default=str: c'è updated_at


# ── SQL Server: il TLS ──────────────────────────────────────────────────────
# `extra.tls` = verify (default: certificato verificato) | trust (autofirmato) | none.
def _sqlserver(session, user, project_id, *, extra=None, name="vendite"):
    body = ConnectionCreate(name=name, db_type="sqlserver", host="sql.x.it", username="u", password="p", database="vendite",
                            **({"extra": json.dumps(extra)} if extra is not None else {}))
    return create_connection(project_id, body, request=None, user=user, session=session)


def test_sqlserver_il_tls_arriva_all_engine_e_di_default_verifica(session, scena):
    admin, p = scena
    con = _sqlserver(session, admin, p.id, extra={"tls": "trust"})
    senza = _sqlserver(session, admin, p.id, name="vendite2")
    assert engine_connection_payload(session.get(Connection, con.id))["extra"] == {"tls": "trust"}
    assert engine_connection_payload(session.get(Connection, senza.id))["extra"] == {"tls": "verify"}


def test_sqlserver_un_tls_sconosciuto_si_rifiuta_al_salvataggio(session, scena):
    admin, p = scena
    with pytest.raises(HTTPException) as e:
        _sqlserver(session, admin, p.id, extra={"tls": "forse"})
    assert e.value.status_code == 422 and "verify, trust or none" in e.value.detail
    created = _sqlserver(session, admin, p.id)
    with pytest.raises(HTTPException):
        update_connection(created.id, ConnectionUpdate(extra=json.dumps({"tls": "forse"})), request=None, user=admin, session=session)


@pytest.mark.anyio
async def test_sqlserver_la_prova_del_form_porta_il_tls(session, scena, monkeypatch):
    """Il pulsante «Test» prova la connessione PRIMA del salvataggio: senza il TLS
    un server col certificato autofirmato fallirebbe anche con «trust» scelto."""
    import app.routes.connections as rc

    admin, p = scena
    seen = {}

    async def finto(payload, action, path="", **kw):  # kw: the language of the messages
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(rc, "_engine_inspect", finto)
    body = ConnectionCreate(name="x", db_type="sqlserver", host="h", username="u", password="p", database="d", extra=json.dumps({"tls": "trust"}))
    await rc.test_draft_connection(p.id, body, user=admin, session=session)
    assert seen["extra"] == {"tls": "trust"}
