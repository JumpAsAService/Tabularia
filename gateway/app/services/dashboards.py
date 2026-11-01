"""Dashboards: who sees them, what they hold, who opened them. docs/specs/dashboards.md; the
object in models/dashboard.py, the routes in routes/dashboards.py.

Who sees a dashboard: VIEW on its folder (administrators: every one). Whoever cannot see it
gets 404, as if it did not exist; EDIT on the folder changes, moves, renames, duplicates and
deletes it, and restores a version.

`parameters` and `datasets` are a report's, checked by a report's code, and a saved
definition points only to what its author may read, as a report's (services/reports.py). A
tile is a report's item of its type with `grid` in place of `x`, `y`, `w`, `h`: `as_report`
writes the definition as a report's, each tile as an item in a row of its own on a page wide
enough for anything, and that is what the gateway's checks and the engine's read. What only a
dashboard has is checked here: its limits, the interval, the grid. The problems are said as
those of a dashboard (`_in_our_words`).

Reading the numbers (`numbers`) is three steps: what was asked is checked (`check_asked`);
rights and values with the reader's, at every request (`_plan`), which no cache stands in for;
the engine works them out on the preview queue (`_computed`, app/reports/tiles.py). A click names
a tile and values, checked against the saved definition (`_checked_clicks`), and the engine filters
by that tile's own expression; a click on a tile the reader may not read counts for nothing. A saved
dashboard's are shared by whoever reads the same source, with one query: the cache is in the
engine (app/reports/tile_cache.py), as Valkey is not the gateway's, and what it is given is only
what `_plan` found this reader may read. A definition being edited shares nothing.

A document of it (`render`, a PDF or an image) has the same three steps: the engine lays it out
(app/reports/dashboard_layout.py) on the export queue, with the rows of the screen. A group's
Pushmail cell sends one with the rights of whoever runs the group: what makes one valid for them is
in services/pushmail.py, which schedule_authority asks too, and `groups_using` says who sends what.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import JSON, and_, case, cast, func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.engine_client import CHECK, get_engine_client
from app.models import Dashboard, DashboardVersion, DashboardView, User
from app.models.permission import Capability
from app.services import audit
from app.services import permissions as perm_service
from app.services import report_definition, report_params, report_views, reports, sql_scripts
from app.services.permissions import is_admin
from app.services.report_definition import ITEM_ID, Problem, _keys, _text

MAX_TILES, MAX_DATASETS, MAX_PARAMETERS = 40, 10, 20
MIN_REFRESH_SECONDS, MAX_REFRESH_SECONDS = 60, 24 * 3600  # 0: by hand only
GRID_COLUMNS, MAX_GRID_ROW, MAX_TILE_ROWS = 12, 1000, 100
TILE_TYPES = ("kpi", "chart", "table", "text", "image", "comment")
FORMATS = ("pdf", "png")
# the smallest tile of each type, columns and rows (a row is 40 px)
MIN_TILE = {"kpi": (2, 2), "chart": (4, 4), "table": (4, 4), "text": (2, 1), "image": (1, 1), "comment": (4, 3)}
FILTERS_OTHERS = ("chart", "table")  # a KPI, a text and an image do not filter
MAX_CLICK_VALUES, MAX_CLICK_TEXT = 100, 1000  # what a person chooses with Ctrl; what a value on an axis can be
_HAS_TITLE = ("chart", "table", "comment")  # a report's items with a title of their own: the rest take the tile's here
_ITEM_PATH = re.compile(r"^sections\.body\.items\[(\d+)\](.*)$")
_TILE_PATH = re.compile(r"^tiles\[\d+\]\.?(.*)$")


# ── who sees what ────────────────────────────────────────────────────────────
def can_see(session: Session, user: User, dashboard: Dashboard) -> bool:
    return is_admin(session, user) or perm_service.has_capability(session, user, dashboard.project_id, Capability.VIEW)


def visible_query(session: Session, user: User, project_ids: Optional[set[int]] = None, ordered: bool = True):
    """A select of the dashboards this person sees (in `project_ids` when given), by name
    unless `ordered` is false, or None when there are none."""
    stmt = select(Dashboard)
    if not is_admin(session, user):
        readable = set(perm_service.readable_project_ids(session, user))
        if project_ids is not None:
            readable &= set(project_ids)
        if not readable:
            return None
        stmt = stmt.where(Dashboard.project_id.in_(readable))  # type: ignore[attr-defined]
    elif project_ids is not None:
        stmt = stmt.where(Dashboard.project_id.in_(project_ids))  # type: ignore[attr-defined]
    return stmt.order_by(Dashboard.name) if ordered else stmt


def get_visible(session: Session, user: User, dashboard_id: int) -> Dashboard:
    """The dashboard, or 404: one that cannot be seen does not exist for this person."""
    dashboard = session.get(Dashboard, dashboard_id)
    if dashboard is None or not can_see(session, user, dashboard):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return dashboard


# ── what reads what ──────────────────────────────────────────────────────────
def dashboards_reading(session: Session, kind: str, object_id: int) -> list[Dashboard]:
    """The dashboards one of whose datasets (in the current version) names the object. A SQL
    on the catalog names its datasources by name: those are not found here."""
    source_kind, key = reports._POINTS_TO[kind]
    rows = session.exec(select(Dashboard, DashboardVersion).join(DashboardVersion, and_(
        Dashboard.id == DashboardVersion.dashboard_id, Dashboard.current_version == DashboardVersion.version,
    )).where(DashboardVersion.content.contains(f'"{key}"'))).all()  # type: ignore[union-attr]
    return [dashboard for dashboard, row in rows
            if any(isinstance(d, dict) and (d.get("source") or {}).get("kind") == source_kind
                   and (d.get("source") or {}).get(key) == object_id for d in _parsed(row).get("datasets") or [])]


def not_read_by_dashboards(session: Session, user: User, kind: str, object_id: int, what: str) -> None:
    """409 when dashboards read the object about to be deleted, as for reports: it names the
    ones this person sees and counts the others, whose names are not theirs to know."""
    found = dashboards_reading(session, kind, object_id)
    if not found:
        return
    seen = sorted(d.name for d in found if can_see(session, user, d))
    others = len(found) - len(seen)
    who = ", ".join(f"«{n}»" for n in seen[:10]) + (" …" if len(seen) > 10 else "")
    if not seen:
        who = f"{others} {'dashboard' if others == 1 else 'dashboards'} you cannot see"
    elif others:
        who = f"the dashboards {who} and {others} you cannot see"
    else:
        who = f"the dashboards {who}"
    raise HTTPException(status_code=409, detail={"message": f"{what} is read by {who}: change them first",
                                                 "flows": [], "groups": [], "reports": [], "dashboards": seen})


# ── versions ─────────────────────────────────────────────────────────────────
def version_row(session: Session, dashboard: Dashboard, version: Optional[int] = None) -> Optional[DashboardVersion]:
    """A version of the dashboard (None: the current one); None when it has no such version."""
    return session.exec(select(DashboardVersion).where(
        DashboardVersion.dashboard_id == dashboard.id,
        DashboardVersion.version == (dashboard.current_version if version is None else version))).first()


def _parsed(row: Optional[DashboardVersion]) -> dict:
    if row is None:
        return empty()
    try:
        return json.loads(row.content)
    except ValueError:
        return empty()


def content_of(session: Session, dashboard: Dashboard, version: Optional[int] = None) -> dict:
    return _parsed(version_row(session, dashboard, version))


def sizes_of(session: Session, dashboard_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    """{dashboard: (datasets, parameters, tiles)} of the current versions, counted by the
    database: a definition holds its images, and a list of dashboards has no use for them."""
    if not dashboard_ids:
        return {}
    if session.get_bind().dialect.name == "postgresql":
        tree = cast(DashboardVersion.content, JSON)

        def length(key: str):
            part = tree.op("->")(key)
            return case((func.json_typeof(part) == "array", func.json_array_length(part)), else_=0)
    else:  # SQLite, the tests'
        def length(key: str):
            return func.coalesce(func.json_array_length(DashboardVersion.content, f"$.{key}"), 0)
    rows = session.exec(select(DashboardVersion.dashboard_id, length("datasets"), length("parameters"), length("tiles")).join(
        Dashboard, and_(Dashboard.id == DashboardVersion.dashboard_id, Dashboard.current_version == DashboardVersion.version),
    ).where(Dashboard.id.in_(dashboard_ids))).all()  # type: ignore[union-attr]
    return {dashboard_id: (datasets, parameters, tiles) for dashboard_id, datasets, parameters, tiles in rows}


def add_version(session: Session, dashboard: Dashboard, content: dict, note: str, user: User) -> DashboardVersion:
    """The next version, which becomes the current one. The dashboard's row stays locked until
    the commit: two saves at once take the next numbers in turn."""
    session.exec(select(Dashboard.id).where(Dashboard.id == dashboard.id).with_for_update())
    last = session.exec(select(DashboardVersion.version).where(DashboardVersion.dashboard_id == dashboard.id)
                        .order_by(DashboardVersion.version.desc())).first() or 0  # type: ignore[attr-defined]
    row = DashboardVersion(dashboard_id=dashboard.id, version=last + 1, content=json.dumps(content), note=note,
                           created_by=user.id)
    session.add(row)
    dashboard.current_version = row.version
    session.add(dashboard)
    return row


# ── the definition ───────────────────────────────────────────────────────────
def empty(locale: str = "en", zone: str = "UTC") -> dict:
    """A new dashboard's definition: no data yet, refreshed by hand."""
    return {"v": 1, "locale": locale if locale in report_definition.LOCALES else "en", "timezone": zone or "UTC",
            "refresh_seconds": 0, "parameters": [], "datasets": [], "tiles": []}


