"""Guardrail della modalità demo (sandbox pubblico).

In DEMO_MODE le superfici pericolose per un'esposizione internet vengono chiuse:
- upload di file arbitrari,
- creazione di connessioni DB (che aprirebbe SSRF e gli output esterni).
Le viste admin restano visibili ma scoped al singolo visitatore (vedi routes/audit).
"""
from fastapi import HTTPException, status

from app.core.config import get_settings


def demo_active() -> bool:
    return get_settings().app.demo_mode


def block_in_demo(feature: str) -> None:
    """Solleva 403 se siamo in demo: `feature` è la cosa disabilitata (per il messaggio)."""
    if demo_active():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{feature} è disabilitato nell'ambiente demo pubblico.",
        )


def mask_ip(ip: str | None) -> str | None:
    """Anonimizza l'IP nell'audit demo (nessun dato personale esposto tra visitatori):
    IPv4 → primi due ottetti, resto oscurato; IPv6 → primo blocco."""
    if not ip:
        return ip
    if "." in ip:
        parts = ip.split(".")
        return ".".join(parts[:2] + ["x", "x"]) if len(parts) == 4 else "x.x.x.x"
    if ":" in ip:
        return ip.split(":")[0] + ":x"
    return "x"
