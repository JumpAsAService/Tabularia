"""Data contracts: il documento, il suo stato, e la decisione al momento in cui
una datasource sta per ricevere dati nuovi.

Il gateway non valuta niente: non ha Polars e non legge i dati. Qui si controlla
la FORMA di un contratto, si conserva ciò che l'engine riferisce, e si tiene lo
stato che gli elenchi mostrano. La valutazione sta in `backend/app/contracts`.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import delete
from sqlmodel import Session, select

from app.models import DataContract, DataContractResult, DataContractVersion, Datasource

logger = logging.getLogger(__name__)

SEVERITIES = ("warning", "error")
DTYPES = ("integer", "number", "string", "boolean", "date", "datetime")
MAX_RULES = 200
MAX_VALUES = 500

# che cosa ogni tipo di regola porta con sé (oltre a id, kind, severity)
_FIELDS: dict[str, tuple[str, ...]] = {
    "column": ("column", "dtype"),
    "not_null": ("column",),
    "unique": ("columns",),
    "accepted_values": ("column", "values"),
    "range": ("column", "min", "max"),
    "pattern": ("column", "regex"),
    "row_count": ("min", "max"),
    "freshness": ("max_age_hours", "column"),
    "expression": ("sql", "name"),
}
KINDS = tuple(_FIELDS)


class ContractInvalid(ValueError):
    """Il documento non ha la forma di un contratto. `errors` dice dove e perché,
    con dei CODICI: la frase la scrive l'interfaccia, nella lingua di chi legge."""

    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} errori nel contratto")
        self.errors = errors


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_rule(r: dict) -> str | None:
    """Il codice del primo difetto della regola, o None se è scritta bene."""
    kind = r["kind"]
    if kind in ("column", "not_null", "accepted_values", "range", "pattern"):
        if not isinstance(r.get("column"), str) or not r["column"].strip():
            return "missing_column"
    if kind == "column" and r.get("dtype") is not None and r["dtype"] not in DTYPES:
        return "bad_dtype"
    if kind == "unique":
        cols = r.get("columns")
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and c.strip() for c in cols):
            return "missing_column"
    if kind == "accepted_values":
        vals = r.get("values")
        if not isinstance(vals, list) or not vals:
            return "empty_values"
        if len(vals) > MAX_VALUES:
            return "too_many_values"
        if not all(isinstance(v, (str, int, float, bool)) for v in vals):
            return "bad_values"
    if kind in ("range", "row_count"):
        lo, hi = r.get("min"), r.get("max")
        if lo is None and hi is None:
            return "no_bounds"
        if kind == "row_count":
            if any(v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0) for v in (lo, hi)):
                return "bad_bounds"
        elif any(v is not None and not (_is_number(v) or isinstance(v, str)) for v in (lo, hi)):
            return "bad_bounds"
        if _is_number(lo) and _is_number(hi) and lo > hi:
            return "bad_bounds"
    if kind == "pattern":
        if not isinstance(r.get("regex"), str) or not r["regex"] or len(r["regex"]) > 500:
            return "bad_regex"
        try:
            re.compile(r["regex"])  # approssimato: l'engine usa un altro motore, e ha l'ultima parola
        except re.error:
            return "bad_regex"
    if kind == "freshness":
        h = r.get("max_age_hours")
        if not _is_number(h) or h <= 0:
            return "bad_max_age"
        if r.get("column") is not None and (not isinstance(r["column"], str) or not r["column"].strip()):
            return "missing_column"
    if kind == "expression":
        if not isinstance(r.get("sql"), str) or not r["sql"].strip() or len(r["sql"]) > 2000:
            return "empty_expression"
    return None


