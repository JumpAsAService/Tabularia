"""The external ClickHouse's connection set from the panel (Administration → Settings).

The rules these tests pin: the connection's items take a text of the right shape, one of
the allowed choices, a password only as a token of the shared key; the password is never
shown — not in the catalog, not in the logs — and is opened only where it is applied;
the connection is read when a process starts (a rollout), like the other settings read
at start; the catalog shows ClickHouse as available as soon as the panel gives it a
host; «Test connection» says step by step what works, with the server's own reason and
without the password, and saves nothing.

The tests against a server need `CLICKHOUSE_TEST_HOST` (see test_clickhouse_engine.py);
the CI runs them next to a ClickHouse and a MinIO on the same network.
"""
import json
import logging
import os

import pytest

from app.core import panel_settings
from app.core.config import get_settings, settings_from_env
from app.core.crypto import encrypt_secret


def _errors(values):
    return panel_settings.validate(values, settings_from_env())[1]


def _code(key, value):
    e = _errors({key: value})
    return e[0]["code"] if e else ""


# ── what is accepted ──────────────────────────────────────────────────────

def test_the_connection_takes_texts_of_the_right_shape():
    host = "clickhouse_external.host"
    assert _code(host, "ch.example.com") == "" and _code(host, "10.0.0.7") == "" and _code(host, "::1") == ""
    for wrong in ("https://ch.example.com", "ch.example.com/db", "ch example", "ch.example.com:8443/x"):
        assert _code(host, wrong) == "format_host", wrong
    assert _code(host, "  ") == "required" and _code(host, 8123) == "text"
    assert _code("clickhouse_external.database", "tab-prod_1") == ""
    assert _code("clickhouse_external.database", "tab;DROP") == "format_name"
    assert _code("clickhouse_external.database", "") == "required"
    assert _code("clickhouse_external.s3_endpoint", "") == ""  # optional: the engine's storage
    assert _code("clickhouse_external.s3_endpoint", "https://s3.fr-par.scw.cloud") == ""
    assert _code("clickhouse_external.s3_endpoint", "s3.fr-par.scw.cloud") == "format_url"
    assert _code("clickhouse_external.username", "svc@acme") == ""
    assert _code("clickhouse_external.username", "due parole") == "format_user"
    assert _code("clickhouse_external.ai_username", "") == ""
    assert _code("clickhouse_external.transport", "push") == "" and _code("clickhouse_external.transport", "ftp") == "choice"
    assert _code("clickhouse_external.port", 8443) == "" and _code("clickhouse_external.port", 70_000) == "max"
    assert _code("clickhouse_external.secure", True) == "" and _code("clickhouse_external.secure", "yes") == "boolean"
    # a text is kept without the spaces typed around it
    good, _ = panel_settings.validate({host: "  ch.example.com "}, settings_from_env())
    assert good == {host: "ch.example.com"}


# the same samples as frontend/tests/engine-settings.test.mjs: the panel checks with the
# engine's shapes before saving or testing
SAMPLES = {
    "host": (["ch.example.com", "10.0.0.7", "::1", "chpanel"],
             ["https://ch.example.com", "ch.example.com/db", "ch example", "ch.example.com:8443/x", "---"]),
    "name": (["tab-prod_1", "default", "_x"], ["tab;DROP", "due parole", "-x"]),
    "url": (["https://s3.fr-par.scw.cloud", "http://minio:9000", "https://s3.example.com/path"],
            ["s3.fr-par.scw.cloud", "ftp://x", "https://x?y=1"]),
    "user": (["svc@acme", "tab", "a.b-c"], ["due parole", "tab\n", ""]),
}


def test_the_shapes_are_the_ones_the_panel_checks():
    assert set(panel_settings.FORMATS) == set(SAMPLES)
    for fmt, (good, bad) in SAMPLES.items():
        for s in good:
            assert panel_settings.FORMATS[fmt].fullmatch(s), (fmt, s)
        for s in bad:
            assert not panel_settings.FORMATS[fmt].fullmatch(s), (fmt, s)


