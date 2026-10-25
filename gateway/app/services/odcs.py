"""The data contract in the open ODCS format (Open Data Contract Standard, Bitol).

Export only: Tabularia's document stays the source, this is what is handed to a
catalog or another tool. The standard's version: v3.0.2, the one declared in
`apiVersion` and against whose schema the export is validated.

What becomes what:
  column           → the property in the schema, with its `logicalType`
  not_null         → `required: true` (when blocking) + a `nullValues` check
  unique           → `unique: true` (one column, when blocking) + `duplicateCount`;
                     on several columns an SQL query
  accepted_values  → an SQL query counting the values outside the list
  range            → `logicalTypeOptions.minimum/maximum` + an SQL query
  pattern          → `logicalTypeOptions.pattern` + a check described in words
  row_count        → a `rowCount` check
  freshness        → `slaProperties` (latency, in hours) + a check in words
  expression       → an SQL query counting the rows where the condition is not true

EVERY rule also becomes a `quality` item with its severity and, in the
`customProperties`, the id and the kind it has in Tabularia: nothing is lost on the
way and whoever reads knows which rule each check comes from.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

ODCS_VERSION = "v3.0.2"

# what the contract promises → ODCS's logical type (3.0 does not tell date from
# date-time: the detail stays in the physicalType)
_LOGICAL = {"integer": "integer", "number": "number", "string": "string", "boolean": "boolean", "date": "date", "datetime": "date"}
_DIMENSION = {
    "column": "conformity", "not_null": "completeness", "unique": "uniqueness", "accepted_values": "conformity",
    "range": "conformity", "pattern": "conformity", "row_count": "completeness", "freshness": "timeliness",
    "expression": "consistency",
}


def _family(dtype: str | None) -> str | None:
    """A physical type's family (the same the engine uses: evaluator.family)."""
    d = dtype or ""
    if re.match(r"U?Int", d):
        return "integer"
    if re.match(r"(Float|Decimal)", d):
        return "number"
    if d == "Boolean":
        return "boolean"
    if d == "Date":
        return "date"
    if d.startswith("Datetime"):
        return "datetime"
    if re.match(r"(String|Utf8|Categorical|Enum)", d):
        return "string"
    return None


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(v: Any) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check(rule: dict, /, **fields: Any) -> dict:
    """A `quality` item: what the rule checks, with its severity.
    (`rule` is positional only: among ODCS's fields there is one called `rule`.)"""
    return {
        **fields,
        "dimension": _DIMENSION[rule["kind"]],
        "severity": rule["severity"],
        "customProperties": [
            {"property": "tabulariaRuleId", "value": rule.get("id", "")},
            {"property": "tabulariaRuleKind", "value": rule["kind"]},
        ],
    }


def _zero_rows(rule: dict, name: str, where: str, description: str) -> dict:
    return _check(rule, type="sql", name=name, description=description,
                  query=f"SELECT COUNT(*) FROM ${{table}} WHERE {where}", mustBe=0, unit="rows")


