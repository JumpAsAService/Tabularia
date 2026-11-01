"""A dashboard as a document, PDF or PNG (docs/specs/dashboards.md, «Going out»): a faithful record
of its numbers, not a screenshot, so the look is a report's and the layout what a sheet of paper
can do with a grid.

The numbers are the screen's because nothing is worked out twice: `tiles.Reading` reads the datasets
and says which tile is refused or stopped, `tiles._Tiles` gives a tile its rows with the clicks of
the moment, on both ways of computing, and the report's model writes them. Here only where they go:

- the 12 columns fill the width of the page; a row is as high, for its width, as on a screen of
  REFERENCE_PX;
- tiles whose rows overlap stand together in a row (`bands`), which a page break never cuts: a row
  too tall for a page is cut between tiles, and a tile too tall is made to fit it. A table takes its
  row alone, and the whole width, and goes on over the pages. Hidden tiles take no place;
- a table's columns take the width by what they hold, and those that do not fit go on in another
  table, with the first column again (`fit_columns`, `slice_table`): a number is never written over
  the next one;
- a tile that cannot say its numbers says why in its place, in the document's language; of a dataset
  the reader may not read, that it cannot and nothing else. `strict`, for what nobody looks at before
  it leaves (a Pushmail cell): it stops the document instead.

An image is the same document on one long page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from app.core import placeholders as ph
from app.reports import charts, formats, glyphs, pdf
from app.reports import data as rdata
from app.reports import definition as rd
from app.reports import model as rmodel
from app.reports import tiles as rtiles
from app.reports.errors import ReportError
from app.reports.render import Limits, Rendered, _write

FORMATS = ("pdf", "png")
DEFAULT_PAGE = {"size": "A4", "orientation": "landscape", "margins": [10.0, 10.0, 10.0, 10.0]}
# the grid (frontend/composables/dashboardGrid.ts), and the width its tiles were drawn to be read at
COLUMNS, ROW_PX, GAP_PX, REFERENCE_PX = 12, 40, 10, 1280
FOOTER_MM = 5.0
TITLE_MM = 5.0  # the title of a text or an image, over it, as on a screen
NOTE_ROWS = 3  # a table that has nothing to show does not keep its height
IMAGE_PPI = 192  # as the charts of an email (at 144 a glyph of a chart came out cut, once: look at the images)
ROW_MM = 5.4  # a table's row of one line in the template, for what the pages can hold
LONGEST = 40  # the longest values of a column are those measured

# what a tile says that has nothing to show, in the document's language (the viewer says the same:
# frontend/i18n, dashboard.err_*)
REFUSED = {"en": "You do not have access to the data of this tile.", "it": "Non hai accesso ai dati di questo riquadro.",
           "de": "Sie haben keinen Zugriff auf die Daten dieser Kachel.", "es": "No tienes acceso a los datos de este panel.",
           "fr": "Vous n'avez pas accès aux données de cette tuile."}
FAILED = {"en": "This tile could not be shown", "it": "Questo riquadro non si può mostrare", "de": "Diese Kachel kann nicht angezeigt werden",
          "es": "Este panel no se puede mostrar", "fr": "Cette tuile ne peut pas être affichée"}
_LEADS = {"truncated": {"en": "Too many rows to show here", "it": "Troppe righe da mostrare qui", "de": "Zu viele Zeilen zum Anzeigen",
                        "es": "Demasiadas filas para mostrar aquí", "fr": "Trop de lignes à afficher ici"},
          "dataset": {"en": "The data of this tile is not available", "it": "I dati di questo riquadro non sono disponibili",
                      "de": "Die Daten dieser Kachel sind nicht verfügbar", "es": "Los datos de este panel no están disponibles",
                      "fr": "Les données de cette tuile ne sont pas disponibles"}}
_DATA_FROM = {"en": "Data from {time}", "it": "Dati del {time}", "de": "Daten vom {time}", "es": "Datos del {time}", "fr": "Données du {time}"}


# ── the room ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Tile:
    """A tile on the grid."""
    id: str
    type: str
    x: int
    y: int
    w: int
    h: int
    title: str = ""


@dataclass(frozen=True)
class Sheet:
    """What the page leaves between its margins, in mm, and the grid on it. `height` None: one long page."""
    page: rd.Page
    page_w: float
    page_h: float
    width: float
    height: float | None

    @classmethod
    def of(cls, page: dict | None, long: bool = False) -> "Sheet":
        try:
            chosen = rd.Page(**{**DEFAULT_PAGE, **(page or {})})
        except ValidationError:
            raise ReportError("the dashboard's page cannot be read: look at its size and its margins", "bad_request") from None
        w, h = chosen.dimensions()
        top, right, bottom, left = chosen.margins
        return cls(chosen, w, h, w - left - right, None if long else h - top - bottom - FOOTER_MM)

    @property
    def scale(self) -> float:
        return self.width / REFERENCE_PX

    @property
    def gap(self) -> float:
        return GAP_PX * self.scale

    @property
    def row(self) -> float:
        return ROW_PX * self.scale

    @property
    def column(self) -> float:
        return (self.width - (COLUMNS - 1) * self.gap) / COLUMNS

    def left_of(self, t: Tile) -> float:
        return t.x * (self.column + self.gap)

    def width_of(self, t: Tile) -> float:
        return t.w * self.column + (t.w - 1) * self.gap

    def height_of(self, t: Tile) -> float:
        return self.rows_mm(t.h)

    def rows_mm(self, rows: int) -> float:
        return rows * self.row + (rows - 1) * self.gap


@dataclass(frozen=True)
class Place:
    tile: Tile
    x: float
    y: float  # from the top of its row
    w: float
    h: float


@dataclass(frozen=True)
class Band:
    height: float
    places: list[Place]


def closed_rows(tiles: list[Tile]) -> list[Tile]:
    """The rows no tile holds, those only hidden tiles held among them, closed up."""
    held = {t.y + i for t in tiles for i in range(t.h)}
    gone = [row for row in range(max(held, default=-1) + 1) if row not in held]
    return [Tile(t.id, t.type, t.x, t.y - sum(1 for row in gone if row < t.y), t.w, t.h, t.title) for t in tiles]


def _overlapping(tiles: list[Tile]) -> list[list[Tile]]:
    """The tiles, in the order they are read, by groups whose rows overlap."""
    groups: list[list[Tile]] = []
    bottom = -1
    for t in sorted(tiles, key=lambda t: (t.y, t.x)):
        if t.y >= bottom:
            groups.append([])
        groups[-1].append(t)
        bottom = max(bottom, t.y + t.h)
    return groups


def _band(group: list[Tile], sheet: Sheet, shift: int) -> Band:
    top = min(t.y for t in group)
    places = []
    for t in group:
        h = sheet.height_of(t)
        places.append(Place(t, sheet.left_of(Tile(t.id, t.type, t.x - shift, t.y, t.w, t.h)), (t.y - top) * (sheet.row + sheet.gap), sheet.width_of(t),
                            min(h, sheet.height) if sheet.height is not None else h))
    return Band(max(p.y + p.h for p in places), places)


def _fitting(group: list[Tile], sheet: Sheet, shift: int) -> list[Band]:
    """One row of tiles, or more when it is taller than a page: cut between tiles, never through one."""
    if sheet.height is None or sheet.rows_mm(max(t.y + t.h for t in group) - min(t.y for t in group)) <= sheet.height:
        return [_band(group, sheet, shift)]
    out: list[Band] = []
    now: list[Tile] = []
    top = bottom = 0
    for t in group:
        reach = max(bottom, t.y + t.h)
        if now and sheet.rows_mm(reach - top) > sheet.height:
            out.append(_band(now, sheet, shift))
            now, reach = [], t.y + t.h
        if not now:
            top = t.y
        now.append(t)
        bottom = reach
    out.append(_band(now, sheet, shift))
    return out


def bands(tiles: list[Tile], sheet: Sheet) -> list[Band]:
    """The rows of a document, in the order they are read. A table takes its row alone, and the whole
    width; what stood beside it moves over the hole it leaves."""
    out: list[Band] = []
    for group in _overlapping(tiles):
        beside = [t for t in group if t.type != "table"]
        shift = min((t.x for t in beside), default=0) if len(beside) < len(group) else 0
        run: list[Tile] = []

        def close() -> None:
            for part in _overlapping(run):
                out.extend(_fitting(part, sheet, shift))
            run.clear()

        for t in group:
            if t.type == "table":
                close()
                out.append(Band(sheet.height_of(t), [Place(t, 0.0, 0.0, sheet.width, sheet.height_of(t))]))
            else:
                run.append(t)
        close()
    return out


# ── a table's columns ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Part:
    """Some of a table's columns, and how wide each is."""
    columns: list[int]
    widths: list[float]


