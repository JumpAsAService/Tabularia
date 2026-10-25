"""«Run now» from several replicas and the scheduler check the queue and enqueue IN LINE,
flow by flow (a real Postgres: the advisory lock does not exist on SQLite).

Live, twenty «Run now» together on three gateways let two through: both saw the queue
empty before the other had committed."""
import threading
import time
from types import SimpleNamespace

from sqlmodel import Session

from app.db import migra as m
from app.services import orchestrator as orch
from tests.conftest import make_flow, make_project, make_user
from tests.test_migrations import temporary_database  # noqa: F401 — the fixture


def _launch(eng, flow_id, user_id_, outcomes, name, late):
    """What «Run now» does: in line, check, enqueue, commit."""
    with Session(eng) as s:
        orch.in_line_for_the_flow(s, flow_id)
        if orch.flow_running(s, flow_id):
            outcomes[name] = 409
            return
        orch.create_orchestration_run(s, SimpleNamespace(id=user_id_), SimpleNamespace(id=flow_id), commit=False)
        time.sleep(late)  # the commit comes late: it is the window in which the other was looking
        s.commit()
        outcomes[name] = 202


def test_two_launches_together_on_the_same_flow_queue_one(temporary_database):  # noqa: F811
    m.migra(temporary_database)
    with Session(temporary_database) as s:
        u = make_user(s, email="fila@x.local", is_superuser=True)
        p = make_project(s, name="fila", owner_id=u.id)
        f = make_flow(s, name="f", project_id=p.id, owner_id=u.id, definition="{}")
        ids = (f.id, u.id)
    outcomes: dict = {}
    first = threading.Thread(target=_launch, args=(temporary_database, *ids, outcomes, "primo", 1.0))
    second = threading.Thread(target=_launch, args=(temporary_database, *ids, outcomes, "secondo", 0))
    first.start()
    time.sleep(0.3)
    second.start()
    first.join(10)
    second.join(10)
    assert outcomes == {"primo": 202, "secondo": 409}
