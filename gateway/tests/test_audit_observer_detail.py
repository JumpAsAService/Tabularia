"""What an observer reads in the audit log's `detail` (review 2026-10-14, sec-authz-2).

The observer (on a public demo, the shared guest) saw actors masked, addresses and
browsers removed, and token prefixes replaced; but the rest of `detail` came through
whole: database and webhook hosts, the SIEM endpoint, SSO groups, emails. Those go
too; what was done (counts, formats, engines, ids) stays."""
import json

from app.models import AuditLog
from app.routes.audit import _to_out


def _out(detail: dict, pieno: bool = False):
    e = AuditLog(id=1, action="connection.create", actor_label="anna@x.it", detail=json.dumps(detail),
                 ip="203.0.113.7", user_agent="Firefox")
    return _to_out(e, pieno=pieno).detail


def test_hosts_and_endpoints_are_hidden():
    d = _out({"host": "pg-finance.internal:6432", "db_type": "postgres", "project_id": 4})
    assert d == {"host": "…", "db_type": "postgres", "project_id": 4}


def test_nested_changes_are_hidden_too():
    d = _out({"changes": {"url": {"from": "https://splunk.corp:8088", "to": "https://new.corp:8088"},
                          "enabled": {"from": False, "to": True}}})
    assert d["changes"]["url"] == "…" and d["changes"]["enabled"] == {"from": False, "to": True}


def test_groups_and_emails_are_hidden():
    d = _out({"groups_added": ["finance-admins"], "members": ["mario@cliente.com", "luca@x.it"], "recipients": 3})
    assert d["groups_added"] == "…"
    assert all("@" in m and not m.startswith(("mario@", "luca@")) for m in d["members"])
    assert d["recipients"] == 3


def test_a_token_prefix_is_still_hidden():
    assert _out({"prefix": "tab_ab12cd34", "scope": "read"}) == {"prefix": "tab_…", "scope": "read"}


def test_an_administrator_sees_everything():
    d = _out({"host": "pg-finance.internal:6432", "members": ["mario@cliente.com"]}, pieno=True)
    assert d == {"host": "pg-finance.internal:6432", "members": ["mario@cliente.com"]}
