"""A dashboard's numbers, tile by tile (docs/specs/dashboards.md, «Reading the data», and «The
answer, tile by tile» for the JSON of each type): what the browser draws without computing
anything, worked out by the reports' model on both ways of computing (app/reports/model.py).

The gateway checks rights dataset by dataset with the reader's and sends a dashboard written as
a report (each tile an item), the datasets the reader may read as a render's, and in `refused`
what the others' tiles say. Here:

- every dataset an asked tile needs is read once: the tile's own, and those its expressions
  name in an aggregate or a LOOKUP, through the calculated fields too (data.used_datasets),
  whatever part of the tile they sit in;
- a tile that needs a refused dataset says what it was refused with, and nothing else;
- past DASHBOARDS__MAX_ROWS a dataset is not read on: its tiles say so and how to aggregate,
  never a number on part of the rows. `truncated` says it per dataset, of those read only: of
  one refused the reader learns nothing, not even that it is large;
- a table turns one page of rows into Python's values (`_PagedTable`), and works out its groups
  and totals on all of them;
- an error stays in its tile, the interruption of a request knocked down apart.

`compute` takes the reading as `read`, where the rows of a dataset can come from the cache
(app/reports/tile_cache.py): then `computed_at` says when they were read from their source, and
`cached` that this request did not read it.

A click on a tile filters the others on its dataset (docs/specs/dashboards.md, «A click filters the
others»): `_Tiles.rows_of` is the one place a tile's rows come from, on both ways of computing. The
browser sends a tile's id and values. The expression is the tile's own, in the definition; the
rows are split by it as the tile itself splits them (`partition`), and those whose key is one of the
values are kept: a value is only ever compared with a key, so nothing in it can be read as an
expression, and NULL, a date or an int past what JavaScript holds are found like any other. A
value not there is dropped, and a click with none left counts for nothing: the tile clicked says
what stayed, in `chosen`. A click on a tile the reader may not read counts for nothing, whatever it
says (`clicks_of`): its expression may read what they may not, and it would tell them which values
exist there. One that cannot be worked out says so in the tiles it touches: none of them shows its
numbers as if nothing had been clicked.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterable

from app.core import placeholders as ph
from app.reports import charts, formats, frames
from app.reports import data as rdata
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import model as rmodel
from app.reports.errors import OverCap, ReportError

logger = logging.getLogger(__name__)

PAGE_ROWS = 50  # a table's page
MAX_POINTS = 10_000  # a scatter's points: what a browser draws and a person reads; past it, aggregate
MAX_WALKED = 100_000  # the rows a RUNNING or a PREVIOUS turns into Python's values to reach a page
MAX_SAFE_INT = 2 ** 53  # past it a browser's JSON changes an int: it goes as a text


def max_rows() -> int:
    """DASHBOARDS__MAX_ROWS, by default REPORTS__MAX_ROWS: the most rows of one dataset."""
    from app.core import config

    settings = config.get_settings()
    return settings.dashboards.max_rows or settings.reports.max_rows


# ── a value as JSON ──────────────────────────────────────────────────────────
def _value(v: Any) -> Any:
    """A value as the answer holds it: what JSON cannot write is no value."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return rmodel.key_id(v)


def _key(v: Any) -> Any:
    """A value a click can name, as the browser holds it and sends it back: the raw one, never
    what the screen says of it. `_forms` finds it again."""
    v = _value(v)
    return str(v) if isinstance(v, int) and not isinstance(v, bool) and abs(v) > MAX_SAFE_INT else v


def _clean(node: Any) -> Any:
    """The whole answer of a tile written in plain JSON: an infinity computed anywhere in it
    would stop every other tile at the door."""
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_clean(v) for v in node]
    return _value(node)


# ── a click ──────────────────────────────────────────────────────────────────
@dataclass(eq=False)
class Click:
    """A click a reader may use: on which tile, on what of it (`on`: a matrix's `columns`, or
    `rows`), the expressions it chooses values of, from the outermost, what it chooses (each a
    path along them, `written` as it was sent), and the datasets that tile reads."""
    tile: str
    on: str
    dataset: str | None
    levels: list[str]
    paths: list[list]
    written: list
    reads: list[str]