def validate_document(doc: Any) -> dict:
    """Il contratto in forma canonica (solo i campi noti, id unici), o `ContractInvalid`."""
    if not isinstance(doc, dict) or not isinstance(doc.get("rules", []), list):
        raise ContractInvalid([{"index": None, "code": "not_a_contract"}])
    rules_in = doc.get("rules", [])
    if len(rules_in) > MAX_RULES:
        raise ContractInvalid([{"index": None, "code": "too_many_rules"}])
    errors: list[dict] = []
    rules: list[dict] = []
    used: set[str] = set()
    for i, raw in enumerate(rules_in):
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            errors.append({"index": i, "code": "unknown_kind"})
            continue
        if raw.get("severity") not in SEVERITIES:
            errors.append({"index": i, "code": "bad_severity"})
            continue
        r: dict = {"kind": raw["kind"], "severity": raw["severity"]}
        if raw["kind"] == "unique" and not raw.get("columns") and raw.get("column"):
            raw = {**raw, "columns": [raw["column"]]}  # forma breve
        for f in _FIELDS[raw["kind"]]:
            if raw.get(f) is not None and raw.get(f) != "":
                r[f] = raw[f]
        code = _check_rule(r)
        if code:
            errors.append({"index": i, "code": code})
            continue
        rid = raw.get("id") if isinstance(raw.get("id"), str) and 0 < len(raw["id"]) <= 40 else None
        if rid is None or rid in used:
            n = len(rules) + 1
            while f"r{n}" in used:
                n += 1
            rid = f"r{n}"
        used.add(rid)
        rules.append({"id": rid, **r})
    if errors:
        raise ContractInvalid(errors)
    description = doc.get("description") if isinstance(doc.get("description"), str) else ""
    return {"description": description[:2000], "rules": rules}


# ── lettura ──────────────────────────────────────────────────────────────────
def get(session: Session, datasource_id: int) -> DataContract | None:
    return session.exec(select(DataContract).where(DataContract.datasource_id == datasource_id)).first()


def active_document(session: Session, datasource_id: int | None) -> dict | None:
    """Il contratto da far valutare sui dati in arrivo: None se non c'è o è spento."""
    if datasource_id is None:
        return None
    c = get(session, datasource_id)
    if c is None or not c.enabled:
        return None
    try:
        doc = json.loads(c.document)
    except json.JSONDecodeError:
        return None
    return doc if doc.get("rules") else None


def effective_status(c: DataContract) -> str:
    """Ciò che l'icona deve dire. Un aggiornamento rifiutato pesa più dell'ultimo
    referto: quei dati reggono il contratto, ma non sono più quelli attuali."""
    return "failed" if c.blocked_at is not None else c.status


def summary(c: DataContract) -> dict:
    return {
        "status": effective_status(c),
        "checked_at": c.checked_at,
        "errors": c.errors,
        "warnings": c.warnings,
        "blocked": c.blocked_at is not None,
        "version": c.version,
    }


def summaries(session: Session, datasource_ids: Iterable[int]) -> dict[int, dict]:
    """Lo stato del contratto per un elenco di datasource, con UNA query.
    Chi non ha un contratto acceso non compare."""
    ids = [i for i in datasource_ids if i is not None]
    if not ids:
        return {}
    rows = session.exec(
        select(DataContract).where(DataContract.datasource_id.in_(ids), DataContract.enabled == True)  # noqa: E712
    ).all()
    return {c.datasource_id: summary(c) for c in rows}


