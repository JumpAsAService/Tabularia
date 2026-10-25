"""Worker pools sized from Admin → Workers (app/tasks/worker_pool.py): a worker grows
at once, shrinks by idle processes only, follows the panel's value for its queue,
and reports its state where the engine API reads it."""
import json

import pytest

from app.core import panel_settings
from app.core.config import settings_from_env
from app.tasks import worker_pool
from app.tasks.worker_pool import PoolFromPanel, kind_of, live_workers, report, resize, state, target_size


class Inner:
    _max_memory_per_child = 1_000_000


class Pool:
    """billiard's pool as the consumer sees it: shrink takes idle processes only and
    raises when there are fewer than asked, after taking them."""

    def __init__(self, processes, busy=0):
        self.num_processes, self.busy, self._pool = processes, busy, Inner()

    def grow(self, n):
        self.num_processes += n

    def shrink(self, n):
        idle = self.num_processes - self.busy
        taken = min(n, idle)
        self.num_processes -= taken
        if taken < n:
            raise ValueError("Can't shrink pool. All processes busy!")


class Queues:
    def __init__(self, names):
        self.consume_from = {n: object() for n in names}


class App:
    def __init__(self, queues):
        self.amqp = type("Amqp", (), {"queues": Queues(queues)})()
        self.conf = type("Conf", (), {"task_time_limit": 3600})()


class Controller:
    autoscaler = None


class Timer:
    def __init__(self):
        self.calls = []

    def call_repeatedly(self, secs, fun, args, priority=0):
        self.calls.append((secs, fun, args))
        return type("Tref", (), {"cancel": lambda self: None})()


class Consumer:
    def __init__(self, processes=2, busy=0, queues=("celery",), name="celery@w1"):
        self.pool, self.hostname = Pool(processes, busy), name
        self.app, self.controller, self.timer = App(queues), Controller(), Timer()
        self.prefetch = []

    def _update_prefetch_count(self, delta):
        self.prefetch.append(delta)


def test_a_worker_on_the_preview_queue_only_is_a_preview_worker():
    assert kind_of(["preview"]) == "preview"
    assert kind_of(["celery"]) == "run"
    assert kind_of(["celery", "preview"]) == "run"
    assert kind_of([]) == "run"


def test_without_a_value_from_the_panel_a_worker_keeps_its_starting_size():
    assert target_size("run", 3) == 3 and target_size("preview", 2) == 2
    panel_settings.write({"workers.run_concurrency": 6})
    assert target_size("run", 3) == 6 and target_size("preview", 2) == 2
    panel_settings.write({"workers.preview_concurrency": 4})
    assert target_size("run", 3) == 3 and target_size("preview", 2) == 4


def test_it_grows_at_once_and_the_reserved_tasks_follow():
    c = Consumer(processes=2)
    assert resize(c, 5) == 5
    assert c.prefetch == [3]
    assert c.pool._pool._max_memory_per_child == worker_pool.per_process_kb(5)


def test_it_shrinks_by_idle_processes_and_never_stops_a_task():
    c = Consumer(processes=4, busy=3)
    assert resize(c, 1) == 3  # one idle process: the three busy ones keep working
    assert c.prefetch == [-1]
    c.pool.busy = 0  # their tasks ended
    assert resize(c, 1) == 1
    assert c.prefetch == [-1, -2]


def test_at_the_target_nothing_moves():
    c = Consumer(processes=3)
    assert resize(c, 3) == 3 and c.prefetch == []


def test_more_processes_get_a_smaller_memory_share_each(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "cgroup_mem_limit_bytes", lambda: 8 * 1024**3)
    assert worker_pool.per_process_kb(2) == 2 * worker_pool.per_process_kb(4)


def test_the_state_says_what_the_panel_shows():
    c = Consumer(processes=2, busy=0, queues=("preview",), name="celery@p1")
    s = state(c, "preview", 2, 100.0)
    assert (s["name"], s["kind"], s["queues"], s["processes"], s["target"]) == ("celery@p1", "preview", ["preview"], 2, 2)
    assert s["busy"] == 0 and s["autoscale"] is False and s["memory_per_process"] == 1_000_000 * 1024
    assert 0 < s["memory_share"] < 1 and s["memory_floor"] > 0