def click_levels(it: Any, on: str = "rows") -> list[str]:
    """What a click on this tile chooses values of. Nothing: it cannot filter (a KPI, a gauge, a table
    with no groups)."""
    if it.type == "chart":
        return [it.category, *([it.split_by] if it.split_by else [])] if it.category and it.kind != "gauge" else []
    if it.type == "table":
        if on == "columns":
            return list(it.matrix.levels) if it.matrix else []
        return [g.by for g in it.groups]
    return []


def dataset_of(report: rd.Report, it: Any) -> str | None:
    name = getattr(it, "dataset", None)
    return name if name is not None or len(report.datasets) != 1 else report.datasets[0].name


def clicks_of(report: rd.Report, clicks: Iterable[dict], refused: dict[str, dict]) -> list[Click]:
    """The clicks of a request this reader may use: one on a tile they may not read counts for
    nothing. The gateway refuses the rest (a tile that cannot filter); here they are only left out."""
    tiles = {it.id: it for _, it in report.items()}
    out = []
    for raw in clicks:
        it, on = tiles.get(raw.get("tile")), raw.get("on") or "rows"
        levels = click_levels(it, on) if it is not None else []
        if not levels:
            continue
        reads = rdata.used_datasets(report, [it])
        if refusal(reads, refused):
            continue
        written = [v for v in raw.get("values") or [] if len(v if isinstance(v, list) else [v]) <= len(levels)]
        out.append(Click(it.id, on, dataset_of(report, it), levels, [v if isinstance(v, list) else [v] for v in written], written, reads))
    return out


def touching(it_id: str, dataset: str | None, reads: Iterable[str], clicks: list[Click]) -> list[Click]:
    """The clicks that filter this tile: on another tile, on a dataset it reads as its own."""
    return [c for c in clicks if c.tile != it_id and c.dataset == dataset and dataset in reads]


def needing(report: rd.Report, it: Any, needs: list[str], clicks: list[Click]) -> list[str]:
    """What a tile needs read: its own, and what the clicks that filter it read."""
    more = [n for c in touching(it.id, dataset_of(report, it), needs, clicks) for n in c.reads]
    return list(dict.fromkeys([*needs, *more]))


def click_marks(report: rd.Report, it: Any, needs: list[str], clicks: list[Click]) -> list:
    """What the clicks change of this tile, for the cache's key, in no order of their own: the
    filters on it, by expression, so that two tiles on one field share; and what it says is chosen."""
    marks = [["filter", c.levels, sorted(json.dumps(p) for p in c.paths)] for c in touching(it.id, dataset_of(report, it), needs, clicks)]
    marks += [["chosen", c.on, c.written] for c in clicks if c.tile == it.id]
    return sorted(marks, key=json.dumps)


def _forms(v: Any) -> list[tuple]:
    """What a value sent can be equal to: JSON keeps its type, an address has only texts."""
    if v is None:
        return [("null",)]
    if isinstance(v, bool):
        return [("bool", v)]
    if isinstance(v, (int, float)):
        return [("number", v)]
    out: list[tuple] = [("text", v)]
    if v in ("true", "false"):
        out.append(("bool", v == "true"))
    try:
        out.append(("number", int(v)))
    except ValueError:
        try:
            if math.isfinite(number := float(v)):
                out.append(("number", number))
        except ValueError:
            pass
    return out


def _form_of(key: Any) -> tuple:
    """A key as `_forms` writes it: as the answer holds it."""
    return _forms(_value(key))[0]