def as_report(definition: dict) -> dict:
    """The definition as a report's, for the code that checks one: its parameters and datasets
    as they are, each tile an item in a row of its own (a report's rules on millimetres have
    nothing to say about a grid) on the largest page there is."""
    tiles = definition.get("tiles")
    items = []
    for i, tile in enumerate(tiles if isinstance(tiles, list) else []):
        if isinstance(tile, dict):
            dropped = {"grid", "filters_others", *(() if tile.get("type") in _HAS_TITLE else ("title",))}
            items.append({**{k: v for k, v in tile.items() if k not in dropped}, "x": 0, "y": i * 11, "w": 100, "h": 10})
    shaped: dict[str, Any] = {"v": definition.get("v"), "page": {"size": {"w": 2000, "h": 2000}, "margins": [0, 0, 0, 0]},
                              "sections": {"body": {"items": items}}}
    for key in ("locale", "timezone", "parameters", "datasets"):
        if key in definition:
            shaped[key] = definition[key]
    return shaped


def _in_our_words(message: str) -> str:
    """A report's message about a dashboard: the checks are the same, the words are not."""
    message = re.sub(r"\b([Tt])he report\b", lambda m: f"{m.group(1)}he dashboard", message)
    return re.sub(r"\b([Tt])he item\b", lambda m: f"{m.group(1)}he tile", message)


