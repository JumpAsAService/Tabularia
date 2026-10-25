"""Sending a run's output as an email with an attachment (csv or xlsx).

Data path: the internal parquet → `PolarsEngine.export` on a temporary file (csv
streamed, xlsx with the format's cap) → the attachment of a MIME message → SMTP. The
temporary file is always removed, on errors too.

The credentials arrive encrypted (`password_encrypted`, the Fernet key shared with the
gateway) and are decrypted only when the connection opens, as for the database sources
and the S3 destinations.

SECURITY BOUNDARY: the recipients are NOT validated here. The list of allowed domains
lives on the connection and the GATEWAY enforces it, the only one that can do so
bindingly: the worker receives recipients resolved already, and a check here could be
bypassed by anyone able to talk to the engine. This module sends what it is told to send.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import tempfile
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.ingest.converters import IngestError

logger = logging.getLogger(__name__)

# The attachment's cap. Nearly every mail server refuses beyond 25 MB, and base64
# inflates by about a third: stopping here gives a clear error instead of an obscure
# SMTP refusal after holding the worker for minutes.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024


class EmailDestinationError(IngestError):
    """A clear error to show the user (attachment, server, credentials)."""


class SmtpConnectionSpec(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    # one of the two: encrypted (as it comes from the gateway) or in clear (direct trials
    # on the engine in development), the same convention as S3ConnectionSpec
    password: str = ""
    password_encrypted: str = ""
    # sender: address and, optionally, display name
    from_address: str = ""
    from_name: str = ""
    # starttls (587, the modern default) | ssl (465, implicit) | none (trusted networks only)
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
    # the body is HTML: sent as multipart/alternative with a text alternative, so it
    # does not arrive unreadable to whoever reads plain text only
    body_is_html: bool = False
    attachment_name: str = "report"
    attachment_format: Literal["csv", "xlsx"] = "xlsx"
    # the subject and the attachment name are templates with the Outputs'
    # placeholders (app.core.placeholders), in this zone; run_id and flow set by
    # the gateway. The body is not a template: it is sent as written.
    timezone: str = Field(default="UTC", max_length=64)
    run_id: int | None = None
    flow: str = ""


def resolve_names(spec: EmailSpec, now: datetime | None = None) -> EmailSpec:
    """The spec with its subject and attachment name resolved. A placeholder value
    cannot put a folder in the attachment name; the subject keeps {flow} as it is."""
    subject, name = spec.subject or "", spec.attachment_name or ""
    if not any(c in subject + name for c in "{}"):
        return spec
    from app.core import placeholders as ph

    try:
        ctx = ph.output_context(now or datetime.now(timezone.utc), spec.timezone, spec.run_id, spec.flow)
        subject = ph.render_output(subject, ctx, "text", "subject") if any(c in subject for c in "{}") else subject
        name = ph.render_output(name, ctx, "file", "attachment name") if any(c in name for c in "{}") else name
    except ph.PlaceholderError as e:
        raise EmailDestinationError(str(e)) from e
    if "\r" in subject or "\n" in subject:
        raise EmailDestinationError("the subject cannot hold a line break")
    resolved = spec.model_copy(update={"subject": subject, "attachment_name": name})
    if any(c in (spec.attachment_name or "") for c in "{}"):  # resolved from placeholders: its final length is checked
        final = _filename(resolved)
        if len(final.encode()) > 255:
            raise EmailDestinationError(f"the attachment name is longer than 255 bytes ({len(final.encode())}): shorten it")
    return resolved


_MIME = {
    "csv": ("text", "csv"),
    "xlsx": ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


def _html_to_text(html: str) -> str:
    """A rough but honest text alternative: stripping the tags is better than sending HTML
    source to whoever does not render it."""
    import re

    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _filename(spec: EmailSpec) -> str:
    base = (spec.attachment_name or "report").strip() or "report"
    # no path separators in an attachment's name
    base = base.replace("/", "_").replace("\\", "_").strip(". ")
    suffix = f".{spec.attachment_format}"
    return base if base.lower().endswith(suffix) else base + suffix


def build_attachment(bucket: str, key: str, spec: EmailSpec) -> tuple[bytes, str]:
    """Downloads the output's parquet and renders it in the format asked for.

    No operations: the chain has run already and the result is in `key`. The same scheme
    as the export with a non-Polars engine (it computes the snapshot, then Polars writes
    the file), so the format is the same the user would get downloading it by hand.
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
                f"The attachment is {size / 1024 / 1024:.1f} MB, over the "
                f"{MAX_ATTACHMENT_BYTES // 1024 // 1024} MB limit: almost every mail server would "
                "reject it. Reduce the data upstream (filter/limit) or choose the CSV format."
            )
        with open(path, "rb") as f:
            return f.read(), _filename(spec)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _tls_context() -> ssl.SSLContext:
    """The TLS context of an SMTP connection: certificate and name verified, unless
    the deployment opted out (SMTP__VERIFY_TLS=false). smtplib's own default
    verifies nothing, so the password went to whoever answered."""
    from app.core.config import get_settings

    if get_settings().smtp.verify_tls:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _open(conn: SmtpConnectionSpec) -> smtplib.SMTP:
    """Connects and encrypts as the connection says (`tls`)."""
    if conn.tls == "ssl":
        return smtplib.SMTP_SSL(conn.host, conn.port, timeout=conn.timeout_seconds, context=_tls_context())
    return smtplib.SMTP(conn.host, conn.port, timeout=conn.timeout_seconds)


