"""A report as the body of an email: its summary as HTML, the charts as images.

What goes in (docs/specs/reports.md, «Email HTML»), in the order of the report's body:

- its texts, its KPI cards and its charts;
- every table and matrix as its totals: the total row of each of its outermost groups (a
  matrix's own rows, when they are its only groups) and its grand total, with the columns
  that have a value — at most five: a wider matrix shows its total column alone;
- a table with nothing to total (no group totals, no grand total) is left out, and with it
  the row of texts right above it that introduces it (its title); a table with no such
  text is introduced by its own title (its sheet's name in Excel), when it has one;
- under Gmail's ~102 KB, which cuts a longer message: each table shows fewer groups, then
  the last tables are left out. Whatever is cut is said where it was, and the detail is in
  the attached PDF or Excel.

Tables with inline styles at 640 px (what Outlook's Word engine and Gmail both draw), the
charts as PNG at twice their size (sharp on any screen), drawn by Typst from the same SVG
as the PDF and attached by `cid`; the same summary as plain text, for a mail client that
shows no HTML.

The file is MHTML: a `multipart/related` MIME document, the HTML first, then the images,
each with its Content-ID, and the plain text — what an email's HTML part is, and what
Chrome and Edge open as a page. Every text is escaped: a name is never markup.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from email.charset import QP, Charset
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any

from app.reports import definition as rd
from app.reports import formats
from app.reports.model import Model, Row, TableOut

WIDTH = 640
CONTENT = 592
MAX_HTML_BYTES = 100 * 1024  # under Gmail's ~102 KB, with room for the email's own headers
MAX_VALUE_COLUMNS = 5  # what 592 px hold beside a label, at 13 px
ROW_LIMITS = (200, 100, 50, 25, 12, 6)  # the groups a table shows, fewer until the body fits
KPI_WIDTH = (CONTENT - 2 * 10) // 3 - 2 * 12  # px: three cards and their gaps, without their padding
FONT = "font-family:'IBM Plex Sans',Segoe UI,Arial,sans-serif;"
TEXT_PART = "summary.txt"


def _e(text: Any) -> str:
    return escape(str(text), quote=True)


# ── what the summary holds ───────────────────────────────────────────────────
@dataclass
class _Totals:
    """A table's summary: the label column's header, the values' headers, the rows as
    (label, texts, grand total), and how many value columns did not fit."""
    corner: str
    headers: list[str]
    rows: list[tuple[str, list[str], bool]]
    columns_left: int = 0

    @property
    def groups(self) -> int:
        return sum(1 for _, _, grand in self.rows if not grand)


@dataclass
class _Piece:
    kind: str  # text (style, text), kpis, chart, table
    what: Any = None
    captions: list[tuple[rd.Style, str]] = field(default_factory=list)  # a table's title texts, (style, text)


TITLE = rd.Style(size=10, weight="bold")  # a table's own title, when no text above it says what it is


def _label(r: Row) -> str:
    return r.cells[0].text if r.kind == "d" else r.label


def totals(t: TableOut) -> _Totals | None:
    """The summary of a table or a matrix; None when it has nothing to total."""
    matrix = t.item.matrix is not None
    if matrix and len(t.item.groups) <= 1:
        rows = [r for r in t.rows if r.kind in ("d", "t")]  # a matrix's rows are its groups' totals
    else:
        outer = min((r.level for r in t.rows if r.kind == "f"), default=None)
        rows = [r for r in t.rows if (r.kind == "f" and r.level == outer) or r.kind == "t"]
    if not rows:
        return None

    def first(r: Row) -> int:
        return max(r.span, 1 if matrix else 0)

    cols = [i for i in range(len(t.cols)) if any(i >= first(r) and i < len(r.cells) and r.cells[i].text for r in rows)]
    shown = cols
    if len(cols) > MAX_VALUE_COLUMNS:
        total_col = len(t.cols) - 1
        shown = [total_col] if matrix and t.item.matrix.total and total_col in cols else cols[:MAX_VALUE_COLUMNS]
    out = [(_label(r), [r.cells[i].text if i >= first(r) else "" for i in shown], r.kind == "t") for r in rows]
    return _Totals(t.cols[0].header if matrix else "", [t.cols[i].header for i in shown], out, len(cols) - len(shown))


def _pieces(model: Model) -> list[_Piece]:
    """The body's blocks as the summary shows them: the KPIs of a row together, every
    table with its title texts, the tables with nothing to total gone with theirs."""
    out: list[_Piece] = []
    captions: list[tuple[rd.Style, str]] = []
    for kind, what in model.blocks:
        if kind == "caption":
            if what[1].strip():
                captions.append((what[0].style, what[1]))
            continue
        if kind == "table":
            summary = totals(what)
            if summary is not None:
                titled = [(TITLE, what.item.title)] if what.item.title else []
                out.append(_Piece("table", summary, captions or titled))
            captions = []
            continue
        if kind == "kpi":
            if out and out[-1].kind == "kpis":
                out[-1].what.append(what)
            else:
                out.append(_Piece("kpis", [what]))
        elif kind == "text":
            if what[1].strip():
                out.append(_Piece("text", (what[0].style, what[1])))
        else:
            out.append(_Piece(kind, what))
    return out


def _cut(pieces: list[_Piece], tables: int) -> tuple[list[_Piece], int]:
    """The pieces with only the first `tables` tables, and how many were left out."""
    kept, seen = [], 0
    for p in pieces:
        if p.kind == "table":
            seen += 1
            if seen > tables:
                continue
        kept.append(p)
    return kept, max(seen - tables, 0)


# ── HTML ─────────────────────────────────────────────────────────────────────
def _text(st: rd.Style, text: str, theme: dict) -> str:
    size = round((st.size or theme["size"]) * 96 / 72)
    weight = {"bold": 700, "semibold": 600, "normal": 400}.get(st.weight, st.weight) if st.weight else 400
    color = st.color or theme["color"]
    align = st.align or "left"
    italic = "font-style:italic;" if st.italic else ""
    body = "<br>".join(_e(line) for line in text.split("\n"))
    return (f'<tr><td align="{align}" style="padding:6px 24px;{FONT}font-size:{size}px;font-weight:{weight};'
            f'color:{color};{italic}">{body}</td></tr>')


def _arrow(change: dict) -> str:
    return {"up": "↑", "down": "↓"}.get(change["dir"], "→")


def _kpis(cards: list[dict], theme: dict) -> str:
    cells = []
    for k in cards:
        change = ""
        if k["change"]:
            tone = theme["positive"] if k["change"]["dir"] == "up" else theme["negative"] \
                if k["change"]["dir"] == "down" else theme["muted"]
            change = (f'<div style="font-size:12px;font-weight:600;color:{tone};">{_arrow(k["change"])} '
                      f'{_e(k["change"]["t"])}</div>')
        # a third of the row each, as the cards of a page usually are: not as wide as their value
        cells.append(f'<td valign="top" width="{KPI_WIDTH}" style="width:{KPI_WIDTH}px;padding:12px;'
                     f'background:{theme["band"]};border-radius:6px;{FONT}">'
                     f'<div style="font-size:12px;color:{theme["muted"]};">{_e(k["label"])}</div>'
                     f'<div style="font-size:22px;font-weight:700;color:{theme["color"]};">{_e(k["value"])}</div>'
                     f'{change}</td>')
    gap = '<td width="10" style="width:10px;"></td>'
    rest = "<td></td>" if len(cards) < 3 else ""
    return (f'<tr><td style="padding:8px 24px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0"><tr>{gap.join(cells)}{rest}</tr></table></td></tr>')


def _image(cid: str, width: int, height: int, alt: str) -> str:
    w = min(width, CONTENT)
    h = round(height * w / width)
    # width and height as attributes: Outlook ignores sizes in CSS
    return (f'<tr><td style="padding:8px 24px;"><img src="cid:{cid}" width="{w}" height="{h}" alt="{_e(alt)}" '
            f'style="display:block;border:0;width:{w}px;max-width:100%;height:auto;"></td></tr>')


def _note(text: str, theme: dict, colspan: int = 1) -> str:
    return (f'<tr><td colspan="{colspan}" style="padding:6px 8px;font-size:12px;color:{theme["muted"]};{FONT}">'
            f'{_e(text)}</td></tr>')


def _plural(loc: formats.Locale, what: str, n: int) -> str:
    return loc.texts[f"more_{what}" if n == 1 else f"more_{what}s"].format(n=n)


def _totals_html(s: _Totals, theme: dict, loc: formats.Locale, limit: int) -> str:
    head = "".join(f'<th align="right" style="padding:4px 8px;font-size:12px;color:{theme["muted"]};{FONT}">'
                   f'{_e(h)}</th>' for h in s.headers)
    corner = (f'<th align="left" style="padding:4px 8px;font-size:12px;color:{theme["muted"]};{FONT}">'
              f'{_e(s.corner)}</th>')
    rows, hidden = _shown(s, limit)
    body = []
    for label, texts, grand in rows:
        style = (f"background:{theme['total']};color:#ffffff;font-weight:700;" if grand
                 else f"border-bottom:1px solid {theme['rule']};")
        cells = "".join(f'<td align="right" style="padding:4px 8px;font-size:13px;{style}{FONT}">{_e(t)}</td>'
                        for t in texts)
        body.append(f'<tr><td style="padding:4px 8px;font-size:13px;{style}{FONT}">{_e(label)}</td>{cells}</tr>')
    notes = ""
    if hidden:
        more = loc.texts["more"].format(n=formats.format_number(hidden, "#,##0", loc))
        notes += _note(more, theme, len(s.headers) + 1)
    if s.columns_left:
        notes += _note(_plural(loc, "column", s.columns_left), theme, len(s.headers) + 1)
    return (f'<tr><td style="padding:12px 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;"><tr>{corner}{head}</tr>{"".join(body)}{notes}</table></td></tr>')


def _shown(s: _Totals, limit: int) -> tuple[list[tuple[str, list[str], bool]], int]:
    """The rows a table shows within `limit` groups (the grand total always), and how many
    groups are left out."""
    groups = [r for r in s.rows if not r[2]][:limit]
    return groups + [r for r in s.rows if r[2]], s.groups - len(groups)


def html(model: Model, cids: dict[str, str], loc: formats.Locale, limit: int = ROW_LIMITS[0],
         tables: int | None = None) -> str:
    """The summary as an HTML document: at most `limit` groups per table, and the first
    `tables` tables (all of them by default)."""
    theme = model.doc["theme"]
    jobs = {j.name: j for j in model.charts}
    pieces, left = _cut(_pieces(model), len(model.blocks) if tables is None else tables)
    parts: list[str] = []
    for p in pieces:
        if p.kind == "text":
            parts.append(_text(*p.what, theme))
        elif p.kind == "kpis":
            parts.extend(_kpis(p.what[i:i + 3], theme) for i in range(0, len(p.what), 3))
        elif p.kind == "chart":
            name = p.what[1]
            if name in cids:
                job = jobs[name]
                parts.append(_image(cids[name], job.width, job.height, job.alt))
        elif p.kind == "table":
            parts.extend(_text(st, text, theme) for st, text in p.captions)
            parts.append(_totals_html(p.what, theme, loc, limit))
    if left:
        parts.append(f'<tr><td style="padding:6px 24px;">'
                     f'<table role="presentation" width="100%">{_note(_plural(loc, "table", left), theme)}</table>'
                     f'</td></tr>')
    return (f'<!doctype html>\n<html lang="{loc.code}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width"><title>{_e(model.title)}</title></head>'
            f'<body style="margin:0;padding:0;background:#f4f5f7;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;">'
            f'<tr><td align="center" style="padding:16px;">'
            f'<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" '
            f'style="width:{WIDTH}px;max-width:100%;background:#ffffff;">'
            f'<tr><td style="padding:20px 24px 8px 24px;{FONT}font-size:20px;font-weight:700;color:{theme["color"]};">'
            f'{_e(model.title)}</td></tr>{"".join(parts)}'
            f'<tr><td style="padding:12px 24px 20px 24px;{FONT}font-size:11px;color:{theme["muted"]};">Tabularia</td></tr>'
            f'</table></td></tr></table></body></html>')


# ── plain text ───────────────────────────────────────────────────────────────
def text(model: Model, loc: formats.Locale, limit: int = ROW_LIMITS[0], tables: int | None = None) -> str:
    """The same summary for a mail client without HTML: a table's row is its label, then
    each column's header and value («Total North — Qty 12 · Revenue 3,400.00»)."""
    pieces, left = _cut(_pieces(model), len(model.blocks) if tables is None else tables)
    out: list[str] = [model.title] if model.title else []
    for p in pieces:
        if p.kind == "text":
            out.append(p.what[1])
        elif p.kind == "kpis":
            out.append("\n".join(f"{k['label']}: {k['value']}" + (f" ({_arrow(k['change'])} {k['change']['t']})"
                                                                   if k["change"] else "") for k in p.what))
        elif p.kind == "table":
            s = p.what
            rows, hidden = _shown(s, limit)
            lines = [t for _, t in p.captions]
            for label, texts, _ in rows:
                values = " · ".join(f"{h} {t}".strip() for h, t in zip(s.headers, texts) if t)
                lines.append(f"{label} — {values}" if label and values else label or values)
            if hidden:
                lines.append(loc.texts["more"].format(n=formats.format_number(hidden, "#,##0", loc)))
            if s.columns_left:
                lines.append(_plural(loc, "column", s.columns_left))
            out.append("\n".join(lines))
    if left:
        out.append(_plural(loc, "table", left))
    return "\n\n".join(x for x in out if x.strip()) + "\n"


