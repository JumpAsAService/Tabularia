"""Le esecuzioni dei flussi passano da una coda sul database.

Finché il gateway era un processo solo, un'esecuzione era un task asyncio dentro
quel processo: l'elenco dei flussi in corso stava in un `set` in memoria, e le
orchestrazioni rimaste a metà si chiudevano all'avvio. Con più processi nessuna
delle due cose regge — un altro processo non vede quel `set`, e l'avvio di uno
chiuderebbe le esecuzioni vive degli altri.

Ora chi lancia (run-now, lo scheduler) scrive una riga PENDING; chi esegue la
prende con un UPDATE condizionato, la firma e ne tiene vivo il battito. Questi
test tengono ferme le garanzie che il `set` dava gratis:

  - una riga in coda la prende UN processo;
  - di un flusso gira UNA esecuzione alla volta (lo dice il database);
  - uno slot dello scheduler lancia UNA volta, anche se lo vedono in due;
  - un'esecuzione il cui processo è morto viene chiusa, le altre no.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

import app.services.orchestrator as orch
from app.models import Flow, Run
from app.models.run import ORCHESTRATION_INTERRUPTED, STEP_INTERRUPTED
from app.routes.flows import run_flow_now
from app.services import scheduler
from tests.conftest import make_flow, make_project, make_run, make_user

pytestmark = pytest.mark.anyio


@pytest.fixture
def queue_(db_engine, session, monkeypatch):
    """Orchestratore e scheduler puntati al DB di test, con un utente e una cartella."""
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "heartbeat_engine", db_engine)
    monkeypatch.setattr(orch, "_alive", set())
    monkeypatch.setattr(scheduler, "engine", db_engine)
    utente = make_user(session, email="chi-lancia@x.local", is_superuser=True)
    folder = make_project(session, name="cartella", owner_id=utente.id)
    return utente, folder


def _flusso(session, utente, folder, name="f", **kw):
    return make_flow(session, name=name, project_id=folder.id, owner_id=utente.id, definition="{}", **kw)


def _state(session, run_id):
    session.expire_all()
    return session.get(Run, run_id)


def _come(monkeypatch, sign):
    """Da qui in poi le funzioni dell'orchestratore agiscono come il processo `sign`."""
    monkeypatch.setattr(orch, "INSTANCE", sign)


# ── mettere in coda ──────────────────────────────────────────────────────────
def test_un_lancio_e_una_riga_in_coda(session, queue_):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    run = orch.create_orchestration_run(session, utente, flusso, "manual", "production")
    assert (run.kind, run.status, run.engine_mode, run.trigger_type) == ("orchestration", "PENDING", "production", "manual")
    assert run.claimed_by is None and run.heartbeat_at is None and run.launched_by == utente.id


async def test_esegui_ora_rifiuta_un_flusso_gia_in_coda_o_in_corso(session, queue_):
    """Ogni «Esegui ora» di troppo costava una riga in `runs` e un giro della coda,
    e ripetuto spingeva in fondo le esecuzioni degli altri flussi: ora è un 409,
    nella lingua di chi chiede, e non scrive niente."""
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)

    async def esegui_ora(request=None):
        return run_flow_now(flusso.id, request=request, mode="development", user=utente, session=session)

    primo = await esegui_ora()
    with pytest.raises(HTTPException) as e:                       # in coda
        await esegui_ora()
    assert e.value.status_code == 409 and "already queued or running" in e.value.detail
    orch.claim(5)
    session.expire_all()
    italiano = SimpleNamespace(headers={"accept-language": "it-IT,it;q=0.9,en;q=0.8"})
    with pytest.raises(HTTPException) as e:                       # in esecuzione
        await esegui_ora(italiano)
    assert e.value.status_code == 409 and "già in coda o in esecuzione" in e.value.detail
    assert len(session.exec(select(Run).where(Run.flow_id == flusso.id)).all()) == 1
    await orch.run_it(primo["run_id"])                            # finito: si rilancia
    session.expire_all()
    secondo = await esegui_ora()
    assert secondo["run_id"] != primo["run_id"] and _state(session, secondo["run_id"]).status == "PENDING"


# ── prendere dalla coda ──────────────────────────────────────────────────────
def test_chi_prende_firma_e_batte(session, queue_, monkeypatch):
    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))
    _come(monkeypatch, "processo-a")
    assert orch.claim(5) == ([run.id], [])
    taken_one = _state(session, run.id)
    assert (taken_one.status, taken_one.claimed_by) == ("STARTED", "processo-a")
    assert taken_one.heartbeat_at is not None


