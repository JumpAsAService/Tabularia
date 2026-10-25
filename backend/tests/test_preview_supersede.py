"""A superseded preview is really knocked down — on the worker AND on the server.

Tried live before writing these tests: interrupting the worker does not stop ClickHouse
(the query went on for 65 s, slowing the one preview wanted), and the reused client
stayed poisoned ("Session … is locked"). The first KILL attempt failed SILENTLY on a
NameError swallowed by the courtesy except: hence the test that demands to SEE the KILL
go out.
"""
import sys
import types

import pytest

from app.core.config import ClickHouseExternalSettings
from app.engine import clickhouse_engine as ce
from app.engine.query_tag import current_query_tag, is_safe_tag, query_tag


def _engine():
    eng = ce.ClickHouseEngine.__new__(ce.ClickHouseEngine)
    eng.cfg = ClickHouseExternalSettings(host="h", transport="s3")
    return eng


class _FakeClient:
    def __init__(self, log): self.log = log
    def command(self, sql, **kw): self.log.append(sql)
    def close(self): self.log.append("close")


@pytest.fixture
def fake_driver(monkeypatch):
    """A fake `clickhouse_connect`: records the clients made and the commands."""
    log: list = []
    mod = types.SimpleNamespace(get_client=lambda **kw: (log.append(("connect", kw)), _FakeClient(log))[1])
    monkeypatch.setitem(sys.modules, "clickhouse_connect", mod)
    return log


# ── the label ────────────────────────────────────────────────────────────────
def test_the_tag_lives_only_inside_its_block():
    assert current_query_tag() is None
    with query_tag("tab-prev:abc-123"):
        assert current_query_tag() == "tab-prev:abc-123"
    assert current_query_tag() is None


@pytest.mark.parametrize("bad", ["x' OR 1=1 --", "a b", "", "é", "x" * 97])
def test_an_unsafe_tag_is_never_used(bad):
    """The label ends up inside a KILL QUERY: no quotes, spaces, odd lengths."""
    assert not is_safe_tag(bad)
    with query_tag(bad):
        assert current_query_tag() is None


def test_the_context_labels_every_query_with_the_tag():
    with query_tag("tab-prev:t1"):
        ctx = ce.ClickHouseContext(object(), None, ClickHouseExternalSettings(host="h", transport="s3"), [])
    assert ctx.settings["log_comment"] == "tab-prev:t1"
    assert "log_comment" not in ce.ClickHouseContext(object(), None, ClickHouseExternalSettings(host="h", transport="s3"), []).settings


# ── the KILL really goes out ──────────────────────────────────────────────────────
def test_kill_tagged_really_sends_the_kill(fake_driver):
    _engine().kill_tagged("tab-prev:t1")
    sql = [x for x in fake_driver if isinstance(x, str) and x.startswith("KILL")]
    assert sql == ["KILL QUERY WHERE Settings['log_comment'] = 'tab-prev:t1' ASYNC"], fake_driver
    assert fake_driver[-1] == "close"
    # with a client WITHOUT a session: a session is a lock
    assert fake_driver[0][1]["autogenerate_session_id"] is False


def test_kill_tagged_refuses_an_unsafe_tag(fake_driver):
    _engine().kill_tagged("x'; DROP TABLE t --")
    assert fake_driver == []


def test_kill_tagged_never_raises(monkeypatch):
    mod = types.SimpleNamespace(get_client=lambda **kw: (_ for _ in ()).throw(RuntimeError("server giu'")))
    monkeypatch.setitem(sys.modules, "clickhouse_connect", mod)
    _engine().kill_tagged("tab-prev:t1")  # must not propagate


# ── telling the interruption ─────────────────────────────────────────────────
class SoftTimeLimitExceeded(Exception):
    """The same NAME as billiard's: the engine tells it by name."""