class _Tiles(rmodel._Builder):
    """The reports' model, whose rows are a tile's: its dataset's, less what a click on another
    tile leaves out. Set `tile` before asking."""

    def __init__(self, *args: Any, clicks: list[Click] = (), **kwargs: Any):  # type: ignore[assignment]
        super().__init__(*args, **kwargs)
        self.clicks = clicks
        self.tile: str | None = None
        self._cuts: dict[tuple, tuple[list, dict[int, list]]] = {}
        self._kept: dict[tuple, Any] = {}

    def _among(self, c: Click, rows: Any, paths: list[tuple[int, list]], found: dict[int, list], before: list) -> list:
        """The sets of these rows whose values are on one of these paths, noting in `found`
        which paths are there, with the keys they have."""
        at = len(before)
        wanted: dict[tuple, list[tuple[int, list]]] = {}
        for i, path in paths:
            for form in _forms(path[at]):
                wanted.setdefault(form, []).append((i, path))
        parts = []
        ctx = self.ctx.at(dataset=c.dataset, scopes=[rows], row=None)
        for key, sub in self.d.partition(rows, rd.parse(c.levels[at]), ctx, c.tile, "the click"):
            here = wanted.get(_form_of(key))
            if not here:
                continue
            ending = [i for i, path in here if len(path) == at + 1]
            for i in ending:
                found[i] = [*before, _key(key)]
            deeper = [(i, path) for i, path in here if len(path) > at + 1]
            inside = self._among(c, sub, deeper, found, [*before, _key(key)]) if deeper else []
            parts += [sub] if ending else inside  # a group chosen whole holds those inside it
        return parts

    def _cut(self, c: Click) -> tuple[list, dict[int, list]]:
        """The sets of the dataset's rows this click chooses, and what of it is there: on the
        dataset, not on what other clicks left, so that one click does not undo another."""
        cut = (self.d is self.py, id(c))
        hit = self._cuts.get(cut)
        if hit is None:
            found: dict[int, list] = {}
            try:
                parts = self._among(c, super().rows_of(c.dataset), list(enumerate(c.paths)), found, [])
            except ReportError as e:
                raise ReportError(f"the click on «{c.tile}» cannot be applied: {e.message}") from None
            hit = self._cuts[cut] = (parts, found)
        return hit

    def chart_numbers(self, it: rd.ChartItem) -> charts.ChartData:
        # the rows are counted before they become Python's values, a million of them on a scatter
        if it.kind == "scatter" and not ex.is_aggregate(rd.parse(it.series[0].value)):
            n = len(self.rows_of(it.dataset))
            if n > MAX_POINTS:
                raise _too_many_points(n)
        d = super().chart_numbers(it)
        if (n := sum(len(points) for _, points in d.points)) > MAX_POINTS:
            raise _too_many_points(n)
        return d

    def rows_of(self, name: str | None) -> Any:
        rows = super().rows_of(name)
        filters = tuple(c for c in self.clicks if c.dataset == name and c.tile != self.tile)
        if not filters:
            return rows
        memo = (self.d is self.py, name, tuple(id(c) for c in filters))
        kept = self._kept.get(memo)
        if kept is None:
            kept = rows
            for c in filters:
                parts, found = self._cut(c)
                if found:
                    kept = self.d.keeping(kept, parts)
            self._kept[memo] = kept
        return kept

    def chosen(self, tile: str) -> dict[str, list]:
        """What is chosen on this tile: the values found, as they are written in the answer."""
        out = {}
        for c in self.clicks:
            if c.tile == tile:
                _, found = self._cut(c)
                out[c.on] = [found[i] if isinstance(c.written[i], list) else found[i][0] for i in sorted(found)]
        return out


# ── a table, a page at a time ────────────────────────────────────────────────
class _Page(list):
    """The rows of a table's page, counting those it leaves out. A screen has no page breaks,
    nor headers written again after one. `path`: what a click on the row being written names, when
    it is not a group's header, whose key says it."""

    def __init__(self, lo: int, hi: int):
        super().__init__()
        self.lo, self.hi, self.total = lo, hi, 0
        self.path: list | None = None
        self.paths: dict[int, list] = {}

    def append(self, row: rmodel.Row) -> None:
        if row.kind == "pb" or row.continued:
            return
        if self.lo <= self.total < self.hi:
            super().append(row)
            if self.path is not None:
                self.paths[id(row)] = self.path
        self.total += 1


def _walks(it: rd.TableItem) -> bool:
    """A column or a rule reads the rows above its own (RUNNING, PREVIOUS): those become
    Python's values too."""
    texts = [c.value for c in it.columns if c.value] + [rule.when for c in it.columns for rule in c.rules]
    return any(ex.calls(rd.parse(t), "RUNNING") or ex.calls(rd.parse(t), "PREVIOUS") for t in texts)