# ── scrittura ────────────────────────────────────────────────────────────────
def save(
    session: Session, ds: Datasource, document: dict, enabled: bool, user_id: int | None,
    *, notify: tuple[str | None, int | None] | None = None,
) -> DataContract:
    """Crea o sostituisce il contratto (documento già validato). Un documento
    cambiato è una versione nuova, e lo stato torna «da verificare»: il referto
    che c'era parlava di un altro contratto. `notify` = (indirizzi, connessione
    SMTP) già controllati; None = come prima. NON committa."""
    now = datetime.now(timezone.utc)
    text = json.dumps(document, ensure_ascii=False, sort_keys=True)
    c = get(session, ds.id)
    if c is None:
        last = session.exec(
            select(DataContractVersion.version)
            .where(DataContractVersion.datasource_id == ds.id)
            .order_by(DataContractVersion.version.desc())
        ).first()
        c = DataContract(datasource_id=ds.id, version=(last or 0) + 1, document=text, enabled=enabled, created_by=user_id)
        changed = True
    else:
        changed = c.document != text
        if changed:
            c.version += 1
            c.document = text
        c.enabled = enabled
    if changed:
        c.status, c.checked_at, c.errors, c.warnings, c.last_report = "pending", None, 0, 0, None
        session.add(DataContractVersion(datasource_id=ds.id, version=c.version, document=text, created_by=user_id))
    if notify is not None:
        c.notify_emails, c.notify_connection_id = notify
    c.updated_by, c.updated_at = user_id, now
    session.add(c)
    session.flush()
    return c


def forget(session: Session, datasource_id: int, keep_history: bool = False) -> None:
    """Toglie il contratto. `keep_history`: lo storico dei referti e delle versioni
    resta (si è tolto il contratto, non la datasource). NON committa."""
    session.exec(delete(DataContract).where(DataContract.datasource_id == datasource_id))
    if not keep_history:
        session.exec(delete(DataContractVersion).where(DataContractVersion.datasource_id == datasource_id))
        session.exec(delete(DataContractResult).where(DataContractResult.datasource_id == datasource_id))


# Gli avvisi partono per ciò che accade da solo (dati nuovi, il tempo che passa).
# Chi salva il contratto o lo verifica a mano sta già guardando lo schermo.
NOTIFY_TRIGGERS = ("refresh", "publish", "freshness")


def signal(c: DataContract) -> str:
    """Ciò che un avviso racconta: lo stato, o «refused» se l'ultimo
    aggiornamento è stato rifiutato."""
    return "refused" if c.blocked_at is not None else c.status


def _worth_telling(before: str, after: str) -> bool:
    """Si avvisa al CAMBIO di stato, non a ogni valutazione: un refresh rifiutato
    ogni notte manda un'email, non una a notte. E la prima verifica andata bene
    (da «pending») non è una notizia."""
    return after != before and not (after == "passed" and before == "pending")


def record_result(
    session: Session, c: DataContract, report: dict, *, trigger: str, run_id: int | None = None,
    snapshot_key: str = "", blocked: bool = False,
) -> DataContractResult:
    """Conserva un referto e aggiorna lo stato del contratto. NON committa.

    `blocked`: il referto riguarda dati che NON sono stati pubblicati. Lo stato
    sui dati correnti non cambia (sono ancora quelli di prima); si segna che
    l'ultimo aggiornamento è stato rifiutato. Un referto su dati pubblicati,
    invece, è il nuovo stato — e se arriva da un aggiornamento riuscito cancella
    il segno del rifiuto precedente."""
    now = datetime.now(timezone.utc)
    outcome = report.get("outcome") if report.get("outcome") in ("passed", "warning", "failed") else "failed"
    text = json.dumps(report, ensure_ascii=False, default=str)
    result = DataContractResult(
        datasource_id=c.datasource_id, contract_version=c.version, trigger=trigger, run_id=run_id,
        snapshot_key=snapshot_key, outcome=outcome, blocked=blocked,
        errors=int(report.get("errors") or 0), warnings=int(report.get("warnings") or 0),
        rows=report.get("rows"), report=text, evaluated_at=now,
    )
    session.add(result)
    before = signal(c)
    if blocked:
        c.blocked_at, c.blocked_run_id = now, run_id
    else:
        c.status, c.checked_at = outcome, now
        c.errors, c.warnings = result.errors, result.warnings
        c.last_report = text
        if trigger in ("refresh", "publish"):
            c.blocked_at, c.blocked_run_id = None, None
    if trigger in NOTIFY_TRIGGERS and c.notify_emails and c.notify_connection_id and _worth_telling(before, signal(c)):
        result.notify = "pending"
    session.add(c)
    session.flush()
    return result


