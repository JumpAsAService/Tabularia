"""Regolazioni dal pannello: i parametri del motore che l'amministratore cambia da
Amministrazione → Impostazioni, senza toccare l'ambiente e senza riavvii.

Quelli che il motore legge al momento dell'uso (`VOCI` senza `rollout`): dimensione
dei row group, timeout di lettura degli import, tetto dell'export, durata dei task,
cache dei passi, ClickHouse, BigQuery, i tetti di SFTP, cartelle S3, API HTTP e
segnaposto. Worker concurrency is one of them: every worker resizes its own pool from
`workers.*` while it runs (tasks/worker_pool.py).

Those a process reads once when it starts (`rollout`: memory and recycling of the
workers, the API's threads, the embedded engines' memory, the beat's intervals) are
taken from here ONCE, at the process's first reading (`all_avvio`), and stay frozen
until it starts again: the panel saves them and the components named in `rollout` are
rolled out (docs/specs/panel-settings.md). A pod that starts later takes them as well.
A few are not settings fields but variables read at import (`ambiente`).

Restano fuori, e si cambiano solo nell'ambiente, le credenziali, gli indirizzi, e gli
interruttori legati alle risorse del chart.

Come arrivano: il gateway le conserva nel suo database (la fonte vera, con l'audit)
e le scrive qui con `PUT /settings`; il motore le tiene in Valkey (`CHIAVE`), che
API e worker condividono, e ogni processo le rilegge al più ogni `RILEGGI_OGNI`
secondi. Il gateway le rispedisce a intervalli: se Valkey si svuota, tornano da
sole. Ogni valore si controlla due volte, quando arriva e quando si legge: uno non
valido si scarta (vale quello dell'ambiente) e lo si scrive nel log.

`get_settings()` (core/config.py) restituisce l'ambiente con queste sovrapposte;
chi tiene una sezione delle impostazioni in un oggetto creato una volta (i motori
ClickHouse e BigQuery) la ripassa da `sovrapposte()` a ogni uso.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

CHIAVE = "tabularia:regolazioni"
RILEGGI_OGNI = 10.0  # secondi: dopo una modifica, ogni processo la vede entro questo tempo
SETTE_GIORNI = 7 * 24 * 3600


@dataclass(frozen=True)
class Voce:
    chiave: str  # "<sezione>.<campo>" delle impostazioni del motore
    categoria: str  # raggruppa le voci nel pannello (le etichette le traduce il frontend)
    unita: str  # rows | seconds | bytes | threads | processes | files | keys | … | "" (a fraction, yes/no)
    minimo: float
    massimo: float
    tipo: str = "int"  # int | float | bool
    # the components that read it only when they start (engine, worker, preview-worker,
    # beat): empty = applied live
    rollout: tuple[str, ...] = ()
    # not a settings field: the variable read at import, and its default
    ambiente: str = ""
    predefinito: Any = None

    @property
    def sezione(self) -> str:
        return self.chiave.split(".", 1)[0]

    @property
    def campo(self) -> str:
        return self.chiave.split(".", 1)[1]

    @property
    def variabile(self) -> str:
        """La variabile d'ambiente che la imposta (e che resta il valore di partenza)."""
        return self.ambiente or f"{self.sezione.upper()}__{self.campo.upper()}"


WORKERS = ("worker", "preview-worker")

