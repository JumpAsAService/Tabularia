"""The proxy's guards on the body forwarded to the engine.

Destinations (database table, S3 object) and the copy to an external S3 go ONLY
through `/flows/{id}/runs`, where the GATEWAY builds the connection payload: the
CONNECT capability checked on the project, the secret taken from the catalog and
encrypted, a `Run` row in the history. Accepting them here would skip it all at once —
any user could point to THEIR OWN endpoint and credentials, in clear, and get the
parquet delivered by the worker without leaving a trace.

A real regression: `mirror` had been added as a sibling field of `destination` without
extending this guard, which until then no test covered. The parametrized test covers
every name of the family precisely so that the next field does not repeat the story —
`email` was added here in the same commit that introduced it.

For `email` the stakes are higher: besides catalog, CONNECT and encryption, skipping
this guard would skip the allowed domains' validation, the only barrier between a
flow and sending data to an arbitrary address.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routes.proxy import transform

pytestmark = pytest.mark.anyio

_USER = SimpleNamespace(id=1, is_superuser=False)

# a payload carrying the attacker's connection: an arbitrary endpoint (internal too,
# e.g. the cloud's metadata endpoint) and the secret IN CLEAR
_HOSTILE_CONNECTION = {
    "connection": {"endpoint_url": "http://169.254.169.254", "access_key": "k", "secret_key": "in-chiaro"},
    "target": {"bucket": "bucket-attaccante", "key": "esfiltrato.parquet"},
}


def _request(body: dict) -> Request:
    """A minimal Request: `_read_json` reads the headers and then the body in chunks."""
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


@pytest.mark.parametrize("field", ["destination", "db_destination", "mirror", "email"])
async def test_destinations_refused_by_the_proxy(field):
    body = {
        "bucket": "data-prep",
        "input_key": "datasets/x.parquet",
        "operations": [],
        field: _HOSTILE_CONNECTION,
    }
    with pytest.raises(HTTPException) as e:
        # session=None is enough: the guard raises BEFORE touching the database
        await transform(_request(body), user=_USER, session=None)
    assert e.value.status_code == 422
    assert "Destinations are not allowed" in str(e.value.detail)


async def test_body_not_an_object_refused():
    """A list in place of an object must not reach the engine."""
    with pytest.raises(HTTPException) as e:
        await transform(_request([1, 2, 3]), user=_USER, session=None)  # type: ignore[arg-type]
    assert e.value.status_code == 422
