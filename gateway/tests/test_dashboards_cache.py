"""A dashboard's cache, on the gateway's side (docs/specs/dashboards.md, «The cache»; the cache itself
is the engine's: backend/tests/test_dashboard_cache.py). What these tests hold: rights are checked at
every request with the reader's, before anything of the cache, and only what the reader may read
reaches it, whatever anyone asked before; two people send the same source only when they read
the same thing; a saved dashboard's request knocks nothing down and shares; a definition being edited
shares nothing; a refresh asks with the reader's rights too; a parameter's list checks nothing against
a dataset the reader may not read.
"""
import json

import pytest

from tests.conftest import make_datasource, make_permission
from tests.test_dashboards_data import (  # noqa: F401 — `app` is a fixture
    CSRF,
    _data,
    _dataset,
    _reader,
    _saved,
    _sign_in,
    _uid,
    _world,
    app,
    definition,
    kpi,
)


def _reads_all(w) -> dict:
    return definition(w, _dataset("pay", w["salaries"]))


def _no_trace(answer: dict, *words) -> None:
    text = json.dumps(answer)
    for word in ("pay_grade", "Salaries", "datasets/s.parquet", *words):
        assert word not in text


# ── isolation ─────────────────────────────────────────────────────────────────
def test_who_may_not_read_a_dataset_sends_nothing_of_it_before_or_after_who_may(app, session, fake_engine):
    """A fills the cache, then B without rights; and the other way round. What reaches the cache is
    the reader's only: there is no key of the salaries in B's request, in either order, so nothing
    the engine holds can be found through it, and B's answer has no number and no hint."""
    w = _world(session)
    board = _saved(session, w, _reads_all(w))
    a = _reader(app, session, w, email="a@x.local")
    make_permission(session, user_id=_uid(session, "a@x.local"), project_id=w["vault"], capability="view")
    b = _reader(app, session, w, email="b@x.local")

    for order in (((a, "a"), (b, "b")), ((b, "b"), (a, "a"))):
        sent = {}
        for reader, who in order:
            assert _data(reader, board).status_code == 200
            sent[who] = fake_engine.report_tiles[-1]
        assert "pay" not in sent["b"]["datasets"] and sent["b"]["refused"]["pay"]["code"] == "no_access"
        _no_trace(sent["b"]["datasets"])
        assert sent["a"]["datasets"]["pay"]["input_key"] == "datasets/s.parquet" and sent["a"]["refused"] == {}
    answer = _data(b, board).json()
    assert answer["tiles"]["on_pay"].keys() == {"error", "code"} and answer["tiles"]["on_pay"]["code"] == "no_access"
    assert "pay" not in answer["truncated"] and answer["tiles"]["mine"]["value"] == 1
    _no_trace(answer)


def test_rights_taken_away_count_at_the_very_next_request_whatever_was_read_before(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, _reads_all(w))
    client = _reader(app, session, w)
    grant = make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=w["vault"], capability="view")
    assert "pay" in (_data(client, board), fake_engine.report_tiles[-1]["datasets"])[1]
    session.delete(grant)
    session.commit()
    assert _data(client, board).status_code == 200
    assert "pay" not in fake_engine.report_tiles[-1]["datasets"]


