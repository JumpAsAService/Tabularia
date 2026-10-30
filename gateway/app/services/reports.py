"""Reports: who sees them, what they hold, how a render is prepared.
docs/specs/reports.md; the object in models/report.py, the routes in routes/reports.py,
the definition's rules in services/report_definition.py, the parameters' and their
place in the SQL in services/report_params.py.

Who sees a report: VIEW on its folder (administrators: every one). Whoever cannot see
it gets 404, as if it did not exist; EDIT on the folder changes, moves, renames,
duplicates and deletes it, and restores a version.

A render reads with the authority of whoever asks for it — the person who opens it,
the API token's owner, the flow group's scheduler — never the author's: a shared report
is a shared layout, not shared access. Every dataset is resolved with their rights, and
one they may not read stops the render with its name, said as one that does not exist:

- SQL on the catalog: the parameters filled in (services/report_params.py), then the
  datasources it names resolved among those the reader may read, as a SQL script's
  (sql_scripts.resolve);
- SQL on a connection: CONNECT on the connection's folder; the SQL goes to the database
  with the parameters filled in;
- a datasource or a saved view: VIEW on its folder (on a view's datasource's too); the
  report's filters first, then the view's own filters, computed fields and pivot, as
  the Viewer builds them;
- a SQL script: VIEW on its folder, its current version on its catalog or connection,
  with the report's parameters.

The engine receives each dataset resolved — a chain on the catalog, or a connection and
its SQL — and renders the report as an export (POST /reports/render), followed and
downloaded by its owner through /tasks/exports. Saving a definition also needs the
right to what it points to: nobody points a report at a database they could not query.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import JSON, and_, case, cast, func
from sqlmodel import Session, select

from app.core.engine_client import CHECK, get_engine_client
from app.models import Connection, Datasource, Report, ReportVersion, SavedView, SqlScript, User
from app.models.permission import Capability
from app.services import audit, report_definition, report_params, sql_scripts
from app.services import permissions as perm_service
from app.services.engine_policy import pick_engine
from app.services.permissions import is_admin
from app.services.report_definition import Problem

logger = logging.getLogger(__name__)

FORMATS = ("pdf", "xlsx", "html", "svg")
MAX_CHOICES = 1000
MAX_STATE_BYTES = 64 * 1024
_OPS = {"=": "eq", "<>": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge", "in": "in", "contains": "contains"}
_PARAM_REF = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


# ── who sees what ────────────────────────────────────────────────────────────
def can_see(session: Session, user: User, report: Report) -> bool:
    return is_admin(session, user) or perm_service.has_capability(session, user, report.project_id, Capability.VIEW)


def visible_query(session: Session, user: User, project_ids: Optional[set[int]] = None, ordered: bool = True):
    """A select of the reports this person sees (in `project_ids` when given), by name
    unless not `ordered`, or None when there are none."""
    stmt = select(Report)
    if not is_admin(session, user):
        readable = set(perm_service.readable_project_ids(session, user))
        if project_ids is not None:
            readable &= set(project_ids)
        if not readable:
            return None
        stmt = stmt.where(Report.project_id.in_(readable))  # type: ignore[attr-defined]
    elif project_ids is not None:
        stmt = stmt.where(Report.project_id.in_(project_ids))  # type: ignore[attr-defined]
    return stmt.order_by(Report.name) if ordered else stmt


def get_visible(session: Session, user: User, report_id: int) -> Report:
    """The report, or 404: one that cannot be seen does not exist for this person."""
    report = session.get(Report, report_id)
    if report is None or not can_see(session, user, report):
        raise HTTPException(status_code=404, detail="Report not found")
    return report


def widens(before: dict, after: dict) -> list[str]:
    """What a change makes the report read that it did not: a dataset added, or one whose
    source changed (another SQL, connection, datasource, view, script or filters). The
    layout only shows what the datasets read. A flow group scheduled by someone else does
    not spend its author's rights on it (routes/reports.py switches its schedule off), as
    for SQL scripts."""
    def sources(content: dict) -> dict:
        return {d.get("name"): d.get("source") for d in content.get("datasets") or [] if isinstance(d, dict)}

    b, a = sources(before), sources(after)
    reasons = []
    if added := [n for n in a if n not in b]:
        reasons.append(f"it reads the new datasets {', '.join(map(str, added))}")
    if changed := [n for n in a if n in b and a[n] != b[n]]:
        reasons.append(f"its datasets {', '.join(map(str, changed))} read something else")
    return reasons


def new_defaults(before: dict, after: dict) -> set[str]:
    """The parameters whose default a change replaced: a Pushmail cell that gives them no
    value reads with the new one (`{today-3650}` instead of `{today-30}`)."""
    def defaults(content: dict) -> dict:
        return {p.get("name"): p.get("default") for p in content.get("parameters") or [] if isinstance(p, dict)}

    b, a = defaults(before), defaults(after)
    return {n for n in a if n in b and a[n] != b[n]}


def given_by(group: Any, report_id: int) -> set[str]:
    """The parameters every Pushmail cell of the group that sends the report gives a value."""
    from app.services.flow_groups import definition_of

    given: Optional[set[str]] = None
    for row in definition_of(group).steps:
        for c in row:
            if c is None or c.kind != "pushmail" or c.pushmail is None:
                continue
            for r in c.pushmail.reports:
                if r.reportId == report_id:
                    names = {k for k, v in r.params.items() if v not in (None, "", [])}
                    given = names if given is None else given & names
    return given or set()


def groups_using(session: Session, report_id: int) -> list:
    """The flow groups with a Pushmail cell that sends the report."""
    from app.models import FlowGroup
    from app.services.flow_groups import definition_of

    return [g for g in session.exec(select(FlowGroup).where(FlowGroup.definition.contains('"reportId"'))).all()  # type: ignore[union-attr]
            if any(c is not None and c.kind == "pushmail" and c.pushmail is not None
                   and any(r.reportId == report_id for r in c.pushmail.reports)
                   for row in definition_of(g).steps for c in row)]


# what a dataset's source names: the object's kind → the source's kind and its key
_POINTS_TO = {"sql_script": ("sql_script", "script_id"), "view": ("view", "view_id"),
              "datasource": ("datasource", "datasource_id"), "connection": ("sql", "connection_id")}


def reports_reading(session: Session, kind: str, object_id: int) -> list[Report]:
    """The reports one of whose datasets (in the current version) names the object. A SQL
    on the catalog names its datasources by name: those are not found here."""
    source_kind, key = _POINTS_TO[kind]
    rows = session.exec(select(Report, ReportVersion).join(ReportVersion, and_(
        Report.id == ReportVersion.report_id, Report.current_version == ReportVersion.version,
    )).where(ReportVersion.content.contains(f'"{key}"'))).all()  # type: ignore[union-attr]
    return [report for report, row in rows
            if any(isinstance(d, dict) and (d.get("source") or {}).get("kind") == source_kind
                   and (d.get("source") or {}).get(key) == object_id for d in _parsed(row).get("datasets") or [])]


def not_read_by_reports(session: Session, user: User, kind: str, object_id: int, what: str) -> None:
    """409 when reports read the object about to be deleted, as for the other references in
    use: they would stop rendering. It names the ones this person sees and counts the
    others, whose names are not theirs to know."""
    found = reports_reading(session, kind, object_id)
    if not found:
        return
    seen = sorted(r.name for r in found if can_see(session, user, r))
    others = len(found) - len(seen)
    who = ", ".join(f"«{n}»" for n in seen[:10]) + (" …" if len(seen) > 10 else "")
    if not seen:
        who = f"{others} {'report' if others == 1 else 'reports'} you cannot see"
    elif others:
        who = f"the reports {who} and {others} you cannot see"
    else:
        who = f"the reports {who}"
    raise HTTPException(status_code=409, detail={"message": f"{what} is read by {who}: change them first",
                                                 "flows": [], "groups": [], "reports": seen})


def groups_reading(session: Session, kind: str, object_id: int) -> list[tuple[Report, Any]]:
    """The flow groups that send a report one of whose datasets (its current version) reads
    the SQL script (`kind` sql_script) or the saved view (`view`), with that report:
    whoever changes the script or the view changes what the report reads."""
    return [(report, group) for report in reports_reading(session, kind, object_id)
            for group in groups_using(session, report.id)]


def turn_off_groups(session: Session, user: User, affected: list[tuple[Any, str]]) -> list[tuple]:
    """The schedules of these flow groups (each with the reason) set by someone other than
    `user`, switched off: (id, name, the audit's detail) of each, to record once committed."""
    off = []
    for group, reason in affected:
        if group.run_schedule and group.run_scheduled_by not in (None, user.id):
            detail = {"turned_off": True, "author": group.run_scheduled_by, "reason": reason}
            group.run_schedule = group.run_scheduled_by = group.next_run_at = None
            session.add(group)
            off.append((group.id, group.name, detail))
    return off


def record_groups_off(session: Session, user: User, off: list[tuple], request: Optional[Request]) -> None:
    for group_id, name, detail in off:
        audit.record_audit(session, actor=user, action=audit.FLOW_GROUP_SCHEDULE, target_type="flow_group",
                           target_id=group_id, target_label=name, detail=detail, request=request)


# ── versions ─────────────────────────────────────────────────────────────────
def version_row(session: Session, report: Report, version: Optional[int] = None) -> Optional[ReportVersion]:
    """A version of the report (None: the current one); None when it has no such version."""
    return session.exec(select(ReportVersion).where(
        ReportVersion.report_id == report.id,
        ReportVersion.version == (report.current_version if version is None else version))).first()


def _parsed(row: Optional[ReportVersion]) -> dict:
    if row is None:
        return report_definition.empty()
    try:
        return json.loads(row.content)
    except ValueError:
        return report_definition.empty()


def content_of(session: Session, report: Report, version: Optional[int] = None) -> dict:
    return _parsed(version_row(session, report, version))


def sizes_of(session: Session, report_ids: list[int]) -> dict[int, tuple[int, int]]:
    """{report: (datasets, parameters)} of the current versions, counted by the database:
    a definition holds its images, and a list of reports has no use for them."""
    if not report_ids:
        return {}
    if session.get_bind().dialect.name == "postgresql":
        tree = cast(ReportVersion.content, JSON)

        def length(key: str):
            part = tree.op("->")(key)
            return case((func.json_typeof(part) == "array", func.json_array_length(part)), else_=0)
    else:  # SQLite, the tests'
        def length(key: str):
            return func.coalesce(func.json_array_length(ReportVersion.content, f"$.{key}"), 0)
    rows = session.exec(select(ReportVersion.report_id, length("datasets"), length("parameters")).join(Report, and_(
        Report.id == ReportVersion.report_id, Report.current_version == ReportVersion.version,
    )).where(Report.id.in_(report_ids))).all()  # type: ignore[union-attr]
    return {report_id: (datasets, parameters) for report_id, datasets, parameters in rows}


def current_contents(session: Session, reports: list[Report]) -> dict[int, dict]:
    """The current version's definition of each report, read at once."""
    ids = [r.id for r in reports]
    if not ids:
        return {}
    rows = session.exec(select(ReportVersion).join(Report, and_(
        Report.id == ReportVersion.report_id, Report.current_version == ReportVersion.version,
    )).where(Report.id.in_(ids))).all()  # type: ignore[union-attr]
    by_report = {r.report_id: r for r in rows}
    return {r.id: _parsed(by_report.get(r.id)) for r in reports}


def add_version(session: Session, report: Report, content: dict, note: str, user: User) -> ReportVersion:
    """The next version, which becomes the current one. The report's row stays locked
    until the commit: two saves at once take the next numbers in turn."""
    session.exec(select(Report.id).where(Report.id == report.id).with_for_update())
    last = session.exec(select(ReportVersion.version).where(ReportVersion.report_id == report.id)
                        .order_by(ReportVersion.version.desc())).first() or 0  # type: ignore[attr-defined]
    row = ReportVersion(report_id=report.id, version=last + 1, content=json.dumps(content), note=note,
                        created_by=user.id)
    session.add(row)
    report.current_version = row.version
    session.add(report)
    return row


# ── what a definition points to ──────────────────────────────────────────────
def _connection(session: Session, user: User, connection_id: int) -> Connection:
    """A database connection `user` may query: CONNECT on its folder. Otherwise said as
    one that does not exist."""
    conn = sql_scripts.queryable_connection(session, connection_id)
    if conn is None or not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise _unreadable(f"a database connection ({connection_id}) that does not exist or that you cannot use")
    return conn


def _readable(session: Session, user: User, project_id: int) -> bool:
    return perm_service.has_capability(session, user, project_id, Capability.VIEW)


def _datasource(session: Session, user: User, datasource_id: int) -> Datasource:
    ds = session.get(Datasource, datasource_id)
    if ds is None or not _readable(session, user, ds.project_id):
        raise _unreadable(f"a datasource ({datasource_id}) that does not exist or that you cannot read")
    return ds


def _view(session: Session, user: User, view_id: int) -> tuple[SavedView, Datasource]:
    view = session.get(SavedView, view_id)
    if view is None or not _readable(session, user, view.project_id):
        raise _unreadable(f"a saved view ({view_id}) that does not exist or that you cannot read")
    return view, _datasource(session, user, view.datasource_id)


def _script(session: Session, user: User, script_id: int) -> SqlScript:
    script = session.get(SqlScript, script_id)
    if script is None or not sql_scripts.can_see(session, user, script):
        raise _unreadable(f"a SQL script ({script_id}) that does not exist or that you cannot read")
    return script


def _unreadable(what: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"it reads {what}")


def _columns(ds: Datasource) -> set[str]:
    try:
        return {str(c.get("name")) for c in json.loads(ds.columns or "[]") if isinstance(c, dict)}
    except ValueError:
        return set()


def _sql_of(session: Session, user: Optional[User], src: dict) -> tuple[str, Optional[Connection], Optional[str]]:
    """The SQL of a `sql` or `sql_script` dataset, its connection (None = the catalog) and
    its engine. With `user`, their right to the script and the connection is checked."""
    if src["kind"] == "sql_script":
        script = _script(session, user, src["script_id"]) if user is not None else session.get(SqlScript, src["script_id"])
        content = sql_scripts.content_of(session, script) if script is not None else sql_scripts.empty_content()
        sql, cid, engine = content.get("sql") or "", content.get("connection_id"), content.get("engine")
    else:
        sql, cid, engine = src["sql"], src.get("connection_id"), src.get("engine")
    if cid is None:
        return sql_scripts.strip_comments(sql), None, engine
    conn = _connection(session, user, cid) if user is not None else sql_scripts.queryable_connection(session, cid)
    return sql, conn, None


def _dialect(conn: Optional[Connection], engine: Optional[str]) -> report_params.Dialect:
    return report_params.dialect_of(conn.db_type if conn is not None else engine)


def _filter_params(src: dict) -> set[str]:
    return {m.group(1) for f in src.get("filters") or [] if isinstance(f.get("value"), str)
            and (m := _PARAM_REF.match(f["value"].strip())) and m.group(1) not in report_params.BUILTINS}


def check_references(session: Session, user: User, definition: dict, current: Optional[dict] = None) -> None:
    """What the definition points to, at a save. A dataset whose source is new or changed
    must point to something that exists and that this person may read (CONNECT for a
    connection: nobody points a report at a database they could not query); its filters
    name columns the datasource has. Every SQL uses only the report's parameters, never
    inside quotes, and a dataset that fills a parameter's choices does not use it."""
    before = {d.get("name"): d.get("source") for d in (current or {}).get("datasets") or [] if isinstance(d, dict)}
    params = {p["name"]: p for p in definition.get("parameters") or []}
    fills = {}
    for p in params.values():
        source = (p.get("choices") or {}).get("dataset")
        if source:
            fills.setdefault(source, set()).add(p["name"])
    for i, ds in enumerate(definition.get("datasets") or []):
        src, where = ds["source"], f"datasets[{i}].source"
        changed = before.get(ds["name"]) != src
        try:
            used = _filter_params(src)
            if src["kind"] == "sql" or (src["kind"] == "sql_script" and changed):
                # the report's own SQL at every save (its parameters may have changed); a
                # script's when it is chosen (it is checked again at every render)
                sql, conn, engine = _sql_of(session, user if changed else None, src)
                used |= report_params.check_sql(sql, _dialect(conn, engine), params)[0]
                if changed and conn is None:  # the datasources it names, as a render finds them
                    _resolve_catalog(session, user, sql, engine, params)
            elif changed and src["kind"] in ("datasource", "view"):
                if src["kind"] == "datasource":
                    target = _datasource(session, user, src["datasource_id"])
                else:
                    target = _view(session, user, src["view_id"])[1]
                known = _columns(target)
                for f in src.get("filters") or []:
                    if known and f["column"] not in known:
                        raise HTTPException(status_code=422, detail=f"its filter names the column «{f['column']}», "
                                                                    f"which «{target.name}» does not have")
        except HTTPException as e:
            raise Problem(f"The dataset «{ds['name']}»: {_within(_text(e))}", where) from None
        both = used & fills.get(ds["name"], set())
        if both:
            raise Problem(f"The dataset «{ds['name']}» fills the choices of «{sorted(both)[0]}»: "
                          "it cannot use that parameter", where)


def known_columns(session: Session, user: User, definition: dict) -> dict[str, dict[str, str]]:
    """The columns of the datasets that read a datasource as it is, which the gateway knows
    (those this person may read): what the engine checks the expressions against, instead
    of the `fields` the editor last saw — a column renamed or dropped since is found at
    the save. A view, a SQL, a script: the editor's `fields`."""
    out: dict[str, dict[str, str]] = {}
    for ds in definition.get("datasets") or []:
        src = ds.get("source") or {}
        if src.get("kind") != "datasource":
            continue
        target = session.get(Datasource, src.get("datasource_id"))
        if target is None or not _readable(session, user, target.project_id):
            continue
        try:
            cols = json.loads(target.columns or "[]")
        except ValueError:
            continue
        found = {str(c["name"]): _field_type(str(c.get("dtype") or ""))
                 for c in cols if isinstance(c, dict) and c.get("name")}
        if found:
            out[ds["name"]] = found
    return out


async def engine_problems(definition: Any, columns: Optional[dict] = None) -> list[dict]:
    """The expressions, fields and formats, checked by the engine (POST /reports/check):
    every problem it found, as it says them ({item, field, pos, length, message}). An
    engine that cannot answer — not reachable, or one without reports yet — finds none:
    the render checks them all the same."""
    body: dict[str, Any] = {"definition": report_definition.without_images(definition)}
    if columns:
        body["columns"] = columns
    try:
        resp = await get_engine_client().post("/reports/check", timeout=CHECK, json=body)
    except Exception as e:  # noqa: BLE001 — the render will check
        logger.warning("report not checked by the engine: %s", type(e).__name__)
        return []
    if resp.status_code != 200:
        if resp.status_code not in (404, 405):
            logger.warning("report not checked by the engine: engine answered %s", resp.status_code)
        return []
    try:
        answer = resp.json()
    except ValueError:
        return []
    found = []
    for e in [e for e in answer.get("problems") or answer.get("errors") or [] if isinstance(e, dict)][:100]:
        pos = e.get("pos", e.get("position"))
        found.append({"item": e.get("item") if isinstance(e.get("item"), str) else None,
                      "field": report_definition.by_name(str(e.get("field") or e.get("where") or ""), definition),
                      "pos": pos if isinstance(pos, int) and not isinstance(pos, bool) else None,
                      "length": e.get("length") if isinstance(e.get("length"), int) else 0,
                      "message": sentence(str(e.get("message") or "The report has an error"))[:2000]})
    return found


@dataclass
class Review:
    """A definition looked over: as it will be saved (None when it has problems), and its
    problems as the engine says them, the gateway's first; `where` is the first one's
    place as a refused save names it."""
    definition: Optional[dict]
    problems: list[dict]
    where: Optional[str] = None

    def refusal(self) -> HTTPException:
        return report_definition.refusal(self.problems, self.where)


async def review(session: Session, user: User, definition: Any, current: Optional[dict] = None) -> Review:
    """Every problem of a definition: the gateway's (its structure, then what it points to,
    which must be readable by `user`; the first one only, each check stops there) and the
    engine's (its expressions against the datasets' fields, all of them), a place said
    once. What a save refuses, what the editor's preview refuses, what the editor shows
    while one types (POST /reports/check)."""
    found: list[dict] = []
    checked, where = None, None
    try:
        checked = report_definition.check(definition)
        check_references(session, user, checked, current)
    except Problem as p:
        found.append(p.as_dict(definition))
        checked, where = None, p.where
    columns = known_columns(session, user, checked) if checked is not None else None
    seen = {(f["item"], f["field"]) for f in found}
    for e in await engine_problems(definition, columns):
        if (e["item"], e["field"]) not in seen:
            seen.add((e["item"], e["field"]))
            found.append(e)
    return Review(checked if not found else None, found, where)


async def checked(session: Session, user: User, definition: Any, current: Optional[dict] = None) -> dict:
    """The definition to save, or a 422 saying every problem and where."""
    looked = await review(session, user, definition, current)
    if looked.problems:
        raise looked.refusal()
    return looked.definition  # type: ignore[return-value]


# ── resolving the datasets with the reader's rights ──────────────────────────
def _text(e: HTTPException) -> str:
    d = e.detail
    return str(d.get("message") if isinstance(d, dict) else d)


def _within(message: str) -> str:
    """A message put after «The dataset «x»: »: its first letter in lower case."""
    return message[:1].lower() + message[1:] if message[1:2].islower() else message


def sentence(message: str) -> str:
    """A message of the engine's as the gateway says its own, alone: its first letter in
    capitals («there is no field…» → «There is no field…»)."""
    return message[:1].upper() + message[1:] if message[:1].islower() else message


def _in_dataset(name: str, e: HTTPException) -> HTTPException:
    if isinstance(e.detail, dict) and "parameter" in e.detail:
        return e  # a value that does not fit: the parameter's name is the point
    status = e.status_code if e.status_code in (400, 502, 504) else 422
    return HTTPException(status_code=status, detail=f"The dataset «{name}»: {_within(_text(e))}")


def _view_value(value: Any, numeric: bool) -> Any:
    """A filter's value as the Viewer sends it (pages/viewer.vue parseVal): a comma makes
    a list, a number column takes numbers."""
    if value in ("", None) or not isinstance(value, str):
        return value
    if "," in value:
        return [s.strip() for s in value.split(",")]
    if numeric:
        try:
            n = float(value)
            return int(n) if n.is_integer() and re.fullmatch(r"\s*[+-]?\d+\s*", value) else n
        except ValueError:
            return value
    return value


def view_operations(spec: dict, ds: Datasource) -> list[dict]:
    """The operations of a saved view, as the Viewer builds them from its configuration
    (pages/viewer.vue buildTableOps): filters, computed fields, pivot."""
    try:
        dtypes = {str(c.get("name")): str(c.get("dtype") or "") for c in json.loads(ds.columns or "[]")
                  if isinstance(c, dict)}
    except ValueError:
        dtypes = {}
    ops: list[dict] = []
    for f in spec.get("filters") or []:
        if not isinstance(f, dict) or not f.get("column") or not f.get("operator"):
            continue
        numeric = bool(re.search(r"Int|Float|Decimal", dtypes.get(f["column"], ""), re.I))
        params: dict[str, Any] = {"column": f["column"], "operator": f["operator"]}
        if f["operator"] == "between":
            if f.get("value", "") == "" or f.get("value2", "") == "":
                continue
            params["value"] = [_view_value(f["value"], numeric), _view_value(f["value2"], numeric)]
        elif f["operator"] not in ("is_null", "is_not_null"):
            params["value"] = _view_value(f.get("value", ""), numeric)
        ops.append({"type": "filter", "params": params})
    computed = [{"name": c["name"].strip(), "expr": c["expr"].strip()} for c in spec.get("computedFields") or []
                if isinstance(c, dict) and str(c.get("name") or "").strip() and str(c.get("expr") or "").strip()]
    if computed:
        ops.append({"type": "compute", "params": {"columns": computed}})
    pivot = spec.get("pivot") or {}
    index = [c for c in pivot.get("index") or [] if c]
    on = [c for c in pivot.get("on") or [] if c]
    if spec.get("pivotOn") and on and pivot.get("values") and index:
        ops.append({"type": "pivot", "params": {"index": index, "on": on, "values": pivot["values"],
                                                "func": pivot.get("func") or "sum"}})
    return ops


@dataclass
class _Plan:
    """A dataset on its way to the engine: what it reads, before the parameters are put in."""
    name: str
    kind: str  # "sql" (on the catalog), "connection", "chain"
    sql: str = ""
    conn: Optional[Connection] = None
    engine: Optional[str] = None
    chain: dict = field(default_factory=dict)
    datasource_id: Optional[int] = None  # the datasource a chain reads
    filters: list = field(default_factory=list)
    placeholders: set = field(default_factory=set)


class _Engines:
    """The engine of each dataset, asked once per render (pick_engine reads /engines)."""

    def __init__(self, session: Session):
        self.session, self.picked = session, {}

    async def pick(self, requested: Optional[str], fallback: bool = False) -> str:
        """`fallback`: a view saved on an engine no longer allowed reads with the default
        one, as the Viewer would; a SQL written for an engine says it cannot run."""
        if requested not in self.picked:
            try:
                self.picked[requested] = await pick_engine(self.session, requested)
            except HTTPException:
                if requested is None or not fallback:
                    raise
                return await self.pick(None)
        return self.picked[requested]


async def _plan(session: Session, user: User, ds: dict, params: dict, engines: _Engines) -> _Plan:
    src, name = ds["source"], ds["name"]
    if src["kind"] in ("sql", "sql_script"):
        sql, conn, engine = _sql_of(session, user, src)
        if conn is None:
            engine = await engines.pick(engine)
        plan = _Plan(name, "connection" if conn is not None else "sql", sql=sql, conn=conn, engine=engine)
        plan.placeholders = report_params.check_sql(sql, _dialect(conn, engine), params)[1]
        return plan
    if src["kind"] == "datasource":
        target, ops, engine = _datasource(session, user, src["datasource_id"]), [], None
    else:
        view, target = _view(session, user, src["view_id"])
        try:
            spec = json.loads(view.spec or "{}")
        except ValueError:
            spec = {}
        ops, engine = view_operations(spec if isinstance(spec, dict) else {}, target), (spec or {}).get("engine")
    if not target.key:
        raise HTTPException(status_code=422, detail=f"the datasource «{target.name}» has no data yet")
    plan = _Plan(name, "chain", engine=await engines.pick(engine, fallback=True), filters=list(src.get("filters") or []))
    plan.chain = {"bucket": target.bucket, "input_key": target.key, "operations": ops}
    plan.datasource_id = target.id
    plan.placeholders = {b for f in plan.filters if (b := report_params.placeholder_of(f.get("value")))}
    return plan


def _filter_ops(plan: _Plan, params: dict, values: Mapping[str, Any], placeholders: Mapping[str, str]) -> list[dict]:
    """The report's filters of a datasource or a view, as the engine's: a parameter
    without a value leaves its filter out (an optional parameter is an optional filter)."""
    ops = []
    for f in plan.filters:
        value = f.get("value")
        if isinstance(value, str) and (m := _PARAM_REF.match(value.strip())) and m.group(1) in params:
            value = values.get(m.group(1))
            if value is None or value == []:
                continue
            value = report_params.to_json(value)
        elif (body := report_params.placeholder_of(value)) is not None:
            value = placeholders[body]
        ops.append({"type": "filter", "params": {"column": f["column"], "operator": _OPS[f["op"]], "value": value}})
    return ops


def _finish(session: Session, user: User, plan: _Plan, params: dict, values: Mapping[str, Any],
            placeholders: Mapping[str, str], read: Optional[set[int]] = None) -> dict:
    """The dataset as the engine reads it; the datasources it reads go in `read`."""
    read = set() if read is None else read
    if plan.kind == "connection":
        from app.routes.connections import engine_connection_payload  # routes → services: late import

        sql = report_params.fill(plan.sql, _dialect(plan.conn, None), params, values, placeholders)
        return {"connection": engine_connection_payload(plan.conn), "sql": sql}
    if plan.kind == "sql":
        sql = report_params.fill(plan.sql, _dialect(None, plan.engine), params, values, placeholders)
        resolved = sql_scripts.resolve(session, user, sql, plan.engine)
        if not resolved.tables:
            raise HTTPException(status_code=422, detail="the query reads no datasource: name one in FROM")
        read.update(ds.id for ds in resolved.tables.values())
        return {**sql_scripts._catalog_chain(resolved), "engine": plan.engine}
    read.add(plan.datasource_id)
    chain = dict(plan.chain)
    chain["operations"] = _filter_ops(plan, params, values, placeholders) + chain["operations"]
    return {**chain, "engine": plan.engine}


def _resolve_catalog(session: Session, user: User, sql: str, engine: Optional[str], params: dict) -> None:
    """A SQL on the catalog read as a render reads it, without asking the engine: every
    parameter a value of its type, every placeholder a date. 422 for a datasource that
    `user` cannot read (or that does not exist), or a SQL that cannot be read."""
    dialect = _dialect(None, engine)
    bodies = report_params.check_sql(sql, dialect, params)[1]
    values = {name: _neutral(p) for name, p in params.items()}
    sql_scripts.resolve(session, user, report_params.fill(sql, dialect, params, values,
                                                          {b: "2000-01-01" for b in bodies}), engine)


def reading_problem(session: Session, user: User, definition: dict) -> Optional[str]:
    """Why `user` could not read every dataset of the definition, or None: the checks of a
    render (`resolve_datasets`) without asking the engine anything — for a flow group's
    Pushmail cell when it is saved and before a scheduled run starts. The parameters take
    a value of their type and the placeholders a date: only who may read what counts
    here, the render checks the rest with the real values."""
    params = {p["name"]: p for p in definition.get("parameters") or []}
    for ds in definition.get("datasets") or []:
        src = ds["source"]
        try:
            if src["kind"] in ("sql", "sql_script"):
                sql, conn, engine = _sql_of(session, user, src)
                if conn is None:
                    _resolve_catalog(session, user, sql, engine, params)
            elif src["kind"] == "datasource":
                _datasource(session, user, src["datasource_id"])
            else:
                _view(session, user, src["view_id"])
        except HTTPException as e:
            return f"The dataset «{ds['name']}»: {_within(_text(e))}"
    return None


async def resolve_datasets(session: Session, user: User, definition: dict, values: Mapping[str, Any],
                           only: Optional[set[str]] = None, read: Optional[set[int]] = None) -> dict[str, dict]:
    """Every dataset (or those in `only`) as the engine reads it, with `user`'s rights and
    the parameters' `values` in place. 422 naming the dataset that cannot be read. The
    datasources read go in `read`, when given (a flow group's lineage)."""
    params = {p["name"]: p for p in definition.get("parameters") or []}
    engines = _Engines(session)
    plans: list[_Plan] = []
    for ds in definition.get("datasets") or []:
        if only is not None and ds["name"] not in only:
            continue
        try:
            plans.append(await _plan(session, user, ds, params, engines))
        except HTTPException as e:
            raise _in_dataset(ds["name"], e) from None
    bodies = {b for p in plans for b in p.placeholders}
    resolved = await report_params.resolve_placeholders(bodies, definition.get("timezone") or "UTC",
                                                        report_params.lang_of(definition)) if bodies else {}
    texts = {}
    for body, (text, error) in resolved.items():
        if error:
            where = next(p.name for p in plans if body in p.placeholders)
            raise HTTPException(status_code=422, detail=f"The dataset «{where}»: {{{body}}}: {error}")
        texts[body] = text
    out = {}
    for plan in plans:
        try:
            out[plan.name] = _finish(session, user, plan, params, values, texts, read)
        except HTTPException as e:
            raise _in_dataset(plan.name, e) from None
    return out


# ── choices taken from a dataset ─────────────────────────────────────────────
async def dataset_choices(definition: dict, datasets: Mapping[str, dict], values: Mapping[str, Any],
                          param: dict) -> Optional[dict]:
    """The choices of a parameter that takes them from a dataset, read by the engine
    (POST /reports/choices): {"choices": [{value, label}], "truncated"}. None when the
    engine has no such route yet."""
    choices = param["choices"]
    resp = await get_engine_client().post("/reports/choices", timeout=300, json={
        "definition": report_definition.without_images(definition), "dataset": choices["dataset"],
        "datasets": {choices["dataset"]: datasets[choices["dataset"]]},
        "params": {k: report_params.to_json(v) for k, v in values.items()},
        "value": choices["value"], "label": choices.get("label"), "limit": MAX_CHOICES,
        "locale": definition.get("locale") or "en"})
    if resp.status_code in (404, 405):
        return None
    if resp.status_code >= 400:
        raise sql_scripts._engine_failure(resp, lambda d: f"The choices of «{param['name']}»: {_within(d)}")
    answer = resp.json()
    return {"choices": [c for c in answer.get("choices") or [] if isinstance(c, dict)][:MAX_CHOICES],
            "truncated": bool(answer.get("truncated"))}


async def check_dataset_choices(definition: dict, datasets: Mapping[str, dict], values: Mapping[str, Any]) -> None:
    """A value of a parameter whose choices come from a dataset must be one of them (when
    the engine can say, and the list is whole)."""
    for p in definition.get("parameters") or []:
        source = (p.get("choices") or {}).get("dataset")
        value = values.get(p["name"])
        if not source or value is None or value == [] or source not in datasets:
            continue
        got = await dataset_choices(definition, datasets, values, p)
        if got is None or got["truncated"]:
            continue
        allowed = set()
        for c in got["choices"]:
            try:
                allowed.add(report_params._key(report_params.convert(p, c.get("value"))))
            except HTTPException:
                continue
        for v in value if isinstance(value, list) else [value]:
            if report_params._key(v) not in allowed:
                raise report_params.bad_value(p["name"], f"'{report_params.to_json(v)}' is not one of its choices")


# ── a render ─────────────────────────────────────────────────────────────────
def filename(report: Optional[Report], fmt: str) -> str:
    """The file's name: the report's, without what a file name cannot hold."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", report.name if report is not None else "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return f"{name or 'report'}.{fmt}"


def given_params(params: Mapping[str, Any]) -> dict[str, list]:
    """Values as they arrive in a body: one or a list per name (null: none given)."""
    out: dict[str, list] = {}
    for name, value in (params or {}).items():
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        if any(isinstance(v, (dict, list)) for v in values):
            raise report_params.bad_value(str(name), "a value is a text, a number or true/false")
        out[str(name)] = values
    return out


def _shown(value: Any) -> Any:
    """A value as the audit keeps it: a long text cut, a long list its first values."""
    if isinstance(value, list):
        return [_shown(v) for v in value[:50]] + (["…"] if len(value) > 50 else [])
    return value[:200] if isinstance(value, str) else value


@dataclass
class Started:
    task_id: str
    filename: str
    detail: dict  # what the audit records


def given_detail(given: Mapping[str, list]) -> dict:
    """The values as given, a few and cut short: the audit of a render refused before they
    were converted."""
    return {str(k)[:64]: [str(v)[:200] for v in vs[:20]] for k, vs in list(given.items())[:40]}


async def prepare(session: Session, user: User, definition: dict, given: Mapping[str, list],
                  detail: Optional[dict] = None, read: Optional[set[int]] = None) -> tuple[dict, dict]:
    """What a render needs before the engine: the parameters' values (400 naming the one
    that does not fit) and every dataset resolved with `user`'s rights (422 naming the one
    that cannot be read), the values of choices read from a dataset among them. The
    converted values go in the audit's `detail`, the datasources read in `read`."""
    values = await report_params.values_for(definition, given)
    if detail is not None:
        detail["parameters"] = {k: _shown(report_params.to_json(v)) for k, v in values.items()}
    datasets = await resolve_datasets(session, user, definition, values, read=read)
    await check_dataset_choices(definition, datasets, values)
    return values, datasets


