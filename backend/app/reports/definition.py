"""A report's definition (`v: 1`) as the gateway stores it, read and checked.

docs/specs/reports.md, «The definition». Lengths are millimetres, colours #rrggbb. The
definition is read with pydantic (its shape, its bounds) and then every expression in it
is parsed and checked where it sits — a column's value against the fields of its dataset,
a condition wanting true or false, PAGE() only in a text — so a mistake is named with its
item, the property and the position inside the expression (`problems`). Unknown keys are
ignored: an editor newer than this engine does not break a render.
"""
from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any, Iterator, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core import placeholders as ph
from app.reports import expressions as ex
from app.reports import formats

PAPER = {"A4": (210.0, 297.0), "A3": (297.0, 420.0), "A5": (148.0, 210.0), "Letter": (215.9, 279.4),
         "Legal": (215.9, 355.6)}
MAX_IMAGE_BYTES = 512 * 1024
MAX_MATRIX_COLUMNS = 60
CHART_KINDS = ("bar", "column", "line", "area", "pie", "donut", "combo", "waterfall", "funnel", "treemap", "heatmap",
               "scatter", "gauge", "bullet")
_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"

Mm = Annotated[float, Field(ge=-1000, le=2000, allow_inf_nan=False)]
Length = Annotated[float, Field(gt=0, le=2000, allow_inf_nan=False)]
Color = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]
Expr = Annotated[str, Field(max_length=ex.MAX_EXPRESSION_CHARS)]
Pattern = Annotated[str, Field(max_length=64)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Border(_Model):
    width: float = Field(0.2, ge=0, le=5, allow_inf_nan=False)
    color: Color = "#1f2933"


class Style(_Model):
    size: float | None = Field(None, ge=4, le=96, allow_inf_nan=False)  # points
    weight: Literal["normal", "semibold", "bold", 400, 600, 700] | None = None
    italic: bool | None = None
    color: Color | None = None
    background: Color | None = None
    align: Literal["left", "center", "right"] | None = None
    valign: Literal["top", "middle", "bottom"] | None = None
    padding: float | None = Field(None, ge=0, le=50, allow_inf_nan=False)
    border: Border | None = None
    radius: float | None = Field(None, ge=0, le=50, allow_inf_nan=False)
    format: Pattern | None = None
    can_grow: bool = False


class PageSize(_Model):
    w: Length
    h: Length


class Page(_Model):
    size: Literal["A4", "A3", "A5", "Letter", "Legal"] | PageSize = "A4"
    orientation: Literal["portrait", "landscape"] = "portrait"
    margins: list[Annotated[float, Field(ge=0, le=200, allow_inf_nan=False)]] = Field(
        default_factory=lambda: [15.0, 12.0, 15.0, 12.0], min_length=4, max_length=4)

    def dimensions(self) -> tuple[float, float]:
        w, h = PAPER[self.size] if isinstance(self.size, str) else (self.size.w, self.size.h)
        return (max(w, h), min(w, h)) if self.orientation == "landscape" else (w, h)


class Theme(_Model):
    font: str = "IBM Plex Sans"
    size: float = Field(9, ge=5, le=24, allow_inf_nan=False)
    color: Color = "#1f2933"
    accent: Color = "#3b5bdb"


class Choices(_Model):
    values: list[Any] | None = Field(None, max_length=1000)
    dataset: str | None = None
    value: Expr | None = None
    label: Expr | None = None


class Parameter(_Model):
    name: str = Field(pattern=_NAME)
    label: str = Field("", max_length=200)
    type: Literal["text", "number", "date", "boolean"] = "text"
    multi: bool = False
    required: bool = False
    default: Any = None
    choices: Choices | None = None

    @field_validator("name")
    @classmethod
    def _not_builtin(cls, v: str) -> str:
        if v in ph.BUILTINS or v in ph.OUTPUT_TEXT or v in ph.OUTPUT_ALIASES:
            raise ValueError(f"«{v}» is a placeholder of Tabularia ({{{v}}}): choose another name")
        return v


class FieldInfo(_Model):
    name: str = Field(min_length=1, max_length=256)
    type: str = Field("", max_length=200)


class Calculated(_Model):
    name: str = Field(min_length=1, max_length=256)
    expr: Expr


class Dataset(_Model):
    name: str = Field(pattern=_NAME)
    source: dict = Field(default_factory=dict)
    fields: list[FieldInfo] = Field(default_factory=list, max_length=2000)
    calculated: list[Calculated] = Field(default_factory=list, max_length=200)


class _Item(_Model):
    id: str = Field(min_length=1, max_length=64)
    x: Mm = 0
    y: Mm = 0
    w: Length = 10
    h: Length = 10
    style: Style = Field(default_factory=Style)
    visible: Expr | None = None


class TextItem(_Item):
    type: Literal["text"]
    value: str = Field("", max_length=ex.MAX_EXPRESSION_CHARS)
    dataset: str | None = None


class ImageItem(_Item):
    type: Literal["image"]
    asset: str = Field("", max_length=(MAX_IMAGE_BYTES * 4) // 3 + 64)  # empty: nothing is drawn
    fit: Literal["contain", "cover", "stretch"] = "contain"


class LineItem(_Item):
    type: Literal["line"]
    w: Mm = 10  # a line goes from (x, y) to (x + w, y + h): either may be 0 or negative
    h: Mm = 0


class RectItem(_Item):
    type: Literal["rect"]


class Rule(_Model):
    when: Expr
    style: Style = Field(default_factory=Style)


class Column(_Model):
    header: str = Field("", max_length=500)
    value: Expr
    width: float | None = Field(None, gt=0, le=2000, allow_inf_nan=False)
    align: Literal["left", "center", "right"] | None = None
    format: Pattern | None = None
    total: Expr | None = None
    style: Style = Field(default_factory=Style)
    rules: list[Rule] = Field(default_factory=list, max_length=20)


class Group(_Model):
    by: Expr
    label: Expr | None = None
    header: bool = True
    footer: bool = True
    page_break: Literal["none", "between", "after"] = "none"
    sort: Literal["asc", "desc"] = "asc"
    keep_together: bool = True


class SortKey(_Model):
    by: Expr
    dir: Literal["asc", "desc"] = "asc"


class Matrix(_Model):
    columns_by: Expr | list[Expr]
    value: Expr
    total: bool = True
    format: Pattern | None = None
    width: float | None = Field(None, gt=0, le=500, allow_inf_nan=False)

    @field_validator("columns_by")
    @classmethod
    def _levels(cls, v: str | list[str]) -> str | list[str]:
        if isinstance(v, list) and not 1 <= len(v) <= 3:
            raise ValueError("a matrix has one to three levels of columns")
        return v

    @property
    def levels(self) -> list[str]:
        return self.columns_by if isinstance(self.columns_by, list) else [self.columns_by]


class Interactive(_Model):
    sort: bool = False
    toggle: bool = False


class TableItem(_Item):
    type: Literal["table"]
    dataset: str
    title: str | None = Field(None, max_length=100)
    columns: list[Column] = Field(default_factory=list, max_length=60)
    groups: list[Group] = Field(default_factory=list, max_length=6)
    detail: bool = True
    sort: list[SortKey] = Field(default_factory=list, max_length=10)
    filter: Expr | None = None
    repeat_header: bool = True
    grand_total: bool = False
    zebra: bool = False
    matrix: Matrix | None = None
    interactive: Interactive = Field(default_factory=Interactive)


class Series(_Model):
    value: Expr
    name: str = Field("", max_length=200)
    as_: Literal["bar", "line"] | None = Field(None, alias="as")
    axis: Literal["left", "right"] = "left"


class ChartOptions(_Model):
    stacked: bool = False
    labels: bool = False
    legend: bool | None = None
    sort: Literal["none", "asc", "desc"] = "none"
    top: int | None = Field(None, ge=1, le=500)
    min: float | None = Field(None, allow_inf_nan=False)
    max: float | None = Field(None, allow_inf_nan=False)
    target: float | Expr | None = None
    bands: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(default_factory=list, max_length=10)
    format: Pattern | None = None


class ChartItem(_Item):
    type: Literal["chart"]
    dataset: str
    kind: Literal[CHART_KINDS]  # type: ignore[valid-type]
    category: Expr | None = None
    series: list[Series] = Field(default_factory=list, max_length=12)
    split_by: Expr | None = None
    title: str | None = Field(None, max_length=200)
    options: ChartOptions = Field(default_factory=ChartOptions)


class Sparkline(_Model):
    category: Expr
    value: Expr


class KpiItem(_Item):
    type: Literal["kpi"]
    dataset: str
    label: str = Field("", max_length=200)
    value: Expr
    compare: Expr | None = None
    format: Pattern | None = None
    sparkline: Sparkline | None = None


Item = Annotated[Union[TextItem, ImageItem, LineItem, RectItem, TableItem, ChartItem, KpiItem],
                 Field(discriminator="type")]
GROWING = ("table",)  # items that grow with their data and take their row alone


class Band(_Model):
    height: float = Field(0, ge=0, le=500, allow_inf_nan=False)
    on_first_page: bool = True
    on_last_page: bool = True
    items: list[Item] = Field(default_factory=list, max_length=200)


class Body(_Model):
    items: list[Item] = Field(default_factory=list, max_length=500)


class Sections(_Model):
    page_header: Band = Field(default_factory=Band)
    body: Body = Field(default_factory=Body)
    page_footer: Band = Field(default_factory=Band)


class Report(_Model):
    v: Literal[1] = 1
    page: Page = Field(default_factory=Page)
    locale: Literal[formats.LOCALES] = "en"  # type: ignore[valid-type]
    timezone: str = "UTC"
    theme: Theme = Field(default_factory=Theme)
    parameters: list[Parameter] = Field(default_factory=list, max_length=50)
    datasets: list[Dataset] = Field(default_factory=list, max_length=20)
    sections: Sections = Field(default_factory=Sections)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        try:
            ph.zone(v)
        except ph.PlaceholderError as e:
            raise ValueError(str(e)) from None
        return v or "UTC"

    @model_validator(mode="after")
    def _consistent(self) -> "Report":
        for what, names in (("dataset", [d.name for d in self.datasets]),
                            ("parameter", [p.name for p in self.parameters]),
                            ("item", [it.id for _, it in self.items()])):
            seen: set[str] = set()
            for n in names:
                if n in seen:
                    raise ValueError(f"two {what}s are called «{n}»: names must be unique")
                seen.add(n)
        w, h = self.page.dimensions()
        top, right, bottom, left = self.page.margins
        hh, fh = self.sections.page_header.height, self.sections.page_footer.height
        if left + right >= w - 10 or top + bottom + hh + fh >= h - 10:
            raise ValueError("the margins, the page header and the page footer leave no room for the body")
        return self

    def items(self) -> Iterator[tuple[str, Any]]:
        """Every item with the section it is in."""
        for section in ("page_header", "body", "page_footer"):
            for it in getattr(self.sections, section).items:
                yield section, it

    def dataset(self, name: str) -> Dataset | None:
        return next((d for d in self.datasets if d.name == name), None)


# ── problems ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Problem:
    """A mistake in a definition: the item it is in (None: the report, a dataset, a
    parameter), the property, and for an expression the position (0 = first character)."""

    message: str
    item: str | None = None
    field: str = ""
    pos: int | None = None
    length: int = 0

    def as_dict(self) -> dict:
        return {"item": self.item, "field": self.field, "pos": self.pos, "length": self.length,
                "message": self.message}

    def __str__(self) -> str:
        where = []
        if self.item:
            where.append(f"item «{self.item}»")
        if self.field:
            where.append(self.field)
        at = f" (at character {self.pos + 1})" if self.pos is not None else ""
        return f"{', '.join(where)}: {self.message}{at}" if where else f"{self.message}{at}"


class DefinitionError(ValueError):
    def __init__(self, problems: list[Problem]):
        super().__init__(str(problems[0]) if problems else "the definition is not valid")
        self.problems = problems


def _path(loc: tuple) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + str(part)
    return out


def _structure_problems(raw: dict, e: ValidationError) -> list[Problem]:
    problems = []
    for err in e.errors()[:20]:
        loc = tuple(err.get("loc") or ())
        item = None
        # sections.<band>.items.<i>.<type>.<field>: the item's id, and the property after it
        if len(loc) >= 4 and loc[0] == "sections" and loc[2] == "items" and isinstance(loc[3], int):
            try:
                item = raw["sections"][loc[1]]["items"][loc[3]].get("id")
            except (KeyError, IndexError, TypeError, AttributeError):
                item = None
            rest = loc[5:] if len(loc) > 4 and isinstance(loc[4], str) and loc[4] in (
                "text", "image", "line", "rect", "table", "chart", "kpi") else loc[4:]
            problems.append(Problem(err["msg"].removeprefix("Value error, "), item, _path(rest)))
            continue
        problems.append(Problem(err["msg"].removeprefix("Value error, "), None, _path(loc)))
    return problems


def load(raw: dict) -> Report:
    """The definition read; DefinitionError with every mistake of shape it has."""
    if not isinstance(raw, dict):
        raise DefinitionError([Problem("the definition is not an object")])
    try:
        return Report.model_validate(raw)
    except ValidationError as e:
        raise DefinitionError(_structure_problems(raw, e)) from None


@lru_cache(maxsize=4096)
def parse(text: str) -> ex.Node:
    """The tree of an expression, parsed once per text (it is immutable). A leading «=», as
    a spreadsheet's formula starts, is allowed (no expression starts with it): it becomes a
    space, so the positions of the mistakes stay those of the text."""
    if text.lstrip().startswith("="):
        text = text.replace("=", " ", 1)
    return ex.parse(text)


_TYPE_NAMES = {ex.NUM: "number", ex.TEXT: "text", ex.DATE: "date", ex.DATETIME: "datetime", ex.BOOL: "boolean"}
_PARAM_TYPES = {"text": ex.TEXT, "number": ex.NUM, "date": ex.DATE, "boolean": ex.BOOL}


class _Checks:
    """Collects the problems of the expressions of a definition."""

    def __init__(self, report: Report, columns: dict[str, dict[str, str]] | None, any_field: bool = False):
        self.report = report
        self.problems: list[Problem] = []
        params = {p.name: (ex.LIST if p.multi else _PARAM_TYPES[p.type]) for p in report.parameters}
        # the fields of every dataset: what the data has (at render time) or what the
        # editor last saw; none listed, or `any_field`, = not known, and any name passes
        self.fields: dict[str, dict[str, str] | None] = {}
        for d in report.datasets:
            if any_field:
                self.fields[d.name] = None
            elif columns is not None and d.name in columns:
                self.fields[d.name] = dict(columns[d.name])
            else:
                self.fields[d.name] = {f.name: f.type for f in d.fields} or None
        self.params = params
        for d in report.datasets:
            known = self.fields[d.name]
            for i, c in enumerate(d.calculated):
                t = self.expr(c.expr, None, f"datasets[{d.name}].calculated[{i}]", self.env(d.name))
                if known is not None:
                    known[c.name] = _TYPE_NAMES.get(t or "", "")

    def env(self, dataset: str | None, **kw: Any) -> ex.Env:
        return ex.Env(fields=self.fields.get(dataset) if dataset else {}, dataset=dataset,
                      datasets=self.fields, params=self.params, **kw)

    def expr(self, text: str | None, item: str | None, where: str, env: ex.Env, offset: int = 0) -> str | None:
        if text is None:
            return None
        try:
            node = parse(text)
            return ex.check(node, env)
        except ex.ExpressionError as e:
            self.problems.append(Problem(e.message, item, where, e.pos + offset, e.length))
            return None

    def pattern(self, pattern: str | None, item: str | None, where: str) -> None:
        if pattern:
            try:
                formats.check_pattern(pattern)
            except formats.FormatError as e:
                self.problems.append(Problem(str(e), item, where))

    def dataset_of(self, item: Any, where: str) -> str | None:
        name = getattr(item, "dataset", None)
        if name is None:
            return self.report.datasets[0].name if len(self.report.datasets) == 1 else None
        if self.report.dataset(name) is None:
            self.problems.append(Problem(f"there is no dataset «{name}»", item.id, where))
            return None
        return name

    def run(self) -> list[Problem]:
        r = self.report
        for i, p in enumerate(r.parameters):
            self.parameter(i, p)
        for section, it in r.items():
            self.item(section, it)
        return self.problems

    def parameter(self, i: int, p: Parameter) -> None:
        where = f"parameters[{p.name}]"
        if isinstance(p.default, str) and "{" in p.default:
            try:
                ph.check(p.default)
            except ph.PlaceholderError as e:
                self.problems.append(Problem(str(e), None, f"{where}.default"))
        c = p.choices
        if c and c.dataset:
            if self.report.dataset(c.dataset) is None:
                self.problems.append(Problem(f"there is no dataset «{c.dataset}»", None, f"{where}.choices.dataset"))
                return
            env = self.env(c.dataset, aggregates=False)
            self.expr(c.value, None, f"{where}.choices.value", env)
            self.expr(c.label, None, f"{where}.choices.label", env)

    def item(self, section: str, it: Any) -> None:
        header = section != "body"
        if header and it.type in ("table", "chart", "kpi"):
            self.problems.append(Problem("the page header and footer hold texts, images, lines and rectangles",
                                         it.id, "type"))
            return
        ds = self.dataset_of(it, "dataset")
        base = dict(header=header)
        self.expr(it.visible, it.id, "visible", self.env(ds, want=ex.BOOL, **base))
        self.pattern(it.style.format, it.id, "style.format")
        if it.type == "text":
            text, is_expr = ex.text_value(it.value)
            if is_expr:
                self.expr(text, it.id, "value", self.env(ds, page=True, **base), offset=1)
        elif it.type == "image":
            problem = image_problem(it.asset)
            if problem:
                self.problems.append(Problem(problem, it.id, "asset"))
        elif it.type == "table":
            self.table(it, ds)
        elif it.type == "chart":
            self.chart(it, ds)
        elif it.type == "kpi":
            self.expr(it.value, it.id, "value", self.env(ds))
            self.expr(it.compare, it.id, "compare", self.env(ds))
            self.pattern(it.format, it.id, "format")
            if it.sparkline:
                self.expr(it.sparkline.category, it.id, "sparkline.category", self.env(ds, aggregates=False))
                self.expr(it.sparkline.value, it.id, "sparkline.value", self.env(ds))

    def table(self, it: TableItem, ds: str | None) -> None:
        if ds is None:
            return
        self.expr(it.filter, it.id, "filter", self.env(ds, want=ex.BOOL))
        for i, g in enumerate(it.groups):
            self.expr(g.by, it.id, f"groups[{i}].by", self.env(ds, aggregates=False))
            self.expr(g.label, it.id, f"groups[{i}].label", self.env(ds))
        for i, s in enumerate(it.sort):
            self.expr(s.by, it.id, f"sort[{i}].by", self.env(ds, aggregates=False))
        for i, c in enumerate(it.columns):
            self.expr(c.value, it.id, f"columns[{i}].value", self.env(ds))
            self.expr(c.total, it.id, f"columns[{i}].total", self.env(ds))
            self.pattern(c.format, it.id, f"columns[{i}].format")
            for j, rule in enumerate(c.rules):
                self.expr(rule.when, it.id, f"columns[{i}].rules[{j}].when", self.env(ds, want=ex.BOOL))
        if it.matrix:
            for i, level in enumerate(it.matrix.levels):
                self.expr(level, it.id, f"matrix.columns_by[{i}]", self.env(ds, aggregates=False))
            self.expr(it.matrix.value, it.id, "matrix.value", self.env(ds))
            self.pattern(it.matrix.format, it.id, "matrix.format")
        elif not it.columns:
            self.problems.append(Problem("a table needs at least one column", it.id, "columns"))

    def chart(self, it: ChartItem, ds: str | None) -> None:
        if ds is None:
            return
        self.expr(it.category, it.id, "category", self.env(ds, aggregates=False))
        self.expr(it.split_by, it.id, "split_by", self.env(ds, aggregates=False))
        if not it.series:
            self.problems.append(Problem("a chart needs at least one series (its values)", it.id, "series"))
        for i, s in enumerate(it.series):
            self.expr(s.value, it.id, f"series[{i}].value", self.env(ds))
        if isinstance(it.options.target, str):
            self.expr(it.options.target, it.id, "options.target", self.env(ds))
        self.pattern(it.options.format, it.id, "options.format")
        if it.category is None and it.kind not in ("gauge", "bullet"):
            self.problems.append(Problem(f"a {it.kind} chart needs a category (what goes along its axis)", it.id,
                                         "category"))


def problems(report: Report, columns: dict[str, dict[str, str]] | None = None,
             any_field: bool = False) -> list[Problem]:
    """Every mistake in the expressions and formats of a read definition. `columns`: the
    real columns of the datasets (name → type), when the data has been read; `any_field`:
    before the data is read, every field name passes (a render uses what the data has, not
    the `fields` the editor last saw)."""
    return _Checks(report, columns, any_field).run()


def check(raw: dict, columns: dict[str, dict[str, str]] | None = None, any_field: bool = False) -> list[Problem]:
    """The mistakes of a raw definition: of shape first, then of its expressions."""
    try:
        report = load(raw)
    except DefinitionError as e:
        return e.problems
    return problems(report, columns, any_field)


# ── images ───────────────────────────────────────────────────────────────────
_SVG_EXTERNAL = re.compile(rb"""(?:href|src)\s*=\s*["'](?!#|data:)""", re.I)


def image_bytes(asset: str) -> tuple[bytes, str]:
    """The decoded image and its kind (png, jpg, svg); ValueError when it is none of
    them, too large, or an SVG that reads something outside itself."""
    data = asset.split(",", 1)[1] if asset.startswith("data:") else asset
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the image is not valid base64") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError(f"the image is larger than {MAX_IMAGE_BYTES // 1024} KiB")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw, "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return raw, "jpg"
    head = raw[:1024].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in raw[:4096].lower():
        if _SVG_EXTERNAL.search(raw) or b"<!entity" in raw.lower():
            raise ValueError("the SVG image refers to files or addresses outside itself: embed everything in it")
        return raw, "svg"
    raise ValueError("the image is not a PNG, a JPEG or an SVG")


def image_problem(asset: str) -> str | None:
    """What is wrong with an image; nothing for an empty one (the gateway sends the
    definition to be checked with its images emptied: it checks them itself)."""
    if not asset:
        return None
    try:
        image_bytes(asset)
    except ValueError as e:
        return str(e)
    return None
