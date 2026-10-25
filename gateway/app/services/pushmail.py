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
from app.schemas.models import FlowGroupCell
from app.services import permissions as perm_service

# an address, roughly: what an SMTP server would take, without a list or a name in it
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def pushmail_errors(session: Session, user: User, cell: FlowGroupCell, readable: set[int] | None = None) -> list[str]:
    """A Pushmail cell, new or changed, or about to run: an SMTP connection this person
    may send through (CONNECT), recipients within the domains it allows, datasources
    they may read. Sending data out of the installation is what the cell does: whoever
    sets it up, and whoever runs it, must be allowed every part of it."""
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
    if not cfg.attachments:
        errors.append(f"pushmail «{name}»: choose at least one datasource")
    ids = [a.datasourceId for a in cfg.attachments]
    if len(set(ids)) != len(ids):
        errors.append(f"pushmail «{name}»: the same datasource twice")
    found = {d.id: d for d in session.exec(select(Datasource).where(Datasource.id.in_(ids))).all()} if ids else {}
    for i in dict.fromkeys(ids):
        ds = found.get(i)
        # not found and not readable read the same: a group does not reveal what exists
        if ds is None or ds.project_id not in readable:
            errors.append(f"pushmail «{name}»: datasource {i} not found")
    return errors
