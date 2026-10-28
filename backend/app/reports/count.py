"""How many rows a report's dataset gives, counted up to the cap and no further: the
designer says it next to the dataset («Refresh fields»), so that a `SELECT *` without a
GROUP BY shows before the first preview reads it (docs/specs/reports.md, «Datasets»).

The dataset as the gateway resolved it, the same three shapes a render reads
(app/reports/data.py): a chain on the catalog, counted by its engine; a query on a
connection, counted by the database; rows given as they are. No row is read into memory.
"""
from __future__ import annotations

from typing import Any

from app.reports.errors import ReportError


def max_rows() -> int:
    """REPORTS__MAX_ROWS: the most rows a render reads from one dataset."""
    from app.core.config import get_settings

    return get_settings().reports.max_rows


def count(spec: Any, cap: int) -> dict:
    """{"rows", "over", "max_rows"}: `rows` at most `cap`, `over` when the dataset has
    more (a render would stop on it)."""
    from app.engine.exceptions import EngineError, SourceNotFoundError
    from app.ingest.converters import IngestError

    if not isinstance(spec, dict):
        raise ReportError("the dataset was not sent: it cannot be counted", "bad_request")
    try:
        if "rows" in spec:
            n = len(spec.get("rows") or [])
        elif "connection" in spec:
            from app.ingest.db_source import DbConnectionSpec, count_rows

            n = count_rows(DbConnectionSpec(**spec["connection"]), spec.get("sql") or "", cap)
        elif "input_key" in spec:
            from app.engine import DEFAULT_ENGINE, DataSource, get_engine

            engine = get_engine((spec.get("engine") or DEFAULT_ENGINE).lower())
            n = engine.count_rows(DataSource(bucket=spec["bucket"], key=spec["input_key"]),
                                  spec.get("operations") or [], cap)
        else:
            raise ReportError("the gateway sent nothing the engine knows how to count", "bad_request")
    except SourceNotFoundError as e:
        raise ReportError(str(e), "not_found") from None
    except (EngineError, IngestError) as e:
        raise ReportError(str(e)) from None
    return {"rows": min(n, cap), "over": n > cap, "max_rows": cap}