def _said(p: Problem) -> Problem:
    """A problem found by a report's code, with the dashboard's words and paths."""
    return Problem(_in_our_words(p.message), _ITEM_PATH.sub(r"tiles[\1]\2", p.where), p.item)


def _problem_dict(p: Problem, definition: Any) -> dict:
    """As the engine says its problems: the property inside the tile (`series[0].value`)."""
    said = p.as_dict(definition)
    if p.item is not None and (m := _TILE_PATH.match(p.where)):
        said["field"] = m.group(1)
    return said


def _at_most(value: Any, most: int, key: str, what: str) -> list:
    """A list of `key`, stating the limit."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise Problem(f"The {what} are a list of at most {most}", key)
    if len(value) > most:
        raise Problem(f"A dashboard holds at most {most} {what}: this one has {len(value)}", key)
    return value


def _refresh(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != 0 and not (
            MIN_REFRESH_SECONDS <= value <= MAX_REFRESH_SECONDS):
        raise Problem(f"The refresh is 0 (by hand) or from {MIN_REFRESH_SECONDS} to {MAX_REFRESH_SECONDS} seconds",
                      "refresh_seconds")


def _whole(value: Any, where: str, what: str, lo: int, hi: int, item: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Problem(f"{what} is a whole number", where, item)
    if not lo <= value <= hi:
        raise Problem(f"{what} is between {lo} and {hi}", where, item)
    return value


def _grid(tile: dict, at: str, what: str, item: str) -> tuple[int, int, int, int]:
    where = f"{at}.grid"
    grid = _keys(tile.get("grid"), where, ("x", "y", "w", "h"), what=f"{what}'s grid", item=item)
    for key in ("x", "y", "w", "h"):
        if grid.get(key) is None:
            raise Problem(f"{what}'s grid: «{key}» is missing", f"{where}.{key}", item)
    x = _whole(grid["x"], f"{where}.x", f"{what}'s column", 0, GRID_COLUMNS - 1, item)
    y = _whole(grid["y"], f"{where}.y", f"{what}'s row", 0, MAX_GRID_ROW, item)
    w = _whole(grid["w"], f"{where}.w", f"{what}'s width", 1, GRID_COLUMNS, item)
    h = _whole(grid["h"], f"{where}.h", f"{what}'s height", 1, MAX_TILE_ROWS, item)
    if x + w > GRID_COLUMNS:
        raise Problem(f"{what} goes past the {GRID_COLUMNS} columns of the grid", where, item)
    least_w, least_h = MIN_TILE[tile["type"]]
    if w < least_w:
        raise Problem(f"{what} is at least {least_w} columns wide", where, item)
    if h < least_h:
        raise Problem(f"{what} is at least {least_h} rows high", where, item)
    return x, y, w, h


def _tiles(tiles: list) -> None:
    """What a report's item does not have: the id and type a dashboard takes, the place on the
    grid, which two tiles do not share, and what the tile itself says."""
    ids: set[str] = set()
    placed: list[tuple[str, tuple[int, int, int, int]]] = []
    for i, tile in enumerate(tiles):
        at = f"tiles[{i}]"
        if not isinstance(tile, dict):
            raise Problem("A tile is an object", at)
        item_id = tile.get("id")
        if not isinstance(item_id, str) or not ITEM_ID.match(item_id):
            raise Problem("A tile needs an id (letters, digits, _ and -)", f"{at}.id")
        if item_id in ids:
            raise Problem(f"Two tiles have the id «{item_id}»", f"{at}.id", item_id)
        ids.add(item_id)
        what = f"The tile «{item_id}»"
        if tile.get("type") not in TILE_TYPES:
            raise Problem(f"A tile is one of {', '.join(TILE_TYPES)}: not «{str(tile.get('type'))[:20]}»", f"{at}.type", item_id)
        for key in ("x", "y", "w", "h"):
            if key in tile:
                raise Problem(f"{what} takes its place from «grid»: not «{key}»", f"{at}.{key}", item_id)
        if tile.get("grid") is None:
            raise Problem(f"{what}: «grid» is missing", f"{at}.grid", item_id)
        box = _grid(tile, at, what, item_id)
        x, y, w, h = box
        for other, (ox, oy, ow, oh) in placed:
            if x < ox + ow and ox < x + w and y < oy + oh and oy < y + h:
                raise Problem(f"{what} lies on «{other}»: two tiles do not share a cell", f"{at}.grid", item_id)
        placed.append((item_id, box))
        if tile.get("type") not in _HAS_TITLE:
            _text(tile.get("title"), f"{at}.title", f"{what}'s title", 200, item_id)
        if "filters_others" in tile:
            if tile["type"] not in FILTERS_OTHERS:
                raise Problem(f"{what} does not filter the others: only a chart or a table does", f"{at}.filters_others", item_id)
            if not isinstance(tile["filters_others"], bool):
                raise Problem(f"{what}'s filters_others is true or false", f"{at}.filters_others", item_id)


def check(definition: Any) -> dict:
    """The definition as it will be saved, or Problem saying what is wrong and where."""
    if not isinstance(definition, dict):
        raise Problem("The dashboard's definition is an object")
    _keys(definition, "", ("v", "locale", "timezone", "refresh_seconds", "page", "parameters", "datasets", "tiles"), ("v",),
          what="The dashboard")
    if definition.get("v") != 1:
        raise Problem("The dashboard's definition is version 1 (v: 1)", "v")
    if "refresh_seconds" in definition:
        _refresh(definition["refresh_seconds"])
    if definition.get("page") is not None:
        report_definition._page(definition["page"])  # its documents': a tile has its place on the grid
    _at_most(definition.get("parameters"), MAX_PARAMETERS, "parameters", "parameters")
    _at_most(definition.get("datasets"), MAX_DATASETS, "datasets", "datasets")
    tiles = _at_most(definition.get("tiles"), MAX_TILES, "tiles", "tiles")
    _tiles(tiles)
    shaped = as_report(definition)
    try:
        report_definition.check(shaped)
    except Problem as p:
        raise _said(p) from None
    # an image as it is kept: its data URL in one piece, as the check wrote it
    for tile, item in zip(tiles, shaped["sections"]["body"]["items"]):
        if tile["type"] == "image":
            tile["asset"] = item["asset"]
    return definition


async def review(session: Session, user: User, definition: Any, current: Optional[dict] = None) -> reports.Review:
    """Every problem of a definition, as for a report (reports.review): the gateway's (the first
    one only, each check stops there) and the engine's (the expressions of the tiles and the
    datasets against the fields, all of them). What a save refuses and what POST /dashboards/check
    says."""
    found: list[dict] = []
    checked, where = None, None
    try:
        checked = check(definition)
        reports.check_references(session, user, checked, current)
    except Problem as p:
        said = _said(p)
        found.append(_problem_dict(said, definition))
        checked, where = None, said.where
    if isinstance(definition, dict):
        columns = reports.known_columns(session, user, checked) if checked is not None else None
        seen = {(f["item"], f["field"]) for f in found}
        for e in await reports.engine_problems(as_report(definition), columns):
            if (e["item"], e["field"]) not in seen:
                seen.add((e["item"], e["field"]))
                found.append({**e, "message": _in_our_words(e["message"])})
    return reports.Review(checked if not found else None, found, where)


async def checked(session: Session, user: User, definition: Any, current: Optional[dict] = None) -> dict:
    """The definition to save, or a 422 saying every problem and where."""
    looked = await review(session, user, definition, current)
    if looked.problems:
        raise looked.refusal()
    return looked.definition  # type: ignore[return-value]


def check_parameters(definition: Any) -> dict:
    """The parameters of a definition being edited, which is what its form needs (the rest may
    be half done), written as a report's."""
    try:
        if isinstance(definition, dict):
            _at_most(definition.get("parameters"), MAX_PARAMETERS, "parameters", "parameters")
        return report_definition.check_parameters(definition)
    except Problem as p:
        raise _said(p) from None


