"""The AI in the dbt export — optional, chosen in the download dialog.

1. Descriptions WHERE MISSING: a paragraph per flow («what it does»), the columns of
   the output models, of the source tables and of the seeds. In English, as the rest
   of the project; marked «(AI)».
2. A TRANSLATION proposal where sqlglot cannot make it (see `translate`): the engine
   checks it, not the AI.

The user's choices (2026-10-10): what the AI writes is REMEMBERED per flow version
(the dbt_ai_texts table), so exporting the same version again gives the same zip; the
AI sees the flow's structure and FEW values per column (at most 3, short), read from
the datasource. The spending goes through the assistant's accounting (ai_spend) and
respects its caps; a cap reached or a provider not answering stops the export with
the reason, instead of removing the AI on the quiet.

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

MARK = "(AI) "
MAX_TEXT = 400            # characters of a description
MAX_COLUMNS = 400          # columns described in one export (beyond: they stay without)
SAMPLES = 3               # distinct values per column shown to the AI
SAMPLE_LENGTH = 40
SAMPLE_ROWS = 30        # rows read from the datasource to choose the values
MAX_KEY = 500           # characters of dbt_ai_texts.key: a longer key is stored as its digest
# characters of SQL the AI is asked to translate. Its answer is as long, and a query
# sent cut short loses its last steps while keeping the right columns, which is all
# the engine's checks compare: over this, no AI.
MAX_TRANSLATION_SQL = 20_000
WAIT = 0.5               # seconds between two looks at a claim another export holds


def ai_model(session: Session) -> Optional[str]:
    """The model to use: the default one when the administrator enabled it, otherwise the
    first enabled. None = the AI is not available."""
    cfg = get_settings().ai
    if not cfg.enabled:
        return None
    enabled_ones = enabled_model_ids(session)
    if not enabled_ones:
        return None
    return cfg.default_model if cfg.default_model in enabled_ones else enabled_ones[0]


def new_deadline() -> float:
    """When (time.monotonic) every AI call of ONE export must have answered. The export
    is one request, and the ingress cuts it at 300 s: the AI gives up first and says
    why, instead of a generic 504 while the gateway goes on paying."""
    return time.monotonic() + get_settings().ai.dbt_export_timeout_seconds


def _expired() -> DbtExportError:
    budget = get_settings().ai.dbt_export_timeout_seconds
    return DbtExportError(f"the AI did not answer within {budget:g} s", "ai_failed", detail=f"timed out after {budget:g} s")


def check_expiry(deadline: float) -> None:
    """Past the export's budget no further step starts: the engine calls between the AI's
    proposals (seeds read again, the ClickHouse syntax check) are not bounded by it."""
    if time.monotonic() > deadline:
        raise _expired()


def _check_caps(session: Session, user: User) -> None:
    cfg = get_settings().ai
    if cfg.max_cost_per_day_total_usd and ai_chats.spent_today_all(session) >= Decimal(str(cfg.max_cost_per_day_total_usd)):
        raise DbtExportError("the AI has reached the installation's spending cap for today", "ai_cap_reached")
    if cfg.max_cost_per_day_usd and ai_chats.spent_today(session, user) >= Decimal(str(cfg.max_cost_per_day_usd)):
        raise DbtExportError("you have reached your AI spending cap for today", "ai_cap_reached")


# What a YAML file cannot carry, even quoted: C0 controls, DEL, C1 controls, surrogates,
# U+FFFE/FFFF (whitespace aside, which `_clean` collapses). The AI echoes sample values,
# and text decoded with the wrong code page is full of C1 controls: one of them in
# schema.yml and `dbt parse` refuses the whole project.
_NOT_YAML = re.compile(r"[^\s\x20-\x7e\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _clean(text: Any) -> str:
    """One line, without Jinja's delimiters (dbt renders descriptions as Jinja) and
    without the characters YAML refuses, cut at MAX_TEXT."""
    t = re.sub(r"\s+", " ", _NOT_YAML.sub("", str(text or ""))).strip().strip("\"'`")
    for target, b in (("{{", "{ {"), ("}}", "} }"), ("{%", "{ %"), ("%}", "% }"), ("{#", "{ #"), ("#}", "# }")):
        t = t.replace(target, b)
    if len(t) > MAX_TEXT:
        t = t[: MAX_TEXT - 1].rstrip() + "…"
    return t


def _version(session: Session, flow: Flow) -> int:
    last_one = session.exec(select(FlowVersion.version).where(FlowVersion.flow_id == flow.id)
                          .order_by(FlowVersion.version.desc()).limit(1)).first()
    return int(last_one or 0)


def _key(key: str) -> str:
    """The key as stored. A survey question as a column name, or the type of a nested
    Struct, makes a key longer than its column: such a key is stored as its digest.
    Reads and writes both go through here; a key cut short was never found again, and
    its second insert broke the unique key."""
    if len(key) <= MAX_KEY:
        return key
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def _remembered(session: Session, flow_id: int, version: int, kind: str) -> dict[str, tuple[str, str]]:
    """What is stored for this flow version, by stored key (`_key`): (text, model)."""
    rows = session.exec(select(DbtAiText.key, DbtAiText.text, DbtAiText.model_id).where(
        DbtAiText.flow_id == flow_id, DbtAiText.flow_version == version, DbtAiText.kind == kind)).all()
    return {k: (t, m) for k, t, m in rows}


def version_texts(session: Session, flow: Flow) -> dict:
    """What the AI wrote that is kept for the flow's current version, for the download
    dialog: a re-export with the AI options gives it back without a call, with the AI
    switched off too, so the dialog can offer it then and name the models that wrote it
    (not the one configured now)."""
    rows = session.exec(select(DbtAiText.kind, DbtAiText.model_id).where(
        DbtAiText.flow_id == flow.id, DbtAiText.flow_version == _version(session, flow),
        DbtAiText.kind.in_(("describe", "translate")), DbtAiText.text != "")).all()
    return {"descriptions": any(k == "describe" for k, _ in rows), "translations": any(k == "translate" for k, _ in rows),
            "models": sorted({m for _, m in rows if m})}


def _insert(session: Session, rows: list[dict]) -> int:
    """INSERT … ON CONFLICT DO NOTHING (Postgres, and SQLite in the tests): a row that
    another export stored first stays as it is. Returns the rows inserted."""
    if not rows:
        return 0
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    now = datetime.now(timezone.utc)
    outcome = session.exec(insert(DbtAiText).values([{"model_id": "", "created_at": now, **r} for r in rows])
                         .on_conflict_do_nothing())
    return outcome.rowcount or 0


def _remember(session: Session, flow_id: int, version: int, kind: str, texts: dict[str, str], model_id: str) -> None:
    """Stores the texts. A key already stored keeps its text: another export, on this
    gateway or another one, stored it first, and that is the text every later export
    gives back."""
    _insert(session, [{"flow_id": flow_id, "flow_version": version, "kind": kind, "key": _key(key),
                          "text": text, "model_id": model_id} for key, text in texts.items()])
    session.commit()


@asynccontextmanager
async def _reservation(session: Session, flow_id: int, version: int, key: str, deadline: float) -> AsyncIterator[None]:
    """One export at a time asks the AI for the same texts of a flow version, on any
    gateway: the claim is a row of dbt_ai_texts (kind «claim») that the unique key lets
    one export insert. The others wait for it to go, and then find the texts stored, or
    for it to expire: an export that died does not hold the version past its deadline."""
    where = (DbtAiText.flow_id == flow_id, DbtAiText.flow_version == version,
            DbtAiText.kind == "claim", DbtAiText.key == _key(key))
    while True:
        rest = deadline - time.monotonic()
        if rest <= 0:
            raise _expired()
        mia = f"{time.time() + rest:.3f}"   # when this claim expires, wall clock: other gateways read it
        if _insert(session, [{"flow_id": flow_id, "flow_version": version, "kind": "claim", "key": _key(key), "text": mia}]):
            session.commit()
            break
        held = session.exec(select(DbtAiText.text).where(*where)).first()
        session.commit()   # no transaction left open while waiting (SQLite would keep its lock)
        if held is None:
            continue       # released in the meantime
        try:
            expired = float(held) < time.time()
        except ValueError:
            expired = True
        if expired:
            session.exec(delete(DbtAiText).where(*where, DbtAiText.text == held))
            session.commit()
            continue
        await asyncio.sleep(min(WAIT, rest))
    try:
        yield
    finally:
        try:
            session.exec(delete(DbtAiText).where(*where, DbtAiText.text == mia))
            session.commit()
        except Exception:  # noqa: BLE001 — the claim expires on its own at its deadline
            session.rollback()
            logger.warning("dbt export: AI claim on flow %s v%s not released", flow_id, version, exc_info=True)


def _record_spend(session: Session, user: User, model_id: str, usage: Any) -> None:
    try:
        ai_chats.record_spend(session, user_id=user.id, model_id=model_id, input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens, cost=ai_pricing.cost_of(usage, model_id))
    except Exception:  # noqa: BLE001 — the accounting must not lose an export paid for already
        logger.exception("dbt export: AI spending not recorded")


async def _run(model_id: str, output_type: type, instructions: str, question: str,
                  session: Session, user: User, deadline: float) -> Any:
    """One AI run under the export's limits: at most 3 requests, the assistant's
    per-turn caps on cost and input tokens (audit A8), the export's deadline. What the
    run cost goes to the spending ledger even when it fails halfway (an answer that
    fails validation is retried, and billed): the daily caps read only the ledger."""
    from pydantic_ai import Agent
    from pydantic_ai.usage import RunUsage, UsageLimits

    from app.services import ai_agent

    cfg = get_settings().ai
    usage = RunUsage()   # ours, passed in: pydantic-ai counts into it, so a run stopped halfway still says what it spent
    try:
        rest = deadline - time.monotonic()
        if rest <= 0:
            raise TimeoutError
        agent = Agent(ai_agent.build_model(model_id), output_type=output_type, instructions=instructions)
        limits = UsageLimits(request_limit=3,
                             cost_limit=Decimal(str(cfg.max_cost_per_turn_usd)) if cfg.max_cost_per_turn_usd else None,
                             input_tokens_limit=cfg.max_input_tokens_per_turn)
        outcome = await asyncio.wait_for(agent.run(question, usage=usage, usage_limits=limits), timeout=rest)
    except TimeoutError as e:
        logger.warning("dbt export: the AI (%s) did not answer within the export's budget", model_id)
        raise _expired() from e
    except Exception as e:  # noqa: BLE001 — the provider does not answer, or answers badly
        logger.warning("dbt export: AI call failed (%s): %s", model_id, e)
        raise DbtExportError(f"the AI did not answer ({str(e)[:200]})", "ai_failed", detail=str(e)[:200]) from e
    finally:
        if usage.requests:
            _record_spend(session, user, model_id, usage)
    return outcome.output


async def _samples(bucket: str, key: str) -> dict[str, list[str]]:
    """At most SAMPLES distinct, non-null and short values per column."""
    body = {"bucket": bucket, "input_key": key, "operations": [], "limit": SAMPLE_ROWS, "no_cache": True, "principal": "ai"}
    try:
        resp = await get_engine_client().post("/tasks/preview", json=body, timeout=120)
        if resp.status_code >= 400:
            return {}
        rows = resp.json().get("rows") or []
    except Exception:  # noqa: BLE001 — without values the description is made all the same
        logger.info("dbt export: sample values not read from %s/%s", bucket, key, exc_info=True)
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        for col, v in (r or {}).items():
            if v is None or v == "":
                continue
            text = str(v)[:SAMPLE_LENGTH]
            values = out.setdefault(col, [])
            if text not in values and len(values) < SAMPLES:
                values.append(text)
    return out


class _Item(BaseModel):
    key: str
    description: str


class _Descriptions(BaseModel):
    items: list[_Item]


_INSTRUCTIONS = (
    "You document a dbt project exported from Tabularia, a visual data-preparation tool. "
    "You get the flows (their steps, sources and outputs) and the columns that have no description, "
    "each with its type and a few sample values. For every item write a description in English: "
    "for a flow, two or three sentences on what it produces and from what; for a column, one sentence "
    "on what it holds. Use only what you are given; when the meaning is not clear, say plainly what the "
    "column contains instead of guessing a business meaning. No markdown, no quotes, no preamble. "
    "Return every item with its key unchanged."
)


def _steps(flow: Flow) -> dict[str, Any]:
    """The flow as the AI sees it: the names of the sources and the outputs, the steps in order."""
    try:
        definition = json.loads(flow.definition or "{}")
    except ValueError:
        definition = {}
    steps, sources, outputs = [], [], []
    for n in definition.get("nodes") or []:
        data = n.get("data") or {}
        if n.get("type") == "operation":
            params = json.dumps(data.get("params") or {}, ensure_ascii=False)
            steps.append({"step": data.get("label") or data.get("opType"), "type": data.get("opType"), "params": params[:300]})
        elif n.get("type") == "source":
            sources.append(data.get("label") or data.get("filename") or f"datasource {data.get('datasourceId')}")
        elif n.get("type") == "output":
            outputs.append(data.get("name") or data.get("table") or data.get("key") or data.get("destType"))
    return {"steps": steps, "sources": sources, "outputs": outputs}


def _column_items(payload: dict) -> list[tuple[str, dict, dict]]:
    """The columns without a description: (key, item for the AI, payload column to fill).
    The key recognizes the column by datasource, name and type — not by the model's name,
    which changes with the dialog's prefix."""
    out: list[tuple[str, dict, dict]] = []

    def add(where: str, ds_id: Optional[int], column: dict, origin: str):
        if column.get("description") or not column.get("name"):
            return
        kind = column.get("dtype") or ""
        base = f"ds:{ds_id}" if ds_id else f"{origin}"
        key = f"col:{base}:{column['name']}:{kind}"
        out.append((key, {"key": key, "kind": "column", "in": where, "column": column["name"], "type": kind}, column))

    for m in payload.get("models") or []:
        ds_id = m.get("datasource_id") or (m.get("meta") or {}).get("datasource_id")
        for c in m.get("columns") or []:
            add(f"model {m['name']}", ds_id, c, f"model:{m['name']}")
    for s in payload.get("sources") or []:
        for t in s.get("tables") or []:
            for c in t.get("columns") or []:
                add(f"source table {t['name']}", t.get("datasource_id"), c, f"table:{t['name']}")
    for sd in payload.get("seeds") or []:
        for c in sd.get("columns") or []:
            add(f"seed {sd['name']}", sd.get("datasource_id"), c, f"seed:{sd.get('key')}")
    return out