def _certificate_refused(conn: SmtpConnectionSpec, e: ssl.SSLCertVerificationError) -> EmailDestinationError:
    reason = getattr(e, "verify_message", "") or str(e)
    return EmailDestinationError(
        f"The certificate of {conn.host} could not be verified ({reason}). Give the server a certificate "
        f"that matches its name, or, for a relay with a self-signed one, set SMTP__VERIFY_TLS=false on the engine."
    )


def _send(conn: SmtpConnectionSpec, msg: EmailMessage, recipients: list[str]) -> None:
    """Opens, encrypts, authenticates and sends. Split out because there are two things to
    send: a flow's output, with its attachment, and a notice, which has none."""
    try:
        with _open(conn) as server:
            if conn.tls == "starttls":
                server.starttls(context=_tls_context())
            if conn.username:
                server.login(conn.username, conn.resolve_secret())
            server.send_message(msg, to_addrs=recipients)
    except ssl.SSLCertVerificationError as e:
        raise _certificate_refused(conn, e) from e
    except smtplib.SMTPAuthenticationError as e:
        raise EmailDestinationError(f"SMTP authentication rejected by {conn.host}: {e}") from e
    except smtplib.SMTPException as e:
        raise EmailDestinationError(f"Sending was rejected by {conn.host}: {type(e).__name__}: {e}") from e
    except OSError as e:  # DNS, network, closed port, TLS
        raise EmailDestinationError(f"Cannot reach {conn.host}:{conn.port}: {e}") from e


def send_notice(conn: SmtpConnectionSpec, to: list[str], subject: str, body: str) -> dict:
    """A plain-text notice without attachments: it says a flow failed, and must arrive
    even when it is the data itself that is missing."""
    recipients = [a.strip() for a in to if a and a.strip()]
    if not recipients:
        raise EmailDestinationError("No recipients: enter at least one address.")
    if not conn.from_address.strip():
        raise EmailDestinationError("The SMTP connection has no sender address.")

    msg = EmailMessage()
    msg["From"] = conn.sender()
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    # `Auto-Submitted` tells the servers it is mail generated by a program: keeps an
    # automatic reply on the other side from starting an endless bounce
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)

    _send(conn, msg, recipients)
    logger.info("📧 notice sent to %d recipients", len(recipients))
    return {"ok": True, "recipients": len(recipients)}


def send_output_email(conn: SmtpConnectionSpec, spec: EmailSpec, bucket: str, key: str,
                      now: datetime | None = None) -> dict:
    """Makes the attachment and sends it. Raises `EmailDestinationError` with a readable
    message; the caller decides whether the run must fail. The subject
    and the attachment name are resolved first (a bad placeholder sends nothing)."""
    spec = resolve_names(spec, now)
    recipients = [a for a in (*spec.to, *spec.cc) if a.strip()]
    if not recipients:
        raise EmailDestinationError("No recipients: enter at least one address.")
    if not conn.from_address.strip():
        raise EmailDestinationError("The SMTP connection has no sender address.")

    data, file_name = build_attachment(bucket, key, spec)

    msg = EmailMessage()
    msg["From"] = conn.sender()
    msg["To"] = ", ".join(a for a in spec.to if a.strip())
    if spec.cc:
        msg["Cc"] = ", ".join(a for a in spec.cc if a.strip())
    msg["Subject"] = spec.subject or file_name
    msg["Message-ID"] = make_msgid()

    if spec.body_is_html:
        msg.set_content(_html_to_text(spec.body))
        msg.add_alternative(spec.body, subtype="html")
    else:
        msg.set_content(spec.body or "")

    maintype, subtype = _MIME[spec.attachment_format]
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=file_name)

    _send(conn, msg, recipients)

    logger.info("📧 email sent to %d recipients, attachment %s (%d bytes)", len(recipients), file_name, len(data))
    return {
        "ok": True,
        "recipients": len(recipients),
        "attachment": file_name,
        "subject": str(msg["Subject"]),
        "bytes": len(data),
    }


def test_connection(conn: SmtpConnectionSpec) -> None:
    """Tests the connection without sending anything: opens, encrypts and authenticates."""
    if not conn.host.strip():
        raise EmailDestinationError("No SMTP server specified.")
    try:
        with _open(conn) as server:
            if conn.tls == "starttls":
                server.starttls(context=_tls_context())
            if conn.username:
                server.login(conn.username, conn.resolve_secret())
    except ssl.SSLCertVerificationError as e:
        raise _certificate_refused(conn, e) from e
    except smtplib.SMTPAuthenticationError as e:
        raise EmailDestinationError(f"Authentication rejected by {conn.host}: {e}") from e
    except smtplib.SMTPException as e:
        raise EmailDestinationError(f"{conn.host} refused the connection: {type(e).__name__}: {e}") from e
    except OSError as e:
        raise EmailDestinationError(f"Cannot reach {conn.host}:{conn.port}: {e}") from e
