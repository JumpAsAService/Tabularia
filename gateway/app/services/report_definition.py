"""What a report's definition may hold (`v: 1`), checked at every save:
docs/specs/reports.md, «The definition»; the report in services/reports.py.

The structure is closed: an unknown key is refused by name, so a mistake is told at the
save and not, later, by a render that ignores it. Only the items' `style` and a chart's
`options` are open (their keys belong to the editor and the renderer), with simple
values and colours as `#rrggbb`. Lengths are millimetres with a tenth at most, and every
item lies on the page: inside the header's or the footer's height, inside the width
between the margins. A table takes its row of the body alone.

What the gateway cannot judge is left to the engine, asked at the save when it can
answer (`POST /reports/check`, services/reports.py): the expressions' grammar, the
fields and functions they name, the formats.

Images are stored in the definition as a data URL, `data:image/png;base64,…`, of 512
KiB at most: PNG, JPEG, or an SVG that is plainly safe — no script, no foreignObject,
no event attribute, no reference outside the image, no DOCTYPE. An SVG that is not is
refused, never «cleaned»: a sanitizer of our own would be one more thing to get wrong.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
from typing import Any, Iterable, Optional
from xml.etree import ElementTree

from fastapi import HTTPException

from app.services import report_params
from app.services.engine_policy import KNOWN_ENGINES

MAX_DEFINITION_BYTES = 4 * 1024 * 1024  # the definition as JSON, images included
MAX_IMAGE_BYTES = 512 * 1024
MAX_PARAMETERS, MAX_DATASETS, MAX_ITEMS, MAX_COLUMNS, MAX_GROUPS, MAX_SERIES = 50, 20, 400, 60, 6, 12
MAX_SQL_CHARS = 100_000  # as a SQL script's
MAX_EXPR = 4000

PAGE_SIZES = {"A4": (210, 297), "A3": (297, 420), "A5": (148, 210), "Letter": (215.9, 279.4),
              "Legal": (215.9, 355.6)}
LOCALES = ("en", "it", "de", "es", "fr")
FIELD_TYPES = ("string", "integer", "float", "date", "datetime", "time", "boolean", "other")
FILTER_OPS = ("=", "<>", "<", "<=", ">", ">=", "in", "contains")
CHART_KINDS = ("bar", "column", "line", "area", "pie", "donut", "combo", "waterfall", "funnel", "treemap",
               "heatmap", "scatter", "gauge", "bullet")
ITEM_TYPES = ("text", "image", "line", "rect", "table", "chart", "kpi")
PAGE_ITEM_TYPES = ("text", "image", "line", "rect")  # what a page header or footer holds
DATASET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STYLE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DATA_URL = re.compile(r"^data:image/(png|jpeg|svg\+xml);base64,([A-Za-z0-9+/=\s]+)$")


class Problem(Exception):
    """What is wrong, and where: the item (its id) and the path inside the definition."""

    def __init__(self, message: str, where: str = "", item: Optional[str] = None):
        super().__init__(message)
        self.message, self.where, self.item = message, where, item

    def http(self) -> HTTPException:
        return HTTPException(status_code=422, detail={"message": self.message, "where": self.where,
                                                      "item": self.item, "errors": []})


# ── small checks ──────────────────────────────────────────────────────────────
def _keys(value: Any, where: str, allowed: Iterable[str], required: Iterable[str] = (), what: str = "",
          item: Optional[str] = None) -> dict:
    if not isinstance(value, dict):
        raise Problem(f"{what or where} is an object", where, item)
    allowed = set(allowed)
    unknown = sorted(k for k in value if k not in allowed)
    if unknown:
        raise Problem(f"{what or where}: unknown key «{str(unknown[0])[:40]}»", where, item)
    missing = [k for k in required if value.get(k) in (None, "")]
    if missing:
        raise Problem(f"{what or where}: «{missing[0]}» is missing", f"{where}.{missing[0]}", item)
    return value


def _number(value: Any, where: str, what: str, lo: float, hi: float, item: Optional[str] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Problem(f"{what} is a number", where, item)
    if not lo <= value <= hi:
        raise Problem(f"{what} is between {lo:g} and {hi:g}", where, item)
    return float(value)


def _mm(value: Any, where: str, what: str, lo: float, hi: float, item: Optional[str] = None) -> float:
    """A length in millimetres, with a tenth at most."""
    v = _number(value, where, what, lo, hi, item)
    if abs(v * 10 - round(v * 10)) > 1e-6:
        raise Problem(f"{what}: millimetres with a tenth at most ({v:g})", where, item)
    return v


def _bool(value: Any, where: str, what: str, item: Optional[str] = None) -> None:
    if value is not None and not isinstance(value, bool):
        raise Problem(f"{what} is true or false", where, item)


def _text(value: Any, where: str, what: str, limit: int = 500, item: Optional[str] = None,
          required: bool = False) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise Problem(f"{what} is a text" + (" that cannot be empty" if required else ""), where, item)
    if len(value) > limit:
        raise Problem(f"{what} is at most {limit} characters", where, item)
    return value


def _expr(value: Any, where: str, what: str, item: Optional[str] = None, required: bool = False) -> Optional[str]:
    return _text(value, where, what, MAX_EXPR, item, required)


def _choice(value: Any, options: Iterable[str], where: str, what: str, item: Optional[str] = None,
            required: bool = False) -> None:
    if value is None and not required:
        return
    if value not in tuple(options):
        raise Problem(f"{what} is one of {', '.join(options)}", where, item)


def _color(value: Any, where: str, what: str, item: Optional[str] = None) -> None:
    if value is not None and (not isinstance(value, str) or not COLOR.match(value)):
        raise Problem(f"{what} is a colour as #rrggbb", where, item)


def _open(value: Any, where: str, what: str, item: Optional[str] = None, depth: int = 0) -> None:
    """`style` and `options`: keys of the editor and the renderer, simple values; a key
    that names a colour holds #rrggbb."""
    if value is None:
        return
    if not isinstance(value, dict) or len(value) > 40:
        raise Problem(f"{what} is an object of at most 40 keys", where, item)
    for k, v in value.items():
        at = f"{where}.{k}"
        if not isinstance(k, str) or not STYLE_KEY.match(k):
            raise Problem(f"{what}: «{str(k)[:40]}» is not a key (lower case letters, digits, _)", where, item)
        if isinstance(v, str):
            if len(v) > 500:
                raise Problem(f"{what}.{k} is at most 500 characters", at, item)
            if ("color" in k or k in ("background", "fill")) and v:
                _color(v, at, f"{what}.{k}", item)
        elif isinstance(v, (list, dict)):
            if depth >= 2 or len(v) > 40:
                raise Problem(f"{what}.{k} is too deep or too long", at, item)
            for sub in (v.values() if isinstance(v, dict) else v):
                if isinstance(sub, (list, dict)):
                    _open(sub if isinstance(sub, dict) else {"v": sub}, at, f"{what}.{k}", item, depth + 1)
                elif not (sub is None or isinstance(sub, (bool, int, float, str))) \
                        or (isinstance(sub, str) and len(sub) > 500):
                    raise Problem(f"{what}.{k} holds simple values", at, item)
            if isinstance(v, dict):
                for kk, sub in v.items():
                    if "color" in str(kk) and isinstance(sub, str) and sub:
                        _color(sub, f"{at}.{kk}", f"{what}.{k}.{kk}", item)
        elif isinstance(v, float) and not math.isfinite(v):
            raise Problem(f"{what}.{k} is a finite number", at, item)
        elif not (v is None or isinstance(v, (bool, int, float))):
            raise Problem(f"{what}.{k} holds a simple value", at, item)


