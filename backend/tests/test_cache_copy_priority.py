"""Copies of steps into the cache run on the preview worker without getting in the
way of the clicks (review of 2026-10-14: they took 39% of its time, in arrival
order). They are queued at the lowest priority, only one runs at a time, and a copy
that finds previews waiting gives up: the cache is an optimisation, the next preview
of that node asks for it again."""
import os
import uuid

import pytest

from app.api.routes import tasks as task_routes
from app.tasks import jobs
from app.tasks.celery_app import broker_lists


class Broker:
    """The few Redis calls the copy makes."""

    def __init__(self, waiting=0, holder=None):
        self.lists = {"preview": waiting}
        self.keys = {} if holder is None else {jobs._COPY_LOCK: holder}

    def llen(self, name):
        return self.lists.get(name, 0)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def eval(self, script, numkeys, key, token):
        if self.keys.get(key) == token:
            del self.keys[key]


class Engine:
    def __init__(self):
        self.copies = 0

    def materialize(self, source, operations):
        self.copies += 1
        return True


@pytest.fixture
def engine(monkeypatch):
    eng = Engine()
    monkeypatch.setattr(jobs, "get_engine", lambda name=None: eng)
    return eng


def _copy(monkeypatch, broker):
    monkeypatch.setattr(jobs, "_broker", lambda: broker)
    return jobs.materialize_step_task.run("duckdb", "b", "datasets/x.parquet", [{"type": "limit", "params": {"n": 1}}])


def test_a_copy_gives_way_to_waiting_previews(monkeypatch, engine):
    assert _copy(monkeypatch, Broker(waiting=2)) == {"ok": False, "skipped": "previews are waiting"}
    assert engine.copies == 0


def test_only_one_copy_runs_at_a_time(monkeypatch, engine):
    assert _copy(monkeypatch, Broker(holder="another")) == {"ok": False, "skipped": "another copy is running"}
    assert engine.copies == 0


def test_a_copy_takes_the_turn_and_gives_it_back(monkeypatch, engine):
    broker = Broker()
    assert _copy(monkeypatch, broker) == {"ok": True, "written": True}
    assert engine.copies == 1 and jobs._COPY_LOCK not in broker.keys


def test_without_the_broker_the_copy_runs_as_before(monkeypatch, engine):
    class Down:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise ConnectionError("valkey is down")
            return fail

    assert _copy(monkeypatch, Down()) == {"ok": True, "written": True}


def test_the_queue_page_counts_every_priority_list(monkeypatch):
    import redis

    lengths = {"preview": 2, broker_lists("preview")[-1]: 5, "celery": 1}

    class Client:
        def llen(self, name):
            return lengths.get(name, 0)

    class Inspect:
        def __init__(self, timeout):
            pass

        def active(self):
            return {}

        reserved = stats = active

        def active_queues(self):
            return {"w1": [{"name": "preview"}], "w2": [{"name": "celery"}]}

    monkeypatch.setattr(task_routes.celery_app.control, "inspect", Inspect)
    monkeypatch.setattr(redis, "from_url", lambda url: Client())
    out = task_routes.queue_overview()
    assert {q["name"]: q["messages"] for q in out["queues"]} == {"celery": 1, "preview": 7}


def test_the_lists_go_from_the_highest_priority_to_the_lowest():
    assert broker_lists("preview") == ["preview", "preview\x06\x163", "preview\x06\x166", "preview\x06\x169"]


@pytest.mark.skipif(not os.getenv("VALKEY_TEST_URL"), reason="needs a throwaway Valkey (VALKEY_TEST_URL)")
def test_on_a_real_broker_a_preview_sent_later_comes_out_first():
    """What a worker does: it empties the lists in priority order, so a copy queued
    first still comes out after a preview queued afterwards."""
    from kombu import Connection

    from app.tasks.celery_app import celery_app

    queue = f"zz-prio-{uuid.uuid4().hex[:6]}"
    celery_app.send_task("app.tasks.jobs.materialize_step_task", args=["duckdb", "b", "k", []], queue=queue,
                         priority=jobs.CACHE_COPY_PRIORITY)
    celery_app.send_task("app.tasks.jobs.preview_task", kwargs={}, queue=queue)
    with Connection(os.environ["VALKEY_TEST_URL"]) as conn:
        q = conn.SimpleQueue(queue)
        first, second = q.get(timeout=5), q.get(timeout=5)
        names = [first.headers["task"], second.headers["task"]]
        first.ack(), second.ack()
        q.close()
    assert names == ["app.tasks.jobs.preview_task", "app.tasks.jobs.materialize_step_task"]
