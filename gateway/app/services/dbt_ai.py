"""L'AI nell'export dbt — opzionale, scelta nel dialogo di download.

1. Descrizioni DOVE MANCANO: un paragrafo per flusso («cosa fa»), le colonne dei
   modelli di uscita, delle tabelle sorgente e dei seed. In inglese, come il resto
   del progetto; marcate «(AI)».
2. Proposta di TRADUZIONE dove sqlglot non ce la fa (vedi `traduci`): la verifica
   la fa l'engine, non l'AI.

Scelte dell'utente (2026-10-10): quello che l'AI scrive si RICORDA per versione del
flusso (tabella dbt_ai_texts), così riesportare la stessa versione dà lo stesso zip;
l'AI vede la struttura del flusso e POCHI valori per colonna (al più 3, corti),
letti dalla datasource. La spesa passa dalla contabilità dell'assistente (ai_spend)
e ne rispetta i tetti; un tetto raggiunto o un provider che non risponde fermano
l'export con il motivo, invece di togliere l'AI di nascosto.

What keeps that promise: every key asked is kept, «no description» included, so a
version is asked once; one export at a time asks for a version, on any gateway (a
claim row), and what is stored wins over what an export just got; the README names
the models that wrote the stored texts. Every AI call of one export shares one budget
(AI__DBT_EXPORT_TIMEOUT_SECONDS), under the ingress' timeout, and has the assistant's
per-turn limits; what a call cost reaches ai_spend even when it fails."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models import Datasource, DbtAiText, Flow, FlowVersion, User
from app.services import ai_chats, ai_pricing
from app.services.ai_models import enabled_model_ids
from app.services.dbt_export import DbtExportError

logger = logging.getLogger(__name__)

MARCA = "(AI) "
MAX_TESTO = 400            # caratteri di una descrizione
MAX_COLONNE = 400          # colonne descritte in un export (oltre: restano senza)
CAMPIONI = 3               # valori distinti per colonna mostrati all'AI
LUNGHEZZA_CAMPIONE = 40
RIGHE_CAMPIONE = 30        # righe lette dalla datasource per scegliere i valori
MAX_CHIAVE = 500           # characters of dbt_ai_texts.key: a longer key is stored as its digest
# characters of SQL the AI is asked to translate. Its answer is as long, and a query
# sent cut short loses its last steps while keeping the right columns, which is all
# the engine's checks compare: over this, no AI.
MAX_SQL_TRADUZIONE = 20_000
ATTESA = 0.5               # seconds between two looks at a claim another export holds


def modello_ai(session: Session) -> Optional[str]:
    """Il modello da usare: quello di default se l'amministratore l'ha abilitato,
    altrimenti il primo abilitato. None = l'AI non è disponibile."""
    cfg = get_settings().ai
    if not cfg.enabled:
        return None
    abilitati = enabled_model_ids(session)
    if not abilitati:
        return None
    return cfg.default_model if cfg.default_model in abilitati else abilitati[0]


def nuova_scadenza() -> float:
    """When (time.monotonic) every AI call of ONE export must have answered. The export
    is one request, and the ingress cuts it at 300 s: the AI gives up first and says
    why, instead of a generic 504 while the gateway goes on paying."""
    return time.monotonic() + get_settings().ai.dbt_export_timeout_seconds


def _scaduta() -> DbtExportError:
    budget = get_settings().ai.dbt_export_timeout_seconds
    return DbtExportError(f"the AI did not answer within {budget:g} s", "ai_failed", detail=f"timed out after {budget:g} s")


def controlla_scadenza(scadenza: float) -> None:
    """Past the export's budget no further step starts: the engine calls between the AI's
    proposals (seeds read again, the ClickHouse syntax check) are not bounded by it."""
    if time.monotonic() > scadenza:
        raise _scaduta()


