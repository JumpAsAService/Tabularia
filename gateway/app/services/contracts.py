"""Data contracts: the document, its state, and the decision at the moment a datasource
is about to receive new data.

The gateway evaluates nothing: it has no Polars and does not read the data. Here the
FORM of a contract is checked, what the engine reports is kept, and the state the
listings show is held. The evaluation is in `backend/app/contracts`.
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

# what each kind of rule carries (besides id, kind, severity)
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
    """The document does not have the form of a contract. `errors` says where and why, with
    CODES: the interface writes the sentence, in the reader's language."""

    def __init__(self, errors: list[dict]):
        super().__init__(f"{len(errors)} errors in the contract")
        self.errors = errors


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_rule(r: dict) -> str | None:
    """The code of the rule's first defect, or None when it is well written."""
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
            re.compile(r["regex"])  # approximate: the engine uses another engine, and has the last word
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
    """The contract in canonical form (only the known fields, unique ids), or `ContractInvalid`."""
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
            raw = {**raw, "columns": [raw["column"]]}  # short form
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


# ── reading ──────────────────────────────────────────────────────────────────
def get(session: Session, datasource_id: int) -> DataContract | None:
    return session.exec(select(DataContract).where(DataContract.datasource_id == datasource_id)).first()


def active_document(session: Session, datasource_id: int | None) -> dict | None:
    """The contract to have evaluated on the incoming data: None when there is none or it is off."""
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
    """What the icon must say. A refused update weighs more than the last report: that data
    holds the contract, but it is no longer the current data."""
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
    """The contract state for a list of datasources, with ONE query. Whoever has no contract
    turned on does not show up."""
    ids = [i for i in datasource_ids if i is not None]
    if not ids:
        return {}
    rows = session.exec(
        select(DataContract).where(DataContract.datasource_id.in_(ids), DataContract.enabled == True)  # noqa: E712
    ).all()
    return {c.datasource_id: summary(c) for c in rows}


# ── writing ──────────────────────────────────────────────────────────────────
def save(
    session: Session, ds: Datasource, document: dict, enabled: bool, user_id: int | None,
    *, notify: tuple[str | None, int | None] | None = None,
) -> DataContract:
    """Creates or replaces the contract (document validated already). A changed document is
    a new version, and the state goes back to «to check»: the report that was there spoke
    of another contract. `notify` = (addresses, SMTP connection) checked already; None =
    as before. Does NOT commit.

    The datasource's row stays locked until the commit, and the contract is read again
    under it: two saves at once take the next versions in turn, instead of the same one
    and a 500 on the second."""
    now = datetime.now(timezone.utc)
    text = json.dumps(document, ensure_ascii=False, sort_keys=True)
    session.exec(select(Datasource.id).where(Datasource.id == ds.id).with_for_update())
    c = session.exec(select(DataContract).where(DataContract.datasource_id == ds.id)
                     .execution_options(populate_existing=True)).first()
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
    """Removes the contract. `keep_history`: the history of reports and versions stays (the
    contract was removed, not the datasource). Does NOT commit."""
    session.exec(delete(DataContract).where(DataContract.datasource_id == datasource_id))
    if not keep_history:
        session.exec(delete(DataContractVersion).where(DataContractVersion.datasource_id == datasource_id))
        session.exec(delete(DataContractResult).where(DataContractResult.datasource_id == datasource_id))


# The notices go out for what happens by itself (new data, time passing). Whoever saves
# the contract or checks it by hand is looking at the screen already.
NOTIFY_TRIGGERS = ("refresh", "publish", "freshness")


def signal(c: DataContract) -> str:
    """What a notice tells: the state, or «refused» when the last update was refused."""
    return "refused" if c.blocked_at is not None else c.status


def _worth_telling(before: str, after: str) -> bool:
    """It notifies at a CHANGE of state, not at every evaluation: a refresh refused every
    night sends one email, not one a night. And the first check that went well (from
    «pending») is not news."""
    return after != before and not (after == "passed" and before == "pending")


def record_result(
    session: Session, c: DataContract, report: dict, *, trigger: str, run_id: int | None = None,
    snapshot_key: str = "", blocked: bool = False,
) -> DataContractResult:
    """Keeps a report and updates the contract's state. Does NOT commit.

    `blocked`: the report is about data that was NOT published. The state on the current
    data does not change (it is still the data of before); it records that the last
    update was refused. A report on published data, instead, is the new state — and when
    it comes from a successful update it clears the mark of the previous refusal."""
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
    """The contract to send to the engine together with the data to write, with its version
    (it comes back in the report). None = nothing to evaluate."""
    doc = active_document(session, datasource_id)
    if doc is None:
        return None
    return {**doc, "version": get(session, datasource_id).version}


def broken_rules_message(report: dict) -> str:
    """The sentence that ends up in a refused run's error: which blocking rules did not hold.
    Short: the detail is in the report."""
    parts = []
    for x in report.get("rules", []):
        if x.get("passed") or x.get("severity") == "warning":
            continue
        where = x.get("column") or ", ".join(x.get("columns") or []) or x.get("name") or ""
        how_many = f" ({x['violations']} rows)" if x.get("violations") else ""
        parts.append(f"{x.get('kind')}{' on ' + where if where else ''}{how_many}")
    if not parts and report.get("error"):
        parts.append(f"contract could not be evaluated: {report['error']}")
    listing = "; ".join(parts[:6]) + (f"; and {len(parts) - 6} more" if len(parts) > 6 else "")
    return (
        f"Data contract violated: {listing}. The new data was NOT published: "
        "the datasource still serves the previous snapshot."
    )


# ── freshness: the only rule that breaks without anything arriving ───────────
def refresh_freshness(session: Session, c: DataContract, ds: Datasource, now: datetime | None = None) -> bool:
    """Recomputes the snapshot's freshness rules on the time that has passed. Returns True
    when the outcome changed (and then a new report was written). Does NOT commit. The
    freshness rules on a COLUMN look at the data: the engine evaluates those, and they
    are not touched here."""
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
    """Goes through the turned-on contracts that have a freshness rule and updates those
    whose outcome changed with time. The scheduler calls it; several processes may do it
    together (at most they write the same report twice). Returns how many states
    changed. Commits one contract at a time."""
    changed_ones = 0
    rows = session.exec(
        select(DataContract, Datasource)
        .where(DataContract.datasource_id == Datasource.id)
        .where(DataContract.enabled == True, DataContract.document.contains('"freshness"'))  # noqa: E712
    ).all()
    for c, ds in rows:
        try:
            if refresh_freshness(session, c, ds, now):
                changed_ones += 1
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("freshness of the contract of datasource %s not updated", ds.id)
    return changed_ones