def to_odcs(*, datasource_id: int, name: str, description: str, columns: list[dict], column_descriptions: dict[str, str],
            document: dict, version: int, enabled: bool, created_at: datetime | None) -> dict:
    """The ODCS document (a dictionary, in the order it must be written)."""
    # the `column` rules first: they fix the logical type, on which depends what of the
    # others (bounds, format) may sit in the schema
    rules = sorted((r for r in document.get("rules", []) if isinstance(r, dict)), key=lambda r: r["kind"] != "column")

    props: dict[str, dict] = {}
    for c in columns:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        p: dict[str, Any] = {"name": c["name"]}
        fam = _family(c.get("dtype"))
        if fam:
            p["logicalType"] = _LOGICAL[fam]
        if c.get("dtype"):
            p["physicalType"] = c["dtype"]
        if column_descriptions.get(c["name"]):
            p["description"] = column_descriptions[c["name"]]
        props[c["name"]] = p

    def prop(col: str) -> dict:
        # a rule may name a column the datasource does not have (yet, or any more)
        return props.setdefault(col, {"name": col})

    table_quality: list[dict] = []
    sla: list[dict] = []
    for r in rules:
        kind, blocking, col = r["kind"], r["severity"] == "error", r.get("column")
        if kind == "column":
            p = prop(col)
            if r.get("dtype"):
                p["logicalType"] = _LOGICAL[r["dtype"]]
            promise = f"of type {r['dtype']}" if r.get("dtype") else "present"
            p.setdefault("quality", []).append(_check(r, type="text", name=f"{col} is {promise}",
                                                      description=f"The column {col} exists" + (f" and its type is {r['dtype']}." if r.get("dtype") else ".")))
        elif kind == "not_null":
            p = prop(col)
            if blocking:
                p["required"] = True
            p.setdefault("quality", []).append(_check(r, type="library", rule="nullValues", name=f"{col} is never null", mustBe=0, unit="rows"))
        elif kind == "unique":
            cols = r.get("columns") or []
            if len(cols) == 1:
                p = prop(cols[0])
                if blocking:
                    p["unique"] = True
                p.setdefault("quality", []).append(_check(r, type="library", rule="duplicateCount", name=f"{cols[0]} is unique", mustBe=0, unit="rows"))
            else:
                listing = ", ".join(_ident(c) for c in cols)
                table_quality.append(_check(
                    r, type="sql", name="unique combination of " + ", ".join(cols),
                    description="No two rows share the same values of " + ", ".join(cols) + " (nulls count as values).",
                    query=f"SELECT COUNT(*) FROM (SELECT {listing} FROM ${{table}} GROUP BY {listing} HAVING COUNT(*) > 1) AS duplicated",
                    mustBe=0, unit="rows",
                ))
        elif kind == "accepted_values":
            values = ", ".join(_literal(v) for v in r.get("values", []))
            prop(col).setdefault("quality", []).append(_zero_rows(
                r, f"{col} has only accepted values", f"{_ident(col)} IS NOT NULL AND {_ident(col)} NOT IN ({values})",
                "Every non-null value is one of: " + ", ".join(str(v) for v in r.get("values", [])) + "."))
        elif kind == "range":
            p, lo, hi = prop(col), r.get("min"), r.get("max")
            # the bounds sit in the schema too, where the logical type allows them
            numeric = p.get("logicalType") in ("integer", "number")
            if (numeric and all(v is None or _is_number(v) for v in (lo, hi))) or (
                    p.get("logicalType") == "date" and all(v is None or isinstance(v, str) for v in (lo, hi))):
                options = p.setdefault("logicalTypeOptions", {})
                if lo is not None:
                    options["minimum"] = lo
                if hi is not None:
                    options["maximum"] = hi
            outside = [f"{_ident(col)} < {_literal(lo)}"] if lo is not None else []
            outside += [f"{_ident(col)} > {_literal(hi)}"] if hi is not None else []
            limits = " and ".join(([f"at least {lo}"] if lo is not None else []) + ([f"at most {hi}"] if hi is not None else []))
            p.setdefault("quality", []).append(_zero_rows(r, f"{col} is in range", " OR ".join(outside), f"Every non-null value is {limits}."))
        elif kind == "pattern":
            p = prop(col)
            if p.get("logicalType") == "string":
                p.setdefault("logicalTypeOptions", {})["pattern"] = r["regex"]
            p.setdefault("quality", []).append(_check(
                r, type="text", name=f"{col} matches the pattern",
                description=f"Every non-null value matches the regular expression {r['regex']} in full."))
        elif kind == "row_count":
            lo, hi = r.get("min"), r.get("max")
            limits = {"mustBeBetween": [lo, hi]} if lo is not None and hi is not None else (
                {"mustBeGreaterOrEqualTo": lo} if lo is not None else {"mustBeLessOrEqualTo": hi})
            table_quality.append(_check(r, type="library", rule="rowCount", name="row count", **limits, unit="rows"))
        elif kind == "freshness":
            hours = r["max_age_hours"]
            item: dict[str, Any] = {"property": "latency", "value": hours, "unit": "h"}
            if col:
                item["element"] = f"{name}.{col}"
            sla.append(item)
            subject = f"the newest value of {col}" if col else "the last update of the data"
            table_quality.append(_check(r, type="text", name="freshness", description=f"At most {hours} hours have passed since {subject}."))
        elif kind == "expression":
            table_quality.append(_check(
                r, type="sql", name=r.get("name") or "condition",
                description="The condition is true on every row; a null result counts as a violation. SQL dialect: Polars SQL.",
                query=f"SELECT COUNT(*) FROM ${{table}} WHERE NOT COALESCE(({r['sql']}), FALSE)", mustBe=0, unit="rows",
            ))

    obj: dict[str, Any] = {"name": name, "logicalType": "object", "physicalType": "table"}
    if description:
        obj["description"] = description
    obj["properties"] = list(props.values())
    if table_quality:
        obj["quality"] = table_quality

    out: dict[str, Any] = {
        "apiVersion": ODCS_VERSION,
        "kind": "DataContract",
        "id": f"urn:tabularia:datasource:{datasource_id}",
        "name": name,
        "version": f"{version}.0.0",
        "status": "active" if enabled else "draft",
    }
    text = document.get("description") or description
    if text:
        out["description"] = {"purpose": text}
    out["schema"] = [obj]
    if sla:
        out["slaProperties"] = sla
    out["customProperties"] = [
        {"property": "tabulariaDatasourceId", "value": datasource_id},
        {"property": "tabulariaContractVersion", "value": version},
    ]
    if created_at is not None:
        when = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        out["contractCreatedTs"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


# ── YAML ─────────────────────────────────────────────────────────────────────
# The gateway carries no YAML library, and this document needs a tiny subset of one:
# block maps and lists, scalars. Every string goes out in double quotes with JSON's
# escapes, which YAML reads as they are: so no user text («yes», «1.0», «a: b», a line
# break) can change meaning.
_TO_ESCAPE = re.compile("[\u007f-\u009f  ﻿]")


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return _TO_ESCAPE.sub(lambda m: f"\\u{ord(m.group()):04x}", json.dumps(str(v), ensure_ascii=False))


def _is_scalar(v: Any) -> bool:
    return not isinstance(v, (dict, list))


def _lines(v: Any, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(v, dict):
        out: list[str] = []
        for k, x in v.items():
            if _is_scalar(x):
                out.append(f"{pad}{k}: {_scalar(x)}")
            elif not x:
                out.append(f"{pad}{k}: " + ("{}" if isinstance(x, dict) else "[]"))
            elif isinstance(x, list) and all(_is_scalar(i) for i in x):
                out.append(f"{pad}{k}: [" + ", ".join(_scalar(i) for i in x) + "]")
            else:
                out.append(f"{pad}{k}:")
                out += _lines(x, indent + 2)
        return out
    out = []
    for x in v:
        if _is_scalar(x):
            out.append(f"{pad}- {_scalar(x)}")
        else:
            body = _lines(x, indent + 2)
            out.append(f"{pad}- {body[0].lstrip()}")
            out += body[1:]
    return out


def dump_yaml(document: dict) -> str:
    return "\n".join(_lines(document, 0)) + "\n"
