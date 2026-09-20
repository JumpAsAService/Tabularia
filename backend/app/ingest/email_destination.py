"""Invio dell'output di un run come email con allegato (csv o xlsx).

Percorso dati: parquet interno → `PolarsEngine.export` su file temporaneo (csv in
streaming, xlsx col tetto del formato) → allegato di un messaggio MIME → SMTP.
Il file temporaneo viene sempre rimosso, anche in errore.

Le credenziali arrivano cifrate (`password_encrypted`, Fernet condiviso col
gateway) e si decifrano solo al momento di aprire la connessione, come per le
sorgenti database e le destinazioni S3.

CONFINE DI SICUREZZA: qui NON si validano i destinatari. L'elenco dei domini
ammessi vive sulla connessione e lo applica il GATEWAY, che è l'unico a poterlo
fare in modo vincolante: il worker riceve destinatari già risolti, e un controllo
in questo punto sarebbe scavalcabile da chiunque riesca a parlare con l'engine.
Questo modulo spedisce ciò che gli viene detto di spedire.
"""
from __future__ import annotations

import logging
import os
import smtplib
import tempfile
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Literal

from pydantic import BaseModel

from app.ingest.converters import IngestError

logger = logging.getLogger(__name__)

# Tetto dell'allegato. Quasi ogni server di posta rifiuta oltre i 25 MB, e la
# codifica base64 gonfia di circa un terzo: fermarsi qui dà un errore chiaro
# invece di un rifiuto SMTP oscuro dopo aver occupato il worker per minuti.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


class EmailDestinationError(IngestError):
    """Errore parlante da mostrare all'utente (allegato, server, credenziali)."""