def _audit_render(session: Session, user: User, report: Optional[Report], request: Optional[Request], outcome: str,
                  detail: dict) -> None:
    audit.record_audit(session, actor=user, action=audit.REPORT_RENDER, outcome=outcome, target_type="report",
                       target_id=report.id if report is not None else None,
                       target_label=report.name if report is not None else "", detail=detail, request=request)


async def render(session: Session, user: User, report: Optional[Report], definition: dict, fmt: str,
                 given: Mapping[str, list], *, state: Optional[dict] = None, origin: str = "interface",
                 request: Optional[Request] = None, audit_success: bool = True,
                 extra: Optional[dict] = None) -> Started:
    """Starts the render of `definition` (a saved report's, or the editor's unsaved one)
    with the parameters `given`, read with `user`'s rights. The file is an export of
    theirs: followed and taken through /tasks/exports. Audited with its format, its
    parameters and where it came from (`origin`: interface, editor, api, notebook; a flow
    group's Pushmail cell renders through the engine's email task, see `prepare`), and
    `extra` (the notebook that asked); `audit_success=False` leaves the success to the
    caller, who records it once it knows the file came out (the API, a notebook)."""
    from app.routes.proxy import export_owner  # routes → services: late import

    if fmt not in FORMATS:
        raise HTTPException(status_code=422, detail=f"The format is one of {', '.join(FORMATS)}")
    if state is not None and len(json.dumps(state)) > MAX_STATE_BYTES:
        raise HTTPException(status_code=422, detail="The view's state is too large")
    if state is not None and not (isinstance(state.get("items", {}), dict)
                                  and all(isinstance(i, dict) for i in state.get("items", {}).values())):
        raise HTTPException(status_code=422, detail="The view's state has its items as an object by item's id")
    # as given until they are converted: a few, cut short, in the audit of a refusal
    detail: dict[str, Any] = {"format": fmt, "from": origin, **(extra or {}), "parameters": given_detail(given)}
    outcome = "failure"
    try:
        values, datasets = await prepare(session, user, definition, given, detail)
        name = filename(report, fmt)
        resp = await get_engine_client().post("/reports/render", timeout=60, json={
            "definition": definition, "datasets": datasets,
            "params": {k: report_params.to_json(v) for k, v in values.items()}, "format": fmt, "filename": name,
            "owner": export_owner(user), "locale": definition.get("locale") or "en", "state": state or {},
            # REPORT() and the PDF's title: the name as written, not as a file may hold it
            **({"title": report.name} if report is not None else {})})
        if resp.status_code >= 400:
            raise sql_scripts._engine_failure(resp, sentence)
        answer = resp.json()
        outcome = "success"
        return Started(task_id=answer["task_id"], filename=answer.get("filename") or name, detail=detail)
    except HTTPException as e:
        detail["error"] = _text(e)[:500]
        raise
    except httpx.TransportError:  # answered 503 by the gateway's handler (main.py)
        detail["error"] = "The engine is not answering"
        raise
    finally:
        if outcome == "failure" or audit_success:
            _audit_render(session, user, report, request, outcome, detail)