def check_dataset(definition: Any, name: str) -> dict:
    """Its parameters and one dataset, which is what reading that dataset's choices needs."""
    try:
        return report_definition.check_dataset(definition, name)
    except Problem as p:
        raise _said(p) from None


def in_our_words(e: HTTPException) -> HTTPException:
    """A report's refusal, about a dashboard."""
    if isinstance(e.detail, dict) and "message" in e.detail:
        return HTTPException(status_code=e.status_code, detail={**e.detail, "message": _in_our_words(str(e.detail["message"]))})
    return HTTPException(status_code=e.status_code, detail=_in_our_words(str(e.detail))) if isinstance(e.detail, str) else e


# ── reading the numbers ──────────────────────────────────────────────────────
TILES_SECONDS = 300  # the engine stops first (PREVIEW_TIMEOUT_SECONDS): 504


@dataclass
class Asked:
    """What a request for numbers asks: the parameters' values as given, the tiles (None: every
    one), a table's page by tile, the on-screen view (a column sorted, groups toggled), the
    clicks (each {"tile", "on", "values"}), and whether to read the data again rather than take
    what is kept."""
    given: dict[str, list] = field(default_factory=dict)
    tiles: Optional[list[str]] = None
    pages: dict[str, int] = field(default_factory=dict)
    state: Optional[dict] = None
    clicks: list = field(default_factory=list)
    refresh: bool = False


