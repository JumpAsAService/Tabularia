"""Ricerca unificata per nome su tutte le risorse del catalogo.

I contratti che contano, e che sono il motivo per cui questa rotta non è una
UNION SQL:

1. ogni tipo ha il SUO insieme di permessi. Le cartelle usano l'insieme
   NAVIGABILE (include gli antenati, per mostrare il percorso), i contenuti
   quello LEGGIBILE, le connessioni la capability ORTOGONALE `CONNECT`. Chi ha
   solo VIEW su una cartella non ne vede le connessioni;
2. i `counts` descrivono tutto ciò che combacia, non la pagina e nemmeno il
   filtro per tipo: sono i numeri del selettore;
3. si cerca solo nel NOME, e i metacaratteri di LIKE restano letterali.
"""
from app.models import Connection, SavedView
from app.models.permission import Capability
from app.routes.search import search_everything
from tests.conftest import (
    make_datasource,
    make_flow,
    make_permission,
    make_project,
    make_user,
)


def _cerca(session, user, q, kind=None, limit=30, offset=0):
    return search_everything(q=q, kind=kind, limit=limit, offset=offset, user=user, session=session)


def _connessione(session, name: str, project_id: int, db_type: str = "postgresql"):
    c = Connection(name=name, project_id=project_id, db_type=db_type, host="dbhost")
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _vista(session, name: str, project_id: int, datasource_id: int):
    v = SavedView(name=name, project_id=project_id, datasource_id=datasource_id)
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


# ── la domanda vuota non è una ricerca ──────────────────────────────────────
def test_an_empty_query_returns_nothing(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="vendite")
    make_flow(session, name="vendite mensili", project_id=p.id)

    for q in ("", "   "):
        res = _cerca(session, admin, q)
        assert res.total == 0 and res.items == [] and res.counts == {}


# ── trova tutti e cinque i tipi ─────────────────────────────────────────────
def test_it_finds_every_kind_of_resource(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="margine")
    ds = make_datasource(session, name="margine righe", project_id=p.id)
    make_flow(session, name="margine per area", project_id=p.id)
    _vista(session, "margine 2025", p.id, ds.id)
    _connessione(session, "margine db", p.id)

    res = _cerca(session, admin, "margine")
    assert res.total == 5
    assert res.counts == {"folder": 1, "flow": 1, "datasource": 1, "view": 1, "connection": 1}


def test_each_hit_says_where_it_lives(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="vendite")
    make_flow(session, name="report", project_id=p.id)

    hit = next(h for h in _cerca(session, admin, "report").items if h.kind == "flow")
    assert hit.project_id == p.id and hit.project_name == "vendite"


# ── 1. ogni tipo ha il suo insieme di permessi ──────────────────────────────
def test_folders_use_the_navigable_set_but_content_uses_the_readable_one(session):
    """Un ANTENATO si vede per nome (serve a mostrare il percorso) ma il suo
    CONTENUTO non è leggibile. È la distinzione fra `visible_project_ids` e
    `readable_project_ids`, e qui dev'essere rispettata."""
    lettore = make_user(session, email="l@x.local")
    radice = make_project(session, name="report radice")
    figlia = make_project(session, name="report figlia", parent_id=radice.id)
    make_permission(session, user_id=lettore.id, project_id=figlia.id, capability=Capability.VIEW.value)

    make_flow(session, name="report della radice", project_id=radice.id)   # NON leggibile
    make_flow(session, name="report della figlia", project_id=figlia.id)   # leggibile

    res = _cerca(session, lettore, "report")
    cartelle = {h.name for h in res.items if h.kind == "folder"}
    flussi = {h.name for h in res.items if h.kind == "flow"}

    assert cartelle == {"report radice", "report figlia"}  # l'antenato è navigabile
    assert flussi == {"report della figlia"}               # ma il suo contenuto no


def test_view_alone_does_not_reveal_connections(session):
    """CONNECT è ortogonale: chi legge la cartella non ne vede le credenziali."""
    lettore = make_user(session, email="l@x.local")
    p = make_project(session, name="p")
    make_permission(session, user_id=lettore.id, project_id=p.id, capability=Capability.VIEW.value)
    _connessione(session, "warehouse", p.id)
    make_flow(session, name="warehouse sync", project_id=p.id)

    res = _cerca(session, lettore, "warehouse")
    assert {h.kind for h in res.items} == {"flow"}
    assert "connection" not in res.counts


def test_connect_reveals_them(session):
    operatore = make_user(session, email="o@x.local")
    p = make_project(session, name="p")
    make_permission(session, user_id=operatore.id, project_id=p.id, capability=Capability.CONNECT.value)
    _connessione(session, "warehouse", p.id)

    res = _cerca(session, operatore, "warehouse")
    assert [h.kind for h in res.items] == ["connection"]


def test_a_stranger_finds_nothing(session):
    estraneo = make_user(session, email="e@x.local")
    p = make_project(session, name="segreta")
    make_flow(session, name="segreta pipeline", project_id=p.id)

    assert _cerca(session, estraneo, "segreta").total == 0


# ── 2. i conteggi descrivono tutto, il filtro restringe solo gli elementi ────
def test_counts_cover_everything_even_when_filtering_by_kind(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    for i in range(3):
        make_flow(session, name=f"nota {i}", project_id=p.id)
    make_datasource(session, name="nota base", project_id=p.id)

    res = _cerca(session, admin, "nota", kind="flow")
    assert [h.kind for h in res.items] == ["flow"] * 3   # filtrati
    assert res.total == 3
    assert res.counts == {"flow": 3, "datasource": 1}     # ma i numeri restano interi


def test_the_window_is_limited_while_the_total_is_not(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    for i in range(10):
        make_flow(session, name=f"riga {i:02d}", project_id=p.id)

    res = _cerca(session, admin, "riga", limit=3, offset=0)
    assert res.total == 10 and len(res.items) == 3
    coda = _cerca(session, admin, "riga", limit=3, offset=9)
    assert len(coda.items) == 1 and coda.items[0].name == "riga 09"


# ── 3. rilevanza e metacaratteri ────────────────────────────────────────────
def test_names_that_start_with_the_term_come_first(session):
    """Senza questo, cercare «ordini» metterebbe «riordini» davanti a «ordini»."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="riordini 2024", project_id=p.id)
    make_flow(session, name="ordini 2024", project_id=p.id)

    assert [h.name for h in _cerca(session, admin, "ordini").items] == ["ordini 2024", "riordini 2024"]


def test_like_metacharacters_stay_literal(session):
    """`_` è un jolly in LIKE. Chi cerca `a_b` intende `a_b`. Questo test è anche
    la prova che l'escape è DICHIARATO: senza, su SQLite passerebbe il jolly."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="a_b esatto", project_id=p.id)
    make_flow(session, name="axb diverso", project_id=p.id)

    assert [h.name for h in _cerca(session, admin, "a_b").items] == ["a_b esatto"]


def test_percent_stays_literal_too(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="sconto 50% netto", project_id=p.id)
    make_flow(session, name="sconto pieno", project_id=p.id)

    assert [h.name for h in _cerca(session, admin, "50%").items] == ["sconto 50% netto"]


def test_the_search_is_case_insensitive(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    make_flow(session, name="Margine Lordo", project_id=p.id)

    assert _cerca(session, admin, "margine lordo").total == 1