# Gli intervalli sono più stretti di quelli che l'ambiente accetta: dal pannello
# non si deve poter mettere in ginocchio il sistema per un errore di battitura (un
# row group enorme manda in OOM l'import, un timeout di pochi secondi fa fallire
# tutto). Chi ha davvero bisogno di un valore fuori intervallo lo mette nell'ambiente.
VOCI: tuple[Voce, ...] = (
    Voce("ingest.parquet_row_group_rows", "import", "rows", 10_000, 5_000_000),
    Voce("ingest.db_read_timeout_seconds", "import", "seconds", 30, 86_400),
    Voce("export.max_rows", "export", "rows", 0, 100_000_000),
    Voce("celery.task_time_limit", "execution", "seconds", 60, SETTE_GIORNI),
    Voce("celery.task_soft_time_limit", "execution", "seconds", 30, SETTE_GIORNI),
    Voce("cache.ttl_seconds", "cache", "seconds", 300, 30 * 24 * 3600),
    Voce("cache.max_step_rows", "cache", "rows", 0, 100_000_000),
    Voce("clickhouse_external.parquet_scan_max_threads", "clickhouse", "threads", 0, 64),
    Voce("clickhouse_external.max_execution_time", "clickhouse", "seconds", 0, 86_400),
    Voce("clickhouse_external.materialize_min_rows", "clickhouse", "rows", 0, 1_000_000_000),
    Voce("clickhouse_external.materialize_ttl_seconds", "clickhouse", "seconds", 60, SETTE_GIORNI),
    Voce("bigquery.maximum_bytes_billed", "bigquery", "bytes", 0, 100 * 1024**4),
    Voce("clickhouse_external.connect_timeout", "clickhouse", "seconds", 1, 300),
    Voce("sftp.max_file_bytes", "sftp", "bytes", 1024**2, 100 * 1024**3),
    Voce("sftp.max_files", "sftp", "files", 1, 100_000),
    Voce("sftp.max_total_bytes", "sftp", "bytes", 1024**2, 1024**4),
    Voce("sftp.timeout_seconds", "sftp", "seconds", 1, 600, tipo="float"),
    Voce("s3_source.max_file_bytes", "s3", "bytes", 1024**2, 100 * 1024**3),
    Voce("s3_source.max_files", "s3", "files", 1, 100_000),
    Voce("s3_source.max_total_bytes", "s3", "bytes", 1024**2, 1024**4),
    Voce("s3_source.max_listed", "s3", "keys", 100, 10_000_000),
    Voce("s3_source.max_request_seconds", "s3", "seconds", 10, 86_400, tipo="float"),
    Voce("api.timeout_seconds", "api", "seconds", 1, 600, tipo="float"),
    Voce("api.max_pages", "api", "pages", 1, 1_000_000),
    Voce("api.max_calls", "api", "calls", 1, 100_000),
    Voce("api.max_rows", "api", "rows", 1, 1_000_000_000),
    Voce("api.max_response_bytes", "api", "bytes", 1024, 10 * 1024**3),
    Voce("api.max_total_bytes", "api", "bytes", 1024, 1024**4),
    Voce("api.max_retries", "api", "retries", 0, 20),
    Voce("api.max_retry_wait_seconds", "api", "seconds", 0, 3600, tipo="float"),
    Voce("api.max_redirects", "api", "redirects", 0, 30),
    Voce("api.max_request_seconds", "api", "seconds", 1, 86_400, tipo="float"),
    Voce("api.min_interval_seconds", "api", "seconds", 0, 3600, tipo="float"),
    Voce("placeholders.max_values", "placeholders", "values", 1, 100_000),
    # set from the gateway's panel together with the gateway's own (the second barrier)
    Voce("upload.max_bytes", "upload", "bytes", 1024**2, 1024**4),  # never 0 from the panel: /tmp has a limit
    # ── read when a process starts ──
    Voce("app.engine_api_threads", "engine_api", "threads", 40, 2000, rollout=("engine",)),
    # at most under the chart's ingress timeout (proxy-read-timeout 300 s): past it people
    # would see the proxy's 504 while the engine keeps working; more goes in the environment
    Voce("previews.timeout_seconds", "engine_api", "seconds", 10, 290, tipo="float", rollout=("engine",),
         ambiente="PREVIEW_TIMEOUT_SECONDS", predefinito=120.0),
    Voce("celery.max_tasks_per_child", "execution", "tasks", 1, 100_000, rollout=WORKERS),
    Voce("celery.max_memory_per_child_kb", "execution", "kilobytes", 100 * 1024, 256 * 1024**2, rollout=WORKERS),
    Voce("celery.max_memory_headroom_frac", "execution", "", 0.1, 0.95, tipo="float", rollout=WORKERS),
    Voce("celery.max_memory_per_child_fallback_kb", "execution", "kilobytes", 100 * 1024, 256 * 1024**2, rollout=WORKERS),
    Voce("celery.max_memory_per_child_floor_kb", "execution", "kilobytes", 50 * 1024, 256 * 1024**2, rollout=WORKERS),
    Voce("cache.sweep_interval_seconds", "cache", "seconds", 60, 7 * 86_400, rollout=("beat",)),
    Voce("metrics.storage_stats_interval_seconds", "cache", "seconds", 10, 3600, rollout=("beat",)),
    Voce("chdb.max_memory_usage", "embedded", "bytes", 128 * 1024**2, 1024**4, rollout=WORKERS,
         ambiente="CHDB_MAX_MEMORY_USAGE", predefinito=1_500_000_000),
    Voce("chdb.max_bytes_before_external_group_by", "embedded", "bytes", 0, 1024**4, rollout=WORKERS,
         ambiente="CHDB_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY", predefinito=512 * 1024**2),
    Voce("chdb.max_bytes_before_external_sort", "embedded", "bytes", 0, 1024**4, rollout=WORKERS,
         ambiente="CHDB_MAX_BYTES_BEFORE_EXTERNAL_SORT", predefinito=512 * 1024**2),
    Voce("chdb.max_bytes_in_join", "embedded", "bytes", 0, 1024**4, rollout=WORKERS,
         ambiente="CHDB_MAX_BYTES_IN_JOIN", predefinito=512 * 1024**2),
    Voce("engine.max_cross_join_rows", "execution", "rows", 1000, 10_000_000_000, rollout=WORKERS,
         ambiente="ENGINE_MAX_CROSS_JOIN_ROWS", predefinito=50_000_000),
    # processes per worker container: shown in Admin → Workers, not among the settings
    Voce("workers.run_concurrency", "workers", "processes", 1, 32),
    Voce("workers.preview_concurrency", "workers", "processes", 1, 32),
)
PER_CHIAVE = {v.chiave: v for v in VOCI}

