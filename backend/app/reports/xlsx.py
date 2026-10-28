"""A report as an Excel file: its tables and matrices as rows, one sheet each, plus the
parameters (docs/specs/reports.md, «Rendering (engine)»).

The cells keep their values — numbers, dates, booleans — with the report's format as the
cell's number format (negatives in red when the format does not say otherwise); the
groups are outline levels Excel opens and closes, subtotals and totals that are SUM, AVG,
COUNT, MIN or MAX of the column's own field are SUBTOTAL formulas with their value cached
(a viewer that does not recalculate shows it too; SUBTOTAL skips the subtotals nested in
its range), the header is frozen and printed on every page, and a page break between
groups is a page break of the print. A text is always written as a text: a customer
called `=HYPERLINK(…)` never becomes a formula.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from app.reports import formats
from app.reports.model import Model, TableOut

_PAPER = {"A4": 9, "A3": 8, "A5": 11, "Letter": 1, "Legal": 5}
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")


def _sheet_name(title: str, used: set[str]) -> str:
    """The title as Excel accepts a sheet's name: none of []:*?/\\, no quote at its ends, 31
    characters, unique in the book (and not «History», which Excel keeps for itself)."""
    base = " ".join(_SHEET_BAD.sub(" ", title or "").split()).strip("'").strip()[:31].rstrip() or "Table"
    name, n = base, 2
    while name.lower() in used or name.lower() == "history":
        suffix = f" ({n})"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name.lower())
    return name


class _Formats:
    """xlsxwriter's formats, made once per combination."""

    def __init__(self, wb: Any, theme: dict):
        self.wb = wb
        self.theme = theme
        self.cache: dict[tuple, Any] = {}

    def get(self, **props: Any) -> Any:
        key = tuple(sorted(props.items()))
        if key not in self.cache:
            self.cache[key] = self.wb.add_format({"font_name": "IBM Plex Sans", "font_size": 9, **props})
        return self.cache[key]


def _style_props(st: dict | None) -> dict:
    out: dict[str, Any] = {}
    if not st:
        return out
    if st.get("weight", 400) >= 600:
        out["bold"] = True
    if st.get("italic"):
        out["italic"] = True
    if st.get("color"):
        out["font_color"] = st["color"]
    if st.get("fill"):
        out["bg_color"] = st["fill"]
    return out


def _chars(mm: float) -> float:
    return round(mm / 1.9, 1)


def _write_value(ws: Any, r: int, c: int, value: Any, fmts: _Formats, props: dict, col_fmt: str | None,
                 loc: formats.Locale) -> None:
    if value is None or (isinstance(value, float) and value != value):
        ws.write_blank(r, c, None, fmts.get(**props))
    elif isinstance(value, bool):
        ws.write_boolean(r, c, value, fmts.get(**props))
    elif isinstance(value, (int, float)):
        ws.write_number(r, c, value, fmts.get(num_format=formats.excel_number_format(col_fmt, isinstance(value, int)),
                                              **props))
    elif isinstance(value, datetime):
        ws.write_datetime(r, c, value, fmts.get(num_format=formats.excel_date_format(col_fmt, loc, True), **props))
    elif isinstance(value, date):
        ws.write_datetime(r, c, datetime(value.year, value.month, value.day),
                          fmts.get(num_format=formats.excel_date_format(col_fmt, loc, False), **props))
    else:
        ws.write_string(r, c, str(value), fmts.get(**props))