# ── images ────────────────────────────────────────────────────────────────────
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK = "http://www.w3.org/1999/xlink"
# the SVG elements that draw: anything else (script, foreignObject, a, animation, iframe…) is refused
_SVG_ELEMENTS = {
    "svg", "g", "defs", "symbol", "use", "title", "desc", "metadata", "style", "path", "rect", "circle", "ellipse",
    "line", "polyline", "polygon", "text", "tspan", "textPath", "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "pattern", "image", "marker", "filter", "feGaussianBlur", "feOffset", "feBlend",
    "feColorMatrix", "feComposite", "feFlood", "feMerge", "feMergeNode", "feMorphology", "feDropShadow",
    "feComponentTransfer", "feFuncR", "feFuncG", "feFuncB", "feFuncA", "switch",
}
# the namespaces of drawing programs' notes, which no renderer draws
_EDITOR_NS = ("http://www.inkscape.org/namespaces/inkscape", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd",
              "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "http://creativecommons.org/ns#",
              "http://purl.org/dc/elements/1.1/", "http://www.bohemiancoding.com/sketch/ns",
              "http://www.serif.com/")
_URL = re.compile(r"url\s*\(\s*(['\"]?)\s*([^)'\"]*)", re.I)


def _unsafe_text(text: str) -> Optional[str]:
    """What makes a style or an attribute's value reach outside the image."""
    low = re.sub(r"\s+", "", text).lower()
    if "javascript:" in low or "@import" in low or "expression(" in low:
        return "a script or an import"
    for m in _URL.finditer(text):
        if not m.group(2).strip().startswith("#"):
            return "a url() outside the image"
    return None


