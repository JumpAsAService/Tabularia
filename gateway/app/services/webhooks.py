"""Webhook: Tabularia chiama un indirizzo esterno quando un'esecuzione finisce.

Eventi (`EVENTI`): un flusso finito bene o male (sia dalla coda degli
orchestratori sia lanciato direttamente), il refresh di una datasource finito bene
o male, e `ping` per provare l'indirizzo.

Consegna:
- la consegna (`WebhookDelivery`) nasce nella STESSA transazione che chiude il run
  (`accoda_per_run`, chiamata da chi chiude): se il processo muore subito dopo, la
  consegna c'è già. Il corpo si costruisce leggendo con una connessione sua: un
  guasto qui si logga e basta, non impedisce mai di chiudere un run;
- la spedisce `consegna_loop`, negli orchestratori: riempie i posti liberi
  (`PARALLELE`) con le consegne dovute appena se ne libera uno (e comunque ogni
  `CICLO` secondi), senza aspettare quelle già in volo, e al più `PER_WEBHOOK` per
  lo stesso webhook: un ricevente che non risponde, con una coda lunga, non
  trattiene le consegne degli altri, e una coda lunga verso un ricevente veloce si
  svuota alla velocità del ricevente. Più
  processi la prendono con un UPDATE condizionato (una sola vince); se chi l'ha
  presa muore a metà, dopo `RIPRESA` torna in coda. Quindi ALMENO UNA consegna:
  il ricevente scarta i doppioni con l'id (`X-Tabularia-Delivery`, e `id` nel corpo);
- un errore o una risposta non 2xx ritenta dopo `ATTESE`, poi la si dà per fallita;
- ogni consegna è firmata: `X-Tabularia-Signature: t=<secondi>,v1=<hex>`, dove
  v1 = HMAC-SHA256(segreto, "<t>.<corpo>"). Il ricevente rifà il conto e controlla
  che t sia recente (docs/api.md ha l'esempio).

Indirizzi: http o https. Non si chiamano indirizzi di loopback, link-local
(compreso il 169.254.169.254 dei metadati dei cloud), multicast, riservati, né gli
altri indirizzi di metadati noti (`METADATI`). Il nome si risolve UNA volta, con la
codifica dei nomi internazionali che usa httpx, e la connessione va all'indirizzo
verificato (il nome resta nell'intestazione Host e nel TLS, dove il certificato si
controlla sul nome): un DNS che cambia risposta fra il controllo e la connessione
non porta altrove. Ogni consegna ha un client suo: un pool condiviso, che vede solo
l'indirizzo, riuserebbe la connessione TLS verificata per un nome per la richiesta
di un altro nome sullo stesso indirizzo (trovato provando dal vivo). Le CA sono
quelle di certifi, o `SSL_CERT_FILE`/`SSL_CERT_DIR` per un ricevente con un
certificato della CA interna. Niente proxy di sistema, niente rediretti. Le reti private sono ammesse, perché un'integrazione
interna (Airflow nella stessa rete) è il caso normale. I webhook li crea solo un
amministratore.

Spedizione: le letture e le scritture sul database stanno in un thread, fuori dal
ciclo degli eventi e senza transazioni aperte durante la chiamata HTTP; ogni
chiamata ha un tempo massimo complessivo, e della risposta si leggono al più
`LETTI` byte. Una consegna non fa mai cadere le altre: qualunque errore diventa
l'esito di quella consegna.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.models import Datasource, Flow, Project, Run, Webhook, WebhookDelivery

logger = logging.getLogger(__name__)

EVENTI = (
    "flow.run.succeeded",
    "flow.run.failed",
    "datasource.refresh.succeeded",
    "datasource.refresh.failed",
)
PING = "ping"
CICLO = 2.0  # secondi fra un giro e l'altro della spedizione
RIPRESA = timedelta(minutes=5)  # una consegna presa e mai chiusa torna in coda dopo
# attesa prima del tentativo n+1: sei tentativi in tutto, l'ultimo dopo ~7 ore e mezzo
ATTESE = (30, 120, 600, 3600, 21600)
MAX_TENTATIVI = len(ATTESE) + 1
LOTTO = 20
PARALLELE = 16  # chiamate in volo insieme, per processo
PER_WEBHOOK = 2  # …di cui al più tante verso lo stesso webhook
LETTI = 4096  # byte della risposta che si leggono (per il registro)
# indirizzi di metadati dei cloud fuori dal link-local, che le categorie di
# ipaddress non riconoscono: Alibaba, AWS in IPv6, Azure (wireserver)
METADATI = tuple(ipaddress.ip_network(n) for n in ("100.100.100.200/32", "fd00:ec2::254/128", "168.63.129.16/32"))


def adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(d: Optional[datetime]) -> Optional[str]:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat().replace("+00:00", "Z")


# ── l'abbonamento ───────────────────────────────────────────────────────────

def nuovo_segreto() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def cifra(segreto: str) -> str:
    return encrypt_secret(segreto)


def eventi_di(w: Webhook) -> list[str]:
    try:
        return [e for e in json.loads(w.events or "[]") if isinstance(e, str)]
    except ValueError:
        return []


def valida_url(url: str) -> str:
    """Forma dell'indirizzo, al salvataggio. Dove punta lo si controlla a ogni
    spedizione (`_risolvi`): un nome può cambiare indirizzo."""
    url = (url or "").strip()
    parti = urlsplit(url)
    if parti.scheme not in ("http", "https") or not parti.hostname:
        raise ValueError("L'indirizzo deve essere http:// o https:// con un host")
    if len(url) > 2000:
        raise ValueError("Indirizzo troppo lungo (al più 2000 caratteri)")
    try:
        _leggi_host(httpx.URL(url))
    except (httpx.InvalidURL, ValueError) as e:  # UnicodeError è un ValueError
        raise ValueError(f"Indirizzo non valido: {e}") from None
    return url


def _leggi_host(u: httpx.URL) -> str:
    """L'host come lo usa httpx per connettersi, controllato come lo controllerà il
    resolver (un'etichetta vuota o troppo lunga lo fa fallire), e la porta."""
    host = u.raw_host.decode("ascii")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        host.encode("idna")
    if u.port is not None and not 0 < u.port < 65536:
        raise ValueError(f"porta fuori intervallo: {u.port}")
    return host


def _ip_vietato(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:  # ::ffff:127.0.0.1 è 127.0.0.1
        ip = ip.ipv4_mapped
    if ip.version == 6 and ip.sixtofour is not None and _ip_vietato(ip.sixtofour):  # 2002:7f00:1:: (6to4)
        return True
    return (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
            or any(ip in rete for rete in METADATI))


class _Vietato(Exception):
    """L'indirizzo non si chiama: ritentare non cambierebbe niente."""


class _NonRisolto(Exception):
    """Il nome non si risolve adesso (un DNS che riparte): si ritenta."""


async def _risolvi(url: str) -> tuple[httpx.URL, str, str]:
    """(url, host come lo usa httpx, l'indirizzo verificato a cui collegarsi).
    Se uno qualunque degli indirizzi del nome è vietato, il nome è vietato: con
    più indirizzi non si sa quale sceglierebbe il resolver la volta dopo."""
    try:
        u = httpx.URL(url)
        host = _leggi_host(u)  # IDNA come lo codifica httpx per connettersi
    except (httpx.InvalidURL, ValueError) as e:  # salvato prima del controllo, o cambiato httpx
        raise _Vietato(f"indirizzo non valido: {e}") from None
    try:
        indirizzi = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            info = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                                type=socket.SOCK_STREAM)
        except UnicodeError as e:  # il nome non si codifica: non cambierà ritentando
            raise _Vietato(f"indirizzo non valido: {e}") from None
        except OSError as e:
            raise _NonRisolto(f"nome non risolto: {e}") from None
        indirizzi = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in info]
    if not indirizzi:
        raise _NonRisolto("nome senza indirizzi")
    for ip in indirizzi:
        if _ip_vietato(ip):
            raise _Vietato(f"indirizzo non ammesso per un webhook: {ip}")
    return u, host, str(indirizzi[0])


