"""The encryption (TLS) of the database connections, on the gateway's side.

Every database connection has one choice in its `extra` (JSON), which the engine applies
to its driver (backend/app/ingest/db_tls.py):

- `tls`: verify (encrypted, certificate and name verified) | trust (encrypted, the
  certificate not checked: a self-signed one) | none (plain text);
- `ca_cert`: with verify, the PEM certificate of a company's own authority. Public, not a
  secret (it is shown in the form, to observers too), but checked: a certificate, readable,
  never a private key pasted by mistake.

A connection without `tls` was saved before the choice existed and keeps what its driver
did (the engine's «legacy» mode): it is not rewritten here, so an upgrade changes nothing.
SQL Server has had the choice from the start, with verify as its default; it takes no
authority (Microsoft's ODBC driver reads only the system's store).
"""
from __future__ import annotations

import json
import ssl
from typing import Optional

DB_TYPES = ("postgresql", "mysql", "mariadb", "clickhouse", "trino", "oracle", "sqlserver")
TLS_MODES = ("verify", "trust", "none")
CA_MAX_BYTES = 64 * 1024
# the port of the encrypted protocol, where it is not the plain one's (engine: db_tls.TLS_PORTS)
TLS_PORTS = {"clickhouse": 8443, "trino": 8443, "oracle": 2484}


def _data(extra) -> dict:
    if isinstance(extra, dict):
        return extra
    try:
        data = json.loads(extra or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def option_error(db_type: str, data: dict) -> Optional[str]:
    """What is wrong with the encryption options of a database connection, or None."""
    if db_type not in DB_TYPES:
        return None
    tls = data.get("tls")
    if tls not in (None, "", *TLS_MODES):
        return "The TLS option must be verify, trust or none"
    ca = data.get("ca_cert")
    if ca in (None, ""):
        return None
    if not isinstance(ca, str):
        return "The certificate authority must be text (PEM)"
    if db_type == "sqlserver":
        return ("SQL Server takes no certificate authority here: its driver reads the system's store, "
                "so add the authority to the backend image (docs/sqlserver.md)")
    if tls != "verify":
        return "The certificate authority is used only to verify: choose verify, or leave it empty"
    if len(ca.encode()) > CA_MAX_BYTES:
        return f"The certificate authority is too long (over {CA_MAX_BYTES // 1024} KiB)"
    if "PRIVATE KEY" in ca:
        return "That is a private key: paste only the authority's certificate (-----BEGIN CERTIFICATE-----)"
    if "-----BEGIN CERTIFICATE-----" not in ca:
        return "The certificate authority cannot be read: paste it in PEM (-----BEGIN CERTIFICATE-----)"
    try:
        ssl.create_default_context(cadata=ca)
    except (ssl.SSLError, ValueError):
        return "The certificate authority cannot be read: paste it in PEM (-----BEGIN CERTIFICATE-----)"
    return None


def mode(db_type: str, extra) -> str:
    """verify | trust | none, or "" for a connection saved before the choice (legacy).
    SQL Server: verify when unset, as always."""
    tls = _data(extra).get("tls")
    if tls in TLS_MODES:
        return tls
    return "verify" if db_type == "sqlserver" else ""


def engine_options(db_type: str, extra) -> dict:
    """What the engine needs to connect: the choice (absent = legacy) and, with verify, the
    company's authority."""
    if db_type not in DB_TYPES:
        return {}
    data = _data(extra)
    tls = mode(db_type, data)
    out: dict = {"tls": tls} if tls else {}
    ca = data.get("ca_cert")
    if tls == "verify" and db_type != "sqlserver" and isinstance(ca, str) and ca.strip():
        out["ca_cert"] = ca.strip()
    return out


def default_port(db_type: str, extra, plain: Optional[int]) -> Optional[int]:
    """The port an empty port means: the encrypted protocol's (ClickHouse's and Trino's
    HTTPS, Oracle's TCPS) when the connection encrypts, otherwise `plain`."""
    if mode(db_type, extra) in ("verify", "trust") and db_type in TLS_PORTS:
        return TLS_PORTS[db_type]
    return plain
