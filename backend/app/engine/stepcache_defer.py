"""
DEFERRED materialization of the step cache, common to every engine.

Every engine used to write the upstream step's output to the cache INSIDE the preview,
before answering: on a table of 25 million rows that was 40 s of waiting at every click,
and the next click cancelled the copy and started it again from scratch. Now the preview
answers after its own query alone and leaves the steps to materialize pending
(`take_pending`): the preview's task hands them to `materialize_step_task`, which calls
`materialize()` outside the request, once (a lock on Valkey).

Cap: a step with more than `CACHE__MAX_STEP_ROWS` rows is not cached (a BOUNDED count,
which stops as soon as it goes over): a copy as large as the source gains nothing over
reading the original parquet, and costs gigabytes of writing. The step is remembered as
«too large» so as not to count it again at every click.

Every engine provides `_materialize_session(source)` (an execution context of its own)
and `_bounded_rows(ctx, obj, n)` (how many rows `obj` has, counted up to n+1 at most),
and calls `_too_big(ctx, obj, final)` inside `_materialize`.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.engine.base import DataSource, Operation
from app.engine.cache import StepCache, plan_hashes

logger = logging.getLogger(__name__)


def _coerce(operations) -> list[Operation]:
    return [op if isinstance(op, Operation) else Operation(**op) for op in operations]


def max_step_rows() -> int:
    """The row cap for a cached step (0 = no cap)."""
    return int(get_settings().cache.max_step_rows or 0)


class DeferredStepCache:
    """A mixin for the engines: `cache` (StepCache) and `_source_id` come from the engine."""

    cache: StepCache

    def _pending_list(self) -> list[tuple[DataSource, list[dict]]]:
        if not hasattr(self, "_pending"):
            self._pending: list[tuple[DataSource, list[dict]]] = []
        return self._pending

    def _defer_materialization(self, source: DataSource, ops: list[Operation]) -> None:
        """Leaves the materialization of `ops` pending unless it is cached already (or
        discarded already as too large)."""
        if not ops:
            return
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]  # type: ignore[attr-defined]
        if self.cache.has(final) or self.cache.is_skipped(final):
            return
        pending = self._pending_list()
        if any(plan_hashes(self._source_id(s), o)[-1] == final for s, o in pending):  # type: ignore[attr-defined]
            return  # pending already
        pending.append((source, [op.model_dump() for op in ops]))

    def _cache_state(self, source: DataSource, ops: list[Operation], use_cache: bool) -> tuple[str | None, int | None]:
        """(state, cap) of the cache of step `ops`, for the preview's answer: what the editor
        shows on the node. No query."""
        if not ops:
            return None, None
        cap = max_step_rows() or None
        if not use_cache:
            return "off", cap
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]  # type: ignore[attr-defined]
        if self.cache.has(final):
            return "hit", cap
        if self.cache.is_skipped(final):
            return "skipped", cap
        return "pending", cap

    def take_pending(self) -> list[tuple[DataSource, list[dict]]]:
        """The materializations left to do (and forgets them)."""
        out = list(self._pending_list())
        self._pending_list().clear()
        return out

    def materialize(self, source: DataSource, operations: list[dict[str, Any]]) -> bool:
        """Materializes the output of `operations` in the cache, outside the request. True
        when it wrote something. A lock on Valkey keeps two clicks on the same step from
        writing it twice."""
        ops = _coerce(operations)
        if not ops:
            return False
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]  # type: ignore[attr-defined]
        if self.cache.has(final) or self.cache.is_skipped(final):
            return False
        if not self.cache.try_lock(final):
            logger.info("step %s: materialization in progress elsewhere already", final[:12])
            return False
        try:
            with self._materialize_session(source) as ctx:  # type: ignore[attr-defined]
                self._materialize(ctx, source, ops)  # type: ignore[attr-defined]
        finally:
            self.cache.unlock(final)
        return self.cache.has(final)

    def _too_big(self, ctx, obj, final: str) -> bool:
        """Is the step over the cap? Then it is not cached, and that is remembered."""
        cap = max_step_rows()
        if cap <= 0:
            return False
        n = int(self._bounded_rows(ctx, obj, cap))  # type: ignore[attr-defined]
        if n <= cap:
            return False
        self.cache.mark_skipped(final)
        logger.info("step %s: over %d rows, not cached (computed again from the source)", final[:12], cap)
        return True

    def _cache_output_allowed(self, rows_written: int) -> bool:
        """A run's output is cached only when it fits under the cap."""
        cap = max_step_rows()
        return cap <= 0 or rows_written <= cap
