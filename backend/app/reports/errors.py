"""The one exception of a render that cannot go on, and what is said of a worker killed while it read."""
from typing import Iterable


class ReportError(Exception):
    """Why a report cannot be rendered, for the person who asked, and the tag the export
    routes turn into an HTTP status (app/api/routes/tasks.py): `unprocessable` (422, the
    definition, the data or the parameters), `not_found` (404, a source that is gone),
    `unavailable` (503, the chart service), `timeout` (504), `bad_request` (400)."""

    def __init__(self, message: str, tag: str = "unprocessable"):
        super().__init__(message)
        self.message = message
        self.tag = tag


class OverCap(ReportError):
    """A dataset with more rows than a report reads."""


def worker_killed(e: object) -> bool:
    """Celery's word for a worker process that died with SIGKILL while it worked («Worker exited
    prematurely: signal 9 (SIGKILL)»), as the result backend gives it back. Only the kernel sends a
    child that signal in our containers, and its OOM killer does it at the memory limit, choosing the
    process using most: the one reading the rows. Celery's own kills say something else (a hard time
    limit is TimeLimitExceeded, a revoke is SIGTERM or SIGUSR1), and a container stopped or OOMKilled
    whole takes the main process too, which then writes no result at all (worker_pool.lost)."""
    from celery.exceptions import WorkerLostError

    return isinstance(e, WorkerLostError) and "signal 9" in str(e)


def out_of_memory(what: str, datasets: Iterable[str]) -> str:
    """What a dashboard or a report (`what`: «a dashboard», «a report») says when its worker was killed:
    the dataset named when it read one only."""
    names = list(datasets)
    if len(names) == 1:
        return (f"«{names[0]}» is too big for {what} to read: the worker ran out of memory. Aggregate it first, "
                f"in its query or in a flow — {what} works best on summarized data.")
    return (f"The data is too big for {what} to read: the worker ran out of memory. Aggregate it first, "
            f"in its queries or in a flow — {what} works best on summarized data.")