class SmtpConnectionSpec(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    # una delle due: cifrata (come arriva dal gateway) o in chiaro (prove dirette
    # sull'engine in sviluppo), stessa convenzione di S3ConnectionSpec
    password: str = ""
    password_encrypted: str = ""
    # mittente: indirizzo e, facoltativo, nome visualizzato
    from_address: str = ""
    from_name: str = ""
    # starttls (587, il default moderno) | ssl (465, implicito) | none (solo reti fidate)
    tls: Literal["starttls", "ssl", "none"] = "starttls"
    timeout_seconds: float = 30.0

    def resolve_secret(self) -> str:
        if self.password_encrypted:
            from app.core.crypto import decrypt_secret

            return decrypt_secret(self.password_encrypted)
        return self.password

    def sender(self) -> str:
        return formataddr((self.from_name, self.from_address)) if self.from_name else self.from_address


class EmailSpec(BaseModel):
    to: list[str] = []
    cc: list[str] = []
    subject: str = ""
    body: str = ""
    # il corpo è HTML: si spedisce multipart/alternative con un'alternativa
    # testuale, così non arriva illeggibile a chi legge in solo testo
    body_is_html: bool = False
    attachment_name: str = "report"
    attachment_format: Literal["csv", "xlsx"] = "xlsx"


_MIME = {
    "csv": ("text", "csv"),
    "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


def _html_to_text(html: str) -> str:
    """Alternativa testuale grossolana ma onesta: togliere i tag è meglio che
    spedire sorgente HTML a chi non lo rende."""
    import re

    testo = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", html, flags=re.I)
    testo = re.sub(r"<[^>]+>", "", testo)
    return re.sub(r"\n{3,}", "\n\n", testo).strip()


def _filename(spec: EmailSpec) -> str:
    base = (spec.attachment_name or "report").strip() or "report"
    # niente separatori di percorso nel nome di un allegato
    base = base.replace("/", "_").replace("\\", "_").strip(". ")
    suffix = f".{spec.attachment_format}"
    return base if base.lower().endswith(suffix) else base + suffix


def build_attachment(bucket: str, key: str, spec: EmailSpec) -> tuple[bytes, str]:
    """Scarica il parquet dell'output e lo rende nel formato richiesto.

    Nessuna operazione: la catena è già stata eseguita e il risultato è in
    `key`. È lo stesso schema dell'export con un engine non-Polars (calcola lo
    snapshot, poi Polars scrive il file), quindi il formato è identico a quello
    che l'utente otterrebbe scaricandolo a mano.
    """
    from app.engine import DataSource, get_engine

    fd, path = tempfile.mkstemp(suffix=f".{spec.attachment_format}")
    os.close(fd)
    try:
        get_engine("polars").export(
            source=DataSource(bucket=bucket, key=key),
            operations=[],
            fmt=spec.attachment_format,
            out_path=path,
        )
        size = os.path.getsize(path)
        if size > MAX_ATTACHMENT_BYTES:
            raise EmailDestinationError(
                f"L'allegato pesa {size / 1024 / 1024:.1f} MB, oltre il limite di "
                f"{MAX_ATTACHMENT_BYTES // 1024 // 1024} MB: quasi ogni server di posta lo "
                "rifiuterebbe. Riduci i dati a monte (filter/limit) o scegli il formato CSV."
            )
        with open(path, "rb") as f:
            return f.read(), _filename(spec)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _spedisci(conn: SmtpConnectionSpec, msg: EmailMessage, destinatari: list[str]) -> None:
    """Apre, cifra, autentica e spedisce. Estratta perché ci sono due cose da
    mandare: l'output di un flusso, con il suo allegato, e un avviso, che non ne
    ha nessuno."""
    try:
        if conn.tls == "ssl":
            server = smtplib.SMTP_SSL(conn.host, conn.port, timeout=conn.timeout_seconds)
        else:
            server = smtplib.SMTP(conn.host, conn.port, timeout=conn.timeout_seconds)
        with server:
            if conn.tls == "starttls":
                server.starttls()
            if conn.username:
                server.login(conn.username, conn.resolve_secret())
            server.send_message(msg, to_addrs=destinatari)
    except smtplib.SMTPAuthenticationError as e:
        raise EmailDestinationError(f"Autenticazione SMTP rifiutata da {conn.host}: {e}") from e
    except smtplib.SMTPException as e:
        raise EmailDestinationError(f"Invio rifiutato da {conn.host}: {type(e).__name__}: {e}") from e
    except OSError as e:  # DNS, rete, porta chiusa, TLS
        raise EmailDestinationError(f"Impossibile raggiungere {conn.host}:{conn.port}: {e}") from e


def send_notice(conn: SmtpConnectionSpec, to: list[str], subject: str, body: str) -> dict:
    """Un avviso in solo testo, senza allegati: serve a dire che un flusso è
    fallito, e deve arrivare anche quando è proprio il dato a mancare."""
    destinatari = [a.strip() for a in to if a and a.strip()]
    if not destinatari:
        raise EmailDestinationError("Nessun destinatario: indica almeno un indirizzo.")
    if not conn.from_address.strip():
        raise EmailDestinationError("La connessione SMTP non ha un indirizzo mittente.")

    msg = EmailMessage()
    msg["From"] = conn.sender()
    msg["To"] = ", ".join(destinatari)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    # `Auto-Submitted` dice ai server che è posta generata da un programma: evita
    # che una risposta automatica dall'altra parte inneschi un rimbalzo infinito
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)

    _spedisci(conn, msg, destinatari)
    logger.info("📧 avviso inviato a %d destinatari", len(destinatari))
    return {"ok": True, "recipients": len(destinatari)}


def send_output_email(conn: SmtpConnectionSpec, spec: EmailSpec, bucket: str, key: str) -> dict:
    """Genera l'allegato e lo spedisce. Solleva `EmailDestinationError` con un
    messaggio leggibile; il chiamante decide se il run debba fallire."""
    destinatari = [a for a in (*spec.to, *spec.cc) if a.strip()]
    if not destinatari:
        raise EmailDestinationError("Nessun destinatario: indica almeno un indirizzo.")
    if not conn.from_address.strip():
        raise EmailDestinationError("La connessione SMTP non ha un indirizzo mittente.")

    dati, nome_file = build_attachment(bucket, key, spec)

    msg = EmailMessage()
    msg["From"] = conn.sender()
    msg["To"] = ", ".join(a for a in spec.to if a.strip())
    if spec.cc:
        msg["Cc"] = ", ".join(a for a in spec.cc if a.strip())
    msg["Subject"] = spec.subject or nome_file
    msg["Message-ID"] = make_msgid()

    if spec.body_is_html:
        msg.set_content(_html_to_text(spec.body))
        msg.add_alternative(spec.body, subtype="html")
    else:
        msg.set_content(spec.body or "")

    maintype, subtype = _MIME[spec.attachment_format]
    msg.add_attachment(dati, maintype=maintype, subtype=subtype, filename=nome_file)

    _spedisci(conn, msg, destinatari)

    logger.info("📧 email inviata a %d destinatari, allegato %s (%d byte)", len(destinatari), nome_file, len(dati))
    return {
        "ok": True,
        "recipients": len(destinatari),
        "attachment": nome_file,
        "bytes": len(dati),
    }


def test_connection(conn: SmtpConnectionSpec) -> None:
    """Prova la connessione senza spedire nulla: apre, cifra e autentica."""
    if not conn.host.strip():
        raise EmailDestinationError("Nessun server SMTP indicato.")
    try:
        if conn.tls == "ssl":
            server = smtplib.SMTP_SSL(conn.host, conn.port, timeout=conn.timeout_seconds)
        else:
            server = smtplib.SMTP(conn.host, conn.port, timeout=conn.timeout_seconds)
        with server:
            if conn.tls == "starttls":
                server.starttls()
            if conn.username:
                server.login(conn.username, conn.resolve_secret())
    except smtplib.SMTPAuthenticationError as e:
        raise EmailDestinationError(f"Autenticazione rifiutata da {conn.host}: {e}") from e
    except smtplib.SMTPException as e:
        raise EmailDestinationError(f"{conn.host} ha rifiutato la connessione: {type(e).__name__}: {e}") from e
    except OSError as e:
        raise EmailDestinationError(f"Impossibile raggiungere {conn.host}:{conn.port}: {e}") from e
