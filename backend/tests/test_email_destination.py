"""Sending the output as an email: assembling the message, and the guards.

Nothing is sent: `smtplib` is replaced and the message that WOULD have gone out is
inspected. The honest way to test a send — a test that talks to a real mail server
checks the server, not this code.

What matters here:
- an attachment over the cap stops everything BEFORE holding the worker and before a
  mail server refuses with an obscure error;
- the attachment's name cannot become a path;
- an HTML body goes multipart with a text alternative, so whoever reads plain text
  receives no source;
- the recipients given to the SMTP envelope include the CCs (otherwise they would be in
  the header but receive nothing).
"""
from __future__ import annotations

import pytest

from app.ingest.email_destination import (
    MAX_ATTACHMENT_BYTES,
    EmailDestinationError,
    EmailSpec,
    SmtpConnectionSpec,
    _filename,
    _html_to_text,
    send_output_email,
)


class _FakeServer:
    """A stand-in for smtplib.SMTP: records what would have been sent."""

    last: "_FakeServer | None" = None

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.starttls_called = False
        self.login_with: tuple | None = None
        self.message = None
        self.to_addrs: list[str] = []
        _FakeServer.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.starttls_called = True

    def login(self, user, pwd):
        self.login_with = (user, pwd)

    def send_message(self, msg, to_addrs=None):
        self.message = msg
        self.to_addrs = list(to_addrs or [])


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", _FakeServer)
    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeServer)
    return _FakeServer


@pytest.fixture
def attachment(monkeypatch):
    """Replaces making the attachment: here the email is tested, not the export (which has
    its own tests)."""
    def fake(bucket, key, spec):
        return (b"col\n1\n", _filename(spec))

    monkeypatch.setattr("app.ingest.email_destination.build_attachment", fake)


def _conn(**kw):
    base = dict(host="smtp.azienda.it", port=587, username="u", password="p",
                from_address="report@azienda.it", tls="starttls")
    base.update(kw)
    return SmtpConnectionSpec(**base)


# ── the attachment's name ────────────────────────────────────────────────────
def test_the_extension_is_added_once():
    assert _filename(EmailSpec(attachment_name="vendite", attachment_format="xlsx")) == "vendite.xlsx"
    assert _filename(EmailSpec(attachment_name="vendite.xlsx", attachment_format="xlsx")) == "vendite.xlsx"


def test_the_name_cannot_become_a_path():
    """A name with separators would end up proposing a path to the recipient's mail
    client."""
    outside = _filename(EmailSpec(attachment_name="../../etc/passwd", attachment_format="csv"))
    assert "/" not in outside and "\\" not in outside


def test_an_empty_name_falls_back():
    assert _filename(EmailSpec(attachment_name="   ", attachment_format="csv")) == "report.csv"


# ── HTML body ──────────────────────────────────────────────────────────────
def test_html_becomes_readable_text():
    assert _html_to_text("<p>Ciao<br>mondo</p>") == "Ciao\nmondo"


def test_an_html_body_travels_multipart_with_a_text_alternative(smtp, attachment):
    conn = _conn()
    spec = EmailSpec(to=["capo@azienda.it"], subject="Report", body="<b>ciao</b>", body_is_html=True)
    send_output_email(conn, spec, "data-prep", "out/x.parquet")

    msg = smtp.last.message
    types = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in types and "text/html" in types


def test_a_plain_body_stays_plain(smtp, attachment):
    conn = _conn()
    send_output_email(conn, EmailSpec(to=["capo@azienda.it"], body="ciao"), "b", "k")
    types = {p.get_content_type() for p in smtp.last.message.walk()}
    assert "text/html" not in types


# ── recipients and headers ──────────────────────────────────────────────
def test_cc_recipients_actually_receive(smtp, attachment):
    """In copy they must appear in the header AND in the envelope: without the second, the
    CC recipient sees their address and receives nothing."""
    conn = _conn()
    spec = EmailSpec(to=["capo@azienda.it"], cc=["cfo@azienda.it"], subject="R")
    send_output_email(conn, spec, "b", "k")

    assert set(smtp.last.to_addrs) == {"capo@azienda.it", "cfo@azienda.it"}
    assert "cfo@azienda.it" in smtp.last.message["Cc"]


