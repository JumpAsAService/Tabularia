"""The queue page asks the workers four things; each is a broadcast that waits its
whole timeout. Asked one after the other they kept every refresh at four seconds,
and the page, refreshing every two, piled requests up on the server."""
import time

from app.api.routes import tasks

ANSWER_S = 0.3


class SlowInspect:
    """Each answer takes ANSWER_S, as a broadcast waiting for its replies does."""

    def __init__(self, timeout):
        self.timeout = timeout

    def _later(self, value):
        time.sleep(ANSWER_S)
        return value

    def active(self):
        return self._later({"w1": [{"id": "t1", "name": "app.tasks.jobs.transform_data_task", "time_start": time.time()}]})

    def reserved(self):
        return self._later({})

    def stats(self):
        return self._later({"w1": {"pool": {"max-concurrency": 4}}})

    def active_queues(self):
        return self._later({"w1": [{"name": "celery"}]})


def test_the_workers_are_asked_together(monkeypatch):
    monkeypatch.setattr(tasks.celery_app.control, "inspect", lambda timeout: SlowInspect(timeout))
    started = time.monotonic()
    out = tasks.queue_overview()
    took = time.monotonic() - started
    assert took < 3 * ANSWER_S  # one after the other it is 4 × ANSWER_S
    assert out["workers"] == [{"name": "w1", "concurrency": 4, "running": 1, "reserved": 0}]
    assert [j["task_id"] for j in out["running"]] == ["t1"] and out["reserved"] == []


class ResizedInspect(SlowInspect):
    def stats(self):  # resized from Admin → Workers: max-concurrency is the starting size
        return {"w1": {"pool": {"max-concurrency": 2, "processes": [11, 12, 13, 14, 15, 16]}}}

    def _later(self, value):
        return value


def test_a_resized_pool_shows_the_processes_it_has_now(monkeypatch):
    monkeypatch.setattr(tasks.celery_app.control, "inspect", lambda timeout: ResizedInspect(timeout))
    assert tasks.queue_overview()["workers"][0]["concurrency"] == 6