def _levels_of(tile: dict, on: str) -> int:
    """How many values a click on this tile can name: its category and the `split_by`; a table's
    groups, or a matrix's levels of columns. None: it cannot filter."""
    if tile.get("filters_others") is False:
        return 0
    if tile.get("type") == "chart":
        return 0 if on != "rows" or not tile.get("category") or tile.get("kind") == "gauge" else (2 if tile.get("split_by") else 1)
    if tile.get("type") != "table":
        return 0
    if on == "rows":
        return len(tile.get("groups") or [])
    levels = (tile.get("matrix") or {}).get("columns_by")
    return len(levels) if isinstance(levels, list) else 1 if levels else 0


def _click_value(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) <= MAX_CLICK_TEXT
    if isinstance(value, float):
        return math.isfinite(value)  # Python's JSON reads NaN, and no key is one
    return value is None or isinstance(value, (bool, int))


def _checked_clicks(definition: dict, clicks: list) -> list[dict]:
    """The clicks as the engine takes them, or 422. The browser names a tile and values, and never
    an expression: a key it does not know is refused, not left out, and what a value chooses on is
    the saved definition's."""
    tiles = {t.get("id"): t for t in definition.get("tiles") or [] if isinstance(t, dict)}
    seen, out = set(), []
    for click in clicks:
        if not isinstance(click, dict) or not set(click) <= {"tile", "on", "values"}:
            raise HTTPException(status_code=422, detail="A click is a tile and its values, and nothing else")
        tile_id, on, values = click.get("tile"), click.get("on") or "rows", click.get("values")
        if not isinstance(tile_id, str) or tile_id not in tiles:
            raise HTTPException(status_code=422, detail=f"The dashboard has no tile «{str(tile_id)[:64]}»")
        if on not in ("rows", "columns"):
            raise HTTPException(status_code=422, detail=f"The tile «{tile_id}»: a click is on its rows or on its columns")
        most = _levels_of(tiles[tile_id], on)
        if not most:
            raise HTTPException(status_code=422, detail=f"The tile «{tile_id}» cannot filter the others")
        if (tile_id, on) in seen:
            raise HTTPException(status_code=422, detail=f"The tile «{tile_id}» has two clicks: send its values together")
        seen.add((tile_id, on))
        if not isinstance(values, list) or not values:
            raise HTTPException(status_code=422, detail=f"The click on «{tile_id}» needs its values")
        if len(values) > MAX_CLICK_VALUES:
            raise HTTPException(status_code=422, detail=f"The click on «{tile_id}» has {len(values)} values: at most {MAX_CLICK_VALUES}")
        for value in values:
            path = value if isinstance(value, list) else [value]
            if not 1 <= len(path) <= most or not all(_click_value(v) for v in path):
                raise HTTPException(status_code=422, detail=f"The click on «{tile_id}»: a value is a text of at most {MAX_CLICK_TEXT} "
                                                            f"characters, a number, true, false or null, or a list of at most {most} of them")
        out.append({"tile": tile_id, "on": on, "values": values})
    return out


