"""The cap on the exports a person has running at once (core/export_slots.py).

The rules these tests pin: a person takes a place per export or document, up to
EXPORT__MAX_RUNNING_PER_PERSON; past it the interface is refused with a sentence (429)
and code waits for a place, up to the seconds it is given; a place comes back when the
worker's task ends or is revoked, and by itself after the longest a task may last; only
people are counted (`user:<id>`), each on their own; two requests at the same instant do
not both take the last place.

They need a throwaway Valkey (VALKEY_TEST_URL): the CI gives one.
"""
import os
import threading
import time
import uuid

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.skipif(not os.getenv("VALKEY_TEST_URL"), reason="needs a throwaway Valkey (VALKEY_TEST_URL)")


@pytest.fixture
def slots(monkeypatch):
    import redis

    from app.core import export_slots

    client = redis.Redis.from_url(os.environ["VALKEY_TEST_URL"], decode_responses=True)
    monkeypatch.setattr(export_slots, "_client", client)
    monkeypatch.setattr(export_slots.get_settings().export, "max_running_per_person", 2)
    return export_slots


def _someone() -> str:
    return f"user:{uuid.uuid4().int % 10**9}"


def test_a_person_takes_places_up_to_the_cap_and_gets_them_back(slots):
    me, other = _someone(), _someone()
    assert slots.admit(me, "a") and slots.admit(me, "b")
    assert not slots.admit(me, "c")
    assert slots.admit(other, "x")  # each person on their own
    slots.release(me, "a")
    assert slots.admit(me, "c")


def test_only_people_are_counted(slots):
    for _ in range(5):
        assert slots.admit(None, uuid.uuid4().hex) and slots.admit("notebook:7", uuid.uuid4().hex)


def test_a_place_never_given_back_expires_with_the_longest_a_task_may_last(slots, monkeypatch):
    me = _someone()
    assert slots.admit(me, "a") and slots.admit(me, "b")
    # a worker killed with both: their places were taken longer ago than a task may last
    slots._r().zadd(slots._PREFIX + me, {"a": time.time() - slots._held_for() - 1,
                                        "b": time.time() - slots._held_for() - 1})
    assert slots.admit(me, "c") and slots.admit(me, "d")


def test_the_worker_gives_the_place_back_when_an_export_ends(slots):
    me = _someone()
    for task in ("t1", "t2"):
        slots._r().set(slots.OWNER_PREFIX + task, me, ex=60)
        assert slots.admit(me, task)
    slots.task_ended("app.tasks.jobs.transform_data_task", "t1")  # not an export: nothing
    assert not slots.admit(me, "t3")
    slots.task_ended("app.tasks.report_jobs.render_report_task", "t1")
    assert slots.admit(me, "t3")


def test_the_celery_signals_give_the_place_back(slots):
    from types import SimpleNamespace

    from app.tasks import celery_app as c

    me = _someone()
    for task in ("s1", "s2"):
        slots._r().set(slots.OWNER_PREFIX + task, me, ex=60)
        assert slots.admit(me, task)
    c._export_place_back(sender=SimpleNamespace(name="app.tasks.jobs.export_task"), task_id="s1")
    c._export_place_back_revoked(request=SimpleNamespace(task="app.tasks.jobs.db_export_task", id="s2"))
    assert slots._r().zcard(slots._PREFIX + me) == 0


def test_code_waits_for_a_place_instead_of_failing(slots):
    me = _someone()
    assert slots.admit(me, "a") and slots.admit(me, "b")
    threading.Timer(0.6, slots.release, args=(me, "a")).start()
    t = time.monotonic()
    assert slots.wait_for_place(me, "c", 5)
    assert 0.5 < time.monotonic() - t < 4
    # nobody gives one back: past the seconds it is told
    t = time.monotonic()
    assert not slots.wait_for_place(me, "d", 1)
    assert time.monotonic() - t < 2.5
    assert not slots.wait_for_place(me, "e", 0)  # the interface: at once


def test_two_at_the_same_instant_do_not_both_take_the_last_place(slots):
    me = _someone()
    assert slots.admit(me, "a")
    got: list[bool] = []
    threads = [threading.Thread(target=lambda i=i: got.append(slots.admit(me, f"r{i}"))) for i in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert got.count(True) == 1


def test_the_routes_refuse_the_interface_past_the_cap_with_a_sentence(slots, monkeypatch):
    from app.api import exports
    from app.api.models import ExportStart
    from app.api.routes import tasks

    monkeypatch.setattr(exports, "_client", slots._r())
    monkeypatch.setattr(tasks, "_send_export", lambda request, task_id: None)
    me = _someone()
    body = dict(bucket="b", input_key="k.parquet", owner=me)
    tasks.start_export(ExportStart(**body))
    tasks.start_export(ExportStart(**body))
    with pytest.raises(HTTPException) as e:
        tasks.start_export(ExportStart(**body))
    assert e.value.status_code == 429 and "EXPORT__MAX_RUNNING_PER_PERSON" in e.value.detail
    # who may follow an export is written only for the ones admitted
    assert slots._r().zcard(slots._PREFIX + me) == 2