async def describe(session: Session, user: User, flow: Flow, flows: list[int], payload: dict,
                   deadline: float | None = None) -> None:
    """Fills the descriptions missing in the payload (changes the payload).

    Each text is asked once per flow version and kept, «no description» included: a
    re-export of the version asks nothing (the AI may be off by then) and gives the
    same zip, whatever model is configured now. `deadline` is the export's budget
    (`new_deadline`), shared with the translations."""
    deadline = deadline if deadline is not None else new_deadline()
    version = _version(session, flow)

    flow_items: list[tuple[str, dict, Flow]] = []
    for fid in sorted(set(flows)):
        f = session.get(Flow, fid)
        if f is None:
            continue
        key = f"flow:{fid}@v{_version(session, f)}"
        flow_items.append((key, {"key": key, "kind": "flow", "name": f.name, "description": f.description or "", **_steps(f)}, f))
    column_items = _column_items(payload)[:MAX_COLUMNS]
    items = [(k, v) for k, v, _ in flow_items] + [(k, v) for k, v, _ in column_items]

    remembered = _remembered(session, flow.id, version, "describe")
    if any(_key(k) not in remembered for k, _ in items):
        async with _reservation(session, flow.id, version, "describe", deadline):
            remembered = _remembered(session, flow.id, version, "describe")   # what the export that held the claim stored
            missing = list({k: v for k, v in items if _key(k) not in remembered}.values())
            if missing:
                await _describe_missing(session, user, flow, version, missing, deadline)
                remembered = _remembered(session, flow.id, version, "describe")   # what is stored wins

    models: set[str] = set()   # the models that wrote the texts used, not the one configured now
    for key, _, column in column_items:
        text, model = remembered.get(_key(key), ("", ""))
        if text:
            column["description"] = MARK + text
            models.add(model)
    summaries = []
    for key, item, f in flow_items:
        text, model = remembered.get(_key(key), ("", ""))
        if not text:
            continue
        models.add(model)
        summaries.append({"flow": f.name, "text": MARK + text})
        for m in payload.get("models") or []:
            if (m.get("meta") or {}).get("flow_id") == f.id and m.get("kind") == "output":
                m["description"] = (m.get("description") or "").rstrip() + " " + MARK + text
    payload["ai"] = {"model": ", ".join(sorted(m for m in models if m)) or None, "summaries": summaries}