def _controlla_tetti(session: Session, user: User) -> None:
    cfg = get_settings().ai
    if cfg.max_cost_per_day_total_usd and ai_chats.speso_oggi_tutti(session) >= Decimal(str(cfg.max_cost_per_day_total_usd)):
        raise DbtExportError("the AI has reached the installation's spending cap for today", "ai_cap_reached")
    if cfg.max_cost_per_day_usd and ai_chats.speso_oggi(session, user) >= Decimal(str(cfg.max_cost_per_day_usd)):
        raise DbtExportError("you have reached your AI spending cap for today", "ai_cap_reached")


# What a YAML file cannot carry, even quoted: C0 controls, DEL, C1 controls, surrogates,
# U+FFFE/FFFF (whitespace aside, which `_pulisci` collapses). The AI echoes sample values,
# and text decoded with the wrong code page is full of C1 controls: one of them in
# schema.yml and `dbt parse` refuses the whole project.
_NON_YAML = re.compile(r"[^\s\x20-\x7e\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _pulisci(testo: Any) -> str:
    """One line, without Jinja's delimiters (dbt renders descriptions as Jinja) and
    without the characters YAML refuses, cut at MAX_TESTO."""
    t = re.sub(r"\s+", " ", _NON_YAML.sub("", str(testo or ""))).strip().strip("\"'`")
    for a, b in (("{{", "{ {"), ("}}", "} }"), ("{%", "{ %"), ("%}", "% }"), ("{#", "{ #"), ("#}", "# }")):
        t = t.replace(a, b)
    if len(t) > MAX_TESTO:
        t = t[: MAX_TESTO - 1].rstrip() + "…"
    return t


def _versione(session: Session, flow: Flow) -> int:
    ultima = session.exec(select(FlowVersion.version).where(FlowVersion.flow_id == flow.id)
                          .order_by(FlowVersion.version.desc()).limit(1)).first()
    return int(ultima or 0)


def _chiave(chiave: str) -> str:
    """The key as stored. A survey question as a column name, or the type of a nested
    Struct, makes a key longer than its column: such a key is stored as its digest.
    Reads and writes both go through here; a key cut short was never found again, and
    its second insert broke the unique key."""
    if len(chiave) <= MAX_CHIAVE:
        return chiave
    return "sha256:" + hashlib.sha256(chiave.encode()).hexdigest()


def _ricordati(session: Session, flow_id: int, versione: int, kind: str) -> dict[str, tuple[str, str]]:
    """What is stored for this flow version, by stored key (`_chiave`): (text, model)."""
    righe = session.exec(select(DbtAiText.key, DbtAiText.text, DbtAiText.model_id).where(
        DbtAiText.flow_id == flow_id, DbtAiText.flow_version == versione, DbtAiText.kind == kind)).all()
    return {k: (t, m) for k, t, m in righe}


def testi_della_versione(session: Session, flow: Flow) -> dict:
    """What the AI wrote that is kept for the flow's current version, for the download
    dialog: a re-export with the AI options gives it back without a call, with the AI
    switched off too, so the dialog can offer it then and name the models that wrote it
    (not the one configured now)."""
    righe = session.exec(select(DbtAiText.kind, DbtAiText.model_id).where(
        DbtAiText.flow_id == flow.id, DbtAiText.flow_version == _versione(session, flow),
        DbtAiText.kind.in_(("describe", "translate")), DbtAiText.text != "")).all()
    return {"descriptions": any(k == "describe" for k, _ in righe), "translations": any(k == "translate" for k, _ in righe),
            "models": sorted({m for _, m in righe if m})}


def _inserisci(session: Session, righe: list[dict]) -> int:
    """INSERT … ON CONFLICT DO NOTHING (Postgres, and SQLite in the tests): a row that
    another export stored first stays as it is. Returns the rows inserted."""
    if not righe:
        return 0
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    adesso = datetime.now(timezone.utc)
    esito = session.exec(insert(DbtAiText).values([{"model_id": "", "created_at": adesso, **r} for r in righe])
                         .on_conflict_do_nothing())
    return esito.rowcount or 0


def _ricorda(session: Session, flow_id: int, versione: int, kind: str, testi: dict[str, str], model_id: str) -> None:
    """Stores the texts. A key already stored keeps its text: another export, on this
    gateway or another one, stored it first, and that is the text every later export
    gives back."""
    _inserisci(session, [{"flow_id": flow_id, "flow_version": versione, "kind": kind, "key": _chiave(chiave),
                          "text": testo, "model_id": model_id} for chiave, testo in testi.items()])
    session.commit()


@asynccontextmanager
async def _prenotazione(session: Session, flow_id: int, versione: int, chiave: str, scadenza: float) -> AsyncIterator[None]:
    """One export at a time asks the AI for the same texts of a flow version, on any
    gateway: the claim is a row of dbt_ai_texts (kind «claim») that the unique key lets
    one export insert. The others wait for it to go, and then find the texts stored, or
    for it to expire: an export that died does not hold the version past its deadline."""
    dove = (DbtAiText.flow_id == flow_id, DbtAiText.flow_version == versione,
            DbtAiText.kind == "claim", DbtAiText.key == _chiave(chiave))
    while True:
        resto = scadenza - time.monotonic()
        if resto <= 0:
            raise _scaduta()
        mia = f"{time.time() + resto:.3f}"   # when this claim expires, wall clock: other gateways read it
        if _inserisci(session, [{"flow_id": flow_id, "flow_version": versione, "kind": "claim", "key": _chiave(chiave), "text": mia}]):
            session.commit()
            break
        tenuta = session.exec(select(DbtAiText.text).where(*dove)).first()
        session.commit()   # no transaction left open while waiting (SQLite would keep its lock)
        if tenuta is None:
            continue       # released in the meantime
        try:
            scaduta = float(tenuta) < time.time()
        except ValueError:
            scaduta = True
        if scaduta:
            session.exec(delete(DbtAiText).where(*dove, DbtAiText.text == tenuta))
            session.commit()
            continue
        await asyncio.sleep(min(ATTESA, resto))
    try:
        yield
    finally:
        try:
            session.exec(delete(DbtAiText).where(*dove, DbtAiText.text == mia))
            session.commit()
        except Exception:  # noqa: BLE001 — the claim expires on its own at its deadline
            session.rollback()
            logger.warning("dbt export: AI claim on flow %s v%s not released", flow_id, versione, exc_info=True)


def _registra_spesa(session: Session, user: User, model_id: str, uso: Any) -> None:
    try:
        ai_chats.registra_spesa(session, user_id=user.id, model_id=model_id, input_tokens=uso.input_tokens,
                                output_tokens=uso.output_tokens, cost=ai_pricing.cost_of(uso, model_id))
    except Exception:  # noqa: BLE001 — la contabilità non deve far perdere un export già pagato
        logger.exception("export dbt: spesa AI non registrata")


async def _esegui(model_id: str, output_type: type, istruzioni: str, domanda: str,
                  session: Session, user: User, scadenza: float) -> Any:
    """One AI run under the export's limits: at most 3 requests, the assistant's
    per-turn caps on cost and input tokens (audit A8), the export's deadline. What the
    run cost goes to the spending ledger even when it fails halfway (an answer that
    fails validation is retried, and billed): the daily caps read only the ledger."""
    from pydantic_ai import Agent
    from pydantic_ai.usage import RunUsage, UsageLimits

    from app.services import ai_agent

    cfg = get_settings().ai
    uso = RunUsage()   # ours, passed in: pydantic-ai counts into it, so a run stopped halfway still says what it spent
    try:
        resto = scadenza - time.monotonic()
        if resto <= 0:
            raise TimeoutError
        agente = Agent(ai_agent.build_model(model_id), output_type=output_type, instructions=istruzioni)
        limiti = UsageLimits(request_limit=3,
                             cost_limit=Decimal(str(cfg.max_cost_per_turn_usd)) if cfg.max_cost_per_turn_usd else None,
                             input_tokens_limit=cfg.max_input_tokens_per_turn)
        esito = await asyncio.wait_for(agente.run(domanda, usage=uso, usage_limits=limiti), timeout=resto)
    except TimeoutError as e:
        logger.warning("dbt export: the AI (%s) did not answer within the export's budget", model_id)
        raise _scaduta() from e
    except Exception as e:  # noqa: BLE001 — il provider non risponde, o risponde male
        logger.warning("dbt export: AI call failed (%s): %s", model_id, e)
        raise DbtExportError(f"the AI did not answer ({str(e)[:200]})", "ai_failed", detail=str(e)[:200]) from e
    finally:
        if uso.requests:
            _registra_spesa(session, user, model_id, uso)
    return esito.output


async def _campioni(bucket: str, key: str) -> dict[str, list[str]]:
    """Al più CAMPIONI valori distinti, non nulli e corti, per colonna."""
    body = {"bucket": bucket, "input_key": key, "operations": [], "limit": RIGHE_CAMPIONE, "no_cache": True, "principal": "ai"}
    try:
        resp = await get_engine_client().post("/tasks/preview", json=body, timeout=120)
        if resp.status_code >= 400:
            return {}
        righe = resp.json().get("rows") or []
    except Exception:  # noqa: BLE001 — senza valori la descrizione si fa lo stesso
        logger.info("export dbt: valori d'esempio non letti da %s/%s", bucket, key, exc_info=True)
        return {}
    out: dict[str, list[str]] = {}
    for r in righe:
        for col, v in (r or {}).items():
            if v is None or v == "":
                continue
            testo = str(v)[:LUNGHEZZA_CAMPIONE]
            valori = out.setdefault(col, [])
            if testo not in valori and len(valori) < CAMPIONI:
                valori.append(testo)
    return out


class _Voce(BaseModel):
    key: str
    description: str


class _Descrizioni(BaseModel):
    items: list[_Voce]


_ISTRUZIONI = (
    "You document a dbt project exported from Tabularia, a visual data-preparation tool. "
    "You get the flows (their steps, sources and outputs) and the columns that have no description, "
    "each with its type and a few sample values. For every item write a description in English: "
    "for a flow, two or three sentences on what it produces and from what; for a column, one sentence "
    "on what it holds. Use only what you are given; when the meaning is not clear, say plainly what the "
    "column contains instead of guessing a business meaning. No markdown, no quotes, no preamble. "
    "Return every item with its key unchanged."
)


def _passi(flow: Flow) -> dict[str, Any]:
    """Il flusso come lo vede l'AI: nomi delle sorgenti e delle uscite, i passi in ordine."""
    try:
        definizione = json.loads(flow.definition or "{}")
    except ValueError:
        definizione = {}
    passi, sorgenti, uscite = [], [], []
    for n in definizione.get("nodes") or []:
        data = n.get("data") or {}
        if n.get("type") == "operation":
            params = json.dumps(data.get("params") or {}, ensure_ascii=False)
            passi.append({"step": data.get("label") or data.get("opType"), "type": data.get("opType"), "params": params[:300]})
        elif n.get("type") == "source":
            sorgenti.append(data.get("label") or data.get("filename") or f"datasource {data.get('datasourceId')}")
        elif n.get("type") == "output":
            uscite.append(data.get("name") or data.get("table") or data.get("key") or data.get("destType"))
    return {"steps": passi, "sources": sorgenti, "outputs": uscite}


def _voci_colonne(payload: dict) -> list[tuple[str, dict, dict]]:
    """Le colonne senza descrizione: (chiave, voce per l'AI, colonna del payload da riempire).
    La chiave riconosce la colonna per datasource, nome e tipo — non per il nome del
    modello, che cambia col prefisso del dialogo."""
    out: list[tuple[str, dict, dict]] = []

    def aggiungi(dove: str, ds_id: Optional[int], colonna: dict, origine: str):
        if colonna.get("description") or not colonna.get("name"):
            return
        tipo = colonna.get("dtype") or ""
        base = f"ds:{ds_id}" if ds_id else f"{origine}"
        chiave = f"col:{base}:{colonna['name']}:{tipo}"
        out.append((chiave, {"key": chiave, "kind": "column", "in": dove, "column": colonna["name"], "type": tipo}, colonna))

    for m in payload.get("models") or []:
        ds_id = m.get("datasource_id") or (m.get("meta") or {}).get("datasource_id")
        for c in m.get("columns") or []:
            aggiungi(f"model {m['name']}", ds_id, c, f"model:{m['name']}")
    for s in payload.get("sources") or []:
        for t in s.get("tables") or []:
            for c in t.get("columns") or []:
                aggiungi(f"source table {t['name']}", t.get("datasource_id"), c, f"table:{t['name']}")
    for sd in payload.get("seeds") or []:
        for c in sd.get("columns") or []:
            aggiungi(f"seed {sd['name']}", sd.get("datasource_id"), c, f"seed:{sd.get('key')}")
    return out


async def descrivi(session: Session, user: User, flow: Flow, flussi: list[int], payload: dict,
                   scadenza: float | None = None) -> None:
    """Riempie le descrizioni che mancano nel payload (modifica il payload).

    Each text is asked once per flow version and kept, «no description» included: a
    re-export of the version asks nothing (the AI may be off by then) and gives the
    same zip, whatever model is configured now. `scadenza` is the export's budget
    (`nuova_scadenza`), shared with the translations."""
    scadenza = scadenza if scadenza is not None else nuova_scadenza()
    versione = _versione(session, flow)

    voci_flussi: list[tuple[str, dict, Flow]] = []
    for fid in sorted(set(flussi)):
        f = session.get(Flow, fid)
        if f is None:
            continue
        chiave = f"flow:{fid}@v{_versione(session, f)}"
        voci_flussi.append((chiave, {"key": chiave, "kind": "flow", "name": f.name, "description": f.description or "", **_passi(f)}, f))
    voci_colonne = _voci_colonne(payload)[:MAX_COLONNE]
    voci = [(k, v) for k, v, _ in voci_flussi] + [(k, v) for k, v, _ in voci_colonne]

    ricordati = _ricordati(session, flow.id, versione, "describe")
    if any(_chiave(k) not in ricordati for k, _ in voci):
        async with _prenotazione(session, flow.id, versione, "describe", scadenza):
            ricordati = _ricordati(session, flow.id, versione, "describe")   # what the export that held the claim stored
            mancanti = list({k: v for k, v in voci if _chiave(k) not in ricordati}.values())
            if mancanti:
                await _descrivi_mancanti(session, user, flow, versione, mancanti, scadenza)
                ricordati = _ricordati(session, flow.id, versione, "describe")   # what is stored wins

    modelli: set[str] = set()   # the models that wrote the texts used, not the one configured now
    for chiave, _, colonna in voci_colonne:
        testo, modello = ricordati.get(_chiave(chiave), ("", ""))
        if testo:
            colonna["description"] = MARCA + testo
            modelli.add(modello)
    riassunti = []
    for chiave, voce, f in voci_flussi:
        testo, modello = ricordati.get(_chiave(chiave), ("", ""))
        if not testo:
            continue
        modelli.add(modello)
        riassunti.append({"flow": f.name, "text": MARCA + testo})
        for m in payload.get("models") or []:
            if (m.get("meta") or {}).get("flow_id") == f.id and m.get("kind") == "output":
                m["description"] = (m.get("description") or "").rstrip() + " " + MARCA + testo
    payload["ai"] = {"model": ", ".join(sorted(m for m in modelli if m)) or None, "summaries": riassunti}


async def _descrivi_mancanti(session: Session, user: User, flow: Flow, versione: int, mancanti: list[dict], scadenza: float) -> None:
    """Asks the AI for the missing texts and stores an outcome for every key asked."""
    model_id = modello_ai(session)
    if model_id is None:
        raise DbtExportError("the AI is not available: the assistant is not configured, or no model is enabled", "ai_unavailable")
    _controlla_tetti(session, user)
    # i pochi valori d'esempio, per le colonne da descrivere (una lettura per datasource)
    campioni: dict[int, dict[str, list[str]]] = {}
    for v in mancanti:
        m = re.match(r"col:ds:(\d+):", v["key"])
        if not m:
            continue
        ds_id = int(m.group(1))
        if ds_id not in campioni:
            ds = session.get(Datasource, ds_id)
            campioni[ds_id] = await _campioni_entro(ds, scadenza) if ds is not None and ds.key else {}
        v["samples"] = campioni[ds_id].get(v["column"], [])
    testi = await _chiedi(model_id, mancanti, session, user, scadenza)
    restano = [v for v in mancanti if v["key"] not in testi]
    if restano:
        # models drop items from long lists, or answer some with nothing: those are asked
        # once more, alone. The first answer is stored first: it is paid for, whatever
        # the second call does.
        _ricorda(session, flow.id, versione, "describe", testi, model_id)
        testi.update(await _chiedi(model_id, restano, session, user, scadenza))
    # every key asked gets an outcome, «no description» for what is still missing: a
    # version is asked once, so its re-export gives the same zip without the provider
    _ricorda(session, flow.id, versione, "describe", {v["key"]: testi.get(v["key"], "") for v in mancanti}, model_id)


async def _campioni_entro(ds: Datasource, scadenza: float) -> dict[str, list[str]]:
    """The sample values, within the export's budget."""
    try:
        return await asyncio.wait_for(_campioni(ds.bucket, ds.key), timeout=max(scadenza - time.monotonic(), 0))
    except TimeoutError as e:
        raise _scaduta() from e


async def _chiedi(model_id: str, voci: list[dict], session: Session, user: User, scadenza: float) -> dict[str, str]:
    """The descriptions the AI writes for `voci`, by key: an item its answer leaves out,
    or answers with nothing, is not there."""
    esito = await _esegui(model_id, _Descrizioni, _ISTRUZIONI, json.dumps({"items": voci}, ensure_ascii=False),
                          session, user, scadenza)
    attese = {v["key"] for v in voci}
    return {v.key: testo for v in (esito.items or []) if v.key in attese and (testo := _pulisci(v.description))}


def chiave_traduzione(sql: str, da: str, a: str) -> str:
    return "sql:" + hashlib.sha256(f"{da}>{a}\n{sql}".encode()).hexdigest()[:32]


# gli errori dell'engine per cui l'AI può proporre una traduzione
TRADUCIBILI = ("translation_failed", "query_not_translatable", "query_not_single_select")
MAX_TRADUZIONI = 5   # proposte in un export: oltre, il flusso è da rivedere a mano
TENTATIVI = 2        # per modello: la seconda volta l'AI vede perché la prima è stata scartata


class _Traduzione(BaseModel):
    sql: str


_ISTRUZIONI_TRADUZIONE = (
    "You translate SQL between dialects for a dbt project. Translate the query you get from {da} to {a}. "
    "Every construct must exist in {a}: rewrite the ones that do not (for example, in ClickHouse SIMILAR TO "
    "becomes match() with an anchored regular expression, and there is no LATERAL nor TABLESAMPLE). "
    "Keep exactly these output columns, with these names, in this order: {colonne}. Keep every table reference "
    "exactly as written — placeholders such as __s_0 or __m_1, and schema.table names: translate only syntax "
    "and functions. Return one SELECT statement, with no comments and no trailing semicolon, in the field sql."
)


async def traduci(session: Session, user: User, flow: Flow, params: dict,
                  scartata: tuple[str, str] | None = None, scadenza: float | None = None) -> tuple[str, str, str]:
    """Una proposta di traduzione per il modello che l'engine non ha tradotto, ricordata
    per versione. Torna (sql, chiave, modello): la chiave serve a dimenticarla se l'engine
    la scarta, il modello è quello che l'ha scritta (non quello configurato ora).
    `scartata` = (proposta di prima, perché l'engine l'ha scartata): un secondo tentativo,
    con l'errore davanti — un modello a volte rimanda la query com'era.

    A query over MAX_SQL_TRADUZIONE characters is refused before any call: the AI would
    get it cut. Over ClickHouse's max_query_size (the engine's refusal of a model too
    large) it always is."""
    sql, da, a = str(params.get("sql") or ""), str(params.get("source_dialect") or ""), str(params.get("target_dialect") or "")
    colonne = [str(c) for c in params.get("columns") or []]
    if len(sql) > MAX_SQL_TRADUZIONE:
        modello = str(params.get("model") or "")
        raise DbtExportError(
            f"the model «{modello}» does not translate to {a} ({params.get('detail') or '-'}), and its SQL is too long "
            f"to ask the AI for a translation: {len(sql):,} characters, at most {MAX_SQL_TRADUZIONE:,}",
            "ai_query_too_long", model=modello, dialect=a, detail=str(params.get("detail") or "-"),
            size=f"{len(sql):,}", max=f"{MAX_SQL_TRADUZIONE:,}")
    scadenza = scadenza if scadenza is not None else nuova_scadenza()
    versione = _versione(session, flow)
    chiave = chiave_traduzione(sql, da, a)
    gia = _ricordati(session, flow.id, versione, "translate").get(_chiave(chiave))
    if gia and gia[0] and scartata is None:
        return gia[0], chiave, gia[1]
    async with _prenotazione(session, flow.id, versione, f"translate:{chiave}", scadenza):
        gia = _ricordati(session, flow.id, versione, "translate").get(_chiave(chiave))
        if gia and gia[0] and scartata is None:
            return gia[0], chiave, gia[1]       # another export asked for it while this one waited
        if gia and not gia[0]:
            dimentica(session, flow, chiave, "")   # an empty proposal kept by an older gateway: the new one takes its place
        model_id = modello_ai(session)
        if model_id is None:
            raise DbtExportError("the AI is not available: the assistant is not configured, or no model is enabled", "ai_unavailable")
        _controlla_tetti(session, user)
        domanda = sql
        if scartata is not None:
            domanda += (f"\n\n-- Your previous translation was rejected: {scartata[1]}\n-- Previous translation:\n"
                        + "\n".join("-- " + r for r in scartata[0][:5000].splitlines()))
        istruzioni = _ISTRUZIONI_TRADUZIONE.format(da=da, a=a, colonne=", ".join(colonne) or "(the same as the query)")
        esito = await _esegui(model_id, _Traduzione, istruzioni, domanda, session, user, scadenza)
        proposta = str(esito.sql or "").strip().rstrip(";").strip()
        if proposta:   # an empty one is rejected by the engine: kept, it would be served again, and paid again
            _ricorda(session, flow.id, versione, "translate", {chiave: proposta}, model_id)
        # what is stored wins: another gateway may have stored its proposal first
        tenuta = _ricordati(session, flow.id, versione, "translate").get(_chiave(chiave)) or (proposta, model_id)
    return tenuta[0], chiave, tenuta[1]


def dimentica(session: Session, flow: Flow, chiave: str, testo: str) -> None:
    """Una proposta che l'engine ha scartato: la prossima volta si richiede. Only that
    text: another export may have replaced it meanwhile with one of its own."""
    session.exec(delete(DbtAiText).where(DbtAiText.flow_id == flow.id, DbtAiText.kind == "translate",
                                         DbtAiText.key == _chiave(chiave), DbtAiText.text == testo))
    session.commit()