def svg_problem(data: bytes) -> Optional[str]:
    """Why an SVG is not safe to keep, or None. Refused, never cleaned."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "it is not UTF-8 text"
    head = text.lower()
    if "<!doctype" in head or "<!entity" in head:
        return "it declares a DOCTYPE or entities"
    if "<?xml-stylesheet" in head:
        return "it links a stylesheet"
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return "it is not well-formed XML"
    if root.tag not in (f"{{{_SVG_NS}}}svg", "svg"):
        return "it is not an SVG"
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        ns, local = (tag[1:].split("}", 1) if tag.startswith("{") else ("", tag))
        if ns in _EDITOR_NS:
            continue
        if ns not in ("", _SVG_NS) or local not in _SVG_ELEMENTS:
            return f"it holds a <{local}> element"
        if local == "style" and (why := _unsafe_text(el.text or "")):
            return f"its style holds {why}"
        for name, value in el.attrib.items():
            attr = name.split("}", 1)[-1].lower()
            if attr.startswith("on"):
                return f"it has an event attribute ({attr})"
            if attr == "href":
                v = value.strip()
                if not (v.startswith("#") or (local == "image" and re.match(r"^data:image/(png|jpeg);base64,", v))):
                    return "it refers to something outside the image"
            if why := _unsafe_text(value):
                return f"an attribute holds {why}"
    return None


def check_image(value: Any, where: str, item: Optional[str]) -> None:
    def bad(message: str) -> Problem:
        return Problem(f"The image «{item}»: {message}", where, item)

    if not isinstance(value, str) or not value:
        raise bad("upload a PNG, a JPEG or an SVG")
    m = DATA_URL.match(value)
    if not m:
        raise bad("an image is a PNG, a JPEG or an SVG uploaded into the report (no links)")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=True)
    except (binascii.Error, ValueError):
        raise bad("it is not valid base64") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise bad(f"it is {len(data) // 1024} KiB: at most {MAX_IMAGE_BYTES // 1024} KiB")
    kind = m.group(1)
    if kind == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise bad("it is not a PNG")
    if kind == "jpeg" and not data.startswith(b"\xff\xd8\xff"):
        raise bad("it is not a JPEG")
    if kind == "svg+xml" and (why := svg_problem(data)):
        raise bad(f"this SVG cannot be used: {why}. Export it again as a plain SVG, or as a PNG")


# ── the definition ────────────────────────────────────────────────────────────
def page_box(page: dict) -> tuple[float, float]:
    """The width and height between the margins, in mm."""
    size = page.get("size", "A4")
    w, h = (size["w"], size["h"]) if isinstance(size, dict) else PAGE_SIZES[size]
    if page.get("orientation") == "landscape":
        w, h = h, w
    top, right, bottom, left = page.get("margins") or [15, 12, 15, 12]
    return w - left - right, h - top - bottom


def _page(page: Any) -> tuple[float, float]:
    page = _keys(page, "page", ("size", "orientation", "margins"), what="The page")
    size = page.get("size", "A4")
    if isinstance(size, dict):
        _keys(size, "page.size", ("w", "h"), ("w", "h"), what="The page's size")
        _mm(size["w"], "page.size.w", "The page's width", 50, 2000)
        _mm(size["h"], "page.size.h", "The page's height", 50, 2000)
    elif size not in PAGE_SIZES:
        raise Problem(f"The page's size is one of {', '.join(PAGE_SIZES)}, or {{w, h}} in mm", "page.size")
    _choice(page.get("orientation"), ("portrait", "landscape"), "page.orientation", "The orientation")
    margins = page.get("margins")
    if margins is not None:
        if not isinstance(margins, list) or len(margins) != 4:
            raise Problem("The margins are four lengths: top, right, bottom, left", "page.margins")
        for i, m in enumerate(margins):
            _mm(m, f"page.margins[{i}]", "A margin", 0, 100)
    w, h = page_box(page)
    if w < 20 or h < 20:
        raise Problem("The margins leave less than 2 cm of page", "page.margins")
    return w, h


def _parameter(p: Any, i: int, names: set[str]) -> dict:
    where = f"parameters[{i}]"
    p = _keys(p, where, ("name", "label", "type", "multi", "required", "default", "choices"), ("name", "type"),
              what="A parameter")
    name = p["name"]
    if not isinstance(name, str) or not report_params.NAME.match(name):
        raise Problem("A parameter's name has letters, digits and _, and starts with a letter", f"{where}.name")
    if name in report_params.RESERVED:
        raise Problem(f"«{name}» is a name of Tabularia's placeholders: give the parameter another", f"{where}.name")
    if name in names:
        raise Problem(f"Two parameters are called «{name}»", f"{where}.name")
    what = f"The parameter «{name}»"
    _text(p.get("label"), f"{where}.label", f"{what}'s label", 200)
    _choice(p["type"], report_params.TYPES, f"{where}.type", f"{what}'s type", required=True)
    _bool(p.get("multi"), f"{where}.multi", f"{what}'s multi")
    _bool(p.get("required"), f"{where}.required", f"{what}'s required")
    multi = bool(p.get("multi"))

    def value(v: Any, at: str) -> Any:
        if report_params.placeholder_of(v) is not None:
            return None  # resolved at every render, in the report's zone
        if isinstance(v, (dict, list)):
            raise Problem(f"{what}: a value is a {p['type']}", at)
        try:
            return report_params.convert(p, v if isinstance(v, str) else json.dumps(v) if not isinstance(v, bool)
                                         else str(v).lower())
        except HTTPException as e:
            raise Problem(e.detail["message"], at) from None

    default = p.get("default")
    if default is not None:
        if multi != isinstance(default, list):
            raise Problem(f"{what}'s default is {'a list of values' if multi else 'one value'}", f"{where}.default")
        defaults = [value(v, f"{where}.default") for v in (default if multi else [default])]
    else:
        defaults = []
    choices = p.get("choices")
    if choices is not None:
        at = f"{where}.choices"
        if not isinstance(choices, dict):
            raise Problem(f"{what}'s choices are {{values}} or {{dataset, value, label}}", at)
        if "values" in choices:
            _keys(choices, at, ("values",), what=f"{what}'s choices")
            fixed = choices["values"]
            if not isinstance(fixed, list) or not 1 <= len(fixed) <= 1000:
                raise Problem(f"{what}'s choices are a list of 1 to 1000 values", at)
            allowed = [value(v, f"{at}.values[{k}]") for k, v in enumerate(fixed)]
            for d in defaults:
                if d is not None and d not in allowed:
                    raise Problem(f"{what}'s default is not one of its choices", f"{where}.default")
        else:
            _keys(choices, at, ("dataset", "value", "label"), ("dataset", "value"), what=f"{what}'s choices")
            _expr(choices["value"], f"{at}.value", f"{what}'s choice value", required=True)
            _expr(choices.get("label"), f"{at}.label", f"{what}'s choice label")
    names.add(name)
    return p


def _filters(filters: Any, where: str, what: str, parameters: dict) -> None:
    if filters is None:
        return
    if not isinstance(filters, list) or len(filters) > 20:
        raise Problem(f"{what}'s filters are a list of at most 20", where)
    for k, f in enumerate(filters):
        at = f"{where}[{k}]"
        f = _keys(f, at, ("column", "op", "value"), ("column", "op"), what=f"{what}'s filter")
        _text(f["column"], f"{at}.column", f"{what}'s filter column", 300, required=True)
        _choice(f["op"], FILTER_OPS, f"{at}.op", f"{what}'s filter operator", required=True)
        v = f.get("value")
        if isinstance(v, str) and (m := re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", v.strip())) \
                and m.group(1) not in report_params.BUILTINS:
            p = parameters.get(m.group(1))
            if p is None:
                raise Problem(f"{what}'s filter: the report has no parameter «{m.group(1)}»", f"{at}.value")
            if bool(p.get("multi")) != (f["op"] == "in"):
                raise Problem(f"{what}'s filter: «{m.group(1)}» "
                              + ("holds a list: use the operator in" if p.get("multi") else "is one value: in takes a list"),
                              f"{at}.op")
        elif f["op"] == "in" and not (isinstance(v, list) and 1 <= len(v) <= 1000):
            raise Problem(f"{what}'s filter: in takes a list of values", f"{at}.value")
        elif isinstance(v, (dict, list)) and f["op"] != "in":
            raise Problem(f"{what}'s filter takes one value", f"{at}.value")
        if isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v):
            raise Problem(f"{what}'s filter takes simple values", f"{at}.value")


def _dataset(ds: Any, i: int, names: set[str], parameters: dict) -> dict:
    where = f"datasets[{i}]"
    ds = _keys(ds, where, ("name", "source", "fields", "calculated"), ("name", "source"), what="A dataset")
    name = ds["name"]
    if not isinstance(name, str) or not DATASET_NAME.match(name):
        raise Problem("A dataset's name has letters, digits and _, and starts with a letter", f"{where}.name")
    if name in names:
        raise Problem(f"Two datasets are called «{name}»", f"{where}.name")
    what = f"The dataset «{name}»"
    src = ds["source"]
    if not isinstance(src, dict):
        raise Problem(f"{what}'s source is an object", f"{where}.source")
    at = f"{where}.source"
    kind = src.get("kind")
    ident = lambda key: isinstance(src.get(key), int) and not isinstance(src.get(key), bool)  # noqa: E731
    if kind == "sql":
        _keys(src, at, ("kind", "sql", "connection_id", "engine"), ("sql",), what=f"{what}'s source")
        _text(src["sql"], f"{at}.sql", f"{what}'s SQL", MAX_SQL_CHARS, required=True)
        if src.get("connection_id") is not None and not ident("connection_id"):
            raise Problem(f"{what}'s connection is a number", f"{at}.connection_id")
        if src.get("engine") is not None and src.get("engine") not in KNOWN_ENGINES:
            raise Problem(f"{what}: unknown engine {str(src.get('engine'))[:40]}", f"{at}.engine")
        if src.get("connection_id") is not None and src.get("engine") is not None:
            raise Problem(f"{what} runs on its connection: it takes no engine", f"{at}.engine")
    elif kind in ("datasource", "view"):
        key = "datasource_id" if kind == "datasource" else "view_id"
        _keys(src, at, ("kind", key, "filters"), (key,), what=f"{what}'s source")
        if not ident(key):
            raise Problem(f"{what}'s {kind} is a number", f"{at}.{key}")
        _filters(src.get("filters"), f"{at}.filters", what, parameters)
    elif kind == "sql_script":
        _keys(src, at, ("kind", "script_id"), ("script_id",), what=f"{what}'s source")
        if not ident("script_id"):
            raise Problem(f"{what}'s SQL script is a number", f"{at}.script_id")
    else:
        raise Problem(f"{what}'s source is sql, datasource, view or sql_script", f"{at}.kind")
    fields = ds.get("fields")
    if fields is not None:
        if not isinstance(fields, list) or len(fields) > 1000:
            raise Problem(f"{what}'s fields are a list of at most 1000", f"{where}.fields")
        for k, f in enumerate(fields):
            f = _keys(f, f"{where}.fields[{k}]", ("name", "type"), ("name",), what=f"{what}'s field")
            _text(f["name"], f"{where}.fields[{k}].name", f"{what}'s field name", 300, required=True)
            _choice(f.get("type"), FIELD_TYPES, f"{where}.fields[{k}].type", f"{what}'s field type")
    calculated = ds.get("calculated")
    if calculated is not None:
        if not isinstance(calculated, list) or len(calculated) > 100:
            raise Problem(f"{what}'s calculated fields are a list of at most 100", f"{where}.calculated")
        seen: set[str] = set()
        for k, c in enumerate(calculated):
            at = f"{where}.calculated[{k}]"
            c = _keys(c, at, ("name", "expr"), ("name", "expr"), what=f"{what}'s calculated field")
            _text(c["name"], f"{at}.name", f"{what}'s calculated field name", 300, required=True)
            if c["name"] in seen:
                raise Problem(f"{what} has two calculated fields called «{c['name']}»", f"{at}.name")
            seen.add(c["name"])
            _expr(c["expr"], f"{at}.expr", f"{what}'s calculated field «{c['name']}»", required=True)
    names.add(name)
    return ds


def _items(items: Any, where: str, section: str, box: tuple[float, float], datasets: set[str],
           ids: set[str]) -> list[dict]:
    """The items of a section, each within it: `box` is (width, height); a height of None
    is the body's, which grows over pages (an item fits one page)."""
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise Problem(f"The {section}'s items are a list of at most {MAX_ITEMS}", where)
    width, height = box
    out = []
    for i, it in enumerate(items):
        at = f"{where}[{i}]"
        if not isinstance(it, dict):
            raise Problem(f"An item of the {section} is an object", at)
        item_id, kind = it.get("id"), it.get("type")
        if not isinstance(item_id, str) or not ITEM_ID.match(item_id):
            raise Problem(f"An item of the {section} needs an id (letters, digits, _ and -)", f"{at}.id")
        if item_id in ids:
            raise Problem(f"Two items have the id «{item_id}»", f"{at}.id", item_id)
        ids.add(item_id)
        if kind not in (ITEM_TYPES if section == "body" else PAGE_ITEM_TYPES):
            allowed = ITEM_TYPES if section == "body" else PAGE_ITEM_TYPES
            raise Problem(f"The {section} holds only {', '.join(allowed)}: not «{str(kind)[:20]}»", f"{at}.type", item_id)
        _item(it, at, kind, item_id, width, height, datasets, section)
        out.append(it)
    return out


