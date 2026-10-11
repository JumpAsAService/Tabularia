"""Messaggi d'errore PARLANTI dai driver dei database.

All'utente arriva il messaggio del SERVER, quello che si leggerebbe nel log del
database, non l'eccezione Python di trasporto. Esempio reale dal playground
ClickHouse: al posto di

    ('Connection broken: IncompleteRead(198 bytes read)', ...)

si mostra

    ClickHouse: Limit for result exceeded, max rows: 1.00 million, current rows:
    1.02 million (TOO_MANY_ROWS_OR_BYTES, code 396). This is a limit set by the
    server: reduce the data with a filter or a LIMIT.

I testi per l'utente sono SEMPRE in inglese (richiesta esplicita, 2026-09-12):
un test di guardia in tests/test_db_errors.py fallisce se tornano in italiano.

Le forme gestite sono state catturate dai driver veri il 2026-09-12 e sono
riprodotte verbatim in tests/test_db_errors.py:

- ClickHouse (clickhouse-connect): `Code: N. DB::Exception: … (NOME)` dopo
  `server response:`. Se lo stream è già partito, lo stesso testo arriva nei
  byte parziali di IncompleteRead, incorniciato da `__exception__`.
- PostgreSQL (ADBC/libpq): la riga `ERROR:` o `FATAL:`, fra il prefisso del
  driver, l'eco della query e `SQLSTATE`.
- MySQL/MariaDB (PyMySQL): args = (codice, messaggio).
- Trino: `TrinoQueryError.message` ed `.error_name`.
- Oracle (python-oracledb, Thin): `ORA-NNNNN: messaggio` del server; i `DPY-6NNN`
  («cannot connect») portano dentro la causa di rete o un ORA del listener.
- SQL Server (mssql-python): `.ddbc_error` col testo del server dietro i prefissi
  `[Microsoft][SQL Server]`; la rete arriva come codice Winsock (`TCP Provider:
  Error code 0x2AFA`), il TLS come errore OpenSSL dell'`SSL Provider`.
- Rete, per tutti: DNS, connessione rifiutata, timeout, TLS, riconosciuti dal
  testo della catena di eccezioni.

Il traceback completo non si perde: chi solleva usa `raise … from e`, quindi la
causa originale resta nel `error_detail` del run.
"""
from __future__ import annotations

import re
from typing import Optional

from app.ingest.converters import IngestError

from app.core.redaction import redact_secrets

LABELS = {
    "postgresql": "PostgreSQL",
    "clickhouse": "ClickHouse",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "trino": "Trino",
    "oracle": "Oracle",
    "sqlserver": "SQL Server",
    "s3": "S3",
}

# Prefissi dei messaggi prodotti qui. Il gateway li usa per NON scambiare un
# errore del server del database, per esempio «out of memory» di Postgres, per
# un crash del worker. Tenere allineato con gateway/app/routes/runs.py.
MESSAGE_PREFIXES = tuple(f"{v}:" for v in LABELS.values()) + ("Database:",)

# il gateway tronca il dettaglio di /db/inspect a 500 caratteri
MAX_LEN = 500

# testi per l'utente: in INGLESE
_LIMIT_HINT = "This is a limit set by the server: reduce the data with a filter or a LIMIT."
_TIMEOUT_HINT = (
    "The query exceeded the time allowed by the server: reduce the data "
    "or ask the database administrator for a higher limit."
)
_AUTH_HINT = "Check the connection's username and password."
_PERM_HINT = "The connection's user lacks the required privileges on this object."
_READONLY_HINT = "The connection's user is read-only: it cannot be used as a destination."
_SERVER_MEMORY_HINT = "The database server ran out of memory for this query: reduce the data."
_NET_HINT = "The server is unreachable: check host, port and network."
_SERVICE_HINT = "Check the service name in the connection's Database field."