async def _describe_missing(session: Session, user: User, flow: Flow, version: int, missing: list[dict], deadline: float) -> None:
    """Asks the AI for the missing texts and stores an outcome for every key asked."""
    model_id = ai_model(session)
    if model_id is None:
        raise DbtExportError("the AI is not available: the assistant is not configured, or no model is enabled", "ai_unavailable")
    _check_caps(session, user)
    # the few sample values, for the columns to describe (one read per datasource)
    samples: dict[int, dict[str, list[str]]] = {}
    for v in missing:
        m = re.match(r"col:ds:(\d+):", v["key"])
        if not m:
            continue
        ds_id = int(m.group(1))
        if ds_id not in samples:
            ds = session.get(Datasource, ds_id)
            samples[ds_id] = await _samples_within(ds, deadline) if ds is not None and ds.key else {}
        v["samples"] = samples[ds_id].get(v["column"], [])
    texts = await _ask(model_id, missing, session, user, deadline)
    remaining = [v for v in missing if v["key"] not in texts]
    if remaining:
        # models drop items from long lists, or answer some with nothing: those are asked
        # once more, alone. The first answer is stored first: it is paid for, whatever
        # the second call does.
        _remember(session, flow.id, version, "describe", texts, model_id)
        texts.update(await _ask(model_id, remaining, session, user, deadline))
    # every key asked gets an outcome, «no description» for what is still missing: a
    # version is asked once, so its re-export gives the same zip without the provider
    _remember(session, flow.id, version, "describe", {v["key"]: texts.get(v["key"], "") for v in missing}, model_id)