def test_una_riga_la_prende_un_processo_solo(session, queue_, monkeypatch):
    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))
    _come(monkeypatch, "processo-a")
    assert orch.claim(5) == ([run.id], [])
    _come(monkeypatch, "processo-b")
    assert orch.claim(5) == ([], [])
    assert _state(session, run.id).claimed_by == "processo-a"


def test_due_processi_che_vedono_la_stessa_riga_la_prende_il_primo(session, queue_, db_engine, monkeypatch):
    """Il caso vero: entrambi hanno letto la riga come PENDING prima che uno dei
    due la prendesse. È l'UPDATE condizionato a decidere, non la lettura."""
    from sqlalchemy import update

    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))

    def prendi(sign):
        with Session(db_engine) as s:
            r = s.exec(update(Run).where(Run.id == run.id, Run.status == "PENDING").values(status="STARTED", claimed_by=sign))
            s.commit()
            return r.rowcount

    assert [prendi("a"), prendi("b")] == [1, 0]


def test_si_prende_fino_al_limite_e_in_ordine(session, queue_, monkeypatch):
    utente, folder = queue_
    runs = [orch.create_orchestration_run(session, utente, _flusso(session, utente, folder, f"f{i}")).id for i in range(4)]
    taken, _ = orch.claim(2)
    assert taken == runs[:2]
    taken, _ = orch.claim(10)
    assert taken == runs[2:]


# ── una esecuzione per flusso ────────────────────────────────────────────────
def test_il_database_rifiuta_due_esecuzioni_vive_dello_stesso_flusso(session, queue_):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    with pytest.raises(IntegrityError):
        make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    session.rollback()
    # quello che NON deve rifiutare: un altro flusso, esecuzioni finite, run di altro tipo
    other = _flusso(session, utente, folder, "altro")
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=other.id)
    make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_id=flusso.id)
    make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=flusso.id)
    make_run(session, kind="flow", status="STARTED", task_id="t-1", flow_id=flusso.id)
    make_run(session, kind="flow", status="STARTED", task_id="t-2", flow_id=flusso.id)


def test_un_lancio_manuale_di_un_flusso_che_gira_e_respinto(session, queue_, monkeypatch):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    primo = orch.create_orchestration_run(session, utente, flusso)
    assert orch.claim(5) == ([primo.id], [])
    secondo = orch.create_orchestration_run(session, utente, flusso)
    _come(monkeypatch, "un-altro-processo")
    assert orch.claim(5) == ([], [secondo.id])
    assert _state(session, secondo.id).status == "PENDING"  # la chiude il chiamante, con l'avviso


async def test_il_respinto_viene_chiuso_come_gia_in_esecuzione(session, queue_):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    primo = orch.create_orchestration_run(session, utente, flusso)
    orch.claim(5)
    secondo = orch.create_orchestration_run(session, utente, flusso)
    _, refused = orch.claim(5)
    for run_id in refused:
        await orch._finalize_orch_run(run_id, "FAILURE", orch.ALREADY_RUNNING)
    closed_one = _state(session, secondo.id)
    assert (closed_one.status, closed_one.error) == ("FAILURE", "flow already running")
    assert _state(session, primo.id).status == "STARTED"  # quello vero non è toccato


def test_uno_slot_schedulato_su_un_flusso_che_gira_sparisce_senza_traccia(session, queue_):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    orch.create_orchestration_run(session, utente, flusso)
    orch.claim(5)
    slot = orch.create_orchestration_run(session, utente, flusso, "schedule", "production")
    assert orch.claim(5) == ([], [])
    assert _state(session, slot.id) is None


# ── battito e orfane ─────────────────────────────────────────────────────────
def test_il_battito_tocca_solo_le_esecuzioni_di_questo_processo(session, queue_, monkeypatch):
    utente, folder = queue_
    old = datetime(2020, 1, 1)
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder, "a").id, claimed_by="io", heartbeat_at=old)
    altrui = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder, "b").id, claimed_by="un-altro", heartbeat_at=old)
    finita = make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_id=_flusso(session, utente, folder, "c").id, claimed_by="io", heartbeat_at=old)
    _come(monkeypatch, "io")
    monkeypatch.setattr(orch, "_alive", {mia.id, altrui.id, finita.id})
    assert orch.beat() == 1
    assert _state(session, mia.id).heartbeat_at > old
    assert _state(session, altrui.id).heartbeat_at == old
    assert _state(session, finita.id).heartbeat_at == old


