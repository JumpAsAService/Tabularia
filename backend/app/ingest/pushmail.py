"""Pushmail: one email with one .xlsx file per datasource, the cell of a flow group
that sends data to people (docs/pushmail.md).

Each datasource is read as it is now (the snapshot the steps before the cell left),
cut to its `max_rows` when it has more — the body then says how many rows each file
holds of how many — and written with the export of the Polars engine, so the file is
the one a person would download. The subject and the file names are templates with
the Outputs' placeholders (app.core.placeholders), resolved once for the whole email.

The recipients arrive already checked: the allowed domains of the SMTP connection are
applied by the GATEWAY, as for the email of an Output node (see email_destination).
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Literal

from pydantic import BaseModel, Field

from app.ingest.email_destination import (
    MAX_ATTACHMENT_BYTES,
    EmailDestinationError,
    SmtpConnectionSpec,
    _html_to_text,
    _spedisci,
)

logger = logging.getLogger(__name__)

XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
MAX_ROWS = 1_000_000  # under the Excel format's own limit (1,048,576 with the header)
MAX_ATTACHMENTS = 20


class PushmailAttachment(BaseModel):
    bucket: str
    key: str = Field(min_length=1)
    label: str = ""  # the datasource's name, for the note on the rows left out
    file_name: str = Field(min_length=1, max_length=255)  # a template, `.xlsx` added when missing
    max_rows: int = Field(default=10_000, ge=1, le=MAX_ROWS)


class PushmailSpec(BaseModel):
    to: list[str] = []
    cc: list[str] = []
    subject: str = ""
    body: str = ""
    body_is_html: bool = False
    attachments: list[PushmailAttachment] = Field(min_length=1, max_length=MAX_ATTACHMENTS)
    # the placeholders' zone; the run and the group's name for {run_id} and {flow}
    timezone: str = Field(default="UTC", max_length=64)
    run_id: int | None = None
    flow: str = ""
    # the language of the note on the rows left out: the one of whoever runs the group
    lang: Literal["en", "it", "fr", "de", "es"] = "en"


# «not every row is attached», and one line per file cut short
_NOTE = {
    "en": ("Not every row is attached:", "{name}: the first {sent} rows of {total}."),
    "it": ("Non tutte le righe sono allegate:", "{name}: le prime {sent} righe su {total}."),
    "fr": ("Toutes les lignes ne sont pas jointes :", "{name} : les {sent} premières lignes sur {total}."),
    "de": ("Nicht alle Zeilen sind angehängt:", "{name}: die ersten {sent} von {total} Zeilen."),
    "es": ("No se adjuntan todas las filas:", "{name}: las primeras {sent} filas de {total}."),
}
_THOUSANDS = {"en": ",", "it": ".", "fr": " ", "de": ".", "es": "."}


def _number(n: int, lang: str) -> str:
    return f"{n:,}".replace(",", _THOUSANDS[lang])


def _file_names(spec: PushmailSpec, now: datetime) -> tuple[str, list[str]]:
    """The subject and the attachments' names, placeholders resolved; a name without a
    folder in it, ending in .xlsx, never the same twice in one email."""
    from app.core import placeholders as ph

    try:
        ctx = ph.output_context(now, spec.timezone, spec.run_id, spec.flow)
        subject = ph.render_output(spec.subject, ctx, "text", "subject") if any(c in spec.subject for c in "{}") else spec.subject
        names = [ph.render_output(a.file_name, ctx, "file", "file name") if any(c in a.file_name for c in "{}") else a.file_name
                 for a in spec.attachments]
    except ph.PlaceholderError as e:
        raise EmailDestinationError(str(e)) from e
    if "\r" in subject or "\n" in subject:
        raise EmailDestinationError("the subject cannot hold a line break")
    out: list[str] = []
    for raw in names:
        base = (raw or "report").replace("/", "_").replace("\\", "_").strip(". ") or "report"
        base = base[:-5] if base.lower().endswith(".xlsx") else base
        name, n = f"{base}.xlsx", 2
        while name.lower() in {x.lower() for x in out}:
            name, n = f"{base} ({n}).xlsx", n + 1
        if len(name.encode()) > 255:
            raise EmailDestinationError(f"the file name «{name[:60]}…» is longer than 255 bytes: shorten it")
        out.append(name)
    return subject, out


def _note(cut: list[tuple[str, int, int]], lang: str, html: bool) -> str:
    """What the body gains when files hold fewer rows than their datasource."""
    head, line = _NOTE[lang]
    lines = [line.format(name=name, sent=_number(sent, lang), total=_number(total, lang)) for name, sent, total in cut]
    if html:
        from html import escape

        return f"<p>{escape(head)}<br>" + "<br>".join(escape(x) for x in lines) + "</p>"
    return head + "\n" + "\n".join(lines)


def send_pushmail(conn: SmtpConnectionSpec, spec: PushmailSpec, now: datetime | None = None) -> dict:
    """Writes the files and sends the email; raises EmailDestinationError with a message
    for the person who set the cell up."""
    from app.engine import DataSource, get_engine

    recipients = [a.strip() for a in (*spec.to, *spec.cc) if a and a.strip()]
    if not recipients:
        raise EmailDestinationError("No recipients: enter at least one address.")
    if not conn.from_address.strip():
        raise EmailDestinationError("The SMTP connection has no sender address.")
    subject, names = _file_names(spec, now or datetime.now(timezone.utc))

    engine = get_engine("polars")
    files: list[tuple[str, bytes]] = []
    cut: list[tuple[str, int, int]] = []
    rows_sent = 0
    with tempfile.TemporaryDirectory(prefix="pushmail_") as tmp:
        for i, (a, name) in enumerate(zip(spec.attachments, names)):
            source = DataSource(bucket=a.bucket, key=a.key)
            total = engine.count(source)
            sent = min(total, a.max_rows)
            path = os.path.join(tmp, f"{i}.xlsx")
            engine.export(source=source, operations=[], fmt="xlsx", out_path=path, limit=sent if total > sent else None)
            with open(path, "rb") as f:
                files.append((name, f.read()))
            rows_sent += sent
            if total > sent:
                cut.append((a.label or name, sent, total))
    size = sum(len(data) for _, data in files)
    if size > MAX_ATTACHMENT_BYTES:
        raise EmailDestinationError(
            f"The {len(files)} files total {size / 1024 / 1024:.1f} MB, over the "
            f"{MAX_ATTACHMENT_BYTES // 1024 // 1024} MB an email may carry here: lower the rows of each "
            "datasource, or send fewer of them. How large a message the SMTP relay accepts, the "
            "administrators know."
        )

    msg = EmailMessage()
    msg["From"] = conn.sender()
    msg["To"] = ", ".join(a.strip() for a in spec.to if a.strip())
    if any(a.strip() for a in spec.cc):
        msg["Cc"] = ", ".join(a.strip() for a in spec.cc if a.strip())
    msg["Subject"] = subject or ", ".join(names)
    msg["Message-ID"] = make_msgid()
    body = spec.body or ""
    if cut:
        note = _note(cut, spec.lang, spec.body_is_html)
        body = f"{body}\n{note}" if spec.body_is_html else (f"{body.rstrip()}\n\n{note}" if body.strip() else note)
    if spec.body_is_html:
        msg.set_content(_html_to_text(body))
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for name, data in files:
        msg.add_attachment(data, maintype=XLSX_MIME[0], subtype=XLSX_MIME[1], filename=name)

    _spedisci(conn, msg, recipients)
    logger.info("📧 pushmail to %d recipients, %d files (%d bytes)", len(recipients), len(files), size)
    return {
        "ok": True,
        "recipients": len(recipients),
        "subject": str(msg["Subject"]),
        "attachments": [{"name": n, "bytes": len(d)} for n, d in files],
        "bytes": size,
        "rows": rows_sent,
        "cut": [{"datasource": n, "sent": s, "total": t} for n, s, t in cut],
    }