async def _samples_within(ds: Datasource, deadline: float) -> dict[str, list[str]]:
    """The sample values, within the export's budget."""
    try:
        return await asyncio.wait_for(_samples(ds.bucket, ds.key), timeout=max(deadline - time.monotonic(), 0))
    except TimeoutError as e:
        raise _expired() from e


async def _ask(model_id: str, items: list[dict], session: Session, user: User, deadline: float) -> dict[str, str]:
    """The descriptions the AI writes for `items`, by key: an item its answer leaves out,
    or answers with nothing, is not there."""
    outcome = await _run(model_id, _Descriptions, _INSTRUCTIONS, json.dumps({"items": items}, ensure_ascii=False),
                          session, user, deadline)
    waits = {v["key"] for v in items}
    return {v.key: text for v in (outcome.items or []) if v.key in waits and (text := _clean(v.description))}


def translation_key(sql: str, source: str, target: str) -> str:
    return "sql:" + hashlib.sha256(f"{source}>{target}\n{sql}".encode()).hexdigest()[:32]


# the engine errors for which the AI may propose a translation
TRANSLATABLE = ("translation_failed", "query_not_translatable", "query_not_single_select")
MAX_TRANSLATIONS = 5   # proposals in one export: beyond, the flow must be reviewed by hand
ATTEMPTS = 2        # per model: the second time the AI sees why the first was discarded