def test_an_interruption_is_recognised_even_when_wrapped():
    try:
        try:
            raise SoftTimeLimitExceeded()
        except Exception as e:
            raise ce.EngineError("ClickHouse: SoftTimeLimitExceeded()") from e
    except Exception as wrapped:
        assert ce._was_interrupted(wrapped)
    assert not ce._was_interrupted(ce.EngineError("colonna sconosciuta"))


def test_forget_client_drops_and_closes_the_cached_client():
    log: list = []
    ce._CLIENTS_PER_THREAD.by_key = {("h", 1): (_FakeClient(log), 0.0)}
    _engine()._forget_client()
    assert ce._CLIENTS_PER_THREAD.by_key == {} and log == ["close"]


# ── the route: the slot ──────────────────────────────────────────────────────────
from celery.exceptions import TaskRevokedError, TimeoutError as CeleryTimeoutError  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.api.models import PreviewRequest  # noqa: E402
from app.api.routes import tasks as routes  # noqa: E402

OK = {"ok": True, "result": {"columns": [], "rows": [], "row_count": 0, "truncated": False}}


class _Result:
    """A fake AsyncResult: `script` = what every get() does (an exception or a payload)."""
    def __init__(self, script): self.script = list(script); self.forgotten = False; self.revoked = None
    def get(self, timeout=None):
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException) or (isinstance(step, type) and issubclass(step, BaseException)):
            raise step
        return step
    def forget(self): self.forgotten = True
    def revoke(self, **kw): self.revoked = kw


class _Celery:
    def __init__(self, result): self.result = result; self.sent = []; self.revoked = []; self.control = self
    def send_task(self, name, kwargs=None, queue=None, task_id=None):
        self.sent.append({"kwargs": kwargs, "queue": queue, "task_id": task_id}); return self.result
    def revoke(self, task_id, **kw): self.revoked.append((task_id, kw))


class _Slots:
    def __init__(self, previous=None): self.store = {}; self.previous = previous; self.released = []
    def claim(self, slot, task_id, ttl):
        prev = self.store.get(slot, self.previous); self.store[slot] = task_id; return prev
    def owner(self, slot): return self.store.get(slot)
    def release(self, slot, task_id): self.released.append((slot, task_id))


def _req(**kw):
    return PreviewRequest(bucket="b", input_key="datasets/x.parquet", operations=[], **kw)


def _wire(monkeypatch, result, slots):
    celery = _Celery(result)
    monkeypatch.setattr(routes, "celery_app", celery)
    monkeypatch.setattr(routes, "preview_slots", slots)
    return celery


def test_without_a_slot_nothing_changes(monkeypatch):
    slots = _Slots(); celery = _wire(monkeypatch, _Result([OK]), slots)
    routes.preview_flow(_req())
    assert celery.revoked == [] and slots.store == {} and slots.released == []
    # no new kwarg to the task: an OLD worker must go on understanding it
    assert set(celery.sent[0]["kwargs"]) == {"bucket", "input_key", "operations", "limit", "engine", "no_cache"}


def test_a_new_preview_takes_down_the_one_holding_the_slot(monkeypatch):
    slots = _Slots(previous="vecchio-task"); celery = _wire(monkeypatch, _Result([OK]), slots)
    routes.preview_flow(_req(slot="u1:ed-preview"))
    assert celery.revoked == [("vecchio-task", {"terminate": True, "signal": "SIGUSR1"})]
    new = celery.sent[0]["task_id"]
    assert slots.store["u1:ed-preview"] == new and slots.released == [("u1:ed-preview", new)]


def test_a_queued_preview_notices_it_was_superseded(monkeypatch):
    """Queued, the task is not marked revoked yet: the SLOT says we are no longer the last.
    Without it, the API's thread would wait for the worker."""
    slots = _Slots(); result = _Result([CeleryTimeoutError()]); celery = _wire(monkeypatch, result, slots)
    orig = slots.claim
    def claim(slot, task_id, ttl):
        orig(slot, task_id, ttl); slots.store[slot] = "uno-piu-nuovo"; return None
    slots.claim = claim
    with pytest.raises(HTTPException) as e:
        routes.preview_flow(_req(slot="u1:ed-preview"))
    assert e.value.status_code == 409 and result.forgotten


