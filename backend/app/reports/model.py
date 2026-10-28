"""The render model: what a report shows, worked out from its definition and its data.

Pages are Typst's job (template.typ); the model holds everything else, already decided
and formatted on the server: the body as blocks (a band of items placed at their x, y, or
a table), every table's rows in order — group headers, detail rows, group footers with
their subtotals, the grand total, page breaks — every text written out in the report's
language, the charts as ECharts options for the chart service. `doc` is the JSON the
template receives; `tables`, `kpis` and `texts` keep the raw values for Excel and for the
email.

Layout of the body (docs/specs/reports.md, «Sections»): items whose vertical ranges
overlap form a row; a table grows with its data and takes its row alone, and what was
designed below it keeps its distance from it.

Keep-together: the last row of a group travels with the group's footer (and a footer with
the footer of the group around it), as one unbreakable block — Typst alone leaves a
subtotal alone at the top of a page. Group headers repeat on the pages a group continues
on (Typst's subheaders).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cmp_to_key
from typing import Any

from app.reports import charts
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import formats
from app.reports.data import DataSet
from app.reports.errors import ReportError

MM_PX = 96 / 25.4  # CSS pixels in a millimetre: the chart service draws in px
MAX_GLUED = 5  # the longest run of rows kept together (a deep nest of footers)
PALETTE = {"muted": "#6b7590", "rule": "#d9dde5", "band": "#e9eef9", "head": "#f1f3f5", "negative": "#c92a2a",
           "positive": "#2b8a3e", "total": "#1f2933"}


# ── what Excel and the email need beyond the template's JSON ─────────────────
@dataclass
class Cell:
    text: str
    value: Any = None
    style: dict | None = None


@dataclass
class Row:
    kind: str  # g group header, d detail, f group footer, t grand total, pb page break
    level: int = 0
    cells: list[Cell] = field(default_factory=list)
    label: str = ""
    span: int = 0  # f/t: the leading columns the label covers
    key: list | None = None  # g: the group's path of keys, as JSON values
    closed: bool = False
    glue: bool = False  # kept on the same page as the next row
    zebra: bool = False
    repeat: bool = True  # g: repeated on the pages the group continues on
    continued: bool = False  # g: written again after a page break inside its group
    first: int = 0  # f/t: the index of the group's first row, for Excel's SUBTOTAL formulas
    formula: bool = False  # f/t: every detail row of the group is written above, so a formula holds


@dataclass
class ColOut:
    header: str
    width: float | None
    align: str | None
    fmt: str | None
    style: dict
    kinds: set = field(default_factory=set)  # the kinds of value seen (number, date, …)
    subtotal: int | None = None  # Excel's SUBTOTAL function number when the total is AGG([this field])

    @property
    def kind(self) -> str:
        k = self.kinds - {"null"}
        return k.pop() if len(k) == 1 else ("number" if k == {"number", "boolean"} else "text")


@dataclass
class TableOut:
    item: rd.TableItem
    title: str
    cols: list[ColOut]
    head: list[list[dict]]  # header rows: {"t", "span", "rspan", "align"}
    rows: list[Row]
    levels: int  # groups, for Excel's outline


@dataclass
class ChartJob:
    name: str  # the file the template reads: chart/<n>.svg
    width: int  # px
    height: int
    option: dict
    alt: str


@dataclass
class Model:
    doc: dict
    tables: list[TableOut]
    charts: list[ChartJob]
    files: dict[str, bytes]  # images of the definition, by the name the template reads
    blocks: list[tuple[str, Any]]  # the body in order, for the email: (text|kpi|chart|table, …)
    params: list[tuple[str, str]]  # (label, value as written), for Excel
    warnings: list[str]
    title: str


def _kind_of(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, datetime):
        return "datetime"
    if isinstance(v, date):
        return "date"
    return "text"


def sort_key(v: Any) -> tuple:
    """A key that orders any value, NULL marked so it can go last."""
    if v is None:
        return (1, 0, 0)
    if isinstance(v, bool):
        return (0, 0, int(v))
    if isinstance(v, (int, float)):
        return (0, 1, v)
    if isinstance(v, datetime):
        return (0, 2, v)
    if isinstance(v, date):
        return (0, 2, datetime(v.year, v.month, v.day))
    if isinstance(v, str):
        return (0, 3, v.casefold(), v)
    return (0, 4, str(v))


def _compare(a: tuple, b: tuple, direction: str) -> int:
    """NULL last in both directions."""
    if a == b:
        return 0
    if a[0] != b[0]:
        return 1 if a[0] else -1
    r = -1 if a < b else 1
    return r if direction == "asc" else -r


def key_id(v: Any) -> Any:
    """A group's key as JSON writes it (how the screen names a group in `state`)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _style(st: rd.Style | None, base: dict | None = None) -> dict:
    out = dict(base or {})
    if st is None:
        return out
    if st.size:
        out["size"] = st.size
    if st.weight is not None:
        out["weight"] = {"normal": 400, "semibold": 600, "bold": 700}.get(st.weight, st.weight)  # type: ignore[arg-type]
    if st.italic is not None:
        out["italic"] = st.italic
    if st.color:
        out["color"] = st.color
    if st.background:
        out["fill"] = st.background
    if st.align:
        out["align"] = st.align
    if st.valign:
        out["valign"] = {"top": "top", "middle": "horizon", "bottom": "bottom"}[st.valign]
    if st.padding is not None:
        out["pad"] = st.padding
    if st.border:
        out["stroke"] = {"w": st.border.width, "c": st.border.color}
    if st.radius is not None:
        out["radius"] = st.radius
    if st.can_grow:
        out["grow"] = True
    return out


