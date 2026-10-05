"""Data contracts nel gateway: la forma del documento, le versioni, chi può
leggere e chi scrivere, e lo stato che gli elenchi di datasource portano con sé.

Il gateway non valuta: qui l'engine è finto e risponde il referto che il test gli
dà. Quello che si prova è che cosa il gateway ne fa.
"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import DataContract, DataContractResult, DataContractVersion
from app.models.permission import Capability
from app.routes import contracts as routes
from app.routes.datasources import delete_datasource, list_all_datasources
from app.services import contracts
from tests.conftest import make_datasource, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio

REGOLE = [
    {"kind": "column", "column": "id", "dtype": "integer", "severity": "error"},
    {"kind": "not_null", "column": "id", "severity": "error"},
    {"kind": "accepted_values", "column": "stato", "values": ["a", "b"], "severity": "warning"},
]
RICHIESTA = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"), url="", method="PUT")


@pytest.fixture
def scena(session):
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    cartella = make_project(session, name="dati", owner_id=capo.id)
    autore = make_user(session, email="autore@x.it")
    lettore = make_user(session, email="lettore@x.it")
    estraneo = make_user(session, email="fuori@x.it")
    make_permission(session, user_id=autore.id, project_id=cartella.id, capability=Capability.EDIT)
    make_permission(session, user_id=lettore.id, project_id=cartella.id, capability=Capability.VIEW)
    ds = make_datasource(session, name="ordini", project_id=cartella.id, key="datasets/1/v1.parquet", rows=3)
    # Le rotte che aspettano l'engine CHIUDONO la sessione prima di aspettare (in
    # produzione è quella della richiesta; qui è l'unica del test). Gli oggetti
    # della scena si staccano già caricati, così restano leggibili dopo.
    for oggetto in (capo, cartella, autore, lettore, estraneo, ds):
        session.refresh(oggetto)
    session.expunge_all()
    return SimpleNamespace(ds=ds, autore=autore, lettore=lettore, estraneo=estraneo, cartella=cartella)


async def _salva(session, chi, ds, regole=REGOLE, enabled=True):
    return await routes.put_contract(ds.id, routes.ContractIn(document={"rules": regole}, enabled=enabled), RICHIESTA, chi, session)


# ── la forma del contratto ───────────────────────────────────────────────────
def test_la_forma_canonica_tiene_solo_i_campi_noti_e_da_un_id_a_ogni_regola():
    doc = contracts.validate_document({"description": "x", "boh": 1, "rules": [
        {"kind": "not_null", "column": "a", "severity": "error", "inventato": True},
        {"id": "chiave", "kind": "unique", "column": "a", "severity": "warning"},   # forma breve
        {"id": "chiave", "kind": "row_count", "min": 1, "severity": "error"},       # id doppio
    ]})
    assert doc == {"description": "x", "rules": [
        {"id": "r1", "kind": "not_null", "severity": "error", "column": "a"},
        {"id": "chiave", "kind": "unique", "severity": "warning", "columns": ["a"]},
        {"id": "r3", "kind": "row_count", "severity": "error", "min": 1},
    ]}


@pytest.mark.parametrize("regola, codice", [
    ({"kind": "inventata", "severity": "error"}, "unknown_kind"),
    ({"kind": "not_null", "column": "a", "severity": "info"}, "bad_severity"),       # due sole severità
    ({"kind": "not_null", "column": "a", "severity": "blocker"}, "bad_severity"),
    ({"kind": "not_null", "column": "a"}, "bad_severity"),
    ({"kind": "not_null", "severity": "error"}, "missing_column"),
    ({"kind": "column", "column": "a", "dtype": "Int64", "severity": "error"}, "bad_dtype"),
    ({"kind": "unique", "columns": [], "severity": "error"}, "missing_column"),
    ({"kind": "accepted_values", "column": "a", "values": [], "severity": "error"}, "empty_values"),
    ({"kind": "accepted_values", "column": "a", "values": list(range(501)), "severity": "error"}, "too_many_values"),
    ({"kind": "range", "column": "a", "severity": "error"}, "no_bounds"),
    ({"kind": "range", "column": "a", "min": 5, "max": 1, "severity": "error"}, "bad_bounds"),
    ({"kind": "row_count", "min": -1, "severity": "error"}, "bad_bounds"),
    ({"kind": "pattern", "column": "a", "regex": "([a", "severity": "error"}, "bad_regex"),
    ({"kind": "freshness", "max_age_hours": 0, "severity": "warning"}, "bad_max_age"),
    ({"kind": "expression", "sql": "  ", "severity": "error"}, "empty_expression"),
])
def test_una_regola_scritta_male_dice_quale_e_perche(regola, codice):
    buona = {"kind": "not_null", "column": "a", "severity": "error"}
    with pytest.raises(contracts.ContractInvalid) as e:
        contracts.validate_document({"rules": [buona, regola]})
    assert e.value.errors == [{"index": 1, "code": codice}]


def test_cio_che_non_e_un_contratto_e_i_contratti_smisurati():
    for cosa in ("testo", [], {"rules": "no"}):
        with pytest.raises(contracts.ContractInvalid) as e:
            contracts.validate_document(cosa)
        assert e.value.errors[0]["code"] == "not_a_contract"
    with pytest.raises(contracts.ContractInvalid) as e:
        contracts.validate_document({"rules": [{"kind": "row_count", "min": 1, "severity": "error"}] * 201})
    assert e.value.errors[0]["code"] == "too_many_rules"
    assert contracts.validate_document({}) == {"description": "", "rules": []}


# ── scrivere, leggere, versioni ──────────────────────────────────────────────
async def test_salvare_crea_il_contratto_lo_verifica_e_lo_stato_e_il_referto(session, scena, fake_engine):
    fake_engine.contract_response = (200, {"outcome": "warning", "rows": 3, "errors": 0, "warnings": 1, "rules": [
        {"id": "r3", "kind": "accepted_values", "severity": "warning", "passed": False, "violations": 1}]})
    out = await _salva(session, scena.autore, scena.ds)
    assert (out.version, out.status, out.errors, out.warnings, out.blocked) == (1, "warning", 0, 1, False)
    assert out.report["rules"][0]["violations"] == 1 and out.checked_at is not None
    # all'engine: lo snapshot che la datasource sta servendo, e il contratto in forma canonica
    path, corpo = fake_engine.contract_calls[0]
    assert path == "/contracts/evaluate" and corpo["key"] == "datasets/1/v1.parquet"
    assert [r["id"] for r in corpo["document"]["rules"]] == ["r1", "r2", "r3"]
    (storico,) = session.exec(__import__("sqlmodel").select(DataContractResult)).all()
    assert (storico.trigger, storico.outcome, storico.blocked) == ("save", "warning", False)


async def test_ogni_documento_diverso_e_una_versione_e_riporta_lo_stato_a_da_verificare(session, scena, fake_engine):
    from sqlmodel import select

    await _salva(session, scena.autore, scena.ds)
    stesso = await _salva(session, scena.autore, scena.ds)
    assert stesso.version == 1 and len(fake_engine.contract_calls) == 1   # niente è cambiato: niente versione, niente verifica
    fake_engine.contract_response = (503, {"detail": "engine giù"})
    nuovo = await _salva(session, scena.autore, scena.ds, REGOLE[:2])
    assert (nuovo.version, nuovo.status, nuovo.report) == (2, "pending", None)
    assert "engine giù" in nuovo.check_error   # salvato lo stesso: la verifica è un di più
    assert [v.version for v in session.exec(select(DataContractVersion).order_by(DataContractVersion.version)).all()] == [1, 2]


async def test_un_contratto_non_valido_non_si_salva(session, scena, fake_engine):
    with pytest.raises(HTTPException) as e:
        await _salva(session, scena.autore, scena.ds, [{"kind": "not_null", "severity": "error"}])
    assert e.value.status_code == 422 and e.value.detail["errors"] == [{"index": 0, "code": "missing_column"}]
    assert contracts.get(session, scena.ds.id) is None and fake_engine.contract_calls == []


async def test_un_contratto_spento_o_vuoto_non_si_verifica_e_non_viaggia(session, scena, fake_engine):
    await _salva(session, scena.autore, scena.ds, enabled=False)
    assert fake_engine.contract_calls == []
    assert contracts.payload(session, scena.ds.id) is None and contracts.summaries(session, [scena.ds.id]) == {}
    await _salva(session, scena.autore, scena.ds, [], enabled=True)
    assert contracts.payload(session, scena.ds.id) is None   # niente regole: niente da far valutare


# ── chi può ──────────────────────────────────────────────────────────────────
async def test_chi_legge_vede_il_contratto_ma_non_lo_scrive(session, scena, fake_engine):
    await _salva(session, scena.autore, scena.ds)
    assert routes.get_contract(scena.ds.id, scena.lettore, session).version == 1
    # l'interfaccia mostra i comandi solo a chi li può usare: lo dice il server
    assert routes.get_contract(scena.ds.id, scena.lettore, session).editable is False
    assert routes.get_contract(scena.ds.id, scena.autore, session).editable is True
    assert len(routes.contract_history(scena.ds.id, 30, False, scena.lettore, session)) == 1
    for azione in (
        lambda: _salva(session, scena.lettore, scena.ds),
        lambda: routes.check_contract(scena.ds.id, scena.lettore, session),
        lambda: routes.propose_contract(scena.ds.id, scena.lettore, session),
    ):
        with pytest.raises(HTTPException) as e:
            await azione()
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        routes.delete_contract(scena.ds.id, RICHIESTA, scena.lettore, session)
    assert e.value.status_code == 403


async def test_un_estraneo_non_sa_nemmeno_se_c_e(session, scena, fake_engine):
    await _salva(session, scena.autore, scena.ds)
    for azione in (lambda: routes.get_contract(scena.ds.id, scena.estraneo, session),
                   lambda: routes.contract_history(scena.ds.id, 30, False, scena.estraneo, session)):
        with pytest.raises(HTTPException) as e:
            azione()
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        routes.get_contract(99999, scena.autore, session)
    assert e.value.status_code == 404


async def test_senza_contratto_la_lettura_dice_404(session, scena):
    with pytest.raises(HTTPException) as e:
        routes.get_contract(scena.ds.id, scena.lettore, session)
    assert e.value.status_code == 404


# ── verifica adesso, proposta ────────────────────────────────────────────────
async def test_verifica_adesso_aggiorna_lo_stato(session, scena, fake_engine):
    await _salva(session, scena.autore, scena.ds)
    fake_engine.contract_response = (200, {"outcome": "failed", "rows": 0, "errors": 1, "warnings": 0, "rules": []})
    out = await routes.check_contract(scena.ds.id, scena.autore, session)
    assert (out.status, out.errors, out.blocked) == ("failed", 1, False)   # i dati correnti violano: nessun aggiornamento rifiutato
    fake_engine.contract_response = (504, {"detail": "The dataset is too large to be checked interactively"})
    out = await routes.check_contract(scena.ds.id, scena.autore, session)
    assert out.status == "failed" and "too large" in out.check_error      # la verifica non riuscita non cambia lo stato


async def test_la_proposta_arriva_dai_dati_e_non_salva_niente(session, scena, fake_engine):
    fake_engine.profile_response = (200, {"profile": {"rows": 3, "columns": [{"name": "id"}]},
                                          "proposal": {"description": "", "rules": [{"id": "r1", "kind": "column", "column": "id", "severity": "error"}]}})
    out = await routes.propose_contract(scena.ds.id, scena.autore, session)
    assert out["document"]["rules"][0]["column"] == "id" and out["profile"]["rows"] == 3
    assert contracts.get(session, scena.ds.id) is None
    assert contracts.validate_document(out["document"])   # ciò che si propone è un contratto valido


# ── lo stato negli elenchi: ciò che alimenta l'icona ─────────────────────────
async def test_ogni_elenco_di_datasource_porta_lo_stato_del_contratto(session, scena, fake_engine):
    altra = make_datasource(session, name="senza-contratto", project_id=scena.cartella.id, key="datasets/2/v1.parquet")
    await _salva(session, scena.autore, scena.ds)
    per_nome = {d.name: d for d in list_all_datasources(scena.lettore, session)}
    assert per_nome["senza-contratto"].contract is None
    assert (per_nome["ordini"].contract.status, per_nome["ordini"].contract.version) == ("passed", 1)


def test_lo_stato_di_tante_datasource_costa_una_query(session, db_engine, scena):
    from sqlalchemy import event

    for i in range(20):
        d = make_datasource(session, name=f"d{i}", project_id=scena.cartella.id, key=f"datasets/{i}.parquet")
        contracts.save(session, d, {"description": "", "rules": []}, True, None)
    session.commit()
    comandi = []
    event.listen(db_engine, "before_cursor_execute", lambda c, cur, stmt, *a: comandi.append(stmt))
    assert len(contracts.summaries(session, range(1, 40))) == 20
    assert len(comandi) == 1


@pytest.mark.parametrize("stato, rifiutato, icona", [
    ("pending", False, "pending"), ("passed", False, "passed"), ("warning", False, "warning"), ("failed", False, "failed"),
    ("passed", True, "failed"),    # i dati reggono, ma l'ultimo aggiornamento è stato rifiutato: non sono più attuali
    ("warning", True, "failed"),
])
def test_cio_che_l_icona_dice(session, scena, stato, rifiutato, icona):
    from datetime import datetime, timezone

    c = contracts.save(session, scena.ds, {"description": "", "rules": []}, True, None)
    c.status = stato
    c.blocked_at = datetime.now(timezone.utc) if rifiutato else None
    assert contracts.summary(c)["status"] == icona and contracts.summary(c)["blocked"] is rifiutato


# ── togliere ─────────────────────────────────────────────────────────────────
async def test_togliere_il_contratto_lascia_lo_storico_togliere_la_datasource_no(session, scena, fake_engine):
    from sqlmodel import select

    await _salva(session, scena.autore, scena.ds)
    routes.delete_contract(scena.ds.id, RICHIESTA, scena.autore, session)
    assert contracts.get(session, scena.ds.id) is None
    assert len(session.exec(select(DataContractResult)).all()) == 1 and len(session.exec(select(DataContractVersion)).all()) == 1
    # ricreato: la numerazione prosegue, non ricomincia
    assert (await _salva(session, scena.autore, scena.ds)).version == 2
    await delete_datasource(scena.ds.id, RICHIESTA, scena.autore, session)
    for modello in (DataContract, DataContractVersion, DataContractResult):
        assert session.exec(select(modello)).all() == []