# vincoli fra due voci: (minore, maggiore) — il primo deve restare SOTTO il secondo
#  - il limite morbido serve a chiudere in ordine PRIMA che il processo venga ucciso;
#  - chi aspetta un import deve leggere il timeout di lettura, non il task ucciso.
VINCOLI = (
    ("celery.task_soft_time_limit", "celery.task_time_limit"),
    ("ingest.db_read_timeout_seconds", "celery.task_soft_time_limit"),
)


def _dall_ambiente(voce: Voce) -> Any:
    """A variable read at import (`Voce.ambiente`): what the environment gives it."""
    raw = (os.getenv(voce.ambiente) or "").strip()
    if not raw:
        return voce.predefinito
    try:
        return float(raw) if voce.tipo == "float" else int(raw)
    except ValueError:
        # as before these settings could come from the panel: a typing mistake in the
        # environment stops the process, it does not quietly become the default
        raise ValueError(f"{voce.ambiente}={raw!r} is not a {'number' if voce.tipo == 'float' else 'whole number'}") from None


def _valore(s: Any, chiave: str) -> Any:
    voce = PER_CHIAVE.get(chiave)
    if voce is not None and voce.ambiente:
        return _dall_ambiente(voce)
    sezione, campo = chiave.split(".", 1)
    return getattr(getattr(s, sezione), campo)


