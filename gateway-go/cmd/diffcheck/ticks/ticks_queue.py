"""queue_loop (services/orchestrator.py): its rounds, as internal/api/jobs_queue.go names them."""
import asyncio

from app.services import orchestrator as orch


async def queue_round():
    """The body of queue_loop's `while` (orchestrator.py, «free = concurrency() …»): what is free
    taken with claim, the refused closed; the taken runs, which _start would leave running in
    tasks, are awaited one after the other here (run_it), so the round ends with them."""
    free = orch.concurrency() - len(orch._running) - orch.running_cells
    if free <= 0 or orch._draining:
        return {"taken": [], "refused": []}
    taken, refused = await asyncio.to_thread(orch.claim, free)
    for run_id in taken:
        with orch._alive_lock:
            orch._alive.add(run_id)
        try:
            await orch.run_it(run_id)
        finally:
            with orch._alive_lock:
                orch._alive.discard(run_id)
    for run_id in refused:
        await orch._finalize_orch_run(run_id, "FAILURE", orch.ALREADY_RUNNING, only_if_queued=True)
    return {"taken": taken, "refused": refused}


async def orphans():
    return await orch.close_orphans()


async def mine_start():
    """queue_loop's start: close_mine(True), then stop_steps of each."""
    ids = await asyncio.to_thread(orch.close_mine, True)
    for run_id in ids:
        await orch.stop_steps(run_id)
    return ids


async def mine_stop():
    return await asyncio.to_thread(orch.close_mine, False)


async def beat():
    return await asyncio.to_thread(orch.beat)


TICKS = {"queue": queue_round, "queue.orphans": orphans, "queue.mine_start": mine_start, "queue.mine_stop": mine_stop,
         "queue.beat": beat}
