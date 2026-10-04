"""L'API dell'engine serve insieme più richieste sincrone del default della libreria.

Un'anteprima occupa un thread finché il worker non ha finito. Con 40 thread, la
quarantunesima anteprima in corso aspetta in coda anche con i worker liberi — e
con lei ogni altra rotta sincrona dell'engine.
"""
import anyio
import anyio.to_thread

from app.api import main
from app.core.config import AppSettings, get_settings


def test_il_default_e_piu_largo_di_quello_della_libreria():
    assert AppSettings().engine_api_threads == 200


def test_all_avvio_il_limite_dei_thread_e_quello_configurato():
    async def prova() -> tuple[int, int]:
        prima = int(anyio.to_thread.current_default_thread_limiter().total_tokens)
        async with main.lifespan(main.app):
            return prima, int(anyio.to_thread.current_default_thread_limiter().total_tokens)

    prima, dopo = anyio.run(prova)
    assert prima == 40  # il default della libreria, ciò che si aveva
    assert dopo == get_settings().app.engine_api_threads