# ── gli eventi ──────────────────────────────────────────────────────────────

def evento_del_run(run: Run) -> Optional[str]:
    """Quale evento racconta la chiusura di questo run, se ne racconta uno. I run
    dei singoli output di un'orchestrazione no: lo racconta l'orchestrazione."""
    esito = {"SUCCESS": "succeeded", "FAILURE": "failed"}.get(run.status)
    if esito is None:
        return None
    if getattr(run, "flow_group_id", None) is not None:
        # a flow group's own run has no event of its own yet: the flows and the
        # refreshes inside it send theirs
        return None
    if run.kind == "orchestration" or (run.kind == "flow" and run.parent_run_id is None):
        return f"flow.run.{esito}"
    if run.kind == "ingest":
        return f"datasource.refresh.{esito}"
    return None


def _dati_del_run(lettura: Session, run: Run) -> tuple[dict, Optional[int]]:
    """Il contenuto dell'evento, e la cartella da cui nasce (per i filtri)."""
    durata = None
    if run.started_at and run.finished_at:
        senza_fuso = lambda d: d.replace(tzinfo=None) if d.tzinfo else d  # noqa: E731 — dal DB tornano naive UTC
        durata = round((senza_fuso(run.finished_at) - senza_fuso(run.started_at)).total_seconds(), 1)
    dati: dict = {"run": {
        "id": run.id, "kind": run.kind, "status": run.status, "trigger": run.trigger_type,
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "duration_seconds": durata, "rows_written": run.rows_written,
        "error": (run.error or None) and run.error[:1000],
    }}
    cartella = None
    if run.flow_id is not None and run.kind != "ingest":
        flow = lettura.get(Flow, run.flow_id)
        if flow is not None:
            dati["flow"] = {"id": flow.id, "name": flow.name}
            cartella = flow.project_id
    if run.kind == "ingest" and run.datasource_id is not None:
        ds = lettura.get(Datasource, run.datasource_id)
        if ds is not None:
            dati["datasource"] = {"id": ds.id, "name": ds.name}
            cartella = ds.project_id
    if cartella is not None:
        p = lettura.get(Project, cartella)
        dati["folder"] = {"id": cartella, "name": p.name if p else None}
    return dati, cartella