def test_a_running_preview_that_gets_revoked_answers_409(monkeypatch):
    slots = _Slots(); _wire(monkeypatch, _Result([TaskRevokedError("revoked")]), slots)
    with pytest.raises(HTTPException) as e:
        routes.preview_flow(_req(slot="u1:ed-preview"))
    assert e.value.status_code == 409


def test_the_timeout_still_applies(monkeypatch):
    slots = _Slots(); result = _Result([CeleryTimeoutError()]); _wire(monkeypatch, result, slots)
    monkeypatch.setattr(routes, "PREVIEW_TIMEOUT_SECONDS", 0.0)
    with pytest.raises(HTTPException) as e:
        routes.preview_flow(_req(slot="u1:ed-preview"))
    assert e.value.status_code == 504 and result.revoked == {"terminate": True}


@pytest.mark.parametrize("bad", ["a b", "x'y", "é", "x" * 97])
def test_an_odd_slot_is_rejected_by_the_model(bad):
    with pytest.raises(Exception):
        _req(slot=bad)


def test_the_slot_registry_fails_open(monkeypatch):
    """Valkey down must not break the preview: it goes on without a slot."""
    from app.api import preview_slots as ps

    class Boom:
        def set(self, *a, **k): raise ConnectionError("giu'")
        def get(self, *a, **k): raise ConnectionError("giu'")
        def eval(self, *a, **k): raise ConnectionError("giu'")
    monkeypatch.setattr(ps, "_client", Boom())
    assert ps.claim("s", "t", 60) is None and ps.owner("s") is None
    ps.release("s", "t")


# ── the interruption is told in ANY wrapper ─────────────────────────
class HTTPClientError(Exception):
    """As botocore's: it carries the cause ONLY in the message."""


def test_an_interruption_wrapped_by_botocore_is_recognised():
    """Seen live in the multi-user test: the signal arrives during the HEAD on S3, botocore
    wraps it, and the task ended "raised unexpected" with a traceback — 19 in an hour, for
    previews nobody waited for."""
    from app.engine.query_tag import was_interrupted

    assert was_interrupted(HTTPClientError("An HTTP Client raised an unhandled exception: SoftTimeLimitExceeded()"))
    assert not was_interrupted(HTTPClientError("connessione rifiutata"))
    assert not was_interrupted(None)


def test_the_task_reports_superseded_instead_of_crashing(monkeypatch):
    from app.tasks import jobs

    class Boom:
        def preview(self, **kw):
            raise HTTPClientError("An HTTP Client raised an unhandled exception: SoftTimeLimitExceeded()")
    monkeypatch.setattr(jobs, "get_engine", lambda name: Boom())
    out = jobs.preview_task.run(bucket="b", input_key="k", operations=[])
    assert out["ok"] is False and out["error"] == "superseded"


def test_a_real_unexpected_error_still_propagates(monkeypatch):
    """The silence holds for the interruption only: a real error must go on being heard."""
    from app.tasks import jobs

    class Boom:
        def preview(self, **kw):
            raise RuntimeError("disco pieno")
    monkeypatch.setattr(jobs, "get_engine", lambda name: Boom())
    with pytest.raises(RuntimeError):
        jobs.preview_task.run(bucket="b", input_key="k", operations=[])


def test_expected_engine_errors_keep_their_tags(monkeypatch):
    from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
    from app.tasks import jobs

    for exc, tag in ((SourceNotFoundError("b", "k"), "not_found"), (OperationError("filter", 0, "x"), "unprocessable"), (EngineError("x"), "bad_request")):
        class Boom:
            def preview(self, _e=exc, **kw):
                raise _e
        monkeypatch.setattr(jobs, "get_engine", lambda name, B=Boom: B())
        assert jobs.preview_task.run(bucket="b", input_key="k", operations=[])["error"] == tag


def test_the_route_maps_superseded_to_409():
    assert routes._PREVIEW_ERROR_STATUS["superseded"] == 409
