"""Data contracts in the gateway: the document's form, the versions, who may read and who
write, and the state the datasource listings carry with them.

The gateway does not evaluate: here the engine is fake and answers the report the test
gives it. What is tested is what the gateway does with it.
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

RULES = [
    {"kind": "column", "column": "id", "dtype": "integer", "severity": "error"},
    {"kind": "not_null", "column": "id", "severity": "error"},
    {"kind": "accepted_values", "column": "stato", "values": ["a", "b"], "severity": "warning"},
]
REQUEST = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"), url="", method="PUT")


@pytest.fixture
def scene(session):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    folder = make_project(session, name="dati", owner_id=boss.id)
    author = make_user(session, email="autore@x.it")
    reader = make_user(session, email="lettore@x.it")
    stranger = make_user(session, email="fuori@x.it")
    make_permission(session, user_id=author.id, project_id=folder.id, capability=Capability.EDIT)
    make_permission(session, user_id=reader.id, project_id=folder.id, capability=Capability.VIEW)
    ds = make_datasource(session, name="ordini", project_id=folder.id, key="datasets/1/v1.parquet", rows=3)
    # The routes that wait for the engine CLOSE the session before waiting (in production it
    # is the request's; here it is the test's only one). The scene's objects get detached
    # loaded already, so they stay readable afterwards.
    for obj in (boss, folder, author, reader, stranger, ds):
        session.refresh(obj)
    session.expunge_all()
    return SimpleNamespace(ds=ds, author=author, reader=reader, stranger=stranger, folder=folder)


async def _save(session, who, ds, rules=RULES, enabled=True):
    return await routes.put_contract(ds.id, routes.ContractIn(document={"rules": rules}, enabled=enabled), REQUEST, who, session)


# ── the contract's form ──────────────────────────────────────────────────────
def test_the_canonical_form_keeps_only_known_fields_and_gives_every_rule_an_id():
    doc = contracts.validate_document({"description": "x", "boh": 1, "rules": [
        {"kind": "not_null", "column": "a", "severity": "error", "inventato": True},
        {"id": "chiave", "kind": "unique", "column": "a", "severity": "warning"},   # short form
        {"id": "chiave", "kind": "row_count", "min": 1, "severity": "error"},       # duplicate id
    ]})
    assert doc == {"description": "x", "rules": [
        {"id": "r1", "kind": "not_null", "severity": "error", "column": "a"},
        {"id": "chiave", "kind": "unique", "severity": "warning", "columns": ["a"]},
        {"id": "r3", "kind": "row_count", "severity": "error", "min": 1},
    ]}


@pytest.mark.parametrize("rule, code", [
    ({"kind": "inventata", "severity": "error"}, "unknown_kind"),
    ({"kind": "not_null", "column": "a", "severity": "info"}, "bad_severity"),       # two severities only
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
def test_a_badly_written_rule_says_which_and_why(rule, code):
    good_one = {"kind": "not_null", "column": "a", "severity": "error"}
    with pytest.raises(contracts.ContractInvalid) as e:
        contracts.validate_document({"rules": [good_one, rule]})
    assert e.value.errors == [{"index": 1, "code": code}]


def test_what_is_not_a_contract_and_oversized_contracts():
    for what in ("testo", [], {"rules": "no"}):
        with pytest.raises(contracts.ContractInvalid) as e:
            contracts.validate_document(what)
        assert e.value.errors[0]["code"] == "not_a_contract"
    with pytest.raises(contracts.ContractInvalid) as e:
        contracts.validate_document({"rules": [{"kind": "row_count", "min": 1, "severity": "error"}] * 201})
    assert e.value.errors[0]["code"] == "too_many_rules"
    assert contracts.validate_document({}) == {"description": "", "rules": []}


# ── write, read, versions ────────────────────────────────────────────────────
async def test_saving_creates_the_contract_checks_it_and_the_state_is_the_report(session, scene, fake_engine):
    fake_engine.contract_response = (200, {"outcome": "warning", "rows": 3, "errors": 0, "warnings": 1, "rules": [
        {"id": "r3", "kind": "accepted_values", "severity": "warning", "passed": False, "violations": 1}]})
    out = await _save(session, scene.author, scene.ds)
    assert (out.version, out.status, out.errors, out.warnings, out.blocked) == (1, "warning", 0, 1, False)
    assert out.report["rules"][0]["violations"] == 1 and out.checked_at is not None
    # to the engine: the snapshot the datasource is serving, and the contract in canonical form
    path, body = fake_engine.contract_calls[0]
    assert path == "/contracts/evaluate" and body["key"] == "datasets/1/v1.parquet"
    assert [r["id"] for r in body["document"]["rules"]] == ["r1", "r2", "r3"]
    (history,) = session.exec(__import__("sqlmodel").select(DataContractResult)).all()
    assert (history.trigger, history.outcome, history.blocked) == ("save", "warning", False)


async def test_every_different_document_is_a_version_and_resets_the_state_to_pending(session, scene, fake_engine):
    from sqlmodel import select

    await _save(session, scene.author, scene.ds)
    same = await _save(session, scene.author, scene.ds)
    assert same.version == 1 and len(fake_engine.contract_calls) == 1   # nothing changed: no version, no check
    fake_engine.contract_response = (503, {"detail": "engine giù"})
    new = await _save(session, scene.author, scene.ds, RULES[:2])
    assert (new.version, new.status, new.report) == (2, "pending", None)
    assert "engine giù" in new.check_error   # saved all the same: the check is an extra
    assert [v.version for v in session.exec(select(DataContractVersion).order_by(DataContractVersion.version)).all()] == [1, 2]


async def test_an_invalid_contract_is_not_saved(session, scene, fake_engine):
    with pytest.raises(HTTPException) as e:
        await _save(session, scene.author, scene.ds, [{"kind": "not_null", "severity": "error"}])
    assert e.value.status_code == 422 and e.value.detail["errors"] == [{"index": 0, "code": "missing_column"}]
    assert contracts.get(session, scene.ds.id) is None and fake_engine.contract_calls == []


async def test_a_contract_off_or_empty_is_not_checked_and_does_not_travel(session, scene, fake_engine):
    await _save(session, scene.author, scene.ds, enabled=False)
    assert fake_engine.contract_calls == []
    assert contracts.payload(session, scene.ds.id) is None and contracts.summaries(session, [scene.ds.id]) == {}
    await _save(session, scene.author, scene.ds, [], enabled=True)
    assert contracts.payload(session, scene.ds.id) is None   # no rules: nothing to have evaluated


# ── who may ──────────────────────────────────────────────────────────────────
async def test_whoever_reads_sees_the_contract_but_does_not_write_it(session, scene, fake_engine):
    await _save(session, scene.author, scene.ds)
    assert routes.get_contract(scene.ds.id, scene.reader, session).version == 1
    # the interface shows the commands only to whoever can use them: the server says it
    assert routes.get_contract(scene.ds.id, scene.reader, session).editable is False
    assert routes.get_contract(scene.ds.id, scene.author, session).editable is True
    assert len(routes.contract_history(scene.ds.id, 30, False, scene.reader, session)) == 1
    for action in (
        lambda: _save(session, scene.reader, scene.ds),
        lambda: routes.check_contract(scene.ds.id, scene.reader, session),
        lambda: routes.propose_contract(scene.ds.id, scene.reader, session),
    ):
        with pytest.raises(HTTPException) as e:
            await action()
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        routes.delete_contract(scene.ds.id, REQUEST, scene.reader, session)
    assert e.value.status_code == 403


async def test_a_stranger_does_not_even_know_whether_there_is_one(session, scene, fake_engine):
    await _save(session, scene.author, scene.ds)
    for action in (lambda: routes.get_contract(scene.ds.id, scene.stranger, session),
                   lambda: routes.contract_history(scene.ds.id, 30, False, scene.stranger, session)):
        with pytest.raises(HTTPException) as e:
            action()
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        routes.get_contract(99999, scene.author, session)
    assert e.value.status_code == 404


async def test_without_contract_reading_says_404(session, scene):
    with pytest.raises(HTTPException) as e:
        routes.get_contract(scene.ds.id, scene.reader, session)
    assert e.value.status_code == 404


# ── check now, proposal ──────────────────────────────────────────────────────
async def test_check_now_updates_the_state(session, scene, fake_engine):
    await _save(session, scene.author, scene.ds)
    fake_engine.contract_response = (200, {"outcome": "failed", "rows": 0, "errors": 1, "warnings": 0, "rules": []})
    out = await routes.check_contract(scene.ds.id, scene.author, session)
    assert (out.status, out.errors, out.blocked) == ("failed", 1, False)   # the current data violates: no update refused
    fake_engine.contract_response = (504, {"detail": "The dataset is too large to be checked interactively"})
    out = await routes.check_contract(scene.ds.id, scene.author, session)
    assert out.status == "failed" and "too large" in out.check_error      # the failed check does not change the state


async def test_the_proposal_comes_from_the_data_and_saves_nothing(session, scene, fake_engine):
    fake_engine.profile_response = (200, {"profile": {"rows": 3, "columns": [{"name": "id"}]},
                                          "proposal": {"description": "", "rules": [{"id": "r1", "kind": "column", "column": "id", "severity": "error"}]}})
    out = await routes.propose_contract(scene.ds.id, scene.author, session)
    assert out["document"]["rules"][0]["column"] == "id" and out["profile"]["rows"] == 3
    assert contracts.get(session, scene.ds.id) is None
    assert contracts.validate_document(out["document"])   # what is proposed is a valid contract


# ── the state in the listings: what feeds the icon ───────────────────────────
async def test_every_datasource_listing_carries_the_contract_state(session, scene, fake_engine):
    other_one = make_datasource(session, name="senza-contratto", project_id=scene.folder.id, key="datasets/2/v1.parquet")
    await _save(session, scene.author, scene.ds)
    by_name = {d.name: d for d in list_all_datasources(scene.reader, session)}
    assert by_name["senza-contratto"].contract is None
    assert (by_name["ordini"].contract.status, by_name["ordini"].contract.version) == ("passed", 1)


def test_many_datasources_state_costs_one_query(session, db_engine, scene):
    from sqlalchemy import event

    for i in range(20):
        d = make_datasource(session, name=f"d{i}", project_id=scene.folder.id, key=f"datasets/{i}.parquet")
        contracts.save(session, d, {"description": "", "rules": []}, True, None)
    session.commit()
    commands = []
    event.listen(db_engine, "before_cursor_execute", lambda c, cur, stmt, *a: commands.append(stmt))
    assert len(contracts.summaries(session, range(1, 40))) == 20
    assert len(commands) == 1


@pytest.mark.parametrize("state, refused, icona", [
    ("pending", False, "pending"), ("passed", False, "passed"), ("warning", False, "warning"), ("failed", False, "failed"),
    ("passed", True, "failed"),    # the data holds, but the last update was refused: it is no longer current
    ("warning", True, "failed"),
])
def test_cio_che_l_icona_dice(session, scene, state, refused, icona):
    from datetime import datetime, timezone

    c = contracts.save(session, scene.ds, {"description": "", "rules": []}, True, None)
    c.status = state
    c.blocked_at = datetime.now(timezone.utc) if refused else None
    assert contracts.summary(c)["status"] == icona and contracts.summary(c)["blocked"] is refused


# ── removing ─────────────────────────────────────────────────────────────────
async def test_removing_the_contract_leaves_the_history_removing_the_datasource_does_not(session, scene, fake_engine):
    from sqlmodel import select

    await _save(session, scene.author, scene.ds)
    routes.delete_contract(scene.ds.id, REQUEST, scene.author, session)
    assert contracts.get(session, scene.ds.id) is None
    assert len(session.exec(select(DataContractResult)).all()) == 1 and len(session.exec(select(DataContractVersion)).all()) == 1
    # created again: the numbering goes on, it does not start over
    assert (await _save(session, scene.author, scene.ds)).version == 2
    delete_datasource(scene.ds.id, REQUEST, scene.author, session)
    for model in (DataContract, DataContractVersion, DataContractResult):
        assert session.exec(select(model)).all() == []