def _shared(needs: list[float], naturals: list[float], room: float) -> list[float]:
    """Each column what it needs, then what would save it wrapping, then the same share of the rest."""
    total = sum(needs)
    if total >= room:
        return [w * room / total for w in needs]
    wants = [max(n - w, 0.0) for w, n in zip(needs, naturals)]
    left = room - total
    given = min(left, sum(wants))
    widths = [w + (given * want / sum(wants) if given else 0.0) for w, want in zip(needs, wants)]
    left -= given
    return [w + left * w / sum(widths) for w in widths] if left > 0 else widths


def fit_columns(needs: list[float], naturals: list[float], room: float) -> list[Part]:
    """`needs`: what a column cannot do without (its longest word), `naturals`: its longest value on
    one line. The columns past the room go on in another table, the first one with them: it names
    the row."""
    if sum(needs) <= room or len(needs) < 2:
        return [Part(list(range(len(needs))), _shared(needs, naturals, room))]
    first = min(needs[0], room * 0.4)
    parts, now, used = [], [0], first
    for j in range(1, len(needs)):
        w = min(needs[j], room - first)
        if used + w > room + 1e-6:
            parts.append(now)
            now, used = [0], first
        now.append(j)
        used += w
    parts.append(now)
    trimmed = [first, *needs[1:]]
    return [Part(cols, _shared([min(trimmed[j], room - first) if j else first for j in cols], [naturals[j] for j in cols], room)) for cols in parts]


