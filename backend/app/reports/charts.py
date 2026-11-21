"""Charts of a report: ECharts options built from the data, drawn to SVG by the chart service.

One preset per kind (docs/specs/reports.md, «Report items»), with a fixed layout: ECharts'
own defaults cut a treemap and move a legend over the plot. The options are plain JSON,
built here, never from text a person wrote: a category called `fmt:x` or `<script>` is
a value inside `data`. Numbers are written by the server in the report's language and
travel with each point (`t`); where ECharts computes the values itself (an axis' ticks)
it formats them through a formatter named from the service's closed list:

    {"$fmt": "label"}                      the point's text (`t`), else its value
    {"$fmt": "short", "max": n}            a category's name, cut to n characters with «…»
    {"$fmt": "nameLabel", "sep": " "}      the category and the point's text (on two lines without sep)
    {"$fmt": "namePercent", "locale": l}   the category and, on a line of its own, its share (a pie)
    {"$fmt": "compact", "locale": l}       1,23 Mio, 3.4K, 0.05… (Intl, compact notation)
    {"$fmt": "percent", "locale": l, "decimals": n}
    {"$fmt": "const", "text": "…"}         a text decided here (a gauge's value)

The service (frontend/chart-renderer/render.mjs) turns only those into functions, only
under a `formatter` key, and measures text with the advance widths of the PDF's font.
"""
from __future__ import annotations

import concurrent.futures
import math
from dataclasses import dataclass, field, replace
from typing import Any

from app.reports import definition as rd
from app.reports import formats
from app.reports.errors import ReportError

MAX_CATEGORIES = 2000
MAX_SERIES = 24
MAX_LABEL_CHARS = 40  # a name on an axis or a slice: longer ones end in «…», so it takes three lines at most
MM_PX = 96 / 25.4
# the y axis of an `xy` chart: a tick label of 9 px every Y_TICK_PX at least, under the x axis' labels and their gap
Y_TICK_PX = 18
XY_X_LABELS_PX = 20
# the PDF's font first (Typst draws the chart with it); then what a browser has, for the
# screen's pages, where a chart inside an <img> cannot use the page's fonts (Plex alone fell
# back to a serif)
FONT = "IBM Plex Sans, Helvetica Neue, Arial, sans-serif"
SERVICE_TIMEOUT_SECONDS = 30
_EXTRA = ["#14a38b", "#e0a23b", "#c2477a", "#6b7590", "#8a63d2", "#2f9e44", "#d9480f", "#1c7ed6", "#868e96"]
GOOD, BAD, MID = "#2b8a3e", "#c92a2a", "#e0a23b"
LIGHT = "#f1f3ff"  # the inside of a box, the low end of a heatmap


@dataclass
class SeriesData:
    name: str
    values: list[float | None]
    labels: list[str]
    as_: str = "bar"
    axis: str = "left"
    fmt: str | None = None  # the series' format, or the chart's


@dataclass
class XYSeries:
    """A mark of an `xy` chart: its name in the legend (`legend`: it is there), how it is
    drawn (point, line, step, stem, band), its points — (x, y, label), a band's (x, low,
    high) — and its colour, an index of the palette."""
    name: str
    mark: str
    points: list[tuple[float, float, Any]]
    colour: int = 0
    legend: bool = True
    color: str | None = None  # a fixed colour (#rrggbb) instead of the palette's `colour`


@dataclass
class ChartData:
    kind: str
    fmt: str | None = None
    title: str = ""
    categories: list[str] = field(default_factory=list)
    series: list[SeriesData] = field(default_factory=list)
    # scatter, density: (series name, [(x, y, text)])
    points: list[tuple[str, list[tuple[float | None, float | None, str]]]] = field(default_factory=list)
    targets: list[float | None] = field(default_factory=list)
    total_label: str = "Total"
    # the raw values behind `categories` (without «Other») and the series of a `split_by`: only a
    # dashboard's click reads them, a value on a report's chart is what it prints
    keys: list[Any] = field(default_factory=list)
    split_keys: list[Any] = field(default_factory=list)
    xy: list[XYSeries] = field(default_factory=list)  # an `xy` chart's marks
    targets_x: list[float | None] = field(default_factory=list)  # its vertical lines
    y_names: dict[int, str] = field(default_factory=dict)  # its y axis' names: at each whole number, a name


def _palette(theme: rd.Theme) -> list[str]:
    return [theme.accent, *_EXTRA]


def _value_formatter(fmt: str | None, loc: formats.Locale, digits: int | None = None) -> dict:
    if fmt and formats.is_number_pattern(fmt) and "%" in fmt:
        sec = formats.number_pattern(fmt)[0]
        return {"$fmt": "percent", "locale": loc.code, "decimals": sec.max_frac}
    # `digits` only when the default three would write one tick for the whole axis (see _tick_digits)
    return {"$fmt": "compact", "locale": loc.code, **({"digits": digits} if digits else {})}