_BASE = ("id", "type", "x", "y", "w", "h", "style", "visible")
_TYPE_KEYS = {
    "text": ("value", "dataset"),
    "image": ("asset", "fit"),
    "line": (),
    "rect": ("border", "radius", "fill"),
    "table": ("dataset", "title", "columns", "groups", "detail", "sort", "filter", "repeat_header", "grand_total",
              "zebra", "matrix", "interactive"),
    "chart": ("dataset", "title", "kind", "category", "series", "split_by", "options"),
    "kpi": ("dataset", "label", "value", "compare", "format", "sparkline"),
}


def _item(it: dict, at: str, kind: str, item_id: str, width: float, height: Optional[float], datasets: set[str],
          section: str) -> None:
    _keys(it, at, _BASE + _TYPE_KEYS[kind], ("x", "y", "w", "h"), what=f"The item «{item_id}»", item=item_id)
    what = f"The item «{item_id}»"
    page_h = height if height is not None else 5000
    x = _mm(it["x"], f"{at}.x", f"{what}'s x", 0, width, item_id)
    y = _mm(it["y"], f"{at}.y", f"{what}'s y", 0, page_h, item_id)
    if kind == "line":  # from (x, y) to (x + w, y + h): either way, on the page
        w = _mm(it["w"], f"{at}.w", f"{what}'s w", -width, width, item_id)
        h = _mm(it["h"], f"{at}.h", f"{what}'s h", -page_h, page_h, item_id)
        if not (0 <= x + w <= width + 1e-6 and (height is None and y + h >= 0 or 0 <= y + h <= page_h + 1e-6)):
            raise Problem(f"{what} ends outside the {section}", at, item_id)
    else:
        w = _mm(it["w"], f"{at}.w", f"{what}'s width", 0.1, width, item_id)
        h = _mm(it["h"], f"{at}.h", f"{what}'s height", 0.1, page_h, item_id)
        if x + w > width + 1e-6:
            raise Problem(f"{what} goes past the right margin by {x + w - width:.1f} mm", at, item_id)
        if height is not None and y + h > height + 1e-6:
            raise Problem(f"{what} goes past the {section}'s height by {y + h - height:.1f} mm", at, item_id)
    _open(it.get("style"), f"{at}.style", f"{what}'s style", item_id)
    _expr(it.get("visible"), f"{at}.visible", f"{what}'s visible", item_id)

    def dataset() -> None:
        name = it.get("dataset")
        if name not in datasets:
            raise Problem(f"{what} reads the dataset «{str(name)[:64]}», which the report does not have",
                          f"{at}.dataset", item_id)

    if kind == "text":  # a text may read a dataset's fields, in the body
        _text(it.get("value"), f"{at}.value", f"{what}'s text", MAX_EXPR, item_id)
        if it.get("dataset") is not None:
            if section != "body":
                raise Problem(f"{what}: the {section} reads no dataset", f"{at}.dataset", item_id)
            dataset()
    elif kind == "image":
        check_image(it.get("asset"), f"{at}.asset", item_id)
        _choice(it.get("fit"), ("contain", "cover", "stretch"), f"{at}.fit", f"{what}'s fit", item_id)
    elif kind == "rect":
        _open({k: it[k] for k in ("border", "radius", "fill") if k in it}, at, what, item_id)
    elif kind == "table":
        dataset()
        _table(it, at, what, item_id, width)
    elif kind == "chart":
        dataset()
        _text(it.get("title"), f"{at}.title", f"{what}'s title", 200, item_id)
        _choice(it.get("kind"), CHART_KINDS, f"{at}.kind", f"{what}'s kind", item_id, required=True)
        _expr(it.get("category"), f"{at}.category", f"{what}'s category", item_id)
        _expr(it.get("split_by"), f"{at}.split_by", f"{what}'s split", item_id)
        series = it.get("series")
        if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
            raise Problem(f"{what} needs 1 to {MAX_SERIES} series", f"{at}.series", item_id)
        for k, s in enumerate(series):
            sat = f"{at}.series[{k}]"
            s = _keys(s, sat, ("value", "name", "as", "axis"), ("value",), what=f"{what}'s series", item=item_id)
            _expr(s["value"], f"{sat}.value", f"{what}'s series value", item_id, required=True)
            _text(s.get("name"), f"{sat}.name", f"{what}'s series name", 200, item_id)
            _choice(s.get("as"), ("bar", "line"), f"{sat}.as", f"{what}'s series as", item_id)
            _choice(s.get("axis"), ("left", "right"), f"{sat}.axis", f"{what}'s series axis", item_id)
        _open(it.get("options"), f"{at}.options", f"{what}'s options", item_id)
    elif kind == "kpi":
        dataset()
        _text(it.get("label"), f"{at}.label", f"{what}'s label", MAX_EXPR, item_id)
        _expr(it.get("value"), f"{at}.value", f"{what}'s value", item_id, required=True)
        _expr(it.get("compare"), f"{at}.compare", f"{what}'s comparison", item_id)
        _text(it.get("format"), f"{at}.format", f"{what}'s format", 64, item_id)
        spark = it.get("sparkline")
        if spark is not None:
            spark = _keys(spark, f"{at}.sparkline", ("category", "value"), ("category", "value"),
                          what=f"{what}'s sparkline", item=item_id)
            _expr(spark["category"], f"{at}.sparkline.category", f"{what}'s sparkline category", item_id, required=True)
            _expr(spark["value"], f"{at}.sparkline.value", f"{what}'s sparkline value", item_id, required=True)