async def test_chi_batte_non_e_orfano_chi_ha_smesso_si(session, queue_):
    utente, folder = queue_
    now = orch._now()
    viva = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder, "a").id, claimed_by="vivo", heartbeat_at=now - timedelta(seconds=5))
    morta = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder, "b").id, claimed_by="morto", heartbeat_at=now - timedelta(seconds=orch._orc().dead_after_seconds + 30))
    in_coda = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flusso(session, utente, folder, "c").id)

    assert await orch.close_orphans() == 1

    assert _state(session, viva.id).status == "STARTED"
    assert _state(session, in_coda.id).status == "PENDING"  # in coda non è «morta»: aspetta chi la prenda
    closed = _state(session, morta.id)
    assert (closed.status, closed.error) == ("FAILURE", ORCHESTRATION_INTERRUPTED)
    assert closed.finished_at is not None


async def test_chiusa_l_orfana_il_flusso_puo_ripartire(session, queue_):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id, claimed_by="morto", heartbeat_at=datetime(2020, 1, 1))
    new = orch.create_orchestration_run(session, utente, flusso)
    assert orch.claim(5) == ([], [new.id])  # finché l'orfana è lì, il flusso risulta occupato
    await orch.close_orphans()
    assert orch.claim(5) == ([new.id], [])


# ── i passi di un'orchestrazione interrotta ───────────────────────────────────
# Un orchestratore che si ferma (rolling update, riavvio dal pannello, sfratto, OOM)
# chiudeva il flusso come interrotto ma lasciava scrivere l'Output già consegnato a un
# worker: il flusso, chiuso, poteva ripartire sopra di lui (righe doppie in append).
async def test_lo_spegnimento_ferma_anche_l_output_gia_partito(session, queue_, fake_engine, monkeypatch):
    utente, folder = queue_
    monkeypatch.setattr(orch._orc(), "poll_interval_seconds", 0.05)
    flusso = _flusso(session, utente, folder)
    run = orch.create_orchestration_run(session, utente, flusso)
    assert orch.claim(5)[0] == [run.id]
    partito, figlio = asyncio.Event(), {}

    async def orchestrate(s, u, f, **kw):
        out = make_run(s, kind="flow", status="STARTED", task_id="t-output", flow_id=f.id,
                       parent_run_id=kw["parent_run_id"])
        figlio["id"] = out.id
        fake_engine.set_task("t-output", "STARTED")
        partito.set()
        await orch._wait_run(s, out)
        return []

    monkeypatch.setattr(orch, "orchestrate", orchestrate)
    task = asyncio.create_task(orch.run_it(run.id))
    await partito.wait()
    task.cancel()  # lo spegnimento del lifespan al SIGTERM
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (_state(session, run.id).status, _state(session, run.id).error) == ("FAILURE", ORCHESTRATION_INTERRUPTED)
    out = _state(session, figlio["id"])
    assert (out.status, out.error) == ("FAILURE", STEP_INTERRUPTED) and fake_engine.revoked == ["t-output"]


async def test_chi_chiude_l_orfana_ferma_i_suoi_passi(session, queue_, fake_engine):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    morta = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id,
                     claimed_by="morto", heartbeat_at=datetime(2020, 1, 1))
    out = make_run(session, kind="flow", status="STARTED", task_id="t-out", flow_id=flusso.id, parent_run_id=morta.id)
    in_coda = make_run(session, kind="flow", status="PENDING", task_id="t-dopo", flow_id=flusso.id,
                       parent_run_id=morta.id)
    finished = make_run(session, kind="flow", status="SUCCESS", task_id="t-prima", flow_id=flusso.id,
                      parent_run_id=morta.id)
    fake_engine.set_task("t-out", "STARTED")  # still running on the engine
    fake_engine.set_task("t-dopo", "PENDING")
    assert await orch.close_orphans() == 1
    assert sorted(fake_engine.revoked) == ["t-dopo", "t-out"]
    assert _state(session, out.id).status == _state(session, in_coda.id).status == "FAILURE"
    assert _state(session, finished.id).status == "SUCCESS"  # quel che è finito resta com'è


