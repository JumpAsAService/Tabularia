"""Golden cases for the Go port: overrides.check, contracts.validate_document, odcs.

Run in the gateway image from gateway/ (python gen_golden.py); the output is split into
internal/settings/testdata/check_golden.json and internal/contracts/testdata/golden.json."""
import json, sys
from datetime import datetime
from app.core import overrides as ov
from app.core.config import settings_from_env
from app.services import contracts, odcs

env = settings_from_env()
check_cases = [
    ({"auth.session_idle_minutes": 30}, []),
    ({"auth.session_idle_minutes": "30", "auth.personal_tokens": 1, "ai.max_cost_per_turn_usd": True, "ai.default_model": 5}, []),
    ({"auth.session_idle_minutes": 1441, "jwt.access_ttl_minutes": 14, "webhooks.timeout_seconds": 0.5}, []),
    ({"auth.api_token_max_days": 30.0}, []),
    ({"app.timezone": "Mars/Base"}, []), ({"app.timezone": "  "}, []), ({"app.timezone": " Europe/Rome "}, []),
    ({"ai.default_model": "m" * 201}, []), ({"ai.default_model": "  gpt  "}, []),
    ({"orchestrator.heartbeat_seconds": 30}, ["orchestrator.heartbeat_seconds"]),
    ({"orchestrator.heartbeat_seconds": 30, "orchestrator.dead_after_seconds": 60}, ["orchestrator.dead_after_seconds"]),
    ({"orchestrator.max_concurrent": 200, "db.pool_size": 5}, ["orchestrator.max_concurrent", "db.pool_size"]),
    ({"orchestrator.max_concurrent": 200, "db.pool_size": 5}, []),
    ({"orchestrator.max_concurrent": 60}, []),
    ({"oidc.require_allowlisted_group": True}, []),
    ({"oidc.require_allowlisted_group": True, "oidc.group_allowlist": " a "}, []),
    ({"notebooks.max_visitors_per_app": 60}, ["notebooks.max_visitors_per_app"]),
    ({"notebooks.max_visitors_ceiling": 5}, []),
    ({"ai.max_cost_per_turn_usd": 2, "upload.max_bytes": 10**20, "upload.max_bytes2": 1}, []),
    ({"ai.max_cost_per_turn_usd": 10**400}, []), ({"ai.max_cost_per_turn_usd": -1}, []),
    ({"orchestrator.poll_interval_seconds": 0.4}, []), ({"orchestrator.poll_interval_seconds": 61}, []),
    ({"monitoring.timeout_seconds": 0.5, "monitoring.cache_seconds": 0}, []),
    ({"scripts.build_cpu": "0"}, []), ({"scripts.build_cpu": "5n"}, []), ({"scripts.build_cpu": "abc"}, []),
    ({"scripts.build_cpu": "1500m"}, []), ({"scripts.build_memory": "1k"}, []), ({"scripts.build_memory": "1e3"}, []),
    ({"scripts.build_memory": "4Gi"}, []), ({"scripts.build_memory": "4gb"}, []), ({"containers.max_cpu": "5n"}, []),
    ({"containers.max_memory": "100Mi"}, []), ({"containers.max_memory": "x"}, []), ({"oidc.groups_claim": " "}, []),
    ({"oidc.button_label": "x" * 61}, []), ({"db.pool_size": None}, []), ({"x.y": 1}, []),
    ({"app.gateway_threads": 4, "db.max_overflow": 0, "orchestrator.max_concurrent": 5}, ["app.gateway_threads"]),
]
out_check = []
for values, changed in check_cases:
    good, errs = ov.check(values, env, set(changed))
    out_check.append({"values": json.dumps(values), "changed": changed, "good": json.dumps(good), "errors": json.dumps(errs)})