def _corpo(evento: str, dati: dict) -> str:
    return json.dumps(
        {"id": str(uuid.uuid4()), "event": evento, "created_at": _iso(adesso()), "data": dati},
        ensure_ascii=False, separators=(",", ":"),
    )


def _nella_cartella(lettura: Session, w: Webhook, cartella: Optional[int]) -> bool:
    if w.project_id is None:
        return True
    if cartella is None:
        return False
    from app.services.permissions import ancestor_ids, folder_parents

    return w.project_id == cartella or w.project_id in ancestor_ids(folder_parents(lettura), cartella)


def accoda_per_run(session: Session, run: Run) -> int:
    """Da chiamare PRIMA del commit che chiude il run, con `run` già riletto nella
    stessa transazione (stato e fine nuovi): le consegne entrano in quel commit.
    Non solleva: un webhook rotto non impedisce mai di chiudere un run."""
    try:
        evento = evento_del_run(run)
        if evento is None:
            return 0
        from app.db.session import engine_di_lettura

        bind = session.get_bind()
        lettura_engine = engine_di_lettura(bind) if isinstance(bind, Engine) else bind
        with Session(lettura_engine) as lettura:
            abbonati = [
                w for w in lettura.exec(select(Webhook).where(Webhook.active == True)).all()  # noqa: E712
                if evento in eventi_di(w)
            ]
            if not abbonati:
                return 0
            dati, cartella = _dati_del_run(lettura, run)
            scelti = [w.id for w in abbonati if _nella_cartella(lettura, w, cartella)]
        ora = adesso()
        for wid in scelti:
            session.add(WebhookDelivery(
                webhook_id=wid, event=evento, payload=_corpo(evento, dati), next_attempt_at=ora, created_at=ora,
            ))
        return len(scelti)
    except Exception:  # noqa: BLE001
        logger.exception("webhook: consegne del run %s non preparate", getattr(run, "id", "?"))
        return 0