async def test_a_step_the_engine_finished_meanwhile_keeps_its_outcome(session, queue_, fake_engine):
    # the gateway pod killed while a flow waited on a refresh: the worker ended it with
    # success before the orphans' sweep. Its new parquet is swapped in, not failed as stopped
    from app.models import Datasource
    from tests.conftest import make_datasource

    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    ds = make_datasource(session, name="ordini", kind="database", key="datasets/1/v0.parquet", project_id=folder.id)
    morta = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id,
                     claimed_by="morto", heartbeat_at=datetime(2020, 1, 1))
    imp = make_run(session, kind="ingest", status="STARTED", task_id="t-imp", datasource_id=ds.id,
                   output_key="datasets/1/new.parquet", parent_run_id=morta.id)
    fake_engine.set_task("t-imp", "SUCCESS", result={"bucket": "data-prep", "rows_written": 5, "columns": []})
    assert await orch.close_orphans() == 1
    assert _state(session, imp.id).status == "SUCCESS" and fake_engine.revoked == []
    session.expire_all()
    assert session.get(Datasource, ds.id).key == "datasets/1/new.parquet"


async def test_cio_che_questo_processo_esegue_non_e_orfano_anche_col_battito_vecchio(session, queue_, fake_engine,
                                                                                      monkeypatch):
    # Postgres fermo oltre dead_after (un failover): al ritorno ogni battito è vecchio, anche
    # quello di chi esegue ancora; il giro degli orfani non chiude (né ferma) le proprie
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id,
                   claimed_by=orch.INSTANCE, heartbeat_at=datetime(2020, 1, 1))
    out = make_run(session, kind="flow", status="STARTED", task_id="t-out", flow_id=flusso.id, parent_run_id=mia.id)
    monkeypatch.setattr(orch, "_alive", {mia.id})
    assert await orch.close_orphans() == 0
    assert _state(session, mia.id).status == _state(session, out.id).status == "STARTED" and fake_engine.revoked == []


def test_il_giro_degli_orfani_aspetta_che_il_processo_batta_da_un_intervallo_intero(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(orch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(orch, "_heartbeat_ok_since", None)
    assert not orch.heartbeat_healthy()  # il database non risponde (o non ancora)
    monkeypatch.setattr(orch, "_heartbeat_ok_since", 1000.0)
    clock[0] += orch._orc().dead_after_seconds - 1
    assert not orch.heartbeat_healthy()  # tornato da poco: gli altri non hanno ancora battuto
    clock[0] += 1
    assert orch.heartbeat_healthy()


async def test_al_riavvio_le_esecuzioni_precedenti_fermano_i_loro_passi(session, queue_, fake_engine, monkeypatch):
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    ospite = orch.INSTANCE.rsplit(":", 1)[0]
    before = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id,
                     claimed_by=f"{ospite}:vecchia-firma", heartbeat_at=orch._now())
    out = make_run(session, kind="flow", status="STARTED", task_id="t-out", flow_id=flusso.id, parent_run_id=before.id)
    fake_engine.set_task("t-out", "STARTED")
    stop = asyncio.Event()
    stop.set()  # il ciclo fa solo l'avvio e si ferma
    monkeypatch.setattr(orch, "start_heartbeat", lambda ev: None)
    await orch.queue_loop(stop)
    assert _state(session, before.id).error == ORCHESTRATION_INTERRUPTED
    assert _state(session, out.id).status == "FAILURE" and fake_engine.revoked == ["t-out"]


# ── eseguire ─────────────────────────────────────────────────────────────────
async def test_eseguire_porta_il_run_in_fondo(session, queue_):
    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))
    orch.claim(5)
    await orch.run_it(run.id)
    done = _state(session, run.id)
    assert (done.status, done.error) == ("SUCCESS", None) and done.finished_at is not None


async def test_eseguire_passa_la_modalita_e_l_origine_scritte_sulla_riga(session, queue_, monkeypatch):
    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder), "schedule", "production")
    orch.claim(5)
    seen = {}

    async def finta(session, user, flow, **kw):
        seen.update(kw, utente=user.id, flusso=flow.id)
        return []

    monkeypatch.setattr(orch, "orchestrate", finta)
    await orch.run_it(run.id)
    assert seen["trigger_type"] == "schedule" and seen["engine_mode"] == "production"
    assert seen["parent_run_id"] == run.id and seen["guarded"] is True and seen["utente"] == utente.id


async def test_un_utente_disattivato_non_fa_girare_niente(session, queue_):
    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))
    utente.is_active = False
    session.add(utente); session.commit()
    orch.claim(5)
    await orch.run_it(run.id)
    closed_one = _state(session, run.id)
    assert (closed_one.status, closed_one.error) == ("FAILURE", "invalid flow or user")


