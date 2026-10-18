"""What the gateway hands the interface to SHOW is not Italian prose.

A getting-started run of 1.2.0 in English found Italian in the UI: the flow
version label «creazione», «N righe» in the global search, «Flusso #N» in the
lineage graph, «Nuova conversazione» as a chat title. Values the interface shows
are now stable English codes or numbers, and the interface translates them
(older rows written in Italian are translated there too).
"""
from app.routes.flows import create_flow, promote_flow_version, update_flow
from app.routes.search import search_everything
from app.schemas.models import FlowCreate, FlowUpdate
from app.services import ai_chats, lineage
from tests.conftest import make_datasource, make_project, make_user


def test_the_flow_version_notes_are_stable_codes(session):
    user = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = create_flow(p.id, FlowCreate(name="f", definition='{"v":1}'), None, user, session)
    update_flow(flow.id, FlowUpdate(definition='{"v":2}'), None, user, session)
    promote_flow_version(flow.id, 1, None, user, session)
    from sqlmodel import select

    from app.models import FlowVersion

    notes = [v.note for v in session.exec(
        select(FlowVersion).where(FlowVersion.flow_id == flow.id).order_by(FlowVersion.version)
    ).all()]
    assert notes == ["created", "edited", "promoted from v1"]


def test_a_datasource_hit_carries_its_row_count_not_a_sentence(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    ds = make_datasource(session, name="sales", project_id=p.id)
    ds.rows = 1234
    session.add(ds)
    session.commit()
    hit = search_everything(q="sales", kind=None, limit=30, offset=0, user=admin, session=session).items[0]
    assert hit.rows == 1234 and "righe" not in hit.detail


def test_a_flow_out_of_reach_in_the_lineage_is_not_labelled_in_italian(session):
    g = lineage._Graph()
    lineage._restricted_flow(g, 7, set())
    assert g.nodes["flow:7"]["label"] == "Flow #7"


def test_an_empty_question_leaves_the_title_to_the_interface(session):
    anna = make_user(session, email="anna@x.it")
    chat = ai_chats.crea(session, anna, "   ", "m", None)
    assert chat.title == ""
    # the first real question names it, as before; an old Italian default too
    ai_chats.salva_turno(session, chat.id, domanda="Orders in March?", messaggi=[], model_id="m",
                         input_tokens=1, output_tokens=1, requests=1, cost=None)
    session.refresh(chat)
    assert chat.title == "Orders in March?"
    old = ai_chats.crea(session, anna, "", "m", None)
    old.title = "Nuova conversazione"
    session.add(old)
    session.commit()
    ai_chats.salva_turno(session, old.id, domanda="Revenue by region", messaggi=[], model_id="m",
                         input_tokens=1, output_tokens=1, requests=1, cost=None)
    session.refresh(old)
    assert old.title == "Revenue by region"