def test_who_may_not_see_the_dashboard_touches_nothing_of_the_cache_not_even_with_a_refresh(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    stranger = _sign_in(app, session, "stranger@x.local")
    assert _data(stranger, board, refresh=True).status_code == 404
    assert fake_engine.refresh_claims == [] and fake_engine.report_tiles == []


# ── the same source, or not ───────────────────────────────────────────────────
def test_a_sql_on_the_catalog_reads_what_each_reader_finds_under_that_name(app, session, fake_engine):
    """The same SQL, the same parameters, the same dashboard: a name of the catalog is resolved among
    what the reader may read, so two readers can read two files under one name. What the engine's key
    is made of is what they really read, and it differs (the engine's side:
    test_two_readers_who_read_two_files_under_one_name_share_nothing)."""
    w = _world(session)
    columns = json.dumps([{"name": "region", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}])
    make_datasource(session, name="Figures", project_id=w["other"], key="datasets/theirs.parquet", columns=columns)
    make_datasource(session, name="Figures", project_id=w["secret"], key="datasets/ours.parquet", columns=columns)
    board = _saved(session, w, definition(w, {"name": "numbers", "source": {"kind": "sql", "sql": 'SELECT * FROM "Figures"'}}))
    sent = {}
    for email, folder in (("first@x.local", "other"), ("second@x.local", "secret")):
        client = _reader(app, session, w, email=email)
        make_permission(session, user_id=_uid(session, email), project_id=w[folder], capability="view")
        assert _data(client, board).status_code == 200
        sent[folder] = fake_engine.report_tiles[-1]["datasets"]["numbers"]
    assert sent["other"]["input_key"] == "datasets/theirs.parquet" and sent["secret"]["input_key"] == "datasets/ours.parquet"
    assert sent["other"] != sent["secret"]


def test_a_new_snapshot_of_a_datasource_is_another_source(app, session, fake_engine):
    """What a refresh of the data does: the key of its parquet changes (runs.snapshot_key)."""
    from app.models import Datasource

    w = _world(session)
    board = _saved(session, w, definition(w))
    client = _reader(app, session, w)
    _data(client, board)
    before = fake_engine.report_tiles[-1]["datasets"]["orders"]
    ds = session.get(Datasource, w["orders"])
    ds.key = "datasets/o-newer.parquet"
    session.add(ds)
    session.commit()
    _data(client, board)
    after = fake_engine.report_tiles[-1]["datasets"]["orders"]
    assert before["input_key"] == "datasets/o.parquet" and after["input_key"] == "datasets/o-newer.parquet"


def test_two_people_on_one_dashboard_send_the_same_thing(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    for email in ("one@x.local", "two@x.local"):
        _data(_reader(app, session, w, email=email), board)
    one, two = fake_engine.report_tiles[-2:]
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


# ── a computation others wait for ─────────────────────────────────────────────
def test_two_tabs_of_one_person_do_not_knock_each_other_down(app, session, fake_engine):
    """A slot would have the newer request revoke the older, which others may be waiting for."""
    w = _world(session)
    board = _saved(session, w, definition(w))
    client = _reader(app, session, w)
    for _ in range(2):
        assert _data(client, board).status_code == 200
    assert all("slot" not in body for body in fake_engine.report_tiles)


def test_a_definition_being_edited_shares_nothing_and_still_has_its_slot(app, session, fake_engine):
    w = _world(session)
    client = _reader(app, session, w, capability="edit")
    r = client.post("/dashboards/preview", headers=CSRF, json={"definition": definition(w), "project_id": w["sales"], "refresh": True})
    assert r.status_code == 200, r.text
    body = fake_engine.report_tiles[-1]
    assert "cache" not in body and ":dashboard:preview-new:" in body["slot"]
    assert fake_engine.refresh_claims == []


def test_the_dashboard_s_interval_goes_with_the_request(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, {**definition(w), "refresh_seconds": 3600})
    _data(_reader(app, session, w), board)
    assert fake_engine.report_tiles[-1]["cache"] == {"refresh_seconds": 3600}


# ── a refresh ─────────────────────────────────────────────────────────────────
def test_a_refresh_granted_names_its_generation_and_the_rights_are_still_the_reader_s(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, _reads_all(w))
    assert _data(_reader(app, session, w), board, refresh=True).status_code == 200
    body = fake_engine.report_tiles[-1]
    assert fake_engine.refresh_claims == [board] and body["cache"] == {"refresh_seconds": 0, "fresh": "fresh-token"}
    assert "pay" not in body["datasets"] and body["refused"]["pay"]["code"] == "no_access"


def test_a_refresh_past_the_limit_is_no_error_and_goes_round_nothing(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    fake_engine.refresh_token = None
    r = _data(_reader(app, session, w), board, refresh=True)
    assert r.status_code == 200 and r.json()["tiles"]["mine"]["computed_at"]
    assert fake_engine.report_tiles[-1]["cache"] == {"refresh_seconds": 0}


def test_without_a_refresh_nothing_is_claimed(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    _data(_reader(app, session, w), board)
    assert fake_engine.refresh_claims == []


# ── a parameter's list ────────────────────────────────────────────────────────
def _on_a_list(w, dataset_id, **parameter) -> dict:
    content = definition(w)
    content["datasets"].append(_dataset("list", dataset_id))
    content["parameters"] = [{"name": "region", "label": "Region", "type": "text", "required": False,
                              "choices": {"dataset": "list", "value": "[region]"}, **parameter}]
    return content


def test_a_list_the_reader_may_not_read_does_not_stop_the_tiles_they_may(app, session, fake_engine):
    """A default value, a list read from a dataset of another folder: the tile on the orders works, and
    the answer says nothing of the list or of whether the value is in it."""
    w = _world(session)
    board = _saved(session, w, _on_a_list(w, w["salaries"], default="North"))
    fake_engine.report_choices_response = (200, {"choices": [{"value": "South", "label": "South"}], "truncated": False})
    client = _reader(app, session, w)
    r = _data(client, board)
    assert r.status_code == 200, r.text
    assert r.json()["tiles"]["mine"]["value"] == 1
    assert fake_engine.report_choices == []
    _no_trace(r.json(), "South", "North", "list")
    # nor does a value given: no answer tells whether it is in a list they cannot read
    for value in ("South", "Anything"):
        r = _data(client, board, params={"region": value})
        assert r.status_code == 200 and fake_engine.report_choices == []


@pytest.mark.parametrize("source", [
    {"kind": "sql", "sql": 'SELECT * FROM "Salaries"'}, {"kind": "datasource", "datasource_id": None}])
def test_the_form_of_who_may_not_read_a_list_says_so_and_nothing_of_what_it_reads(app, session, fake_engine, source):
    w = _world(session)
    content = _on_a_list(w, w["orders"])
    content["datasets"][-1]["source"] = {**source, **({"datasource_id": w["salaries"]} if "datasource_id" in source else {})}
    board = _saved(session, w, content)
    r = _reader(app, session, w).get(f"/dashboards/{board}/parameters")
    assert r.status_code == 200 and fake_engine.report_choices == []
    assert not r.json()[0]["choices"] and r.json()[0]["choices_error"]
    _no_trace(r.json(), str(w["salaries"]) + ")", "cannot read the dataset «list»")


def test_a_list_read_by_the_engine_goes_with_the_dashboard_s_cache(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, {**_on_a_list(w, w["orders"]), "refresh_seconds": 600})
    fake_engine.report_choices_response = (200, {"choices": [{"value": "North", "label": "North"}], "truncated": False})
    client = _reader(app, session, w)
    assert _data(client, board, params={"region": "North"}).status_code == 200
    assert fake_engine.report_choices[-1]["cache"] == {"refresh_seconds": 600}
    r = client.get(f"/dashboards/{board}/parameters")
    assert r.status_code == 200 and fake_engine.report_choices[-1]["cache"] == {"refresh_seconds": 600}
    assert r.json()[0]["choices"][0]["value"] == "North"


def test_a_refresh_reads_the_list_again_under_the_same_token_as_the_tiles(app, session, fake_engine):
    """One token: where the list and a tile read the same source, one query."""
    w = _world(session)
    board = _saved(session, w, _on_a_list(w, w["orders"]))
    fake_engine.report_choices_response = (200, {"choices": [{"value": "North", "label": "North"}], "truncated": False})
    assert _data(_reader(app, session, w), board, params={"region": "North"}, refresh=True).status_code == 200
    assert fake_engine.refresh_claims == [board]
    assert fake_engine.report_choices[-1]["cache"]["fresh"] == fake_engine.report_tiles[-1]["cache"]["fresh"] == "fresh-token"


def test_a_list_that_cannot_be_read_right_now_stops_no_tile(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, _on_a_list(w, w["orders"]))
    fake_engine.report_choices_response = (504, {"detail": "These numbers are still being computed for someone else"})
    r = _data(_reader(app, session, w), board, params={"region": "North"})
    assert r.status_code == 200 and r.json()["tiles"]["mine"]["value"] == 1


def test_a_value_not_in_a_list_the_reader_may_read_is_still_refused(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, _on_a_list(w, w["orders"]))
    fake_engine.report_choices_response = (200, {"choices": [{"value": "North", "label": "North"}], "truncated": False})
    r = _data(_reader(app, session, w), board, params={"region": "Nowhere"})
    assert r.status_code == 400 and "region" in r.text


def test_hostile_values_are_only_values_on_their_way_to_the_engine(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, _on_a_list(w, w["orders"]))
    fake_engine.report_choices_response = (200, {"choices": [{"value": v, "label": v} for v in (
        "x'; DROP TABLE t;--", "dataprep:dashboards:lock:*", '","fresh":"x', "\r\nFLUSHALL\r\n")], "truncated": False})
    client = _reader(app, session, w)
    sent = []
    for value in [c["value"] for c in fake_engine.report_choices_response[1]["choices"]]:
        assert _data(client, board, params={"region": value}).status_code == 200
        body = fake_engine.report_tiles[-1]
        assert body["params"]["region"] == value and body["cache"] == {"refresh_seconds": 0}
        sent.append(json.dumps(body["params"], sort_keys=True))
    assert len(set(sent)) == 4
