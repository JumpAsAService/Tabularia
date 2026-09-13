"""Guardie del proxy sul corpo inoltrato all'engine.

Destinazioni (tabella di database, oggetto S3) e copia su S3 esterno passano
SOLO da `/flows/{id}/runs`, dove è il GATEWAY a costruire il payload della
connessione: capability CONNECT verificata sul progetto, secret presa dal
catalogo e cifrata, riga `Run` in cronologia. Accettarle qui salterebbe tutto
in una volta — un utente qualsiasi potrebbe indicare endpoint e credenziali
PROPRIE, in chiaro, e farsi consegnare il parquet dal worker senza lasciare
traccia.

Regressione vera: `mirror` era stato aggiunto come campo fratello di
`destination` senza estendere questa guardia, che fino ad allora non era
coperta da alcun test. Il parametrizzato copre tutti i nomi della famiglia
proprio perché il prossimo campo non ripeta la storia — `email` è stato
aggiunto qui nello stesso commit che lo ha introdotto.

Per `email` la posta in gioco è più alta: oltre a catalogo, CONNECT e cifratura,
saltare questa guardia salterebbe la validazione dei domini ammessi, cioè
l'unica barriera fra un flusso e l'invio di dati a un indirizzo arbitrario.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routes.proxy import transform

pytestmark = pytest.mark.anyio

_UTENTE = SimpleNamespace(id=1, is_superuser=False)

# payload che porta una connessione dell'attaccante: endpoint arbitrario
# (anche interno, es. l'endpoint metadata del cloud) e secret IN CHIARO
_CONNESSIONE_OSTILE = {
    "connection": {"endpoint_url": "http://169.254.169.254", "access_key": "k", "secret_key": "in-chiaro"},
    "target": {"bucket": "bucket-attaccante", "key": "esfiltrato.parquet"},
}


def _richiesta(body: dict) -> Request:
    """Request minima: `_read_json` legge gli header e poi il corpo a chunk."""
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/tasks/transform-data",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


@pytest.mark.parametrize("campo", ["destination", "db_destination", "mirror", "email"])
async def test_destinazioni_rifiutate_dal_proxy(campo):
    body = {
        "bucket": "data-prep",
        "input_key": "datasets/x.parquet",
        "operations": [],
        campo: _CONNESSIONE_OSTILE,
    }
    with pytest.raises(HTTPException) as e:
        # session=None basta: la guardia solleva PRIMA di toccare il database
        await transform(_richiesta(body), user=_UTENTE, session=None)
    assert e.value.status_code == 422
    assert "Destinazione non consentita" in str(e.value.detail)


async def test_corpo_non_oggetto_rifiutato():
    """Una lista al posto di un oggetto non deve arrivare all'engine."""
    with pytest.raises(HTTPException) as e:
        await transform(_richiesta([1, 2, 3]), user=_UTENTE, session=None)  # type: ignore[arg-type]
    assert e.value.status_code == 422
