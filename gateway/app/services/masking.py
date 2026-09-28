"""Maschera i dati personali per chi legge senza comandare.

Un osservatore vede i pannelli di amministrazione, e quei pannelli contengono
email, indirizzi IP e la traccia di cosa ha fatto ciascuno. Sono i dati che
servono a un amministratore per decidere, e che a chi soltanto guarda non
servono affatto: gli basta sapere CHE COSA succede nell'installazione, non CHI
c'è dietro.

Non è una concessione a una demo pubblica — anche se nasce lì. Un osservatore
che verifica che le cose girino senza poter identificare le persone è la figura
più difendibile delle due, e i pannelli restano leggibili: un'attività resta
attribuita in modo stabile (la stessa email dà sempre la stessa maschera),
quindi «questi tre accessi sono la stessa persona» si vede ancora.

Un amministratore non passa da qui: chi comanda vede.
"""
from __future__ import annotations


def mask_email(email: str | None) -> str:
    """`alice.rossi@esempio.it` → `a***@esempio.it`.

    Il dominio resta: dice se l'accesso viene da dentro o da fuori
    l'organizzazione, che è informazione utile e non identifica nessuno. La
    prima lettera tiene distinte due persone nello stesso elenco senza dire chi
    sono."""
    testo = (email or "").strip()
    if "@" not in testo:
        return "***" if testo else ""
    locale, _, dominio = testo.rpartition("@")
    return f"{locale[:1]}***@{dominio}" if locale else f"***@{dominio}"