def check_asked(definition: dict, asked: Asked) -> None:
    """422 for what a browser may not ask: a tile the dashboard does not have, a page that is
    none, a view too large, a click on what cannot filter or with too many values."""
    asked.clicks = _checked_clicks(definition, asked.clicks)
    known = {t.get("id") for t in definition.get("tiles") or [] if isinstance(t, dict)}
    for tile_id in [*(asked.tiles or []), *asked.pages]:
        if tile_id not in known:
            raise HTTPException(status_code=422, detail=f"The dashboard has no tile «{str(tile_id)[:64]}»")
    for tile_id, page in asked.pages.items():
        if page < 0:
            raise HTTPException(status_code=422, detail=f"The tile «{tile_id}»: a page is 0 or more")
    reports.check_state(asked.state)


@dataclass
class _Plan:
    """A request, its rights and values checked: the dashboard written as a report's (without
    its images), the parameters' values, the datasets the reader may read, and what stopped
    the others."""
    shaped: dict
    params: dict[str, Any]
    datasets: dict[str, dict]
    refused: dict[str, dict]


async def _plan(session: Session, user: User, definition: dict, asked: Asked, cache: Optional[dict] = None) -> _Plan:
    """With `user`'s rights, whoever asks and whatever they asked before: a value that does not
    fit is a 400 naming its parameter, as for a report; a dataset they may not read is only
    its tiles' refusal, and one that holds the choices of a parameter checks nothing."""
    shaped = as_report(definition)
    try:
        values = await report_params.values_for(shaped, asked.given)
        datasets, refused = await reports.resolve_each(session, user, shaped, values)
        await reports.check_dataset_choices(session, user, shaped, values, cache)
    except HTTPException as e:
        raise in_our_words(e) from None
    return _Plan(report_definition.without_images(shaped), {k: report_params.to_json(v) for k, v in values.items()},
                 datasets, refused)


async def _computed(plan: _Plan, asked: Asked, title: str, slot: Optional[str] = None, cache: Optional[dict] = None) -> dict:
    """{"tiles": {id: its numbers or {"error", "code"}}, "truncated": {dataset: bool}}, from the
    engine. An error of a tile is the tile's; what comes here as an error is the whole request's:
    a newer one took its place, or the engine could not answer. `cache` or `slot`, not both."""
    resp = await get_engine_client().post("/reports/tiles", timeout=TILES_SECONDS, json={
        "definition": plan.shaped, "datasets": plan.datasets, "refused": plan.refused, "params": plan.params,
        "tiles": asked.tiles, "pages": asked.pages, "state": asked.state, "clicks": asked.clicks, "locale": plan.shaped.get("locale") or "en",
        "title": title[:300], **({"cache": cache} if cache is not None else {"slot": slot})})
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="A newer request for the same tiles took the place of this one")
    if resp.status_code in (404, 405):
        raise HTTPException(status_code=503, detail="The engine cannot read dashboards yet: update it to the gateway's version")
    if resp.status_code >= 400:
        raise sql_scripts._engine_failure(resp, reports.sentence)
    return resp.json()


def _finished(definition: dict, answer: dict) -> dict:
    """What the engine does not need to know: a tile's title and an image's picture."""
    tiles = {t["id"]: t for t in definition.get("tiles") or []}
    for tile_id, out in answer.get("tiles", {}).items():
        if "error" in out:
            continue
        tile = tiles[tile_id]
        out["title"] = tile.get("title") or tile.get("label") or ""
        if out.get("type") == "image" and not out.get("hidden"):
            out["src"] = tile.get("asset") or None
    return answer


def slot_of(user: User, where: str, asked: Asked) -> str:
    """A definition being edited: a newer request of this person for the same tiles knocks this
    one down; a table's page next to the whole dashboard's first opening does not."""
    tiles = hashlib.sha256(json.dumps(sorted(asked.tiles) if asked.tiles is not None else None).encode()).hexdigest()[:16]
    return f"u{user.id}:dashboard:{where}:{tiles}"


async def _refresh_token(dashboard_id: int) -> Optional[str]:
    """A refresh by hand, if the engine grants one (at most every 10 seconds per dashboard, for
    everyone); None means answering with what is kept, without saying it is an error."""
    try:
        resp = await get_engine_client().post("/reports/tiles/refresh", timeout=CHECK, json={"dashboard": dashboard_id})
    except httpx.HTTPError:
        return None
    return resp.json().get("fresh") if resp.status_code == 200 else None


async def cache_of(definition: dict, dashboard_id: int, refresh: bool = False) -> dict:
    """What the engine's cache is told of a saved dashboard."""
    interval = definition.get("refresh_seconds")
    cache: dict[str, Any] = {"refresh_seconds": interval if isinstance(interval, int) else 0}
    if refresh and (fresh := await _refresh_token(dashboard_id)):
        cache["fresh"] = fresh
    return cache


