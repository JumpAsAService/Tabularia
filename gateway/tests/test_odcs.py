"""The data contract's export in the open ODCS v3.0.2 format.

Here it is tested what each rule becomes and that the YAML written by hand lets no text
change its meaning. The validation against ODCS's official schema is in the live round
(`contratti.py`), which has the libraries.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.permission import Capability
from app.routes import contracts as routes
from app.services import contracts, odcs
from tests.conftest import make_datasource, make_permission, make_project, make_user

COLUMNS = [
    {"name": "id", "dtype": "Int64"}, {"name": "stato", "dtype": "String"}, {"name": "importo", "dtype": "Float64"},
    {"name": "creato", "dtype": "Datetime(time_unit='us', time_zone=None)"}, {"name": "giorno", "dtype": "Date"},
    {"name": "codice", "dtype": "String"}, {"name": "blob", "dtype": "Binary"},
]
RULES = [
    {"id": "r1", "kind": "column", "column": "id", "dtype": "integer", "severity": "error"},
    {"id": "r2", "kind": "not_null", "column": "id", "severity": "error"},
    {"id": "r3", "kind": "not_null", "column": "stato", "severity": "warning"},
    {"id": "r4", "kind": "unique", "columns": ["id"], "severity": "error"},
    {"id": "r5", "kind": "unique", "columns": ["stato", "giorno"], "severity": "warning"},
    {"id": "r6", "kind": "accepted_values", "column": "stato", "values": ["nuovo", "l'altro"], "severity": "error"},
    {"id": "r7", "kind": "range", "column": "importo", "min": 0, "max": 99.5, "severity": "warning"},
    {"id": "r8", "kind": "range", "column": "giorno", "min": "2020-01-01", "severity": "error"},
    {"id": "r9", "kind": "pattern", "column": "codice", "regex": r"^[A-Z]{3}-\d+$", "severity": "warning"},
    {"id": "r10", "kind": "row_count", "min": 1, "max": 1000, "severity": "error"},
    {"id": "r11", "kind": "freshness", "max_age_hours": 26, "severity": "warning"},
    {"id": "r12", "kind": "freshness", "max_age_hours": 48, "column": "creato", "severity": "error"},
    {"id": "r13", "kind": "expression", "name": "date in ordine", "sql": "creato >= giorno", "severity": "error"},
    {"id": "r14", "kind": "column", "column": "sparita", "severity": "warning"},
]


def _doc(rules=RULES, **other):
    return odcs.to_odcs(**{
        "datasource_id": 7, "name": "ordini", "description": "Ordini evasi", "columns": COLUMNS,
        "column_descriptions": {"id": "chiave"}, "document": {"rules": rules}, "version": 3, "enabled": True,
        "created_at": datetime(2026, 10, 5, 8, 30), **other,
    })


def test_the_header_is_the_one_the_standard_requires():
    d = _doc()
    assert (d["apiVersion"], d["kind"], d["id"], d["version"], d["status"]) == ("v3.0.2", "DataContract", "urn:tabularia:datasource:7", "3.0.0", "active")
    assert d["description"] == {"purpose": "Ordini evasi"} and d["contractCreatedTs"] == "2026-10-05T08:30:00Z"
    assert _doc(enabled=False)["status"] == "draft"
    assert "description" not in _doc(description="")


def test_the_columns_become_properties_with_logical_and_physical_type():
    (obj,) = _doc()["schema"]
    p = {x["name"]: x for x in obj["properties"]}
    assert (obj["name"], obj["logicalType"], obj["physicalType"]) == ("ordini", "object", "table")
    assert list(p) == ["id", "stato", "importo", "creato", "giorno", "codice", "blob", "sparita"]   # the table's order, then the columns only promised
    assert (p["id"]["logicalType"], p["id"]["physicalType"], p["id"]["description"]) == ("integer", "Int64", "chiave")
    assert p["creato"]["logicalType"] == "date" and p["creato"]["physicalType"].startswith("Datetime")   # 3.0 has no «timestamp»
    assert "logicalType" not in p["blob"] and "physicalType" not in p["sparita"]


def test_only_blocking_rules_become_schema_constraints():
    p = {x["name"]: x for x in _doc()["schema"][0]["properties"]}
    assert p["id"]["required"] is True and p["id"]["unique"] is True
    assert "required" not in p["stato"]          # not_null as a warning: it stays a check, not a constraint
    assert p["importo"]["logicalTypeOptions"] == {"minimum": 0, "maximum": 99.5}
    assert p["giorno"]["logicalTypeOptions"] == {"minimum": "2020-01-01"}
    assert p["codice"]["logicalTypeOptions"] == {"pattern": r"^[A-Z]{3}-\d+$"}


def test_every_rule_is_a_check_with_its_severity_and_origin():
    d = _doc()
    checks = [q for p in d["schema"][0]["properties"] for q in p.get("quality", [])] + d["schema"][0]["quality"]
    origin = {next(c["value"] for c in q["customProperties"] if c["property"] == "tabulariaRuleId"): q for q in checks}
    assert sorted(origin, key=lambda i: int(i[1:])) == [r["id"] for r in RULES]
    assert all(origin[r["id"]]["severity"] == r["severity"] for r in RULES)
    assert all(q["type"] in ("library", "sql", "text") and q["name"] for q in checks)
    assert (origin["r2"]["rule"], origin["r2"]["mustBe"]) == ("nullValues", 0)
    assert (origin["r4"]["rule"], origin["r4"]["mustBe"]) == ("duplicateCount", 0)
    assert origin["r10"]["rule"] == "rowCount" and origin["r10"]["mustBeBetween"] == [1, 1000]
    assert 'GROUP BY "stato", "giorno" HAVING COUNT(*) > 1' in origin["r5"]["query"]
    assert origin["r6"]["query"].endswith("""WHERE "stato" IS NOT NULL AND "stato" NOT IN ('nuovo', 'l''altro')""")
    assert origin["r7"]["query"].endswith('WHERE "importo" < 0 OR "importo" > 99.5')
    assert origin["r8"]["query"].endswith("""WHERE "giorno" < '2020-01-01'""")
    assert origin["r13"]["query"] == "SELECT COUNT(*) FROM ${table} WHERE NOT COALESCE((creato >= giorno), FALSE)"
    assert origin["r9"]["type"] == "text" and r"^[A-Z]{3}-\d+$" in origin["r9"]["description"]


def test_the_rules_order_does_not_change_the_schema():
    # the promised type arrives AFTER the range: the numeric bounds must not end up on a text column
    rules = [
        {"id": "r1", "kind": "range", "column": "importo", "min": 0, "severity": "error"},
        {"id": "r2", "kind": "pattern", "column": "id", "regex": "x", "severity": "error"},
        {"id": "r3", "kind": "column", "column": "importo", "dtype": "string", "severity": "error"},
        {"id": "r4", "kind": "column", "column": "id", "dtype": "string", "severity": "error"},
    ]
    p = {x["name"]: x for x in _doc(rules)["schema"][0]["properties"]}
    assert p["importo"]["logicalType"] == "string" and "logicalTypeOptions" not in p["importo"]
    assert p["id"]["logicalTypeOptions"] == {"pattern": "x"}


def test_freshness_is_a_service_level_too():
    assert _doc()["slaProperties"] == [
        {"property": "latency", "value": 26, "unit": "h"},
        {"property": "latency", "value": 48, "unit": "h", "element": "ordini.creato"},
    ]
    assert "slaProperties" not in _doc(RULES[:3])


def test_one_bound_only_of_the_row_count():
    rule = lambda **k: _doc([{"id": "r1", "kind": "row_count", "severity": "error", **k}])["schema"][0]["quality"][0]
    assert rule(min=5)["mustBeGreaterOrEqualTo"] == 5 and "mustBeBetween" not in rule(min=5)
    assert rule(max=9)["mustBeLessOrEqualTo"] == 9


def test_the_yaml_does_not_let_text_change_its_meaning():
    text = odcs.dump_yaml({
        "name": "yes", "note": "a: b # c", "riga": "uno\ndue", "virgolette": 'dice "ciao"', "numero": "1.0",
        "vero": True, "nullo": None, "conta": 3, "soglia": 0.5, "vuota": [], "coppia": [1, 2.5],
        "strano": "fine riga", "elenco": [{"a": "x", "dentro": [{"b": "y"}], "piatta": ["p", "q"]}, {"a": "z"}],
        "mappa": {"k": "v"},
    })
    assert text == (
        'name: "yes"\n'
        'note: "a: b # c"\n'
        'riga: "uno\\ndue"\n'
        'virgolette: "dice \\"ciao\\""\n'
        'numero: "1.0"\n'
        "vero: true\n"
        "nullo: null\n"
        "conta: 3\n"
        "soglia: 0.5\n"
        "vuota: []\n"
        "coppia: [1, 2.5]\n"
        'strano: "fine\\u2028riga"\n'
        "elenco:\n"
        '  - a: "x"\n'
        "    dentro:\n"
        '      - b: "y"\n'
        '    piatta: ["p", "q"]\n'
        '  - a: "z"\n'
        "mappa:\n"
        '  k: "v"\n'
    )


# ── the route ────────────────────────────────────────────────────────────────
@pytest.fixture
def scene(session):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    folder = make_project(session, name="dati", owner_id=boss.id)
    reader, stranger = make_user(session, email="lettore@x.it"), make_user(session, email="fuori@x.it")
    make_permission(session, user_id=reader.id, project_id=folder.id, capability=Capability.VIEW)
    ds = make_datasource(session, name="Ordini 2026 / nord", project_id=folder.id, key="datasets/1/v1.parquet", rows=3)
    return SimpleNamespace(ds=ds, boss=boss, reader=reader, stranger=stranger)


def test_whoever_reads_the_datasource_downloads_the_contract_in_odcs(session, scene):
    contracts.save(session, scene.ds, contracts.validate_document({"rules": [{"kind": "row_count", "min": 1, "severity": "error"}]}), True, scene.boss.id)
    session.commit()
    answer = routes.export_odcs(scene.ds.id, scene.reader, session)
    assert answer.media_type == "application/yaml"
    assert answer.headers["content-disposition"] == 'attachment; filename="Ordini_2026_nord.odcs.yaml"'
    text = answer.body.decode()
    assert text.startswith('apiVersion: "v3.0.2"\nkind: "DataContract"\n') and 'rule: "rowCount"' in text
    with pytest.raises(HTTPException) as e:
        routes.export_odcs(scene.ds.id, scene.stranger, session)
    assert e.value.status_code == 403


def test_without_contract_there_is_nothing_to_export(session, scene):
    with pytest.raises(HTTPException) as e:
        routes.export_odcs(scene.ds.id, scene.boss, session)
    assert e.value.status_code == 404