class _PagedTable(rmodel._Table):
    """A table of the reports' model, whose detail rows outside the page are only counted. Its
    groups and totals are worked out on all of them: a total is never the page's."""

    def __init__(self, b: rmodel._Builder, it: rd.TableItem, page: int):
        super().__init__(b, it)
        self.out = _Page(page * PAGE_ROWS, (page + 1) * PAGE_ROWS)
        self.walks = _walks(it)
        self.open_paths: dict[int, list] = {}

    def glue_previous(self, keep: bool) -> None:  # what stays together on a sheet of paper
        pass

    # what a click names: a group with no header, by its footer; a matrix's innermost group is
    # a row with no key of its own; its columns, by each header of its levels
    def group(self, node: dict, index: int, siblings: int, scopes: list[Any], parent_rows: Any, start: int,
              path: list, open_headers: list[rmodel.Row], before: ex.Context | None = None) -> ex.Context | None:
        self.open_paths[len(path)] = [*path, rmodel.key_id(node["key"])]
        return super().group(node, index, siblings, scopes, parent_rows, start, path, open_headers, before)

    def footer(self, level: int | None, ctx: ex.Context, keep: bool, label: str | None = None, first: int = 0) -> None:
        self.out.path = self.open_paths[level] if level is not None and not self.it.groups[level].header else None
        super().footer(level, ctx, keep, label, first)
        self.out.path = None

    def matrix_group(self, node: dict, index: int, siblings: int, scopes: list[list[dict]], parent_rows: list[dict],
                     path: list, open_headers: list[rmodel.Row]) -> None:
        self.out.path = [*path, rmodel.key_id(node["key"])] if node["children"] is None else None
        super().matrix_group(node, index, siblings, scopes, parent_rows, path, open_headers)
        self.out.path = None

    def matrix_head(self, order: list[tuple], distinct: dict, first: str, depth: int, total: bool) -> list[list[dict]]:
        head = super().matrix_head(order, distinct, first, depth, total)
        for level, row in enumerate(head):
            at = 0
            for cell in row[1:] if level == 0 else row:
                if at >= len(order):
                    break  # the total's column
                cell["key"] = [_key(v) for v in distinct[order[at]][:level + 1]]
                at += cell["span"]
        return head

    def details(self, rows: Any, scopes: list[Any]) -> None:
        page, n = self.out, len(rows)
        first, last = max(page.lo - page.total, 0), min(page.hi - page.total, n)
        if first >= last:
            page.total += n
            self.zebra ^= n % 2 == 1
            return
        page.total += first
        self.zebra ^= first % 2 == 1
        if self.walks:
            if last > MAX_WALKED:
                raise ReportError(f"this page is {last:,} rows down, and its RUNNING or PREVIOUS reads every row above it: "
                                  f"at most {MAX_WALKED:,}. Filter the rows, or show the first pages")
            above = self.b.d.window(rows, 0, last)
            self.write_rows(above[first:], scopes, above, first)
        else:
            self.write_rows(self.b.d.window(rows, first, last), scopes, [], first)
        page.total += n - last
        self.zebra ^= (n - last) % 2 == 1


# ── the JSON of each type ────────────────────────────────────────────────────
def _kpi(b: rmodel._Builder, it: rd.KpiItem) -> dict:
    numbers = b.kpi_numbers(it)
    change, spark = numbers["change"], numbers["spark"]
    return {"label": it.label, "value": numbers["value"], "text": numbers["shown"],
            "change": change and {"ratio": change["ratio"], "text": change["t"], "dir": change["dir"]},
            "spark": spark and {"cats": spark[0], "values": spark[1]}}


def _series(s: charts.SeriesData) -> dict:
    return {"name": s.name, "values": s.values, "texts": s.labels, "as": s.as_, "axis": s.axis, "format": s.fmt}