async def numbers(session: Session, user: User, definition: dict, asked: Asked, title: str = "",
                  saved: Optional[int] = None, editing: str = "") -> dict:
    """The numbers of a definition `user` may open, tile by tile. `saved`: the dashboard's id, whose
    numbers are shared, so that no request of anyone knocks down a computation others may be
    waiting for: two tabs of one person, too. Otherwise `editing` names the definition being
    edited, which shares nothing: requests for one do not knock down those for another."""
    from app.routes.proxy import _release_db  # routes → services: late import

    check_asked(definition, asked)
    cache = await cache_of(definition, saved, asked.refresh) if saved is not None else None
    plan = await _plan(session, user, definition, asked, cache)
    slot = slot_of(user, editing, asked) if saved is None else None
    _release_db(session, user)  # nothing holds a database connection while the engine works
    return _finished(definition, await _computed(plan, asked, title, slot, cache))


# ── a document ───────────────────────────────────────────────────────────────
def laid_out(definition: dict) -> dict:
    """What the engine lays a document out from: the dashboard written as a report's, with its
    pictures; each tile's place on the grid and its title; the page chosen, if one was."""
    tiles = [t for t in definition.get("tiles") or [] if isinstance(t, dict)]
    return {"definition": as_report(definition), "page": definition.get("page"),
            "tiles": [{"id": t.get("id"), "title": t.get("title") or "", "grid": t.get("grid")} for t in tiles]}


def read_click(text: str) -> Any:
    """A value of a click as an address writes it (frontend/composables/dashboardClicks.ts): `{none}`
    an empty group, `\\` before a text that would read as something else, JSON for several levels."""
    if text == "{none}":
        return None
    if text.startswith("\\"):
        return text[1:]
    if text.startswith("["):
        try:
            path = json.loads(text)
        except ValueError:
            return text
        if isinstance(path, list) and path and all(v is None or isinstance(v, (str, int, float, bool)) for v in path):
            return path
    return text


def in_the_address(given: dict[str, list]) -> tuple[dict[str, list], list[dict]]:
    """A query string's values, as the viewer's address holds them: the parameters, and the clicks
    (`click.by_area=Nord-Est`, `click.cross.columns=2023`)."""
    params: dict[str, list] = {}
    clicks: list[dict] = []
    for key, values in given.items():
        if not key.startswith("click."):
            params[key] = values
            continue
        name = key[len("click."):]
        on = "columns" if name.endswith(".columns") else "rows"
        clicks.append({"tile": name[:-len(".columns")] if on == "columns" else name, "on": on, "values": [read_click(str(v)) for v in values]})
    return params, clicks


def audit_render(session: Session, user: User, dashboard: Dashboard, request: Optional[Request], outcome: str, detail: dict) -> None:
    audit.record_audit(session, actor=user, action=audit.DASHBOARD_RENDER, outcome=outcome, target_type="dashboard", target_id=dashboard.id,
                       target_label=dashboard.name, detail=detail, request=request)


async def render(session: Session, user: User, dashboard: Dashboard, fmt: str, given: dict[str, list], *, clicks: Optional[list] = None,
                 state: Optional[dict] = None, origin: str = "interface", request: Optional[Request] = None,
                 audit_success: bool = True) -> reports.Started:
    """Starts a document of the current version, read with `user`'s rights as its numbers are (`_plan`):
    a dataset they may not read goes only as what its tiles say, and a click on one of those counts for
    nothing. Its rows are the ones its screen reads, so the numbers too. The file is an export of theirs:
    followed and taken through /tasks/exports. Audited with what it showed; `audit_success=False` leaves
    the success to the caller, who records it once it knows the file came out (the API)."""
    from app.routes.proxy import export_owner  # routes → services: late import

    if fmt not in FORMATS:
        raise HTTPException(status_code=422, detail=f"The format is one of {', '.join(FORMATS)}")
    definition = content_of(session, dashboard)
    asked = Asked(given=given, clicks=clicks or [], state=state)
    detail: dict[str, Any] = {"format": fmt, "from": origin, "parameters": reports.given_detail(given)}
    owner = export_owner(user)
    outcome = "failure"
    try:
        check_asked(definition, asked)
        if asked.clicks:
            detail["clicks"] = [{**c, "values": reports._shown(c["values"])} for c in asked.clicks]
        cache = await cache_of(definition, dashboard.id)
        plan = await _plan(session, user, definition, asked, cache)
        detail["parameters"] = {k: reports._shown(v) for k, v in plan.params.items()}
        name = reports.filename(dashboard, fmt)
        resp = await get_engine_client().post("/reports/dashboard-render", timeout=60, json={
            **laid_out(definition), "datasets": plan.datasets, "refused": plan.refused, "params": plan.params, "clicks": asked.clicks,
            "state": state, "format": fmt, "filename": name, "owner": owner, "locale": definition.get("locale") or "en",
            "title": dashboard.name[:300], "cache": cache})
        if resp.status_code in (404, 405):
            raise HTTPException(status_code=503, detail="The engine cannot make documents of a dashboard yet: update it to the gateway's version")
        if resp.status_code >= 400:
            raise sql_scripts._engine_failure(resp, reports.sentence)
        answer = resp.json()
        outcome = "success"
        return reports.Started(task_id=answer["task_id"], filename=answer.get("filename") or name, detail=detail)
    except HTTPException as e:
        detail["error"] = _in_our_words(reports._text(e))[:500]
        raise
    except httpx.TransportError:  # answered 503 by the gateway's handler (main.py)
        detail["error"] = "The engine is not answering"
        raise
    finally:
        if outcome == "failure" or audit_success:
            audit_render(session, user, dashboard, request, outcome, detail)