def _table(ws: Any, t: TableOut, fmts: _Formats, loc: formats.Locale, page: dict) -> None:
    theme = fmts.theme
    ncols = len(t.cols)
    taken: set[tuple[int, int]] = set()  # cells covered by a merged header above or to the left
    r = 0
    for hr in t.head:
        c = 0
        for h in hr:
            while (r, c) in taken:
                c += 1
            align = {"right": "right", "center": "center"}.get(h["align"], "left")
            f = fmts.get(bold=True, bg_color=theme["head"], bottom=1, valign="vcenter", align=align)
            if h["span"] > 1 or h["rspan"] > 1:
                ws.merge_range(r, c, r + h["rspan"] - 1, c + h["span"] - 1, h["t"], f)
                for rr in range(r, r + h["rspan"]):
                    for cc in range(c, c + h["span"]):
                        taken.add((rr, cc))
            else:
                ws.write_string(r, c, h["t"], f)
                taken.add((r, c))
            c += h["span"]
        r += 1
    header_rows = r
    ws.freeze_panes(header_rows, 0)
    ws.repeat_rows(0, header_rows - 1)
    ws.outline_settings(True, True, True, False)

    widths = [len(c.header) for c in t.cols]
    sheet_row: dict[int, int] = {}
    breaks: list[int] = []
    for i, row in enumerate(t.rows):
        if row.kind == "pb":
            breaks.append(r)
            continue
        if row.kind == "g" and row.continued:
            continue  # written again for the PDF's next page: Excel prints its own headers
        sheet_row[i] = r
        if row.kind == "g":
            fill = theme["band"] if row.level == 0 else "#f4f6fc"
            f = fmts.get(bold=True, bg_color=fill)
            ws.write_string(r, 0, row.label, f)
            for c in range(1, ncols):
                ws.write_blank(r, c, None, f)
            ws.set_row(r, None, None, {"level": min(row.level, 7)})
            widths[0] = max(widths[0], len(row.label))
        elif row.kind == "d":
            for c, (cell, col) in enumerate(zip(row.cells, t.cols)):
                props = _style_props(col.style) | _style_props(cell.style)
                if row.zebra and "bg_color" not in props:
                    props["bg_color"] = theme["head"]
                _write_value(ws, r, c, cell.value, fmts, props, col.fmt, loc)
                widths[c] = max(widths[c], min(len(cell.text), 60))
            ws.set_row(r, None, None, {"level": min(t.levels, 7)})
        else:
            grand = row.kind == "t"
            props = {"bold": True, "top": 1, **({"bg_color": theme["total"], "font_color": "#ffffff"} if grand else {})}
            label = fmts.get(align="right", **props)
            if row.span:
                ws.write_string(r, 0, row.label, label)
            elif row.label:  # every column has a total: the label on a row of its own, above the values
                ws.write_string(r, 0, row.label, fmts.get(align="left", **props))
                for c in range(1, ncols):
                    ws.write_blank(r, c, None, fmts.get(**props))
                if not grand:
                    ws.set_row(r, None, None, {"level": min(row.level, 7)})
                r += 1
            for c in range(1, row.span):
                ws.write_blank(r, c, None, label)
            first = sheet_row.get(row.first, header_rows) if not grand else header_rows
            for c in range(row.span, ncols):
                cell, col = row.cells[c], t.cols[c]
                cprops = props | _style_props(cell.style)
                if col.subtotal and row.formula and isinstance(cell.value, (int, float)) and r > first:
                    letter = xl_col_to_name(c)
                    start = first + (1 if not grand and t.rows[row.first].kind == "g" else 0)
                    ws.write_formula(r, c, f"=SUBTOTAL({col.subtotal},{letter}{start + 1}:{letter}{r})",
                                     fmts.get(num_format=formats.excel_number_format(col.fmt, isinstance(cell.value, int)),
                                              **cprops), cell.value)
                else:
                    _write_value(ws, r, c, cell.value, fmts, cprops, col.fmt, loc)
            if not grand:
                ws.set_row(r, None, None, {"level": min(row.level, 7)})
            widths[0] = max(widths[0], len(row.label))
        r += 1
    for c, col in enumerate(t.cols):
        ws.set_column(c, c, _chars(col.width) if col.width else min(max(widths[c] + 2, 8), 60))
    if breaks:
        ws.set_h_pagebreaks(breaks)
    w, h = page["w"], page["h"]
    ws.set_landscape() if w > h else ws.set_portrait()
    ws.set_paper(page.get("paper", 9))
    ws.fit_to_pages(1, 0)
    ws.hide_gridlines(2)
    ws.print_area(0, 0, max(r - 1, 0), max(ncols - 1, 0))



def _escape_hf(text: str) -> str:
    return text.replace("&", "&&")


def write(model: Model, path: str, loc: formats.Locale, paper: str | None = None) -> None:
    wb = xlsxwriter.Workbook(path, {"strings_to_formulas": False, "strings_to_urls": False,
                                    "strings_to_numbers": False, "nan_inf_to_errors": True})
    wb.set_properties({"title": model.title, "author": "Tabularia", "company": "Tabularia"})
    theme = model.doc["theme"]
    fmts = _Formats(wb, theme)
    page = {**model.doc["page"], "paper": _PAPER.get(paper or "A4", 9)}
    used: set[str] = set()
    used.add(loc.texts["parameters"].lower())
    footer = "&R" + _escape_hf(loc.texts["page"].format(page="&P", pages="&N"))
    for t in model.tables:
        ws = wb.add_worksheet(_sheet_name(t.title, used))
        ws.set_header("&L" + _escape_hf(model.title)[:200])
        ws.set_footer(footer)
        _table(ws, t, fmts, loc, page)
    ws = wb.add_worksheet(loc.texts["parameters"][:31])
    bold = fmts.get(bold=True, bg_color=theme["head"], bottom=1)
    ws.write_string(0, 0, loc.texts["parameter"], bold)
    ws.write_string(0, 1, loc.texts["value"], bold)
    for i, (label, value) in enumerate(model.params, start=1):
        ws.write_string(i, 0, label, fmts.get())
        ws.write_string(i, 1, value, fmts.get())
    ws.set_column(0, 0, 28)
    ws.set_column(1, 1, 40)
    wb.close()