# ── a dataset's fields, for the editor ───────────────────────────────────────
def _field_type(dtype: str) -> str:
    d = (dtype or "").lower()
    for prefix, kind in (("int", "integer"), ("uint", "integer"), ("float", "float"), ("decimal", "float"),
                         ("double", "float"), ("string", "string"), ("utf8", "string"), ("str", "string"),
                         ("categorical", "string"), ("enum", "string"), ("datetime", "datetime"),
                         ("timestamp", "datetime"), ("date", "date"), ("time", "time"), ("bool", "boolean")):
        if d.startswith(prefix):
            return kind
    return "other"


def _neutral(param: dict) -> Any:
    """A value of the parameter's type, for a dataset read only for its columns."""
    value = {"text": "", "number": 0, "date": date.today(), "boolean": False}[param["type"]]
    return [value] if param.get("multi") else value


async def fields(session: Session, user: User, definition: dict, dataset: str, slot: str) -> list[dict]:
    """The columns of a dataset (name and type), read with `user`'s rights: the
    parameters take their defaults, or a neutral value of their type."""
    ds = next((d for d in definition.get("datasets") or [] if d.get("name") == dataset), None)
    if ds is None:
        raise HTTPException(status_code=404, detail=f"The report has no dataset «{dataset}»")
    values: dict[str, Any] = {}
    for p in definition.get("parameters") or []:
        try:
            value = (await report_params.values_for({**definition, "parameters": [p]}, {}))[p["name"]]
        except HTTPException:
            value = None
        values[p["name"]] = _neutral(p) if value in (None, []) else value
    resolved = (await resolve_datasets(session, user, definition, values, only={dataset}))[dataset]
    src = ds["source"]
    if src["kind"] == "datasource":
        target = session.get(Datasource, src["datasource_id"])
        try:
            cols = json.loads(target.columns or "[]") if target is not None else []
        except ValueError:
            cols = []
        if cols:
            return [{"name": str(c.get("name")), "type": _field_type(str(c.get("dtype") or ""))}
                    for c in cols if isinstance(c, dict)]
    if "connection" in resolved:
        path, body = "/db/query", {**resolved, "limit": 1, "slot": slot}
    else:
        path, body = "/tasks/preview", {**resolved, "limit": 1, "no_cache": True, "slot": slot}
    try:
        resp = await get_engine_client().post(path, json=body, timeout=300)
    except Exception as e:  # noqa: BLE001 — said in words
        raise HTTPException(status_code=502, detail=f"The engine is not answering: {type(e).__name__}") from None
    if resp.status_code >= 400:
        raise _in_dataset(dataset, sql_scripts._engine_failure(resp, lambda d: d))
    return [{"name": str(c.get("name")), "type": _field_type(str(c.get("dtype") or ""))}
            for c in resp.json().get("columns") or [] if isinstance(c, dict)]


