"""Liste paginate con ricerca server-side sul dataset INTERO.

Regressione per il requisito: il filtro `q` deve combaciare su tutto il dataset
(non solo sulla pagina), e la lista deve restare limitata (limit/offset + total).
"""
import pytest

from app.routes.datasources import search_datasources
from app.routes.flows import search_flows
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


def _viewer_with_project(session):
    user = make_user(session, email="v@x.local")
    p = make_project(session, name="p")
    make_permission(session, user_id=user.id, project_id=p.id, capability="view")
    return user, p


async def test_datasources_scoped_to_readable(session):
    user, p = _viewer_with_project(session)
    other = make_project(session, name="other")
    make_datasource(session, name="mia", project_id=p.id)
    make_datasource(session, name="altrui", project_id=other.id)  # non leggibile
    res = search_datasources(q=None, limit=50, offset=0, user=user, session=session)
    assert res.total == 1 and [d.name for d in res.items] == ["mia"]


async def test_datasources_query_matches_whole_dataset_not_page(session):
    user, p = _viewer_with_project(session)
    # 20 datasource; solo 3 contengono "vendite"
    for i in range(20):
        name = f"vendite_{i}" if i < 3 else f"ordini_{i}"
        make_datasource(session, name=name, project_id=p.id)
    # pagina piccola, ma la ricerca vede TUTTO il dataset → total = 3
    res = search_datasources(q="vendite", limit=2, offset=0, user=user, session=session)
    assert res.total == 3
    assert len(res.items) == 2  # la finestra è limitata
    assert all("vendite" in d.name for d in res.items)


async def test_datasources_pagination_window(session):
    user, p = _viewer_with_project(session)
    for i in range(5):
        make_datasource(session, name=f"ds_{i:02d}", project_id=p.id)
    page1 = search_datasources(q=None, limit=2, offset=0, user=user, session=session)
    page3 = search_datasources(q=None, limit=2, offset=4, user=user, session=session)
    assert page1.total == 5 and len(page1.items) == 2
    assert len(page3.items) == 1  # ultima pagina
    # ordinate per nome, senza sovrapposizioni tra le pagine
    assert page1.items[0].name == "ds_00" and page3.items[0].name == "ds_04"


async def test_datasources_empty_when_no_readable_projects(session):
    user = make_user(session, email="noaccess@x.local")  # nessun grant
    make_datasource(session, name="x", project_id=1)
    res = search_datasources(q=None, limit=50, offset=0, user=user, session=session)
    assert res.total == 0 and res.items == []


async def test_flows_query_and_scope(session):
    user, p = _viewer_with_project(session)
    other = make_project(session, name="other")
    make_flow(session, name="vendite pivot", project_id=p.id)
    make_flow(session, name="ordini clean", project_id=p.id)
    make_flow(session, name="vendite altrui", project_id=other.id)  # non leggibile
    res = search_flows(q="vendite", limit=50, offset=0, user=user, session=session)
    assert res.total == 1 and res.items[0].name == "vendite pivot"


async def test_flows_superuser_sees_all(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    pa = make_project(session, name="pa")
    pb = make_project(session, name="pb")
    make_flow(session, name="a", project_id=pa.id)
    make_flow(session, name="b", project_id=pb.id)
    res = search_flows(q=None, limit=50, offset=0, user=admin, session=session)
    assert res.total == 2
