"""Lo slot delle preview e' recintato per utente.

Una nuova preview su uno slot butta giu' la precedente: e' il meccanismo che
impedisce all'editor di restare appeso quando si clicca un nodo dopo l'altro.
Ma lo slot lo dichiara il browser — senza recinto, sarebbe un modo per annullare
le anteprime di un ALTRO utente.
"""
import json

import pytest

from app.routes.proxy import scope_preview_slot

BODY = {"bucket": "b", "input_key": "datasets/x.parquet", "operations": [], "limit": 100}


def _scoped(payload, user_id=7):
    raw = json.dumps(payload).encode()
    return raw, scope_preview_slot(raw, payload, user_id)


def test_the_slot_is_prefixed_with_the_authenticated_user():
    _, out = _scoped({**BODY, "slot": "ed-3f2a-preview"})
    assert json.loads(out)["slot"] == "u7:ed-3f2a-preview"


def test_two_users_with_the_same_slot_never_collide():
    a = json.loads(_scoped({**BODY, "slot": "ed-1-preview"}, user_id=1)[1])["slot"]
    b = json.loads(_scoped({**BODY, "slot": "ed-1-preview"}, user_id=2)[1])["slot"]
    assert a != b


def test_without_a_slot_the_body_is_untouched_byte_for_byte():
    raw, out = _scoped(BODY)
    assert out is raw


@pytest.mark.parametrize("bad", ["u1:ed-preview", "a b", "x'y", "", "x" * 65, 42, None, ["a"]])
def test_a_malformed_slot_is_dropped_not_forwarded(bad):
    """Compreso il tentativo di spacciarsi per un altro utente ("u1:…"): i due
    punti non sono ammessi nello slot del client."""
    _, out = _scoped({**BODY, "slot": bad})
    assert "slot" not in json.loads(out)


def test_everything_else_in_the_body_survives():
    _, out = _scoped({**BODY, "slot": "ed-1-cols", "engine": "clickhouse", "no_cache": True})
    got = json.loads(out)
    assert got.pop("slot") == "u7:ed-1-cols" and got == {**BODY, "engine": "clickhouse", "no_cache": True}