# ClickHouse: codice → suggerimento (nomi da ErrorCodes.cpp)
_CH_HINTS = {
    158: _LIMIT_HINT,           # TOO_MANY_ROWS
    307: _LIMIT_HINT,           # TOO_MANY_BYTES
    396: _LIMIT_HINT,           # TOO_MANY_ROWS_OR_BYTES
    201: _LIMIT_HINT,           # QUOTA_EXCEEDED
    159: _TIMEOUT_HINT,         # TIMEOUT_EXCEEDED
    160: _TIMEOUT_HINT,         # TOO_SLOW
    241: _SERVER_MEMORY_HINT,   # MEMORY_LIMIT_EXCEEDED
    516: _AUTH_HINT,            # AUTHENTICATION_FAILED
    192: _AUTH_HINT,            # UNKNOWN_USER
    194: _AUTH_HINT,            # REQUIRED_PASSWORD
    497: _PERM_HINT,            # ACCESS_DENIED
    164: _READONLY_HINT,        # READONLY
}
# PostgreSQL: SQLSTATE → suggerimento
_PG_HINTS = {
    "28P01": _AUTH_HINT,          # invalid_password
    "28000": _AUTH_HINT,          # invalid_authorization_specification
    "42501": _PERM_HINT,          # insufficient_privilege
    "57014": _TIMEOUT_HINT,       # query_canceled (statement_timeout)
    "53200": _SERVER_MEMORY_HINT, # out_of_memory
    "25006": _READONLY_HINT,      # read_only_sql_transaction
}
# MySQL/MariaDB: codice → suggerimento
_MYSQL_HINTS = {
    1045: _AUTH_HINT, 1044: _PERM_HINT, 1142: _PERM_HINT, 1143: _PERM_HINT,
    3024: _TIMEOUT_HINT, 1290: _READONLY_HINT,
    2003: _NET_HINT, 2005: _NET_HINT, 2006: _NET_HINT, 2013: _NET_HINT,
}

# Oracle: codice → suggerimento
_ORACLE_HINTS = {
    "ORA-01017": _AUTH_HINT,          # invalid username/password
    "ORA-28000": _AUTH_HINT,          # account locked
    "ORA-01045": _PERM_HINT,          # no CREATE SESSION
    "ORA-01031": _PERM_HINT,          # insufficient privileges
    "ORA-01013": _TIMEOUT_HINT,       # user requested cancel (call timeout)
    "DPY-4024": _TIMEOUT_HINT,        # call timeout
    "ORA-04030": _SERVER_MEMORY_HINT, # out of process memory
    "ORA-04031": _SERVER_MEMORY_HINT, # shared pool
    "ORA-16000": _READONLY_HINT,      # read-only database
    "ORA-12514": _SERVICE_HINT,       # service not registered with the listener
    "DPY-6001": _SERVICE_HINT,        # idem, in modalità Thin
}
# SQL Server
_SQLSERVER_PREFIX_RE = re.compile(r"^(?:\[[^\]]+\])+\s*")
_SQLSERVER_TCP_RE = re.compile(r"TCP Provider: Error code (0x[0-9A-Fa-f]+)")
# codici Winsock → la stessa causa che il riconoscitore di rete sa dire
_SQLSERVER_TCP = {"0X2AF9": "Name or service not known", "0X2AFA": "Name or service not known",
                  "0X274D": "Connection refused", "0X2749": "Connection refused", "0X2751": "Connection refused",
                  "0X274C": "timed out"}
_SQLSERVER_ARROW_RE = re.compile(r"Unsupported data type for Arrow batch fetch for column - (.+?), Type - (-?\d+)")
_SQLSERVER_TIPI = {"-150": "sql_variant", "-151": "a CLR type: geometry, geography, hierarchyid"}
_SQLSERVER_LOGIN_HINT = (
    "Check the connection's username, password and database: SQL Server reports a database "
    "that does not exist, or that the user cannot open, as a failed login."
)
_SQLSERVER_TLS_HINT = (
    "Give the server a certificate that can be verified, or choose «trust the server's "
    "certificate» in the connection (self-signed certificates)."
)