def _tick_digits(series: list[SeriesData]) -> int | None:
    """The significant digits the ticks of an axis that does not start from zero need: values
    around 1e9 that differ by tens give ticks that are all «1B» at three digits. None when the
    default says it all (the spread is not tiny against the magnitude)."""
    vals = [v for s in series for v in s.values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    big, spread = max(abs(v) for v in vals), max(vals) - min(vals)
    if big == 0 or spread <= 0:
        return None
    need = math.ceil(math.log10(big / spread)) + 2  # the ticks go by a fraction of the spread
    return min(need, 15) if need > 3 else None


def _points(s: SeriesData, below: str | None = None) -> list[dict | None]:
    """The values with their texts; `below`: where the label of a value under zero goes (a
    bar's label sits at its end, which for a negative bar is the other side)."""
    return [None if v is None else {"value": v, "t": t, **({"label": {"position": below}} if below and v < 0 else {})}
            for v, t in zip(s.values, s.labels)]


LEGEND_NAME = 22  # the characters of a legend's name ECharts writes (it truncates the rest at 120 px)
LEGEND_SHARE = 0.4  # the most of a chart's height its legend takes: the plot keeps the rest


def _legend_rows(names: list[str], width: int, height: int = 0) -> int:
    """How many lines the legend takes: ECharts does not keep room for it in the grid. As many as
    its names need, up to four, or up to LEGEND_SHARE of the chart's height on a tall one (the plot
    keeps the rest)."""
    rows, used = 1, 0.0
    for name in names:
        w = 10 + 5 + min(len(name), LEGEND_NAME) * 9 * 0.56 + 8  # icon, gap, text (truncated at 120 px), item gap
        if used and used + w > width - 10:
            rows, used = rows + 1, 0.0
        used += w
    most = max(4, int(height * LEGEND_SHARE) // 16)
    return min(rows, most)


def _short_names(names: list[str]) -> list[str]:
    """Names a legend can tell apart: when some are longer than it writes and they share their first or last
    words («plant=Plant number 06 — north-east side», «plant=Plant number 07 — north-east side»), those
    words are left out, an ellipsis in their place («…number 06…»). The names as they are when they fit,
    when nothing is shared, or when the shortened ones would no longer differ."""
    if len(names) < 2 or all(len(n) <= LEGEND_NAME for n in names):
        return names
    words = [n.split(" ") for n in names]
    first = 0
    while all(len(w) > first + 1 for w in words) and len({w[first] for w in words}) == 1:
        first += 1
    last = 0
    while all(len(w) > first + last + 1 for w in words) and len({w[-1 - last] for w in words}) == 1:
        last += 1
    if not first and not last:
        return names
    short = [("…" if first else "") + " ".join(w[first:len(w) - last]) + ("…" if last else "") for w in words]
    return short if len(set(short)) == len(set(names)) else names


def _base(data: ChartData, theme: rd.Theme, legend: bool, width: int, height: int = 0) -> dict:
    top = 22 if data.title else 8
    names = [s.name for s in data.series] or [name for name, _ in data.points] or _xy_legend(data)
    lines = _legend_rows(names, width, height) if legend else 0
    option: dict[str, Any] = {
        "animation": False,
        "color": _palette(theme),
        "textStyle": {"fontFamily": FONT, "fontSize": 9, "color": theme.color},
        # a legend's line is 16 px, and the value axis' top label sticks out of the plot by half a line
        "grid": {"left": 6, "right": 10, "top": top + (16 * lines + 4 if lines else 0), "bottom": 6,
                 "containLabel": True},
        "legend": {"show": legend, "top": top - 2 if data.title else 2, "left": "center", "itemWidth": 10,
                   "itemHeight": 7, "itemGap": 8, "textStyle": {"fontSize": 9, "overflow": "truncate", "width": 120}},
    }
    if data.title:
        option["title"] = {"text": data.title, "left": 0, "top": 0, "textStyle": {"fontSize": 10, "fontWeight": "bold",
                                                                                   "color": theme.color}}
    return option


def _x_axis(categories: list[str], width_px: int) -> dict:
    """A horizontal category axis. When the categories have room (a few regions, the months
    of a year) every label is written, on more lines if it needs them (a label with a number
    in it is turned instead); when they crowd (the
    days of a year) ECharts writes those that do not overlap, whole — never «20…»."""
    band = (width_px - 60) / max(len(categories), 1)  # the plot is the chart less its value axis
    widest = max((len(c) for c in categories), default=0) * 9 * 0.58  # px, at 9 pt
    if band >= 14 and widest > band - 4 and any(ch.isdigit() for c in categories for ch in c):
        # a label with a number in it is never wrapped (the line would end inside the number,
        # «[4,48 – 5,⏎47)»): when it does not fit under its bar it is turned, every one written
        label = {"fontSize": 9, "interval": 0, "rotate": 40, "hideOverlap": True,
                 "formatter": {"$fmt": "short", "max": MAX_LABEL_CHARS}}
    elif band >= 36:
        label = {"fontSize": 9, "interval": 0, "overflow": "break", "width": int(band - 4),
                 "formatter": {"$fmt": "short", "max": MAX_LABEL_CHARS}}
    else:
        label = {"fontSize": 9, "hideOverlap": True, "overflow": "truncate", "width": 90}
    return {"type": "category", "data": categories, "axisLabel": label, "axisTick": {"alignWithLabel": True}}


def _y_axis(categories: list[str], width_px: int, height_px: int) -> dict:
    """A vertical category axis (bars, a bullet, a heatmap's rows): a label wraps in its
    third of the width while the rows have room for two lines, and is cut with «…» beyond."""
    band = (height_px - 40) / max(len(categories), 1)
    width = max(int(width_px * 0.3), 40)
    if band >= 22:
        label = {"fontSize": 9, "interval": 0, "overflow": "break", "width": width,
                 "formatter": {"$fmt": "short", "max": MAX_LABEL_CHARS}}
    else:
        label = {"fontSize": 9, "hideOverlap": True, "overflow": "truncate", "width": width}
    return {"type": "category", "data": categories, "axisLabel": label, "axisTick": {"alignWithLabel": True}}


def _value_axis(data: ChartData, loc: formats.Locale, o: rd.ChartOptions, side: str = "left",
                scaled: bool = False) -> dict:
    # an axis writes its numbers in the format of the first series on it (a margin in % beside amounts)
    fmt = next((s.fmt for s in data.series if s.axis == side and s.fmt), data.fmt)
    digits = _tick_digits(data.series) if scaled else None  # an axis that does not start from zero
    axis: dict[str, Any] = {"type": "value", "axisLabel": {"fontSize": 9, "formatter": _value_formatter(fmt, loc, digits)},
                            "splitLine": {"lineStyle": {"color": "#e9ecef"}}}
    if o.min is not None:
        axis["min"] = o.min
    if o.max is not None:
        axis["max"] = o.max
    return axis


def _label(o: rd.ChartOptions, position: str) -> dict:
    return {"show": o.labels, "position": position, "fontSize": 8, "formatter": {"$fmt": "label"}}


HIDE_OVERLAP = {"hideOverlap": True}  # the values written on a chart: those that would overlap are left out


def _has_values(data: ChartData) -> bool:
    """Whether the chart has something to draw (a pie, a funnel, a treemap draw sizes: above zero)."""
    if data.kind in ("scatter", "density"):
        return any(x is not None and y is not None for _, pts in data.points for x, y, _ in pts)
    if data.kind == "xy":
        return any(s.points for s in data.xy)
    if data.kind == "boxplot":  # a box is its five numbers, or nothing
        return any(all(s.values[i] is not None for s in data.series) for i in range(len(data.categories)))
    values = [v for s in data.series for v in s.values if v is not None]
    if data.kind in ("pie", "donut", "funnel", "treemap"):
        return any(v > 0 for v in values)
    return bool(values)


def _no_data(option: dict, loc: formats.Locale) -> dict:
    note = {"text": loc.texts["empty"], "left": "center", "top": "middle",
            "textStyle": {"fontSize": 10, "fontWeight": "normal", "color": "#6b7590"}}
    option["title"] = [option["title"], note] if "title" in option else note
    return option


def option(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale) -> dict:
    """The ECharts option of a chart item; one with nothing to draw says «No data»."""
    out = _option(data, it, theme, loc)
    return out if _has_values(data) else _no_data(out, loc)


def _option(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale) -> dict:
    o = it.options
    kind = data.kind
    many = len(data.series) > 1 or len(data.points) > 1
    if kind == "xy":
        many = len(_xy_legend(data)) > 1
    legend = o.legend if o.legend is not None else (many and kind not in ("pie", "donut", "funnel", "treemap", "heatmap",
                                                                          "gauge", "bullet", "boxplot", "interval"))
    width_px = max(int(it.w * MM_PX), 40)
    height_px = max(int(it.h * MM_PX), 20)
    if kind == "xy":  # names a legend can tell apart (the strata of a survival model, a common prefix)
        short = dict(zip(names := _xy_legend(data), _short_names(names)))
        if any(k != v for k, v in short.items()):
            data = replace(data, xy=[replace(x, name=short.get(x.name, x.name)) for x in data.xy])
    base = _base(data, theme, bool(legend), width_px, height_px)
    if kind in ("column", "line", "area", "combo"):
        right = any(s.axis == "right" for s in data.series)
        series = []
        for s in data.series:
            line = kind in ("line", "area") or (kind == "combo" and s.as_ == "line")
            entry: dict[str, Any] = {"type": "line" if line else "bar", "name": s.name,
                                     "data": _points(s, None if line or o.stacked else "bottom"),
                                     "label": _label(o, "top"), "labelLayout": HIDE_OVERLAP,
                                     "yAxisIndex": 1 if s.axis == "right" and right else 0}
            if line:
                entry.update(symbol="circle", symbolSize=4, lineStyle={"width": 1.6}, connectNulls=False)
                if kind == "area":
                    entry["areaStyle"] = {"opacity": 0.18}
            else:
                entry["barMaxWidth"] = 48
            if o.stacked:
                entry["stack"] = "total"
            series.append(entry)
        yaxis = [_value_axis(data, loc, o)] + ([{**_value_axis(data, loc, o, "right"), "splitLine": {"show": False}}]
                                               if right else [])
        return {**base, "xAxis": _x_axis(data.categories, width_px), "yAxis": yaxis, "series": series}
    if kind == "bar":
        series = [{"type": "bar", "name": s.name, "data": _points(s, None if o.stacked else "left"),
                   "label": _label(o, "right"), "labelLayout": HIDE_OVERLAP, "barMaxWidth": 28,
                   **({"stack": "total"} if o.stacked else {})} for s in data.series]
        if o.labels:  # room for the value written after the longest bar
            longest = max((len(t) for s in data.series for t in s.labels), default=0)
            base["grid"]["right"] = 10 + int(longest * 8 * 0.6)
        y = _y_axis(data.categories, width_px, height_px - base["grid"]["top"])
        return {**base, "yAxis": {**y, "inverse": True},
                "xAxis": _value_axis(data, loc, o), "series": series}
    if kind in ("pie", "donut"):
        s = data.series[0]
        points = [{"name": c, "value": v, "t": t} for c, v, t in zip(data.categories, s.values, s.labels)
                  if v is not None and v > 0]
        # a slice's share on a line of its own under its name, which wraps and is cut with «…» past
        # MAX_LABEL_CHARS: the share is never what is cut; slices too thin for a label go without
        return {**base, "series": [{
            "type": "pie", "radius": ["45%", "70%"] if kind == "donut" else [0, "70%"], "center": ["50%", "54%"],
            "data": points, "avoidLabelOverlap": True, "showEmptyCircle": False, "minShowLabelAngle": 3,
            "label": {"fontSize": 8, "formatter": {"$fmt": "namePercent", "locale": loc.code, "max": MAX_LABEL_CHARS},
                      "overflow": "break", "width": max(width_px // 4, 50)},
            "labelLine": {"length": 6, "length2": 6}}]}
    if kind == "waterfall":
        s = data.series[0]
        running, bases, bars = 0.0, [], []
        for v, t in zip(s.values, s.labels):
            v = v or 0.0
            if v >= 0:
                bases.append(running)
                bars.append({"value": v, "t": t, "itemStyle": {"color": GOOD}})
                running += v
            else:
                running += v
                bases.append(running)
                bars.append({"value": -v, "t": t, "itemStyle": {"color": BAD}, "label": {"position": "bottom"}})
        total = formats.display(running, data.fmt, loc)
        bases.append(0)
        bars.append({"value": running, "t": total, "itemStyle": {"color": theme.accent},
                     **({"label": {"position": "bottom"}} if running < 0 else {})})
        # the invisible bases and the steps stacked across zero: by sign (ECharts' default) a step
        # down from a total under zero was drawn from zero upwards
        return {**base, "xAxis": _x_axis([*data.categories, data.total_label], width_px),
                "yAxis": _value_axis(data, loc, o),
                "series": [{"type": "bar", "stack": "w", "stackStrategy": "all", "silent": True, "data": bases,
                            "itemStyle": {"color": "transparent"}, "label": {"show": False}},
                           {"type": "bar", "stack": "w", "stackStrategy": "all", "name": s.name, "data": bars,
                            "barMaxWidth": 48, "label": {**_label(o, "top"), "show": True},
                            "labelLayout": HIDE_OVERLAP}]}
    if kind == "funnel":
        s = data.series[0]
        # the stages that have a size, their names and values beside them: inside, a small stage cut them
        points = sorted(({"name": c, "value": v, "t": t} for c, v, t in zip(data.categories, s.values, s.labels)
                         if v is not None and v > 0), key=lambda p: -p["value"])
        return {**base, "series": [{"type": "funnel", "left": "2%", "width": "58%", "top": base["grid"]["top"],
                                    "bottom": 6, "sort": "descending", "gap": 2, "data": points,
                                    "label": {"position": "right", "fontSize": 8, "color": theme.color,
                                              "overflow": "break", "width": int(width_px * 0.36),
                                              "formatter": {"$fmt": "nameLabel", "sep": " ", "max": MAX_LABEL_CHARS}},
                                    "labelLine": {"length": 8}}]}
    if kind == "treemap":
        if it.split_by:
            nodes = []
            for ci, cat in enumerate(data.categories):
                children = [{"name": s.name, "value": s.values[ci], "t": s.labels[ci]} for s in data.series
                            if s.values[ci] is not None and s.values[ci] > 0]
                if children:
                    nodes.append({"name": cat, "children": children})
        else:
            s = data.series[0]
            nodes = [{"name": c, "value": v, "t": t} for c, v, t in zip(data.categories, s.values, s.labels)
                     if v is not None and v > 0]
        return {**base, "series": [{
            "type": "treemap", "roam": False, "nodeClick": False, "breadcrumb": {"show": False},
            "left": 0, "right": 0, "top": base["grid"]["top"] - 6, "bottom": 0, "data": nodes,
            "label": {"fontSize": 8, "overflow": "truncate",
                      "formatter": {"$fmt": "nameLabel", "max": MAX_LABEL_CHARS}},
            "upperLabel": {"show": bool(it.split_by), "height": 14, "fontSize": 8, "color": "#fff"},
            "levels": [{"itemStyle": {"borderWidth": 0, "gapWidth": 2}},
                       {"itemStyle": {"gapWidth": 1, "borderColorSaturation": 0.6}}]}]}
    if kind == "heatmap":
        cells, values = [], []
        for yi, s in enumerate(data.series):
            for xi, (v, t) in enumerate(zip(s.values, s.labels)):
                if v is not None:
                    cells.append({"value": [xi, yi, v], "t": t})
                    values.append(v)
        lo, hi = (min(values), max(values)) if values else (0, 1)
        if o.fixed_scale:  # a fixed scale, as a probability's from 0 to 1: the colours do not follow the data's own ends
            lo, hi = o.fixed_scale
        base["grid"]["bottom"] = 30
        # the scale's ends written by the server, in the chart's format
        ends = [formats.display(hi, data.fmt, loc), formats.display(lo, data.fmt, loc)] if values else ["", ""]
        return {**base, "xAxis": _x_axis(data.categories, width_px),
                "yAxis": _y_axis([s.name for s in data.series], width_px, height_px - base["grid"]["top"] - 30),
                "visualMap": {"min": lo, "max": hi if hi > lo else lo + 1, "calculable": False, "orient": "horizontal",
                              "left": "center", "bottom": 0, "itemWidth": 8, "itemHeight": 90, "text": ends,
                              "textGap": 4, "textStyle": {"fontSize": 8},
                              "inRange": {"color": [LIGHT, theme.accent]}},
                "series": [{"type": "heatmap", "data": cells, "label": _label(o, "inside"), "labelLayout": HIDE_OVERLAP,
                            "itemStyle": {"borderColor": "#fff", "borderWidth": 1}}]}
    if kind == "scatter":
        series = [{"type": "scatter", "name": name, "symbolSize": 5,
                   "data": [{"value": [x, y], "t": t} for x, y, t in pts if x is not None and y is not None],
                   "label": _label(o, "top"), "labelLayout": HIDE_OVERLAP} for name, pts in data.points]
        return {**base, "xAxis": {**_value_axis(data, loc, rd.ChartOptions()), "scale": True},
                "yAxis": {**_value_axis(data, loc, o), "scale": True}, "series": series}
    if kind == "boxplot":
        return _boxplot(data, it, theme, loc, base, width_px)
    if kind == "interval":
        return _interval(data, it, theme, loc, base, width_px)
    if kind == "density":
        return _density(data, it, theme, loc, base)
    if kind == "xy":
        return _xy(data, it, theme, loc, base)
    if kind == "gauge":
        return _gauge(data, it, theme, base)
    if kind == "bullet":
        return _bullet(data, it, theme, loc, base, height_px)
    raise ReportError(f"item «{it.id}»: unknown chart kind {kind}")  # pragma: no cover


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    import math

    magnitude = 10 ** math.floor(math.log10(v))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * magnitude >= v:
            return step * magnitude
    return 10 * magnitude  # pragma: no cover


def _band_colors(n: int) -> list[str]:
    return {1: [GOOD], 2: [BAD, GOOD], 3: [BAD, MID, GOOD]}.get(n, ["#adb5bd"] * n)


def _gauge(data: ChartData, it: rd.ChartItem, theme: rd.Theme, base: dict) -> dict:
    o = it.options
    s = data.series[0]
    value, label = s.values[0], s.labels[0]
    target = data.targets[0] if data.targets else None
    lo = o.min if o.min is not None else 0.0
    hi = o.max if o.max is not None else _nice_max(max(value or 0, target or 0, *(o.bands or [0])) * 1.1)
    if o.bands:
        stops = [[min(max((b - lo) / (hi - lo), 0), 1), c] for b, c in zip(sorted(o.bands), _band_colors(len(o.bands)))]
        if stops[-1][0] < 1:
            stops.append([1, "#dee2e6"])
        axis_line = {"lineStyle": {"width": 10, "color": stops}}
        progress = {"show": False}
        pointer = {"show": True, "length": "52%", "width": 4, "itemStyle": {"color": theme.color}}
        detail_at, title_at = "38%", "66%"  # below the pointer's hub
    else:
        axis_line = {"lineStyle": {"width": 10, "color": [[1, "#e9ecef"]]}}
        progress = {"show": True, "width": 10, "itemStyle": {"color": theme.accent}}
        pointer = {"show": False}
        detail_at, title_at = "8%", "48%"
    main = {"type": "gauge", "min": lo, "max": hi, "startAngle": 210, "endAngle": -30, "radius": "88%",
            "center": ["50%", "58%"], "axisLine": axis_line, "progress": progress, "pointer": pointer,
            "axisTick": {"show": False}, "splitLine": {"show": False}, "axisLabel": {"show": False},
            "title": {"show": bool(s.name), "offsetCenter": [0, title_at], "fontSize": 9},
            "detail": {"offsetCenter": [0, detail_at], "fontSize": 16, "fontWeight": "bold", "color": theme.color,
                       "formatter": {"$fmt": "const", "text": label}},
            # the pointer and the arc stay between the ends; the text says the value as it is
            "data": [{"value": min(max(value, lo), hi) if value is not None else lo, "name": s.name}]}
    series = [main]
    if target is not None:
        series.append({"type": "gauge", "min": lo, "max": hi, "startAngle": 210, "endAngle": -30, "radius": "88%",
                       "center": ["50%", "58%"], "axisLine": {"show": False}, "progress": {"show": False},
                       "axisTick": {"show": False}, "splitLine": {"show": False}, "axisLabel": {"show": False},
                       "pointer": {"show": True, "length": "16%", "width": 3, "offsetCenter": [0, "-78%"],
                                   "itemStyle": {"color": theme.color}},
                       "detail": {"show": False}, "title": {"show": False}, "data": [{"value": target}]})
    return {**base, "series": series}


def _bullet(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale, base: dict,
            height_px: int) -> dict:
    o = it.options
    s = data.series[0]
    targets = data.targets or [None] * len(data.categories)
    bands = sorted(o.bands)
    top = max([*(v or 0 for v in s.values), *(t or 0 for t in targets), *(bands or [0])])
    hi = o.max if o.max is not None else bands[-1] if bands and bands[-1] >= top else _nice_max(top * 1.05)
    # a value under zero is drawn to the left of zero, not left out of the axis
    bottom = min([0.0, *(v for v in s.values if v is not None), *(t for t in targets if t is not None)])
    lo = o.min if o.min is not None else -_nice_max(-bottom * 1.05) if bottom < 0 else 0
    series: list[dict] = []
    prev = 0.0
    shades = ["#adb5bd", "#ced4da", "#e9ecef", "#f1f3f5"]
    for i, b in enumerate(bands):
        series.append({"type": "bar", "stack": "bands", "silent": True, "barWidth": "70%", "yAxisIndex": 0,
                       "itemStyle": {"color": shades[min(i, len(shades) - 1)]}, "data": [b - prev] * len(data.categories)})
        prev = b
    # the value on a second category axis laid over the first: each bar centred in its own row
    # its value written after the bar, readable over the target's mark
    series.append({"type": "bar", "name": s.name, "barWidth": "26%", "yAxisIndex": 1, "z": 3,
                   "itemStyle": {"color": theme.color}, "data": _points(s, "left"), "labelLayout": HIDE_OVERLAP,
                   "label": {**_label(o, "right"), "textBorderColor": "#ffffff", "textBorderWidth": 2}})
    marks = [{"value": [t, i]} for i, t in enumerate(targets) if t is not None]
    if marks:
        series.append({"type": "scatter", "symbol": "rect", "symbolSize": [3, 14], "z": 4, "yAxisIndex": 0,
                       "itemStyle": {"color": BAD}, "data": marks})
    axis = {**_y_axis(data.categories, max(int(it.w * MM_PX), 40), height_px - base["grid"]["top"]), "inverse": True}
    return {**base, "yAxis": [axis, {**axis, "show": False}],
            "xAxis": {**_value_axis(data, loc, o), "max": hi, "min": lo}, "series": series}


_NO_BOX = ["-"] * 5  # ECharts' own word for a value that is not there
# a category axis under values that may be below zero: its line stays at the plot's edge, not on the zero
_AT_THE_EDGE = {"axisTick": {"show": False}, "axisLine": {"onZero": False}}


def _target_line(target: float, along: str, fmt: str | None, theme: rd.Theme, loc: formats.Locale, dashed: bool) -> dict:
    """A reference value as a line across the plot, its value written at its end by the server."""
    return {"silent": True, "symbol": "none", "animation": False,
            "lineStyle": {"color": theme.color, "width": 1.2, "type": "dashed" if dashed else "solid"},
            "label": {"show": True, "position": "end", "fontSize": 8, "color": theme.color,
                      "formatter": {"$fmt": "const", "text": formats.display(target, fmt, loc)}},
            "data": [{along: target}]}


def _unseen(point: list) -> dict:
    """A point nobody sees, which the axis counts: a reference value outside the data stays
    on the plot (ECharts does not make room for a `markLine`)."""
    return {"type": "scatter", "silent": True, "symbolSize": 0, "legendHoverLink": False, "data": [point],
            "itemStyle": {"opacity": 0}, "label": {"show": False}}


def _boxplot(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale, base: dict, width_px: int) -> dict:
    """A box per category from its five numbers (minimum, first quartile, median, third
    quartile, maximum), as the data gives them: the whiskers reach the minimum and the maximum."""
    boxes = []
    for i in range(len(data.categories)):
        five = [s.values[i] for s in data.series]
        boxes.append({"value": five, "t": data.series[2].labels[i]} if all(v is not None for v in five) else {"value": _NO_BOX})
    # the value axis does not start from zero: a box is read by where it sits among the others
    return {**base, "xAxis": {**_x_axis(data.categories, width_px), **_AT_THE_EDGE},
            "yAxis": {**_value_axis(data, loc, it.options, scaled=True), "scale": True},
            "series": [{"type": "boxplot", "name": data.series[2].name, "data": boxes, "boxWidth": [8, 44],
                        "itemStyle": {"color": LIGHT, "borderColor": theme.accent, "borderWidth": 1.3}}]}


def _interval(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale, base: dict, width_px: int) -> dict:
    """A value with its interval per category: a line from the low end to the high end with a
    cap at each, a dot on the value; `options.target` as a dashed line across."""
    o = it.options
    value, low, high = data.series
    bars, dots = [], []
    for i in range(len(data.categories)):
        v, a, b = value.values[i], low.values[i], high.values[i]
        # a box of no height between its two whiskers: the value's tick, the interval's line and caps.
        # An end that is not there (a one-sided interval) leaves its side without a line
        bars.append({"value": _NO_BOX} if v is None else
                    {"value": [v if a is None else a, v, v, v, v if b is None else b]})
        dots.append(None if v is None else {"value": v, "t": value.labels[i]})
    dot: dict[str, Any] = {"type": "scatter", "name": value.name, "data": dots, "symbolSize": 7, "z": 3,
                           "itemStyle": {"color": theme.accent}, "label": {**_label(o, "right"), "distance": 9},
                           "labelLayout": HIDE_OVERLAP}
    series: list[dict] = [{"type": "boxplot", "silent": True, "data": bars, "boxWidth": [6, 14],
                           "itemStyle": {"color": theme.accent, "borderColor": theme.accent, "borderWidth": 1.3}}, dot]
    target = data.targets[0] if data.targets else None
    if target is not None:
        dot["markLine"] = _target_line(target, "yAxis", value.fmt, theme, loc, dashed=True)
        base["grid"]["right"] = 14 + int(len(formats.display(target, value.fmt, loc)) * 8 * 0.6)  # room for its value
        if data.categories:
            series.append(_unseen([data.categories[0], target]))
    yaxis = {**_value_axis(data, loc, o, scaled=True), "scale": True}
    if o.log:  # odds and hazard ratios: a ratio and its inverse at the same distance from 1, ticks at 0.5, 1, 2…
        yaxis.update(type="log", logBase=2)
        yaxis.pop("scale")
    return {**base, "xAxis": {**_x_axis(data.categories, width_px), **_AT_THE_EDGE}, "yAxis": yaxis, "series": series}


def _density(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale, base: dict) -> dict:
    """A curve on a numeric axis: the first series (the first value of `split_by`) is the
    line, the others are regions under it, filled and without a line; `options.target` is a
    value marked on the axis by a vertical line, with its value."""
    series: list[dict] = []
    for i, (name, pts) in enumerate(data.points):
        entry: dict[str, Any] = {"type": "line", "name": name, "showSymbol": False, "smooth": False,
                                 "data": [[x, y] for x, y, _ in pts if x is not None and y is not None]}
        if i == 0:
            entry.update(lineStyle={"width": 1.6, "color": theme.accent}, itemStyle={"color": theme.accent}, z=3)
        else:
            entry.update(lineStyle={"width": 0, "opacity": 0}, itemStyle={"color": BAD},
                         areaStyle={"color": BAD, "opacity": 0.45}, z=2)
        series.append(entry)
    target = data.targets[0] if data.targets else None
    if target is not None and series:
        series[0]["markLine"] = _target_line(target, "xAxis", data.fmt, theme, loc, dashed=False)
        base["grid"]["top"] += 10  # the target's value is written above the plot
        series.append(_unseen([target, 0]))
    return {**base, "xAxis": {**_value_axis(data, loc, rd.ChartOptions()), "scale": True, "splitLine": {"show": False}},
            "yAxis": {"min": 0, **_value_axis(data, loc, it.options), "axisLine": {"onZero": False}}, "series": series}


MAX_Y_NAME = 24  # the characters of a name on an `xy` chart's y axis, then «…»
MAX_XY_POINTS = 2000  # the points of a mark of an `xy` chart: beyond, thinned evenly (a runner caps them already)
_DAY_MS = 86_400_000
_TIME_LABELS = {"year": "{yyyy}", "month": "{yyyy}-{MM}", "day": "{yyyy}-{MM}-{dd}", "hour": "{HH}:{mm}",
                "minute": "{HH}:{mm}", "second": "{HH}:{mm}:{ss}", "millisecond": "{HH}:{mm}:{ss}"}


def _nice_bounds(lo: float, hi: float) -> dict:
    """An axis from `lo` to `hi` with round ends: a step of 1, 2 or 5 times a power of ten,
    about five of them."""
    import math

    if hi <= lo:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / 5
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)
    return {"min": math.floor(lo / step) * step, "max": math.ceil(hi / step) * step}


def _xy_legend(data: ChartData) -> list[str]:
    """The names an `xy` chart's legend shows, each once, in their order."""
    return list(dict.fromkeys(s.name for s in data.xy if s.legend and s.name))


def _thinned(points: list, keep_labelled: bool = True) -> list:
    """At most MAX_XY_POINTS of them, evenly spaced, the first, the last and the labelled kept."""
    n = len(points)
    if n <= MAX_XY_POINTS:
        return points
    step = (n - 1) / (MAX_XY_POINTS - 1)
    chosen = {round(i * step) for i in range(MAX_XY_POINTS)}
    return [p for i, p in enumerate(points) if i in chosen or (keep_labelled and len(p) > 2 and p[2] and isinstance(p[2], str))]


def _xy(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale, base: dict) -> dict:
    """Marks on two numeric axes (or dates along x): points with their labels, lines, steps
    (a survival curve: the value holds until the next x), stems from zero (a correlogram),
    bands (two edges, the area between filled: the low edge invisible, the width stacked on
    it); `options.target` a dashed horizontal line (`mirror`: at −target too),
    `options.target_x` vertical ones, `options.diagonal` the line y = x; `options.unit_square`
    both axes from 0 to 1 on a square plot, so that the line y = x is at 45°."""
    o = it.options
    palette = _palette(theme)
    days = (lambda x: x * _DAY_MS) if o.x_time else (lambda x: x)

    def colour(i: int) -> str:
        return palette[i % len(palette)]

    lines: list[dict] = []
    bands: list[dict] = []
    xs: list[float] = []
    ys: list[float] = []
    for k, s in enumerate(data.xy):
        pts = [p for p in s.points if not (o.log_x and p[0] <= 0)]
        pts = _thinned(pts) if s.mark == "point" else _thinned(pts, keep_labelled=False)
        c = s.color or colour(s.colour)
        xs += [p[0] for p in pts]
        ys += [v for p in pts for v in ((p[1], p[2]) if s.mark == "band" else (p[1],))]
        if s.mark == "band":
            step = "end" if any(t.mark == "step" and t.colour == s.colour for t in data.xy) else False
            edge = {"type": "line", "stack": f"band{k}", "stackStrategy": "all", "showSymbol": False, "silent": True,
                    "legendHoverLink": False, "lineStyle": {"opacity": 0}, "itemStyle": {"color": c}, "z": 1,
                    **({"step": step} if step else {})}
            bands.append({**edge, "name": s.name, "data": [[days(x), lo] for x, lo, _ in pts]})
            bands.append({**edge, "name": s.name, "data": [[days(x), hi - lo] for x, lo, hi in pts],
                          "areaStyle": {"color": c, "opacity": 0.16}})
            continue
        entry: dict[str, Any] = {"name": s.name, "itemStyle": {"color": c}, "z": 3}
        if s.mark == "point":
            labelled = any(isinstance(p[2], str) and p[2] for p in pts)
            entry.update(type="scatter", symbolSize=4.5,
                         data=[{"value": [days(x), y], **({"t": t, "label": {"show": True}} if isinstance(t, str) and t else {})}
                               for x, y, t in pts])
            if labelled:
                entry.update(label={"show": False, "position": "right", "distance": 3, "fontSize": 7, "color": theme.color,
                                    "formatter": {"$fmt": "label"}}, labelLayout=HIDE_OVERLAP)
        elif s.mark == "stem":
            entry.update(type="bar", barWidth=2, barGap="-100%", data=[[days(x), y] for x, y, _ in pts])
        else:
            entry.update(type="line", showSymbol=False, smooth=False, connectNulls=False, lineStyle={"width": 1.5, "color": c},
                         data=[[days(x), y] for x, y, _ in pts], **({"step": "end"} if s.mark == "step" else {}))
        lines.append(entry)
    series = lines + bands
    marks: list[dict] = []
    target = data.targets[0] if data.targets else None
    if target is not None:
        marks.append({"yAxis": target})
        ys.append(target)
        if o.mirror:
            marks.append({"yAxis": -target})
            ys.append(-target)
    for t in data.targets_x:
        if t is not None and not (o.log_x and t <= 0):
            marks.append({"xAxis": days(t)})
            xs.append(t)
    if o.diagonal and xs and ys:
        # where both axes reach (ECharts draws no line with an end outside the plot)
        lo, hi = (0.0, 1.0) if o.unit_square else (max(min(xs), min(ys)), min(max(xs), max(ys)))
        if lo < hi:
            marks.append([{"coord": [days(lo), lo]}, {"coord": [days(hi), hi]}])
    if marks:
        # on a series of its own, that nobody sees: the reference lines are not any mark's
        series.append({"type": "line", "name": "", "data": [], "silent": True, "legendHoverLink": False,
                       "markLine": {"silent": True, "symbol": "none", "animation": False, "label": {"show": False},
                                    "lineStyle": {"color": theme.color, "width": 1, "type": "dashed"}, "data": marks}})
        # the axes count the lines, which may lie outside every point (ECharts makes no room for a markLine)
        unseen = [[days(t), (sum(ys) / len(ys)) if ys else 0] for t in data.targets_x if t is not None] if xs or ys else []
        if target is not None and xs:
            unseen += [[days(xs[0]), target]] + ([[days(xs[0]), -target]] if o.mirror else [])
        if unseen:
            series.append({**_unseen(unseen[0]), "data": unseen})
    x_axis: dict[str, Any] = {**_value_axis(data, loc, rd.ChartOptions()), "scale": True, "splitLine": {"show": False},
                              "axisLine": {"onZero": False}}
    if o.x_time:
        x_axis.update(type="time", axisLabel={"fontSize": 9, "hideOverlap": True, "formatter": _TIME_LABELS})
        base["grid"]["right"] = 24  # the last date is written centred under its tick: room for its half
    elif o.log_x:
        x_axis.update(type="log", logBase=10)
        x_axis.pop("scale")
    y_axis = {**_value_axis(data, loc, o), "scale": True}
    if data.y_names:  # names at the whole numbers; ECharts draws the ends 0.5 and top + 0.5 too, written as nothing
        whole = list(range(1, max(data.y_names) + 1))  # a tick at each whole number: the axis itself starts at 0.5
        y_axis.update(min=0.5, max=whole[-1] + 0.5, interval=1, scale=False,
                      axisLabel={"fontSize": 9, "interval": 0, "customValues": whole, "formatter": {
                          "$fmt": "names", "max": MAX_Y_NAME, "names": {str(j): n for j, n in data.y_names.items()}}},
                      axisTick={"customValues": whole},
                      splitLine={"show": True, "customValues": whole, "lineStyle": {"color": "#e6e8ec", "width": 0.6}})
        # the grid's own room for the names (containLabel measures the ticks ECharts would have chosen, not these)
        longest = max(min(len(n), MAX_Y_NAME) for n in data.y_names.values())
        base["grid"].update(containLabel=False, left=10 + int(longest * 9 * 0.58))
    elif any(s.mark == "stem" for s in data.xy):
        y_axis.pop("scale")  # a stem stands on zero: the axis has it
    elif bands and ys and o.min is None and o.max is None:
        # a band's stack counts zero (the low edge stands on it): the axis is the marks' own, rounded
        y_axis.update(_nice_bounds(min(ys), max(ys)))
    if o.unit_square:  # 0 to 1 on both axes, and a square plot: the diagonal at 45°, centred in the chart's width
        for axis in (x_axis, y_axis):
            axis.pop("scale", None)
            axis.update(min=0, max=1, interval=0.2)
        width_px, height_px = max(int(it.w * MM_PX), 40), max(int(it.h * MM_PX), 20)
        left, right, bottom = 34, 10, 24  # the labels of the y axis («0.8») and of the x axis, outside the square
        side = max(40, min(width_px - left - right, height_px - base["grid"]["top"] - bottom))
        base["grid"].update(containLabel=False, left=left + max(0, (width_px - left - right - side) // 2), width=side,
                            height=side)
        base["grid"].pop("right", None)
        base["grid"].pop("bottom", None)
    if "interval" not in y_axis:  # a short plot asks for fewer ticks: ECharts' five on 50 px are a stack of labels
        plot_px = max(int(it.h * MM_PX), 20) - base["grid"]["top"] - XY_X_LABELS_PX
        if plot_px < 5 * Y_TICK_PX:
            y_axis["splitNumber"] = max(1, plot_px // Y_TICK_PX)
    legend = {**base["legend"], "data": _xy_legend(data)}
    return {**base, "legend": legend, "xAxis": x_axis, "yAxis": y_axis, "series": series}


def sparkline(categories: list[str], values: list[Any], accent: str) -> dict:
    nums = [v if isinstance(v, (int, float)) and not isinstance(v, bool) else None for v in values]
    return {"animation": False, "color": [accent], "grid": {"left": 1, "right": 1, "top": 2, "bottom": 1},
            "xAxis": {"type": "category", "show": False, "data": categories, "boundaryGap": False},
            "yAxis": {"type": "value", "show": False, "scale": True},
            "series": [{"type": "line", "data": nums, "symbol": "none", "lineStyle": {"width": 1.4},
                        "areaStyle": {"opacity": 0.15}}]}


# ── the service ──────────────────────────────────────────────────────────────
def render_all(jobs: list, url: str) -> dict[str, str]:
    """Every chart as SVG text, by the name the template reads: in parallel, through the
    chart service. ReportError when the service is not there, or refuses a chart."""
    if not jobs:
        return {}
    if not url:
        raise ReportError("the report has charts and no chart service is configured (REPORTS__CHART_URL)",
                          "unavailable")
    import requests

    endpoint = url.rstrip("/") + "/render"

    def one(job: Any) -> tuple[str, str]:
        try:
            resp = requests.post(endpoint, json={"width": job.width, "height": job.height, "option": job.option},
                                 timeout=SERVICE_TIMEOUT_SECONDS)
        except requests.RequestException:
            raise ReportError("the chart service is not answering, so the charts cannot be drawn: "
                              "try again in a minute, or tell an administrator (REPORTS__CHART_URL)",
                              "unavailable") from None
        if resp.status_code != 200 or not resp.text.lstrip().startswith("<svg"):
            raise ReportError(f"the chart «{job.alt}» could not be drawn: {resp.text[:300]}", "bad_request")
        return job.name, resp.text

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        return dict(pool.map(one, jobs))
