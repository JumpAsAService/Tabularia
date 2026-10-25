"""The engine's API serves more synchronous requests at once than the library's default.

A preview holds a thread until the worker has finished. With 40 threads, the forty-first
preview in progress waits in line even with the workers idle — and with it every other
synchronous route of the engine.
"""
import anyio
import anyio.to_thread

from app.api import main
from app.core.config import AppSettings, get_settings


def test_the_default_is_wider_than_the_librarys():
    assert AppSettings().engine_api_threads == 200


def test_at_start_the_thread_limit_is_the_configured_one():
    async def trial() -> tuple[int, int]:
        before = int(anyio.to_thread.current_default_thread_limiter().total_tokens)
        async with main.lifespan(main.app):
            return before, int(anyio.to_thread.current_default_thread_limiter().total_tokens)

    before, after = anyio.run(trial)
    assert before == 40  # the library's default, what there was
    assert after == get_settings().app.engine_api_threads