def test_a_password_only_as_a_token_of_the_shared_key():
    key = "clickhouse_external.password"
    assert _code(key, "in-chiaro") == "secret"  # the gateway encrypts: clear text is not a token
    assert _code(key, encrypt_secret("s3gr3ta")) == ""
    assert _code(key, encrypt_secret("")) == ""  # a user without a password
    from cryptography.fernet import Fernet

    other_key = Fernet(Fernet.generate_key()).encrypt(b"s3gr3ta").decode()
    assert _code(key, other_key) == "secret"  # a key changed underneath
    assert _code(key, None) == "text"


def test_the_password_applies_opened_and_is_never_shown(caplog):
    token = encrypt_secret("s3gr3ta-lunga")
    panel_settings.write({"clickhouse_external.password": token, "clickhouse_external.host": "ch.panel"})
    caplog.set_level(logging.INFO, logger="app.core.panel_settings")
    panel_settings.forget()  # a process that starts now
    s = get_settings()
    assert s.clickhouse_external.password.get_secret_value() == "s3gr3ta-lunga"
    assert s.clickhouse_external.host == "ch.panel" and s.clickhouse_external.enabled
    logged = caplog.text
    assert "clickhouse_external.password" in logged  # it is said that it is set…
    assert "s3gr3ta" not in logged and token not in logged  # …never what it is

    from app.api.routes.panel_settings import read

    items = {v["key"]: v for v in read()["items"]}
    assert items["clickhouse_external.password"]["value"] is True
    assert items["clickhouse_external.password"]["env_value"] in (True, False)
    assert token not in json.dumps(read())
    assert items["clickhouse_external.host"] | {"value": "ch.panel", "kind": "str"} == items["clickhouse_external.host"]
    assert items["clickhouse_external.transport"]["choices"] == ["s3", "push"]
    assert items["clickhouse_external.ai_username"]["optional"] is True


def test_the_connection_waits_for_the_process_to_start_again():
    """A process keeps one connection for its life: the server changes after the rollout."""
    env = settings_from_env()
    assert get_settings().clickhouse_external.host == env.clickhouse_external.host  # first reading
    panel_settings.write({"clickhouse_external.host": "ch.nuovo", "clickhouse_external.port": 8443})
    assert get_settings().clickhouse_external.host == env.clickhouse_external.host
    panel_settings.forget()
    s = get_settings()
    assert (s.clickhouse_external.host, s.clickhouse_external.port) == ("ch.nuovo", 8443)
    item = panel_settings.BY_KEY["clickhouse_external.host"]
    assert set(item.rollout) == {"engine", "worker", "preview-worker", "beat"}


def test_clickhouse_shows_available_when_the_panel_gives_it_a_host(monkeypatch):
    from app.api.routes.panel_settings import read

    monkeypatch.setenv("CLICKHOUSE_EXTERNAL__HOST", "")
    settings_from_env.cache_clear()
    try:
        assert "clickhouse" in read()["unavailable"]
        panel_settings.write({"clickhouse_external.host": "ch.panel"})
        assert "clickhouse" not in read()["unavailable"]
        # the connection's own box is never greyed out: it is where the engine is turned on
        assert "clickhouse_connection" not in read()["unavailable"]
    finally:
        settings_from_env.cache_clear()


def test_the_engine_made_once_signs_in_with_the_panels_password():
    from app.engine.clickhouse_engine import ClickHouseEngine

    panel_settings.write({"clickhouse_external.host": "ch.panel", "clickhouse_external.username": "svc",
                          "clickhouse_external.password": encrypt_secret("dal-pannello")})
    panel_settings.forget()
    eng = ClickHouseEngine.__new__(ClickHouseEngine)
    eng.cfg = get_settings().clickhouse_external
    assert eng._credentials() == ("svc", "dal-pannello")
    assert eng.cfg.host == "ch.panel"


def test_the_redaction_knows_the_panels_password():
    from app.core.redaction import redact_secrets

    panel_settings.write({"clickhouse_external.password": encrypt_secret("parola-del-pannello")})
    panel_settings.forget()
    assert redact_secrets("auth failed for parola-del-pannello") == "auth failed for ***"


def test_the_check_refuses_malformed_values_and_saves_nothing():
    from app.api.routes.panel_settings import Check, clickhouse_check

    r = clickhouse_check(Check(values={"clickhouse_external.host": "https://x", "export.max_rows": 5,
                                       "clickhouse_external.password": "in-chiaro"}))
    assert r.status_code == 422
    codes = {e["key"]: e["code"] for e in json.loads(r.body)["detail"]["errors"]}
    assert codes == {"export.max_rows": "unknown", "clickhouse_external.host": "format_host",
                     "clickhouse_external.password": "secret"}
    assert panel_settings.read_raw() == {}


