"""L'export del data contract nel formato aperto ODCS v3.0.2.

Qui si prova che cosa diventa ogni regola e che lo YAML scritto a mano non si
lascia cambiare significato da nessun testo. La validazione contro lo schema
ufficiale di ODCS sta nel giro dal vivo (`contratti.py`), che ha le librerie.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.permission import Capability
from app.routes import contracts as routes
from app.services import contracts, odcs
from tests.conftest import make_datasource, make_permission, make_project, make_user

COLONNE = [
    {"name": "id", "dtype": "Int64"}, {"name": "stato", "dtype": "String"}, {"name": "importo", "dtype": "Float64"},
    {"name": "creato", "dtype": "Datetime(time_unit='us', time_zone=None)"}, {"name": "giorno", "dtype": "Date"},
    {"name": "codice", "dtype": "String"}, {"name": "blob", "dtype": "Binary"},
]
REGOLE = [
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


def _doc(regole=REGOLE, **altro):
    return odcs.to_odcs(**{
        "datasource_id": 7, "name": "ordini", "description": "Ordini evasi", "columns": COLONNE,
        "column_descriptions": {"id": "chiave"}, "document": {"rules": regole}, "version": 3, "enabled": True,
        "created_at": datetime(2026, 10, 5, 8, 30), **altro,
    })


def test_l_intestazione_e_quella_che_lo_standard_richiede():
    d = _doc()
    assert (d["apiVersion"], d["kind"], d["id"], d["version"], d["status"]) == ("v3.0.2", "DataContract", "urn:tabularia:datasource:7", "3.0.0", "active")
    assert d["description"] == {"purpose": "Ordini evasi"} and d["contractCreatedTs"] == "2026-10-05T08:30:00Z"
    assert _doc(enabled=False)["status"] == "draft"
    assert "description" not in _doc(description="")


def test_le_colonne_diventano_proprieta_con_tipo_logico_e_fisico():
    (oggetto,) = _doc()["schema"]
    p = {x["name"]: x for x in oggetto["properties"]}
    assert (oggetto["name"], oggetto["logicalType"], oggetto["physicalType"]) == ("ordini", "object", "table")
    assert list(p) == ["id", "stato", "importo", "creato", "giorno", "codice", "blob", "sparita"]   # l'ordine della tabella, poi le colonne solo promesse
    assert (p["id"]["logicalType"], p["id"]["physicalType"], p["id"]["description"]) == ("integer", "Int64", "chiave")
    assert p["creato"]["logicalType"] == "date" and p["creato"]["physicalType"].startswith("Datetime")   # la 3.0 non ha «timestamp»
    assert "logicalType" not in p["blob"] and "physicalType" not in p["sparita"]


def test_solo_le_regole_bloccanti_diventano_vincoli_dello_schema():
    p = {x["name"]: x for x in _doc()["schema"][0]["properties"]}
    assert p["id"]["required"] is True and p["id"]["unique"] is True
    assert "required" not in p["stato"]          # not_null come avviso: resta un controllo, non un vincolo
    assert p["importo"]["logicalTypeOptions"] == {"minimum": 0, "maximum": 99.5}
    assert p["giorno"]["logicalTypeOptions"] == {"minimum": "2020-01-01"}
    assert p["codice"]["logicalTypeOptions"] == {"pattern": r"^[A-Z]{3}-\d+$"}


def test_ogni_regola_e_un_controllo_con_la_sua_severita_e_la_sua_origine():
    d = _doc()
    controlli = [q for p in d["schema"][0]["properties"] for q in p.get("quality", [])] + d["schema"][0]["quality"]
    origine = {next(c["value"] for c in q["customProperties"] if c["property"] == "tabulariaRuleId"): q for q in controlli}
    assert sorted(origine, key=lambda i: int(i[1:])) == [r["id"] for r in REGOLE]
    assert all(origine[r["id"]]["severity"] == r["severity"] for r in REGOLE)
    assert all(q["type"] in ("library", "sql", "text") and q["name"] for q in controlli)
    assert (origine["r2"]["rule"], origine["r2"]["mustBe"]) == ("nullValues", 0)
    assert (origine["r4"]["rule"], origine["r4"]["mustBe"]) == ("duplicateCount", 0)
    assert origine["r10"]["rule"] == "rowCount" and origine["r10"]["mustBeBetween"] == [1, 1000]
    assert 'GROUP BY "stato", "giorno" HAVING COUNT(*) > 1' in origine["r5"]["query"]
    assert origine["r6"]["query"].endswith("""WHERE "stato" IS NOT NULL AND "stato" NOT IN ('nuovo', 'l''altro')""")
    assert origine["r7"]["query"].endswith('WHERE "importo" < 0 OR "importo" > 99.5')
    assert origine["r8"]["query"].endswith("""WHERE "giorno" < '2020-01-01'""")
    assert origine["r13"]["query"] == "SELECT COUNT(*) FROM ${table} WHERE NOT COALESCE((creato >= giorno), FALSE)"
    assert origine["r9"]["type"] == "text" and r"^[A-Z]{3}-\d+$" in origine["r9"]["description"]


def test_l_ordine_delle_regole_non_cambia_lo_schema():
    # il tipo promesso arriva DOPO l'intervallo: gli estremi numerici non devono finire su una colonna di testo
    regole = [
        {"id": "r1", "kind": "range", "column": "importo", "min": 0, "severity": "error"},
        {"id": "r2", "kind": "pattern", "column": "id", "regex": "x", "severity": "error"},
        {"id": "r3", "kind": "column", "column": "importo", "dtype": "string", "severity": "error"},
        {"id": "r4", "kind": "column", "column": "id", "dtype": "string", "severity": "error"},
    ]
    p = {x["name"]: x for x in _doc(regole)["schema"][0]["properties"]}
    assert p["importo"]["logicalType"] == "string" and "logicalTypeOptions" not in p["importo"]
    assert p["id"]["logicalTypeOptions"] == {"pattern": "x"}


def test_la_freschezza_e_anche_un_livello_di_servizio():
    assert _doc()["slaProperties"] == [
        {"property": "latency", "value": 26, "unit": "h"},
        {"property": "latency", "value": 48, "unit": "h", "element": "ordini.creato"},
    ]
    assert "slaProperties" not in _doc(REGOLE[:3])


def test_un_solo_estremo_del_numero_di_righe():
    regola = lambda **k: _doc([{"id": "r1", "kind": "row_count", "severity": "error", **k}])["schema"][0]["quality"][0]
    assert regola(min=5)["mustBeGreaterOrEqualTo"] == 5 and "mustBeBetween" not in regola(min=5)
    assert regola(max=9)["mustBeLessOrEqualTo"] == 9


def test_lo_yaml_non_si_lascia_cambiare_significato_dal_testo():
    testo = odcs.dump_yaml({
        "name": "yes", "note": "a: b # c", "riga": "uno\ndue", "virgolette": 'dice "ciao"', "numero": "1.0",
        "vero": True, "nullo": None, "conta": 3, "soglia": 0.5, "vuota": [], "coppia": [1, 2.5],
        "strano": "fine riga", "elenco": [{"a": "x", "dentro": [{"b": "y"}], "piatta": ["p", "q"]}, {"a": "z"}],
        "mappa": {"k": "v"},
    })
    assert testo == (
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


# ── la rotta ─────────────────────────────────────────────────────────────────
@pytest.fixture
def scena(session):
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    cartella = make_project(session, name="dati", owner_id=capo.id)
    lettore, estraneo = make_user(session, email="lettore@x.it"), make_user(session, email="fuori@x.it")
    make_permission(session, user_id=lettore.id, project_id=cartella.id, capability=Capability.VIEW)
    ds = make_datasource(session, name="Ordini 2026 / nord", project_id=cartella.id, key="datasets/1/v1.parquet", rows=3)
    return SimpleNamespace(ds=ds, capo=capo, lettore=lettore, estraneo=estraneo)


def test_chi_legge_la_datasource_scarica_il_contratto_in_odcs(session, scena):
    contracts.save(session, scena.ds, contracts.validate_document({"rules": [{"kind": "row_count", "min": 1, "severity": "error"}]}), True, scena.capo.id)
    session.commit()
    risposta = routes.export_odcs(scena.ds.id, scena.lettore, session)
    assert risposta.media_type == "application/yaml"
    assert risposta.headers["content-disposition"] == 'attachment; filename="Ordini_2026_nord.odcs.yaml"'
    testo = risposta.body.decode()
    assert testo.startswith('apiVersion: "v3.0.2"\nkind: "DataContract"\n') and 'rule: "rowCount"' in testo
    with pytest.raises(HTTPException) as e:
        routes.export_odcs(scena.ds.id, scena.estraneo, session)
    assert e.value.status_code == 403


def test_senza_contratto_non_c_e_niente_da_esportare(session, scena):
    with pytest.raises(HTTPException) as e:
        routes.export_odcs(scena.ds.id, scena.capo, session)
    assert e.value.status_code == 404
