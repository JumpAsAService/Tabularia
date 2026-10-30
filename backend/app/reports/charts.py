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
from dataclasses import dataclass, field
from typing import Any

from app.reports import definition as rd
from app.reports import formats
from app.reports.errors import ReportError

MAX_CATEGORIES = 2000
MAX_SERIES = 24
MAX_LABEL_CHARS = 40  # a name on an axis or a slice: longer ones end in «…», so it takes three lines at most
MM_PX = 96 / 25.4
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
    fmt: str | None = None  # the series' format, or the chart's


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


def _points(s: SeriesData, below: str | None = None) -> list[dict | None]:
    """The values with their texts; `below`: where the label of a value under zero goes (a
    bar's label sits at its end, which for a negative bar is the other side)."""
    return [None if v is None else {"value": v, "t": t, **({"label": {"position": below}} if below and v < 0 else {})}
            for v, t in zip(s.values, s.labels)]


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
    of a year) every label is written, on more lines if it needs them; when they crowd (the
    days of a year) ECharts writes those that do not overlap, whole — never «20…»."""
    band = (width_px - 60) / max(len(categories), 1)  # the plot is the chart less its value axis
    if band >= 36:
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


def _value_axis(data: ChartData, loc: formats.Locale, o: rd.ChartOptions, side: str = "left") -> dict:
    # an axis writes its numbers in the format of the first series on it (a margin in % beside amounts)
    fmt = next((s.fmt for s in data.series if s.axis == side and s.fmt), data.fmt)
    axis: dict[str, Any] = {"type": "value", "axisLabel": {"fontSize": 9, "formatter": _value_formatter(fmt, loc)},
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
    if data.kind == "scatter":
        return any(x is not None and y is not None for _, pts in data.points for x, y, _ in pts)
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
    legend = o.legend if o.legend is not None else (many and kind not in ("pie", "donut", "funnel", "treemap", "heatmap",
                                                                          "gauge", "bullet"))
    width_px = max(int(it.w * MM_PX), 40)
    height_px = max(int(it.h * MM_PX), 20)
    base = _base(data, theme, bool(legend), width_px)
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
        base["grid"]["bottom"] = 30
        # the scale's ends written by the server, in the chart's format
        ends = [formats.display(hi, data.fmt, loc), formats.display(lo, data.fmt, loc)] if values else ["", ""]
        return {**base, "xAxis": _x_axis(data.categories, width_px),
                "yAxis": _y_axis([s.name for s in data.series], width_px, height_px - base["grid"]["top"] - 30),
                "visualMap": {"min": lo, "max": hi if hi > lo else lo + 1, "calculable": False, "orient": "horizontal",
                              "left": "center", "bottom": 0, "itemWidth": 8, "itemHeight": 90, "text": ends,
                              "textGap": 4, "textStyle": {"fontSize": 8},
                              "inRange": {"color": ["#f1f3ff", theme.accent]}},
                "series": [{"type": "heatmap", "data": cells, "label": _label(o, "inside"), "labelLayout": HIDE_OVERLAP,
                            "itemStyle": {"borderColor": "#fff", "borderWidth": 1}}]}
    if kind == "scatter":
        series = [{"type": "scatter", "name": name, "symbolSize": 5,
                   "data": [{"value": [x, y], "t": t} for x, y, t in pts if x is not None and y is not None],
                   "label": _label(o, "top"), "labelLayout": HIDE_OVERLAP} for name, pts in data.points]
        return {**base, "xAxis": {**_value_axis(data, loc, rd.ChartOptions()), "scale": True},
                "yAxis": {**_value_axis(data, loc, o), "scale": True}, "series": series}
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