def _chart_data(d: charts.ChartData, it: rd.ChartItem, loc: formats.Locale) -> dict:
    """What the kind's builder in frontend/composables/chartKinds.ts takes (or ChartPanel's
    axes: `cats` and `series`), under the same names, and the texts the server wrote; `keys` and
    `split_keys`: the raw values a click names, of the categories (none for «Other») and of a
    `split_by`'s series."""
    kind, o = it.kind, it.options
    split_keys = [_key(k) for k in d.split_keys]
    if kind == "scatter":
        return {"series": [{"name": name, "points": [[x, y, t] for x, y, t in points if x is not None and y is not None]}
                           for name, points in d.points], "split_keys": split_keys}
    if kind == "gauge":
        s = d.series[0]
        return {"value": s.values[0], "text": s.labels[0], "name": s.name, "min": o.min, "max": o.max,
                "target": d.targets[0] if d.targets else None, "bands": o.bands}
    out: dict[str, Any] = {"cats": d.categories, "series": [_series(s) for s in d.series], "keys": [_key(k) for k in d.keys],
                           "split_keys": split_keys}
    first = d.series[0] if d.series else charts.SeriesData("", [], [])
    if kind in ("pie", "donut"):
        out["slices"] = [{"name": c, "value": v, "text": t, "at": i} for i, (c, v, t) in enumerate(zip(d.categories, first.values, first.labels))
                         if v is not None and v > 0]
        out["left_out"] = len(d.categories) - len(out["slices"])
        out["name"] = first.name
    elif kind in ("treemap", "funnel", "waterfall"):
        out.update(values=first.values, texts=first.labels, name=first.name)
        if kind == "waterfall":  # the total charts.py writes after the steps
            total = sum(v or 0.0 for v in first.values)
            out.update(total=total, total_text=formats.display(total, d.fmt, loc), total_label=d.total_label)
    elif kind == "heatmap":
        cells = [(x, y, v, t) for y, s in enumerate(d.series) for x, (v, t) in enumerate(zip(s.values, s.labels))
                 if v is not None]
        out.update(xCats=d.categories, yCats=[s.name for s in d.series], cells=[list(c[:3]) for c in cells],
                   texts=[c[3] for c in cells], name=first.name)
    elif kind == "combo":
        bars = next((s for s in d.series if s.as_ != "line"), first)
        line = next((s for s in d.series if s.as_ == "line"), None)
        out.update(bars=bars.values, line=line.values if line else [], barName=bars.name, lineName=line.name if line else "")
    elif kind == "bullet":
        # the target's name is the interface's word: nothing in a chart names it
        out.update(values=first.values, texts=first.labels, targets=d.targets, name=first.name, targetName="",
                   max=o.max, bands=o.bands)
    return out


def _too_many_points(n: int) -> ReportError:
    return ReportError(f"{n:,} points, the most a chart here draws is {MAX_POINTS:,}: aggregate the value, or filter the rows")


def _chart(b: rmodel._Builder, it: rd.ChartItem) -> dict:
    d = b.chart_numbers(it)
    o = it.options
    return {"kind": it.kind, "format": o.format, "empty": not charts._has_values(d), "data": _chart_data(d, it, b.loc),
            "options": {"stacked": o.stacked, "labels": o.labels, "legend": o.legend, "min": o.min, "max": o.max}}


def _cell(c: rmodel.Cell) -> dict:
    return {"text": c.text, "value": c.value, **({"style": c.style} if c.style else {})}


def _row(r: rmodel.Row, toggles: bool, paths: dict[int, list]) -> dict:
    """`path`: what a click on this row names, where it names something."""
    if r.kind == "g":
        return {"kind": "g", "level": r.level, "label": r.label, "key": r.key if toggles else None, "closed": r.closed,
                "path": [_key(v) for v in r.key or []]}
    path = {"path": [_key(v) for v in paths[id(r)]]} if id(r) in paths else {}
    if r.kind == "d":
        return {"kind": "d", "cells": [_cell(c) for c in r.cells], **({"zebra": True} if r.zebra else {}), **path}
    return {"kind": r.kind, "level": r.level, "label": r.label, "span": r.span, "cells": [_cell(c) for c in r.cells], **path}