def test_a_reported_worker_is_live_until_its_state_expires(regolazioni_isolate):
    report({"name": "celery@a", "processes": 2})
    report({"name": "celery@b", "processes": 4})
    assert [w["name"] for w in live_workers()] == ["celery@a", "celery@b"]
    regolazioni_isolate.delete(worker_pool.KEY_PREFIX + "celery@a")  # what the TTL does
    assert [w["name"] for w in live_workers()] == ["celery@b"]
    assert regolazioni_isolate.smembers(worker_pool.REGISTRY) == {"celery@b"}


def test_the_step_resizes_and_reports_at_start_and_then_on_a_timer(regolazioni_isolate):
    panel_settings.write({"workers.run_concurrency": 4})
    c = Consumer(processes=2)
    step = PoolFromPanel(None)
    step.start(c)
    assert c.pool.num_processes == 4 and step.started_with == 2
    assert c.timer.calls[0][0] == worker_pool.TICK
    assert json.loads(regolazioni_isolate.get(worker_pool.KEY_PREFIX + "celery@w1"))["processes"] == 4
    panel_settings.write({})  # the value removed: back to the size it started with
    step.tick(c)
    assert c.pool.num_processes == 2
    step.shutdown(c)
    assert live_workers() == []


def test_a_worker_shutting_down_keeps_its_tasks_alive_until_it_exits(regolazioni_isolate, monkeypatch):
    # Celery 5's warm shutdown (a rolling update, a drain, a scale-in): the consumer's steps
    # stop, then the pool waits for the running tasks, and `shutdown` comes only after them.
    # Meanwhile the main process's thread keeps the state alive: the tasks are not lost.
    at_exit, threads = [], []
    monkeypatch.setattr(worker_pool.atexit, "register", lambda f, *a: at_exit.append((f, a)))
    monkeypatch.setattr(worker_pool.threading, "Thread", lambda **kw: threads.append(kw) or type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(worker_pool, "CONFIRM_SECONDS", 0)
    c = Consumer()
    step = PoolFromPanel(None)
    step.start(c)
    assert len(threads) == 1 and threads[0]["daemon"] is True
    meta = {"pid": 7, "hostname": "celery@w1", "since": step.started_at + 1}
    step.stop(c)  # what Celery calls first
    assert live_workers() == []  # out of the panel at once
    assert worker_pool.lost("t", meta) == "" and worker_pool.lost("t", meta) == ""
    regolazioni_isolate.delete(worker_pool.KEY_PREFIX + "celery@w1")  # its TTL, had nobody written it
    worker_pool.keep_alive("celery@w1", step.started_at)  # what the thread does every TICK
    assert worker_pool.lost("t", meta) == "" and live_workers() == []
    step.start(c)  # the consumer back (a broker connection restored): listed again, one thread still
    assert [w["name"] for w in live_workers()] == ["celery@w1"] and len(threads) == 1
    step.shutdown(c)
    for f, a in at_exit:  # the process exits once the pool is done
        f(*a)
    assert worker_pool.lost("t", meta) == worker_pool.LOST  # still running then: it went with the process


def test_a_failing_check_never_stops_the_worker(regolazioni_isolate):
    panel_settings.write({"workers.run_concurrency": 4})
    c = Consumer(processes=2)

    def boom(n):
        raise RuntimeError("pool closed")

    c.pool.grow = boom
    step = PoolFromPanel(None)
    step.started_with = 2
    step.tick(c)  # no exception
    assert json.loads(regolazioni_isolate.get(worker_pool.KEY_PREFIX + "celery@w1"))["processes"] == 2


def test_with_autoscale_celery_keeps_sizing_the_pool():
    panel_settings.write({"workers.run_concurrency": 4})
    c = Consumer(processes=2)
    c.controller.autoscaler = object()
    step = PoolFromPanel(None)
    step.started_with = 2
    step.tick(c)
    assert c.pool.num_processes == 2


def test_the_engine_api_lists_the_workers_and_the_queues(monkeypatch):
    from app.api.routes import workers as route

    report({"name": "celery@a", "processes": 2})
    monkeypatch.setattr(route, "_queued", lambda: {"celery": 3, "preview": 0})
    out = route.workers()
    assert [w["name"] for w in out["workers"]] == ["celery@a"]
    assert out["queued"] == {"celery": 3, "preview": 0} and out["tick_seconds"] == worker_pool.TICK


@pytest.mark.parametrize("value", [0, 33])
def test_the_panel_value_stays_between_one_and_thirty_two(value):
    _, errors = panel_settings.validate({"workers.run_concurrency": value}, settings_from_env())
    assert errors and errors[0]["code"] in ("min", "max")


def test_a_pool_that_cannot_resize_is_left_alone():
    class SoloPool:  # --pool=solo: one process, no grow/shrink
        num_processes = 1

    c = Consumer()
    c.pool = SoloPool()
    assert resize(c, 4) == 1 and c.prefetch == []


# ── a STARTED task whose worker is gone (worker_pool.lost) ─────────────────────
def _worker(fake, name, started_at):
    fake.set(worker_pool.KEY_PREFIX + name, json.dumps({"name": name, "started_at": started_at}))


def test_a_task_of_a_worker_still_in_the_same_life_is_not_lost(regolazioni_isolate):
    _worker(regolazioni_isolate, "celery@w1", started_at=100.0)
    assert worker_pool.lost("t", {"pid": 7, "hostname": "celery@w1", "since": 150.0}) == ""


def test_a_task_of_a_worker_that_started_again_is_lost(regolazioni_isolate):
    # an OOMKilled container starts again in the same pod, with the same name
    _worker(regolazioni_isolate, "celery@w1", started_at=200.0)
    assert worker_pool.lost("t", {"pid": 7, "hostname": "celery@w1", "since": 150.0}) == worker_pool.LOST


def test_a_task_of_a_worker_gone_is_lost_once_it_stays_gone(regolazioni_isolate, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(worker_pool.time, "time", lambda: clock[0])
    meta = {"pid": 7, "hostname": "celery@gone", "since": 900.0}
    assert worker_pool.lost("t", meta) == ""  # first seen missing: a blip of Valkey, maybe
    clock[0] += worker_pool.CONFIRM_SECONDS - 1
    assert worker_pool.lost("t", meta) == ""
    clock[0] += 2
    assert worker_pool.lost("t", meta) == worker_pool.LOST
    # back meanwhile (it wrote again after the blip): not lost
    _worker(regolazioni_isolate, "celery@gone", started_at=800.0)
    assert worker_pool.lost("t", meta) == ""


@pytest.mark.parametrize("meta", [
    None, "x", {"pid": 7}, {"pid": 7, "hostname": "celery@old"},  # an older worker's meta: no verdict
])
def test_without_the_facts_no_verdict(regolazioni_isolate, meta, monkeypatch):
    monkeypatch.setattr(worker_pool, "CONFIRM_SECONDS", 0)
    assert worker_pool.lost("t", meta) == ""


def test_without_valkey_no_verdict(monkeypatch):
    def broken():
        raise ConnectionError("valkey down")
    monkeypatch.setattr(panel_settings, "_client", broken)
    assert worker_pool.lost("t", {"pid": 7, "hostname": "celery@w1", "since": 1.0}) == ""


def test_every_task_notes_its_worker_and_its_start():
    from app.tasks.celery_app import _TaskThatSparesTheWorker, celery_app

    seen = []

    class Probe(_TaskThatSparesTheWorker):
        name = "tests.worker_pool.probe"

        def run(self):
            return "done"

        def update_state(self, state=None, meta=None, **kw):
            seen.append((state, meta))

    task = celery_app.register_task(Probe())
    task.push_request(id="t-9", hostname="celery@w1", called_directly=False, is_eager=False)
    try:
        assert task() == "done"
    finally:
        task.pop_request()
    assert seen and seen[0][0] == "STARTED" and seen[0][1]["hostname"] == "celery@w1" and seen[0][1]["since"] > 0