def valida(valori: Any, base: Any, limite_duro_max: Optional[int] = None,
           cambiate: Optional[set[str]] = None) -> tuple[dict[str, Any], list[dict]]:
    """(valori buoni, errori). Gli errori hanno un codice, non una frase: la frase la
    scrive il frontend nella lingua di chi guarda.

    `base` sono le impostazioni dell'ambiente; i vincoli fra voci si controllano
    sui valori EFFETTIVI (ambiente con sopra i buoni). `limite_duro_max` lo dà il
    gateway: dichiara perso un run dopo una sua soglia, e il limite duro deve
    restarle sotto. `cambiate`: le voci che chi scrive ha appena cambiato — quando
    un vincolo salta fra una cambiata e una che c'era già, la colpa è della
    cambiata (l'errore deve nominare quello che si è toccato)."""
    buoni: dict[str, Any] = {}
    errori: list[dict] = []
    if not isinstance(valori, dict):  # un JSON valido che non è un oggetto (una lista, un numero)
        return {}, [{"key": "", "code": "unknown"}]
    for chiave, v in valori.items():
        voce = PER_CHIAVE.get(chiave)
        if voce is None:
            errori.append({"key": chiave, "code": "unknown"})
            continue
        if voce.tipo == "bool":
            if isinstance(v, bool):
                buoni[chiave] = v
            else:
                errori.append({"key": chiave, "code": "boolean"})
            continue
        if voce.tipo == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errori.append({"key": chiave, "code": "number"})
                continue
            try:
                v = float(v)
            except OverflowError:  # an integer beyond any decimal (10**400)
                errori.append({"key": chiave, "code": "max", "limit": voce.massimo})
                continue
            if not math.isfinite(v):
                errori.append({"key": chiave, "code": "number"})
                continue
        elif isinstance(v, bool) or not isinstance(v, int):
            errori.append({"key": chiave, "code": "integer"})
            continue
        massimo = voce.massimo
        if chiave == "celery.task_time_limit" and limite_duro_max is not None:
            massimo = min(massimo, limite_duro_max)
        if v < voce.minimo:
            errori.append({"key": chiave, "code": "min", "limit": voce.minimo})
        elif v > massimo:
            errori.append({"key": chiave, "code": "max", "limit": massimo})
        else:
            buoni[chiave] = v
    cambiate = cambiate or set()
    # the hard limit taken back to the environment's: that value too must stay under the
    # gateway's lost-run threshold (else a run still alive would be closed as failed)
    duro = "celery.task_time_limit"
    if limite_duro_max is not None and duro not in buoni and duro in cambiate \
            and _valore(base, duro) > limite_duro_max:
        errori.append({"key": duro, "code": "env_too_high", "limit": limite_duro_max})
    # finché ne salta uno: togliere una voce può rompere un vincolo già controllato
    # (morbido tolto → torna quello dell'ambiente, magari sopra il duro impostato)
    toccato = True
    while toccato:
        toccato = False
        for minore, maggiore in VINCOLI:
            if minore not in buoni and maggiore not in buoni:
                continue  # entrambe dall'ambiente: non le ha toccate nessuno qui
            a = buoni.get(minore, _valore(base, minore))
            b = buoni.get(maggiore, _valore(base, maggiore))
            if a < b:
                continue
            # la colpa è di una impostata qui, e fra due impostate di quella appena
            # cambiata; a parità, della minore
            impostate = [k for k in (minore, maggiore) if k in buoni]
            colpevole = next((k for k in impostate if k in cambiate), None) if len(impostate) == 2 else None
            colpevole = colpevole or impostate[0]
            altra = maggiore if colpevole == minore else minore
            buoni.pop(colpevole)
            errori.append({"key": colpevole, "code": "below" if colpevole == minore else "above",
                           "other": altra, "limit": b if colpevole == minore else a})
            toccato = True
    return buoni, errori


def applica(base: Any, buoni: dict[str, Any]) -> Any:
    """`base` con i valori sopra, sezione per sezione (le altre restano gli stessi oggetti).
    The variables read at import are not settings: their modules ask `valore_all_avvio`."""
    buoni = {k: v for k, v in buoni.items() if not PER_CHIAVE[k].ambiente}
    if not buoni:
        return base
    per_sezione: dict[str, dict[str, Any]] = {}
    for chiave, v in buoni.items():
        sezione, campo = chiave.split(".", 1)
        per_sezione.setdefault(sezione, {})[campo] = v
    return base.model_copy(update={
        sezione: getattr(base, sezione).model_copy(update=campi) for sezione, campi in per_sezione.items()
    })