def _round_mm(v: float) -> float:
    return round(float(v), 2)


_SUBTOTAL = {"SUM": 9, "AVG": 1, "COUNT": 3, "MAX": 4, "MIN": 5}


def _subtotal_fn(col: rd.Column) -> int | None:
    """Excel's SUBTOTAL function when the column's total is SUM/AVG/COUNT/MIN/MAX of the
    very field the column shows: then a formula can stand for it."""
    if not col.total:
        return None
    try:
        total, value = rd.parse(col.total), rd.parse(col.value)
    except ex.ExpressionError:
        return None
    if (isinstance(total, ex.Call) and total.name in _SUBTOTAL and len(total.args) == 1
            and isinstance(total.args[0], ex.Field) and isinstance(value, ex.Field)
            and total.args[0].name == value.name):
        return _SUBTOTAL[total.name]
    return None


class _Builder:
    def __init__(self, report: rd.Report, data: dict[str, DataSet], ctx: ex.Context, state: dict | None,
                 interactive: bool):
        self.r = report
        self.data = data
        self.ctx = ctx
        self.loc = ctx.loc
        self.ev = ex.Evaluator()
        self.state = (state or {}).get("items") or {}
        self.interactive = interactive
        self.charts: list[ChartJob] = []
        self.files: dict[str, bytes] = {}
        self.blocks: list[tuple[str, Any]] = []
        self.warnings: list[str] = []
        self.in_body = False  # the email shows the body's texts, KPIs and charts

    def tables(self) -> list[TableOut]:
        return [t for kind, t in self.blocks if kind == "table"]

    def note(self, kind: str, what: Any) -> None:
        if self.in_body:
            self.blocks.append((kind, what))

    # ── evaluation with the item named ──
    def value(self, item: Any, where: str, text: str, ctx: ex.Context) -> Any:
        try:
            return self.ev(rd.parse(text), ctx)
        except ex.EvalError as e:
            raise ReportError(f"item «{item.id}», {where}: {e}") from None
        except (ex.ExpressionError, RecursionError) as e:  # checked before: only a bug lands here
            raise ReportError(f"item «{item.id}», {where}: {e}") from None

    def show(self, v: Any, pattern: str | None) -> str:
        return formats.display(v, pattern, self.loc)

    def rows_of(self, name: str | None) -> list[dict]:
        if name is None:
            return []
        ds = self.data.get(name)
        return ds.rows if ds else []

    def item_dataset(self, it: Any) -> str | None:
        name = getattr(it, "dataset", None)
        if name is None and len(self.r.datasets) == 1:
            return self.r.datasets[0].name
        return name

    def visible(self, it: Any) -> bool:
        if not it.visible:
            return True
        rows = self.rows_of(self.item_dataset(it))
        return ex.truthy(self.value(it, "visible", it.visible, self.ctx.at(scopes=[rows] if rows else [])))

    # ── items placed in a band ──
    def placed(self, it: Any, dy: float = 0.0) -> dict | None:
        if not self.visible(it):
            return None
        box = {"x": _round_mm(it.x), "y": _round_mm(it.y - dy), "w": _round_mm(it.w), "h": _round_mm(it.h),
               "st": _style(it.style)}
        if it.type == "text":
            return {"k": "text", **box, "parts": self.text_parts(it)}
        if it.type == "image":
            if not it.asset:
                return None
            raw, kind = rd.image_bytes(it.asset)
            name = f"img/{len(self.files) + 1}.{kind}"
            self.files[name] = raw
            return {"k": "image", **box, "src": name, "fit": it.fit}
        if it.type == "line":
            b = it.style.border
            return {"k": "line", **box, "stroke": {"w": b.width if b else 0.3, "c": b.color if b else "#1f2933"}}
        if it.type == "rect":
            return {"k": "rect", **box}
        if it.type == "chart":
            return {"k": "chart", **box, "src": self.chart(it)}
        if it.type == "kpi":
            return {"k": "kpi", **box, **self.kpi(it)}
        raise ReportError(f"item «{it.id}»: a {it.type} cannot be placed here")  # pragma: no cover

    def text_parts(self, it: rd.TextItem) -> list:
        text, is_expr = ex.text_value(it.value)
        if not is_expr:
            self.note("text", (it, text))
            return [text]
        rows = self.rows_of(self.item_dataset(it))
        v = self.value(it, "value", text, self.ctx.at(scopes=[rows] if rows else []))
        if isinstance(v, (ex.PageText, ex.PageNumber)):
            parts = list(v) if isinstance(v, ex.PageText) else [v]
            return [{p.kind: 1} if isinstance(p, ex.PageNumber) else p for p in parts]
        shown = self.show(v, it.style.format)
        self.note("text", (it, shown))
        return [shown]

    # ── KPI ──
    def kpi(self, it: rd.KpiItem) -> dict:
        rows = self.rows_of(it.dataset)
        ctx = self.ctx.at(scopes=[rows])
        value = self.value(it, "value", it.value, ctx)
        out: dict[str, Any] = {"label": it.label, "value": self.show(value, it.format), "change": None, "spark": None}
        if it.compare:
            base = self.value(it, "compare", it.compare, ctx)
            if isinstance(value, (int, float)) and isinstance(base, (int, float)) and base:
                change = (value - base) / abs(base)
                out["change"] = {"t": formats.format_number(change, "+0.0%;-0.0%;0.0%", self.loc),
                                 "dir": "up" if change > 0 else "down" if change < 0 else "flat"}
        if it.sparkline:
            cats, values = [], []
            for key, sub in self.partition(it, "sparkline.category", it.sparkline.category, rows):
                cats.append(self.show(key, None))
                values.append(self.value(it, "sparkline.value", it.sparkline.value, ctx.at(scopes=[rows, sub])))
            spark_h = min(it.h * 0.28, 8.0)
            out["spark"] = self.add_chart(charts.sparkline(cats, values, self.r.theme.accent), it.w - 5, spark_h,
                                          f"{it.label}: trend")
            out["spark_h"] = _round_mm(spark_h)
        self.note("kpi", {"label": it.label, "value": out["value"], "change": out["change"]})
        return out

    # ── charts ──
    def partition(self, it: Any, where: str, by: str, rows: list[dict]) -> list[tuple[Any, list[dict]]]:
        """The rows by the value of `by`, in its order (NULL last)."""
        node = rd.parse(by)
        ctx = self.ctx.at(scopes=[rows])
        buckets: dict[tuple, tuple[Any, list[dict]]] = {}
        try:
            for r in rows:
                ctx.row = r
                k = self.ev(node, ctx)
                buckets.setdefault(sort_key(k), (k, []))[1].append(r)
        except ex.EvalError as e:
            raise ReportError(f"item «{it.id}», {where}: {e}") from None
        return [buckets[k] for k in sorted(buckets, key=cmp_to_key(lambda a, b: _compare(a, b, "asc")))]

    def add_chart(self, option: dict, w_mm: float, h_mm: float, alt: str) -> str:
        name = f"chart/{len(self.charts) + 1}.svg"
        self.charts.append(ChartJob(name, max(int(round(w_mm * MM_PX)), 20), max(int(round(h_mm * MM_PX)), 10),
                                    option, alt))
        return name

    def chart(self, it: rd.ChartItem) -> str:
        rows = self.rows_of(it.dataset)
        base = self.ctx.at(scopes=[rows])
        o = it.options
        fmt = o.format
        data = charts.ChartData(kind=it.kind, fmt=fmt, title=it.title or "")
        if it.kind in ("gauge", "bullet") and it.category is None:
            v = self.value(it, "series[0].value", it.series[0].value, base)
            data.categories = [it.series[0].name or ""]
            data.series = [charts.SeriesData(it.series[0].name, [self._num(v)], [self.show(v, fmt)])]
            data.targets = [self._target(it, base)]
        elif it.kind == "scatter":
            groups = self.partition(it, "split_by", it.split_by, rows) if it.split_by else [(None, rows)]
            s = it.series[0]
            agg = ex.is_aggregate(rd.parse(s.value))
            for key, sub in groups:
                points = []
                for x, xs in self.partition(it, "category", it.category, sub) if agg else []:
                    y = self.value(it, "series[0].value", s.value, base.at(scopes=[rows, xs]))
                    points.append((self._num(x), self._num(y), self.show(y, fmt)))
                if not agg:
                    cat, val = rd.parse(it.category), rd.parse(s.value)
                    c = base.at()
                    for r in sub:
                        c.row = r
                        try:
                            x, y = self.ev(cat, c), self.ev(val, c)
                        except ex.EvalError as e:
                            raise ReportError(f"item «{it.id}»: {e}") from None
                        points.append((self._num(x), self._num(y), self.show(y, fmt)))
                data.points.append((self.show(key, None) if it.split_by else (s.name or ""), points))
        else:
            self._categories(it, rows, base, data)
        option = charts.option(data, it, self.r.theme, self.loc)
        name = self.add_chart(option, it.w, it.h, it.title or it.id)
        self.note("chart", (it, name))
        return name

    @staticmethod
    def _num(v: Any) -> float | None:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v)

    def _target(self, it: rd.ChartItem, ctx: ex.Context) -> float | None:
        t = it.options.target
        if isinstance(t, str):
            return self._num(self.value(it, "options.target", t, ctx))
        return t

    def _categories(self, it: rd.ChartItem, rows: list[dict], base: ex.Context, data: charts.ChartData) -> None:
        cats = self.partition(it, "category", it.category, rows)  # type: ignore[arg-type]
        first = it.series[0]

        def measure(sub: list[dict], s: rd.Series, i: int) -> Any:
            return self.value(it, f"series[{i}].value", s.value, base.at(scopes=[rows, sub]))

        # the order: by the first series' value when asked, and the top N with the rest as «Other»
        if it.options.sort != "none" or it.options.top:
            keyed = [(self._num(measure(sub, first, 0)), k, sub) for k, sub in cats]
            direction = it.options.sort if it.options.sort != "none" else "desc"
            keyed.sort(key=cmp_to_key(lambda a, b: _compare(sort_key(a[0]), sort_key(b[0]), direction)))
            cats = [(k, sub) for _, k, sub in keyed]
        other: list[dict] = []
        if it.options.top and len(cats) > it.options.top:
            other = [r for _, sub in cats[it.options.top:] for r in sub]
            cats = cats[:it.options.top]
        if len(cats) > charts.MAX_CATEGORIES:
            raise ReportError(f"item «{it.id}»: {len(cats):,} categories, the most a chart draws is "
                              f"{charts.MAX_CATEGORIES:,}: set its option «top», or aggregate")
        data.categories = [self.show(k, None) for k, _ in cats] + ([self.loc.texts["other"]] if other else [])
        subsets = [sub for _, sub in cats] + ([other] if other else [])
        if it.split_by:
            splits = self.partition(it, "split_by", it.split_by, rows)
            if len(splits) > charts.MAX_SERIES:
                raise ReportError(f"item «{it.id}»: split_by gives {len(splits)} series, the most is "
                                  f"{charts.MAX_SERIES}")
            for key, srows in splits:
                ids = {id(r) for r in srows}
                values = [measure([r for r in sub if id(r) in ids], first, 0) for sub in subsets]
                data.series.append(charts.SeriesData(self.show(key, None), [self._num(v) for v in values],
                                                     [self.show(v, it.options.format) for v in values],
                                                     first.as_ or "bar", first.axis))
        else:
            for i, s in enumerate(it.series):
                values = [measure(sub, s, i) for sub in subsets]
                data.series.append(charts.SeriesData(s.name, [self._num(v) for v in values],
                                                     [self.show(v, it.options.format) for v in values],
                                                     s.as_ or ("line" if i and it.kind == "combo" else "bar"),
                                                     s.axis))
        if it.kind == "bullet":
            data.targets = [self._target(it, base.at(scopes=[rows, sub])) for sub in subsets]
        data.total_label = self.loc.texts["total"]

    # ── tables ──
    def table(self, it: rd.TableItem) -> TableOut:
        return _Table(self, it).build()

    # ── the body ──
    def body(self) -> list[dict]:
        self.in_body = True
        items = sorted(self.r.sections.body.items, key=lambda i: (i.y, i.x))
        blocks: list[dict] = []
        band: list[Any] = []
        top = bottom = 0.0
        prev_bottom = 0.0

        def close() -> None:
            nonlocal band, prev_bottom
            if not band:
                return
            placed = [p for p in (self.placed(i, top) for i in band) if p is not None]
            blocks.append({"k": "band", "gap": _round_mm(max(top - prev_bottom, 0)), "h": _round_mm(bottom - top),
                           "items": placed})
            prev_bottom = bottom
            band = []

        for it in items:
            if it.type in rd.GROWING:
                close()
                if self.visible(it):
                    out = self.table(it)
                    blocks.append({"k": "table", "gap": _round_mm(max(it.y - prev_bottom, 0)), **self.typst_table(out)})
                    self.note("table", out)
                prev_bottom = it.y + it.h
                continue
            if band and it.y < bottom:
                bottom = max(bottom, it.y + it.h)
                band.append(it)
                continue
            close()
            band, top, bottom = [it], it.y, it.y + it.h
        close()
        self.in_body = False
        return blocks

    def page_band(self, band: rd.Band, which: str) -> dict | None:
        if band.height <= 0 and not band.items:
            return None
        items = [p for p in (self.placed(i) for i in band.items) if p is not None]
        key = "first" if which == "header" else "last"
        return {"h": _round_mm(band.height), key: band.on_first_page if which == "header" else band.on_last_page,
                "items": items}

    def typst_table(self, t: TableOut) -> dict:
        it = t.item
        sorted_by = None
        st = self.state.get(it.id) or {}
        if it.interactive.sort and isinstance(st.get("sort"), dict):
            sorted_by = {"col": st["sort"].get("column"), "dir": st["sort"].get("dir", "asc")}
        rows = []
        for row in t.rows:
            if row.kind == "pb":
                rows.append({"k": "pb"})
                continue
            out: dict[str, Any] = {"k": row.kind}
            if row.glue:
                out["glue"] = True
            if row.kind == "g":
                out.update(lv=row.level, t=row.label, rep=row.repeat)
                if row.key is not None and it.interactive.toggle:
                    out["key"] = row.key
                    out["closed"] = row.closed
            elif row.kind == "d":
                out["c"] = [c.text if not c.style else {"t": c.text, "st": c.style} for c in row.cells]
                if row.zebra:
                    out["z"] = True
            else:
                out.update(lv=row.level, label=row.label, span=row.span,
                           c=[c.text if not c.style else {"t": c.text, "st": c.style} for c in row.cells[row.span:]])
            rows.append(out)
        return {"x": _round_mm(it.x), "w": _round_mm(it.w), "id": it.id,
                "cols": [{"w": c.width, "align": c.align or ("right" if c.kind == "number" else "left"), "st": c.style}
                         for c in t.cols],
                "head": t.head, "rows": rows, "repeat": it.repeat_header, "levels": t.levels,
                "hot": {"sort": it.interactive.sort, "toggle": it.interactive.toggle} if self.interactive else None,
                "sorted": sorted_by}


