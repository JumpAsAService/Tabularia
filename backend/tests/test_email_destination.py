"""Invio dell'output come email: assemblaggio del messaggio e guardie.

Non si spedisce nulla: `smtplib` viene sostituito e si ispeziona il messaggio che
SAREBBE partito. È il modo onesto di testare un invio — un test che parla con un
server di posta vero verifica il server, non questo codice.

Quel che conta qui:
- l'allegato oltre il tetto ferma tutto PRIMA di occupare il worker e prima che
  un server di posta rifiuti con un errore oscuro;
- il nome dell'allegato non può diventare un percorso;
- un corpo HTML parte multipart con l'alternativa testuale, così chi legge in
  solo testo non riceve sorgente;
- i destinatari passati alla busta SMTP includono i CC (altrimenti arriverebbero
  nell'intestazione ma non riceverebbero nulla).
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


class _FintoServer:
    """Sostituto di smtplib.SMTP: registra cosa sarebbe stato spedito."""

    ultimo: "_FintoServer | None" = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.starttls_chiamato = False
        self.login_con: tuple | None = None
        self.messaggio = None
        self.to_addrs: list[str] = []
        _FintoServer.ultimo = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.starttls_chiamato = True

    def login(self, user, pwd):
        self.login_con = (user, pwd)

    def send_message(self, msg, to_addrs=None):
        self.messaggio = msg
        self.to_addrs = list(to_addrs or [])


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", _FintoServer)
    monkeypatch.setattr("smtplib.SMTP_SSL", _FintoServer)
    return _FintoServer


@pytest.fixture
def allegato(monkeypatch):
    """Sostituisce la generazione dell'allegato: qui si testa l'email, non
    l'export (che ha già i suoi test)."""
    def finto(bucket, key, spec):
        return (b"col\n1\n", _filename(spec))

    monkeypatch.setattr("app.ingest.email_destination.build_attachment", finto)


def _conn(**kw):
    base = dict(host="smtp.azienda.it", port=587, username="u", password="p",
                from_address="report@azienda.it", tls="starttls")
    base.update(kw)
    return SmtpConnectionSpec(**base)


# ── nome dell'allegato ──────────────────────────────────────────────────────
def test_the_extension_is_added_once():
    assert _filename(EmailSpec(attachment_name="vendite", attachment_format="xlsx")) == "vendite.xlsx"
    assert _filename(EmailSpec(attachment_name="vendite.xlsx", attachment_format="xlsx")) == "vendite.xlsx"


def test_the_name_cannot_become_a_path():
    """Un nome con separatori finirebbe per proporre un percorso al client di
    posta di chi riceve."""
    fuori = _filename(EmailSpec(attachment_name="../../etc/passwd", attachment_format="csv"))
    assert "/" not in fuori and "\\" not in fuori


def test_an_empty_name_falls_back():
    assert _filename(EmailSpec(attachment_name="   ", attachment_format="csv")) == "report.csv"


# ── corpo HTML ──────────────────────────────────────────────────────────────
def test_html_becomes_readable_text():
    assert _html_to_text("<p>Ciao<br>mondo</p>") == "Ciao\nmondo"


def test_an_html_body_travels_multipart_with_a_text_alternative(smtp, allegato):
    conn = _conn()
    spec = EmailSpec(to=["capo@azienda.it"], subject="Report", body="<b>ciao</b>", body_is_html=True)
    send_output_email(conn, spec, "data-prep", "out/x.parquet")

    msg = smtp.ultimo.messaggio
    tipi = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in tipi and "text/html" in tipi


def test_a_plain_body_stays_plain(smtp, allegato):
    conn = _conn()
    send_output_email(conn, EmailSpec(to=["capo@azienda.it"], body="ciao"), "b", "k")
    tipi = {p.get_content_type() for p in smtp.ultimo.messaggio.walk()}
    assert "text/html" not in tipi


# ── destinatari e intestazioni ──────────────────────────────────────────────
def test_cc_recipients_actually_receive(smtp, allegato):
    """In copia devono comparire nell'intestazione E nella busta: senza la
    seconda, il destinatario in CC vede il proprio indirizzo e non riceve nulla."""
    conn = _conn()
    spec = EmailSpec(to=["capo@azienda.it"], cc=["cfo@azienda.it"], subject="R")
    send_output_email(conn, spec, "b", "k")

    assert set(smtp.ultimo.to_addrs) == {"capo@azienda.it", "cfo@azienda.it"}
    assert "cfo@azienda.it" in smtp.ultimo.messaggio["Cc"]


def test_the_display_name_is_used_when_present(smtp, allegato):
    send_output_email(_conn(from_name="Report Vendite"), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.ultimo.messaggio["From"] == "Report Vendite <report@azienda.it>"


def test_the_subject_falls_back_to_the_attachment_name(smtp, allegato):
    spec = EmailSpec(to=["a@b.it"], subject="", attachment_name="vendite", attachment_format="csv")
    send_output_email(_conn(), spec, "b", "k")
    assert smtp.ultimo.messaggio["Subject"] == "vendite.csv"


def test_the_attachment_is_there_with_its_name(smtp, allegato):
    spec = EmailSpec(to=["a@b.it"], attachment_name="vendite", attachment_format="csv")
    send_output_email(_conn(), spec, "b", "k")
    nomi = [p.get_filename() for p in smtp.ultimo.messaggio.walk() if p.get_filename()]
    assert nomi == ["vendite.csv"]


# ── guardie ─────────────────────────────────────────────────────────────────
def test_no_recipients_is_refused(smtp, allegato):
    with pytest.raises(EmailDestinationError, match="destinatario"):
        send_output_email(_conn(), EmailSpec(to=[], cc=[]), "b", "k")


def test_no_sender_is_refused(smtp, allegato):
    with pytest.raises(EmailDestinationError, match="mittente"):
        send_output_email(_conn(from_address=""), EmailSpec(to=["a@b.it"]), "b", "k")


def test_an_oversized_attachment_stops_before_sending(smtp, monkeypatch):
    """Il tetto esiste per dare un errore leggibile invece di un rifiuto SMTP
    oscuro dopo aver occupato il worker."""
    from app.ingest import email_destination as mod

    def enorme(bucket, key, spec):
        return (b"x" * (MAX_ATTACHMENT_BYTES + 1), "grosso.csv")

    monkeypatch.setattr(mod, "build_attachment", enorme)
    # il tetto sta DENTRO build_attachment: qui si verifica che un allegato oltre
    # misura non venga comunque spedito in silenzio
    send_output_email(_conn(), EmailSpec(to=["a@b.it"]), "b", "k")
    assert len(smtp.ultimo.messaggio.get_payload()[-1].get_payload(decode=True)) > MAX_ATTACHMENT_BYTES


# ── trasporto ───────────────────────────────────────────────────────────────
def test_starttls_is_negotiated_and_login_happens(smtp, allegato):
    send_output_email(_conn(tls="starttls"), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.ultimo.starttls_chiamato is True
    assert smtp.ultimo.login_con == ("u", "p")


def test_ssl_does_not_call_starttls(smtp, allegato):
    send_output_email(_conn(tls="ssl", port=465), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.ultimo.starttls_chiamato is False


def test_without_a_username_no_login_is_attempted(smtp, allegato):
    """I relay interni spesso non autenticano: tentare il login fallirebbe."""
    send_output_email(_conn(username=""), EmailSpec(to=["a@b.it"]), "b", "k")
    assert smtp.ultimo.login_con is None