# ── the MHTML ────────────────────────────────────────────────────────────────
def build(model: Model, pngs: dict[str, bytes], loc: formats.Locale) -> bytes:
    """The MHTML document: the HTML, every chart's PNG with its Content-ID, the plain text."""
    cids = {name: f"{name.split('/')[-1].split('.')[0]}.{hashlib.sha256(data).hexdigest()[:12]}@report.tabularia"
            for name, data in pngs.items()}
    tables = sum(1 for kind, _ in model.blocks if kind == "table")

    def fits(body: str) -> bool:
        return len(body.encode("utf-8")) <= MAX_HTML_BYTES

    # fewer groups per table, then fewer tables, until the body is under the limit
    attempts = [(limit, tables) for limit in ROW_LIMITS] + [(ROW_LIMITS[-1], n) for n in range(tables - 1, -1, -1)]
    for limit, kept in attempts:
        body = html(model, cids, loc, limit, kept)
        if fits(body):
            break
    charset = Charset("utf-8")
    charset.body_encoding = QP
    boundary = "tabularia-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
    root = MIMEMultipart("related", boundary=boundary, type="text/html")
    root["Subject"] = model.title
    root.attach(MIMEText(body, "html", charset))
    for name, data in pngs.items():
        img = MIMEImage(data, "png")
        img.add_header("Content-ID", f"<{cids[name]}>")
        img.add_header("Content-Disposition", "inline", filename=name.split("/")[-1].replace(".svg", ".png"))
        root.attach(img)
    plain = MIMEText(text(model, loc, limit, kept), "plain", charset)
    plain.add_header("Content-Location", TEXT_PART)
    root.attach(plain)
    return root.as_bytes()