def _head_cells(head: list[list[dict]]) -> list[tuple[int, dict, list[int]]]:
    """Each cell of a table's head with its level and the columns it covers: one that spans levels
    keeps its columns from those under it."""
    held: dict[int, int] = {}
    out = []
    for level, row in enumerate(head):
        at = 0
        for cell in row:
            while held.get(at, 0) > level:
                at += 1
            columns = list(range(at, at + cell["span"]))
            for c in columns:
                held[c] = level + cell.get("rspan", 1)
            out.append((level, cell, columns))
            at += cell["span"]
    return out


def slice_table(t: dict, keep: list[int], widths: list[float]) -> dict:
    """A table of the template, with only these columns of its own."""
    kept = set(keep)
    head: list[list[dict]] = [[] for _ in t["head"]]
    for level, cell, columns in _head_cells(t["head"]):
        span = len(kept.intersection(columns))
        if span:
            head[level].append({**cell, "span": span})
    rows = []
    for r in t["rows"]:
        if r["k"] == "d":
            rows.append({**r, "c": [r["c"][j] for j in keep]})
        elif r["k"] in ("f", "t"):
            span = r["span"]
            rows.append({**r, "span": sum(1 for j in keep if j < span), "c": [r["c"][j - span] for j in keep if j >= span]})
        else:
            rows.append(r)
    return {**t, "head": [row for row in head if row], "rows": rows, "cols": [{**t["cols"][j], "w": w} for j, w in zip(keep, widths)]}


def _measured(t: rmodel.TableOut, size: float) -> tuple[list[float], list[float]]:
    """(the longest word, the longest value) of each column, in mm with the cell's padding: on the
    longest values of each, as a table can have many rows."""
    n = len(t.cols)
    texts: list[dict[str, tuple[float, float]]] = [{} for _ in range(n)]
    for _, cell, columns in _head_cells(t.head):
        if len(columns) == 1 and columns[0] < n:
            texts[columns[0]][cell["t"]] = (size, 600)
    for row in t.rows:
        if row.kind not in ("d", "f", "t"):
            continue
        for j, cell in enumerate(row.cells[:n]):
            if row.kind != "d" and j < row.span:
                continue
            st = {**t.cols[j].style, **(cell.style or {})}
            texts[j][cell.text] = (st.get("size", size), 700 if row.kind != "d" else st.get("weight", 400))
    needs, naturals = [], []
    padding = 2 * rmodel.CELL_PAD + 0.4
    for j, found in enumerate(texts):
        longest = sorted(found, key=len, reverse=True)[:LONGEST]
        word = max((glyphs.width_mm(w, *found[text]) for text in longest for w in text.split()), default=0.0)
        whole = max((glyphs.width_mm(text, *found[text]) for text in longest), default=0.0)
        own = t.cols[j].width or 0.0
        needs.append(max(word + padding, own, rmodel.MIN_SHARE))
        naturals.append(max(whole + padding, needs[-1]))
    return needs, naturals


