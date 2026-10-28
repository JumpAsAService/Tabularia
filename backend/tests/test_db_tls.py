"""The encryption (TLS) of the database connections: what each driver is asked for.

One choice for every dialect, in the connection's `extra` (see app/ingest/db_tls.py):
verify (encrypted, certificate and name verified, a company authority optional), trust
(encrypted, the certificate not checked), none (plain). A connection saved before the
choice keeps what its driver did (legacy). Here the drivers are replaced by fakes that
record their arguments: the behaviour against real servers (PostgreSQL 18, MySQL 8.4,
ClickHouse 24.8 with a company authority, a TLS server for Oracle's TCPS and Trino's
HTTPS) was proved by hand on 2026-10-28, and the error texts it produced are reproduced
verbatim below. Against the CI's PostgreSQL (no TLS) the end of the file checks the
refusal and the plain modes for real.
"""
import os
import ssl
import sys
import types
from urllib.parse import unquote, urlparse

import pytest

from app.ingest import db_source as ds
from app.ingest import db_tls
from app.ingest.converters import IngestError
from app.ingest.db_errors import describe_db_error


def _ca() -> str:
    """A self-signed authority, made on the spot (cryptography is in the image)."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Company CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


CA = _ca()
BROKEN = "-----BEGIN CERTIFICATE-----\nnot a certificate\n-----END CERTIFICATE-----\n"


def _conn(db_type, tls=None, ca=None, **kw) -> ds.DbConnectionSpec:
    extra = {k: v for k, v in (("tls", tls), ("ca_cert", ca)) if v is not None}
    fields = {"host": "db.example.com", "username": "reader", "password": "s3cret", "database": "shop", **kw}
    return ds.DbConnectionSpec(db_type=db_type, extra=extra, **fields)


def _ca_files() -> list[str]:
    import glob
    import tempfile

    return glob.glob(os.path.join(tempfile.gettempdir(), "db_ca_*"))


# ── the choice ──────────────────────────────────────────────────────────────────
def test_a_connection_saved_before_the_choice_keeps_its_drivers_behaviour():
    assert _conn("postgresql").tls == "legacy"
    assert _conn("oracle").tls == "legacy"
    assert _conn("sqlserver").tls == "verify"   # SQL Server has verified from the start


def test_an_unknown_choice_is_refused_with_a_clear_error():
    with pytest.raises(IngestError, match="Unknown TLS option 'maybe'"):
        _ = _conn("mysql", "maybe").tls


def test_the_authority_counts_only_when_the_connection_verifies():
    assert _conn("postgresql", "verify", CA).ca_cert == CA.strip()
    assert _conn("postgresql", "trust", CA).ca_cert == ""
    assert _conn("postgresql", None, CA).ca_cert == ""


@pytest.mark.parametrize("db_type, tls, port", [
    ("clickhouse", "verify", 8443), ("clickhouse", "trust", 8443), ("clickhouse", "none", 8123), ("clickhouse", None, 8123),
    ("trino", "verify", 8443), ("trino", None, 8080), ("oracle", "verify", 2484), ("oracle", "none", 1521),
    ("postgresql", "verify", 5432), ("mysql", "trust", 3306), ("sqlserver", None, 1433),
])
def test_an_empty_port_is_the_one_of_the_protocol_spoken(db_type, tls, port):
    assert _conn(db_type, tls).port_or_default == port
    assert _conn(db_type, tls, port=9999).port_or_default == 9999


def test_the_context_verifies_or_trusts():
    v = db_tls.ssl_context("verify", CA)
    assert v.verify_mode == ssl.CERT_REQUIRED and v.check_hostname
    t = db_tls.ssl_context("trust")
    assert t.verify_mode == ssl.CERT_NONE and not t.check_hostname
    with pytest.raises(IngestError, match="cannot be read: paste it in PEM"):
        db_tls.ssl_context("verify", BROKEN)


def test_the_authoritys_file_lives_with_the_connection_only():
    with db_tls.ca_file(CA) as path:
        assert open(path).read().strip() == CA.strip()
        assert os.stat(path).st_mode & 0o077 == 0     # its owner's only
    assert not os.path.exists(path)
    with db_tls.ca_file("") as nothing:
        assert nothing is None


# ── PostgreSQL: libpq's sslmode ─────────────────────────────────────────────────
@pytest.fixture
def fake_adbc(monkeypatch):
    seen = {}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def connect(uri):
        query = dict(p.split("=", 1) for p in (urlparse(uri).query or "").split("&") if p)
        seen.update(uri=uri, query={k: unquote(v) for k, v in query.items()})
        root = seen["query"].get("sslrootcert", "")
        seen["root_at_connect"] = open(root).read() if os.path.isfile(root) else root
        return _Conn()

    dbapi = types.SimpleNamespace(connect=connect)
    pkg = types.ModuleType("adbc_driver_postgresql")
    pkg.dbapi = dbapi
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql", pkg)
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql.dbapi", dbapi)
    return seen


@pytest.mark.parametrize("tls, query", [
    ("verify", {"sslmode": "verify-full", "sslrootcert": "system"}),
    ("trust", {"sslmode": "require"}),
    ("none", {"sslmode": "disable"}),
    (None, {}),   # legacy: libpq's default, prefer
])
def test_postgresql_sslmode(fake_adbc, tls, query):
    with ds._pg_connect(_conn("postgresql", tls)):
        pass
    assert fake_adbc["query"] == query


def test_postgresql_the_company_authority_is_a_file_while_connecting(fake_adbc):
    before = _ca_files()
    with ds._pg_connect(_conn("postgresql", "verify", CA)):
        assert fake_adbc["query"]["sslmode"] == "verify-full"
        assert fake_adbc["root_at_connect"].strip() == CA.strip()
    assert _ca_files() == before


def test_postgresql_a_broken_authority_is_said_before_libpq(fake_adbc):
    with pytest.raises(IngestError, match="cannot be read"):
        with ds._pg_connect(_conn("postgresql", "verify", BROKEN)):
            pass
    assert "uri" not in fake_adbc


# ── MySQL / MariaDB: PyMySQL ────────────────────────────────────────────────────
@pytest.fixture
def fake_pymysql(monkeypatch):
    import pymysql

    seen = {}
    monkeypatch.setattr(pymysql, "connect", lambda **kw: seen.update(kw) or object())
    return seen


def test_mysql_verify_requires_encryption_and_checks_name_and_chain(fake_pymysql):
    ds._mysql_connect(_conn("mysql", "verify", CA))
    ctx = fake_pymysql["ssl"]
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert "ssl_disabled" not in fake_pymysql


def test_mysql_trust_none_and_legacy(fake_pymysql):
    ds._mysql_connect(_conn("mariadb", "trust"))
    assert fake_pymysql["ssl"].verify_mode == ssl.CERT_NONE
    fake_pymysql.clear()
    ds._mysql_connect(_conn("mysql", "none"))
    assert fake_pymysql["ssl_disabled"] is True and "ssl" not in fake_pymysql
    fake_pymysql.clear()
    ds._mysql_connect(_conn("mysql"))   # PyMySQL's own default: encrypted when offered
    assert "ssl" not in fake_pymysql and "ssl_disabled" not in fake_pymysql


# ── ClickHouse: clickhouse-connect over HTTP(S) ─────────────────────────────────
@pytest.fixture
def fake_clickhouse(monkeypatch):
    import clickhouse_connect

    seen = {}

    class _Client:
        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(clickhouse_connect, "get_client", lambda **kw: seen.update(kw) or _Client())
    return seen


@pytest.mark.parametrize("tls, interface, verify, port", [
    ("verify", "https", True, 8443), ("trust", "https", False, 8443), ("none", "http", None, 8123), (None, None, None, 8123),
])
def test_clickhouse_interface_and_verification(fake_clickhouse, tls, interface, verify, port):
    with ds._clickhouse_client(_conn("clickhouse", tls)):
        pass
    assert fake_clickhouse.get("interface") == interface
    assert fake_clickhouse.get("verify") == verify
    assert fake_clickhouse["port"] == port and fake_clickhouse["closed"]
    assert "pool_mgr" not in fake_clickhouse


def test_clickhouse_the_company_authority_goes_in_memory(fake_clickhouse):
    before = _ca_files()
    with ds._clickhouse_client(_conn("clickhouse", "verify", CA)):
        pool = fake_clickhouse["pool_mgr"]
        assert pool.connection_pool_kw["ca_cert_data"] == CA.strip()
        assert pool.connection_pool_kw["cert_reqs"] == "CERT_REQUIRED"
    assert _ca_files() == before


# ── Trino: its client over requests ─────────────────────────────────────────────
@pytest.fixture
def fake_trino(monkeypatch):
    import trino

    seen = {}

    class _Conn:
        def close(self):
            seen["closed"] = True

    def connect(**kw):
        seen.update(kw)
        v = kw.get("verify")
        seen["ca_at_connect"] = open(v).read() if isinstance(v, str) else None
        return _Conn()

    monkeypatch.setattr(trino.dbapi, "connect", connect)
    return seen


def test_trino_verify_with_the_company_authority(fake_trino):
    with ds._trino_connect(_conn("trino", "verify", CA)):
        path = fake_trino["verify"]
        assert fake_trino["http_scheme"] == "https" and fake_trino["port"] == 8443
        assert fake_trino["ca_at_connect"].strip() == CA.strip()
    assert not os.path.exists(path) and fake_trino["closed"]


def test_trino_trust_and_legacy(fake_trino):
    with ds._trino_connect(_conn("trino", "trust", password="")):
        assert fake_trino["http_scheme"] == "https" and fake_trino["verify"] is False
    fake_trino.clear()
    with ds._trino_connect(_conn("trino")):   # a password: HTTPS, verified, as always
        assert fake_trino["http_scheme"] == "https" and "verify" not in fake_trino and fake_trino["port"] == 8080
    fake_trino.clear()
    with ds._trino_connect(_conn("trino", password="")):   # no password: plain HTTP, as always
        assert "http_scheme" not in fake_trino


def test_trino_a_password_never_goes_in_plain_text(fake_trino):
    with pytest.raises(IngestError, match="password travels only over HTTPS"):
        with ds._trino_connect(_conn("trino", "none")):
            pass
    assert "host" not in fake_trino
    with ds._trino_connect(_conn("trino", "none", password="")):
        assert "http_scheme" not in fake_trino


# ── Oracle: python-oracledb's TCPS, Thin mode ───────────────────────────────────
@pytest.fixture
def fake_oracledb(monkeypatch):
    seen = {}
    module = types.ModuleType("oracledb")
    module.connect = lambda **kw: seen.update(kw) or object()
    monkeypatch.setitem(sys.modules, "oracledb", module)
    return seen


def test_oracle_verify_is_tcps_with_the_name_matched(fake_oracledb):
    ds._oracle_connect(_conn("oracle", "verify", CA, database="ORCLPDB1"))
    assert fake_oracledb["protocol"] == "tcps" and fake_oracledb["ssl_server_dn_match"] is True
    assert fake_oracledb["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert fake_oracledb["port"] == 2484


def test_oracle_trust_is_tcps_without_checks(fake_oracledb):
    ds._oracle_connect(_conn("oracle", "trust", database="ORCLPDB1", port=1522))
    assert fake_oracledb["protocol"] == "tcps" and fake_oracledb["ssl_server_dn_match"] is False
    assert fake_oracledb["ssl_context"].verify_mode == ssl.CERT_NONE and fake_oracledb["port"] == 1522


@pytest.mark.parametrize("tls", ["none", None])
def test_oracle_none_and_legacy_are_plain_tcp(fake_oracledb, tls):
    ds._oracle_connect(_conn("oracle", tls, database="ORCLPDB1"))
    assert "protocol" not in fake_oracledb and "ssl_context" not in fake_oracledb and fake_oracledb["port"] == 1521


# ── the errors, as the real drivers gave them on 2026-10-28 ─────────────────────
class _Err(Exception):
    pass


@pytest.mark.parametrize("db_type, text, expected", [
    ("postgresql",
     'IO: [libpq] Failed to connect: connection to server at "db.example.com" (10.0.0.2), port 5432 failed: SSL error: certificate verify failed\n',
     "PostgreSQL: the TLS certificate of db.example.com:5432 cannot be verified (certificate verify failed). If your company's own "
     "authority signed it, paste that authority's certificate in the connection; if it is self-signed, choose «trust the server's certificate»."),
    ("postgresql",
     'IO: [libpq] Failed to connect: connection to server at "10.0.0.2", port 5432 failed: server certificate for "db.example.com" '
     '(and 5 other names) does not match host name "10.0.0.2"\n',
     "PostgreSQL: the TLS certificate of db.example.com:5432 is not issued for the name db.example.com. Write as host the name the "
     "certificate is issued for, or choose «trust the server's certificate» in the connection."),
    ("postgresql",
     'IO: [libpq] Failed to connect: connection to server at "db.example.com" (10.0.0.2), port 5432 failed: FATAL:  no pg_hba.conf entry '
     'for host "10.0.0.5", user "tab", database "tab", no encryption\n',
     'PostgreSQL: no pg_hba.conf entry for host "10.0.0.5", user "tab", database "tab", no encryption. '
     "The server accepts only encrypted connections: choose an encrypted option in the connection."),
    ("postgresql",
     'IO: [libpq] Failed to connect: connection to server at "db.example.com" (10.0.0.2), port 5432 failed: server does not support SSL, '
     'but SSL was required\n',
     "PostgreSQL: db.example.com:5432 does not offer TLS encryption. Enable TLS on the server, or choose «not encrypted» in the "
     "connection if the network is trusted."),
    ("clickhouse",
     "HTTPSConnectionPool(host='db.example.com', port=8443): Max retries exceeded with url: /?query_id=1 (Caused by "
     "SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
     "certificate (_ssl.c:1011)')))",
     "ClickHouse: the TLS certificate of db.example.com:8443 cannot be verified (certificate verify failed: unable to get local issuer "
     "certificate). If your company's own authority signed it, paste that authority's certificate in the connection; if it is "
     "self-signed, choose «trust the server's certificate»."),
    ("clickhouse",
     "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: IP address mismatch, certificate is not valid for '10.0.0.4'. (_ssl.c:1011)",
     "ClickHouse: the TLS certificate of db.example.com:8443 is not issued for the name db.example.com. Write as host the name the "
     "certificate is issued for, or choose «trust the server's certificate» in the connection."),
    ("oracle",
     'DPY-6006: The name on the server certificate does not match the expected value: "10.0.0.6"',
     "Oracle: the TLS certificate of db.example.com:2484 is not issued for the name db.example.com. Write as host the name the "
     "certificate is issued for, or choose «trust the server's certificate» in the connection."),
])
def test_tls_errors_say_what_to_choose(db_type, text, expected):
    port = {"postgresql": 5432, "clickhouse": 8443, "oracle": 2484}[db_type]
    assert describe_db_error(_Err(text), db_type, "db.example.com", port) == expected


def test_mysql_refusing_plain_text_says_to_encrypt():
    import pymysql

    e = pymysql.err.OperationalError(3159, "Connections using insecure transport are prohibited while --require_secure_transport=ON.")
    assert describe_db_error(e, "mysql", "my.example.com", 3306) == (
        "MySQL: Connections using insecure transport are prohibited while --require_secure_transport=ON (code 3159). "
        "The server accepts only encrypted connections: choose an encrypted option in the connection."
    )


def test_mysql_a_certificate_error_is_not_called_unreachable():
    import pymysql

    e = pymysql.err.OperationalError(2003, "Can't connect to MySQL server on 'my.example.com' ([SSL: CERTIFICATE_VERIFY_FAILED] "
                                           "certificate verify failed: self-signed certificate in certificate chain (_ssl.c:1011))")
    message = describe_db_error(e, "mysql", "my.example.com", 3306)
    assert message.startswith("MySQL: the TLS certificate of my.example.com:3306 cannot be verified "
                              "(certificate verify failed: self-signed certificate in certificate chain).")
    assert "unreachable" not in message


# ── against the CI's PostgreSQL, which has no TLS ───────────────────────────────
URI = os.environ.get("POSTGRES_TEST_URI")


def _real(tls):
    u = urlparse(URI)
    return ds.DbConnectionSpec(db_type="postgresql", host=u.hostname, port=u.port or 5432, username=unquote(u.username or ""),
                               password=unquote(u.password or ""), database=u.path.lstrip("/"), extra={"tls": tls} if tls else {})


@pytest.mark.skipif(not URI, reason="needs a PostgreSQL (POSTGRES_TEST_URI)")
def test_postgresql_without_tls_refuses_an_encrypted_choice_and_serves_the_plain_ones():
    for tls in ("verify", "trust"):
        with pytest.raises(IngestError, match="does not offer TLS encryption. Enable TLS on the server"):
            ds.query_rows(_real(tls), "SELECT 1 AS ok", 1)
    for tls in ("none", None):
        df, _ = ds.query_rows(_real(tls), "SELECT 1 AS ok", 1)
        assert df["ok"].to_list() == [1]


# ── the native dbt profile: dbt reaches the warehouse as Tabularia does ─────────
def _native_files(db_type, layout=None, **conn):
    from app.engine import dbt_export as d

    native = {"db_type": db_type, "pw_env": "P", "target_schema": "s",
              "conn": {"host": "db.x.it", "port": 5432 if db_type == "postgresql" else 8443, "database": "shop", "username": "u", **conn}}
    model = {"name": "m", "sql": "select 1", "materialized": "table", "description": "", "columns": []}
    return d.build_dbt_project("Flow", [model], None, [], native=native, layout=layout)


def test_dbt_postgres_profile_verifies_with_the_company_authority_shipped():
    files = _native_files("postgresql", tls="verify", ca_cert=CA)
    assert "      sslmode: verify-full\n      sslrootcert: certs/ca.pem" in files["profiles.yml"]
    assert files["certs/ca.pem"].strip() == CA.strip()
    assert "`certs/ca.pem` is the certificate of the authority" in files["README.md"]
    folder = _native_files("postgresql", layout={"package": "folder", "folder": "f"}, tls="verify", ca_cert=CA)
    assert "sslmode: verify-full" in folder["INTEGRATION.md"]


@pytest.mark.parametrize("tls, lines", [
    ("verify", ["sslmode: verify-full", "sslrootcert: system"]), ("trust", ["sslmode: require"]),
    ("none", ["sslmode: disable"]), (None, []),
])
def test_dbt_postgres_profile_sslmode(tls, lines):
    profile = _native_files("postgresql", **({"tls": tls} if tls else {}))["profiles.yml"]
    assert [x.strip() for x in profile.splitlines() if x.strip().startswith("ssl")] == lines


@pytest.mark.parametrize("tls, port, secure, unverified", [
    ("verify", 8443, "true", False), ("trust", 8443, "true", True), ("none", 8443, "false", False), (None, 8443, "true", False),
    (None, 8123, "false", False),
])
def test_dbt_clickhouse_profile_follows_the_choice(tls, port, secure, unverified):
    files = _native_files("clickhouse", port=port, **({"tls": tls} if tls else {}))
    secure_line = next(x for x in files["profiles.yml"].splitlines() if x.strip().startswith("secure:"))
    assert f"'CLICKHOUSE_SECURE', '{secure}'" in secure_line
    assert ("      verify: false" in files["profiles.yml"]) is unverified