# ── lettura da Valkey, per processo ─────────────────────────────────────────

_lock = threading.Lock()
_letto = float("-inf")  # quando si è riletto l'ultima volta (monotonic)
_valori: dict[str, Any] = {}  # gli ultimi letti e validi, live ones and rollout ones
# the rollout ones as they were at this process's first reading: frozen until it starts
# again (None = not read yet)
_avvio: Optional[dict[str, Any]] = None
_versione = 0  # cresce a ogni cambiamento: chiave delle copie in cache
_risultato: tuple[Any, int, Any] | None = None  # (base, versione, impostazioni)
# (id dell'oggetto, versione) → (l'oggetto, la sua copia): l'oggetto resta qui
# perché il suo id non passi a un altro mentre la copia è in cache
_copie: dict[tuple[int, int], tuple[Any, Any]] = {}
_redis = None


def _client():
    global _redis
    if _redis is None:
        import redis

        from app.core.config import settings_dall_ambiente

        # timeout corti: la lettura sta sul percorso di chi chiede le impostazioni,
        # e se Valkey non risponde si va avanti con gli ultimi valori letti
        _redis = redis.Redis.from_url(settings_dall_ambiente().redis.url, decode_responses=True,
                                      socket_timeout=0.5, socket_connect_timeout=0.5)
    return _redis


def leggi_grezzi() -> dict:
    raw = _client().get(CHIAVE)
    return json.loads(raw) if raw else {}


def _fissa_avvio(valori: dict[str, Any]) -> None:
    global _avvio
    if _avvio is None:
        _avvio = {k: v for k, v in valori.items() if PER_CHIAVE[k].rollout}
        if _avvio:
            logger.info("regolazioni read when this process starts, from the panel: %s", _avvio)


def _correnti() -> dict[str, Any]:
    global _letto, _valori, _versione
    ora = time.monotonic()
    if ora - _letto < RILEGGI_OGNI:
        return _valori
    with _lock:
        if ora - _letto < RILEGGI_OGNI:  # un altro thread ha appena riletto
            return _valori
        _letto = ora
        try:
            grezzi = leggi_grezzi()
        except Exception as e:  # noqa: BLE001 — Valkey giù: restano gli ultimi valori
            logger.warning("regolazioni: Valkey non risponde, restano le ultime lette (%s)", e)
            _fissa_avvio(_valori)
            return _valori
        from app.core.config import settings_dall_ambiente

        if not isinstance(grezzi, dict):
            # scritto da qualcuno che non è il gateway: restano le ultime valide, e il
            # gateway rimette le sue entro un minuto
            logger.warning("regolazioni: in Valkey c'è un %s invece di un oggetto: restano le ultime lette",
                           type(grezzi).__name__)
            _fissa_avvio(_valori)
            return _valori
        buoni, errori = valida(grezzi, settings_dall_ambiente())
        _fissa_avvio(buoni)
        if buoni != _valori:
            if errori:
                logger.warning("regolazioni: scartate perché non valide: %s", errori)
            logger.info("regolazioni dal pannello: %s", buoni or "nessuna (valgono quelle dell'ambiente)")
            _valori = buoni
            _versione += 1
            _copie.clear()
    return _valori


def _in_vigore() -> dict[str, Any]:
    """The live ones as they are now, the rollout ones as this process started with."""
    correnti = _correnti()
    return {**{k: v for k, v in correnti.items() if not PER_CHIAVE[k].rollout}, **(_avvio or {})}


ATTESA_ALL_AVVIO = 45.0  # seconds: the gateway sends its values at least every minute