# ── the document ─────────────────────────────────────────────────────────────
class _Printed(rtiles._Tiles):
    """A tile's rows and numbers, as on the screen; its columns are measured here (`_measured`)."""

    def column_widths(self, t: rmodel.TableOut) -> list[float | None]:
        return [c.width for c in t.cols]


def _text(x: float, y: float, w: float, h: float, parts: list, **style: Any) -> dict:
    return {"k": "text", "x": rmodel._round_mm(x), "y": rmodel._round_mm(y), "w": rmodel._round_mm(w), "h": rmodel._round_mm(h), "st": style, "parts": parts}


def _noted(answer: dict, x: float, y: float, w: float, h: float, lang: str) -> dict:
    """What a tile says instead of its numbers. Of a dataset refused, that it is, and nothing else:
    not even the sentence that came with it, which names the dataset."""
    code = answer.get("code")
    parts = [REFUSED[lang]] if code == "no_access" else [_LEADS.get(code, FAILED)[lang], "\n", str(answer.get("error"))[:500]]
    return _text(x, y, w, h, parts, size=7.5, color=rmodel.PALETTE["muted"], fill=rmodel.PALETTE["head"], pad=2.0, valign="horizon", align="center")


def _click_values(b: _Printed, values: list) -> str:
    said = [" · ".join(b.name(v) for v in value) if isinstance(value, list) else b.name(value) for value in values[:10]]
    return ", ".join(said) + (f" … +{len(values) - 10}" if len(values) > 10 else "")


def _heading(r: rtiles.Reading, b: _Printed, title: str, names: dict[str, str], sheet: Sheet) -> dict:
    """The document's name, and what it is showing: the parameters, the clicks, when the rows are from."""
    loc, lang = r.loc, r.report.locale
    shown = [f"{p.label or p.name}: {formats.display(r.values.get(p.name), None, loc)}" for p in r.report.parameters
             if r.values.get(p.name) not in (None, [], "")]
    clicked = []
    for c in r.usable:
        attempted, failure = rtiles.attempt(c.tile, lambda c=c: b.chosen(c.tile).get(c.on))
        if failure is None and attempted:
            clicked.append(f"{names.get(c.tile) or c.tile}: {_click_values(b, attempted)}")
    read = [d.read_at or r.moment for d in r.data.values()]
    moment = min(read, default=r.moment).astimezone(ph.zone(r.report.timezone))
    items = [_text(0, 0, sheet.width * 0.7, 7, [title], size=14, weight=700, color=r.report.theme.color),
             _text(sheet.width * 0.7, 2, sheet.width * 0.3, 5, [_DATA_FROM[lang].format(time=formats.display(moment, None, loc))], size=7.5,
                   color=rmodel.PALETTE["muted"], align="right")]
    height = 8.0
    for line in (" · ".join(shown), " · ".join(clicked)):
        if line:
            items.append(_text(0, height, sheet.width, 4, [line], size=8, color=rmodel.PALETTE["muted"], grow=True))
            height += 4.5
    return {"k": "band", "gap": 0, "h": rmodel._round_mm(height), "items": items}


def _footer(title: str, loc: formats.Locale, sheet: Sheet) -> dict | None:
    if sheet.height is None:
        return None
    parts: list = []
    for piece in re.split(r"(\{page\}|\{pages\})", loc.texts["page"]):
        if piece:
            parts.append({"page": 1} if piece == "{page}" else {"pages": 1} if piece == "{pages}" else piece)
    style = {"size": 7, "color": rmodel.PALETTE["muted"]}
    return {"h": FOOTER_MM, "last": True, "items": [_text(0, 1, sheet.width * 0.7, 4, [title], **style),
                                                    _text(sheet.width * 0.7, 1, sheet.width * 0.3, 4, parts, **style, align="right")]}


def _table_blocks(b: _Printed, out: rmodel.TableOut, title: str, sheet: Sheet) -> list[dict]:
    """A table, as one table of the template per part of its columns; the title stays with its rows,
    in the head that comes back on every page."""
    it = out.item
    it.x, it.w = 0.0, sheet.width
    whole = b.typst_table(out)
    needs, naturals = _measured(out, b.r.theme.size)
    blocks = []
    for i, part in enumerate(fit_columns(needs, naturals, sheet.width)):
        block = slice_table(whole, part.columns, [rmodel._round_mm(w) for w in part.widths])
        if title:
            block["head"] = [[{"t": title, "span": len(part.columns), "rspan": 1, "align": "left"}], *block["head"]]
        blocks.append({"k": "table", "gap": rmodel._round_mm(sheet.gap), **block, "sorted": None})
    return blocks


