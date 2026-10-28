"""Unified search by NAME over all the catalog's resources.

`/flows/search`, `/datasources/search` and `/connections/search` exist already, but
each answers for one kind only and two kinds had none at all (folders and saved
views). Whoever looks for «margine» does not know, and must not need to know, whether
what they look for is a flow or a view: this route answers the real question.

**Why not an SQL UNION.** The five kinds live in tables with no columns in common and
— above all — with DIFFERENT permissions: folders use the navigable set
(`visible_project_ids`, which includes the ancestors to show the path), contents use
the readable one (`readable_project_ids`), and connections an ORTHOGONAL capability
(CONNECT: whoever has only VIEW does not see them). A UNION would have to carry three
different filters in one statement. Five filtered queries and a union in Python say
the same thing readably, and it is the choice this code makes elsewhere already (see
services/permissions.py, which resolves the tree in Python on purpose).

**Only the NAME is searched.** It is what is needed, and it is predictable: a result
that shows up for a match in a field the user does not see is a result that looks like
a mistake.
"""
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import Connection, Datasource, Flow, FlowGroup, Project, SavedScript, SavedView, User
from app.schemas.models import SearchHit, SearchOut
from app.services import flow_access
from app.services import permissions as perm_service
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, tags=["search"])

# presentation order at equal relevance: the containers first, then what is opened
# most often. It also makes the sorting deterministic.
_KIND_ORDER = {"folder": 0, "flow": 1, "group": 2, "datasource": 3, "view": 4, "script": 5, "sql": 6, "notebook": 7,
               "report": 8, "connection": 9}


def _like(q: str) -> str:
    """Searched text → LIKE pattern, with the special characters made literal: whoever
    searches `a_b` means `a_b`, not «a, any character, b».

    The escape must be DECLARED at every `ilike(..., escape="\\\\")`: in PostgreSQL the
    backslash is LIKE's default escape already, in SQLite it is not. Without declaring it
    the same search would give different results between the tests (SQLite) and
    production (Postgres), which is the worst kind of divergence: green here, wrong in
    the field.
    """
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@router.get("/search", response_model=SearchOut)
def search_everything(
    q: str = Query("", description="searches by name over all the resources"),
    kind: str | None = Query(None, description="limits to one kind: folder|flow|group|datasource|view|script|sql|notebook|report|connection"),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    term = q.strip()
    if not term:
        return SearchOut(items=[], total=0, counts={})

    pattern = _like(term)
    readable = perm_service.readable_project_ids(session, user)
    navigable = perm_service.visible_project_ids(session, user)
    connectable = perm_service.connectable_project_ids(session, user)

    # the folders' names: needed to say WHERE each result is
    folder_names = dict(session.exec(select(Project.id, Project.name)).all())

    hits: list[SearchHit] = []

    if navigable:
        for p in session.exec(
            select(Project).where(Project.id.in_(navigable), Project.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="folder", id=p.id, name=p.name,
                project_id=p.parent_id, project_name=folder_names.get(p.parent_id),
            ))

    if readable:
        for f in session.exec(
            select(Flow).where(Flow.project_id.in_(readable), Flow.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="flow", id=f.id, name=f.name,
                project_id=f.project_id, project_name=folder_names.get(f.project_id),
                detail=f.engine, locked_reason=flow_access.blocked(session, user, f.definition),
            ))

        for g in session.exec(
            select(FlowGroup).where(FlowGroup.project_id.in_(readable), FlowGroup.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="group", id=g.id, name=g.name,
                project_id=g.project_id, project_name=folder_names.get(g.project_id),
            ))

        for d in session.exec(
            select(Datasource).where(Datasource.project_id.in_(readable), Datasource.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="datasource", id=d.id, name=d.name,
                project_id=d.project_id, project_name=folder_names.get(d.project_id),
                # a number, not a sentence: the interface writes it in its language
                rows=d.rows,
            ))

        for v in session.exec(
            select(SavedView).where(SavedView.project_id.in_(readable), SavedView.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="view", id=v.id, name=v.name,
                project_id=v.project_id, project_name=folder_names.get(v.project_id),
            ))

    # scripts: VIEW on the folder AND the use of their environment (services/saved_scripts.py)
    from app.services import saved_scripts, script_builds

    stmt = saved_scripts.visible_query(session, user) if script_builds.disabled_reason() is None else None
    if stmt is not None:
        for sc in session.exec(stmt.where(SavedScript.name.ilike(pattern, escape="\\"))).all():
            hits.append(SearchHit(
                kind="script", id=sc.id, name=sc.name,
                project_id=sc.project_id, project_name=folder_names.get(sc.project_id),
            ))

    # SQL scripts: VIEW on the folder (services/sql_scripts.py)
    from app.models import SqlScript
    from app.services import sql_scripts

    stmt = sql_scripts.visible_query(session, user)
    if stmt is not None:
        for q in session.exec(stmt.where(SqlScript.name.ilike(pattern, escape="\\"))).all():
            hits.append(SearchHit(
                kind="sql", id=q.id, name=q.name,
                project_id=q.project_id, project_name=folder_names.get(q.project_id),
            ))

    # notebooks: as scripts, VIEW on the folder AND the use of their environment
    # (services/notebooks.py)
    from app.models import Notebook
    from app.services import notebooks

    stmt = notebooks.visible_query(session, user) if script_builds.disabled_reason() is None else None
    if stmt is not None:
        for nb in session.exec(stmt.where(Notebook.name.ilike(pattern, escape="\\"))).all():
            hits.append(SearchHit(
                kind="notebook", id=nb.id, name=nb.name,
                project_id=nb.project_id, project_name=folder_names.get(nb.project_id),
            ))

    # reports: VIEW on the folder (services/reports.py)
    from app.models import Report
    from app.services import reports

    stmt = reports.visible_query(session, user)
    if stmt is not None:
        for r in session.exec(stmt.where(Report.name.ilike(pattern, escape="\\"))).all():
            hits.append(SearchHit(
                kind="report", id=r.id, name=r.name,
                project_id=r.project_id, project_name=folder_names.get(r.project_id),
            ))

    # CONNECT is orthogonal to VIEW: whoever reads a folder does not necessarily see its
    # connections, so the set is another
    if connectable:
        for c in session.exec(
            select(Connection).where(Connection.project_id.in_(connectable), Connection.name.ilike(pattern, escape="\\"))
        ).all():
            hits.append(SearchHit(
                kind="connection", id=c.id, name=c.name,
                project_id=c.project_id, project_name=folder_names.get(c.project_id),
                detail=c.db_type,
            ))

    # the counts describe EVERYTHING that matches, not the current page: it is the number
    # that goes in the kind selector
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.kind] = counts.get(h.kind, 0) + 1

    if kind:
        hits = [h for h in hits if h.kind == kind]

    # minimal but real relevance: whoever STARTS with the searched term comes first.
    # Without it, searching "ordini" puts "riordini_2024" above "ordini".
    lowercase = term.lower()
    hits.sort(key=lambda h: (
        0 if h.name.lower().startswith(lowercase) else 1,
        _KIND_ORDER.get(h.kind, 9),
        h.name.lower(),
    ))

    return SearchOut(items=hits[offset:offset + limit], total=len(hits), counts=counts)
