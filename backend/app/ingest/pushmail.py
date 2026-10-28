"""Pushmail: one email with one .xlsx file per datasource and one file per report, the
cell of a flow group that sends data to people (docs/pushmail.md).

Each datasource is read as it is now (the snapshot the steps before the cell left),
cut to its `max_rows` when it has more — the body then says how many rows each file
holds of how many — and written with the export of the Polars engine, so the file is
the one a person would download. Each report is rendered here, in this worker, as the
export queue renders one (app.reports.render: Typst in a child process, with its
limits), from what the gateway resolved with the rights of whoever runs the group: its
definition, its datasets, its parameters converted. One report may also put its
summary in the body (the `html` format: the HTML under the cell's text, its charts as
images by `cid`). The subject and the file names are templates with the Outputs'
placeholders (app.core.placeholders), resolved once for the whole email.

The recipients arrive already checked: the allowed domains of the SMTP connection are
applied by the GATEWAY, as for the email of an Output node (see email_destination).
"""
from __future__ import annotations

import email
import email.policy
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from html import escape
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, Field, model_validator

from app.ingest.email_destination import (
    MAX_ATTACHMENT_BYTES,
    EmailDestinationError,
    SmtpConnectionSpec,
    _html_to_text,
    _send,
)

logger = logging.getLogger(__name__)

XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
PDF_MIME = ("application", "pdf")
MAX_ROWS = 1_000_000  # under the Excel format's own limit (1,048,576 with the header)
MAX_ATTACHMENTS = 20
MAX_REPORTS = 5


class PushmailAttachment(BaseModel):
    bucket: str
    key: str = Field(min_length=1)
    label: str = ""  # the datasource's name, for the note on the rows left out
    file_name: str = Field(min_length=1, max_length=255)  # a template, `.xlsx` added when missing
    max_rows: int = Field(default=10_000, ge=1, le=MAX_ROWS)


class PushmailReport(BaseModel):
    """A report as the gateway resolved it for whoever runs the group: what a render on
    the export queue receives (app.api.routes.reports.ReportRender)."""
    name: str = Field(min_length=1, max_length=300)  # the report's: its title, and the messages
    definition: dict
    datasets: dict[str, dict] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    format: Literal["pdf", "xlsx"] = "pdf"
    file_name: str = Field(min_length=1, max_length=255)  # a template, the extension added when missing
    in_body: bool = False  # its summary in the body, under the cell's text
    locale: Literal["en", "it", "fr", "de", "es"] | None = None