def _sorted_by(b: rmodel._Builder, it: rd.TableItem) -> dict | None:
    """The sort the state asks for, when it is the one applied (what a browser sent may be anything)."""
    sort = (b.state.get(it.id) or {}).get("sort")
    column = sort.get("column") if isinstance(sort, dict) else None
    if not isinstance(column, int) or isinstance(column, bool) or not 0 <= column < len(it.columns) or not rmodel._sorts(it, column):
        return None
    return {"column": column, "dir": "desc" if sort.get("dir") == "desc" else "asc"}


def _table(b: rmodel._Builder, it: rd.TableItem, page: int) -> dict:
    t = _PagedTable(b, it, page)
    out = t.build()
    if out.rows.total and page * PAGE_ROWS >= out.rows.total:  # a page past the last (the data changed): the last
        page = (out.rows.total - 1) // PAGE_ROWS
        t = _PagedTable(b, it, page)
        out = t.build()
    sortable = it.interactive.sort and it.matrix is None
    total = out.rows.total
    return {"matrix": it.matrix is not None, "empty": total == 0,
            "columns": [{"header": c.header, "align": c.align or ("right" if c.kind == "number" else "left"),
                         "width": c.width, "style": c.style} for c in out.cols],
            "head": out.head, "rows": [_row(r, it.interactive.toggle, out.rows.paths) for r in out.rows], "levels": out.levels,
            "page": page, "pages": max(math.ceil(total / PAGE_ROWS), 1), "page_rows": PAGE_ROWS, "total_rows": total,
            "sorted": _sorted_by(b, it) if sortable else None,
            "hot": b.hot(it, sortable)}


def _text(b: rmodel._Builder, it: rd.TextItem) -> dict:
    # a page's number is a sheet's: on a screen there is none to say
    return {"text": "".join(p for p in b.text_parts(it) if isinstance(p, str))}


def _image(it: rd.ImageItem) -> dict:
    return {"fit": it.fit}  # the picture is the gateway's: it does not come here


def _numbers(b: _Tiles, it: Any, page: int) -> dict:
    """The tile's own JSON, or `hidden` when its condition says so; `chosen`: what is clicked on
    it, and still there."""
    b.tile = it.id
    b.ctx.dataset = b.item_dataset(it)
    if not b.visible(it):
        return {"hidden": True}
    if it.type == "kpi":
        out = _kpi(b, it)
    elif it.type == "chart":
        out = _chart(b, it)
    elif it.type == "table":
        out = _table(b, it, page)
    elif it.type == "text":
        out = _text(b, it)
    else:
        out = _image(it)
    chosen = b.chosen(it.id)
    return {**out, "chosen": chosen} if chosen else out


def _said(message: str, tile: str) -> str:
    """A model's message inside the tile it is about: without saying which item, as a sentence."""
    for lead in (f"item «{tile}»: ", f"item «{tile}», "):
        if message.startswith(lead):
            message = message[len(lead):]
    return _sentence(message)


def _sentence(message: str) -> str:
    return message[:1].upper() + message[1:]


def _failure(message: str, code: str = "error") -> dict:
    return {"error": message, "code": code}


def attempt(tile: str, fn: Callable[[], Any]) -> tuple[Any, dict | None]:
    """What `fn` gives, or what it was stopped by, as the tile says it: what stops a tile stays in
    it. Only the request knocked down goes on up (SoftTimeLimitExceeded, by name: the engine does
    not import Celery)."""
    try:
        return fn(), None
    except ReportError as e:
        return None, _failure(_said(e.message, tile))
    except Exception as e:
        from app.engine.query_tag import was_interrupted

        if was_interrupted(e):
            raise
        logger.exception("dashboard: tile «%s» could not be computed", tile)
        return None, _failure("This tile could not be computed")


def _tile(b: _Tiles, it: Any, page: int, at: str, cached: bool) -> dict:
    """One tile, whatever happens to it."""
    def answered() -> dict:
        answer = b.either(it, _numbers, b, it, page)
        # as plain JSON here, or a bad value would stop the whole answer at the door
        return {"type": it.type, **_clean(answer), "computed_at": at, "cached": cached}

    numbers, failure = attempt(it.id, answered)
    return failure if failure is not None else numbers