def attendi_al_primo_avvio(massimo: float = ATTESA_ALL_AVVIO) -> bool:
    """A worker or beat that starts on an empty Valkey (a restart without persistence, a
    new cluster) would freeze the environment's values of the settings read at start, for
    its whole life, while the panel shows the panel's. It waits for the gateway's values
    instead, up to `massimo` seconds; True if they came."""
    inizio = time.monotonic()
    fine = inizio + massimo
    while True:
        try:
            if _client().get(CHIAVE) is not None:
                attesa = time.monotonic() - inizio
                if attesa >= 2:  # a warning: Celery's logging is not set up yet at import
                    logger.warning("regolazioni: waited %ds for the gateway's values (Valkey had none)", int(attesa))
                return True
        except Exception:  # noqa: BLE001 — Valkey not up yet: the broker waits for it too
            pass
        if time.monotonic() >= fine:
            logger.warning("regolazioni: no values from the gateway after %ss: this process starts with the "
                           "environment's for the settings read at start", int(massimo))
            return False
        time.sleep(2)


def all_avvio(base: Any) -> Any:
    """`base` with the rollout ones this process started with (Celery's configuration,
    built from the environment's settings). Forces the first reading."""
    _correnti()
    return applica(base, _avvio or {})


def valore_all_avvio(variabile: str) -> Any:
    """A variable read at import (CHDB_MAX_MEMORY_USAGE…): the panel's value this process
    started with, else the environment's."""
    voce = next(v for v in VOCI if v.ambiente == variabile)
    _correnti()
    return (_avvio or {}).get(voce.chiave, _dall_ambiente(voce))


def avvio() -> dict[str, Any]:
    """The rollout ones this process started with (the panel's; the others: the environment's)."""
    _correnti()
    return dict(_avvio or {})


def applicate(base: Any) -> Any:
    """Le impostazioni dell'ambiente con sopra le regolazioni correnti. È la strada
    di ogni `get_settings()`: costa un confronto finché non c'è da rileggere."""
    global _risultato
    valori = _in_vigore()
    if not valori:
        return base
    r = _risultato
    if r is not None and r[0] is base and r[1] == _versione:
        return r[2]
    s = applica(base, valori)
    _risultato = (base, _versione, s)
    return s


def sovrapposte(oggetto: Any, sezione: str) -> Any:
    """Una sezione delle impostazioni tenuta da un oggetto creato una volta (il
    motore ClickHouse tiene la sua dall'avvio): con sopra le regolazioni correnti
    di quella sezione. Lo stesso oggetto se non ce ne sono."""
    campi = {k.split(".", 1)[1]: v for k, v in _in_vigore().items()
             if k.startswith(sezione + ".") and not PER_CHIAVE[k].ambiente}
    if not campi:
        return oggetto
    chiave = (id(oggetto), _versione)
    trovata = _copie.get(chiave)
    if trovata is not None and trovata[0] is oggetto:
        return trovata[1]
    if len(_copie) > 64:  # gli oggetti vivi sono pochi (uno per motore): è solo un tetto
        _copie.clear()
    copia = oggetto.model_copy(update=campi)
    _copie[chiave] = (oggetto, copia)
    return copia


def dimentica(anche_avvio: bool = True) -> None:
    """Si rilegge alla prossima richiesta (test, e `get_settings.cache_clear()`).
    `anche_avvio=False` after a change: what this process started with stays."""
    global _letto, _valori, _risultato, _versione, _avvio
    with _lock:
        _letto = float("-inf")
        _valori = {}
        if anche_avvio:
            _avvio = None
        _risultato = None
        _versione += 1
        _copie.clear()


def scrivi(buoni: dict[str, Any]) -> None:
    """Le nuove regolazioni per tutti i processi (le ha già validate chi chiama).
    Questo processo le usa subito; gli altri entro RILEGGI_OGNI."""
    # always written, `{}` too: a key present means the gateway has sent its values, which
    # the workers wait for when they start (`attendi_al_primo_avvio`)
    _client().set(CHIAVE, json.dumps(buoni, sort_keys=True))
    dimentica(anche_avvio=False)
