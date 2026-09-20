"""Quanto è documentata una datasource, e quando si può dire «pronta».

L'assistente legge la descrizione della TABELLA per capire se una domanda la
riguarda, e le descrizioni dei CAMPI per scrivere la query giusta. Senza, il
modello tira a indovinare dal nome della colonna — ed è proprio così che il
2026-09-19 si è inventato il contenuto di una tabella che non aveva mai letto.

Il giudizio sta qui e non nelle pagine perché lo mostrano in tre posti diversi
(catalogo, rail dell'assistente, ricerca): scritto tre volte, divergerebbe alla
prima modifica.
"""
from __future__ import annotations

from typing import Any


def conta_descritte(columns: list[Any], descriptions: dict[str, str]) -> tuple[int, int]:
    """(campi descritti, campi totali).

    Una descrizione può stare nel dizionario curato a mano o già dentro la
    colonna: vale l'una o l'altra, e gli spazi non contano come descrizione."""
    totali = 0
    descritte = 0
    for c in columns:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        totali += 1
        testo = descriptions.get(c["name"]) or c.get("description") or ""
        if isinstance(testo, str) and testo.strip():
            descritte += 1
    return descritte, totali


def is_ai_ready(description: str | None, descritte: int, totali: int) -> bool:
    """Pronta = la tabella dice cosa è, e OGNI campo dice cosa significa.

    Servono entrambe: una tabella con tutti i campi descritti ma senza una
    descrizione propria non dice all'assistente *di cosa parla*, e una descritta
    con metà dei campi anonimi lo lascia indovinare sul resto. Zero colonne non è
    «pronta»: è una datasource di cui non sappiamo ancora niente."""
    return bool((description or "").strip()) and totali > 0 and descritte == totali
