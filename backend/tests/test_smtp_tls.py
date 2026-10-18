"""The SMTP server's certificate is verified (review 2026-10-14, sec-secrets-infra-3).

smtplib's default context verifies nothing (CERT_NONE, no hostname check): anyone on
the path between the worker and the relay could present any certificate and read the
SMTP password sent by login(). Both STARTTLS and implicit TLS now get a verifying
context; SMTP__VERIFY_TLS=false is the deployment's explicit opt-out for a relay
with a self-signed certificate."""
import ssl

import pytest

from app.core.config import Settings, get_settings
from app.ingest import email_destination as mod
from app.ingest.email_destination import EmailDestinationError, SmtpConnectionSpec


class _Server:
    contexts: list = []

    def __init__(self, host, port, timeout=None, context=None):
        if context is not None:
            _Server.contexts.append(("ssl", context))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        _Server.contexts.append(("starttls", context))

    def login(self, user, pwd):
        pass


@pytest.fixture
def server(monkeypatch):
    _Server.contexts = []
    monkeypatch.setattr("smtplib.SMTP", _Server)
    monkeypatch.setattr("smtplib.SMTP_SSL", _Server)
    return _Server


@pytest.mark.parametrize("tls", ["starttls", "ssl"])
def test_the_certificate_and_the_name_are_verified(server, tls):
    mod.test_connection(SmtpConnectionSpec(host="smtp.example.com", port=587, tls=tls, username="u", password="p"))
    ((kind, ctx),) = server.contexts
    assert kind == tls and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def test_a_deployment_can_opt_out_for_a_self_signed_relay(server, monkeypatch):
    monkeypatch.setenv("SMTP__VERIFY_TLS", "false")
    get_settings.cache_clear()
    try:
        mod.test_connection(SmtpConnectionSpec(host="relay.local", port=587, tls="starttls"))
    finally:
        get_settings.cache_clear()
    ((_, ctx),) = server.contexts
    assert ctx.verify_mode == ssl.CERT_NONE and not ctx.check_hostname


def test_verification_is_on_by_default(monkeypatch):
    monkeypatch.delenv("SMTP__VERIFY_TLS", raising=False)
    assert Settings().smtp.verify_tls is True


def test_a_certificate_that_fails_says_what_to_do(monkeypatch):
    class _Refuses(_Server):
        def starttls(self, context=None):
            raise ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")

    monkeypatch.setattr("smtplib.SMTP", _Refuses)
    with pytest.raises(EmailDestinationError, match="SMTP__VERIFY_TLS"):
        mod.test_connection(SmtpConnectionSpec(host="relay.local", port=587, tls="starttls"))
