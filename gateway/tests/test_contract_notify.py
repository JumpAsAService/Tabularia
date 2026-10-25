"""Avvisi dei data contract: chi viene avvisato, quando, e quando NO.

Come per i flussi, la parte che conta è il silenzio: un avviso a ogni valutazione
verrebbe filtrato, e allora non servirebbe più. Si avvisa al cambio di stato, per
ciò che accade da solo (dati nuovi, il tempo che passa).
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, Connection, DataContractResult
from app.models.permission import Capability
from app.routes import contracts as routes
from app.services import contract_notifier, contracts
from tests.conftest import make_datasource, make_permission, make_project, make_user

REGOLE = [{"kind": "row_count", "max": 10, "severity": "error"}, {"kind": "not_null", "column": "id", "severity": "warning"}]
RICHIESTA = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"), url="", method="PUT")

BENE = {"outcome": "passed", "rows": 3, "errors": 0, "warnings": 0, "rules": []}
AVVISO = {"outcome": "warning", "rows": 3, "errors": 0, "warnings": 1, "rules": [
    {"id": "r2", "kind": "not_null", "column": "id", "severity": "warning", "passed": False, "violations": 2}]}
ROTTO = {"outcome": "failed", "rows": 50, "errors": 1, "warnings": 0, "rules": [
    {"id": "r1", "kind": "row_count", "severity": "error", "passed": False, "observed": 50, "expected": {"min": None, "max": 10}}]}


@pytest.fixture
def scena(session, monkeypatch):
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    folder = make_project(session, name="dati", owner_id=capo.id)
    altrove = make_project(session, name="posta", owner_id=capo.id)
    author = make_user(session, email="autore@x.it")
    make_permission(session, user_id=author.id, project_id=folder.id, capability=Capability.EDIT)
    lettore = make_user(session, email="lettore@x.it")
    make_permission(session, user_id=lettore.id, project_id=folder.id, capability=Capability.VIEW)
    smtp = Connection(name="posta", db_type="smtp", project_id=altrove.id, owner_id=capo.id, host="smtp.x.it", port=587,
                      username="u", extra='{"from_address": "tabularia@x.it", "allowed_domains": ["x.it"]}')
    session.add(smtp)
    session.commit()
    ds = make_datasource(session, name="ordini", project_id=folder.id, key="datasets/1/v1.parquet", rows=3)
    c = contracts.save(session, ds, contracts.validate_document({"rules": REGOLE}), True, capo.id, notify=("alice@x.it, bruno@x.it", smtp.id))
    contracts.record_result(session, c, BENE, trigger="save")
    session.commit()

    sent = []

    class _Client:
        answer = 200

        async def post(self, url, json=None, timeout=None):
            sent.append({"url": url, **(json or {})})
            return SimpleNamespace(status_code=self.answer, text="no")

    monkeypatch.setattr(contract_notifier, "get_engine_client", lambda: _Client())
    # le rotte che aspettano l'engine chiudono la sessione prima di aspettare: gli
    # oggetti della scena si staccano già caricati (vedi test_contracts.scena)
    for o in (capo, author, lettore, smtp, ds, folder):
        session.refresh(o)
    session.expunge_all()
    return SimpleNamespace(ds=ds, capo=capo, author=author, lettore=lettore, smtp=smtp, sent=sent, client=_Client)


def _referto(session, scena, report, trigger="refresh", **k):
    r = contracts.record_result(session, contracts.get(session, scena.ds.id), report, trigger=trigger, **k)
    session.commit()
    return r


def _consegna(session):
    return asyncio.run(contract_notifier.deliver_pending(session))


def _stati(session):
    return [r.notify for r in session.exec(select(DataContractResult).order_by(DataContractResult.id)).all()]


# ── quando ───────────────────────────────────────────────────────────────────
def test_un_aggiornamento_rifiutato_avvisa_e_dice_quale_regola(session, scena):
    _referto(session, scena, ROTTO, blocked=True, run_id=41)
    assert _consegna(session) == 1
    (m,) = scena.sent
    assert m["url"] == "/db/notify" and m["to"] == ["alice@x.it", "bruno@x.it"]
    # alice and bruno are not users: English (see test_email_language.py for the others)
    assert m["subject"] == "[Tabularia] Data contract of «ordini»: update refused"
    assert "was NOT published" in m["body"] and "run #41" in m["body"]
    assert "[blocking] row_count: observed 50" in m["body"]
    assert _stati(session) == [None, "sent"]
    (item,) = session.exec(select(AuditLog).where(AuditLog.action == "email.send")).all()
    assert item.target_type == "datasource" and '"contract_state"' in item.detail and '"refused"' in item.detail


def test_non_ripete_finche_lo_stato_resta_quello(session, scena):
    _referto(session, scena, ROTTO, blocked=True)
    _referto(session, scena, ROTTO, blocked=True)      # la notte dopo: rifiutato di nuovo
    _referto(session, scena, ROTTO, blocked=True)
    assert _consegna(session) == 1 and len(scena.sent) == 1
    assert _stati(session) == [None, "sent", None, None]


def test_avvisa_quando_torna_a_posto_e_quando_si_guasta_di_nuovo(session, scena):
    _referto(session, scena, ROTTO, blocked=True)
    _referto(session, scena, BENE)                      # refresh riuscito: il rifiuto è superato
    _referto(session, scena, AVVISO)
    _referto(session, scena, AVVISO)
    assert _consegna(session) == 3
    assert [m["subject"].rsplit(": ", 1)[1] for m in scena.sent] == ["update refused", "met again", "rules not met (warning)"]
    assert "[warning] not_null on id: 2 rows" in scena.sent[2]["body"]


def test_chi_salva_o_verifica_a_mano_non_riceve_niente(session, scena):
    _referto(session, scena, ROTTO, trigger="save")
    _referto(session, scena, AVVISO, trigger="manual")
    assert _consegna(session) == 0 and scena.sent == [] and _stati(session) == [None, None, None]


def test_la_prima_verifica_andata_bene_non_e_una_notizia(session, scena):
    c = contracts.save(session, scena.ds, contracts.validate_document({"rules": REGOLE[:1]}), True, scena.capo.id)   # documento nuovo: «pending»
    assert c.status == "pending"
    _referto(session, scena, BENE)
    assert _consegna(session) == 0
    _referto(session, scena, AVVISO)                    # ma da «pending» a un guasto sì
    assert _consegna(session) == 1


def test_la_freschezza_che_scade_avvisa_senza_che_arrivi_niente(session, scena):
    _referto(session, scena, {**ROTTO, "rules": [{"id": "r9", "kind": "freshness", "severity": "error", "passed": False, "observed": 30.5, "expected": 24}]}, trigger="freshness")
    assert _consegna(session) == 1
    assert "contract broken" in scena.sent[0]["subject"] and "freshness (the time" in scena.sent[0]["body"]
    assert "[blocking] freshness: observed 30.5" in scena.sent[0]["body"]


def test_senza_destinatari_o_senza_connessione_non_si_segna_niente(session, scena):
    c = contracts.get(session, scena.ds.id)
    c.notify_emails = None
    session.add(c)
    session.commit()
    _referto(session, scena, ROTTO, blocked=True)
    assert _stati(session) == [None, None] and _consegna(session) == 0


# ── la consegna ──────────────────────────────────────────────────────────────
def test_un_avviso_gia_preso_da_un_altro_processo_non_parte_due_volte(session, scena):
    r = _referto(session, scena, ROTTO, blocked=True)
    assert contract_notifier._mark(session, r.id, "pending", "sending")      # un altro processo lo ha preso
    assert _consegna(session) == 0 and scena.sent == []


def test_se_la_posta_rifiuta_resta_scritto_e_non_si_riprova_all_infinito(session, scena):
    scena.client.answer = 502
    _referto(session, scena, ROTTO, blocked=True)
    assert _consegna(session) == 0 and _stati(session) == [None, "failed"]
    assert _consegna(session) == 0 and len(scena.sent) == 1


def test_un_avviso_rotto_non_ferma_gli_altri(session, scena, monkeypatch):
    _referto(session, scena, ROTTO, blocked=True)
    _referto(session, scena, BENE)
    vero, chiamate = contract_notifier._send, []

    async def _a_volte(session, rid):
        chiamate.append(rid)
        if len(chiamate) == 1:
            raise RuntimeError("posta giù")
        return await vero(session, rid)

    monkeypatch.setattr(contract_notifier, "_send", _a_volte)
    assert _consegna(session) == 1 and _stati(session) == [None, "failed", "sent"]


def test_una_posta_lenta_non_tiene_fermo_lo_scheduler(session, scena):
    _referto(session, scena, ROTTO, blocked=True)
    _referto(session, scena, BENE)
    # tempo del giro già esaurito dopo il primo: il secondo aspetta il giro dopo, non si perde
    assert asyncio.run(contract_notifier.deliver_pending(session, budget_seconds=-1)) == 0
    assert _stati(session) == [None, "pending", "pending"]
    assert _consegna(session) == 2


def test_connessione_o_contratto_spariti_l_avviso_si_salta(session, scena):
    _referto(session, scena, ROTTO, blocked=True)
    session.delete(session.get(Connection, scena.smtp.id))
    session.commit()
    assert _consegna(session) == 0 and _stati(session) == [None, "skipped"] and scena.sent == []


# ── chi può chiedere di essere avvisato ──────────────────────────────────────

async def _salva(session, chi, ds, **notify):
    return await routes.put_contract(ds.id, routes.ContractIn(document={"rules": REGOLE}, enabled=True, **notify), RICHIESTA, chi, session)


@pytest.mark.anyio
async def test_gli_indirizzi_passano_dalle_barriere_della_connessione(session, scena, fake_engine):
    out = await _salva(session, scena.capo, scena.ds, notify_emails="carla@x.it; dario@x.it", notify_connection_id=scena.smtp.id)
    assert (out.notify_emails, out.notify_connection_id) == ("carla@x.it, dario@x.it", scena.smtp.id)
    with pytest.raises(HTTPException) as e:
        await _salva(session, scena.capo, scena.ds, notify_emails="fuori@altrove.com")
    assert e.value.status_code == 422 and "altrove.com" not in "x.it" and "fuori@altrove.com" in e.value.detail
    with pytest.raises(HTTPException) as e:
        await _salva(session, scena.capo, scena.ds, notify_emails="non-un-indirizzo")
    assert e.value.status_code == 422
    assert contracts.get(session, scena.ds.id).notify_emails == "carla@x.it, dario@x.it"     # i rifiuti non hanno cambiato niente


@pytest.mark.anyio
async def test_scegliere_una_connessione_chiede_connect_tenerla_no(session, scena, fake_engine):
    c = contracts.get(session, scena.ds.id)
    c.notify_connection_id = None
    session.add(c)
    session.commit()
    with pytest.raises(HTTPException) as e:      # l'autore modifica il contratto, ma quella posta non è sua
        await _salva(session, scena.author, scena.ds, notify_connection_id=scena.smtp.id)
    assert e.value.status_code == 403
    await _salva(session, scena.capo, scena.ds, notify_connection_id=scena.smtp.id)
    out = await _salva(session, scena.author, scena.ds, notify_emails="alice@x.it, bruno@x.it", notify_connection_id=scena.smtp.id)   # rimanda tutto com'è
    assert out.notify_connection_id == scena.smtp.id
    with pytest.raises(HTTPException) as e:      # ma non decide a chi spedisce la posta di un altro
        await _salva(session, scena.author, scena.ds, notify_emails="alice@x.it, nuovo@x.it", notify_connection_id=scena.smtp.id)
    assert e.value.status_code == 403
    assert contracts.get(session, scena.ds.id).notify_emails == "alice@x.it, bruno@x.it"
    out = await _salva(session, scena.author, scena.ds, notify_connection_id=0)                                           # spegnerla sì
    assert out.notify_connection_id is None


@pytest.mark.anyio
async def test_chi_non_manda_i_campi_non_li_cambia_e_chi_legge_non_li_vede(session, scena, fake_engine):
    out = await _salva(session, scena.capo, scena.ds)
    assert (out.notify_emails, out.notify_connection_id) == ("alice@x.it, bruno@x.it", scena.smtp.id)
    seen = routes.get_contract(scena.ds.id, scena.lettore, session)
    assert (seen.notify_emails, seen.notify_connection_id, seen.editable) == ("", None, False)
    assert routes.get_contract(scena.ds.id, scena.author, session).notify_emails == "alice@x.it, bruno@x.it"
