"""One render of a report, from its definition to a file (docs/specs/reports.md, «Rendering
(engine)»): the parameters converted, every dataset read (the cap plus one), the
expressions checked against the columns the data really has, the calculated fields, the
render model, the charts drawn by the chart service, then the format asked for —

  pdf   Typst on the fixed template, in a child process with its limits
  svg   the same compile, one SVG per page, as JSON {"pages": [...], "hotspots": [...]}:
        the pages on screen, with where a click sorts a column or opens a group
  xlsx  the tables and matrices, one sheet each, and the parameters
  html  the email's body as MHTML: the summary, the charts as PNG by cid
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core import placeholders as ph
from app.reports import charts, data as rdata, definition as rd, email_html, expressions as ex, formats, frames
from app.reports import model as rmodel, pdf, xlsx
from app.reports.errors import OverCap, ReportError

FORMATS = ("pdf", "svg", "xlsx", "html")
EXTENSION = {"pdf": "pdf", "svg": "json", "xlsx": "xlsx", "html": "mhtml"}


@dataclass
class Limits:
    max_rows: int = 20_000
    max_pages: int = 400
    memory_mb: int = 1024
    seconds: float = 120
    chart_url: str = ""

    @classmethod
    def from_settings(cls) -> "Limits":
        from app.core.config import get_settings

        r = get_settings().reports
        return cls(r.max_rows, r.max_pages, r.render_memory_mb, r.render_seconds, r.chart_url)


@dataclass
class Rendered:
    path: Path
    pages: int | None = None
    warnings: list[str] = field(default_factory=list)


def _write(root: Path, name: str, payload: bytes | str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload if isinstance(payload, bytes) else payload.encode("utf-8"))


def load(definition: dict) -> rd.Report:
    try:
        report = rd.load(definition)
    except rd.DefinitionError as e:
        raise ReportError(f"the report's definition: {e}") from None
    problems = rd.problems(report, any_field=True)  # the fields: once the data is read
    if problems:
        raise ReportError(f"the report's definition: {problems[0]}")
    return report


def render(definition: dict, datasets: dict[str, dict], params: dict[str, Any] | None, fmt: str, workdir: Path,
           *, locale: str | None = None, state: dict | None = None, title: str = "", limits: Limits | None = None,
           now: datetime | None = None) -> Rendered:
    """The report as a file in `workdir`; ReportError with what stopped it."""
    if fmt not in FORMATS:
        raise ReportError(f"unknown format «{fmt}»: use pdf, xlsx, html or svg", "bad_request")
    limits = limits or Limits.from_settings()
    workdir.mkdir(parents=True, exist_ok=True)
    report = load(definition)
    if locale:
        report.locale = locale if locale in formats.LOCALES else report.locale
    loc = formats.locale(report.locale)
    tz = ph.zone(report.timezone)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    pctx = ph.Context(now=now_local, tz=tz, lang=loc.code)
    values = rdata.parameters(report, params or {}, pctx)
    ctx = ex.Context(loc=loc, now=now_local, params=values, report=title, ph_context=pctx)

    data = rdata.read(report, datasets or {}, limits.max_rows)
    columns = {name: ds.columns for name, ds in data.items()}
    problems = rd.problems(report, columns)
    if problems:
        raise ReportError(f"the report does not fit its data: {problems[0]}")
    m = _model(report, data, values, ctx, state=state, title=title, max_pages=limits.max_pages,
               interactive=fmt == "svg", layout=fmt in ("pdf", "svg"))
    out = workdir / f"report.{EXTENSION[fmt]}"
    if fmt == "xlsx":
        size = report.page.size if isinstance(report.page.size, str) else None
        xlsx.write(m, str(out), loc, paper=size)
        return Rendered(out, None, m.warnings)

    svgs = charts.render_all(m.charts, limits.chart_url)
    root = workdir / "typst"
    root.mkdir(parents=True, exist_ok=True)
    for name, svg in svgs.items():
        _write(root, name, svg)
    if fmt == "html":
        jobs = [j for j in m.charts if any(kind == "chart" and what[1] == j.name for kind, what in m.blocks)]
        pngs = [{"src": j.name, "w": j.width, "h": j.height, "output": str(root / (j.name + ".png"))} for j in jobs]
        if pngs:
            pdf.run({"root": str(root), "pngs": pngs}, limits.memory_mb, limits.seconds)
        images = {j.name: (root / (j.name + ".png")).read_bytes() for j in jobs}
        out.write_bytes(email_html.build(m, images, loc))
        return Rendered(out, None, m.warnings)

    for name, payload in m.files.items():
        _write(root, name, payload)
    pdf.prepare(root)
    job = {"root": str(root), "main": "report.typ", "format": fmt, "output": str(out), "hotspots": fmt == "svg",
           "inputs": {"model": json.dumps(m.doc, ensure_ascii=False, separators=(",", ":"))}}
    result = pdf.run(job, limits.memory_mb, limits.seconds, limits.max_pages)
    return Rendered(out, result.get("pages"), m.warnings)


def _model(report: rd.Report, data: dict[str, rdata.DataSet], values: dict[str, Any], ctx: ex.Context,
           **kw: Any) -> rmodel.Model:
    """The calculated fields and the render model: in DuckDB (app/reports/frames.py), which
    is closed with them, or the interpreter's way."""
    if not frames.enabled():
        # given before the calculated fields: one may read an aggregate of another dataset
        ctx.datasets = {name: ds.rows for name, ds in data.items()}
        rdata.add_calculated(report, data, ctx, ex.Evaluator())
        return rmodel.build(report, data, values, ctx, **kw)
    with frames.build_store(report, data, ctx.loc) as store:
        ctx.datasets = store.datasets(data)
        frames.add_calculated(report, data, store, ctx)
        return rmodel.build(report, data, values, ctx, store=store, **kw)


