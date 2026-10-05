"""Il data contract nel formato aperto ODCS (Open Data Contract Standard, Bitol).

Solo esportazione: il documento di Tabularia resta la fonte, questo è ciò che si
consegna a un catalogo o a un altro strumento. Versione dello standard: v3.0.2,
quella dichiarata in `apiVersion` e contro il cui schema l'export è validato.

Che cosa diventa cosa:
  column           → la proprietà nello schema, con il suo `logicalType`
  not_null         → `required: true` (se bloccante) + controllo `nullValues`
  unique           → `unique: true` (una colonna, se bloccante) + `duplicateCount`;
                     su più colonne una query SQL
  accepted_values  → una query SQL che conta i valori fuori elenco
  range            → `logicalTypeOptions.minimum/maximum` + una query SQL
  pattern          → `logicalTypeOptions.pattern` + un controllo descritto a parole
  row_count        → controllo `rowCount`
  freshness        → `slaProperties` (latency, in ore) + un controllo a parole
  expression       → una query SQL che conta le righe dove la condizione non è vera

OGNI regola diventa anche una voce di `quality` con la sua severità e, nelle
`customProperties`, l'id e il tipo che ha in Tabularia: niente si perde per strada
e chi legge sa da quale regola nasce ogni controllo.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

ODCS_VERSION = "v3.0.2"

# ciò che il contratto promette → il tipo logico di ODCS (la 3.0 non distingue
# data e data-ora: il dettaglio resta nel physicalType)
_LOGICAL = {"integer": "integer", "number": "number", "string": "string", "boolean": "boolean", "date": "date", "datetime": "date"}
_DIMENSION = {
    "column": "conformity", "not_null": "completeness", "unique": "uniqueness", "accepted_values": "conformity",
    "range": "conformity", "pattern": "conformity", "row_count": "completeness", "freshness": "timeliness",
    "expression": "consistency",
}


def _family(dtype: str | None) -> str | None:
    """La famiglia di un tipo fisico (la stessa che usa l'engine: evaluator.family)."""
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


def _check(regola: dict, /, **fields: Any) -> dict:
    """Una voce di `quality`: ciò che la regola verifica, con la sua severità.
    (`regola` è solo posizionale: fra i campi di ODCS ce n'è uno che si chiama `rule`.)"""
    return {
        **fields,
        "dimension": _DIMENSION[regola["kind"]],
        "severity": regola["severity"],
        "customProperties": [
            {"property": "tabulariaRuleId", "value": regola.get("id", "")},
            {"property": "tabulariaRuleKind", "value": regola["kind"]},
        ],
    }


def _zero_rows(regola: dict, name: str, where: str, description: str) -> dict:
    return _check(regola, type="sql", name=name, description=description,
                  query=f"SELECT COUNT(*) FROM ${{table}} WHERE {where}", mustBe=0, unit="rows")


def to_odcs(*, datasource_id: int, name: str, description: str, columns: list[dict], column_descriptions: dict[str, str],
            document: dict, version: int, enabled: bool, created_at: datetime | None) -> dict:
    """Il documento ODCS (un dizionario, nell'ordine in cui va scritto)."""
    # prima le regole `column`: fissano il tipo logico, da cui dipende che cosa
    # delle altre (estremi, formato) può stare nello schema
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
        # una regola può nominare una colonna che la datasource non ha (ancora, o più)
        return props.setdefault(col, {"name": col})

    table_quality: list[dict] = []
    sla: list[dict] = []
    for r in rules:
        kind, blocking, col = r["kind"], r["severity"] == "error", r.get("column")
        if kind == "column":
            p = prop(col)
            if r.get("dtype"):
                p["logicalType"] = _LOGICAL[r["dtype"]]
            promessa = f"of type {r['dtype']}" if r.get("dtype") else "present"
            p.setdefault("quality", []).append(_check(r, type="text", name=f"{col} is {promessa}",
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
                elenco = ", ".join(_ident(c) for c in cols)
                table_quality.append(_check(
                    r, type="sql", name="unique combination of " + ", ".join(cols),
                    description="No two rows share the same values of " + ", ".join(cols) + " (nulls count as values).",
                    query=f"SELECT COUNT(*) FROM (SELECT {elenco} FROM ${{table}} GROUP BY {elenco} HAVING COUNT(*) > 1) AS duplicated",
                    mustBe=0, unit="rows",
                ))
        elif kind == "accepted_values":
            valori = ", ".join(_literal(v) for v in r.get("values", []))
            prop(col).setdefault("quality", []).append(_zero_rows(
                r, f"{col} has only accepted values", f"{_ident(col)} IS NOT NULL AND {_ident(col)} NOT IN ({valori})",
                "Every non-null value is one of: " + ", ".join(str(v) for v in r.get("values", [])) + "."))
        elif kind == "range":
            p, lo, hi = prop(col), r.get("min"), r.get("max")
            # gli estremi stanno anche nello schema, dove il tipo logico li ammette
            numerico = p.get("logicalType") in ("integer", "number")
            if (numerico and all(v is None or _is_number(v) for v in (lo, hi))) or (
                    p.get("logicalType") == "date" and all(v is None or isinstance(v, str) for v in (lo, hi))):
                opzioni = p.setdefault("logicalTypeOptions", {})
                if lo is not None:
                    opzioni["minimum"] = lo
                if hi is not None:
                    opzioni["maximum"] = hi
            fuori = [f"{_ident(col)} < {_literal(lo)}"] if lo is not None else []
            fuori += [f"{_ident(col)} > {_literal(hi)}"] if hi is not None else []
            limiti = " and ".join(([f"at least {lo}"] if lo is not None else []) + ([f"at most {hi}"] if hi is not None else []))
            p.setdefault("quality", []).append(_zero_rows(r, f"{col} is in range", " OR ".join(fuori), f"Every non-null value is {limiti}."))
        elif kind == "pattern":
            p = prop(col)
            if p.get("logicalType") == "string":
                p.setdefault("logicalTypeOptions", {})["pattern"] = r["regex"]
            p.setdefault("quality", []).append(_check(
                r, type="text", name=f"{col} matches the pattern",
                description=f"Every non-null value matches the regular expression {r['regex']} in full."))
        elif kind == "row_count":
            lo, hi = r.get("min"), r.get("max")
            limiti = {"mustBeBetween": [lo, hi]} if lo is not None and hi is not None else (
                {"mustBeGreaterOrEqualTo": lo} if lo is not None else {"mustBeLessOrEqualTo": hi})
            table_quality.append(_check(r, type="library", rule="rowCount", name="row count", **limiti, unit="rows"))
        elif kind == "freshness":
            ore = r["max_age_hours"]
            voce: dict[str, Any] = {"property": "latency", "value": ore, "unit": "h"}
            if col:
                voce["element"] = f"{name}.{col}"
            sla.append(voce)
            su = f"the newest value of {col}" if col else "the last update of the data"
            table_quality.append(_check(r, type="text", name="freshness", description=f"At most {ore} hours have passed since {su}."))
        elif kind == "expression":
            table_quality.append(_check(
                r, type="sql", name=r.get("name") or "condition",
                description="The condition is true on every row; a null result counts as a violation. SQL dialect: Polars SQL.",
                query=f"SELECT COUNT(*) FROM ${{table}} WHERE NOT COALESCE(({r['sql']}), FALSE)", mustBe=0, unit="rows",
            ))

    oggetto: dict[str, Any] = {"name": name, "logicalType": "object", "physicalType": "table"}
    if description:
        oggetto["description"] = description
    oggetto["properties"] = list(props.values())
    if table_quality:
        oggetto["quality"] = table_quality

    out: dict[str, Any] = {
        "apiVersion": ODCS_VERSION,
        "kind": "DataContract",
        "id": f"urn:tabularia:datasource:{datasource_id}",
        "name": name,
        "version": f"{version}.0.0",
        "status": "active" if enabled else "draft",
    }
    testo = document.get("description") or description
    if testo:
        out["description"] = {"purpose": testo}
    out["schema"] = [oggetto]
    if sla:
        out["slaProperties"] = sla
    out["customProperties"] = [
        {"property": "tabulariaDatasourceId", "value": datasource_id},
        {"property": "tabulariaContractVersion", "value": version},
    ]
    if created_at is not None:
        quando = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        out["contractCreatedTs"] = quando.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


# ── YAML ─────────────────────────────────────────────────────────────────────
# Il gateway non porta una libreria YAML, e a questo documento ne serve un
# sottoinsieme minuscolo: mappe e liste a blocchi, scalari. Ogni stringa esce fra
# virgolette doppie con le sequenze di JSON, che YAML legge tali e quali: così
# nessun testo dell'utente («yes», «1.0», «a: b», un a capo) può cambiare significato.
_DA_SFUGGIRE = re.compile("[\u007f-\u009f  ﻿]")


def _scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return _DA_SFUGGIRE.sub(lambda m: f"\\u{ord(m.group()):04x}", json.dumps(str(v), ensure_ascii=False))


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
            corpo = _lines(x, indent + 2)
            out.append(f"{pad}- {corpo[0].lstrip()}")
            out += corpo[1:]
    return out


def dump_yaml(document: dict) -> str:
    return "\n".join(_lines(document, 0)) + "\n"