def accoda_ping(session: Session, w: Webhook) -> WebhookDelivery:
    ora = adesso()
    d = WebhookDelivery(
        webhook_id=w.id, event=PING, next_attempt_at=ora, created_at=ora,
        payload=_corpo(PING, {"webhook": {"id": w.id, "name": w.name}, "message": "Tabularia webhook test"}),
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def rispedisci(session: Session, d: WebhookDelivery) -> WebhookDelivery:
    """Una consegna nuova con lo stesso contenuto e un id nuovo (chi riceve scarta
    i doppioni per id: una rispedizione voluta non deve sembrargli un doppione)."""
    corpo = json.loads(d.payload)
    corpo["id"] = str(uuid.uuid4())
    ora = adesso()
    nuova = WebhookDelivery(
        webhook_id=d.webhook_id, event=d.event, next_attempt_at=ora, created_at=ora,
        payload=json.dumps(corpo, ensure_ascii=False, separators=(",", ":")),
    )
    session.add(nuova)
    session.commit()
    session.refresh(nuova)
    return nuova


# ── la spedizione ───────────────────────────────────────────────────────────

def firma(segreto: str, istante: int, corpo: str) -> str:
    return hmac.new(segreto.encode(), f"{istante}.{corpo}".encode(), hashlib.sha256).hexdigest()


def intestazioni(segreto: str, evento: str, corpo: str, istante: Optional[int] = None) -> dict[str, str]:
    istante = istante or int(time.time())
    return {
        "Content-Type": "application/json",
        "User-Agent": f"Tabularia-Webhooks/{get_settings().app.version}",
        "X-Tabularia-Event": evento,
        "X-Tabularia-Delivery": json.loads(corpo)["id"],
        "X-Tabularia-Signature": f"t={istante},v1={firma(segreto, istante, corpo)}",
    }


@dataclass
class _Lavoro:
    """Una consegna presa, con tutto quel che serve a spedirla: dopo la presa non
    si tiene aperta nessuna transazione."""
    id: int
    webhook: int
    url: str
    segreto: str
    evento: str
    corpo: str
    tentativi: int
    presa: datetime


@functools.lru_cache(maxsize=1)
def _contesto_tls() -> ssl.SSLContext:
    """Uno per processo: caricare le CA costa, e il contesto si condivide fra le
    connessioni (il nome da verificare è di ciascuna). Legge SSL_CERT_FILE e
    SSL_CERT_DIR se ci sono: un ricevente interno con la CA dell'azienda."""
    return httpx.create_ssl_context(trust_env=True)


def _client() -> httpx.AsyncClient:
    """Un client per UNA consegna: ci si collega all'indirizzo, non al nome, e un
    pool condiviso riuserebbe per il nome b la connessione TLS verificata per il
    nome a sullo stesso indirizzo — saltando il controllo del certificato di b."""
    return httpx.AsyncClient(
        verify=_contesto_tls(), timeout=get_settings().webhooks.timeout_seconds, follow_redirects=False,
        trust_env=False,  # niente proxy di sistema: l'indirizzo verificato è quello che si chiama
    )


async def _spedisci(lavoro: _Lavoro, client: httpx.AsyncClient) -> tuple[bool, Optional[int], Optional[str]]:
    """(riuscita, codice HTTP, errore). Solleva `_Vietato` per un indirizzo non
    ammesso o non valido; per il resto non solleva."""
    try:
        u, host, ip = await _risolvi(lavoro.url)
    except _NonRisolto as e:
        return False, None, str(e)[:500]
    # all'indirizzo verificato: il nome non si risolve una seconda volta
    destinazione = u.copy_with(host=ip)
    intest = {**intestazioni(lavoro.segreto, lavoro.evento, lavoro.corpo), "Host": u.netloc.decode("ascii")}
    estensioni = {"sni_hostname": host} if u.scheme == "https" else {}
    try:
        async with client.stream("POST", destinazione, content=lavoro.corpo.encode(), headers=intest,
                                 extensions=estensioni) as r:
            letti = b""
            async for pezzo in r.aiter_bytes():
                letti += pezzo
                if len(letti) >= LETTI:
                    break
    except httpx.HTTPError as e:
        return False, None, f"{type(e).__name__}: {e}"[:500]
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"[:500]
    if 200 <= r.status_code < 300:
        return True, r.status_code, None
    return False, r.status_code, f"risposta {r.status_code}: {letti[:300].decode('utf-8', 'replace')}"


def _prendi_dovute(bind: Engine, limite: int, occupati: Optional[Counter] = None) -> list[_Lavoro]:
    """Prende fino a `limite` consegne dovute (nel thread: database fuori dal ciclo
    degli eventi), le più vecchie per prime ma al più `PER_WEBHOOK` per webhook,
    contando quelle già in volo in questo processo (`occupati`). Ognuna è di chi la
    prende per primo, e solo se è ancora dovuta."""
    occupati = Counter(occupati or {})
    lavori: list[_Lavoro] = []
    with Session(bind) as session:
        ora = adesso()
        # chi l'aveva presa è morto a metà: torna in coda
        session.exec(
            update(WebhookDelivery)
            .where(WebhookDelivery.status == "sending", WebhookDelivery.claimed_at < ora - RIPRESA)
            .values(status="pending")
        )
        session.commit()
        # le prime PER_WEBHOOK dovute di ogni webhook: la coda lunga di un ricevente
        # morto non nasconde le consegne degli altri dietro di sé
        numerate = (
            select(
                WebhookDelivery.id.label("id"), WebhookDelivery.webhook_id.label("webhook_id"),
                WebhookDelivery.next_attempt_at.label("quando"),
                func.row_number().over(
                    partition_by=WebhookDelivery.webhook_id,
                    order_by=(WebhookDelivery.next_attempt_at, WebhookDelivery.id),
                ).label("n"),
            )
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= ora)
            .subquery()
        )
        candidate = session.exec(
            select(numerate.c.id, numerate.c.webhook_id)
            .where(numerate.c.n <= PER_WEBHOOK)
            .order_by(numerate.c.quando, numerate.c.id)
            .limit(limite + PER_WEBHOOK * len(occupati))
        ).all()
        for did, wid in candidate:
            if len(lavori) >= limite:
                break
            if occupati[wid] >= PER_WEBHOOK:
                continue
            presa = adesso()
            preso = session.exec(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == did, WebhookDelivery.status == "pending",
                       WebhookDelivery.next_attempt_at <= presa)
                .values(status="sending", claimed_at=presa, attempts=WebhookDelivery.attempts + 1)
            )
            session.commit()
            if (preso.rowcount or 0) != 1:
                continue  # un altro processo l'ha presa, o è stata rimandata
            d = session.get(WebhookDelivery, did)
            w = session.get(Webhook, d.webhook_id)
            if w is None or not w.active:
                _chiudi_consegna(session, did, presa, status="failed", last_error="webhook spento o tolto")
                continue
            lavori.append(_Lavoro(did, w.id, w.url, decrypt_secret(w.secret_enc), d.event, d.payload, d.attempts, presa))
            occupati[wid] += 1
            session.commit()  # fine della lettura: nessuna transazione aperta durante la chiamata
    return lavori