def _table(it: dict, at: str, what: str, item_id: str, width: float) -> None:
    _text(it.get("title"), f"{at}.title", f"{what}'s title", 100, item_id)
    columns = it.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise Problem(f"{what} needs 1 to {MAX_COLUMNS} columns", f"{at}.columns", item_id)
    total = 0.0
    for k, c in enumerate(columns):
        cat = f"{at}.columns[{k}]"
        c = _keys(c, cat, ("header", "value", "width", "align", "format", "total", "style", "rules"), ("value",),
                  what=f"{what}'s column {k + 1}", item=item_id)
        _text(c.get("header"), f"{cat}.header", f"{what}'s column header", 500, item_id)
        _expr(c["value"], f"{cat}.value", f"{what}'s column value", item_id, required=True)
        if c.get("width") is not None:
            total += _mm(c["width"], f"{cat}.width", f"{what}'s column width", 1, width, item_id)
        _choice(c.get("align"), ("left", "center", "right"), f"{cat}.align", f"{what}'s column alignment", item_id)
        _text(c.get("format"), f"{cat}.format", f"{what}'s column format", 64, item_id)
        _expr(c.get("total"), f"{cat}.total", f"{what}'s column total", item_id)
        _open(c.get("style"), f"{cat}.style", f"{what}'s column style", item_id)
        rules = c.get("rules")
        if rules is not None:
            if not isinstance(rules, list) or len(rules) > 20:
                raise Problem(f"{what}'s column rules are a list of at most 20", f"{cat}.rules", item_id)
            for r, rule in enumerate(rules):
                rat = f"{cat}.rules[{r}]"
                rule = _keys(rule, rat, ("when", "style"), ("when",), what=f"{what}'s rule", item=item_id)
                _expr(rule["when"], f"{rat}.when", f"{what}'s rule", item_id, required=True)
                _open(rule.get("style"), f"{rat}.style", f"{what}'s rule style", item_id)
    if total > width + 1e-6:
        raise Problem(f"{what}'s columns are {total:.1f} mm wide: the page has {width:.1f} mm", f"{at}.columns", item_id)
    groups = it.get("groups")
    if groups is not None:
        if not isinstance(groups, list) or len(groups) > MAX_GROUPS:
            raise Problem(f"{what}'s groups are a list of at most {MAX_GROUPS}", f"{at}.groups", item_id)
        for k, g in enumerate(groups):
            gat = f"{at}.groups[{k}]"
            g = _keys(g, gat, ("by", "label", "header", "footer", "page_break", "sort", "keep_together"), ("by",),
                      what=f"{what}'s group", item=item_id)
            _expr(g["by"], f"{gat}.by", f"{what}'s group", item_id, required=True)
            _expr(g.get("label"), f"{gat}.label", f"{what}'s group label", item_id)
            for flag in ("header", "footer", "keep_together"):
                _bool(g.get(flag), f"{gat}.{flag}", f"{what}'s group {flag}", item_id)
            _choice(g.get("page_break"), ("none", "between", "after"), f"{gat}.page_break", f"{what}'s page break", item_id)
            _choice(g.get("sort"), ("asc", "desc"), f"{gat}.sort", f"{what}'s group sort", item_id)
    sort = it.get("sort")
    if sort is not None:
        if not isinstance(sort, list) or len(sort) > 10:
            raise Problem(f"{what}'s sort is a list of at most 10", f"{at}.sort", item_id)
        for k, s in enumerate(sort):
            s = _keys(s, f"{at}.sort[{k}]", ("by", "dir"), ("by",), what=f"{what}'s sort", item=item_id)
            _expr(s["by"], f"{at}.sort[{k}].by", f"{what}'s sort", item_id, required=True)
            _choice(s.get("dir"), ("asc", "desc"), f"{at}.sort[{k}].dir", f"{what}'s sort direction", item_id)
    _expr(it.get("filter"), f"{at}.filter", f"{what}'s filter", item_id)
    for flag in ("detail", "repeat_header", "grand_total", "zebra"):
        _bool(it.get(flag), f"{at}.{flag}", f"{what}'s {flag}", item_id)
    matrix = it.get("matrix")
    if matrix is not None:
        matrix = _keys(matrix, f"{at}.matrix", ("columns_by", "value", "total", "format", "width"),
                       ("columns_by", "value"), what=f"{what}'s matrix", item=item_id)
        by = matrix["columns_by"]
        for k, one in enumerate(by if isinstance(by, list) and 1 <= len(by) <= 3 else [by]):
            _expr(one, f"{at}.matrix.columns_by" + (f"[{k}]" if isinstance(by, list) else ""),
                  f"{what}'s matrix columns", item_id, required=True)
        _expr(matrix["value"], f"{at}.matrix.value", f"{what}'s matrix value", item_id, required=True)
        _bool(matrix.get("total"), f"{at}.matrix.total", f"{what}'s matrix total", item_id)
        _text(matrix.get("format"), f"{at}.matrix.format", f"{what}'s matrix format", 64, item_id)
        if matrix.get("width") is not None:
            _mm(matrix["width"], f"{at}.matrix.width", f"{what}'s matrix column width", 1, width, item_id)
    interactive = it.get("interactive")
    if interactive is not None:
        interactive = _keys(interactive, f"{at}.interactive", ("sort", "toggle"), what=f"{what}'s interactivity",
                            item=item_id)
        _bool(interactive.get("sort"), f"{at}.interactive.sort", f"{what}'s interactive sort", item_id)
        _bool(interactive.get("toggle"), f"{at}.interactive.toggle", f"{what}'s interactive toggle", item_id)


