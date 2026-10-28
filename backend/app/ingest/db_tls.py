"""Encryption (TLS) of the database connections: one choice for every dialect.

A connection's `extra` carries it, as the gateway validated it:

- `tls`: **verify** (encrypted, the server's certificate and name verified), **trust**
  (encrypted, the certificate taken as it is: a self-signed one), **none** (plain text);
- `ca_cert`: with verify, the certificate (PEM) of a company's own authority, for a
  server whose certificate the public authorities did not sign. Public, not a secret.

A connection saved before the choice existed has no `tls`: it keeps what its driver did
before, the LEGACY mode, so that an upgrade breaks no connection:

- PostgreSQL (libpq) and MySQL/MariaDB (PyMySQL 1.2+): encrypted when the server offers
  it, the certificate not verified ("prefer");
- Oracle: plain TCP;
- ClickHouse: HTTPS with the certificate verified on the ports 8443 and 443, plain HTTP
  on the others;
- Trino: HTTPS with the certificate verified when there is a password (Trino's client
  refuses one over HTTP), plain HTTP without;
- SQL Server: verify, its default from the start.

The CA reaches each driver the way it can take it: in memory (an `ssl.SSLContext`) for
PyMySQL, python-oracledb and clickhouse-connect; as a file for libpq and Trino's client
(requests), which read only a path — a 0600 file in the worker's own temporary directory,
removed when the connection closes (see `ca_file`).
"""
from __future__ import annotations

import os
import ssl
import tempfile
from contextlib import contextmanager
from typing import Iterator, Optional

from app.ingest.converters import IngestError

TLS_MODES = ("verify", "trust", "none")
LEGACY = "legacy"

# the port of the encrypted protocol, where it is not the plain one's (ClickHouse's and
# Trino's HTTPS, Oracle's TCPS): an empty port with encryption means this one
TLS_PORTS = {"clickhouse": 8443, "trino": 8443, "oracle": 2484}


class DbTlsError(IngestError):
    """A TLS choice the engine cannot use (unknown, or a CA that is not a certificate)."""


def tls_mode(db_type: str, extra: Optional[dict]) -> str:
    """verify | trust | none, or legacy for a connection saved before the choice (SQL
    Server: verify, as it has always been)."""
    mode = (extra or {}).get("tls") or ""
    if not mode:
        return "verify" if db_type == "sqlserver" else LEGACY
    if mode not in TLS_MODES:
        raise DbTlsError(f"Unknown TLS option {mode!r} for the connection: verify, trust or none.")
    return mode


def ca_pem(extra: Optional[dict]) -> str:
    """The company authority's certificate, when the connection verifies (empty
    otherwise: with trust nothing is verified, with none nothing is encrypted)."""
    extra = extra or {}
    pem = extra.get("ca_cert") or ""
    if not isinstance(pem, str) or extra.get("tls") != "verify":
        return ""
    return pem.strip()


def encrypted_port(db_type: str, mode: str) -> Optional[int]:
    """The default port when the mode encrypts and the dialect has a port of its own for
    it; None = the dialect's usual default."""
    return TLS_PORTS.get(db_type) if mode in ("verify", "trust") else None


def ssl_context(mode: str, pem: str = "") -> ssl.SSLContext:
    """The context of the drivers that take one (PyMySQL, python-oracledb, urllib3):
    verify = the system's authorities, or ONLY the company's when given; trust =
    encrypted, nothing checked."""
    if mode == "trust":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        ctx = ssl.create_default_context(cadata=pem) if pem else ssl.create_default_context()
    except (ssl.SSLError, ValueError) as e:
        raise DbTlsError("The certificate authority of the connection cannot be read: paste it in PEM "
                         "(-----BEGIN CERTIFICATE-----).") from e
    # Python 3.13 checks the certificates against RFC 5280 strictly, which libpq and the
    # ODBC driver do not, and which many company authorities fail (PyMySQL drops it too):
    # the chain and the name are what verify means here
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


@contextmanager
def ca_file(pem: str) -> Iterator[Optional[str]]:
    """The PEM in a file, for the drivers that read an authority only from a path. In the
    worker's own temporary directory (removed with the process if it dies: see
    app/core/tempdirs.py), readable by its owner only, deleted on the way out. None when
    there is no PEM."""
    if not pem:
        yield None
        return
    ssl_context("verify", pem)  # a PEM the driver could not read: said clearly, before it
    fd, path = tempfile.mkstemp(prefix="db_ca_", suffix=".pem")
    try:
        with os.fdopen(fd, "w", encoding="ascii", errors="replace") as f:
            f.write(pem if pem.endswith("\n") else pem + "\n")
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