def filename(name: str | None, fmt: str) -> str:
    """The file's name with the extension of what is in it (an MHTML is not an .html)."""
    ext = EXTENSION.get(fmt, fmt)
    base = (name or "").strip()
    if not base:
        return f"report.{ext}"
    stem = base.rsplit(".", 1)[0] if "." in base and base.rsplit(".", 1)[1].lower() in (*FORMATS, *EXTENSION.values()) \
        else base
    return f"{stem}.{ext}"


def _only_shown(spec: Any, value: str, label: str | None, calculated: set[str]) -> dict | None:
    """A chain read for a list whose value and label are plain fields: those columns, each
    pair once, sorted, instead of every column of every row: `[area]` on millions of rows
    is the normal case. None for anything else, which reads the rows, and for a query with
    an ORDER BY of its own: its order is the list's."""
    if not isinstance(spec, dict) or "input_key" not in spec:
        return None
    if any(op.get("type") == "sql" and re.search(r"\border\s+by\b", str((op.get("params") or {}).get("query")), re.I)
           for op in spec.get("operations") or []):
        return None
    try:
        nodes = [rd.parse(t) for t in (label, value) if t]
    except ex.ExpressionError:
        return None
    if not all(isinstance(n, ex.Field) and n.name not in calculated for n in nodes):
        return None
    columns = list(dict.fromkeys(n.name for n in nodes))
    return {**spec, "operations": [*(spec.get("operations") or []), {"type": "select", "params": {"columns": columns}},
                                   {"type": "unique", "params": {}}, {"type": "sort", "params": {"by": columns}}]}


def choices(definition: dict, dataset: str, datasets: dict[str, dict], params: dict[str, Any] | None, value: str,
            label: str | None = None, limit: int = 500, locale: str | None = None, max_rows: int | None = None,
            now: datetime | None = None) -> dict:
    """The choices of a parameter taken from a dataset: `value` and `label` evaluated on
    every row, each value once, in the order of the rows (`_only_shown` for what a chain
    reads); `truncated` when there are more than `limit`. The parameters are those of a
    form still being filled in (not strict)."""
    try:
        report = rd.load(definition)
    except rd.DefinitionError as e:
        raise ReportError(f"the report's definition: {e}") from None
    if report.dataset(dataset) is None:
        raise ReportError(f"there is no dataset «{dataset}»")
    loc = formats.locale(locale if locale in formats.LOCALES else report.locale)
    tz = ph.zone(report.timezone)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    pctx = ph.Context(now=now_local, tz=tz, lang=loc.code)
    values = rdata.parameters(report, params or {}, pctx, strict=False)
    ctx = ex.Context(loc=loc, now=now_local, params=values, ph_context=pctx)
    spec, cap = (datasets or {}).get(dataset), max_rows or Limits.from_settings().max_rows
    calculated = {c.name for c in report.dataset(dataset).calculated}
    ds = None
    if (shown := _only_shown(spec, value, label, calculated)) is not None:
        try:
            ds = rdata.read_dataset(dataset, shown, cap, tz)
        except ReportError:  # a field it does not have, say: as it is said when the rows are read
            pass
    narrowed = ds is not None
    if ds is None:
        try:
            ds = rdata.read_dataset(dataset, spec, cap, tz)
        except OverCap as e:  # read whole for its list, without the filter a render reads it with
            raise OverCap(f"{e.message}. For its list, make the value a plain field, or read the choices "
                          "from a dataset of their own (SELECT DISTINCT)") from None
    env = ex.Env(fields={**ds.columns, **dict.fromkeys(calculated, "")}, dataset=dataset,
                 params={p.name: (ex.LIST if p.multi else rd._PARAM_TYPES[p.type]) for p in report.parameters},
                 aggregates=False)
    nodes = []
    for where, text in (("value", value), ("label", label)):
        if text:
            try:
                node = rd.parse(text)
                ex.check(node, env)
            except ex.ExpressionError as e:
                raise ReportError(f"the choices' {where}: {e.message} (at character {e.pos + 1})") from None
            nodes.append(node)
        else:
            nodes.append(None)
    if not narrowed:  # those fields are not read: nothing here needs them
        rdata.add_calculated(report, {dataset: ds}, ctx, ex.Evaluator())
    evaluate = ex.Evaluator()
    ctx.scopes = [ds.rows]
    seen: set = set()
    out: list[dict] = []
    for row in ds.rows:
        ctx.row = row
        try:
            v = evaluate(nodes[0], ctx)
            shown = evaluate(nodes[1], ctx) if nodes[1] is not None else v
        except ex.EvalError as e:
            raise ReportError(f"the choices of the dataset «{dataset}»: {e}") from None
        key = rmodel.sort_key(v)
        if v is None or key in seen:
            continue
        if len(out) == limit:
            return {"choices": out, "truncated": True}
        seen.add(key)
        out.append({"value": rmodel.key_id(v), "label": formats.display(shown, None, loc)})
    return {"choices": out, "truncated": False}