def _document(r: rtiles.Reading, store: Any, tiles: list[dict], state: dict | None, title: str, sheet: Sheet, strict: bool,
              limits: Limits) -> rmodel.Model:
    r.report.page = sheet.page
    b = r.builder(store, state, interactive=False, layout=True, max_pages=limits.max_pages, cls=_Printed)
    grid = {t.get("id"): t for t in tiles}
    lang = r.report.locale
    names = {it.id: grid.get(it.id, {}).get("title") or getattr(it, "label", "") or it.id for it in r.items}

    def stops(it: Any, answer: dict) -> dict:
        if strict:
            raise ReportError(f"the tile «{names[it.id]}»: {answer.get('error')}", "unprocessable")
        return answer

    def shows(it: Any) -> bool:
        b.ctx.dataset = b.item_dataset(it)
        return b.visible(it)

    shown: list[Tile] = []
    notes: dict[str, dict] = {}
    tables: dict[str, rmodel.TableOut] = {}
    most = int(limits.max_pages * (sheet.height if sheet.height is not None else sheet.page_h) / ROW_MM)
    for it in r.items:
        if it.type == "comment":
            continue
        place = grid.get(it.id) or {}
        g = place.get("grid") or {"x": 0, "y": len(shown) * 6, "w": 6, "h": 6}
        b.tile = it.id
        kind, height = it.type, g["h"]
        stopped = r.stopped(it)
        if stopped is None:
            if it.type == "table":
                out, stopped = rtiles.attempt(it.id, lambda it=it: b.either(it, b._visible_table, it))
                if stopped is None and out is None:
                    continue
                if stopped is None and len(out.rows) > most:
                    stopped = rtiles._failure(f"{len(out.rows):,} rows to show, more than {limits.max_pages} pages hold: "
                                              "show its groups' totals without the detail, or filter it")
                if stopped is None:
                    tables[it.id] = out
            else:
                visible, stopped = rtiles.attempt(it.id, lambda it=it: b.either(it, shows, it))
                if stopped is None and (not visible or (it.type == "image" and not it.asset)):
                    continue
        if stopped is not None:
            notes[it.id] = stops(it, stopped)
            if it.type == "table":
                kind, height = "note", min(height, NOTE_ROWS)
        shown.append(Tile(it.id, kind, g["x"], g["y"], g["w"], height, place.get("title") or ""))

    by_id = {it.id: it for it in r.items}
    body = [_heading(r, b, title, names, sheet)]
    for band in bands(closed_rows(shown), sheet):
        items: list[dict] = []
        for p in band.places:
            t, it = p.tile, by_id[p.tile.id]
            if t.id in tables:
                body.extend(_table_blocks(b, tables[t.id], t.title, sheet))
                continue
            # the title of what does not write one of its own
            top = 0.0
            if t.title and (it.type in ("text", "image") or t.id in notes):
                top = min(TITLE_MM, p.h / 3)
                items.append(_text(p.x, p.y, p.w, top, [t.title], size=8, weight=600))
            if t.id in notes:
                items.append(_noted(notes[t.id], p.x, p.y + top, p.w, p.h - top, lang))
                continue
            it.x, it.y, it.w, it.h = p.x, p.y + top, p.w, p.h - top
            if it.type == "kpi" and not it.label:
                it.label = t.title
            b.tile = it.id
            done, failure = rtiles.attempt(it.id, lambda it=it: b.placed(it))
            if failure is not None:
                items.append(_noted(stops(it, failure), p.x, p.y + top, p.w, p.h - top, lang))
            elif done is not None:
                items.append(done)
        if items:
            body.append({"k": "band", "gap": rmodel._round_mm(sheet.gap), "h": rmodel._round_mm(band.height), "items": items})
    theme = r.report.theme
    doc = {"lang": lang, "title": title, "page": {"w": sheet.page_w, "h": sheet.page_h if sheet.height is not None else None, "margins": list(sheet.page.margins)},
           "theme": {"size": theme.size, "color": theme.color, "accent": theme.accent, **rmodel.PALETTE},
           "header": None, "footer": _footer(title, r.loc, sheet), "body": body, "max_pages": limits.max_pages, "hot": False, "empty": r.loc.texts["empty"]}
    seen: set[str] = set()
    rmodel._texts_of(doc, seen)
    if missing := glyphs.missing(seen):
        b.warnings.append(f"{len(missing)} character(s) are not in the document's font and print as boxes: {''.join(sorted(missing)[:20])}")
    return rmodel.Model(doc, list(tables.values()), b.charts, b.files, [], [], b.warnings, title)


