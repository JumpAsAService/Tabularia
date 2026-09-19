"""Quanto è costato un turno, senza listini scritti da noi.

`pydantic-ai` riporta il costo da solo in `RunUsage.cost`, ma lo cerca sotto il
PROVIDER: il nostro è un endpoint compatibile OpenAI, quindi la ricerca è
«questo modello, presso OpenAI». Funziona per i modelli che esistono anche lì
(`gpt-oss-120b`), fallisce per tutti gli altri — e il costo arriva `None` anche
quando il prezzo di quel modello è noto benissimo. Visto dal vivo il
2026-09-19: tutti i turni con `glm-5.2` salvati senza costo, mentre lo stesso
modello cercato per NOME si prezza.

Qui si aggiunge un solo ripiego: cercare per nome di modello, senza vincolare
il provider. I prezzi restano quelli del listino della libreria, che si
aggiorna con la libreria; noi non ne scriviamo nessuno.

**Resta una stima**, e va detto all'utente: è il listino pubblico del modello,
non la tariffa del fornitore che stai usando davvero. Quando non si trova
nulla, il costo è `None` — «non lo so», che non è «gratis».
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

# coda di versione/data che i fornitori appiccicano al nome
# (`mistral-small-3.2-24b-instruct-2506` → `…-instruct`)
_CODA_DATA = re.compile(r"-\d{4,}$")


def _per_nome(usage: Any, model_ref: str) -> Optional[Decimal]:
    from genai_prices import calc_price

    try:
        return calc_price(usage, model_ref=model_ref).total_price
    except Exception:  # noqa: BLE001 — modello sconosciuto al listino: non è un errore
        return None


def cost_of(usage: Any, model_id: str) -> Optional[Decimal]:
    """Il costo del turno in dollari, o `None` se non è determinabile.

    Ordine: quello che riporta pydantic-ai; poi il listino cercato per nome di
    modello; poi lo stesso nome senza la coda di data."""
    try:
        diretto = getattr(usage, "cost", None)
        if diretto is not None:
            return diretto
        if not model_id:
            return None
        senza_data = _CODA_DATA.sub("", model_id)
        for ref in ([model_id] if senza_data == model_id else [model_id, senza_data]):
            prezzo = _per_nome(usage, ref)
            if prezzo is not None:
                return prezzo
        logger.info("assistente: nessun prezzo noto per il modello %r, costo non riportato", model_id)
        return None
    except Exception:  # noqa: BLE001
        # il costo e' un'informazione accessoria: la risposta all'utente e' gia'
        # stata data e non si perde perche' un listino e' rotto
        logger.exception("assistente: calcolo del costo fallito per %r", model_id)
        return None