def _chiudi_consegna(session: Session, did: int, presa: datetime, **valori) -> None:
    """Scrive l'esito solo se la consegna è ancora di chi l'ha presa: un processo
    in ritardo non sovrascrive quello di chi l'ha ripresa dopo."""
    session.exec(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == did, WebhookDelivery.status == "sending", WebhookDelivery.claimed_at == presa)
        .values(**valori)
    )
    session.commit()


def _registra(bind: Engine, lavoro: _Lavoro, ok: bool, codice: Optional[int], errore: Optional[str], definitivo: bool) -> None:
    valori: dict = {"last_status_code": codice, "last_error": errore}
    if ok:
        valori.update(status="delivered", delivered_at=adesso())
    elif definitivo or lavoro.tentativi >= MAX_TENTATIVI:
        valori.update(status="failed")
    else:
        valori.update(status="pending", next_attempt_at=adesso() + timedelta(seconds=ATTESE[lavoro.tentativi - 1]))
    with Session(bind) as session:
        _chiudi_consegna(session, lavoro.id, lavoro.presa, **valori)


async def _una(bind: Engine, lavoro: _Lavoro, client: Optional[httpx.AsyncClient] = None) -> None:
    """Spedisce una consegna e ne scrive l'esito. Non solleva mai: un errore
    imprevisto è l'esito di QUESTA consegna (ritentata, poi fallita), non la fine
    del giro per tutte le altre."""
    ok, codice, errore, definitivo = False, None, None, False
    c = client or _client()
    try:
        ok, codice, errore = await asyncio.wait_for(
            _spedisci(lavoro, c), timeout=get_settings().webhooks.timeout_seconds * 2,
        )
    except _Vietato as e:
        errore, definitivo = str(e), True
    except asyncio.TimeoutError:
        # il tempo massimo è per la chiamata intera: un ricevente che risponde a
        # gocce non tiene la consegna oltre RIPRESA (e non la fa spedire due volte)
        errore = "nessuna risposta completa in tempo"
    except asyncio.CancelledError:
        raise  # spegnimento: la consegna resta presa e torna in coda dopo RIPRESA
    except Exception as e:  # noqa: BLE001
        logger.exception("webhook: consegna %s non spedita", lavoro.id)
        errore = f"errore interno: {type(e).__name__}"
    finally:
        if client is None:
            await c.aclose()
    try:
        await asyncio.to_thread(_registra, bind, lavoro, ok, codice, errore, definitivo)
    except Exception:  # noqa: BLE001
        logger.exception("webhook: esito della consegna %s non registrato", lavoro.id)


