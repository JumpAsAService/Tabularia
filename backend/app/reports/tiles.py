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

Two places are for what comes next: `compute` takes the reading as `read`, where the rows of a
dataset can come from elsewhere (the cache), and `_Builder.rows_of` is the one place a tile's
rows come from, on both ways of computing (a click's filter).
"""
from __future__ import annotations

import logging
import math
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


def _clean(node: Any) -> Any:
    """The whole answer of a tile written in plain JSON: an infinity computed anywhere in it
    would stop every other tile at the door."""
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_clean(v) for v in node]
    return _value(node)


# ── a table, a page at a time ────────────────────────────────────────────────
class _Page(list):
    """The rows of a table's page, counting those it leaves out. A screen has no page breaks,
    nor headers written again after one."""

    def __init__(self, lo: int, hi: int):
        super().__init__()
        self.lo, self.hi, self.total = lo, hi, 0

    def append(self, row: rmodel.Row) -> None:
        if row.kind == "pb" or row.continued:
            return
        if self.lo <= self.total < self.hi:
            super().append(row)
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

    def glue_previous(self, keep: bool) -> None:  # what stays together on a sheet of paper
        pass

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
    axes: `cats` and `series`), under the same names, and the texts the server wrote."""
    kind, o = it.kind, it.options
    if kind == "scatter":
        return {"series": [{"name": name, "points": [[x, y, t] for x, y, t in points if x is not None and y is not None]}
                           for name, points in d.points]}
    if kind == "gauge":
        s = d.series[0]
        return {"value": s.values[0], "text": s.labels[0], "name": s.name, "min": o.min, "max": o.max,
                "target": d.targets[0] if d.targets else None, "bands": o.bands}
    out: dict[str, Any] = {"cats": d.categories, "series": [_series(s) for s in d.series]}
    first = d.series[0] if d.series else charts.SeriesData("", [], [])
    if kind in ("pie", "donut"):
        out["slices"] = [{"name": c, "value": v, "text": t} for c, v, t in zip(d.categories, first.values, first.labels)
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
    # the rows are counted before they become Python's values, a million of them on a scatter
    if it.kind == "scatter" and not ex.is_aggregate(rd.parse(it.series[0].value)):
        n = len(b.rows_of(it.dataset))
        if n > MAX_POINTS:
            raise _too_many_points(n)
    d = b.chart_numbers(it)
    if (n := sum(len(points) for _, points in d.points)) > MAX_POINTS:
        raise _too_many_points(n)
    o = it.options
    return {"kind": it.kind, "format": o.format, "empty": not charts._has_values(d), "data": _chart_data(d, it, b.loc),
            "options": {"stacked": o.stacked, "labels": o.labels, "legend": o.legend, "min": o.min, "max": o.max}}


def _cell(c: rmodel.Cell) -> dict:
    return {"text": c.text, "value": c.value, **({"style": c.style} if c.style else {})}


def _row(r: rmodel.Row, toggles: bool) -> dict:
    if r.kind == "g":
        return {"kind": "g", "level": r.level, "label": r.label, "key": r.key if toggles else None, "closed": r.closed}
    if r.kind == "d":
        return {"kind": "d", "cells": [_cell(c) for c in r.cells], **({"zebra": True} if r.zebra else {})}
    return {"kind": r.kind, "level": r.level, "label": r.label, "span": r.span, "cells": [_cell(c) for c in r.cells]}


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
            "head": out.head, "rows": [_row(r, it.interactive.toggle) for r in out.rows], "levels": out.levels,
            "page": page, "pages": max(math.ceil(total / PAGE_ROWS), 1), "page_rows": PAGE_ROWS, "total_rows": total,
            "sorted": _sorted_by(b, it) if sortable else None,
            "hot": b.hot(it, sortable)}


def _text(b: rmodel._Builder, it: rd.TextItem) -> dict:
    # a page's number is a sheet's: on a screen there is none to say
    return {"text": "".join(p for p in b.text_parts(it) if isinstance(p, str))}


def _image(it: rd.ImageItem) -> dict:
    return {"fit": it.fit}  # the picture is the gateway's: it does not come here


def _numbers(b: rmodel._Builder, it: Any, page: int) -> dict:
    """The tile's own JSON, or `hidden` when its condition says so."""
    b.ctx.dataset = b.item_dataset(it)
    if not b.visible(it):
        return {"hidden": True}
    if it.type == "kpi":
        return _kpi(b, it)
    if it.type == "chart":
        return _chart(b, it)
    if it.type == "table":
        return _table(b, it, page)
    if it.type == "text":
        return _text(b, it)
    return _image(it)


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


def _tile(b: rmodel._Builder, it: Any, page: int, at: str) -> dict:
    """One tile, whatever happens to it: what stops it stays here. Only the request knocked
    down goes on up (SoftTimeLimitExceeded, by name: the engine does not import Celery)."""
    try:
        answer = b.either(it, _numbers, b, it, page)
        # as plain JSON here, or a bad value would stop the whole answer at the door
        return {"type": it.type, **_clean(answer), "computed_at": at, "cached": False}
    except ReportError as e:
        return _failure(_said(e.message, it.id))
    except Exception as e:
        from app.engine.query_tag import was_interrupted

        if was_interrupted(e):
            raise
        logger.exception("dashboard: tile «%s» could not be computed", it.id)
        return _failure("This tile could not be computed")


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


def _page(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def compute(definition: dict, datasets: dict[str, dict], params: dict[str, Any] | None = None, *,
            refused: dict[str, dict] | None = None, only: list[str] | None = None, pages: dict[str, int] | None = None,
            state: dict | None = None, locale: str | None = None, title: str = "", cap: int | None = None,
            now: datetime | None = None, read: Callable[[str, Any, int, Any], rdata.DataSet] = rdata.read_dataset) -> dict:
    """{"tiles": {id: numbers or {"error", "code"}}, "truncated": {dataset: bool}}. ReportError
    when nothing can be said of any tile: a definition that cannot be read, or parameters
    that do not fit."""
    refused, pages = refused or {}, pages or {}
    report = _load(definition)
    if locale:
        report.locale = locale if locale in formats.LOCALES else report.locale
    loc = formats.locale(report.locale)
    tz = ph.zone(report.timezone)
    moment = now or datetime.now(timezone.utc)
    now_local = moment.astimezone(tz)
    pctx = ph.Context(now=now_local, tz=tz, lang=loc.code)
    values = rdata.parameters(report, params or {}, pctx)
    ctx = ex.Context(loc=loc, now=now_local, params=values, report=title, ph_context=pctx)
    cap = cap or max_rows()
    at = moment.isoformat()

    items = [it for _, it in report.items() if only is None or it.id in only]
    needs = {it.id: rdata.used_datasets(report, [it]) for it in items}
    # rights first: a tile that needs what the reader may not read says only that
    answers: dict[str, dict] = {}
    for it in items:
        stopped = sorted((refused[n] for n in needs[it.id] if n in refused), key=lambda r: r.get("code") != "no_access")
        if stopped:
            answers[it.id] = {"error": stopped[0].get("error"), "code": stopped[0].get("code")}

    # every dataset once
    data: dict[str, rdata.DataSet] = {}
    failed: dict[str, dict] = {}
    truncated: dict[str, bool] = {}
    for name in dict.fromkeys(n for it in items if it.id not in answers for n in needs[it.id]):
        try:
            data[name] = read(name, datasets.get(name), cap, tz)
            truncated[name] = False
        except OverCap:
            truncated[name] = True
            failed[name] = _over_cap(name, cap)
        except ReportError as e:
            failed[name] = _failure(_sentence(e.message))

    def stopped_by_data(it: Any) -> dict | None:
        return next((failed[n] for n in needs[it.id] if n in failed), None)

    # the tile's expressions against the columns the data really has
    columns = {name: ds.columns for name, ds in data.items()}
    wrong: dict[str, str] = {}
    for p in rd.problems(report, columns):
        if p.item is None:
            where = next((d.name for d in report.datasets if p.field.startswith(f"datasets[{d.name}]")), None)
            if where is not None:
                failed.setdefault(where, _failure(f"The dataset «{where}»: {p}"))
            continue
        wrong.setdefault(p.item, str(p))

    def work(store: frames.Store | None) -> None:
        if store is not None:
            ctx.datasets = store.datasets(data)
        else:
            ctx.datasets = {name: ds.rows for name, ds in data.items()}
        failed.update(_calculated(report, data, ctx, store, set(failed)))
        b = rmodel._Builder(report, data, ctx, state, interactive=True, layout=False, store=store)
        for it in items:
            if it.id in answers:
                continue
            stopped = stopped_by_data(it)
            if stopped is not None:
                answers[it.id] = dict(stopped)
            elif it.id in wrong:
                answers[it.id] = _failure(_said(wrong[it.id], it.id))
            else:
                answers[it.id] = _tile(b, it, _page(pages.get(it.id)), at)

    if frames.enabled():
        with frames.build_store(report, data, loc) as store:
            work(store)
    else:
        work(None)
    return {"tiles": {it.id: answers[it.id] for it in items}, "truncated": truncated}