class _Table:
    """One table (or matrix) laid out as rows."""

    def __init__(self, b: _Builder, it: rd.TableItem):
        self.b = b
        self.it = it
        self.loc = b.loc
        state = b.state.get(it.id) or {}
        self.sort_state = state.get("sort") if it.interactive.sort and isinstance(state.get("sort"), dict) else None
        toggle = it.interactive.toggle
        self.collapse_all = bool(state.get("collapse_all")) if toggle else False
        self.collapsed = {json.dumps(p) for p in state.get("collapsed") or []} if toggle else set()
        self.expanded = {json.dumps(p) for p in state.get("expanded") or []} if toggle else set()
        self.out: list[Row] = []
        self.zebra = False

    def value(self, where: str, text: str, ctx: ex.Context) -> Any:
        return self.b.value(self.it, where, text, ctx)

    # ── rows: filtered, sorted, grouped ──
    def build(self) -> TableOut:
        it = self.it
        rows = list(self.b.rows_of(it.dataset))
        ctx = self.b.ctx.at(scopes=[rows])
        if it.filter:
            node = rd.parse(it.filter)
            kept = []
            try:
                for r in rows:
                    ctx.row = r
                    if ex.truthy(self.b.ev(node, ctx)):
                        kept.append(r)
            except ex.EvalError as e:
                raise ReportError(f"item «{it.id}», filter: {e}") from None
            rows = kept
        rows = self.sorted(rows)
        self.all_rows = rows
        if it.matrix:
            return self.matrix(rows)
        self.cols = [ColOut(c.header, c.width, c.align, c.format or c.style.format, _style(c.style),
                            subtotal=_subtotal_fn(c)) for c in it.columns]
        tree = self.partition(rows, 0) if it.groups else None
        if tree is None:
            self.details(rows, [rows])
        else:
            for i, node in enumerate(tree):
                self.group(node, i, len(tree), [rows], rows, [], [])
        if it.grand_total:
            self.footer(None, ctx.at(row=None, scopes=[rows]), keep=True)
        head = [[{"t": c.header, "span": 1, "rspan": 1, "align": c.align or ("right" if c.kind == "number" else "left")}
                 for c in self.cols]]
        return TableOut(it, it.title or it.id, self.cols, head, self.out, len(it.groups))

    def sorted(self, rows: list[dict]) -> list[dict]:
        keys: list[tuple[str, str]] = [(s.by, s.dir) for s in self.it.sort]
        if self.sort_state is not None:
            col = self.sort_state.get("column")
            if isinstance(col, int) and 0 <= col < len(self.it.columns):
                keys = [(self.it.columns[col].value, "desc" if self.sort_state.get("dir") == "desc" else "asc")]
        if not keys:
            return rows
        ctx = self.b.ctx.at(scopes=[rows])
        nodes = [(rd.parse(by), d) for by, d in keys]
        decorated = []
        try:
            for r in rows:
                ctx.row = r
                decorated.append(([sort_key(self.b.ev(n, ctx)) for n, _ in nodes], r))
        except ex.EvalError as e:
            raise ReportError(f"item «{self.it.id}», sort: {e}") from None

        def cmp(a: tuple, b: tuple) -> int:
            for ka, kb, (_, d) in zip(a[0], b[0], nodes):
                c = _compare(ka, kb, d)
                if c:
                    return c
            return 0

        decorated.sort(key=cmp_to_key(cmp))
        return [r for _, r in decorated]

    def partition(self, rows: list[dict], level: int) -> list[dict]:
        """The rows of a level split by its group's key, in the group's order; a group's
        rows are the concatenation of its children's, so a RUNNING total over a parent
        follows the order the groups are written in."""
        g = self.it.groups[level]
        node = rd.parse(g.by)
        ctx = self.b.ctx.at(scopes=[self.all_rows])
        buckets: dict[tuple, tuple[Any, list[dict]]] = {}
        try:
            for r in rows:
                ctx.row = r
                k = self.b.ev(node, ctx)
                buckets.setdefault(sort_key(k), (k, []))[1].append(r)
        except ex.EvalError as e:
            raise ReportError(f"item «{self.it.id}», groups[{level}].by: {e}") from None
        out = []
        for sk in sorted(buckets, key=cmp_to_key(lambda a, b: _compare(a, b, g.sort))):
            key, sub = buckets[sk]
            children = self.partition(sub, level + 1) if level + 1 < len(self.it.groups) else None
            if children is not None:
                sub = [r for c in children for r in c["rows"]]
            out.append({"key": key, "rows": sub, "children": children})
        return out

    def is_closed(self, path: list) -> bool:
        p = json.dumps(path)
        if self.collapse_all:
            return p not in self.expanded
        return p in self.collapsed

    # ── groups ──
    def group(self, node: dict, index: int, siblings: int, scopes: list[list[dict]], parent_rows: list[dict],
              path: list, open_headers: list[Row]) -> None:
        it = self.it
        level = len(path)
        g = it.groups[level]
        rows = node["rows"]
        here = [*scopes, rows]
        path = [*path, key_id(node["key"])]
        last = parent_rows.index(rows[-1]) if rows else -1
        gctx = self.b.ctx.at(row=None, scopes=here, running=parent_rows, running_index=last, rownumber=index + 1)
        closed = self.is_closed(path)
        has_content = not closed and (node["children"] is not None or it.detail)
        first = len(self.out)
        header = None
        if g.header:
            label = self.value(f"groups[{level}].label", g.label, gctx) if g.label else node["key"]
            header = Row("g", level, label=self.b.show(label, None), key=path, closed=closed, repeat=has_content)
            self.out.append(header)
        headers = [*open_headers, header] if header is not None and header.repeat else open_headers
        if not closed:
            if node["children"] is not None:
                for i, child in enumerate(node["children"]):
                    self.group(child, i, len(node["children"]), here, rows, path, headers)
            elif it.detail:
                self.zebra = False
                self.details(rows, here)
        if g.footer:
            label = self.value(f"groups[{level}].label", g.label, gctx) if g.label else node["key"]
            self.footer(level, gctx, keep=g.keep_together, label=f"{self.loc.texts['total']} {self.b.show(label, None)}",
                        first=first)
        if g.page_break == "after" or (g.page_break == "between" and index < siblings - 1):
            self.out.append(Row("pb"))
            if index < siblings - 1:
                # the groups around this one start the next page again with their headers
                for h in open_headers:
                    self.out.append(Row("g", h.level, label=h.label, key=h.key, closed=h.closed, continued=True))

    def details(self, rows: list[dict], scopes: list[list[dict]]) -> None:
        it = self.it
        nodes = [rd.parse(c.value) for c in it.columns]
        rules = [[(rd.parse(rule.when), _style(rule.style)) for rule in c.rules] for c in it.columns]
        ctx = self.b.ctx.at(scopes=scopes, running=rows)
        for i, r in enumerate(rows):
            ctx.row, ctx.running_index, ctx.rownumber = r, i, i + 1
            cells = []
            for j, (node, col) in enumerate(zip(nodes, self.cols)):
                try:
                    v = self.b.ev(node, ctx)
                    style = None
                    for when, st in rules[j]:
                        if ex.truthy(self.b.ev(when, ctx)):
                            style = {**(style or {}), **st}
                except ex.EvalError as e:
                    raise ReportError(f"item «{it.id}», columns[{j}] ({col.header or 'no header'}), row {i + 1}: {e}") \
                        from None
                col.kinds.add(_kind_of(v))
                cells.append(Cell(self.b.show(v, col.fmt), v, style))
            self.zebra = not self.zebra
            self.out.append(Row("d", cells=cells, zebra=it.zebra and not self.zebra))

    def footer(self, level: int | None, ctx: ex.Context, keep: bool, label: str | None = None, first: int = 0) -> None:
        it = self.it
        cells = []
        for j, (c, col) in enumerate(zip(it.columns, self.cols)):
            if c.total:
                v = self.value(f"columns[{j}].total", c.total, ctx)
                col.kinds.add(_kind_of(v))
                cells.append(Cell(self.b.show(v, col.fmt), v))
            else:
                cells.append(Cell("", None))
        span = next((j for j, c in enumerate(it.columns) if c.total), len(it.columns))
        self.glue_previous(keep)
        formula = it.detail and not self.collapsed and not self.collapse_all
        self.out.append(Row("f" if level is not None else "t", level or 0, cells, label or self.loc.texts["total"], span,
                            first=first, formula=formula))

    def glue_previous(self, keep: bool) -> None:
        """The row before a footer stays with it (the subtotal never alone on a page)."""
        if not keep or not self.out or self.out[-1].kind not in ("d", "f"):
            return
        run = 0
        for row in reversed(self.out[:-1]):
            if not row.glue:
                break
            run += 1
        if run < MAX_GLUED - 1:
            self.out[-1].glue = True

    # ── a matrix: one column per value of columns_by ──
    def matrix(self, rows: list[dict]) -> TableOut:
        it, m = self.it, self.it.matrix
        assert m is not None
        levels = [rd.parse(x) for x in m.levels]
        ctx = self.b.ctx.at(scopes=[rows])
        keys_of: dict[int, tuple] = {}
        distinct: dict[tuple, tuple] = {}
        try:
            for r in rows:
                ctx.row = r
                raw = tuple(self.b.ev(n, ctx) for n in levels)
                sk = tuple(sort_key(v) for v in raw)
                keys_of[id(r)] = sk
                distinct.setdefault(sk, raw)
        except ex.EvalError as e:
            raise ReportError(f"item «{it.id}», matrix.columns_by: {e}") from None
        order = sorted(distinct, key=cmp_to_key(lambda a, b: next(
            (c for c in (_compare(x, y, "asc") for x, y in zip(a, b)) if c), 0)))
        if len(order) > rd.MAX_MATRIX_COLUMNS:
            raise ReportError(f"item «{it.id}»: the matrix would have {len(order):,} columns, the most is "
                              f"{rd.MAX_MATRIX_COLUMNS}: filter the rows, or choose another columns_by")
        self.matrix_keys = order
        self.keys_of = keys_of
        first = it.columns[0] if it.columns else None
        label_col = ColOut(first.header if first else "", first.width if first else None, "left", None,
                           _style(first.style) if first else {})
        self.cols = [label_col] + [ColOut("", m.width, "right", m.format, {}) for _ in order]
        if m.total:
            self.cols.append(ColOut(self.loc.texts["total"], m.width, "right", m.format, {"weight": 700}))
        head = self.matrix_head(order, distinct, label_col.header, len(levels), m.total)
        if it.groups:
            tree = self.partition(rows, 0)
            for i, node in enumerate(tree):
                self.matrix_group(node, i, len(tree), [rows], rows, [], [])
        else:
            self.matrix_row(self.loc.texts["total"], [rows], "d")
        if it.grand_total and it.groups:
            self.glue_previous(True)
            self.matrix_row(self.loc.texts["total"], [rows], "t")
        return TableOut(it, it.title or it.id, self.cols, head, self.out, max(len(it.groups) - 1, 0))

    def matrix_head(self, order: list[tuple], distinct: dict, first: str, depth: int, total: bool) -> list[list[dict]]:
        head: list[list[dict]] = []
        for lv in range(depth):
            row: list[dict] = []
            if lv == 0:
                row.append({"t": first, "span": 1, "rspan": depth, "align": "left"})
            i = 0
            while i < len(order):
                j = i + 1
                while j < len(order) and order[j][:lv + 1] == order[i][:lv + 1]:
                    j += 1
                span = j - i if lv < depth - 1 else 1
                if lv < depth - 1:
                    row.append({"t": self.b.show(distinct[order[i]][lv], None), "span": span, "rspan": 1,
                                "align": "center"})
                    i = j
                else:
                    row.append({"t": self.b.show(distinct[order[i]][lv], None), "span": 1, "rspan": 1, "align": "right"})
                    i += 1
            if lv == 0 and total:
                row.append({"t": self.loc.texts["total"], "span": 1, "rspan": depth, "align": "right"})
            head.append(row)
        return head

    def matrix_cells(self, rows: list[dict], scopes: list[list[dict]], ctx: ex.Context) -> list[Cell]:
        m = self.it.matrix
        assert m is not None
        by_key: dict[tuple, list[dict]] = {}
        for r in rows:
            by_key.setdefault(self.keys_of[id(r)], []).append(r)
        cells = []
        for k in self.matrix_keys:
            sub = by_key.get(k, [])
            v = self.value("matrix.value", m.value, ctx.at(scopes=[*scopes, sub])) if sub else None
            cells.append(Cell(self.b.show(v, m.format), v))
            self.cols[len(cells)].kinds.add(_kind_of(v))
        if m.total:
            v = self.value("matrix.value", m.value, ctx.at(scopes=scopes))
            cells.append(Cell(self.b.show(v, m.format), v, {"weight": 700}))
        return cells

    def matrix_row(self, label: str, scopes: list[list[dict]], kind: str, level: int = 0) -> None:
        ctx = self.b.ctx.at(row=None, scopes=scopes)
        cells = [Cell(label, label)] + self.matrix_cells(scopes[-1], scopes, ctx)
        if kind == "d":
            self.zebra = not self.zebra
            self.out.append(Row("d", cells=cells, zebra=self.it.zebra and not self.zebra))
        else:
            self.out.append(Row(kind, level, cells, label, 1))

    def matrix_group(self, node: dict, index: int, siblings: int, scopes: list[list[dict]], parent_rows: list[dict],
                     path: list, open_headers: list[Row]) -> None:
        it = self.it
        level = len(path)
        g = it.groups[level]
        here = [*scopes, node["rows"]]
        path = [*path, key_id(node["key"])]
        gctx = self.b.ctx.at(row=None, scopes=here, rownumber=index + 1)
        label = self.b.show(self.value(f"groups[{level}].label", g.label, gctx) if g.label else node["key"], None)
        if node["children"] is None:  # the innermost group is a row of the matrix
            self.matrix_row(label, here, "d")
            return
        closed = self.is_closed(path)
        header = None
        if g.header:
            header = Row("g", level, label=label, key=path, closed=closed, repeat=not closed)
            self.out.append(header)
        headers = [*open_headers, header] if header is not None and header.repeat else open_headers
        if not closed:
            for i, child in enumerate(node["children"]):
                self.matrix_group(child, i, len(node["children"]), here, node["rows"], path, headers)
        if g.footer:
            self.glue_previous(g.keep_together)
            self.matrix_row(f"{self.loc.texts['total']} {label}", here, "f", level)
        if g.page_break == "after" or (g.page_break == "between" and index < siblings - 1):
            self.out.append(Row("pb"))
            for h in open_headers if index < siblings - 1 else []:
                self.out.append(Row("g", h.level, label=h.label, key=h.key, closed=h.closed, continued=True))


