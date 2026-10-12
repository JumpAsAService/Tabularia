"""Inoltro del registro di audit a un SIEM: Splunk, con l'HTTP Event Collector (HEC).

Che cosa parte: ogni voce dell'audit (accessi, accessi falliti, creazioni e
modifiche, esecuzioni, export, permessi, token, impostazioni…), una per evento HEC,
con i campi della voce (`evento`). Da quando l'inoltro si accende la prima volta in
poi: lo storico di prima resta nel pannello Audit.

Come, senza perderne e senza code nuove: la tabella dell'audit È la coda. La
destinazione ricorda l'id dell'ultima voce consegnata (`cursor_id`); un giro prende
le voci dopo, le spedisce a lotti e sposta il cursore SOLO quando Splunk ha detto
sì (risposta 2xx, `code` 0). Un guasto (Splunk giù, token sbagliato, rete) lascia
il cursore dov'è e si ritenta con attese crescenti fino a `ATTESA_MAX`: niente si
salta. Quindi ALMENO UNA volta: un giro interrotto fra la risposta e il salvataggio
del cursore rispedisce quel lotto, e ogni evento porta l'`id` della voce per
riconoscere i doppioni (`| dedup id`).

Due cautele:
- gli id si assegnano prima del commit: una voce con un id più basso può diventare
  visibile dopo una con un id più alto. Si inoltrano solo le voci più vecchie di
  `RITARDO`, così il cursore non passa mai sopra una voce non ancora scritta;
- più processi (gli orchestratori) fanno girare l'inoltro: un lucchetto nel
  database (lo stesso meccanismo delle impostazioni del motore) fa sì che spedisca
  uno solo, lotto per lotto.

La scadenza del registro (AUDIT__RETENTION_MINUTES) resta quella promessa
nell'informativa: se Splunk non riceve per più di quel tempo, le voci più vecchie
si cancellano prima di partire. Il pannello mostra quanto si è indietro.

Connessione: come i webhook (services/webhooks.py): il nome si risolve una volta, ci
si collega all'indirizzo verificato, il certificato si controlla sul nome (CA di
certifi o SSL_CERT_FILE). `verify_tls` spento è una scelta esplicita
dell'amministratore per lo Splunk col certificato di fabbrica.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.models import AuditLog, SiemDestination
from app.services import webhooks

logger = logging.getLogger(__name__)

CICLO = 5.0  # secondi fra un giro e l'altro
RITARDO = timedelta(seconds=10)  # si inoltrano le voci più vecchie di così
LOTTO = 500  # voci per richiesta
MAX_BYTE = 900_000  # e non oltre questi byte (HEC ha un tetto per richiesta)
MAX_LOTTI_PER_GIRO = 20
ATTESA_MAX = 300  # secondi: il tetto delle attese fra un tentativo fallito e l'altro
LETTI = 4096
LUCCHETTO = 7_402_119
SOURCETYPE, SOURCE, HOST = "tabularia:audit", "tabularia", "tabularia"
# il canale HEC: obbligatorio se il token ha la conferma d'indicizzazione (useACK),
# ignorato altrimenti. Uno per processo basta
CANALE = str(uuid.uuid4())


def adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def endpoint(url: str) -> str:
    """L'indirizzo dell'HEC: l'amministratore può incollare la base
    (https://splunk:8088), il collector o l'endpoint completo."""
    u = url.strip().rstrip("/")
    if u.endswith("/services/collector/event"):
        return u
    if u.endswith("/services/collector"):
        return u + "/event"
    if urlsplit(u).path in ("", "/"):  # la libreria standard: non solleva su un indirizzo strano
        return u + "/services/collector/event"
    return u  # un percorso suo (un proxy davanti a Splunk): così com'è


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def evento(voce: AuditLog, d: SiemDestination) -> dict:
    """Una voce dell'audit come evento HEC. I nomi seguono il registro; `user` e
    `src` sono quelli che Splunk si aspetta (CIM) per chi e da dove."""
    try:
        dettaglio = json.loads(voce.detail) if voce.detail else None
    except ValueError:
        dettaglio = {"_raw": voce.detail}
    corpo = {
        "id": voce.id, "time": _utc(voce.ts).isoformat().replace("+00:00", "Z"),
        "action": voce.action, "outcome": voce.outcome,
        "user": voce.actor_label, "user_id": voce.actor_id, "src": voce.ip, "user_agent": voce.user_agent,
        "target_type": voce.target_type, "target_id": voce.target_id, "target_label": voce.target_label,
        "detail": dettaglio, "app": "tabularia",
    }
    hec = {
        "time": round(_utc(voce.ts).timestamp(), 3), "host": d.host or HOST, "source": d.source or SOURCE,
        "sourcetype": d.sourcetype or SOURCETYPE, "event": {k: v for k, v in corpo.items() if v is not None},
    }
    if d.index:
        hec["index"] = d.index
    return hec


def _riga(e: dict) -> bytes:
    return json.dumps(e, ensure_ascii=False, default=str, separators=(",", ":")).encode()


async def spedisci(d: SiemDestination, eventi: list[dict],
                   client: Optional[httpx.AsyncClient] = None) -> tuple[bool, Optional[int], str]:
    """(riuscita, codice HTTP, messaggio di Splunk o dell'errore). Non solleva."""
    if not d.token_enc:
        return False, None, "nessun token configurato"
    corpo = b"\n".join(_riga(e) for e in eventi)
    try:
        u, host, ip = await webhooks._risolvi(endpoint(d.url))
    except (webhooks._Vietato, webhooks._NonRisolto) as e:
        return False, None, str(e)[:500]
    destinazione = u.copy_with(host=ip)
    intest = {
        "Authorization": f"Splunk {decrypt_secret(d.token_enc)}", "Content-Type": "application/json",
        "X-Splunk-Request-Channel": CANALE, "Host": u.netloc.decode("ascii"),
    }
    estensioni = {"sni_hostname": host} if u.scheme == "https" else {}
    proprio = client is None
    if proprio:
        client = httpx.AsyncClient(
            verify=webhooks._contesto_tls() if d.verify_tls else False,
            timeout=get_settings().webhooks.timeout_seconds, follow_redirects=False, trust_env=False,
        )
    try:
        async with client.stream("POST", destinazione, content=corpo, headers=intest, extensions=estensioni) as r:
            letti = b""
            async for pezzo in r.aiter_bytes():
                letti += pezzo
                if len(letti) >= LETTI:
                    break
    except Exception as e:  # noqa: BLE001 — rete, TLS, timeout: è l'esito di questo tentativo
        return False, None, f"{type(e).__name__}: {e}"[:500]
    finally:
        if proprio:
            await client.aclose()
    testo = letti[:500].decode("utf-8", "replace")
    try:
        risposta = json.loads(letti)
    except ValueError:
        risposta = None
    if 200 <= r.status_code < 300 and (not isinstance(risposta, dict) or risposta.get("code", 0) == 0):
        return True, r.status_code, (risposta or {}).get("text", "Success") if isinstance(risposta, dict) else "Success"
    if isinstance(risposta, dict) and risposta.get("text"):
        testo = f"{risposta['text']} (code {risposta.get('code')})"
    return False, r.status_code, f"risposta {r.status_code}: {testo}"[:500]


def _lotto(session: Session, dopo: int) -> list[AuditLog]:
    return list(session.exec(
        select(AuditLog).where(AuditLog.id > dopo, AuditLog.ts <= adesso() - RITARDO).order_by(AuditLog.id).limit(LOTTO)
    ).all())


def _entro_il_tetto(eventi: list[dict]) -> int:
    """Quanti dei primi eventi stanno in una richiesta (almeno uno)."""
    totale = 0
    for i, e in enumerate(eventi):
        totale += len(_riga(e)) + 1
        if totale > MAX_BYTE and i:
            return i
    return len(eventi)


def attesa(fallimenti: int) -> float:
    """5, 10, 20, 40… secondi, fino a ATTESA_MAX."""
    return min(ATTESA_MAX, 5 * 2 ** max(0, fallimenti - 1))


async def un_giro(bind: Engine, client: Optional[httpx.AsyncClient] = None) -> int:
    """Spedisce quello che c'è da spedire (fino a MAX_LOTTI_PER_GIRO lotti). Torna
    quante voci sono partite. Un lotto alla volta col lucchetto: un altro processo
    che lo trova preso salta il giro."""
    from app.services.regolazioni_motore import blocca

    partite = 0
    for _ in range(MAX_LOTTI_PER_GIRO):
        with Session(bind) as session:
            if not await blocca(session, attesa=0, chiave=LUCCHETTO):
                return partite
            d = session.get(SiemDestination, 1)
            if d is None or not d.enabled or not d.url or not d.token_enc:
                return partite
            if d.next_attempt_at and d.next_attempt_at > adesso():
                return partite
            voci = _lotto(session, d.cursor_id)
            if not voci:
                return partite
            eventi = [evento(v, d) for v in voci]
            n = _entro_il_tetto(eventi)
            ok, codice, messaggio = await spedisci(d, eventi[:n], client)
            ora = adesso()
            if ok:
                d.cursor_id, d.sent_total = voci[n - 1].id, (d.sent_total or 0) + n
                d.last_ok_at, d.failures, d.next_attempt_at = ora, 0, None
                partite += n
            else:
                d.failures = (d.failures or 0) + 1
                d.last_error, d.last_error_at = messaggio, ora
                d.next_attempt_at = ora + timedelta(seconds=attesa(d.failures))
                logger.warning("siem: %s voci non consegnate (%s), si riprova fra %ss", n, messaggio, attesa(d.failures))
            session.add(d)
            session.commit()  # col cursore si rilascia anche il lucchetto
            if not ok:
                return partite
    return partite


async def inoltro_loop(stop: asyncio.Event) -> None:
    from app.db.session import engine

    logger.info("siem: inoltro dell'audit avviato (ogni %ss)", CICLO)
    while not stop.is_set():
        try:
            await un_giro(engine)
        except Exception:  # noqa: BLE001 — l'inoltro non deve mai fermare il gateway
            logger.exception("siem: giro di inoltro non riuscito")
        try:
            await asyncio.wait_for(stop.wait(), timeout=CICLO)
        except asyncio.TimeoutError:
            pass


def arretrato(session: Session, d: SiemDestination) -> tuple[int, Optional[datetime]]:
    """Quante voci aspettano di partire, e da quando la più vecchia."""
    n, piu_vecchia = session.exec(
        select(func.count(), func.min(AuditLog.ts)).where(AuditLog.id > d.cursor_id)
    ).one()
    return int(n or 0), piu_vecchia


def ultimo_id(session: Session) -> int:
    return int(session.exec(select(func.max(AuditLog.id))).one() or 0)
