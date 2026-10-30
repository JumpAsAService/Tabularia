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

The rows: what the model asks of them — filter them, sort them, group them, split them by a
category, give the rows a page shows — goes through a strategy, `PyData` (the
interpreter's way, every expression walked row by row over Python's dicts) or
`frames.SqlData` (the rows stay in columns, DuckDB computes): the same questions, the same
answers. Each item tries DuckDB's way first and, where it cannot be sure of the
interpreter's result, is built again the interpreter's way (`_Builder.either`).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cmp_to_key
from typing import Any

from app.reports import charts
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import formats, frames, glyphs
from app.reports.data import DataSet
from app.reports.errors import ReportError

logger = logging.getLogger(__name__)

MM_PX = 96 / 25.4  # CSS pixels in a millimetre: the chart service draws in px
MAX_GLUED = 5  # the longest run of rows kept together (a deep nest of footers)
CELL_PAD = 1.5  # mm left and right of a cell's text (template.typ, cell-inset)
MIN_SHARE = 10.0  # mm a column without a width keeps when the others are too wide
MIN_ROW = 1.2  # mm: no row of a table is lower (the padding of its cells alone is 2.2, template.typ)
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


sort_key = frames.sort_key  # a key that orders any value, NULL marked so it can go last
_compare = frames.compare  # NULL last in both directions


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


class _Datasets(Mapping):
    """The datasets by name for the evaluator (an aggregate that names one): their rows as
    the strategy has them, taken when an aggregate asks."""

    def __init__(self, names: list[str], rows: Any):
        self._names = names
        self._rows = rows

    def __getitem__(self, name: str) -> Any:
        if name not in self._names:
            raise KeyError(name)
        return self._rows(name)

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


