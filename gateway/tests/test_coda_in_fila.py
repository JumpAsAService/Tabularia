"""«Esegui ora» da più repliche e lo scheduler controllano la coda e accodano IN FILA,
flusso per flusso (Postgres vero: il lucchetto consultivo non esiste su SQLite).

Dal vivo, venti «Esegui ora» insieme su tre gateway ne facevano passare due: entrambi
vedevano la coda vuota prima che l'altro avesse fatto il commit."""
import threading
import time
from types import SimpleNamespace

from sqlmodel import Session

from app.db import migra as m
from app.services import orchestrator as orch
from tests.conftest import make_flow, make_project, make_user
from tests.test_migrazioni import database_temporaneo  # noqa: F401 — la fixture


def _lancia(eng, flow_id, user_id_, esiti, name, tarda):
    """Quello che fa «Esegui ora»: in fila, controllo, accodamento, commit."""
    with Session(eng) as s:
        orch.in_line_for_the_flow(s, flow_id)
        if orch.flow_running(s, flow_id):
            esiti[name] = 409
            return
        orch.create_orchestration_run(s, SimpleNamespace(id=user_id_), SimpleNamespace(id=flow_id), commit=False)
        time.sleep(tarda)  # il commit arriva tardi: è la finestra in cui l'altro guardava
        s.commit()
        esiti[name] = 202


def test_due_lanci_insieme_sullo_stesso_flusso_ne_accodano_uno(database_temporaneo):  # noqa: F811
    m.migra(database_temporaneo)
    with Session(database_temporaneo) as s:
        u = make_user(s, email="fila@x.local", is_superuser=True)
        p = make_project(s, name="fila", owner_id=u.id)
        f = make_flow(s, name="f", project_id=p.id, owner_id=u.id, definition="{}")
        ids = (f.id, u.id)
    esiti: dict = {}
    primo = threading.Thread(target=_lancia, args=(database_temporaneo, *ids, esiti, "primo", 1.0))
    secondo = threading.Thread(target=_lancia, args=(database_temporaneo, *ids, esiti, "secondo", 0))
    primo.start()
    time.sleep(0.3)
    secondo.start()
    primo.join(10)
    secondo.join(10)
    assert esiti == {"primo": 202, "secondo": 409}