def _texts_of(doc: Any, out: set[str]) -> None:
    if isinstance(doc, str):
        out.update(doc)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            if k not in ("src", "st", "k", "key", "align", "fit", "stroke"):
                _texts_of(v, out)
    elif isinstance(doc, list):
        for v in doc:
            _texts_of(v, out)


def build(report: rd.Report, data: dict[str, DataSet], params: dict[str, Any], ctx: ex.Context,
          state: dict | None = None, title: str = "", max_pages: int = 400, interactive: bool = False) -> Model:
    """The model of a report whose data has been read."""
    b = _Builder(report, data, ctx, state, interactive)
    w, h = report.page.dimensions()
    theme = report.theme
    if theme.font.strip().lower() != "ibm plex sans":
        b.warnings.append(f"the font «{theme.font}» is not shipped with Tabularia: the report uses IBM Plex Sans")
    doc = {
        "lang": report.locale,
        "title": title,
        "page": {"w": w, "h": h, "margins": list(report.page.margins)},
        "theme": {"size": theme.size, "color": theme.color, "accent": theme.accent, **PALETTE},
        "header": b.page_band(report.sections.page_header, "header"),
        "footer": b.page_band(report.sections.page_footer, "footer"),
        "body": b.body(),
        "max_pages": max_pages,
        "hot": interactive,
        "empty": b.loc.texts["empty"],
    }
    from app.reports import glyphs

    seen: set[str] = set()
    _texts_of(doc, seen)
    missing = glyphs.missing(seen)
    if missing:
        sample = "".join(sorted(missing)[:20])
        b.warnings.append(f"{len(missing)} character(s) are not in the report's font and print as boxes: {sample}")
    shown_params = []
    for p in report.parameters:
        shown_params.append((p.label or p.name, formats.display(params.get(p.name), None, b.loc)))
    return Model(doc, b.tables(), b.charts, b.files, b.blocks, shown_params, b.warnings, title)