async def test_chi_e_stato_dato_per_morto_si_ferma_prima_del_passo_successivo(session, queue_, monkeypatch):
    """Un processo che non riusciva a battere (database irraggiungibile per un
    minuto) viene dato per perso e il suo run chiuso. Se poi torna, non deve
    andare avanti come se niente fosse: l'esito è già stato scritto, e forse il
    flusso è già stato rilanciato."""
    import json

    utente, folder = queue_
    definition = json.dumps({"nodes": [{"id": "r1", "type": "refresh", "data": {"datasourceId": 123}}], "edges": []})
    flusso = make_flow(session, name="con-un-passo", project_id=folder.id, owner_id=utente.id, definition=definition)
    run = orch.create_orchestration_run(session, utente, flusso)
    orch.claim(5)
    # un altro orchestratore lo chiude
    make = session.get(Run, run.id)
    make.status, make.error = "FAILURE", ORCHESTRATION_INTERRUPTED
    session.add(make); session.commit()

    eseguiti = []

    async def step(*a, **kw):
        eseguiti.append(a)

    monkeypatch.setattr(orch, "_refresh_and_wait", step)
    await orch.run_it(run.id)
    assert eseguiti == []
    assert _state(session, run.id).error == ORCHESTRATION_INTERRUPTED  # non sovrascritto


async def test_un_esito_gia_scritto_non_si_riscrive(session, queue_):
    utente, folder = queue_
    run = make_run(session, kind="orchestration", status="FAILURE", task_id="", flow_id=_flusso(session, utente, folder).id, error="chiuso prima")
    await orch._finalize_orch_run(run.id, "SUCCESS", None)
    assert (_state(session, run.id).status, _state(session, run.id).error) == ("FAILURE", "chiuso prima")


async def test_fermare_il_processo_chiude_le_esecuzioni_in_corso(session, queue_, monkeypatch):
    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))
    orch.claim(5)
    partita = asyncio.Event()

    async def lunga(*a, **kw):
        partita.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(orch, "orchestrate", lunga)
    task = asyncio.create_task(orch.run_it(run.id))
    await partita.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closed_one = _state(session, run.id)
    assert (closed_one.status, closed_one.error) == ("FAILURE", ORCHESTRATION_INTERRUPTED)


# ── il lavoratore, dall'inizio alla fine ─────────────────────────────────────
async def test_il_lavoratore_prende_esegue_e_si_ferma(session, queue_, monkeypatch):
    utente, folder = queue_
    monkeypatch.setattr(orch._orc(), "queue_poll_seconds", 0.05)
    stop = asyncio.Event()
    lavoratore = asyncio.create_task(orch.queue_loop(stop))
    try:
        run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))
        orch.wake()
        for _ in range(100):
            if _state(session, run.id).status == "SUCCESS":
                break
            await asyncio.sleep(0.05)
        assert _state(session, run.id).status == "SUCCESS"
        assert _state(session, run.id).claimed_by == orch.INSTANCE
    finally:
        stop.set()
        await asyncio.wait_for(lavoratore, timeout=5)
    assert orch._wakeup is None  # fermo: `sveglia()` torna a non fare niente


def test_svegliare_dove_non_c_e_un_lavoratore_non_fa_niente():
    assert orch._wakeup is None
    orch.wake()  # un processo `api`: nessun errore, la riga la troverà un orchestratore


# ── lo scheduler: uno slot, un lancio ────────────────────────────────────────
def _schedulato(session, utente, folder, name="notturno"):
    return _flusso(
        session, utente, folder, name, run_schedule="0 3 * * *", run_scheduled_by=utente.id,
        next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
    )


def _in_coda(session, flusso_id):
    session.expire_all()
    return session.exec(select(Run).where(Run.kind == "orchestration", Run.flow_id == flusso_id)).all()


async def test_lo_slot_scaduto_mette_in_coda_e_avanza(session, queue_):
    utente, folder = queue_
    flusso = _schedulato(session, utente, folder)
    era = flusso.next_run_at
    await scheduler._fire_flow(session, flusso, datetime.now(timezone.utc))
    (run,) = _in_coda(session, flusso.id)
    assert (run.status, run.trigger_type, run.engine_mode, run.launched_by) == ("PENDING", "schedule", "production", utente.id)
    assert session.get(Flow, flusso.id).next_run_at > era


async def test_due_processi_sullo_stesso_slot_lanciano_una_volta(session, queue_, db_engine):
    """Entrambi hanno letto il flusso come scaduto. Il primo sposta in avanti la
    prossima esecuzione; il secondo la trova spostata e lascia stare."""
    utente, folder = queue_
    flusso = _schedulato(session, utente, folder)
    now = datetime.now(timezone.utc)
    with Session(db_engine) as a, Session(db_engine) as b:
        visto_da_a = a.get(Flow, flusso.id)
        visto_da_b = b.get(Flow, flusso.id)
        assert visto_da_a.next_run_at == visto_da_b.next_run_at  # lo stesso slot
        await scheduler._fire_flow(a, visto_da_a, now)
        await scheduler._fire_flow(b, visto_da_b, now)
    assert len(_in_coda(session, flusso.id)) == 1


