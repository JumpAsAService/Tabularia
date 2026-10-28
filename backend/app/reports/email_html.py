"""A report as the body of an email: its summary as HTML, the charts as images.

What goes in (docs/specs/reports.md, «Email HTML»): the body's texts, the KPIs, the first
table's group totals, in the order of the report; the detail stays in the attached PDF,
because Gmail cuts a message whose HTML passes ~102 KB. Tables with inline styles at 640
px (what Outlook's Word engine and Gmail both draw), the charts as PNG at twice their size
(sharp on any screen), drawn by Typst from the same SVG as the PDF and attached by `cid`.

The file is MHTML: a `multipart/related` MIME document, the HTML first and the images
after it, each with its Content-ID — what an email's HTML part is, and what Chrome and Edge
open as a page. Every text is escaped: a name is never markup.
"""
from __future__ import annotations

import hashlib
from email.charset import QP, Charset
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any

from app.reports import formats
from app.reports.model import Model, TableOut

WIDTH = 640
CONTENT = 592
MAX_HTML_BYTES = 100 * 1024  # under Gmail's ~102 KB, with room for the email's own headers
FONT = "font-family:'IBM Plex Sans',Segoe UI,Arial,sans-serif;"


def _e(text: Any) -> str:
    return escape(str(text), quote=True)


def _text(it: Any, text: str, theme: dict) -> str:
    st = it.style
    size = round((st.size or theme["size"]) * 96 / 72)
    weight = {"bold": 700, "semibold": 600, "normal": 400}.get(st.weight, st.weight) if st.weight else 400
    color = st.color or theme["color"]
    align = st.align or "left"
    italic = "font-style:italic;" if st.italic else ""
    body = "<br>".join(_e(line) for line in text.split("\n"))
    return (f'<tr><td align="{align}" style="padding:6px 24px;{FONT}font-size:{size}px;font-weight:{weight};'
            f'color:{color};{italic}">{body}</td></tr>')


def _kpis(cards: list[dict], theme: dict) -> str:
    cells = []
    for k in cards:
        change = ""
        if k["change"]:
            tone = theme["positive"] if k["change"]["dir"] == "up" else theme["negative"] \
                if k["change"]["dir"] == "down" else theme["muted"]
            arrow = {"up": "↑", "down": "↓"}.get(k["change"]["dir"], "→")
            change = f'<div style="font-size:12px;font-weight:600;color:{tone};">{arrow} {_e(k["change"]["t"])}</div>'
        cells.append(f'<td valign="top" style="padding:12px;background:{theme["band"]};border-radius:6px;{FONT}">'
                     f'<div style="font-size:12px;color:{theme["muted"]};">{_e(k["label"])}</div>'
                     f'<div style="font-size:22px;font-weight:700;color:{theme["color"]};">{_e(k["value"])}</div>'
                     f'{change}</td>')
    gap = '<td width="10" style="width:10px;"></td>'
    return (f'<tr><td style="padding:8px 24px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0"><tr>{gap.join(cells)}</tr></table></td></tr>')


def _image(cid: str, width: int, height: int, alt: str) -> str:
    w = min(width, CONTENT)
    h = round(height * w / width)
    # width and height as attributes: Outlook ignores sizes in CSS
    return (f'<tr><td style="padding:8px 24px;"><img src="cid:{cid}" width="{w}" height="{h}" alt="{_e(alt)}" '
            f'style="display:block;border:0;width:{w}px;max-width:100%;height:auto;"></td></tr>')


def _totals(t: TableOut, theme: dict, loc: formats.Locale, limit: int) -> str:
    """The first table's group totals (its outermost groups) and the grand total."""
    rows = [r for r in t.rows if (r.kind == "f" and r.level == 0) or r.kind == "t"]
    if not rows:
        return ""
    cols = [i for i, c in enumerate(t.cols) if any(r.cells[i].text for r in rows if i < len(r.cells) and i >= r.span)]
    head = "".join(f'<th align="right" style="padding:4px 8px;font-size:12px;color:{theme["muted"]};{FONT}">'
                   f'{_e(t.cols[i].header)}</th>' for i in cols)
    body = []
    shown = [r for r in rows if r.kind == "f"][:limit]
    hidden = len([r for r in rows if r.kind == "f"]) - len(shown)
    for r in shown + [r for r in rows if r.kind == "t"]:
        grand = r.kind == "t"
        style = (f"background:{theme['total']};color:#ffffff;font-weight:700;" if grand
                 else f"border-bottom:1px solid {theme['rule']};")
        cells = "".join(f'<td align="right" style="padding:4px 8px;font-size:13px;{style}{FONT}">'
                        f'{_e(r.cells[i].text if i >= r.span else "")}</td>' for i in cols)
        body.append(f'<tr><td style="padding:4px 8px;font-size:13px;{style}{FONT}">{_e(r.label)}</td>{cells}</tr>')
    more = ""
    if hidden > 0:
        more = (f'<tr><td colspan="{len(cols) + 1}" style="padding:6px 8px;font-size:12px;color:{theme["muted"]};{FONT}">'
                f'{_e(loc.texts["more"].format(n=hidden))}</td></tr>')
    return (f'<tr><td style="padding:12px 24px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;"><tr><th></th>{head}</tr>{"".join(body)}{more}</table></td></tr>')


def html(model: Model, cids: dict[str, str], loc: formats.Locale, limit: int = 200) -> str:
    theme = model.doc["theme"]
    jobs = {j.name: j for j in model.charts}
    parts: list[str] = []
    cards: list[dict] = []
    table_done = False

    def flush() -> None:
        for i in range(0, len(cards), 3):
            parts.append(_kpis(cards[i:i + 3], theme))
        cards.clear()

    for kind, what in model.blocks:
        if kind == "kpi":
            cards.append(what)
            continue
        flush()
        if kind == "text":
            it, text = what
            parts.append(_text(it, text, theme))
        elif kind == "chart":
            it, name = what
            job = jobs[name]
            if name in cids:
                parts.append(_image(cids[name], job.width, job.height, job.alt))
        elif kind == "table" and not table_done:
            parts.append(_totals(what, theme, loc, limit))
            table_done = True
    flush()
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


def build(model: Model, pngs: dict[str, bytes], loc: formats.Locale) -> bytes:
    """The MHTML document: the HTML, then every chart's PNG with its Content-ID."""
    cids = {name: f"{name.split('/')[-1].split('.')[0]}.{hashlib.sha256(data).hexdigest()[:12]}@report.tabularia"
            for name, data in pngs.items()}
    body = html(model, cids, loc)
    limit = 200
    while len(body.encode("utf-8")) > MAX_HTML_BYTES and limit > 5:
        limit //= 2
        body = html(model, cids, loc, limit)
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
    return root.as_bytes()
