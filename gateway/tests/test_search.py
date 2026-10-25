"""Unified search by name over all the catalog's resources.

The contracts that count, and that are the reason this route is not an SQL UNION:

1. every kind has ITS OWN set of permissions. Folders use the NAVIGABLE set (it
   includes the ancestors, to show the path), contents the READABLE one, connections
   the ORTHOGONAL `CONNECT` capability. Whoever has only VIEW on a folder does not
   see its connections;
2. the `counts` describe everything that matches, not the page and not even the kind
   filter: they are the selector's numbers;
3. only the NAME is searched, and LIKE's metacharacters stay literal.
"""
from app.models import Connection, FlowGroup, SavedView
from app.models.permission import Capability
from app.routes.search import search_everything
from tests.conftest import (
    make_datasource,
    make_flow,
    make_permission,
    make_project,
    make_user,
)


def _search(session, user, q, kind=None, limit=30, offset=0):
    return search_everything(q=q, kind=kind, limit=limit, offset=offset, user=user, session=session)


def _connection(session, name: str, project_id: int, db_type: str = "postgresql"):
    c = Connection(name=name, project_id=project_id, db_type=db_type, host="dbhost")
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _view(session, name: str, project_id: int, datasource_id: int):
    v = SavedView(name=name, project_id=project_id, datasource_id=datasource_id)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


# ── the empty question is not a search ──────────────────────────────────────
def test_an_empty_query_returns_nothing(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="vendite")
    make_flow(session, name="vendite mensili", project_id=p.id)

    for q in ("", "   "):
        res = _search(session, admin, q)
        assert res.total == 0 and res.items == [] and res.counts == {}


# ── finds all six kinds ─────────────────────────────────────────────────────
def test_it_finds_every_kind_of_resource(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="margine")
    ds = make_datasource(session, name="margine righe", project_id=p.id)
    make_flow(session, name="margine per area", project_id=p.id)
    _view(session, "margine 2025", p.id, ds.id)
    _connection(session, "margine db", p.id)
    session.add(FlowGroup(name="margine notturno", project_id=p.id))
    session.commit()

    res = _search(session, admin, "margine")
    assert res.total == 6
    assert res.counts == {"folder": 1, "flow": 1, "group": 1, "datasource": 1, "view": 1, "connection": 1}


def test_each_hit_says_where_it_lives(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="vendite")
    make_flow(session, name="report", project_id=p.id)

    hit = next(h for h in _search(session, admin, "report").items if h.kind == "flow")
    assert hit.project_id == p.id and hit.project_name == "vendite"


# ── 1. every kind has its own set of permissions ────────────────────────────
def test_folders_use_the_navigable_set_but_content_uses_the_readable_one(session):
    """An ANCESTOR is seen by name (it serves to show the path) but its CONTENT is not
    readable. It is the difference between `visible_project_ids` and
    `readable_project_ids`, and here it must be respected."""
    reader = make_user(session, email="l@x.local")
    root = make_project(session, name="report radice")
    child_folder = make_project(session, name="report figlia", parent_id=root.id)
    make_permission(session, user_id=reader.id, project_id=child_folder.id, capability=Capability.VIEW.value)

    make_flow(session, name="report della radice", project_id=root.id)   # NOT readable
    make_flow(session, name="report della figlia", project_id=child_folder.id)   # readable

    res = _search(session, reader, "report")
    folders = {h.name for h in res.items if h.kind == "folder"}
    flows = {h.name for h in res.items if h.kind == "flow"}

    assert folders == {"report radice", "report figlia"}  # the ancestor is navigable
    assert flows == {"report della figlia"}               # but its content is not


def test_view_alone_does_not_reveal_connections(session):
    """CONNECT is orthogonal: whoever reads the folder does not see its credentials."""
    reader = make_user(session, email="l@x.local")
    p = make_project(session, name="p")
    make_permission(session, user_id=reader.id, project_id=p.id, capability=Capability.VIEW.value)
    _connection(session, "warehouse", p.id)
    make_flow(session, name="warehouse sync", project_id=p.id)

    res = _search(session, reader, "warehouse")
    assert {h.kind for h in res.items} == {"flow"}
    assert "connection" not in res.counts


def test_connect_reveals_them(session):
    operator = make_user(session, email="o@x.local")
    p = make_project(session, name="p")
    make_permission(session, user_id=operator.id, project_id=p.id, capability=Capability.CONNECT.value)
    _connection(session, "warehouse", p.id)

    res = _search(session, operator, "warehouse")
    assert [h.kind for h in res.items] == ["connection"]


def test_a_stranger_finds_nothing(session):
    stranger = make_user(session, email="e@x.local")
    p = make_project(session, name="segreta")
    make_flow(session, name="segreta pipeline", project_id=p.id)

    assert _search(session, stranger, "segreta").total == 0


# ── 2. the counts describe everything, the filter narrows only the items ────
def test_counts_cover_everything_even_when_filtering_by_kind(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    for i in range(3):
        make_flow(session, name=f"nota {i}", project_id=p.id)
    make_datasource(session, name="nota base", project_id=p.id)

    res = _search(session, admin, "nota", kind="flow")
    assert [h.kind for h in res.items] == ["flow"] * 3   # filtered
    assert res.total == 3
    assert res.counts == {"flow": 3, "datasource": 1}     # but the numbers stay whole


def test_the_window_is_limited_while_the_total_is_not(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    for i in range(10):
        make_flow(session, name=f"riga {i:02d}", project_id=p.id)

    res = _search(session, admin, "riga", limit=3, offset=0)
    assert res.total == 10 and len(res.items) == 3
    queue_ = _search(session, admin, "riga", limit=3, offset=9)
    assert len(queue_.items) == 1 and queue_.items[0].name == "riga 09"


# ── 3. relevance and metacharacters ─────────────────────────────────────────
def test_names_that_start_with_the_term_come_first(session):
    """Without this, searching «ordini» would put «riordini» before «ordini»."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="riordini 2024", project_id=p.id)
    make_flow(session, name="ordini 2024", project_id=p.id)

    assert [h.name for h in _search(session, admin, "ordini").items] == ["ordini 2024", "riordini 2024"]


def test_like_metacharacters_stay_literal(session):
    """`_` is a wildcard in LIKE. Whoever searches `a_b` means `a_b`. This test is also the
    proof that the escape is DECLARED: without it, on SQLite the wildcard would pass."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="a_b esatto", project_id=p.id)
    make_flow(session, name="axb diverso", project_id=p.id)

    assert [h.name for h in _search(session, admin, "a_b").items] == ["a_b esatto"]


def test_percent_stays_literal_too(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="sconto 50% netto", project_id=p.id)
    make_flow(session, name="sconto pieno", project_id=p.id)

    assert [h.name for h in _search(session, admin, "50%").items] == ["sconto 50% netto"]


def test_the_search_is_case_insensitive(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="Margine Lordo", project_id=p.id)

    assert _search(session, admin, "margine lordo").total == 1
