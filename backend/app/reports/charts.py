"""Charts of a report: ECharts options built from the data, drawn to SVG by the chart service.

One preset per kind (docs/specs/reports.md, «Report items»), with a fixed layout: ECharts'
own defaults cut a treemap and move a legend over the plot. The options are plain JSON,
built here, never from text a person wrote: a category called `fmt:x` or `<script>` is
a value inside `data`. Numbers are written by the server in the report's language and
travel with each point (`t`); where ECharts computes the values itself (an axis' ticks)
it formats them through a formatter named from the service's closed list:

    {"$fmt": "label"}                      the point's text (`t`), else its value
    {"$fmt": "nameLabel", "sep": " "}      the category and the point's text (on two lines without sep)
    {"$fmt": "namePercent", "locale": l}   the category and its share, as a pie shows it
    {"$fmt": "compact", "locale": l}       1,2 Mio, 3.4K… (Intl, compact notation)
    {"$fmt": "percent", "locale": l, "decimals": n}
    {"$fmt": "const", "text": "…"}         a text decided here (a gauge's value)

The service (frontend/chart-renderer/render.mjs) turns only those into functions, only
under a `formatter` key, and measures text with the advance widths of the PDF's font.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Any

from app.reports import definition as rd
from app.reports import formats
from app.reports.errors import ReportError

MAX_CATEGORIES = 2000
MAX_SERIES = 24
# the PDF's font first (Typst draws the chart with it); then what a browser has, for the
# screen's pages, where a chart inside an <img> cannot use the page's fonts (Plex alone fell
# back to a serif)
FONT = "IBM Plex Sans, Helvetica Neue, Arial, sans-serif"
SERVICE_TIMEOUT_SECONDS = 30
_EXTRA = ["#14a38b", "#e0a23b", "#c2477a", "#6b7590", "#8a63d2", "#2f9e44", "#d9480f", "#1c7ed6", "#868e96"]
GOOD, BAD, MID = "#2b8a3e", "#c92a2a", "#e0a23b"


@dataclass
class SeriesData:
    name: str
    values: list[float | None]
    labels: list[str]
    as_: str = "bar"
    axis: str = "left"


@dataclass
class ChartData:
    kind: str
    fmt: str | None = None
    title: str = ""
    categories: list[str] = field(default_factory=list)
    series: list[SeriesData] = field(default_factory=list)
    # scatter: (series name, [(x, y, text)])
    points: list[tuple[str, list[tuple[float | None, float | None, str]]]] = field(default_factory=list)
    targets: list[float | None] = field(default_factory=list)
    total_label: str = "Total"


def _palette(theme: rd.Theme) -> list[str]:
    return [theme.accent, *_EXTRA]


def _value_formatter(fmt: str | None, loc: formats.Locale) -> dict:
    if fmt and formats.is_number_pattern(fmt) and "%" in fmt:
        sec = formats.number_pattern(fmt)[0]
        return {"$fmt": "percent", "locale": loc.code, "decimals": sec.max_frac}
    return {"$fmt": "compact", "locale": loc.code}


def _points(s: SeriesData) -> list[dict | None]:
    return [None if v is None else {"value": v, "t": t} for v, t in zip(s.values, s.labels)]


def _legend_rows(names: list[str], width: int) -> int:
    """How many lines the legend takes: ECharts does not keep room for it in the grid."""
    rows, used = 1, 0.0
    for name in names:
        w = 10 + 5 + min(len(name), 22) * 9 * 0.56 + 8  # icon, gap, text (truncated at 120 px), item gap
        if used and used + w > width - 10:
            rows, used = rows + 1, 0.0
        used += w
    return min(rows, 4)


def _base(data: ChartData, theme: rd.Theme, legend: bool, width: int) -> dict:
    top = 22 if data.title else 8
    names = [s.name for s in data.series] or [name for name, _ in data.points]
    lines = _legend_rows(names, width) if legend else 0
    option: dict[str, Any] = {
        "animation": False,
        "color": _palette(theme),
        "textStyle": {"fontFamily": FONT, "fontSize": 9, "color": theme.color},
        "grid": {"left": 6, "right": 10, "top": top + 14 * lines, "bottom": 6, "containLabel": True},
        "legend": {"show": legend, "top": top - 2 if data.title else 2, "left": "center", "itemWidth": 10,
                   "itemHeight": 7, "itemGap": 8, "textStyle": {"fontSize": 9, "overflow": "truncate", "width": 120}},
    }
    if data.title:
        option["title"] = {"text": data.title, "left": 0, "top": 0, "textStyle": {"fontSize": 10, "fontWeight": "bold",
                                                                                   "color": theme.color}}
    return option


def _across(width_px: int, categories: list) -> int:
    """The room of a label on a horizontal category axis: its band, but never so little that
    a label is cut to «20…» — when they crowd, ECharts hides the ones that overlap
    (`hideOverlap`) and those it shows stay whole."""
    return max(width_px // max(len(categories), 1) - 4, 90)


def _category_axis(data: ChartData, width: int) -> dict:
    return {"type": "category", "data": data.categories,
            "axisLabel": {"fontSize": 9, "hideOverlap": True, "overflow": "truncate", "width": width},
            "axisTick": {"alignWithLabel": True}}


def _value_axis(data: ChartData, loc: formats.Locale, o: rd.ChartOptions) -> dict:
    axis: dict[str, Any] = {"type": "value", "axisLabel": {"fontSize": 9, "formatter": _value_formatter(data.fmt, loc)},
                            "splitLine": {"lineStyle": {"color": "#e9ecef"}}}
    if o.min is not None:
        axis["min"] = o.min
    if o.max is not None:
        axis["max"] = o.max
    return axis


def _label(o: rd.ChartOptions, position: str) -> dict:
    return {"show": o.labels, "position": position, "fontSize": 8, "formatter": {"$fmt": "label"}}


def option(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale) -> dict:
    """The ECharts option of a chart item."""
    o = it.options
    kind = data.kind
    many = len(data.series) > 1 or len(data.points) > 1
    legend = o.legend if o.legend is not None else (many and kind not in ("pie", "donut", "funnel", "treemap", "heatmap",
                                                                          "gauge", "bullet"))
    width_px = max(int(it.w * 96 / 25.4), 40)
    base = _base(data, theme, bool(legend), width_px)
    if kind in ("column", "line", "area", "combo"):
        right = any(s.axis == "right" for s in data.series)
        series = []
        for s in data.series:
            line = kind in ("line", "area") or (kind == "combo" and s.as_ == "line")
            entry: dict[str, Any] = {"type": "line" if line else "bar", "name": s.name, "data": _points(s),
                                     "label": _label(o, "top"), "yAxisIndex": 1 if s.axis == "right" and right else 0}
            if line:
                entry.update(symbol="circle", symbolSize=4, lineStyle={"width": 1.6}, connectNulls=False)
                if kind == "area":
                    entry["areaStyle"] = {"opacity": 0.18}
            else:
                entry["barMaxWidth"] = 48
            if o.stacked:
                entry["stack"] = "total"
            series.append(entry)
        yaxis = [_value_axis(data, loc, o)] + ([{**_value_axis(data, loc, o), "splitLine": {"show": False}}]
                                               if right else [])
        return {**base, "xAxis": _category_axis(data, _across(width_px, data.categories)),
                "yAxis": yaxis, "series": series}
    if kind == "bar":
        series = [{"type": "bar", "name": s.name, "data": _points(s), "label": _label(o, "right"), "barMaxWidth": 28,
                   **({"stack": "total"} if o.stacked else {})} for s in data.series]
        if o.labels:  # room for the value written after the longest bar
            longest = max((len(t) for s in data.series for t in s.labels), default=0)
            base["grid"]["right"] = 10 + int(longest * 8 * 0.6)
        return {**base, "yAxis": {**_category_axis(data, max(int(width_px * 0.3), 40)), "inverse": True},
                "xAxis": _value_axis(data, loc, o), "series": series}
    if kind in ("pie", "donut"):
        s = data.series[0]
        points = [{"name": c, "value": v, "t": t} for c, v, t in zip(data.categories, s.values, s.labels)
                  if v is not None and v > 0]
        return {**base, "series": [{
            "type": "pie", "radius": ["45%", "70%"] if kind == "donut" else [0, "70%"], "center": ["50%", "54%"],
            "data": points, "avoidLabelOverlap": True,
            "label": {"fontSize": 8, "formatter": {"$fmt": "namePercent", "locale": loc.code}, "overflow": "truncate",
                      "width": max(width_px // 4, 50)},
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
                bars.append({"value": -v, "t": t, "itemStyle": {"color": BAD}})
        total = formats.display(running, data.fmt, loc)
        bases.append(0)
        bars.append({"value": running, "t": total, "itemStyle": {"color": theme.accent}})
        cats = ChartData(kind, data.fmt, data.title, [*data.categories, data.total_label])
        return {**base, "xAxis": _category_axis(cats, _across(width_px, cats.categories)),
                "yAxis": _value_axis(data, loc, o),
                "series": [{"type": "bar", "stack": "w", "silent": True, "data": bases,
                            "itemStyle": {"color": "transparent"}, "label": {"show": False}},
                           {"type": "bar", "stack": "w", "name": s.name, "data": bars, "barMaxWidth": 48,
                            "label": {**_label(o, "top"), "show": True}}]}
    if kind == "funnel":
        s = data.series[0]
        points = sorted(({"name": c, "value": v, "t": t} for c, v, t in zip(data.categories, s.values, s.labels)
                         if v is not None), key=lambda p: -p["value"])
        return {**base, "series": [{"type": "funnel", "left": "10%", "right": "10%", "top": base["grid"]["top"],
                                    "bottom": 6, "sort": "descending", "gap": 2, "data": points,
                                    "label": {"position": "inside", "fontSize": 8, "color": "#fff",
                                              "formatter": {"$fmt": "nameLabel", "sep": " "}}}]}
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
            "label": {"fontSize": 8, "overflow": "truncate", "formatter": {"$fmt": "nameLabel"}},
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
        names = ChartData(kind, categories=[s.name for s in data.series])
        base["grid"]["bottom"] = 30
        return {**base, "xAxis": _category_axis(data, _across(width_px, data.categories)),
                "yAxis": _category_axis(names, max(int(width_px * 0.25), 40)),
                "visualMap": {"min": lo, "max": hi if hi > lo else lo + 1, "calculable": False, "orient": "horizontal",
                              "left": "center", "bottom": 0, "itemWidth": 8, "itemHeight": 90,
                              "textStyle": {"fontSize": 8}, "formatter": _value_formatter(data.fmt, loc),
                              "inRange": {"color": ["#f1f3ff", theme.accent]}},
                "series": [{"type": "heatmap", "data": cells, "label": _label(o, "inside"),
                            "itemStyle": {"borderColor": "#fff", "borderWidth": 1}}]}
    if kind == "scatter":
        series = [{"type": "scatter", "name": name, "symbolSize": 5,
                   "data": [{"value": [x, y], "t": t} for x, y, t in pts if x is not None and y is not None],
                   "label": _label(o, "top")} for name, pts in data.points]
        return {**base, "xAxis": {**_value_axis(data, loc, rd.ChartOptions()), "scale": True},
                "yAxis": {**_value_axis(data, loc, o), "scale": True}, "series": series}
    if kind == "gauge":
        return _gauge(data, it, theme, base)
    if kind == "bullet":
        return _bullet(data, it, theme, loc, base)
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
            "data": [{"value": value if value is not None else lo, "name": s.name}]}
    series = [main]
    if target is not None:
        series.append({"type": "gauge", "min": lo, "max": hi, "startAngle": 210, "endAngle": -30, "radius": "88%",
                       "center": ["50%", "58%"], "axisLine": {"show": False}, "progress": {"show": False},
                       "axisTick": {"show": False}, "splitLine": {"show": False}, "axisLabel": {"show": False},
                       "pointer": {"show": True, "length": "16%", "width": 3, "offsetCenter": [0, "-78%"],
                                   "itemStyle": {"color": theme.color}},
                       "detail": {"show": False}, "title": {"show": False}, "data": [{"value": target}]})
    return {**base, "series": series}


def _bullet(data: ChartData, it: rd.ChartItem, theme: rd.Theme, loc: formats.Locale, base: dict) -> dict:
    o = it.options
    s = data.series[0]
    targets = data.targets or [None] * len(data.categories)
    bands = sorted(o.bands)
    top = max([*(v or 0 for v in s.values), *(t or 0 for t in targets), *(bands or [0])])
    hi = o.max if o.max is not None else bands[-1] if bands and bands[-1] >= top else _nice_max(top * 1.05)
    series: list[dict] = []
    prev = 0.0
    shades = ["#adb5bd", "#ced4da", "#e9ecef", "#f1f3f5"]
    for i, b in enumerate(bands):
        series.append({"type": "bar", "stack": "bands", "silent": True, "barWidth": "70%", "yAxisIndex": 0,
                       "itemStyle": {"color": shades[min(i, len(shades) - 1)]}, "data": [b - prev] * len(data.categories)})
        prev = b
    # the value on a second category axis laid over the first: each bar centred in its own row
    series.append({"type": "bar", "name": s.name, "barWidth": "26%", "yAxisIndex": 1, "z": 3,
                   "itemStyle": {"color": theme.color}, "data": _points(s), "label": _label(o, "right")})
    marks = [{"value": [t, i]} for i, t in enumerate(targets) if t is not None]
    if marks:
        series.append({"type": "scatter", "symbol": "rect", "symbolSize": [3, 14], "z": 4, "yAxisIndex": 0,
                       "itemStyle": {"color": BAD}, "data": marks})
    axis = {**_category_axis(data, max(int(it.w * 96 / 25.4 * 0.25), 40)), "inverse": True}
    return {**base, "yAxis": [axis, {**axis, "show": False}],
            "xAxis": {**_value_axis(data, loc, o), "max": hi, "min": o.min if o.min is not None else 0},
            "series": series}


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