_ORACLE_CODE_RE = re.compile(r"\b((?:ORA|DPY)-\d{4,5}):\s*([^\n]+)")

_CH_EXC_RE = re.compile(r"Code:\s*(\d+)\.\s*DB::Exception:\s*")
_CH_NAME_RE = re.compile(r"\(([A-Z][A-Z0-9_]{2,})\)")
_CH_TRAILERS = (
    re.compile(r"\s*\((?:for url|version)\b[^)]*\)\s*$"),  # (for url …) / (version …)
    re.compile(r"\s*\([A-Z][A-Z0-9_]{2,}\)\s*$"),          # (NOME_ERRORE)
)
_PG_LINE_RE = re.compile(r"\b(ERROR|FATAL):\s+(.+)")
_SQLSTATE_RE = re.compile(r"SQLSTATE:\s*([0-9A-Z]{5})")
_ADBC_PREFIX_RE = re.compile(r"^[A-Z_]+:\s*(?:\[[\w-]+\]\s*)?(?:Failed to [^:]*:\s*)?")

# connessione stabilita e query accettata, ma nessun byte entro il tempo massimo
# di lettura (urllib3: «Read timed out. (read timeout=300)»): NON è la rete
_READ_TIMEOUT_RE = re.compile(r"Read timed out\.?(?:\s*\(read timeout=(\d+(?:\.\d+)?)\))?", re.I)

