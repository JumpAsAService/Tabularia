"""L'avanzamento di un import, per chi lo guarda: le fasi che ogni import
riporta (database di ogni tipo, SharePoint), il task che le scrive nel backend
dei risultati al più ogni due secondi, e l'engine che le restituisce.

Il percorso del database è uno solo per tutti i driver (ingest_db_to_parquet):
qui lo esercita un driver finto su SQLite, come test_ingest_row_groups."""
import pytest

from app.api.routes import tasks as rotta_tasks
from app.ingest import db_source
from app.ingest import sharepoint_source as sp
from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, _dbapi_batches, ingest_db_to_parquet
from app.tasks import jobs
from tests.fake_graph import FakeGraph
from tests.fakes import FakeStorage
from tests.test_ingest_row_groups import _cursore
from tests.test_sharepoint_source import TREE, _client, _conn


def test_l_import_da_database_dice_attesa_righe_e_salvataggio(monkeypatch):
    rows = [(i, f"n{i}") for i in range(25)]
    monkeypatch.setitem(db_source._DRIVERS, "postgresql",
                        lambda conn, query: _dbapi_batches(_cursore(rows), batch_rows=10))
    visti = []
    ingest_db_to_parquet(DbConnectionSpec(db_type="postgresql", host="h", database="d"), DbSourceSpec(mode="table", ref="t"),
                         bucket="b", key="k.parquet", storage=FakeStorage(), progress=lambda f, **c: visti.append((f, c)))
    assert visti == [("waiting", {}), ("reading", {"rows": 10}), ("reading", {"rows": 20}), ("reading", {"rows": 25}),
                     ("saving", {"rows": 25})]


def test_l_import_da_sharepoint_conta_i_file(monkeypatch):
    visti = []
    sp.ingest_sharepoint_to_parquet(_conn(), sp.SharePointSourceSpec(path="Budget/2026/*.xlsx", sheet="Dati"), "b", "k.parquet",
                                    FakeStorage(), client=_client(FakeGraph(TREE)), progress=lambda f, **c: visti.append((f, c)))
    # feb.xlsx (1 riga) poi gen.xlsx (2 righe): le righe sono quelle lette PRIMA del file
    assert visti == [("files", {"file": 1, "files": 2, "rows": 0}), ("files", {"file": 2, "files": 2, "rows": 1}),
                     ("saving", {"rows": 3})]


class _TaskFinto:
    def __init__(self):
        self.request = type("R", (), {"id": "t-1"})()
        self.stati = []

    def update_state(self, state, meta):
        self.stati.append((state, meta))


def test_il_task_scrive_al_piu_ogni_due_secondi_ma_sempre_al_cambio_di_fase(monkeypatch):
    import celery

    task = _TaskFinto()
    monkeypatch.setattr(celery, "current_task", task)
    orologio = iter([0.0, 0.5, 1.0, 2.5, 2.6, 2.7])
    monkeypatch.setattr(jobs._time_mod, "monotonic", lambda: next(orologio))
    avanza = jobs._Avanzamento()
    for fase, righe in (("reading", 1), ("reading", 2), ("reading", 3), ("reading", 4), ("reading", 5), ("saving", 5)):
        avanza(fase, rows=righe)
    assert task.stati == [("STARTED", {"phase": "reading", "rows": 1}), ("STARTED", {"phase": "reading", "rows": 4}),
                          ("STARTED", {"phase": "saving", "rows": 5})]


def test_fuori_da_un_worker_non_fa_niente_e_un_guasto_non_ferma_l_import(monkeypatch):
    import celery

    monkeypatch.setattr(celery, "current_task", None)
    jobs._Avanzamento()("reading", rows=1)  # nessun task: niente da scrivere, niente errori

    class _Rotto(_TaskFinto):
        def update_state(self, state, meta):
            raise ConnectionError("valkey giù")

    monkeypatch.setattr(celery, "current_task", _Rotto())
    jobs._Avanzamento()("reading", rows=1)


@pytest.mark.parametrize("info, atteso", [
    ({"phase": "reading", "rows": 1200, "pid": 7}, {"phase": "reading", "rows": 1200}),
    ({"phase": "files", "file": 2, "files": 5, "rows": 0}, {"phase": "files", "file": 2, "files": 5, "rows": 0}),
    ({"pid": 7, "hostname": "worker@x"}, None),  # STARTED di Celery: nessun avanzamento
])
def test_l_engine_restituisce_l_avanzamento(monkeypatch, info, atteso):
    class _Risultato:
        def __init__(self, task_id, app=None):
            self.status, self.info = "STARTED", info

    monkeypatch.setattr(rotta_tasks, "AsyncResult", _Risultato)
    assert rotta_tasks.get_task_status("t-1").progress == atteso
