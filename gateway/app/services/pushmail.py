"""The Pushmail cell of a flow group (docs/pushmail.md): what makes one valid for a
person. Checked when the group is saved (routes/flow_groups.py), before a scheduled
group starts (schedule_authority.gaps_of_group) and when the cell runs
(flow_group_runner._pushmail_cell), each time with the rights of whoever acts."""
from __future__ import annotations

import re

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import Datasource, User
from app.models.permission import Capability
from app.schemas.models import PUSHMAIL_MAX_REPORT_PARAMS, FlowGroupCell, PushmailCell
from app.services import permissions as perm_service

# an address, roughly: what an SMTP server would take, without a list or a name in it
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def email_language(user: User) -> str:
    """The language a Pushmail email is written in — its note, the names of months and days
    of its placeholders (`{today:MMMM}`): that of whoever runs the group."""
    return user.locale if user.locale in ("en", "it", "fr", "de", "es") else "en"


def pushmail_errors(session: Session, user: User, cell: FlowGroupCell, readable: set[int] | None = None) -> list[str]:
    """A Pushmail cell, new or changed, or about to run: an SMTP connection this person
    may send through (CONNECT), recipients within the domains it allows, datasources
    they may read, reports and dashboards they see and whose data they may read. Sending data out of the
    installation is what the cell does: whoever sets it up, and whoever runs it, must be
    allowed every part of it."""
    from app.models import Connection
    from app.routes.connections import allowed_email_domains

    cfg = cell.pushmail
    if cfg is None:
        return ["pushmail: the cell has no settings"]
    readable = perm_service.readable_project_ids(session, user) if readable is None else readable
    name = cfg.label.strip() or "Pushmail"
    errors: list[str] = []
    conn = session.get(Connection, cfg.connectionId) if cfg.connectionId else None
    # CONNECT is the right to send through it, as for the email of an Output node
    if conn is None or conn.db_type != "smtp":
        errors.append(f"pushmail «{name}»: choose an SMTP connection")
        conn = None
    elif not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        errors.append(f"pushmail «{name}»: sending through «{conn.name}» needs CONNECT on its folder")
        conn = None
    to = [a.strip() for a in cfg.to if a.strip()]
    everyone = to + [a.strip() for a in cfg.cc if a.strip()]
    if not to:
        errors.append(f"pushmail «{name}»: add at least one recipient")
    bad = [a for a in everyone if not _EMAIL.match(a)]
    if bad:
        errors.append(f"pushmail «{name}»: not an email address: {', '.join(bad[:5])}")
    if conn is not None and everyone:
        try:
            allowed = allowed_email_domains(conn)
        except HTTPException as e:  # a malformed restriction: nothing leaves
            errors.append(f"pushmail «{name}»: {e.detail}")
            allowed = []
        outside = [a for a in everyone if allowed and a.rsplit("@", 1)[-1].lower() not in allowed]
        if outside:
            errors.append(f"pushmail «{name}»: recipients outside the domains «{conn.name}» allows "
                          f"({', '.join(allowed)}): {', '.join(outside[:5])}")
    if not cfg.attachments and not cfg.reports and not cfg.dashboards:
        errors.append(f"pushmail «{name}»: choose at least one datasource, report or dashboard")
    for n, a in enumerate(cfg.attachments, 1):
        if a.datasourceId is None:
            errors.append(f"pushmail «{name}»: choose the datasource of file {n}")
        if not a.fileName.strip():
            errors.append(f"pushmail «{name}»: give file {n} a name")
    ids = [a.datasourceId for a in cfg.attachments if a.datasourceId is not None]
    if len(set(ids)) != len(ids):
        errors.append(f"pushmail «{name}»: the same datasource twice")
    found = {d.id: d for d in session.exec(select(Datasource).where(Datasource.id.in_(ids))).all()} if ids else {}
    for i in dict.fromkeys(ids):
        ds = found.get(i)
        # not found and not readable read the same: a group does not reveal what exists
        if ds is None or ds.project_id not in readable:
            errors.append(f"pushmail «{name}»: datasource {i} not found")
    errors.extend(report_errors(session, user, cfg, name))
    errors.extend(dashboard_errors(session, user, cfg, name))
    return errors


def report_errors(session: Session, user: User, cfg: PushmailCell, name: str) -> list[str]:
    """The reports of a cell: each one this person sees (VIEW on its folder), with values
    that fit its parameters and datasets they may all read — the checks of a render
    (services/reports.py), without asking the engine. Rendered when the cell runs, they
    are checked again in full, with the real values."""
    from app.models import Report
    from app.services import report_params, reports

    errors: list[str] = []
    if sum(1 for r in cfg.reports if r.inBody) > 1:
        errors.append(f"pushmail «{name}»: only one report can go in the email's body")
    for n, r in enumerate(cfg.reports, 1):
        if r.reportId is None:
            errors.append(f"pushmail «{name}»: choose the report {n}")
            continue
        if not r.fileName.strip():
            errors.append(f"pushmail «{name}»: give the file of report {n} a name")
        if len(r.params) > PUSHMAIL_MAX_REPORT_PARAMS:
            errors.append(f"pushmail «{name}»: report {n} has too many values")
            continue
        report = session.get(Report, r.reportId)
        # not found and not visible read the same, as for a datasource
        if report is None or not reports.can_see(session, user, report):
            errors.append(f"pushmail «{name}»: report {r.reportId} not found")
            continue
        definition = reports.content_of(session, report)
        try:
            report_params.check_values(definition, reports.given_params(r.params))
        except HTTPException as e:
            errors.append(f"pushmail «{name}»: the report «{report.name}»: {reports._text(e)}")
            continue
        problem = reports.reading_problem(session, user, definition)
        if problem:
            errors.append(f"pushmail «{name}»: the report «{report.name}»: {problem}")
    return errors


def dashboard_errors(session: Session, user: User, cfg: PushmailCell, name: str) -> list[str]:
    """The dashboards of a cell, as `report_errors` for its reports: each one this person sees, with
    values that fit its parameters and datasets they may all read. A dataset they may not read does
    not become a tile that says so in an email nobody looked at: it stops the cell."""
    from app.models import Dashboard
    from app.services import dashboards, report_params, reports

    errors: list[str] = []
    for n, d in enumerate(cfg.dashboards, 1):
        if d.dashboardId is None:
            errors.append(f"pushmail «{name}»: choose the dashboard {n}")
            continue
        if not d.fileName.strip():
            errors.append(f"pushmail «{name}»: give the file of dashboard {n} a name")
        if len(d.params) > PUSHMAIL_MAX_REPORT_PARAMS:
            errors.append(f"pushmail «{name}»: dashboard {n} has too many values")
            continue
        board = session.get(Dashboard, d.dashboardId)
        # not found and not visible read the same, as for a datasource
        if board is None or not dashboards.can_see(session, user, board):
            errors.append(f"pushmail «{name}»: dashboard {d.dashboardId} not found")
            continue
        definition = dashboards.as_report(dashboards.content_of(session, board))
        try:
            report_params.check_values(definition, reports.given_params(d.params))
        except HTTPException as e:
            errors.append(f"pushmail «{name}»: the dashboard «{board.name}»: {dashboards._in_our_words(reports._text(e))}")
            continue
        problem = reports.reading_problem(session, user, definition)
        if problem:
            errors.append(f"pushmail «{name}»: the dashboard «{board.name}»: {dashboards._in_our_words(problem)}")
    return errors