_NET_PATTERNS = (
    ("dns", re.compile(
        r"Name or service not known|Failed to resolve|could not translate host name|"
        r"Temporary failure in name resolution|nodename nor servname|getaddrinfo failed", re.I)),
    ("tls", re.compile(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSLError|wrong version number", re.I)),
    ("refused", re.compile(r"Connection refused|ECONNREFUSED", re.I)),
    ("timeout", re.compile(r"timed out|ConnectTimeout|timeout expired", re.I)),
    ("broken", re.compile(
        r"IncompleteRead|Connection broken|Connection reset|RemoteDisconnected|"
        r"server closed the connection", re.I)),
)


def _label(db_type: Optional[str]) -> Optional[str]:
    return LABELS.get((db_type or "").lower())


def _chain(exc: BaseException) -> list[BaseException]:
    """L'eccezione e tutte le sue cause: __cause__, __context__ ed eccezioni in args
    (urllib3.ProtocolError porta l'IncompleteRead negli args)."""
    out: list[BaseException] = []
    seen: set[int] = set()
    queue: list[Optional[BaseException]] = [exc]
    while queue:
        cur = queue.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        out.append(cur)
        queue.extend([cur.__cause__, cur.__context__])
        queue.extend(a for a in getattr(cur, "args", ()) if isinstance(a, BaseException))
    return out


def _texts(chain: list[BaseException]) -> list[str]:
    texts: list[str] = []
    for e in chain:
        partial = getattr(e, "partial", None)  # http.client.IncompleteRead
        if isinstance(partial, (bytes, bytearray)) and partial:
            texts.append(bytes(partial).decode("utf-8", "replace"))
        try:
            texts.append(str(e))
        except Exception:
            pass
    return [t for t in texts if t]


def _strip_trailers(text: str) -> str:
    while True:
        before = text
        for rx in _CH_TRAILERS:
            text = rx.sub("", text)
        if text == before:
            return text.strip()


def _clickhouse(texts: list[str]) -> Optional[tuple[str, Optional[str]]]:
    for t in texts:
        m = _CH_EXC_RE.search(t)
        if not m:
            continue
        code = int(m.group(1))
        rest = t[m.end():].split("__exception__")[0]
        # il nome dell'errore è l'ULTIMO token maiuscolo fra parentesi: nei
        # messaggi di sintassi compaiono anche frammenti come «(SELEC)»
        names = [n for n in _CH_NAME_RE.findall(rest)]
        name = names[-1] if names else None
        first = rest.strip().splitlines()[0] if rest.strip() else ""
        body = _strip_trailers(first).rstrip(".").strip() or "server error"
        tag = f"{name}, code {code}" if name else f"code {code}"
        return f"{body} ({tag}).", _CH_HINTS.get(code)
    return None


def _trino(chain: list[BaseException]) -> Optional[tuple[str, Optional[str]]]:
    for e in chain:
        if not (type(e).__module__ or "").startswith("trino"):
            continue
        message = getattr(e, "message", None)
        if isinstance(message, str) and message:
            name = getattr(e, "error_name", None)
            return (f"{message} ({name})." if name else f"{message}."), None
    return None


def _oracle(chain: list[BaseException]) -> Optional[tuple[str, Optional[str]]]:
    """Il messaggio del server Oracle. Un ORA vince su un DPY. Un «DPY-6005: cannot
    connect» non dice niente da solo: il perché sta nelle righe dopo — una causa di
    rete (lasciata al riconoscitore di rete, che la dice meglio) o un altro DPY,
    come «DPY-6001: Service … is not registered with the listener», il più interno,
    che si mostra."""
    for e in chain:
        if not (type(e).__module__ or "").startswith("oracledb"):
            continue
        err = e.args[0] if e.args else None
        testo = getattr(err, "message", None) or str(e)
        codici = _ORACLE_CODE_RE.findall(testo)
        if not codici:
            return None
        ora = [(c, m) for c, m in codici if c.startswith("ORA-")]
        if ora:
            codice, messaggio = ora[0]
        elif any(p.search(t) for t in _texts(chain) for _, p in _NET_PATTERNS):
            return None
        else:
            codice, messaggio = codici[-1]
        messaggio = re.sub(r"\s*\(CONNECTION_ID=[^)]*\)", "", messaggio).strip().rstrip(".")
        return f"{messaggio} ({codice}).", _ORACLE_HINTS.get(codice)
    return None


def _sqlserver(chain: list[BaseException], texts: list[str], host: Optional[str], port: Optional[int]) -> Optional[tuple[str, Optional[str]]]:
    for t in texts:
        m = _SQLSERVER_ARROW_RE.search(t)
        if m:
            tipo = _SQLSERVER_TIPI.get(m.group(2), f"ODBC type {m.group(2)}")
            return (f"column {m.group(1)} has a type that cannot be imported ({tipo}).",
                    "Convert it in a SQL source, e.g. CAST(col AS NVARCHAR(4000)).")
    for e in chain:
        if not (type(e).__module__ or "").startswith("mssql_python"):
            continue
        ddbc = str(getattr(e, "ddbc_error", None) or "").strip()
        driver = str(getattr(e, "driver_error", None) or "").strip()
        if "certificate verify failed" in ddbc:
            causa = ddbc.split("::", 1)[-1].rstrip("]").strip().replace("failed:", "failed: ")
            dove = f"{host}:{port}" if host and port else (host or "the server")
            return f"the TLS certificate of {dove} cannot be verified ({causa}).", _SQLSERVER_TLS_HINT
        m = _SQLSERVER_TCP_RE.search(ddbc)
        if m and m.group(1).upper() in _SQLSERVER_TCP:
            return _network([_SQLSERVER_TCP[m.group(1).upper()]], host, port), None
        testo = _SQLSERVER_PREFIX_RE.sub("", ddbc) or driver
        if not testo:
            return None
        testo = testo.rstrip(".")
        if "Login failed for user" in testo:
            return f"{testo}.", _SQLSERVER_LOGIN_HINT
        if "permission was denied" in testo:
            return f"{testo}.", _PERM_HINT
        return f"{testo}.", None
    return None


def _mysql(chain: list[BaseException]) -> Optional[tuple[str, Optional[str]]]:
    for e in chain:
        args = getattr(e, "args", ())
        if ((type(e).__module__ or "").startswith("pymysql") and len(args) >= 2
                and isinstance(args[0], int) and isinstance(args[1], str)):
            code, message = args[0], args[1].strip().rstrip(".")
            return f"{message} (code {code}).", _MYSQL_HINTS.get(code)
    return None


def _postgres(chain: list[BaseException], texts: list[str]) -> Optional[tuple[str, Optional[str]]]:
    for t in texts:
        m = _PG_LINE_RE.search(t)
        if not m:
            continue
        message = m.group(2).strip().rstrip(".")
        state = next((s for e in chain if isinstance(s := getattr(e, "sqlstate", None), str) and len(s) == 5), None)
        if state is None:
            sm = _SQLSTATE_RE.search(t)
            state = sm.group(1) if sm else None
        # il FATAL di autenticazione arriva da libpq SENZA sqlstate: il codice
        # serve solo per scegliere il suggerimento, non si mostra
        hint_state = state or ("28P01" if "password authentication failed" in message else None)
        text = f"{message} (SQLSTATE {state})." if state else f"{message}."
        return text, _PG_HINTS.get(hint_state or "")
    return None


def _network(texts: list[str], host: Optional[str], port: Optional[int]) -> Optional[str]:
    blob = "\n".join(texts)
    where = f"{host}:{port}" if host and port else host
    target = where or "the server"
    lento = _READ_TIMEOUT_RE.search(blob)
    if lento:
        quanto = f"{float(lento.group(1)):g} seconds" if lento.group(1) else "the time limit"
        return (f"{target} accepted the query but sent no data for {quanto}: the database was still "
                "computing the result, as happens with large sorts and heavy queries.")
    for kind, rx in _NET_PATTERNS:
        if not rx.search(blob):
            continue
        if kind == "dns":
            return (f"Cannot resolve host {host}: check the server name." if host
                    else "Cannot resolve the server name: check the connection's host.")
        if kind == "tls":
            return f"TLS error with {target}: check that the port matches the protocol, encrypted or plain."
        if kind == "refused":
            return f"Connection refused by {target}: no service is listening on that port."
        if kind == "timeout":
            return f"No response from {target} within the time limit: check host, port and firewall."
        return ("The server closed the connection mid-response without giving a reason: "
                "this can be caused by a server limit or timeout.")
    return None


def _fallback(exc: BaseException) -> str:
    text = (str(exc) or type(exc).__name__).strip()
    text = _ADBC_PREFIX_RE.sub("", text).strip()
    return (text.splitlines()[0].strip() if text else type(exc).__name__)


def describe_db_error(
    exc: BaseException,
    db_type: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> str:
    """Messaggio leggibile per l'utente, col testo del server del database.

    Gli errori già parlanti (IngestError) passano invariati. Non solleva mai:
    un guasto qui non deve nascondere l'errore originale.
    """
    if isinstance(exc, IngestError):
        return str(exc)
    label = _label(db_type)
    try:
        chain = _chain(exc)
        texts = _texts(chain)
        found = None
        inferred = None
        for extractor, name in (
            (lambda: _clickhouse(texts), "ClickHouse"),
            (lambda: _trino(chain), "Trino"),
            (lambda: _oracle(chain), "Oracle"),
            (lambda: _sqlserver(chain, texts, host, port), "SQL Server"),
            (lambda: _mysql(chain), "MySQL"),
            (lambda: _postgres(chain, texts), "PostgreSQL"),
        ):
            found = extractor()
            if found:
                inferred = name
                break
        if found:
            text, hint = found
        else:
            text, hint = (_network(texts, host, port) or _fallback(exc)), None
        prefix = label or inferred or "Database"
        message = f"{prefix}: {text}" + (f" {hint}" if hint else "")
    except Exception:  # pragma: no cover — il traduttore non deve mai propagare
        message = f"{label or 'Database'}: {type(exc).__name__}: {exc}"
    # i segreti non escono MAI verso l'utente: si redige PRIMA di troncare,
    # altrimenti un taglio a metà chiave lascerebbe visibile il resto
    return (redact_secrets(message) or message)[:MAX_LEN]