def payload(session: Session, datasource_id: int | None) -> dict | None:
    """Il contratto da mandare all'engine insieme ai dati da scrivere, con la sua
    versione (torna nel referto). None = niente da valutare."""
    doc = active_document(session, datasource_id)
    if doc is None:
        return None
    return {**doc, "version": get(session, datasource_id).version}


def broken_rules_message(report: dict) -> str:
    """La frase che finisce nell'errore di un run rifiutato: quali regole bloccanti
    non hanno retto. Breve: il dettaglio sta nel referto."""
    parts = []
    for x in report.get("rules", []):
        if x.get("passed") or x.get("severity") == "warning":
            continue
        dove = x.get("column") or ", ".join(x.get("columns") or []) or x.get("name") or ""
        quante = f" ({x['violations']} righe)" if x.get("violations") else ""
        parts.append(f"{x.get('kind')}{' su ' + dove if dove else ''}{quante}")
    if not parts and report.get("error"):
        parts.append(f"contratto non valutabile: {report['error']}")
    elenco = "; ".join(parts[:6]) + (f"; e altre {len(parts) - 6}" if len(parts) > 6 else "")
    return (
        f"Data contract violato: {elenco}. I dati nuovi NON sono stati pubblicati: "
        "la datasource serve ancora lo snapshot precedente."
    )


# ── freschezza: l'unica regola che si rompe senza che arrivi niente ──────────
def refresh_freshness(session: Session, c: DataContract, ds: Datasource, now: datetime | None = None) -> bool:
    """Ricalcola le regole di freschezza dello snapshot sul tempo che è passato.
    Torna True se l'esito è cambiato (e allora un referto nuovo è stato scritto).
    NON committa. Le regole di freschezza su una COLONNA guardano i dati: quelle
    le valuta l'engine, e qui non si toccano."""
    if not c.enabled or not c.last_report:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        document, report = json.loads(c.document), json.loads(c.last_report)
    except json.JSONDecodeError:
        return False
    rules = {r["id"]: r for r in document.get("rules", []) if r.get("kind") == "freshness" and not r.get("column")}
    if not rules:
        return False
    produced = ds.refreshed_at or ds.updated_at or ds.created_at
    if produced.tzinfo is None:
        produced = produced.replace(tzinfo=timezone.utc)
    age = round((now - produced).total_seconds() / 3600, 2)
    for x in report.get("rules", []):
        r = rules.get(x.get("id"))
        if r is not None:
            x.update(passed=age <= r["max_age_hours"], observed=age, expected=r["max_age_hours"])
            x.pop("error", None)
    broken = [x for x in report.get("rules", []) if not x.get("passed")]
    errors = sum(1 for x in broken if x.get("severity") != "warning")
    outcome = "failed" if errors else "warning" if broken else "passed"
    if outcome == c.status:
        return False
    report.update(outcome=outcome, errors=errors, warnings=len(broken) - errors)
    record_result(session, c, report, trigger="freshness", snapshot_key=ds.key)
    return True


def sweep_freshness(session: Session, now: datetime | None = None) -> int:
    """Passa i contratti accesi che hanno una regola di freschezza e aggiorna
    quelli il cui esito è cambiato col tempo. Lo chiama lo scheduler; più
    processi possono farlo insieme (al più scrivono lo stesso referto due volte).
    Torna quanti stati sono cambiati. Committa un contratto alla volta."""
    cambiati = 0
    rows = session.exec(
        select(DataContract, Datasource)
        .where(DataContract.datasource_id == Datasource.id)
        .where(DataContract.enabled == True, DataContract.document.contains('"freshness"'))  # noqa: E712
    ).all()
    for c, ds in rows:
        try:
            if refresh_freshness(session, c, ds, now):
                cambiati += 1
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("freschezza del contratto della datasource %s non aggiornata", ds.id)
    return cambiati