# ── who sends it ─────────────────────────────────────────────────────────────
def _cells_sending(group: Any, dashboard_id: int) -> list:
    from app.services.flow_groups import definition_of

    return [d for row in definition_of(group).steps for c in row if c is not None and c.kind == "pushmail" and c.pushmail is not None
            for d in c.pushmail.dashboards if d.dashboardId == dashboard_id]


def groups_using(session: Session, dashboard_id: int) -> list:
    """The flow groups with a Pushmail cell that sends the dashboard."""
    from app.models import FlowGroup

    return [g for g in session.exec(select(FlowGroup).where(FlowGroup.definition.contains('"dashboardId"'))).all()  # type: ignore[union-attr]
            if _cells_sending(g, dashboard_id)]


def given_by(group: Any, dashboard_id: int) -> set[str]:
    """The parameters every Pushmail cell of the group that sends the dashboard gives a value."""
    cells = _cells_sending(group, dashboard_id)
    given = [{k for k, v in d.params.items() if v not in (None, "", [])} for d in cells]
    return set.intersection(*given) if given else set()


def groups_off(session: Session, user: User, dashboard: Dashboard, before: dict, after: dict) -> list[tuple]:
    """The flow groups that send the dashboard, scheduled by someone else: a change that makes it read
    more switches their schedules off, as for a report (routes/reports.py), since a schedule carries
    its author's rights. More is a dataset that reads something else, or a new default of a parameter
    the group's cell gives no value. Its tiles only show what the datasets read."""
    reasons, defaults = reports.widens(before, after), reports.new_defaults(before, after)
    if not reasons and not defaults:
        return []
    affected = []
    for group in groups_using(session, dashboard.id):
        mine = reasons + [f"the default of «{name}» changed" for name in sorted(defaults - given_by(group, dashboard.id))]
        if mine:
            affected.append((group, f"the dashboard '{dashboard.name}' it sends changed: {'; '.join(mine)}"))
    return reports.turn_off_groups(session, user, affected)


def groups_reading(session: Session, kind: str, object_id: int) -> list[tuple[Dashboard, Any]]:
    """The flow groups that send a dashboard one of whose datasets reads the SQL script (`kind`
    sql_script) or the saved view (`view`), with that dashboard."""
    return [(dashboard, group) for dashboard in dashboards_reading(session, kind, object_id)
            for group in groups_using(session, dashboard.id)]


# ── who opened it ────────────────────────────────────────────────────────────
def recent_counts():
    """A subquery: per dashboard opened in the window, the people (`people`) and the openings, a
    person once a day (`opens`). The window is the reports' (services/report_views.py)."""
    return (select(DashboardView.dashboard_id.label("dashboard_id"),
                   func.count(func.distinct(DashboardView.user_id)).label("people"),
                   func.count(DashboardView.id).label("opens"))
            .where(DashboardView.day >= report_views.window_start())
            .group_by(DashboardView.dashboard_id)
            .subquery())


def record_open(session: Session, dashboard: Dashboard, user: User, at: Optional[datetime] = None) -> bool:
    """`user` opened `dashboard` now. True when it is their first opening of the day, which
    counts (services/report_views.py says why, and the window); the latest opening is noted
    either way. Commits."""
    now = at or datetime.now(timezone.utc)
    day = report_views.today(now)
    seen = session.exec(select(DashboardView.id).where(
        DashboardView.dashboard_id == dashboard.id, DashboardView.user_id == user.id, DashboardView.day == day)).first()
    counted = False
    if seen is None:
        session.add(DashboardView(dashboard_id=dashboard.id, user_id=user.id, day=day, at=now))
        try:
            session.flush()
            counted = True
        except IntegrityError:  # the same opening at the same moment, from another tab
            session.rollback()
    values: dict[str, Any] = {"last_viewed_at": now}
    if counted:
        values["views_total"] = Dashboard.views_total + 1  # in the database: two openings at once both count
    session.exec(update(Dashboard).where(Dashboard.id == dashboard.id).values(**values))
    session.commit()
    return counted