class _InVolo:
    """Le consegne in volo di questo processo: ogni giro riempie solo i posti
    liberi, senza aspettare che finiscano quelle partite prima."""

    def __init__(self, bind: Engine, client: Optional[httpx.AsyncClient] = None):
        self.bind, self.client = bind, client
        self.compiti: dict[asyncio.Task, int] = {}  # compito → webhook

    async def giro(self, limite: int = PARALLELE) -> int:
        for t in [t for t in self.compiti if t.done()]:
            self.compiti.pop(t, None)
        liberi = min(limite, PARALLELE - len(self.compiti))
        if liberi <= 0:
            return 0
        try:
            lavori = await asyncio.to_thread(_prendi_dovute, self.bind, liberi, Counter(self.compiti.values()))
        except Exception:  # noqa: BLE001
            logger.exception("webhook: coda delle consegne non leggibile")
            return 0
        for x in lavori:
            t = asyncio.create_task(_una(self.bind, x, self.client))
            self.compiti[t] = x.webhook
            t.add_done_callback(lambda fatto: self.compiti.pop(fatto, None))
        return len(lavori)

    async def finisci(self, secondi: Optional[float] = None) -> None:
        """Aspetta quelle in volo (al più `secondi`), poi ferma le altre: restano
        prese e tornano in coda dopo RIPRESA."""
        if not self.compiti:
            return
        _, restanti = await asyncio.wait(list(self.compiti), timeout=secondi)
        for t in restanti:
            t.cancel()
        if restanti:
            await asyncio.gather(*restanti, return_exceptions=True)


async def consegna_dovute(bind: Engine, client: Optional[httpx.AsyncClient] = None, limite: int = LOTTO) -> int:
    """Un giro solo, aspettando che finisca (per i test e per chi vuole spedire
    adesso). Torna quante consegne ha provato. Non solleva."""
    volo = _InVolo(bind, client)
    n = 0
    while n < limite:
        presi = await volo.giro(limite - n)
        if not presi:
            break
        n += presi
        if n < limite:
            await volo.finisci()
    await volo.finisci()
    return n


async def consegna_loop(stop: asyncio.Event) -> None:
    from app.db.session import engine

    logger.info("webhook: spedizione avviata (ogni %ss)", CICLO)
    volo = _InVolo(engine)
    fermo = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set():
            try:
                await volo.giro()
            except Exception:  # noqa: BLE001 — la spedizione non deve mai fermare il gateway
                logger.exception("webhook: giro di spedizione non riuscito")
            # si riparte appena una consegna finisce (il suo posto è libero), o dopo
            # CICLO per quelle diventate dovute nel frattempo, o allo spegnimento.
            # Prima si aspettava sempre CICLO: con al più PER_WEBHOOK in volo per
            # webhook, 300 consegne verso un ricevente veloce andavano a una al secondo
            await asyncio.wait([fermo, *volo.compiti], timeout=CICLO, return_when=asyncio.FIRST_COMPLETED)
    finally:
        fermo.cancel()
        await volo.finisci(5)


def pulisci(session: Session) -> int:
    """Il registro delle consegne oltre WEBHOOKS__RETENTION_DAYS (lo chiama lo scheduler)."""
    taglio = adesso() - timedelta(days=get_settings().webhooks.retention_days)
    n = session.exec(
        delete(WebhookDelivery).where(WebhookDelivery.created_at < taglio, WebhookDelivery.status.in_(("delivered", "failed")))
    ).rowcount or 0
    session.commit()
    return n