def _alone_in_its_row(items: list[dict]) -> None:
    """A table grows with its data and takes its row of the body alone: no other item
    overlaps its vertical range."""
    for t in items:
        if t.get("type") != "table":
            continue
        top, bottom = t["y"], t["y"] + t["h"]
        for other in items:
            if other is t:
                continue
            o_top = min(other["y"], other["y"] + other["h"])
            o_bottom = max(other["y"], other["y"] + other["h"])
            if o_top < bottom - 1e-6 and o_bottom > top + 1e-6:
                raise Problem(f"The table «{t['id']}» takes its row alone: move «{other['id']}» above or below it",
                              "sections.body.items", other["id"])


def check(definition: Any) -> dict:
    """The definition as it will be saved, or Problem saying what is wrong and where."""
    if not isinstance(definition, dict):
        raise Problem("The report's definition is an object")
    if len(json.dumps(definition)) > MAX_DEFINITION_BYTES:
        raise Problem(f"The report is larger than {MAX_DEFINITION_BYTES // (1024 * 1024)} MiB: use smaller images")
    _keys(definition, "", ("v", "page", "locale", "timezone", "theme", "parameters", "datasets", "sections"), ("v",),
          what="The report")
    if definition.get("v") != 1:
        raise Problem("The report's definition is version 1 (v: 1)", "v")
    width, height = _page(definition.get("page") or {})
    _choice(definition.get("locale"), LOCALES, "locale", "The language")
    tz = definition.get("timezone")
    if tz is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            if not isinstance(tz, str) or not tz:
                raise ValueError
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise Problem(f"Unknown time zone «{str(tz)[:64]}»: write one as Europe/Rome", "timezone") from None
    theme = definition.get("theme")
    if theme is not None:
        theme = _keys(theme, "theme", ("font", "size", "color", "accent"), what="The theme")
        _text(theme.get("font"), "theme.font", "The theme's font", 64)
        if theme.get("size") is not None:
            _number(theme["size"], "theme.size", "The theme's size", 5, 24)
        _color(theme.get("color"), "theme.color", "The theme's colour")
        _color(theme.get("accent"), "theme.accent", "The theme's accent")

    parameters = definition.get("parameters") or []
    if not isinstance(parameters, list) or len(parameters) > MAX_PARAMETERS:
        raise Problem(f"The parameters are a list of at most {MAX_PARAMETERS}", "parameters")
    names: set[str] = set()
    by_name = {p["name"]: p for p in (_parameter(p, i, names) for i, p in enumerate(parameters))}
    datasets = definition.get("datasets") or []
    if not isinstance(datasets, list) or len(datasets) > MAX_DATASETS:
        raise Problem(f"The datasets are a list of at most {MAX_DATASETS}", "datasets")
    ds_names: set[str] = set()
    for i, ds in enumerate(datasets):
        _dataset(ds, i, ds_names, by_name)
    for i, p in enumerate(parameters):
        source = (p.get("choices") or {}).get("dataset")
        if source is not None and source not in ds_names:
            raise Problem(f"The parameter «{p['name']}» takes its choices from «{str(source)[:64]}», "
                          "which the report does not have", f"parameters[{i}].choices.dataset")

    sections = _keys(definition.get("sections") or {}, "sections", ("page_header", "body", "page_footer"),
                     what="The sections")
    header = _keys(sections.get("page_header") or {}, "sections.page_header", ("height", "on_first_page", "items"),
                   what="The page header")
    footer = _keys(sections.get("page_footer") or {}, "sections.page_footer", ("height", "on_last_page", "items"),
                   what="The page footer")
    body = _keys(sections.get("body") or {}, "sections.body", ("items",), what="The body")
    hh = _mm(header.get("height", 0), "sections.page_header.height", "The page header's height", 0, height)
    fh = _mm(footer.get("height", 0), "sections.page_footer.height", "The page footer's height", 0, height)
    if hh + fh > height - 20:
        raise Problem("The page header and footer leave less than 2 cm for the body", "sections")
    _bool(header.get("on_first_page"), "sections.page_header.on_first_page", "The page header's first page")
    _bool(footer.get("on_last_page"), "sections.page_footer.on_last_page", "The page footer's last page")
    ids: set[str] = set()
    _items(header.get("items"), "sections.page_header.items", "page header", (width, hh), ds_names, ids)
    _items(footer.get("items"), "sections.page_footer.items", "page footer", (width, fh), ds_names, ids)
    body_items = _items(body.get("items"), "sections.body.items", "body", (width, None), ds_names, ids)
    for it in body_items:
        if it.get("type") != "line" and it["h"] > height - hh - fh + 1e-6:
            raise Problem(f"The item «{it['id']}» is taller than the body of a page ({height - hh - fh:.1f} mm)",
                          "sections.body.items", it["id"])
    _alone_in_its_row(body_items)
    return definition