async def test_se_il_giro_precedente_non_e_finito_lo_slot_si_salta(session, queue_):
    utente, folder = queue_
    flusso = _schedulato(session, utente, folder)
    era = flusso.next_run_at
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id, claimed_by="x", heartbeat_at=orch._now())
    await scheduler._fire_flow(session, flusso, datetime.now(timezone.utc))
    assert len(_in_coda(session, flusso.id)) == 1  # solo quella che c'era
    assert session.get(Flow, flusso.id).next_run_at > era  # ma lo slot è consumato


async def test_un_autore_disattivato_spegne_la_schedulazione(session, queue_):
    utente, folder = queue_
    flusso = _schedulato(session, utente, folder)
    utente.is_active = False
    session.add(utente); session.commit()
    await scheduler._fire_flow(session, flusso, datetime.now(timezone.utc))
    assert _in_coda(session, flusso.id) == []
    spento = session.get(Flow, flusso.id)
    assert spento.run_schedule is None and spento.next_run_at is None


# ── cosa fa questo processo ──────────────────────────────────────────────────
async def test_un_processo_che_risponde_soltanto_non_avvia_lavoro_di_fondo(monkeypatch):
    import app.main as main
    from app.core.config import get_settings

    avviati = []

    def finto(name):
        async def ciclo(stop):
            avviati.append(name)
        return ciclo

    cicli = ["scheduler_loop", "queue_loop", "retention_loop", "housekeeping_loop", "backup_loop", "webhooks_loop", "engine_settings_loop", "siem_loop"]
    for name in cicli:
        monkeypatch.setattr(main, name, finto(name))

    for ruolo, attesi in (("api", []), ("all", cicli), ("orchestrator", cicli)):
        avviati.clear()
        monkeypatch.setenv("APP__ROLE", ruolo)
        get_settings.cache_clear()
        tasks = main.start_background_work(asyncio.Event())
        await asyncio.gather(*tasks)
        assert avviati == attesi, ruolo


# ── rilievi della revisione avversaria (2026-10-05) ──────────────────────────
# Ognuno di questi test fissa un difetto che i test sopra non vedevano.

async def test_una_riga_firmata_il_cui_task_e_morto_smette_di_battere(session, queue_, monkeypatch):
    """Il battito è delle esecuzioni che hanno un task vivo, non del processo. Se
    il task muore senza chiudere il run (il database cade mentre scrive l'esito)
    e il processo resta in piedi, battere «per firma» terrebbe quella riga viva
    per sempre: nessuno la chiuderebbe e l'indice unico bloccherebbe il flusso."""
    utente, folder = queue_
    flusso = _flusso(session, utente, folder)
    run = orch.create_orchestration_run(session, utente, flusso)
    orch.claim(5)  # firmata da questo processo…
    assert orch._alive == set()  # …ma nessun task la sta eseguendo
    assert orch.beat() == 0
    # passato il tempo, è un'orfana come le altre
    old = session.get(Run, run.id)
    old.heartbeat_at = orch._now() - timedelta(seconds=orch._orc().dead_after_seconds + 5)
    session.add(old); session.commit()
    assert await orch.close_orphans() == 1
    new = orch.create_orchestration_run(session, utente, flusso)
    assert orch.claim(5) == ([new.id], [])  # e il flusso riparte


async def test_il_registro_dei_task_vivi_segue_i_task(session, queue_, monkeypatch):
    utente, folder = queue_
    run = orch.create_orchestration_run(session, utente, _flusso(session, utente, folder))
    orch.claim(5)
    dentro = asyncio.Event(); via = asyncio.Event()

    async def lunga(*a, **kw):
        dentro.set()
        await via.wait()
        return []

    monkeypatch.setattr(orch, "orchestrate", lunga)
    task = orch._start(run.id)
    await dentro.wait()
    assert orch._alive == {run.id} and orch.beat() == 1
    via.set()
    await task
    await asyncio.sleep(0)  # lascia girare la callback di fine task
    assert orch._alive == set() and orch.beat() == 0
    assert _state(session, run.id).status == "SUCCESS"


