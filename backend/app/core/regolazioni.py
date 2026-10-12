"""Regolazioni dal pannello: i parametri del motore che l'amministratore cambia da
Amministrazione → Impostazioni, senza toccare l'ambiente e senza riavvii.

Solo quelli che il motore legge al momento dell'uso (`VOCI`): dimensione dei row
group, timeout di lettura degli import, tetto dell'export, durata dei task, cache
dei passi, ClickHouse, BigQuery. Restano fuori, e si cambiano solo nell'ambiente,
le credenziali, gli indirizzi, e quello che un processo legge una volta all'avvio
(concorrenza e memoria dei worker, thread dell'API).

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
    unita: str  # rows | seconds | bytes | threads
    minimo: int
    massimo: int

    @property
    def sezione(self) -> str:
        return self.chiave.split(".", 1)[0]

    @property
    def campo(self) -> str:
        return self.chiave.split(".", 1)[1]

    @property
    def variabile(self) -> str:
        """La variabile d'ambiente che la imposta (e che resta il valore di partenza)."""
        return f"{self.sezione.upper()}__{self.campo.upper()}"


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
)
PER_CHIAVE = {v.chiave: v for v in VOCI}

# vincoli fra due voci: (minore, maggiore) — il primo deve restare SOTTO il secondo
#  - il limite morbido serve a chiudere in ordine PRIMA che il processo venga ucciso;
#  - chi aspetta un import deve leggere il timeout di lettura, non il task ucciso.
VINCOLI = (
    ("celery.task_soft_time_limit", "celery.task_time_limit"),
    ("ingest.db_read_timeout_seconds", "celery.task_soft_time_limit"),
)


def _valore(s: Any, chiave: str) -> Any:
    sezione, campo = chiave.split(".", 1)
    return getattr(getattr(s, sezione), campo)


def valida(valori: Any, base: Any, limite_duro_max: Optional[int] = None,
           cambiate: Optional[set[str]] = None) -> tuple[dict[str, int], list[dict]]:
    """(valori buoni, errori). Gli errori hanno un codice, non una frase: la frase la
    scrive il frontend nella lingua di chi guarda.

    `base` sono le impostazioni dell'ambiente; i vincoli fra voci si controllano
    sui valori EFFETTIVI (ambiente con sopra i buoni). `limite_duro_max` lo dà il
    gateway: dichiara perso un run dopo una sua soglia, e il limite duro deve
    restarle sotto. `cambiate`: le voci che chi scrive ha appena cambiato — quando
    un vincolo salta fra una cambiata e una che c'era già, la colpa è della
    cambiata (l'errore deve nominare quello che si è toccato)."""
    buoni: dict[str, int] = {}
    errori: list[dict] = []
    if not isinstance(valori, dict):  # un JSON valido che non è un oggetto (una lista, un numero)
        return {}, [{"key": "", "code": "unknown"}]
    for chiave, v in valori.items():
        voce = PER_CHIAVE.get(chiave)
        if voce is None:
            errori.append({"key": chiave, "code": "unknown"})
            continue
        if isinstance(v, bool) or not isinstance(v, int):
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
    # finché ne salta uno: togliere una voce può rompere un vincolo già controllato
    # (morbido tolto → torna quello dell'ambiente, magari sopra il duro impostato)
    cambiate = cambiate or set()
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


def applica(base: Any, buoni: dict[str, int]) -> Any:
    """`base` con i valori sopra, sezione per sezione (le altre restano gli stessi oggetti)."""
    if not buoni:
        return base
    per_sezione: dict[str, dict[str, int]] = {}
    for chiave, v in buoni.items():
        sezione, campo = chiave.split(".", 1)
        per_sezione.setdefault(sezione, {})[campo] = v
    return base.model_copy(update={
        sezione: getattr(base, sezione).model_copy(update=campi) for sezione, campi in per_sezione.items()
    })


# ── lettura da Valkey, per processo ─────────────────────────────────────────

_lock = threading.Lock()
_letto = float("-inf")  # quando si è riletto l'ultima volta (monotonic)
_valori: dict[str, int] = {}  # gli ultimi letti e validi
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


def _correnti() -> dict[str, int]:
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
            return _valori
        from app.core.config import settings_dall_ambiente

        if not isinstance(grezzi, dict):
            # scritto da qualcuno che non è il gateway: restano le ultime valide, e il
            # gateway rimette le sue entro un minuto
            logger.warning("regolazioni: in Valkey c'è un %s invece di un oggetto: restano le ultime lette",
                           type(grezzi).__name__)
            return _valori
        buoni, errori = valida(grezzi, settings_dall_ambiente())
        if buoni != _valori:
            if errori:
                logger.warning("regolazioni: scartate perché non valide: %s", errori)
            logger.info("regolazioni dal pannello: %s", buoni or "nessuna (valgono quelle dell'ambiente)")
            _valori = buoni
            _versione += 1
            _copie.clear()
    return _valori


def applicate(base: Any) -> Any:
    """Le impostazioni dell'ambiente con sopra le regolazioni correnti. È la strada
    di ogni `get_settings()`: costa un confronto finché non c'è da rileggere."""
    global _risultato
    valori = _correnti()
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
    campi = {k.split(".", 1)[1]: v for k, v in _correnti().items() if k.startswith(sezione + ".")}
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


def dimentica() -> None:
    """Si rilegge alla prossima richiesta (test, e `get_settings.cache_clear()`)."""
    global _letto, _valori, _risultato, _versione
    with _lock:
        _letto = float("-inf")
        _valori = {}
        _risultato = None
        _versione += 1
        _copie.clear()


def scrivi(buoni: dict[str, int]) -> None:
    """Le nuove regolazioni per tutti i processi (le ha già validate chi chiama).
    Questo processo le usa subito; gli altri entro RILEGGI_OGNI."""
    if buoni:
        _client().set(CHIAVE, json.dumps(buoni, sort_keys=True))
    else:
        _client().delete(CHIAVE)
    dimentica()