def build(definition: dict, tiles: list[dict], datasets: dict[str, dict], params: dict[str, Any] | None = None, *,
          refused: dict[str, dict] | None = None, clicks: list[dict] | None = None, state: dict | None = None,
          locale: str | None = None, title: str = "", page: dict | None = None, long: bool = False, strict: bool = False,
          cap: int | None = None, limits: Limits | None = None, now: datetime | None = None,
          read: Callable[[str, Any, int, Any], rdata.DataSet] = rdata.read_dataset) -> rmodel.Model:
    """The model of the document. `definition`: the dashboard written as a report's, as for its numbers,
    with its images; `tiles`: {"id", "title", "grid"} of each. `refused`, `clicks`, `state`: as for
    its numbers (tiles.compute), for this reader."""
    limits = limits or Limits.from_settings()
    sheet = Sheet.of(page, long)
    r = rtiles.Reading(definition, datasets, params, refused=refused, locale=locale, title=title, cap=cap, now=now, read=read, clicks=clicks)
    return r.work(lambda store: _document(r, store, tiles, state, title, sheet, strict, limits))


def _without_pictures(doc: dict, lang: str) -> None:
    """The document with what its tiles of pictures say instead of them."""
    for block in doc["body"]:
        if block["k"] == "band":
            block["items"] = [_noted({"code": "error", "error": "This picture cannot be read"}, i["x"], i["y"], i["w"], i["h"], lang) if i["k"] == "image" else i
                              for i in block["items"]]


def filename(name: str | None, fmt: str) -> str:
    """The file's name, with the extension of what is in it."""
    base = (name or "").strip()
    return f"{(base[:-len(fmt) - 1] if base.lower().endswith(f'.{fmt}') else base) or 'dashboard'}.{fmt}"


def _of_a_dashboard(message: str) -> str:
    return re.sub(r"\breport\b", "dashboard", message)


def render(definition: dict, tiles: list[dict], datasets: dict[str, dict], fmt: str, workdir: Path, *, params: dict[str, Any] | None = None,
           limits: Limits | None = None, title: str = "", **kw: Any) -> Rendered:
    """The document as a file in `workdir`; ReportError with what stopped it."""
    if fmt not in FORMATS:
        raise ReportError(f"unknown format «{fmt}»: use pdf or png", "bad_request")
    limits = limits or Limits.from_settings()
    workdir.mkdir(parents=True, exist_ok=True)
    root = workdir / "typst"
    root.mkdir(parents=True, exist_ok=True)
    m = build(definition, tiles, datasets, params, long=fmt == "png", limits=limits, title=title, **kw)
    out = workdir / f"dashboard.{fmt}"
    try:
        for name, svg in charts.render_all(m.charts, limits.chart_url).items():
            _write(root, name, svg)
        for name, payload in m.files.items():
            _write(root, name, payload)
        pdf.prepare(root)

        def typeset() -> dict:
            job = {"root": str(root), "main": "report.typ", "format": fmt, "output": str(out), "ppi": IMAGE_PPI,
                   "inputs": {"model": json.dumps(m.doc, ensure_ascii=False, separators=(",", ":"))}}
            return pdf.run(job, limits.memory_mb, limits.seconds, limits.max_pages)

        try:
            result = typeset()
        except ReportError as e:
            # a picture the checks took and Typst cannot read: what it stops is its tile, unless nobody is to look
            if "decode image" not in e.message or kw.get("strict"):
                raise
            _without_pictures(m.doc, m.doc["lang"])
            result = typeset()
    except ReportError as e:
        # an image is one page in memory, as a PDF's pages are not
        hint = ". A PDF holds more" if fmt == "png" and "RENDER_MEMORY_MB" in e.message else ""
        raise ReportError(_of_a_dashboard(e.message) + hint, e.tag) from None
    return Rendered(out, result.get("pages"), m.warnings)