def check_dataset(definition: Any, name: str) -> dict:
    """The parameters and one dataset of a definition being edited, which is what reading
    that dataset's fields needs (the rest may be half done): a definition with those only."""
    if not isinstance(definition, dict):
        raise Problem("The report's definition is an object")
    parameters = definition.get("parameters") or []
    if not isinstance(parameters, list) or len(parameters) > MAX_PARAMETERS:
        raise Problem(f"The parameters are a list of at most {MAX_PARAMETERS}", "parameters")
    names: set[str] = set()
    by_name = {p["name"]: p for p in (_parameter(p, i, names) for i, p in enumerate(parameters))}
    datasets = definition.get("datasets") or []
    for i, ds in enumerate(datasets if isinstance(datasets, list) else []):
        if isinstance(ds, dict) and ds.get("name") == name:
            _dataset(ds, i, set(), by_name)
            tz = definition.get("timezone")
            return {"v": 1, "timezone": tz if isinstance(tz, str) else "UTC", "parameters": parameters, "datasets": [ds]}
    raise Problem(f"The report has no dataset «{name[:64]}»", "datasets")


def without_images(definition: dict) -> dict:
    """The definition with its images' data left out: what the engine's check reads."""
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: ("" if k == "asset" and isinstance(v, str) else strip(v)) for k, v in node.items()}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(definition)


def empty(locale: str = "en", timezone: str = "UTC") -> dict:
    """A new report's definition: an A4 page, no data yet."""
    return {
        "v": 1,
        "page": {"size": "A4", "orientation": "portrait", "margins": [15, 12, 15, 12]},
        "locale": locale if locale in LOCALES else "en",
        "timezone": timezone or "UTC",
        "theme": {"font": "IBM Plex Sans", "size": 9, "color": "#1f2933", "accent": "#3b5bdb"},
        "parameters": [],
        "datasets": [],
        "sections": {"page_header": {"height": 18, "on_first_page": True, "items": []}, "body": {"items": []},
                     "page_footer": {"height": 10, "on_last_page": True, "items": []}},
    }