# ── a request ────────────────────────────────────────────────────────────────
def _load(definition: dict) -> rd.Report:
    try:
        report = rd.load(definition)
    except rd.DefinitionError as e:
        raise ReportError(f"the dashboard's definition: {e}") from None
    return report


def _over_cap(name: str, cap: int) -> dict:
    return _failure(f"The dataset «{name}» has more than {cap:,} rows, the most a dashboard reads, so this tile shows no "
                    "number on part of them: aggregate it in its SQL, or filter it", "truncated")


def _calculated(report: rd.Report, data: dict[str, rdata.DataSet], ctx: ex.Context, store: frames.Store | None,
                skip: Iterable[str]) -> dict[str, dict]:
    """The calculated fields, dataset by dataset (those in `skip` stopped already): one that fails
    stops the tiles of its dataset."""
    failed: dict[str, dict] = {}
    for d in report.datasets:
        if d.name in skip:
            continue
        one = SimpleNamespace(datasets=[d])
        try:
            if store is not None:
                frames.add_calculated(one, data, store, ctx)
            else:
                rdata.add_calculated(one, data, ctx, ex.Evaluator())
        except ReportError as e:
            failed[d.name] = _failure(_sentence(e.message))
        except Exception as e:
            from app.engine.query_tag import was_interrupted

            if was_interrupted(e):
                raise
            logger.exception("dashboard: the calculated fields of «%s» could not be computed", d.name)
            failed[d.name] = _failure(f"The calculated fields of the dataset «{d.name}» could not be computed")
    return failed