class PyData:
    """The interpreter's way: a dataset's rows as Python's dicts, every expression walked
    row by row."""

    def __init__(self, data: dict[str, DataSet], ev: ex.Evaluator):
        self.data = data
        self.ev = ev
        self._ids: dict[int, tuple[list, set]] = {}

    def rows(self, name: str | None) -> list[dict]:
        if name is None:
            return []
        ds = self.data.get(name)
        return ds.rows if ds else []

    def where(self, rows: list[dict], node: ex.Node, ctx: ex.Context, item: str) -> list[dict]:
        kept = []
        try:
            for r in rows:
                ctx.row = r
                if ex.truthy(self.ev(node, ctx)):
                    kept.append(r)
        except ex.EvalError as e:
            raise ReportError(f"item «{item}», filter: {e}") from None
        return kept

    def sort(self, rows: list[dict], keys: list[tuple[ex.Node, str]], ctx: ex.Context, item: str) -> list[dict]:
        if not keys:
            return rows
        decorated = []
        try:
            for r in rows:
                ctx.row = r
                decorated.append(([sort_key(self.ev(n, ctx)) for n, _ in keys], r))
        except ex.EvalError as e:
            raise ReportError(f"item «{item}», sort: {e}") from None

        def cmp(a: tuple, b: tuple) -> int:
            for ka, kb, (_, d) in zip(a[0], b[0], keys):
                c = _compare(ka, kb, d)
                if c:
                    return c
            return 0

        decorated.sort(key=cmp_to_key(cmp))
        return [r for _, r in decorated]

    def tree(self, rows: list[dict], levels: list[tuple[ex.Node, str]], ctx: ex.Context,
             item: str) -> tuple[list[dict], list[dict]]:
        """The rows of a level split by its group's key, in the group's order; a group's
        rows are the concatenation of its children's, so a RUNNING total over a parent
        follows the order the groups are written in. And the table's rows one group after
        the other."""

        def split(rows: list[dict], level: int) -> list[dict]:
            node, direction = levels[level]
            buckets: dict[tuple, tuple[Any, list[dict]]] = {}
            try:
                for r in rows:
                    ctx.row = r
                    k = self.ev(node, ctx)
                    buckets.setdefault(sort_key(k), (k, []))[1].append(r)
            except ex.EvalError as e:
                raise ReportError(f"item «{item}», groups[{level}].by: {e}") from None
            out = []
            for sk in sorted(buckets, key=cmp_to_key(lambda a, b: _compare(a, b, direction))):
                key, sub = buckets[sk]
                children = split(sub, level + 1) if level + 1 < len(levels) else None
                if children is not None:
                    sub = [r for c in children for r in c["rows"]]
                out.append({"key": key, "rows": sub, "children": children})
            return out

        tree = split(rows, 0)
        return tree, [r for node in tree for r in node["rows"]]

    def details(self, rows: list[dict]) -> list[dict]:
        return rows

    def partition(self, rows: list[dict], node: ex.Node, ctx: ex.Context, item: str,
                  where: str) -> list[tuple[Any, list[dict]]]:
        buckets: dict[tuple, tuple[Any, list[dict]]] = {}
        try:
            for r in rows:
                ctx.row = r
                k = self.ev(node, ctx)
                buckets.setdefault(sort_key(k), (k, []))[1].append(r)
        except ex.EvalError as e:
            raise ReportError(f"item «{item}», {where}: {e}") from None
        return list(buckets.values())

    def union(self, parts: list[list[dict]]) -> list[dict]:
        return [r for sub in parts for r in sub]

    def intersect(self, a: list[dict], b: list[dict]) -> list[dict]:
        hit = self._ids.get(id(b))
        if hit is None or hit[0] is not b:
            hit = (b, {id(r) for r in b})
            self._ids[id(b)] = hit
        ids = hit[1]
        return [r for r in a if id(r) in ids]

    def points(self, rows: list[dict], x: ex.Node, y: ex.Node, ctx: ex.Context, item: str) -> list[tuple]:
        out = []
        for r in rows:
            ctx.row = r
            try:
                out.append((self.ev(x, ctx), self.ev(y, ctx)))
            except ex.EvalError as e:
                raise ReportError(f"item «{item}»: {e}") from None
        return out

    def matrix(self, rows: list[dict], levels: list[ex.Node], ctx: ex.Context,
               item: str) -> tuple[list[tuple], dict, Any]:
        keys_of: dict[int, tuple] = {}
        distinct: dict[tuple, tuple] = {}
        try:
            for r in rows:
                ctx.row = r
                raw = tuple(self.ev(n, ctx) for n in levels)
                sk = tuple(sort_key(v) for v in raw)
                keys_of[id(r)] = sk
                distinct.setdefault(sk, raw)
        except ex.EvalError as e:
            raise ReportError(f"item «{item}», matrix.columns_by: {e}") from None
        order = sorted(distinct, key=cmp_to_key(lambda a, b: next(
            (c for c in (_compare(x, y, "asc") for x, y in zip(a, b)) if c), 0)))
        return order, distinct, {"keys_of": keys_of, "order": order, "by": {}}

    def matrix_sub(self, scope: list[dict], state: dict, j: int) -> list[dict]:
        hit = state["by"].get(id(scope))
        if hit is None or hit[0] is not scope:
            by_key: dict[tuple, list[dict]] = {}
            for r in scope:
                by_key.setdefault(state["keys_of"][id(r)], []).append(r)
            hit = (scope, by_key)
            state["by"][id(scope)] = hit
        return hit[1].get(state["order"][j], [])


