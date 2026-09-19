"""
Materializzazione DIFFERITA della step-cache, comune a tutti i motori.

Prima ogni motore scriveva in cache l'output del passo a monte DENTRO la
preview, prima di rispondere: su una tabella da 25 milioni di righe erano 40 s
di attesa a ogni click, e il click successivo annullava la copia e la faceva
ripartire da zero. Ora la preview risponde dopo la sua sola query e lascia i
passi da materializzare in sospeso (`take_pending`): il task della preview li
affida a `materialize_step_task`, che chiama `materialize()` fuori dalla
richiesta, una volta sola (lucchetto su Valkey).

Tetto: un passo con piu' di `CACHE__MAX_STEP_ROWS` righe non viene messo in
cache (conteggio LIMITATO, che si ferma appena supera il tetto): una copia
grande quanto la sorgente non fa guadagnare nulla rispetto a rileggere il
parquet originale, e costa gigabyte di scrittura. Il passo viene ricordato
come «troppo grande» per non ricontarlo a ogni click.

Ogni motore fornisce `_materialize_session(source)` (un contesto d'esecuzione
proprio) e `_bounded_rows(ctx, obj, n)` (quante righe ha `obj`, contate al
massimo fino a n+1), e chiama `_too_big(ctx, obj, final)` dentro `_materialize`.
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
    """Tetto di righe per un passo in cache (0 = nessun tetto)."""
    return int(get_settings().cache.max_step_rows or 0)


class DeferredStepCache:
    """Mixin per gli engine: `cache` (StepCache) e `_source_id` vengono dall'engine."""

    cache: StepCache

    def _pending_list(self) -> list[tuple[DataSource, list[dict]]]:
        if not hasattr(self, "_pending"):
            self._pending: list[tuple[DataSource, list[dict]]] = []
        return self._pending

    def _defer_materialization(self, source: DataSource, ops: list[Operation]) -> None:
        """Mette in sospeso la materializzazione di `ops` se non e' gia' in cache
        (o gia' scartata perche' troppo grande)."""
        if not ops:
            return
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]  # type: ignore[attr-defined]
        if self.cache.has(final) or self.cache.is_skipped(final):
            return
        pending = self._pending_list()
        if any(plan_hashes(self._source_id(s), o)[-1] == final for s, o in pending):  # type: ignore[attr-defined]
            return  # gia' in sospeso
        pending.append((source, [op.model_dump() for op in ops]))

    def _cache_state(self, source: DataSource, ops: list[Operation], use_cache: bool) -> tuple[str | None, int | None]:
        """(stato, tetto) della cache del passo `ops`, per la risposta della
        preview: e' cio' che l'editor mostra sul nodo. Nessuna query."""
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
        """Le materializzazioni rimaste da fare (e le dimentica)."""
        out = list(self._pending_list())
        self._pending_list().clear()
        return out

    def materialize(self, source: DataSource, operations: list[dict[str, Any]]) -> bool:
        """Materializza in cache l'output di `operations`, fuori dalla richiesta.
        True se ha scritto qualcosa. Un lucchetto su Valkey evita che due click
        sullo stesso passo lo scrivano due volte."""
        ops = _coerce(operations)
        if not ops:
            return False
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]  # type: ignore[attr-defined]
        if self.cache.has(final) or self.cache.is_skipped(final):
            return False
        if not self.cache.try_lock(final):
            logger.info("step %s: materializzazione gia' in corso altrove", final[:12])
            return False
        try:
            with self._materialize_session(source) as ctx:  # type: ignore[attr-defined]
                self._materialize(ctx, source, ops)  # type: ignore[attr-defined]
        finally:
            self.cache.unlock(final)
        return self.cache.has(final)

    def _too_big(self, ctx, obj, final: str) -> bool:
        """Il passo supera il tetto? Allora non va in cache, e lo si ricorda."""
        cap = max_step_rows()
        if cap <= 0:
            return False
        n = int(self._bounded_rows(ctx, obj, cap))  # type: ignore[attr-defined]
        if n <= cap:
            return False
        self.cache.mark_skipped(final)
        logger.info("step %s: oltre %d righe, non va in cache (si ricalcola dalla sorgente)", final[:12], cap)
        return True

    def _cache_output_allowed(self, rows_written: int) -> bool:
        """L'output di un run finisce in cache solo se sta nel tetto."""
        cap = max_step_rows()
        return cap <= 0 or rows_written <= cap