def page_of(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def refusal(needs: Iterable[str], refused: dict[str, dict]) -> dict | None:
    """What a tile says when it needs what the reader may not read, and nothing else."""
    stopped = sorted((refused[n] for n in needs if n in refused), key=lambda r: r.get("code") != "no_access")
    return {"error": stopped[0].get("error"), "code": stopped[0].get("code")} if stopped else None


class Reading:
    """What a request reads, whatever it makes of it: the dashboard written as a report's, its
    tiles (`items`) and what each needs, the clicks this reader may use, the refusals, every dataset
    once. `compute` turns it into JSON for a screen, and app/reports/dashboard_layout.py into a
    document: the same rows, so the same numbers. ReportError when nothing can be said of any tile:
    a definition that cannot be read, or parameters that do not fit."""

    def __init__(self, definition: dict, datasets: dict[str, dict], params: dict[str, Any] | None = None, *,
                 refused: dict[str, dict] | None = None, only: list[str] | None = None, locale: str | None = None,
                 title: str = "", cap: int | None = None, now: datetime | None = None,
                 read: Callable[[str, Any, int, Any], rdata.DataSet] = rdata.read_dataset, clicks: list[dict] | None = None):
        refused = refused or {}
        self.report = report = _load(definition)
        if locale:
            report.locale = locale if locale in formats.LOCALES else report.locale
        self.loc = formats.locale(report.locale)
        tz = ph.zone(report.timezone)
        self.moment = now or datetime.now(timezone.utc)
        now_local = self.moment.astimezone(tz)
        pctx = ph.Context(now=now_local, tz=tz, lang=self.loc.code)
        self.values = rdata.parameters(report, params or {}, pctx)
        self.ctx = ex.Context(loc=self.loc, now=now_local, params=self.values, report=title, ph_context=pctx)
        self.cap = cap or max_rows()

        self.items = [it for _, it in report.items() if only is None or it.id in only]
        self.needs = {it.id: rdata.used_datasets(report, [it]) for it in self.items}
        self.usable = clicks_of(report, clicks or [], refused)
        # rights first: a tile that needs what the reader may not read says only that
        self.answers: dict[str, dict] = {}
        for it in self.items:
            stopped = refusal(self.needs[it.id], refused)
            if stopped:
                self.answers[it.id] = stopped

        # every dataset once
        self.data: dict[str, rdata.DataSet] = {}
        self.failed: dict[str, dict] = {}
        self.truncated: dict[str, bool] = {}
        for name in dict.fromkeys(n for it in self.items if it.id not in self.answers for n in needing(report, it, self.needs[it.id], self.usable)):
            try:
                self.data[name] = read(name, datasets.get(name), self.cap, tz)
                self.truncated[name] = False
            except OverCap:
                self.truncated[name] = True
                self.failed[name] = _over_cap(name, self.cap)
            except ReportError as e:
                self.failed[name] = _failure(_sentence(e.message))

        # the tile's expressions against the columns the data really has
        columns = {name: ds.columns for name, ds in self.data.items()}
        self.wrong: dict[str, str] = {}
        for p in rd.problems(report, columns):
            if p.item is None:
                where = next((d.name for d in report.datasets if p.field.startswith(f"datasets[{d.name}]")), None)
                if where is not None:
                    self.failed.setdefault(where, _failure(f"The dataset «{where}»: {p}"))
                continue
            self.wrong.setdefault(p.item, str(p))

    def stopped(self, it: Any) -> dict | None:
        """What stops this tile before anything is worked out; None when nothing does."""
        if it.id in self.answers:
            return self.answers[it.id]
        data = next((self.failed[n] for n in self.needs[it.id] if n in self.failed), None)
        if data is not None:
            return dict(data)
        # what filters a tile cannot be worked out: better no number than the numbers of all the rows
        for c in touching(it.id, dataset_of(self.report, it), self.needs[it.id], self.usable):
            if c.tile in self.wrong or any(n in self.failed for n in c.reads):
                return _failure(f"The click on «{c.tile}» cannot be applied: its tile cannot read its data. Clear that click")
        return _failure(_said(self.wrong[it.id], it.id)) if it.id in self.wrong else None

    def read_when(self, it: Any) -> tuple[str, bool]:
        """When the rows of a tile were read from their source (the oldest of them), and
        whether this request did without reading it."""
        read = [self.data[n] for n in self.needs[it.id] if n in self.data]
        return min((d.read_at or self.moment for d in read), default=self.moment).isoformat(), bool(read) and all(d.from_cache for d in read)

    def builder(self, store: frames.Store | None, state: dict | None, *, interactive: bool, layout: bool, max_pages: int = 400,
                cls: type[_Tiles] = _Tiles) -> Any:
        """The model on these rows, with its calculated fields."""
        if store is not None:
            self.ctx.datasets = store.datasets(self.data)
        else:
            self.ctx.datasets = {name: ds.rows for name, ds in self.data.items()}
        self.failed.update(_calculated(self.report, self.data, self.ctx, store, set(self.failed)))
        return cls(self.report, self.data, self.ctx, state, interactive=interactive, layout=layout, store=store, clicks=self.usable, max_pages=max_pages)

    def work(self, fn: Callable[[frames.Store | None], Any]) -> Any:
        """`fn` with the rows in DuckDB, which is closed with it, or the interpreter's way."""
        if frames.enabled():
            with frames.build_store(self.report, self.data, self.loc) as store:
                return fn(store)
        return fn(None)


def compute(definition: dict, datasets: dict[str, dict], params: dict[str, Any] | None = None, *,
            refused: dict[str, dict] | None = None, only: list[str] | None = None, pages: dict[str, int] | None = None,
            state: dict | None = None, locale: str | None = None, title: str = "", cap: int | None = None,
            now: datetime | None = None, read: Callable[[str, Any, int, Any], rdata.DataSet] = rdata.read_dataset,
            clicks: list[dict] | None = None) -> dict:
    """{"tiles": {id: numbers or {"error", "code"}}, "truncated": {dataset: bool}}. ReportError
    when nothing can be said of any tile: a definition that cannot be read, or parameters
    that do not fit."""
    pages = pages or {}
    r = Reading(definition, datasets, params, refused=refused, only=only, locale=locale, title=title, cap=cap, now=now, read=read, clicks=clicks)

    def work(store: frames.Store | None) -> None:
        b = r.builder(store, state, interactive=True, layout=False)
        for it in r.items:
            if it.id in r.answers:
                continue
            stopped = r.stopped(it)
            r.answers[it.id] = stopped if stopped is not None else _tile(b, it, page_of(pages.get(it.id)), *r.read_when(it))

    r.work(work)
    return {"tiles": {it.id: r.answers[it.id] for it in r.items}, "truncated": r.truncated}