class _Builder:
    def __init__(self, report: rd.Report, data: dict[str, DataSet], ctx: ex.Context, state: dict | None,
                 interactive: bool, layout: bool = True, store: frames.Store | None = None, max_pages: int = 400):
        self.r = report
        self.max_pages = max_pages
        self.data = data
        self.loc = ctx.loc
        self.ev = ex.Evaluator()
        names = [d.name for d in report.datasets]
        self.py = PyData(data, self.ev)
        self.ctx_py = ctx.at(datasets=_Datasets(names, self.py.rows))
        self.sql = frames.SqlData(store, self.ev) if store is not None else None
        if self.sql is not None:
            sql = self.sql

            def sql_rows(name: str) -> Any:
                try:
                    return sql.rows(name)
                except frames.Fallback:  # a dataset the interpreter keeps
                    return self.py.rows(name)

            self.ctx_sql = ctx.at(datasets=_Datasets(names, sql_rows))
        self.d: Any = self.sql or self.py
        self.ctx = self.ctx_sql if self.sql is not None else self.ctx_py
        items = state.get("items") if isinstance(state, dict) else None  # what a browser sent: it may be anything
        self.state = {k: v for k, v in items.items() if isinstance(v, dict)} if isinstance(items, dict) else {}
        self.interactive = interactive
        self.charts: list[ChartJob] = []
        self.files: dict[str, bytes] = {}
        self.blocks: list[tuple[str, Any]] = []
        self.warnings: list[str] = []
        self.in_body = False  # the email shows the body's texts, KPIs and charts
        self.layout = layout  # the pages are laid out (pdf, svg): what does not fit them is said

    def tables(self) -> list[TableOut]:
        return [t for kind, t in self.blocks if kind == "table"]

    def note(self, kind: str, what: Any) -> None:
        if self.in_body:
            self.blocks.append((kind, what))

    def either(self, it: Any, fn: Any, *args: Any) -> Any:
        """An item worked out in DuckDB, or, where DuckDB cannot be sure of the interpreter's
        result, worked out again the interpreter's way, with what the first try left undone."""
        if self.sql is None or self.d is self.py:
            return fn(*args)
        marks = (len(self.charts), len(self.files), len(self.blocks), len(self.warnings))
        try:
            return fn(*args)
        except frames.Fallback as e:
            logger.info("report: item «%s» by the interpreter: %s", getattr(it, "id", "?"), e)
        del self.charts[marks[0]:]
        for name in list(self.files)[marks[1]:]:
            del self.files[name]
        del self.blocks[marks[2]:]
        del self.warnings[marks[3]:]
        self.d, self.ctx = self.py, self.ctx_py
        try:
            return fn(*args)
        finally:
            self.d, self.ctx = self.sql, self.ctx_sql

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

    def name(self, v: Any) -> str:
        """A value that names something — a group, a chart's category or series, a matrix's
        column: a missing one is named too («(blank)»), never an empty label."""
        return self.loc.texts["blank"] if v is None else self.show(v, None)

    def rows_of(self, name: str | None) -> Any:
        return self.d.rows(name)

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
        return self.either(it, self._placed, it, dy)

    def _placed(self, it: Any, dy: float = 0.0) -> dict | None:
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
        # no value (no rows, a division by zero): a dash, not an empty card
        shown = "—" if value is None else self.show(value, it.format)
        out: dict[str, Any] = {"label": it.label, "value": shown, "change": None, "spark": None}
        if self.layout and glyphs.width_mm(shown, it.style.size or 16, 700) > it.w - 5:  # 2.5 mm inside each side
            self.warnings.append(f"KPI «{it.id}»: its value «{shown}» is wider than the card and is cut: widen the card, "
                                 "or give the value a smaller size or a shorter format")
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
    def partition(self, it: Any, where: str, by: str, rows: Any) -> list[tuple[Any, Any]]:
        """The rows by the value of `by`, each value where it first comes in the rows: a
        chart's axis, its series and a sparkline follow the dataset's own order (its SQL's
        ORDER BY), never the alphabet of what the values print as."""
        return self.d.partition(rows, rd.parse(by), self.ctx.at(scopes=[rows]), it.id, where)

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
                    for x, y in self.d.points(sub, rd.parse(it.category), rd.parse(s.value), base.at(), it.id):
                        points.append((self._num(x), self._num(y), self.show(y, fmt)))
                data.points.append((self.name(key) if it.split_by else (s.name or ""), points))
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

        # the order (docs/specs/reports.md, `options.sort`): the data's, or the first series'
        # value; `top` keeps the N largest values, in that order, and gathers the rest as «Other»
        o = it.options
        other: list[dict] = []
        if o.sort != "none" or o.top:
            values = {id(sub): sort_key(self._num(measure(sub, first, 0))) for _, sub in cats}

            def by_value(direction: str) -> Any:
                return cmp_to_key(lambda a, b: _compare(values[id(a[1])], values[id(b[1])], direction))

            if o.top and len(cats) > o.top:
                kept = {id(sub) for _, sub in sorted(cats, key=by_value("desc"))[:o.top]}
                other = self.d.union([sub for _, sub in cats if id(sub) not in kept])
                cats = [c for c in cats if id(c[1]) in kept]
            if o.sort != "none":
                cats.sort(key=by_value(o.sort))
        if len(cats) > charts.MAX_CATEGORIES:
            raise ReportError(f"item «{it.id}»: {len(cats):,} categories, the most a chart draws is "
                              f"{charts.MAX_CATEGORIES:,}: set its option «top», or aggregate")
        data.categories = [self.name(k) for k, _ in cats] + ([self.loc.texts["other"]] if other else [])
        subsets = [sub for _, sub in cats] + ([other] if other else [])
        if it.split_by:
            splits = self.partition(it, "split_by", it.split_by, rows)
            if len(splits) > charts.MAX_SERIES:
                raise ReportError(f"item «{it.id}»: split_by gives {len(splits)} series, the most is "
                                  f"{charts.MAX_SERIES}")
            for key, srows in splits:
                values = [measure(self.d.intersect(sub, srows), first, 0) for sub in subsets]
                data.series.append(charts.SeriesData(self.name(key), [self._num(v) for v in values],
                                                     [self.show(v, first.format or it.options.format) for v in values],
                                                     first.as_ or "bar", first.axis, first.format or it.options.format))
        else:
            for i, s in enumerate(it.series):
                values = [measure(sub, s, i) for sub in subsets]
                fmt = s.format or it.options.format
                data.series.append(charts.SeriesData(s.name, [self._num(v) for v in values],
                                                     [self.show(v, fmt) for v in values],
                                                     s.as_ or ("line" if i and it.kind == "combo" else "bar"),
                                                     s.axis, fmt))
        if it.kind == "bullet":
            data.targets = [self._target(it, base.at(scopes=[rows, sub])) for sub in subsets]
        data.total_label = self.loc.texts["total"]

    # ── tables ──
    def table(self, it: rd.TableItem) -> TableOut:
        return _Table(self, it).build()

    def _visible_table(self, it: rd.TableItem) -> TableOut | None:
        return self.table(it) if self.visible(it) else None

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
                noted = len(self.blocks)
                close()
                out = self.either(it, self._visible_table, it)
                if out is not None:
                    blocks.append({"k": "table", "gap": _round_mm(max(it.y - prev_bottom, 0)), **self.typst_table(out)})
                    above = self.blocks[noted:]
                    if above and all(kind == "text" for kind, _ in above):
                        # a row of texts right above a table introduces it (its title): the email
                        # shows them with the table's summary, or not at all
                        self.blocks[noted:] = [("caption", what) for _, what in above]
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
        sortable = it.interactive.sort and it.matrix is None  # a matrix's columns are values, not columns to sort by
        if sortable and isinstance(st.get("sort"), dict):
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
        widths = self.column_widths(t)
        return {"x": _round_mm(it.x), "w": _round_mm(it.w), "id": it.id,
                "cols": [{"w": w, "align": c.align or ("right" if c.kind == "number" else "left"), "st": c.style}
                         for w, c in zip(widths, t.cols)],
                "head": t.head, "rows": rows, "repeat": it.repeat_header, "levels": t.levels,
                "hot": {"sort": sortable, "toggle": it.interactive.toggle} if self.interactive else None,
                "sorted": sorted_by}


    # ── what does not fit ──
    def column_widths(self, t: TableOut) -> list[float | None]:
        """The columns' widths in mm, None sharing what is left: fixed widths wider than the
        table are narrowed to fit it (the last columns would print past the paper), and a
        column too narrow for its values is said — they would run into the next one."""
        it = t.item
        widths: list[float | None] = [c.width for c in t.cols]
        fixed = sum(w for w in widths if w)
        shared = sum(1 for w in widths if not w)
        room = max(it.w - MIN_SHARE * shared, it.w * 0.3)
        if fixed > room + 0.05:
            widths = [_round_mm(w * room / fixed) if w else None for w in widths]
            if self.layout:
                self.warnings.append(f"table «{it.id}»: its columns are {fixed:.0f} mm wide, the table {it.w:.0f} mm: "
                                     "they are narrowed to fit it, widen the table or narrow them")
        if not self.layout:
            return widths
        left = it.w - sum(w for w in widths if w)
        actual = [w if w else left / shared for w in widths]
        size = self.r.theme.size
        narrow: list[str] = []
        for j, (c, w) in enumerate(zip(t.cols, actual)):
            room_j = w - 2 * CELL_PAD + 0.2
            for row in t.rows:
                if row.kind not in ("d", "f", "t") or j >= len(row.cells) or (row.kind != "d" and j < row.span):
                    continue
                cell = row.cells[j]
                st = {**c.style, **(cell.style or {})} if cell.style else c.style
                weight, pt = 700 if row.kind != "d" else st.get("weight", 400), st.get("size", size)
                # a word shorter than the room over the widest character fits: not measured
                fits = room_j / (glyphs.widest(weight) * pt * glyphs.PT_MM)
                if any(len(word) > fits and glyphs.width_mm(word, pt, weight) > room_j for word in cell.text.split()):
                    narrow.append(f"«{c.header}» ({w:.0f} mm)" if c.header else f"{j + 1} ({w:.0f} mm)")
                    break
        if narrow:
            names = ", ".join(narrow[:5]) + (f" and {len(narrow) - 5} more" if len(narrow) > 5 else "")
            self.warnings.append(f"table «{it.id}»: values wider than their column run into the next one, in the "
                                 f"column{'s' if len(narrow) > 1 else ''} {names}: widen them, or give them a "
                                 "shorter format")
        return widths

    def past_the_paper(self, it: Any, where: str) -> None:
        """An item that goes past the edge of the paper is cut there: say so."""
        w, _ = self.r.page.dimensions()
        top, right, bottom, left = self.r.page.margins
        x0, x1 = sorted((it.x, it.x + it.w))
        if left + x1 > w + 0.5 or left + x0 < -0.5:
            self.warnings.append(f"{where} «{it.id}» goes past the edge of the paper and is cut there: "
                                 "move it or make it narrower")


