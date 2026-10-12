"""Regolazioni del motore dal pannello (vedi core/regolazioni.py). Le chiama solo il
gateway, come ogni rotta di questa API: il gateway decide chi può, tiene la storia
e l'audit, e qui si controlla e si scrive per tutti i processi del motore."""
from typing import Any, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import regolazioni
from app.core.config import settings_dall_ambiente

router = APIRouter(prefix="/settings", tags=["settings"])


class Scrittura(BaseModel):
    values: dict[str, Any] = {}
    # la soglia oltre la quale il gateway dichiara perso un run, meno il margine:
    # il limite duro dei task deve restarle sotto
    max_task_time_limit: Optional[int] = None
    # le voci che chi scrive ha appena cambiato: se un vincolo salta, l'errore nomina loro
    changed: list[str] = []


def _catalogo(max_task_time_limit: Optional[int] = None) -> dict:
    env = settings_dall_ambiente()
    try:
        impostate, scartate = regolazioni.valida(regolazioni.leggi_grezzi(), env)
    except Exception:  # noqa: BLE001 — Valkey giù: si mostrano i valori dell'ambiente
        impostate, scartate = {}, []
    voci = []
    for v in regolazioni.VOCI:
        massimo = v.massimo
        if v.chiave == "celery.task_time_limit" and max_task_time_limit is not None:
            massimo = min(massimo, max_task_time_limit)
        voci.append({
            "key": v.chiave, "category": v.categoria, "unit": v.unita, "min": v.minimo, "max": massimo,
            "env_var": v.variabile, "env_value": regolazioni._valore(env, v.chiave),
            "value": impostate.get(v.chiave),
        })
    return {
        "items": voci,
        # le categorie dei motori che questa installazione non ha: il pannello le
        # mostra spente (regolarle non cambierebbe niente)
        "unavailable": [c for c, attiva in (("clickhouse", env.clickhouse_external.enabled),
                                            ("bigquery", env.bigquery.enabled)) if not attiva],
        "refresh_seconds": regolazioni.RILEGGI_OGNI,
        "discarded": scartate,
    }


@router.get("")
def leggi(max_task_time_limit: Optional[int] = None):
    return _catalogo(max_task_time_limit)


@router.put("")
def scrivi(body: Scrittura):
    """Sostituisce TUTTE le regolazioni (chi non c'è torna al valore dell'ambiente).
    Niente di scritto se anche una sola non va: 422 con l'elenco."""
    buoni, errori = regolazioni.valida(body.values, settings_dall_ambiente(), body.max_task_time_limit,
                                      cambiate=set(body.changed))
    if errori:
        return JSONResponse(status_code=422, content={"detail": {"errors": errori}})
    regolazioni.scrivi(buoni)
    return _catalogo(body.max_task_time_limit)
