"""L'assistente AI di Tabularia (pydantic-ai): chat sui dati del catalogo.

Tre strumenti, tutti sotto lo STESSO controllo d'accesso del resto del prodotto
— l'assistente vede e interroga solo le datasource che l'utente puo' LEGGERE
(capability VIEW sulla cartella, ereditata):

- `list_datasources`     cosa c'e' nel catalogo, per questo utente;
- `describe_datasource`  colonne, tipi e DESCRIZIONI DEI CAMPI curate a mano
                         (sono il contesto semantico che fa scrivere la query
                         giusta), piu' qualche riga d'esempio;
- `query_datasource`     una SELECT sulla datasource, eseguita dal motore con
                         il nodo `sql` (tabella = `self`): stesse protezioni di
                         un nodo SQL dell'editor, righe limitate, e un evento
                         di audit per ogni query — e' un accesso ai dati.

I numeri nelle risposte devono venire da una query: il modello non li inventa,
e la chat mostra comunque la tabella restituita dallo strumento.
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from fastapi import Request

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # niente banner nei log del gateway

from pydantic_ai import Agent, ModelRetry, RunContext  # noqa: E402
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models import Datasource, Project, User
from app.services import audit
from app.services.permissions import readable_project_ids

logger = logging.getLogger(__name__)

MAX_LISTED = 60
LIST_DESCRIPTION_CHARS = 280  # nell'elenco basta l'inizio: il testo intero arriva da describe_datasource
MAX_CELL_CHARS = 300
SAMPLE_ROWS = 5

# dialetto SQL del nodo `sql` per motore, e come si quotano i nomi "difficili"
_DIALECTS = {
    "polars": ("Polars SQL (a PostgreSQL-like subset: no window frames, keep it simple)", '"'),
    "duckdb": ("DuckDB SQL", '"'),
    "chdb": ("ClickHouse SQL", "`"),
    "clickhouse": ("ClickHouse SQL", "`"),
    "bigquery": ("GoogleSQL (BigQuery)", "`"),
}
_LANGUAGES = {"it": "Italian", "en": "English", "fr": "French", "de": "German", "es": "Spanish"}
# solo SELECT: il nodo `sql` avvolge gia' la query in un suo WITH (input/self),
# quindi una query che INIZIA con WITH non e' valida su nessun motore
_SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE)


@dataclass
class ChatDeps:
    """Cio' che gli strumenti possono toccare: l'utente (per i permessi), una
    fabbrica di sessioni DB (una per chiamata: lo stream vive piu' a lungo della
    richiesta), il motore scelto e la richiesta HTTP per l'audit."""

    user: User
    session_factory: Callable[[], AbstractContextManager[Session]]
    engine: str | None
    model_id: str
    locale: str = "en"
    request: Request | None = None
    # datasource che l'utente ha messo a fuoco nella pagina (gia' filtrate sui
    # suoi permessi): [{id, name}]. Solo un suggerimento di contesto: ogni
    # strumento ricontrolla comunque l'accesso.
    focus: list[dict[str, Any]] | None = None


def _columns(ds: Datasource) -> list[dict[str, Any]]:
    try:
        cols = json.loads(ds.columns or "[]")
        descr = json.loads(ds.column_descriptions or "{}")
    except ValueError:
        cols, descr = [], {}
    out = []
    for c in cols if isinstance(cols, list) else []:
        name = str(c.get("name", ""))
        item = {"name": name, "type": str(c.get("dtype", ""))}
        if isinstance(descr, dict) and descr.get(name):
            item["description"] = str(descr[name])
        out.append(item)
    return out


def _readable_datasource(session: Session, user: User, datasource_id: int) -> Datasource:
    """La datasource, SE l'utente puo' leggerla. Una datasource che non esiste e
    una che non puo' vedere danno lo stesso errore: l'assistente non deve
    rivelare cosa c'e' fuori dai suoi permessi."""
    ds = session.get(Datasource, datasource_id)
    if ds is None or ds.project_id not in readable_project_ids(session, user):
        raise ModelRetry(f"No datasource with id {datasource_id} is available to this user. Call list_datasources first.")
    return ds


def readable_focus(session: Session, user: User, ids: list[int]) -> list[dict[str, Any]]:
    """Le datasource messe a fuoco dall'utente, ridotte a quelle che puo' leggere."""
    if not ids:
        return []
    readable = readable_project_ids(session, user)
    out = []
    for ds_id in ids[:8]:
        ds = session.get(Datasource, ds_id)
        if ds is not None and ds.project_id in readable and ds.key:
            out.append({"id": ds.id, "name": ds.name})
    return out


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LIST_DESCRIPTION_CHARS else text[:LIST_DESCRIPTION_CHARS].rstrip() + "…"