# ── a dataset's rows, counted for the editor ─────────────────────────────────
COUNT_SECONDS = 20  # the engine counts this long at most: «Refresh fields» does not wait for more


async def to_count(session: Session, user: User, definition: dict, dataset: str) -> Optional[dict]:
    """The dataset as a render with the parameters' defaults reads it — what the viewer
    shows when the report opens — resolved with `user`'s rights, to be counted
    (`count_rows`). None when the count could not mean anything or the dataset cannot be
    read (the fields say why): a required parameter it uses has no default."""
    ds = next((d for d in definition.get("datasets") or [] if d.get("name") == dataset), None)
    if ds is None:
        return None
    params = {p["name"]: p for p in definition.get("parameters") or []}
    values: dict[str, Any] = {}
    missing: set[str] = set()
    for p in params.values():
        try:
            values[p["name"]] = (await report_params.values_for({**definition, "parameters": [p]}, {}))[p["name"]]
        except HTTPException:  # required without a default, or a default that does not fit
            values[p["name"]] = [] if p.get("multi") else None
            missing.add(p["name"])
    try:
        if missing:
            src = ds["source"]
            if src["kind"] in ("sql", "sql_script"):
                sql, conn, engine = _sql_of(session, user, src)
                used = report_params.check_sql(sql, _dialect(conn, engine), params)[0]
            else:
                used = _filter_params(src)
            if used & missing:
                return None
        return (await resolve_datasets(session, user, definition, values, only={dataset}))[dataset]
    except HTTPException:
        return None


async def count_rows(resolved: Optional[dict], slot: str) -> Optional[dict]:
    """{"rows", "over", "max_rows"}: the rows of a dataset `to_count` resolved, counted by
    the engine up to the cap a render reads (POST /reports/count) within COUNT_SECONDS.
    None when they could not be counted — a failure, too long, an engine without the
    route: a hint next to the fields, never an error."""
    if resolved is None:
        return None
    try:
        resp = await get_engine_client().post("/reports/count", timeout=COUNT_SECONDS + 10, json={
            "dataset": resolved, "slot": slot, "seconds": COUNT_SECONDS})
    except Exception as e:  # noqa: BLE001 — the fields come without the count
        logger.info("report: the rows were not counted: %s", type(e).__name__)
        return None
    if resp.status_code >= 400:
        logger.info("report: the rows were not counted: the engine answered %s", resp.status_code)
        return None
    try:
        got = resp.json()
        return {"rows": int(got["rows"]), "over": bool(got["over"]), "max_rows": int(got["max_rows"])}
    except (ValueError, KeyError, TypeError):
        return None