def _listed(v: Any) -> list:
    return v if isinstance(v, list) else []


class _Table:
    """One table (or matrix) laid out as rows."""

    def __init__(self, b: _Builder, it: rd.TableItem):
        self.b = b
        self.it = it
        self.loc = b.loc
        state = b.state.get(it.id) or {}
        sortable = it.interactive.sort and it.matrix is None
        self.sort_state = state.get("sort") if sortable and isinstance(state.get("sort"), dict) else None
        toggle = it.interactive.toggle
        self.collapse_all = bool(state.get("collapse_all")) if toggle else False
        self.collapsed = {json.dumps(p) for p in _listed(state.get("collapsed"))} if toggle else set()
        self.expanded = {json.dumps(p) for p in _listed(state.get("expanded"))} if toggle else set()
        self.out: list[Row] = []
        self.zebra = False
        self.shown = 0  # the detail rows written so far

    def value(self, where: str, text: str, ctx: ex.Context) -> Any:
        return self.b.value(self.it, where, text, ctx)

    # ── rows: filtered, sorted, grouped ──
    def build(self) -> TableOut:
        it = self.it
        d = self.b.d
        rows = self.b.rows_of(it.dataset)
        if it.filter:
            rows = d.where(rows, rd.parse(it.filter), self.b.ctx.at(scopes=[rows]), it.id)
        rows = self.sorted(rows)
        self.all_rows = rows
        if it.matrix:
            return self.matrix(rows)
        self.cols = [ColOut(c.header, c.width, c.align, c.format or c.style.format, _style(c.style),
                            subtotal=_subtotal_fn(c)) for c in it.columns]
        if not it.groups:
            self.details(rows, [rows])
        else:
            tree, ordered = self.tree(rows)
            start = 0
            for i, node in enumerate(tree):
                self.group(node, i, len(tree), [rows], ordered, start, [], [])
                start += len(node["rows"])
        # an empty table says «No data» (the template), not a total of nothing
        if it.grand_total and rows and any(c.total for c in it.columns):
            self.footer(None, self.b.ctx.at(row=None, scopes=[rows]), keep=True)
        head = [[{"t": c.header, "span": 1, "rspan": 1, "align": c.align or ("right" if c.kind == "number" else "left")}
                 for c in self.cols]]
        return TableOut(it, it.title or it.id, self.cols, head, self.out, len(it.groups))

    def sorted(self, rows: Any) -> Any:
        keys: list[tuple[str, str]] = [(s.by, s.dir) for s in self.it.sort]
        if self.sort_state is not None:
            col = self.sort_state.get("column")
            if isinstance(col, int) and 0 <= col < len(self.it.columns):
                keys = [(self.it.columns[col].value, "desc" if self.sort_state.get("dir") == "desc" else "asc")]
        if not keys:
            return rows
        return self.b.d.sort(rows, [(rd.parse(by), d) for by, d in keys], self.b.ctx.at(scopes=[rows]), self.it.id)

    def tree(self, rows: Any) -> tuple[list[dict], Any]:
        """The groups of every level, each {key, rows, children}, in the groups' order; and
        the table's rows one group after the other."""
        levels = [(rd.parse(g.by), g.sort) for g in self.it.groups]
        return self.b.d.tree(rows, levels, self.b.ctx.at(scopes=[self.all_rows]), self.it.id)

    def is_closed(self, path: list) -> bool:
        p = json.dumps(path)
        if self.collapse_all:
            return p not in self.expanded
        return p in self.collapsed

    # ── groups ──
    def group(self, node: dict, index: int, siblings: int, scopes: list[Any], parent_rows: Any, start: int,
              path: list, open_headers: list[Row]) -> None:
        """A group, whose rows begin at `start` among its parent's (a running total over the
        parent stops at its last)."""
        it = self.it
        level = len(path)
        g = it.groups[level]
        rows = node["rows"]
        here = [*scopes, rows]
        path = [*path, key_id(node["key"])]
        last = start + len(rows) - 1 if rows else -1
        gctx = self.b.ctx.at(row=None, scopes=here, running=parent_rows, running_index=last, rownumber=index + 1)
        closed = self.is_closed(path)
        has_content = not closed and (node["children"] is not None or it.detail)
        first = len(self.out)
        header = None
        if g.header:
            label = self.value(f"groups[{level}].label", g.label, gctx) if g.label else node["key"]
            header = Row("g", level, label=self.b.name(label), key=path, closed=closed, repeat=has_content)
            self.out.append(header)
        headers = [*open_headers, header] if header is not None and header.repeat else open_headers
        if not closed:
            if node["children"] is not None:
                offset = 0
                for i, child in enumerate(node["children"]):
                    self.group(child, i, len(node["children"]), here, rows, offset, path, headers)
                    offset += len(child["rows"])
            elif it.detail:
                self.zebra = False
                self.details(rows, here)
        # a footer shows its subtotals: with none it is an empty row, unless it is all that names the group
        if g.footer and (not g.header or any(c.total for c in it.columns)):
            label = self.value(f"groups[{level}].label", g.label, gctx) if g.label else node["key"]
            self.footer(level, gctx, keep=g.keep_together, label=f"{self.loc.texts['total']} {self.b.name(label)}",
                        first=first)
        if g.page_break == "after" or (g.page_break == "between" and index < siblings - 1):
            self.out.append(Row("pb"))
            if index < siblings - 1:
                # the groups around this one start the next page again with their headers
                for h in open_headers:
                    self.out.append(Row("g", h.level, label=h.label, key=h.key, closed=h.closed, continued=True))

    def details(self, rows: Any, scopes: list[Any]) -> None:
        """The rows a table shows: the only ones that become Python's values — and when the
        pages could not hold them, none does."""
        it = self.it
        self.shown += len(rows)
        if self.b.layout:
            _, h = self.b.r.page.dimensions()
            top, _, bottom, _ = self.b.r.page.margins
            most = int(self.b.max_pages * max(h - top - bottom, 0) / MIN_ROW)
            if self.shown > most:
                total = len(self.all_rows)
                raise ReportError(f"item «{it.id}»: {total:,} rows to show, more than {self.b.max_pages} pages hold: "
                                  "show its groups' totals without the detail, or filter it")
        rows = self.b.d.details(rows)
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
        order, distinct, self.matrix_columns = self.b.d.matrix(rows, levels, self.b.ctx.at(scopes=[rows]), it.id)
        if len(order) > rd.MAX_MATRIX_COLUMNS:
            raise ReportError(f"item «{it.id}»: the matrix would have {len(order):,} columns, the most is "
                              f"{rd.MAX_MATRIX_COLUMNS}: filter the rows, or choose another columns_by")
        self.matrix_keys = order
        first = it.columns[0] if it.columns else None
        label_col = ColOut(first.header if first else "", first.width if first else None, "left", None,
                           _style(first.style) if first else {})
        # a value column is named by its path of values (the head draws the levels apart)
        self.cols = [label_col] + [ColOut(" · ".join(self.b.name(v) for v in distinct[k]), m.width, "right",
                                          m.format, {}) for k in order]
        if m.total:
            self.cols.append(ColOut(self.loc.texts["total"], m.width, "right", m.format, {"weight": 700}))
        head = self.matrix_head(order, distinct, label_col.header, len(levels), m.total)
        if it.groups:
            tree, _ = self.tree(rows)
            for i, node in enumerate(tree):
                self.matrix_group(node, i, len(tree), [rows], rows, [], [])
        elif rows:
            self.matrix_row(self.loc.texts["total"], [rows], "d")
        if it.grand_total and it.groups and rows:
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
                    row.append({"t": self.b.name(distinct[order[i]][lv]), "span": span, "rspan": 1,
                                "align": "center"})
                    i = j
                else:
                    row.append({"t": self.b.name(distinct[order[i]][lv]), "span": 1, "rspan": 1, "align": "right"})
                    i += 1
            if lv == 0 and total:
                row.append({"t": self.loc.texts["total"], "span": 1, "rspan": depth, "align": "right"})
            head.append(row)
        return head

    def matrix_cells(self, rows: Any, scopes: list[Any], ctx: ex.Context) -> list[Cell]:
        m = self.it.matrix
        assert m is not None
        cells = []
        for j in range(len(self.matrix_keys)):
            sub = self.b.d.matrix_sub(rows, self.matrix_columns, j)
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
        label = self.b.name(self.value(f"groups[{level}].label", g.label, gctx) if g.label else node["key"])
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
          state: dict | None = None, title: str = "", max_pages: int = 400, interactive: bool = False,
          layout: bool = True, store: frames.Store | None = None) -> Model:
    """The model of a report whose data has been read; `layout`: its pages will be laid out
    (pdf, svg), and what does not fit them is said in its warnings. `store`: the datasets in
    DuckDB (frames.py); None builds one for this model when the data's columns are on (the
    default), or works it all out the interpreter's way."""
    if store is None and frames.enabled():
        with frames.build_store(report, data, ctx.loc) as own:
            return _build(report, data, params, ctx, state, title, max_pages, interactive, layout, own)
    return _build(report, data, params, ctx, state, title, max_pages, interactive, layout, store)


def _build(report: rd.Report, data: dict[str, DataSet], params: dict[str, Any], ctx: ex.Context, state: dict | None,
           title: str, max_pages: int, interactive: bool, layout: bool, store: frames.Store | None) -> Model:
    b = _Builder(report, data, ctx, state, interactive, layout, store, max_pages)
    for section, it in report.items() if layout else ():
        b.past_the_paper(it, "table" if it.type in rd.GROWING else "item")
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
