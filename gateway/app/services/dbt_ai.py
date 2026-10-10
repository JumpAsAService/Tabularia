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
l'export con il motivo, invece di togliere l'AI di nascosto."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel
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


def _controlla_tetti(session: Session, user: User) -> None:
    cfg = get_settings().ai
    if cfg.max_cost_per_day_total_usd and ai_chats.speso_oggi_tutti(session) >= Decimal(str(cfg.max_cost_per_day_total_usd)):
        raise DbtExportError("l'AI ha raggiunto il tetto di spesa di oggi dell'installazione", "ai_cap_reached")
    if cfg.max_cost_per_day_usd and ai_chats.speso_oggi(session, user) >= Decimal(str(cfg.max_cost_per_day_usd)):
        raise DbtExportError("hai raggiunto il tetto di spesa di oggi per l'AI", "ai_cap_reached")


def _pulisci(testo: Any) -> str:
    """Una riga, senza i delimitatori di Jinja (dbt rende le descrizioni come Jinja),
    tagliata a MAX_TESTO."""
    t = re.sub(r"\s+", " ", str(testo or "")).strip().strip("\"'`")
    for a, b in (("{{", "{ {"), ("}}", "} }"), ("{%", "{ %"), ("%}", "% }"), ("{#", "{ #"), ("#}", "# }")):
        t = t.replace(a, b)
    if len(t) > MAX_TESTO:
        t = t[: MAX_TESTO - 1].rstrip() + "…"
    return t


def _versione(session: Session, flow: Flow) -> int:
    ultima = session.exec(select(FlowVersion.version).where(FlowVersion.flow_id == flow.id)
                          .order_by(FlowVersion.version.desc()).limit(1)).first()
    return int(ultima or 0)


def _ricordati(session: Session, flow_id: int, versione: int, kind: str) -> dict[str, str]:
    righe = session.exec(select(DbtAiText).where(DbtAiText.flow_id == flow_id, DbtAiText.flow_version == versione,
                                                 DbtAiText.kind == kind)).all()
    return {r.key: r.text for r in righe}


def _ricorda(session: Session, flow_id: int, versione: int, kind: str, testi: dict[str, str], model_id: str) -> None:
    gia = set(_ricordati(session, flow_id, versione, kind))
    for chiave, testo in testi.items():
        if chiave not in gia:
            session.add(DbtAiText(flow_id=flow_id, flow_version=versione, kind=kind, key=chiave[:500], text=testo, model_id=model_id))
    session.commit()


def _registra_spesa(session: Session, user: User, model_id: str, esito: Any) -> None:
    uso = esito.usage
    uso = uso() if callable(uso) else uso
    try:
        ai_chats.registra_spesa(session, user_id=user.id, model_id=model_id, input_tokens=uso.input_tokens,
                                output_tokens=uso.output_tokens, cost=ai_pricing.cost_of(uso, model_id))
    except Exception:  # noqa: BLE001 — la contabilità non deve far perdere un export già pagato
        logger.exception("export dbt: spesa AI non registrata")


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


async def descrivi(session: Session, user: User, flow: Flow, flussi: list[int], payload: dict) -> None:
    """Riempie le descrizioni che mancano nel payload (modifica il payload)."""
    model_id = modello_ai(session)
    if model_id is None:
        raise DbtExportError("l'AI non è disponibile: assistente non configurato o nessun modello abilitato", "ai_unavailable")
    versione = _versione(session, flow)
    ricordati = _ricordati(session, flow.id, versione, "describe")

    voci_flussi: list[tuple[str, dict, Flow]] = []
    for fid in sorted(set(flussi)):
        f = session.get(Flow, fid)
        if f is None:
            continue
        chiave = f"flow:{fid}@v{_versione(session, f)}"
        voci_flussi.append((chiave, {"key": chiave, "kind": "flow", "name": f.name, "description": f.description or "", **_passi(f)}, f))
    voci_colonne = _voci_colonne(payload)[:MAX_COLONNE]

    mancanti = [v for k, v, _ in voci_flussi if k not in ricordati] + [v for k, v, _ in voci_colonne if k not in ricordati]
    if mancanti:
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
                campioni[ds_id] = await _campioni(ds.bucket, ds.key) if ds is not None and ds.key else {}
            v["samples"] = campioni[ds_id].get(v["column"], [])
        testi = await _chiedi(model_id, mancanti, session, user)
        _ricorda(session, flow.id, versione, "describe", testi, model_id)
        ricordati = {**ricordati, **testi}

    for chiave, _, colonna in voci_colonne:
        if ricordati.get(chiave):
            colonna["description"] = MARCA + ricordati[chiave]
    riassunti = []
    for chiave, voce, f in voci_flussi:
        testo = ricordati.get(chiave)
        if not testo:
            continue
        riassunti.append({"flow": f.name, "text": MARCA + testo})
        for m in payload.get("models") or []:
            if (m.get("meta") or {}).get("flow_id") == f.id and m.get("kind") == "output":
                m["description"] = (m.get("description") or "").rstrip() + " " + MARCA + testo
    payload["ai"] = {"model": model_id, "summaries": riassunti}