def test_without_a_host_the_check_says_so():
    from app.core.config import ClickHouseExternalSettings
    from app.engine.clickhouse_check import check

    out = check(ClickHouseExternalSettings(host=""))
    assert out == {"ok": False, "steps": [{"step": "server", "ok": False,
                                           "detail": "No host: set the ClickHouse server's address."}]}


# ── against a real server ───────────────────────────────────────────────────

server = pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve un ClickHouse (CLICKHOUSE_TEST_HOST)")


def _cfg(**over):
    from app.core.config import ClickHouseExternalSettings

    base = dict(host=os.getenv("CLICKHOUSE_TEST_HOST", ""), port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
                database=os.getenv("CLICKHOUSE_TEST_DB", "default"), transport="push", connect_timeout=5)
    return ClickHouseExternalSettings(**(base | over))


@server
def test_the_check_of_a_working_connection_with_push():
    from app.engine.clickhouse_check import check

    out = check(_cfg())
    assert out["ok"], out
    assert [s["step"] for s in out["steps"]] == ["server", "database", "staging"]
    assert out["steps"][0]["detail"][:2].isdigit()  # the server's version
    from app.engine.clickhouse_check import _client

    c = _client(_cfg(), _cfg().username, _cfg().password.get_secret_value())
    try:  # nothing left on the server
        assert int(c.command("SELECT count() FROM system.tables WHERE name LIKE '_tabularia_check_%'")) == 0
    finally:
        c.close()


@server
def test_the_check_reads_from_the_storage_with_s3_and_leaves_nothing():
    from app.engine.clickhouse_check import CHECK_PREFIX, check
    from app.utils import get_storage_service

    svc = get_storage_service()
    bucket = settings_from_env().storage.bucket
    svc.create_bucket(bucket)  # there already: nothing happens
    out = check(_cfg(transport="s3", s3_endpoint=os.getenv("CLICKHOUSE_TEST_S3_ENDPOINT", "")), storage=svc)
    assert out["ok"], out
    assert [s["step"] for s in out["steps"]] == ["server", "database", "storage"]
    assert svc.list_objects(bucket, CHECK_PREFIX) == []


@server
def test_a_wrong_password_fails_at_the_server_without_saying_it():
    from app.engine.clickhouse_check import check

    out = check(_cfg(password="sbagliata-davvero-123"))
    assert not out["ok"] and [s["step"] for s in out["steps"]] == ["server"]
    assert "sbagliata-davvero-123" not in out["steps"][0]["detail"]
    assert out["steps"][0]["detail"].startswith("ClickHouse")


@server
def test_a_missing_database_is_named():
    from app.engine.clickhouse_check import check

    out = check(_cfg(database="non_esiste_proprio"))
    assert not out["ok"] and out["steps"][-1] == {
        "step": "database", "ok": False,
        "detail": "The database «non_esiste_proprio» does not exist on the server: create it, or set another one."}


@server
def test_a_wrong_ai_user_fails_its_step_only():
    from app.engine.clickhouse_check import check

    out = check(_cfg(ai_username="nessuno", ai_password="nessuna-password"))
    assert not out["ok"]
    assert [(s["step"], s["ok"]) for s in out["steps"]] == [("server", True), ("database", True), ("ai_user", False),
                                                           ("staging", True)]
    assert "nessuna-password" not in out["steps"][2]["detail"]


@server
def test_the_route_tries_the_typed_values_over_the_saved_ones():
    from app.api.routes.panel_settings import Check, clickhouse_check

    panel_settings.write({"clickhouse_external.host": "non.risponde.invalid", "clickhouse_external.connect_timeout": 2})
    c = _cfg()
    out = clickhouse_check(Check(values={
        "clickhouse_external.host": c.host, "clickhouse_external.port": c.port,
        "clickhouse_external.username": c.username,
        "clickhouse_external.password": encrypt_secret(c.password.get_secret_value()),
        "clickhouse_external.database": c.database, "clickhouse_external.transport": "push"}))
    assert out["ok"], out
    assert panel_settings.read_raw()["clickhouse_external.host"] == "non.risponde.invalid"  # nothing saved