class _Translation(BaseModel):
    sql: str


_TRANSLATION_INSTRUCTIONS = (
    "You translate SQL between dialects for a dbt project. Translate the query you get from {source} to {target}. "
    "Every construct must exist in {target}: rewrite the ones that do not (for example, in ClickHouse SIMILAR TO "
    "becomes match() with an anchored regular expression, and there is no LATERAL nor TABLESAMPLE). "
    "Keep exactly these output columns, with these names, in this order: {columns}. Keep every table reference "
    "exactly as written — placeholders such as __s_0 or __m_1, and schema.table names: translate only syntax "
    "and functions. Return one SELECT statement, with no comments and no trailing semicolon, in the field sql."
)


async def translate(session: Session, user: User, flow: Flow, params: dict,
                  discarded: tuple[str, str] | None = None, deadline: float | None = None) -> tuple[str, str, str]:
    """A translation proposal for the model the engine did not translate, remembered per
    version. Returns (sql, key, model): the key serves to forget it when the engine
    discards it, the model is the one that wrote it (not the one configured now).
    `discarded` = (earlier proposal, why the engine discarded it): a second attempt, with
    the error in front — a model sometimes sends the query back as it was.

    A query over MAX_TRANSLATION_SQL characters is refused before any call: the AI would
    get it cut. Over ClickHouse's max_query_size (the engine's refusal of a model too
    large) it always is."""
    sql, source, target = str(params.get("sql") or ""), str(params.get("source_dialect") or ""), str(params.get("target_dialect") or "")
    columns = [str(c) for c in params.get("columns") or []]
    if len(sql) > MAX_TRANSLATION_SQL:
        model = str(params.get("model") or "")
        raise DbtExportError(
            f"the model «{model}» does not translate to {target} ({params.get('detail') or '-'}), and its SQL is too long "
            f"to ask the AI for a translation: {len(sql):,} characters, at most {MAX_TRANSLATION_SQL:,}",
            "ai_query_too_long", model=model, dialect=target, detail=str(params.get("detail") or "-"),
            size=f"{len(sql):,}", max=f"{MAX_TRANSLATION_SQL:,}")
    deadline = deadline if deadline is not None else new_deadline()
    version = _version(session, flow)
    key = translation_key(sql, source, target)
    already = _remembered(session, flow.id, version, "translate").get(_key(key))
    if already and already[0] and discarded is None:
        return already[0], key, already[1]
    async with _reservation(session, flow.id, version, f"translate:{key}", deadline):
        already = _remembered(session, flow.id, version, "translate").get(_key(key))
        if already and already[0] and discarded is None:
            return already[0], key, already[1]       # another export asked for it while this one waited
        if already and not already[0]:
            forget(session, flow, key, "")   # an empty proposal kept by an older gateway: the new one takes its place
        model_id = ai_model(session)
        if model_id is None:
            raise DbtExportError("the AI is not available: the assistant is not configured, or no model is enabled", "ai_unavailable")
        _check_caps(session, user)
        question = sql
        if discarded is not None:
            question += (f"\n\n-- Your previous translation was rejected: {discarded[1]}\n-- Previous translation:\n"
                        + "\n".join("-- " + r for r in discarded[0][:5000].splitlines()))
        instructions = _TRANSLATION_INSTRUCTIONS.format(source=source, target=target, columns=", ".join(columns) or "(the same as the query)")
        outcome = await _run(model_id, _Translation, instructions, question, session, user, deadline)
        proposal = str(outcome.sql or "").strip().rstrip(";").strip()
        if proposal:   # an empty one is rejected by the engine: kept, it would be served again, and paid again
            _remember(session, flow.id, version, "translate", {key: proposal}, model_id)
        # what is stored wins: another gateway may have stored its proposal first
        held = _remembered(session, flow.id, version, "translate").get(_key(key)) or (proposal, model_id)
    return held[0], key, held[1]


def forget(session: Session, flow: Flow, key: str, text: str) -> None:
    """A proposal the engine discarded: next time it is asked again. Only that
    text: another export may have replaced it meanwhile with one of its own."""
    session.exec(delete(DbtAiText).where(DbtAiText.flow_id == flow.id, DbtAiText.kind == "translate",
                                         DbtAiText.key == _key(key), DbtAiText.text == text))
    session.commit()
