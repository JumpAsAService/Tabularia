"""«Stop» on a render nobody waits for any more (the Report tab of a statistics node left it):
POST /tasks/exports/{id}/cancel revokes the task, gives its place back and is the owner's only."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import exports
from app.api.routes import tasks
from app.core import export_slots
from tests.fakes import FakeRedis


@pytest.fixture
def stub(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(exports, "_client", fake)
    monkeypatch.setattr(export_slots, "_client", fake)
    revoked: list[tuple] = []
    monkeypatch.setattr(tasks.celery_app, "control", SimpleNamespace(
        revoke=lambda *a, **k: revoked.append((a, k))), raising=False)
    state = {"value": "PENDING"}
    monkeypatch.setattr(tasks, "AsyncResult", lambda *a, **k: SimpleNamespace(state=state["value"]))
    return SimpleNamespace(fake=fake, revoked=revoked, state=state)


def test_the_owner_stops_a_render_and_gets_its_place_back(stub):
    exports.remember_owner("t1", "user:7")
    stub.fake.zadd(export_slots._PREFIX + "user:7", {"t1": 1.0})
    assert tasks.export_cancel("t1", owner="user:7") == {"cancelled": True}
    assert stub.revoked == [(("t1",), {"terminate": True, "signal": "SIGUSR1"})]
    assert stub.fake.zscore(export_slots._PREFIX + "user:7", "t1") is None


def test_another_person_cannot_stop_it(stub):
    exports.remember_owner("t2", "user:7")
    for who in ("user:8", None, ""):
        with pytest.raises(HTTPException) as e:
            tasks.export_cancel("t2", owner=who)
        assert e.value.status_code == 404
    assert stub.revoked == []


def test_a_finished_render_is_left_alone(stub):
    exports.remember_owner("t3", "user:7")
    for state in ("SUCCESS", "FAILURE", "REVOKED"):
        stub.state["value"] = state
        assert tasks.export_cancel("t3", owner="user:7") == {"cancelled": False}
    assert stub.revoked == []