def test_una_presa_interrotta_a_meta_torna_quelle_gia_prese(session, queue_, monkeypatch):
    """Se sollevasse, chi chiama non saprebbe di averle: firmate, e nessuno le esegue."""
    utente, folder = queue_
    runs = [orch.create_orchestration_run(session, utente, _flusso(session, utente, folder, f"f{i}")).id for i in range(3)]
    vero = orch._now
    chiamate = []

    def now():
        chiamate.append(1)
        if len(chiamate) == 2:
            raise RuntimeError("database caduto")
        return vero()

    monkeypatch.setattr(orch, "_now", now)
    taken, refused = orch.claim(5)
    assert taken == runs[:1] and refused == []
    assert _state(session, runs[1]).status == "PENDING"


async def test_il_respinto_che_nel_frattempo_e_partito_non_viene_chiuso(session, queue_):
    """Fra il rifiuto dell'indice e la chiusura come «già in esecuzione», l'altra
    esecuzione può finire e un altro processo può prendere questa: sta girando,
    e chiuderla la farebbe risultare mai partita."""
    utente, folder = queue_
    partita = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder).id, claimed_by="un-altro", heartbeat_at=orch._now())
    await orch._finalize_orch_run(partita.id, "FAILURE", orch.ALREADY_RUNNING, only_if_queued=True)
    assert _state(session, partita.id).status == "STARTED"
    in_coda = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flusso(session, utente, folder, "b").id)
    await orch._finalize_orch_run(in_coda.id, "FAILURE", orch.ALREADY_RUNNING, only_if_queued=True)
    assert _state(session, in_coda.id).status == "FAILURE"


def test_all_avvio_si_chiudono_le_esecuzioni_di_un_incarnazione_precedente(session, queue_, monkeypatch):
    """Il container che riparte dopo un crash: stesso host, stesso pid, altra firma.
    Quel processo non c'è più di sicuro, e non serve aspettare il battito."""
    utente, folder = queue_
    _come(monkeypatch, "host-a:1:nuova")
    now = orch._now()
    def row(name, sign):
        return make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder, name).id, claimed_by=sign, heartbeat_at=now).id
    previous = row("a", "host-a:1:vecchia")
    altro_host = row("b", "host-b:1:vecchia")       # un altro orchestratore: può essere vivo
    altro_pid = row("c", "host-a:7:vecchia")        # un altro processo sulla stessa macchina
    simile = row("d", "host-a:12:vecchia")          # «host-a:1» è un prefisso di «host-a:12»: non deve bastare
    mia = row("e", "host-a:1:nuova")
    assert len(orch.close_mine(True)) == 1
    assert _state(session, previous).status == "FAILURE" and _state(session, previous).error == ORCHESTRATION_INTERRUPTED
    assert [_state(session, r).status for r in (altro_host, altro_pid, simile, mia)] == ["STARTED"] * 4


def test_allo_spegnimento_si_chiudono_tutte_le_proprie(session, queue_, monkeypatch):
    utente, folder = queue_
    _come(monkeypatch, "host-a:1:io")
    now = orch._now()
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder, "a").id, claimed_by="host-a:1:io", heartbeat_at=now).id
    altrui = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flusso(session, utente, folder, "b").id, claimed_by="host-b:1:x", heartbeat_at=now).id
    assert len(orch.close_mine(False)) == 1
    assert (_state(session, mia).status, _state(session, altrui).status) == ("FAILURE", "STARTED")


# lo scheduler: dal SECONDO slot dello stesso giro in poi
def _ds_schedulata(session, utente, folder, conn, name):
    from app.models import Permission
    from tests.conftest import make_datasource

    return make_datasource(
        session, name=name, project_id=folder.id, kind="database", connection_id=conn.id,
        refresh_schedule="0 3 * * *", refresh_scheduled_by=utente.id,
        next_refresh_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
    )