async def _chiedi(model_id: str, voci: list[dict], session: Session, user: User) -> dict[str, str]:
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    from app.services import ai_agent

    try:
        agente = Agent(ai_agent.build_model(model_id), output_type=_Descrizioni, instructions=_ISTRUZIONI)
        esito = await agente.run(json.dumps({"items": voci}, ensure_ascii=False), usage_limits=UsageLimits(request_limit=3))
    except Exception as e:  # noqa: BLE001 — il provider non risponde, o risponde male
        logger.warning("export dbt: descrizioni AI non riuscite (%s): %s", model_id, e)
        raise DbtExportError(f"l'AI non ha risposto ({str(e)[:200]})", "ai_failed", detail=str(e)[:200]) from e
    _registra_spesa(session, user, model_id, esito)
    attese = {v["key"] for v in voci}
    return {v.key: _pulisci(v.description) for v in (esito.output.items or []) if v.key in attese and _pulisci(v.description)}


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
                  scartata: tuple[str, str] | None = None) -> tuple[str, str]:
    """Una proposta di traduzione per il modello che l'engine non ha tradotto, ricordata
    per versione. Torna (sql, chiave): la chiave serve a dimenticarla se l'engine la scarta.
    `scartata` = (proposta di prima, perché l'engine l'ha scartata): un secondo tentativo,
    con l'errore davanti — un modello a volte rimanda la query com'era."""
    model_id = modello_ai(session)
    if model_id is None:
        raise DbtExportError("l'AI non è disponibile: assistente non configurato o nessun modello abilitato", "ai_unavailable")
    sql, da, a = str(params.get("sql") or ""), str(params.get("source_dialect") or ""), str(params.get("target_dialect") or "")
    colonne = [str(c) for c in params.get("columns") or []]
    versione = _versione(session, flow)
    chiave = chiave_traduzione(sql, da, a)
    gia = _ricordati(session, flow.id, versione, "translate").get(chiave)
    if gia and scartata is None:
        return gia, chiave
    _controlla_tetti(session, user)
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    from app.services import ai_agent

    try:
        agente = Agent(ai_agent.build_model(model_id), output_type=_Traduzione,
                       instructions=_ISTRUZIONI_TRADUZIONE.format(da=da, a=a, colonne=", ".join(colonne) or "(the same as the query)"))
        domanda = sql[:20000]
        if scartata is not None:
            domanda += (f"\n\n-- Your previous translation was rejected: {scartata[1]}\n-- Previous translation:\n"
                        + "\n".join("-- " + r for r in scartata[0][:5000].splitlines()))
        esito = await agente.run(domanda, usage_limits=UsageLimits(request_limit=3))
    except Exception as e:  # noqa: BLE001
        logger.warning("export dbt: traduzione AI non riuscita (%s): %s", model_id, e)
        raise DbtExportError(f"l'AI non ha risposto ({str(e)[:200]})", "ai_failed", detail=str(e)[:200]) from e
    _registra_spesa(session, user, model_id, esito)
    proposta = str(esito.output.sql or "").strip().rstrip(";").strip()
    _ricorda(session, flow.id, versione, "translate", {chiave: proposta}, model_id)
    return proposta, chiave


def dimentica(session: Session, flow: Flow, chiave: str) -> None:
    """Una proposta che l'engine ha scartato: la prossima volta si richiede."""
    from sqlalchemy import delete

    session.exec(delete(DbtAiText).where(DbtAiText.flow_id == flow.id, DbtAiText.kind == "translate", DbtAiText.key == chiave))
    session.commit()