class PushmailSpec(BaseModel):
    to: list[str] = []
    cc: list[str] = []
    subject: str = ""
    body: str = ""
    body_is_html: bool = False
    attachments: list[PushmailAttachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    reports: list[PushmailReport] = Field(default_factory=list, max_length=MAX_REPORTS)
    # the placeholders' zone; the run and the group's name for {run_id} and {flow}
    timezone: str = Field(default="UTC", max_length=64)
    run_id: int | None = None
    flow: str = ""
    # the language of the note on the rows left out and of the names of months and days in the
    # subject and the file names ({today:MMMM}): the one of whoever runs the group
    lang: Literal["en", "it", "fr", "de", "es"] = "en"

    @model_validator(mode="after")
    def _something_to_send(self) -> "PushmailSpec":
        if not self.attachments and not self.reports:
            raise ValueError("nothing to send: no datasource and no report")
        if sum(1 for r in self.reports if r.in_body) > 1:
            raise ValueError("only one report can go in the email's body")
        return self


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


def _file_names(spec: PushmailSpec, now: datetime) -> tuple[str, list[str], list[str]]:
    """The subject and the names of the datasources' and the reports' files, placeholders
    resolved; a name without a folder in it, ending in its extension (.xlsx, a report's
    .pdf), never the same twice in one email."""
    from app.core import placeholders as ph

    try:
        ctx = ph.output_context(now, spec.timezone, spec.run_id, spec.flow, spec.lang)
        subject = ph.render_output(spec.subject, ctx, "text", "subject") if any(c in spec.subject for c in "{}") else spec.subject
        names = [ph.render_output(t, ctx, "file", "file name") if any(c in t for c in "{}") else t
                 for t in [a.file_name for a in spec.attachments] + [r.file_name for r in spec.reports]]
    except ph.PlaceholderError as e:
        raise EmailDestinationError(str(e)) from e
    if "\r" in subject or "\n" in subject:
        raise EmailDestinationError("the subject cannot hold a line break")
    extensions = ["xlsx"] * len(spec.attachments) + [r.format for r in spec.reports]
    out: list[str] = []
    for raw, ext in zip(names, extensions):
        base = (raw or "report").replace("/", "_").replace("\\", "_").strip(". ") or "report"
        base = base[:-len(ext) - 1] if base.lower().endswith(f".{ext}") else base
        name, n = f"{base}.{ext}", 2
        while name.lower() in {x.lower() for x in out}:
            name, n = f"{base} ({n}).{ext}", n + 1
        if len(name.encode()) > 255:
            raise EmailDestinationError(f"the file name «{name[:60]}…» is longer than 255 bytes: shorten it")
        out.append(name)
    return subject, out[:len(spec.attachments)], out[len(spec.attachments):]


def _render(r: PushmailReport, fmt: str, workdir: Path, now: datetime) -> Path:
    """One report rendered here, as the export queue renders it; EmailDestinationError
    naming the report when it cannot be."""
    from app.reports import render
    from app.reports.errors import ReportError

    try:
        return render.render(r.definition, r.datasets, r.params, fmt, workdir, locale=r.locale, title=r.name,
                             now=now).path
    except ReportError as e:
        raise EmailDestinationError(f"the report «{r.name}»: {e.message}") from None


class _Summary(NamedTuple):
    html: str  # what the summary's <body> holds
    text: str  # the same for a mail client without HTML
    images: list[tuple[str, bytes, str]]  # (Content-ID, PNG, file name) each


def _summary(mhtml: bytes) -> _Summary:
    """A report's summary from its MHTML (app.reports.email_html)."""
    doc = email.message_from_bytes(mhtml, policy=email.policy.default)
    html, text, images = "", "", []
    for part in doc.walk():
        if part.get_content_type() == "text/html":
            html = part.get_content()
        elif part.get_content_type() == "text/plain":
            text = part.get_content()
        elif part.get_content_maintype() == "image":
            images.append((str(part["Content-ID"] or "").strip("<>"), part.get_content(), part.get_filename() or "chart.png"))
    m = re.search(r"<body[^>]*>(.*)</body>", html, re.S | re.I)
    return _Summary(m.group(1) if m else html, text.strip() or _html_to_text(html), images)


def _with_summary(body: str, is_html: bool, summary: str) -> str:
    """The cell's text, then the report's summary under it, as one HTML document. A plain
    text sits on the summary's background, aligned with its card (app.reports.email_html)."""
    from app.reports.email_html import FONT, WIDTH

    if is_html:
        at = body.lower().rfind("</body>")
        if at >= 0:  # a whole document: the summary goes at the end of its body
            return body[:at] + summary + body[at:]
        text = body
    else:
        lines = "<br>".join(escape(line) for line in body.rstrip().split("\n"))
        text = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;">'
                f'<tr><td align="center" style="padding:16px 16px 0 16px;">'
                f'<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" style="width:{WIDTH}px;max-width:100%;">'
                f'<tr><td style="padding:4px 0;{FONT}font-size:14px;line-height:1.5;color:#1f2933;">{lines}</td></tr>'
                f'</table></td></tr></table>') if body.strip() else ""
    return (f'<!doctype html>\n<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'</head><body style="margin:0;padding:0;background:#f4f5f7;">{text}{summary}</body></html>')


def _note(cut: list[tuple[str, int, int]], lang: str, html: bool) -> str:
    """What the body gains when files hold fewer rows than their datasource."""
    head, line = _NOTE[lang]
    lines = [line.format(name=name, sent=_number(sent, lang), total=_number(total, lang)) for name, sent, total in cut]
    if html:
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
    now = now or datetime.now(timezone.utc)
    subject, names, report_names = _file_names(spec, now)
    count = len(names) + len(report_names)

    files: list[tuple[str, bytes]] = []
    cut: list[tuple[str, int, int]] = []
    summary: _Summary | None = None
    rows_sent = size = 0

    def add(path: str | Path, n: int) -> bytes:
        # checked file by file: past the limit the rest is not written, nor held in memory
        nonlocal size
        size += os.path.getsize(path)
        if size > MAX_ATTACHMENT_BYTES:
            raise EmailDestinationError(
                f"The files reach {size / 1024 / 1024:.1f} MB at file {n} of {count}, over the "
                f"{MAX_ATTACHMENT_BYTES // 1024 // 1024} MB an email may carry here: lower the rows of each "
                "datasource, or send fewer files. How large a message the SMTP relay accepts, the "
                "administrators know."
            )
        with open(path, "rb") as f:
            return f.read()

    with tempfile.TemporaryDirectory(prefix="pushmail_") as tmp:
        engine = get_engine("polars") if spec.attachments else None
        for i, (a, name) in enumerate(zip(spec.attachments, names)):
            source = DataSource(bucket=a.bucket, key=a.key)
            total = engine.count(source)
            sent = min(total, a.max_rows)
            path = os.path.join(tmp, f"{i}.xlsx")
            engine.export(source=source, operations=[], fmt="xlsx", out_path=path, limit=sent if total > sent else None)
            files.append((name, add(path, i + 1)))
            rows_sent += sent
            if total > sent:
                cut.append((a.label or name, sent, total))
        for i, (r, name) in enumerate(zip(spec.reports, report_names)):
            path = _render(r, r.format, Path(tmp) / f"report-{i}", now)
            files.append((name, add(path, len(names) + i + 1)))
            if r.in_body:
                summary = _summary(add(_render(r, "html", Path(tmp) / f"summary-{i}", now), len(names) + i + 1))

    msg = EmailMessage()
    msg["From"] = conn.sender()
    msg["To"] = ", ".join(a.strip() for a in spec.to if a.strip())
    if any(a.strip() for a in spec.cc):
        msg["Cc"] = ", ".join(a.strip() for a in spec.cc if a.strip())
    msg["Subject"] = subject or ", ".join(names + report_names)
    msg["Message-ID"] = make_msgid()
    body = spec.body or ""
    if cut:
        note = _note(cut, spec.lang, spec.body_is_html)
        body = f"{body}\n{note}" if spec.body_is_html else (f"{body.rstrip()}\n\n{note}" if body.strip() else note)
    if summary is not None:
        # the cell's text first, the report's summary under it; the images inside the HTML
        # part (multipart/related), so a mail client shows them in place, not as files
        plain = _html_to_text(body) if spec.body_is_html else body.rstrip()
        msg.set_content("\n\n".join(t for t in (plain, summary.text) if t))
        msg.add_alternative(_with_summary(body, spec.body_is_html, summary.html), subtype="html")
        html_part = msg.get_payload()[-1]
        for cid, data, filename in summary.images:
            html_part.add_related(data, maintype="image", subtype="png", cid=f"<{cid}>", filename=filename,
                                  disposition="inline")
    elif spec.body_is_html:
        msg.set_content(_html_to_text(body))
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for name, data in files:
        mime = PDF_MIME if name.lower().endswith(".pdf") else XLSX_MIME
        msg.add_attachment(data, maintype=mime[0], subtype=mime[1], filename=name)

    _send(conn, msg, recipients)
    logger.info("📧 pushmail to %d recipients, %d files (%d bytes, %d reports)", len(recipients), len(files), size,
                len(spec.reports))
    return {
        "ok": True,
        "recipients": len(recipients),
        "subject": str(msg["Subject"]),
        "attachments": [{"name": n, "bytes": len(d)} for n, d in files],
        "bytes": size,
        "rows": rows_sent,
        "cut": [{"datasource": n, "sent": s, "total": t} for n, s, t in cut],
    }