def _trim(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append({k: (v[:MAX_CELL_CHARS] + "…" if isinstance(v, str) and len(v) > MAX_CELL_CHARS else v) for k, v in r.items()})
    return out


async def _engine_preview(ds: Datasource, operations: list[dict], limit: int, deps: ChatDeps) -> dict[str, Any]:
    """Una preview sull'engine, come la farebbe il Viewer (senza step-cache).
    Lo slot `u<id>:ai` fa si' che una nuova query dell'assistente per lo stesso
    utente butti giu' la precedente ancora in corso."""
    body: dict[str, Any] = {
        "bucket": ds.bucket, "input_key": ds.key, "operations": operations,
        "limit": limit, "no_cache": True, "slot": f"u{deps.user.id}:ai",
        # l'engine ClickHouse esegue queste query con l'utenza dedicata `ai`, se
        # configurata (CLICKHOUSE_EXTERNAL__AI_USERNAME/__AI_PASSWORD)
        "principal": "ai",
    }
    if deps.engine:
        body["engine"] = deps.engine
    try:
        sort_keys = json.loads(ds.sort_keys or "[]")
        if sort_keys:
            body["sort_keys"] = sort_keys
    except ValueError:
        pass
    resp = await get_engine_client().post("/tasks/preview", json=body, timeout=180)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        raise _EngineRefusal(resp.status_code, str(detail)[:1500])
    return resp.json()


class _EngineRefusal(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _instructions(ctx: RunContext[ChatDeps]) -> str:
    deps = ctx.deps
    dialect, quote = _DIALECTS.get((deps.engine or "polars").lower(), _DIALECTS["polars"])
    language = _LANGUAGES.get(deps.locale[:2].lower(), "English")
    cap = get_settings().ai.max_result_rows
    scope = ""
    if deps.focus:
        names = ", ".join(f"{d['name']} (id {d['id']})" for d in deps.focus)
        scope = f"\n\nThe user has selected these datasources as the scope of the conversation: {names}. Work on them, skipping list_datasources, unless the user asks about something else."
    return _BASE_INSTRUCTIONS(language, dialect, quote, cap) + scope


def _BASE_INSTRUCTIONS(language: str, dialect: str, quote: str, cap: int) -> str:
    return f"""You are the data assistant of Tabularia, a self-hosted data-preparation platform. \
You help the user understand the data in the catalog: find the right datasource, query it, and explain what the numbers say.

Today is {date.today().isoformat()}. Answer in {language}, concisely, in Markdown.

How to work:
1. You only know what the tools tell you. Start from `list_datasources` unless the conversation already identified the datasource. Each datasource may carry a description written by its owners (what it contains, what one row is, the period, caveats): use it to pick the right table, and tell the user when two candidates look alike.
2. Before querying a datasource, call `describe_datasource`: the full datasource description, column names, types and the hand-written field descriptions tell you what the table and each column MEAN. Trust the descriptions over your guesses about a column name.
3. Query with `query_datasource`. The table is always called `self`. Write one statement that STARTS with SELECT, in {dialect}: no leading WITH (the engine already wraps your query in one), use subqueries in FROM instead. Quote column names that contain spaces or symbols with {quote}. Aggregate in SQL (GROUP BY, COUNT, SUM, AVG…) instead of fetching raw rows: results are capped at {cap} rows.
4. If a query fails, read the error, fix the SQL and try again. If it keeps failing, say so plainly.

Rules:
- A question about two datasources is one query, not two: join them with `join_datasource_id` instead of querying each and combining the numbers yourself. Adding up figures from two separate results is exactly how a wrong total gets stated confidently.
- When the answer is a ranking, a share of a total or a series over time, pass `chart` to `query_datasource` so the result is drawn as well as listed — and always when the user asks for a chart. Omit it for a single number or a couple of rows.
- Every figure you state must come from a query result in this conversation. Never estimate, never invent, and do the arithmetic (totals, shares, differences) in SQL rather than in your head.
- Name the datasource you used. If the data cannot answer the question, say what is missing.
- Never describe what a datasource contains from its name alone: quote its description, or its columns, from a tool result. If it has no description, say so and look at the columns. The same holds for fields: a column without a description is undescribed, and a meaning you infer from its name or its values must be presented as your reading, never as catalog documentation.
- You cannot modify data, run flows or see datasources the user has no access to. If nothing is available, tell the user to ask for access to a folder.
- The interface shows the result table of each query under your answer: do not repeat long tables in text, summarise them and point at what matters."""


agent: Agent[ChatDeps, str] = Agent(
    deps_type=ChatDeps,
    instructions=_instructions,
    retries=2,
    name="tabularia-assistant",
)


def _words(text: str) -> str:
    """Testo ridotto a parole minuscole: `anagrafica_test`, «Anagrafica di test»
    e `ANAGRAFICA-TEST` devono incontrarsi."""
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


_STOPWORDS = {"di", "del", "della", "dei", "the", "of", "de", "la", "le", "il", "lo", "der", "die", "das", "el", "los", "las", "du", "des"}


@agent.tool
async def list_datasources(ctx: RunContext[ChatDeps], search: str = "") -> dict[str, Any]:
    """List the datasources this user can read, each with the description its owners wrote. `search` is matched word by word against name and description, ignoring case, underscores and dashes; leave it empty to list everything. If nothing matches, everything available is returned with a note."""
    deps = ctx.deps
    with deps.session_factory() as session:
        readable = readable_project_ids(session, deps.user)
        if not readable:
            return {"datasources": [], "note": "This user has no readable datasource."}
        rows = session.exec(select(Datasource).where(Datasource.project_id.in_(readable))).all()  # type: ignore[attr-defined]
        projects = {p.id: p.name for p in session.exec(select(Project)).all()}
        items = []
        for ds in sorted(rows, key=lambda d: d.name.lower()):
            if not ds.key:  # datasource database mai ingerita: nessun dato da interrogare
                continue
            items.append((_words(f"{ds.name} {ds.description or ''}"), {
                "id": ds.id, "name": ds.name, "folder": projects.get(ds.project_id, ""),
                "rows": ds.rows, "columns": len(_columns(ds)), "description": _short(ds.description or ""),
            }))
    tokens = [t for t in _words(search).split() if len(t) > 1 and t not in _STOPWORDS]
    if not tokens:
        return {"datasources": [item for _, item in items][:MAX_LISTED]}
    hits = [item for hay, item in items if all(t in hay for t in tokens)] or [item for hay, item in items if any(t in hay for t in tokens)]
    if hits:
        return {"datasources": hits[:MAX_LISTED]}
    # nessuna corrispondenza: meglio mostrare tutto che lasciare il modello a indovinare
    return {"datasources": [item for _, item in items][:MAX_LISTED],
            "note": f"Nothing matches '{search}'. These are ALL the datasources available to the user: pick from them or say none fits."}


@agent.tool
async def describe_datasource(ctx: RunContext[ChatDeps], datasource_id: int) -> dict[str, Any]:
    """Columns (name, type, and the curated description of what each field means), row count and a few sample rows of one datasource."""
    deps = ctx.deps
    with deps.session_factory() as session:
        ds = _readable_datasource(session, deps.user, datasource_id)
        info: dict[str, Any] = {
            "id": ds.id, "name": ds.name, "description": ds.description or "", "rows": ds.rows,
            "columns": _columns(ds),
        }
        snapshot = Datasource(id=ds.id, name=ds.name, project_id=ds.project_id, bucket=ds.bucket, key=ds.key, sort_keys=ds.sort_keys)
    try:
        res = await _engine_preview(snapshot, [], SAMPLE_ROWS, deps)
        info["sample_rows"] = _trim(res.get("rows", []))
    except Exception as e:  # noqa: BLE001 — le righe d'esempio sono un aiuto, non un requisito
        logger.info("assistente: righe d'esempio di %s non disponibili (%s)", datasource_id, e)
    return info


def _chiavi(testo: Optional[str]) -> list[str]:
    return [c.strip() for c in (testo or "").replace(";", ",").split(",") if c.strip()]


def _join_operation(session, user, ds_id, left_on, right_on, how):
    """L'operazione di join verso una SECONDA datasource, o `(None, None)`.

    La sicurezza sta tutta qui: il lato destro arriva come ID e viene risolto
    con `_readable_datasource`, cioe' con gli stessi permessi di tutto il resto.
    Il modello non vede ne' bucket ne' chiavi, e la SQL continua a leggere solo
    `self` — che dopo questa operazione e' il risultato gia' unito. La lista
    bianca del nodo sql resta quella che e'."""
    if ds_id is None:
        return None, None
    sinistra, destra = _chiavi(left_on), _chiavi(right_on)
    if not sinistra or not destra:
        raise ModelRetry("To join, give both join_left_on and join_right_on: the key columns on each side.")
    if len(sinistra) != len(destra):
        raise ModelRetry(
            f"The join keys must match in number: {len(sinistra)} on the left, {len(destra)} on the right."
        )
    altra = _readable_datasource(session, user, ds_id)
    params = {
        "right": {"source": {"bucket": altra.bucket, "key": altra.key}, "operations": []},
        "left_on": sinistra,
        "right_on": destra,
        "how": how or "inner",
    }
    return {"type": "join", "params": params}, altra.name


CHART_TYPES = ("bar", "hbar", "line", "area", "pie", "donut", "scatter")




def _chart_spec(chart: Optional[Any], columns: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Controlla la specifica del grafico contro le colonne che la query ha davvero
    restituito.

    Un grafico che nomina una colonna inesistente e' peggio di nessun grafico: la
    pagina disegnerebbe il vuoto e l'utente non saprebbe perche'. Si rifiuta con
    un motivo, che torna al modello."""
    if not chart:
        return None, None
    tipo = str(chart.get("type") or "").lower()
    if tipo not in CHART_TYPES:
        return None, f"Unknown chart type {tipo!r}. Use one of: {', '.join(CHART_TYPES)}."
    nomi = {str(c.get("name")) for c in columns if isinstance(c, dict)}
    x, y = str(chart.get("x") or ""), str(chart.get("y") or "")
    mancanti = [n for n in (x, y) if n not in nomi]
    if mancanti:
        return None, (
            f"The chart names columns the query does not return: {', '.join(mancanti)}. "
            f"Available: {', '.join(sorted(nomi))}."
        )
    serie = chart.get("series")
    if serie is not None and str(serie) not in nomi:
        return None, f"The series column {serie!r} is not in the result."
    spec = {"type": tipo, "x": x, "y": y}
    if serie is not None:
        spec["series"] = str(serie)
    titolo = chart.get("title")
    if titolo:
        spec["title"] = str(titolo)[:120]
    return spec, None


@agent.tool
async def query_datasource(
    ctx: RunContext[ChatDeps], datasource_id: int, sql: str, limit: int = 50,
    join_datasource_id: Optional[int] = None,
    join_left_on: Optional[str] = None,
    join_right_on: Optional[str] = None,
    join_how: Optional[Literal["inner", "left", "full"]] = None,
    chart_type: Optional[Literal["bar", "hbar", "line", "area", "pie", "donut", "scatter"]] = None,
    chart_x: Optional[str] = None,
    chart_y: Optional[str] = None,
    chart_series: Optional[str] = None,
    chart_title: Optional[str] = None,
) -> dict[str, Any]:
    """Run ONE read-only SELECT on a datasource. The table is named `self` (e.g. `SELECT paese, COUNT(*) AS n FROM self GROUP BY paese ORDER BY n DESC`). Returns columns, rows (capped) and whether the result was truncated.

    To answer a question that spans TWO datasources, set `join_datasource_id` to the second one together with `join_left_on` and `join_right_on` (the key columns on each side, comma-separated if the key has several parts). The two are joined before your SQL runs, and `self` is the joined result — so write the query against the columns of both. `join_how` is inner by default; use left to keep rows that have no match. A column that exists on both sides and is not a key gets the suffix `_right`. Call `describe_datasource` on both first, so you join on columns that exist.

    To draw the result as a chart under the table, set `chart_type` together with `chart_x` (the categories) and `chart_y` (the numeric column) — both must be columns your query actually returns. `chart_series` optionally splits the data into several series, `chart_title` is a short title in the user's language. Use bar for a ranking, hbar when the labels are long or the categories many, line or area for a series over time, pie or donut for shares of a total, scatter for two numeric measures. Leave them unset for a single number, for a couple of rows that already read fine, or for a non-numeric result: a chart of three rows is noise."""
    deps = ctx.deps
    cap = get_settings().ai.max_result_rows
    limit = max(1, min(int(limit or 50), cap))
    query = sql.strip().rstrip(";").strip()
    if not _SELECT_ONLY.match(query) or ";" in query:
        raise ModelRetry("Only ONE statement starting with SELECT is allowed (no leading WITH, no ';'). Use subqueries in FROM instead of CTEs.")
    if not re.search(r"\bfrom\s+(self|input)\b", query, re.IGNORECASE):
        raise ModelRetry("The query must read from the datasource: use `FROM self`.")
    with deps.session_factory() as session:
        ds = _readable_datasource(session, deps.user, datasource_id)
        snapshot = Datasource(id=ds.id, name=ds.name, project_id=ds.project_id, bucket=ds.bucket, key=ds.key, sort_keys=ds.sort_keys)
        # La seconda datasource si risolve da un ID sotto gli STESSI permessi:
        # il modello non nomina mai un bucket o una chiave, quindi la join non
        # e' una strada per leggere qualcosa fuori dai permessi di chi chiede.
        join_op, join_nome = _join_operation(
            session, deps.user, join_datasource_id, join_left_on, join_right_on, join_how,
        )
    outcome, n_rows = "success", 0
    try:
        operazioni = ([join_op] if join_op else []) + [{"type": "sql", "params": {"query": query}}]
        res = await _engine_preview(snapshot, operazioni, limit, deps)
        rows = _trim(res.get("rows", []))
        n_rows = len(rows)
        colonne = res.get("columns", [])
        esito: dict[str, Any] = {
            "datasource": f"{snapshot.name} ⋈ {join_nome}" if join_nome else snapshot.name,
            "columns": colonne, "rows": rows,
            "row_count": n_rows, "truncated": bool(res.get("truncated")),
        }
        # il grafico e' dell'utente: si valida, non si esegue niente di nuovo
        spec, motivo = _chart_spec(
            {"type": chart_type, "x": chart_x, "y": chart_y, "series": chart_series, "title": chart_title}
            if chart_type else None,
            colonne,
        )
        if spec:
            esito["chart"] = spec
        elif motivo:
            # al modello si dice PERCHE', cosi' alla prossima domanda sceglie meglio
            esito["chart_error"] = motivo
        return esito
    except _EngineRefusal as e:
        outcome = "failure"
        if e.status >= 500:
            return {"error": f"The engine failed ({e.status}): {e.detail}"}
        # errore di SQL o di colonna: il modello puo' correggersi
        raise ModelRetry(f"The engine refused the query: {e.detail}") from e
    finally:
        with deps.session_factory() as session:
            audit.record_audit(
                session, actor=deps.user, action=audit.AI_QUERY, outcome=outcome,
                target_type="datasource", target_id=snapshot.id, target_label=snapshot.name,
                detail={"sql": query[:2000], "model": deps.model_id, "engine": deps.engine or "default", "rows": n_rows},
                request=deps.request,
            )


def build_model(model_id: str):
    """Il modello pydantic-ai per un id del provider. Separato (e sostituibile)
    perche' i test non devono parlare con un provider vero."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    cfg = get_settings().ai
    provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.secret_key.get_secret_value())
    return OpenAIChatModel(model_id, provider=provider)


# ── titolo della conversazione ───────────────────────────────────────────────
TITLE_MAX_CHARS = 60

_TITLE_INSTRUCTIONS = (
    "You name conversations. Given the user's first question, reply with a title of "
    "three to six words that says what the conversation is about. Reply with the title "
    "ALONE: no quotes, no final period, no preamble. Write it in {lingua}."
)


async def suggest_title(model_id: str, question: str, locale: str = "en") -> tuple[str | None, Any]:
    """Un titolo breve per la conversazione, chiesto al modello.

    Vale una chiamata minuscola una volta sola, al primo turno: il titolo resta
    poi per sempre nell'elenco, e «Quante righe ha la tabella degli ordini del
    2025 divise per...» tagliato a 60 caratteri non aiuta a ritrovare nulla.

    Ritorna `(titolo, uso)`; il titolo è `None` a ogni intoppo e il chiamante
    tiene il ripiego che ha già (la domanda accorciata). Un titolo non vale un
    errore in faccia all'utente. L'uso torna insieme perché anche questa è una
    chiamata a pagamento: il totale della conversazione deve comprenderla."""
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    domanda = (question or "").strip()
    if not domanda:
        return None, None
    try:
        agente = Agent(
            build_model(model_id),
            instructions=_TITLE_INSTRUCTIONS.format(lingua=_LANGUAGES.get(locale[:2].lower(), "English")),
        )
        esito = await agente.run(domanda[:1000], usage_limits=UsageLimits(request_limit=1))
        uso = esito.usage
        uso = uso() if callable(uso) else uso
        titolo = " ".join(str(esito.output or "").split()).strip(" \"'«»`.")
        if not titolo:
            return None, uso
        return titolo[:TITLE_MAX_CHARS].rstrip(" ,;:-"), uso
    except Exception:  # noqa: BLE001 — accessorio: non deve mai disturbare la risposta
        logger.info("assistente: titolo non generato per la chat (modello %s)", model_id, exc_info=True)
        return None, None