docs = [
    {"rules": []}, {"rules": "x"}, {"rules": [{}] * 201}, {"description": "d" * 2100, "rules": []}, {"description": 5},
    {"rules": [{"kind": "column", "severity": "error", "column": "a", "dtype": "integer", "id": "keep"},
               {"kind": "column", "severity": "error", "column": "a", "id": "keep"},
               {"kind": "not_null", "severity": "warning", "column": " ", "id": "x"},
               {"kind": "nope", "severity": "error"}, 5, {"kind": "unique", "severity": "fatal", "columns": ["a"]}]},
    {"rules": [{"kind": "unique", "severity": "error", "column": "a"}, {"kind": "unique", "severity": "error", "columns": ["a", ""]},
               {"kind": "accepted_values", "severity": "error", "column": "c", "values": [1, "x", True, 2.5]},
               {"kind": "accepted_values", "severity": "error", "column": "c", "values": []},
               {"kind": "accepted_values", "severity": "error", "column": "c", "values": [None]},
               {"kind": "accepted_values", "severity": "error", "column": "c", "values": list(range(501))}]},
    {"rules": [{"kind": "range", "severity": "error", "column": "a", "min": 5, "max": 1},
               {"kind": "range", "severity": "error", "column": "a", "min": "2020-01-01"},
               {"kind": "range", "severity": "error", "column": "a", "min": True},
               {"kind": "range", "severity": "error", "column": "a", "min": "", "max": None},
               {"kind": "row_count", "severity": "error", "min": -1}, {"kind": "row_count", "severity": "error", "min": 1.5},
               {"kind": "row_count", "severity": "warning", "min": 1, "max": 10, "id": "r1"},
               {"kind": "range", "severity": "warning", "column": "a", "min": 1, "max": 2.5}]},
    {"rules": [{"kind": "pattern", "severity": "error", "column": "a", "regex": "^[A-Z]+$"},
               {"kind": "pattern", "severity": "error", "column": "a", "regex": "("},
               {"kind": "pattern", "severity": "error", "column": "a", "regex": "(?<=x)y"},
               {"kind": "pattern", "severity": "error", "column": "a", "regex": "x" * 501},
               {"kind": "freshness", "severity": "error", "max_age_hours": 0},
               {"kind": "freshness", "severity": "error", "max_age_hours": 24, "column": "t"},
               {"kind": "freshness", "severity": "error", "max_age_hours": 1.5, "column": 3},
               {"kind": "expression", "severity": "error", "sql": "  "},
               {"kind": "expression", "severity": "error", "sql": "a > 0", "name": "positive", "extra": 1}]},
    {"rules": [{"kind": "expression", "severity": "error", "sql": "a > 0", "id": "r2"},
               {"kind": "expression", "severity": "error", "sql": "b > 0"},
               {"kind": "expression", "severity": "error", "sql": "c > 0", "id": "r2"},
               {"kind": "expression", "severity": "error", "sql": "d > 0", "id": "x" * 41}]},
]
out_docs = []
for doc in docs:
    try:
        good = contracts.validate_document(doc)
        out_docs.append({"doc": json.dumps(doc), "ok": json.dumps(good, ensure_ascii=False, sort_keys=True)})
    except contracts.ContractInvalid as e:
        out_docs.append({"doc": json.dumps(doc), "errors": json.dumps(e.errors)})

odcs_doc = contracts.validate_document({"description": "Sales «net»", "rules": [
    {"kind": "not_null", "severity": "error", "column": "id"},
    {"kind": "column", "severity": "error", "column": "id", "dtype": "integer"},
    {"kind": "unique", "severity": "error", "columns": ["id"]},
    {"kind": "unique", "severity": "warning", "columns": ["a", 'b"c']},
    {"kind": "accepted_values", "severity": "error", "column": "state", "values": ["it's", 1, 2.5, True]},
    {"kind": "range", "severity": "error", "column": "id", "min": 1, "max": 1e20},
    {"kind": "range", "severity": "error", "column": "day", "min": "2020-01-01"},
    {"kind": "range", "severity": "error", "column": "new", "max": 3},
    {"kind": "pattern", "severity": "warning", "column": "code", "regex": "^[A-Z]{2}\\d+$"},
    {"kind": "row_count", "severity": "error", "min": 1}, {"kind": "row_count", "severity": "error", "max": 9},
    {"kind": "row_count", "severity": "error", "min": 1, "max": 9},
    {"kind": "freshness", "severity": "warning", "max_age_hours": 1.5, "column": "day"},
    {"kind": "freshness", "severity": "warning", "max_age_hours": 24},
    {"kind": "expression", "severity": "error", "sql": "id > 0"},
    {"kind": "column", "severity": "warning", "column": "line sep\u0085x"},
]})
columns = [{"name": "id", "dtype": "Int64"}, {"name": "code", "dtype": "String"}, {"name": "day", "dtype": "Date"},
           {"name": "state", "dtype": "Categorical"}, {"name": "", "dtype": "Int8"}, "x", {"name": "f", "dtype": "Float64"},
           {"name": "t", "dtype": "Datetime(time_unit='us', time_zone=None)"}, {"name": "b", "dtype": "Boolean"},
           {"name": "o", "dtype": "Object"}, {"name": "n"}]
yaml = odcs.dump_yaml(odcs.to_odcs(datasource_id=7, name="Sales", description="the datasource", columns=columns,
                                   column_descriptions={"id": "the key", "code": ""}, document=odcs_doc, version=3,
                                   enabled=False, created_at=datetime(2026, 1, 2, 3, 4, 5, 678)))
yaml2 = odcs.dump_yaml(odcs.to_odcs(datasource_id=8, name="Empty", description="", columns=[], column_descriptions={},
                                    document={"rules": []}, version=1, enabled=True, created_at=None))
json.dump({"check": out_check, "documents": out_docs,
           "odcs": [{"columns": json.dumps(columns), "document": json.dumps(odcs_doc), "yaml": yaml}, {"yaml": yaml2}]},
          sys.stdout, ensure_ascii=False, indent=1)