async def test_il_secondo_slot_del_giro_non_si_prende_su_un_valore_riletto(session, queue_, db_engine, monkeypatch):
    """Il difetto: `seen` veniva letto dall'oggetto DOPO il commit del primo slot,
    che fa scadere tutto — quindi era il valore già spostato dall'altro processo,
    e il confronto riusciva sempre. Qui A lavora il primo slot mentre B prende il
    secondo; quando A arriva al secondo NON deve lanciarlo."""
    from app.models import Connection, Datasource

    utente, folder = queue_
    conn = Connection(name="c", db_type="postgresql", project_id=folder.id, owner_id=utente.id, host="h", port=5432, username="u", database="d")
    session.add(conn); session.commit(); session.refresh(conn)
    ds1 = _ds_schedulata(session, utente, folder, conn, "uno")
    ds2 = _ds_schedulata(session, utente, folder, conn, "due")
    lanciati = []

    async def finto_lancio(session, user, ds, conn, trigger_type="manual"):
        lanciati.append(ds.id)

    monkeypatch.setattr(scheduler, "launch_ingest_run", finto_lancio)
    now = datetime.now(timezone.utc)
    with Session(db_engine) as a, Session(db_engine) as b:
        # A fa la query degli scaduti, come il tick
        scaduti_a = [(d, d.next_refresh_at) for d in a.exec(select(Datasource).where(Datasource.id.in_([ds1.id, ds2.id])).order_by(Datasource.id)).all()]
        await scheduler._fire_ds(a, scaduti_a[0][0], now, scaduti_a[0][1])   # A: primo slot (commit → oggetti scaduti)
        await scheduler._fire_ds(b, b.get(Datasource, ds2.id), now)          # B: prende il secondo
        await scheduler._fire_ds(a, scaduti_a[1][0], now, scaduti_a[1][1])   # A: arriva al secondo
    assert lanciati == [ds1.id, ds2.id]  # ds2 una volta sola


async def test_anche_senza_il_valore_passato_uno_slot_gia_spostato_non_si_riprende(session, queue_, db_engine, monkeypatch):
    """La seconda difesa: l'UPDATE chiede che lo slot sia ancora scaduto. Uno già
    preso sta nel futuro, quindi non combacia nemmeno se lo si rilegge."""
    utente, folder = queue_
    flusso = _schedulato(session, utente, folder)
    now = datetime.now(timezone.utc)
    with Session(db_engine) as b:
        await scheduler._fire_flow(b, b.get(Flow, flusso.id), now)
    session.expire_all()
    riletto = session.get(Flow, flusso.id)       # legge il valore già spostato da B
    # la prima esecuzione è già finita: `flow_running` non farebbe da rete
    for r in _in_coda(session, flusso.id):
        r.status = "SUCCESS"; session.add(r)
    session.commit()
    await scheduler._fire_flow(session, session.get(Flow, flusso.id), now)
    assert len(_in_coda(session, flusso.id)) == 1


# la coda non aspetta per sempre
async def test_un_esecuzione_in_coda_da_troppo_non_parte_piu(session, queue_):
    from app.routes.runs import QUEUE_TIMED_OUT, _reconcile

    utente, folder = queue_
    vecchia = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flusso(session, utente, folder, "a").id,
                       started_at=datetime.now(timezone.utc) - timedelta(seconds=orch._orc().queue_timeout_seconds + 60))
    fresca = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flusso(session, utente, folder, "b").id)
    # chi la guarda (anche un processo che risponde soltanto) la trova chiusa
    letta = await _reconcile(session, session.get(Run, vecchia.id))
    assert (letta.status, letta.error) == ("FAILURE", QUEUE_TIMED_OUT)
    assert (await _reconcile(session, session.get(Run, fresca.id))).status == "PENDING"
    assert orch.claim(5) == ([fresca.id], [])


async def test_l_orchestratore_chiude_la_coda_scaduta_prima_di_prenderla(session, queue_):
    utente, folder = queue_
    vecchia = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flusso(session, utente, folder).id,
                       started_at=datetime.now(timezone.utc) - timedelta(seconds=orch._orc().queue_timeout_seconds + 60))
    await orch.close_orphans()
    assert _state(session, vecchia.id).status == "FAILURE"
    assert orch.claim(5) == ([], [])


def test_a_dead_after_lowered_from_the_panel_keeps_the_old_one_while_the_others_catch_up(monkeypatch):
    # 120 s → 20 s: another orchestrator reads it up to REREAD_EVERY later and beat at the
    # old pace until then; its live runs are not orphans meanwhile
    from app.core.overrides import REREAD_EVERY

    clock, dead = [1000.0], [120]
    monkeypatch.setattr(orch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(orch, "_orc", lambda: SimpleNamespace(dead_after_seconds=dead[0]))
    monkeypatch.setattr(orch, "_first_threshold", None)
    monkeypatch.setattr(orch, "_last_threshold", None)
    assert orch.death_threshold() == 120
    dead[0] = 20
    assert orch.death_threshold() == 120
    clock[0] += 120 + REREAD_EVERY - 1
    assert orch.death_threshold() == 120
    clock[0] += 2
    assert orch.death_threshold() == 20
    dead[0] = 300  # raised: the new one at once
    assert orch.death_threshold() == 300