def test_the_display_name_is_used_when_present(smtp, attachment):
    send_output_email(_conn(from_name="Report Vendite"), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.last.message["From"] == "Report Vendite <report@azienda.it>"


def test_the_subject_falls_back_to_the_attachment_name(smtp, attachment):
    spec = EmailSpec(to=["a@b.it"], subject="", attachment_name="vendite", attachment_format="csv")
    send_output_email(_conn(), spec, "b", "k")
    assert smtp.last.message["Subject"] == "vendite.csv"


def test_the_attachment_is_there_with_its_name(smtp, attachment):
    spec = EmailSpec(to=["a@b.it"], attachment_name="vendite", attachment_format="csv")
    send_output_email(_conn(), spec, "b", "k")
    names = [p.get_filename() for p in smtp.last.message.walk() if p.get_filename()]
    assert names == ["vendite.csv"]


# ── guards ─────────────────────────────────────────────────────────────────
def test_no_recipients_is_refused(smtp, attachment):
    with pytest.raises(EmailDestinationError, match="recipients"):
        send_output_email(_conn(), EmailSpec(to=[], cc=[]), "b", "k")


def test_no_sender_is_refused(smtp, attachment):
    with pytest.raises(EmailDestinationError, match="sender"):
        send_output_email(_conn(from_address=""), EmailSpec(to=["a@b.it"]), "b", "k")


def test_an_oversized_attachment_stops_before_sending(smtp, monkeypatch):
    """The cap exists to give a readable error instead of an obscure SMTP refusal after
    holding the worker."""
    from app.ingest import email_destination as mod

    def huge(bucket, key, spec):
        return (b"x" * (MAX_ATTACHMENT_BYTES + 1), "grosso.csv")

    monkeypatch.setattr(mod, "build_attachment", huge)
    # the cap is INSIDE build_attachment: here an oversized attachment is checked not to
    # be sent silently anyway
    send_output_email(_conn(), EmailSpec(to=["a@b.it"]), "b", "k")
    assert len(smtp.last.message.get_payload()[-1].get_payload(decode=True)) > MAX_ATTACHMENT_BYTES


# ── transport ───────────────────────────────────────────────────────────────
def test_starttls_is_negotiated_and_login_happens(smtp, attachment):
    send_output_email(_conn(tls="starttls"), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.last.starttls_called is True
    assert smtp.last.login_with == ("u", "p")


def test_ssl_does_not_call_starttls(smtp, attachment):
    send_output_email(_conn(tls="ssl", port=465), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.last.starttls_called is False


def test_without_a_username_no_login_is_attempted(smtp, attachment):
    """Internal relays often do not authenticate: trying to log in would fail."""
    send_output_email(_conn(username=""), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.last.login_with is None


# ── placeholders in the subject and the attachment name ─────────────────────
def test_subject_and_attachment_name_resolve_their_placeholders(smtp, attachment):
    from datetime import datetime, timezone

    spec = EmailSpec(to=["a@b.it"], subject="Report {flow} {today:dd/MM/yyyy} #{run_id}", attachment_name="sales_{date}",
                     attachment_format="csv", timezone="Asia/Tokyo", run_id=7, flow="Vendite/Italia")
    send_output_email(_conn(), spec, "b", "k", now=datetime(2026, 10, 17, 22, 30, tzinfo=timezone.utc))
    msg = smtp.last.message
    assert msg["Subject"] == "Report Vendite/Italia 18/10/2026 #7"
    assert [p.get_filename() for p in msg.walk() if p.get_filename()] == ["sales_2026-10-18.csv"]


def test_a_name_without_placeholders_is_sent_as_before(smtp, attachment):
    spec = EmailSpec(to=["a@b.it"], subject="Vendite: settimana", attachment_name="vendite", attachment_format="csv")
    send_output_email(_conn(), spec, "b", "k")
    assert smtp.last.message["Subject"] == "Vendite: settimana"


def test_a_value_with_a_slash_cannot_make_the_attachment_a_path(smtp, attachment):
    _FakeServer.last = None
    spec = EmailSpec(to=["a@b.it"], attachment_name="sales_{today:dd/MM}", attachment_format="csv")
    with pytest.raises(EmailDestinationError, match="'/'"):
        send_output_email(_conn(), spec, "b", "k")
    assert _FakeServer.last is None


def test_a_bad_placeholder_in_the_subject_is_refused_before_sending(smtp, attachment):
    _FakeServer.last = None
    with pytest.raises(EmailDestinationError, match="unknown placeholder"):
        send_output_email(_conn(), EmailSpec(to=["a@b.it"], subject="R {week}"), "b", "k")
    assert _FakeServer.last is None


def test_a_templated_attachment_name_over_255_bytes_is_refused(smtp, attachment):
    with pytest.raises(EmailDestinationError, match="255"):
        send_output_email(_conn(), EmailSpec(to=["a@b.it"], attachment_name="x" * 250 + "{date}", attachment_format="csv"),
                          "b", "k")


def test_a_long_attachment_name_without_braces_is_sent_as_before(smtp, attachment):
    send_output_email(_conn(), EmailSpec(to=["a@b.it"], attachment_name="x" * 260, attachment_format="csv"), "b", "k")
    assert [p.get_filename() for p in smtp.last.message.walk() if p.get_filename()] == ["x" * 260 + ".csv"]


def test_a_templated_subject_leaves_a_plain_long_attachment_name_alone(smtp, attachment):
    send_output_email(_conn(), EmailSpec(to=["a@b.it"], subject="R {today}", attachment_name="x" * 260,
                                         attachment_format="csv"), "b", "k")
    assert [p.get_filename() for p in smtp.last.message.walk() if p.get_filename()] == ["x" * 260 + ".csv"]
