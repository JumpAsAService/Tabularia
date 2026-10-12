"""Sorgenti database: esegue una query (o legge una tabella) e scrive il
risultato in parquet SENZA mai materializzare tutto in memoria.

Percorso dati: driver → batch Arrow → ParquetWriter su file temporaneo → upload
su storage. La memoria resta O(dimensione batch); il disco fa da buffer, come
per gli upload di file.

Un driver per tipo di database, tutti con lo stesso protocollo (un generatore
che produce PRIMA lo schema Arrow, POI i RecordBatch):
- postgresql        → ADBC (Arrow nativo, streaming lato server)
- clickhouse        → clickhouse-connect (query_arrow_stream, Arrow nativo)
- mysql / mariadb   → PyMySQL con cursore unbuffered (fetchmany → Arrow)
- trino             → client DBAPI ufficiale (paginazione naturale → Arrow)
- oracle            → python-oracledb in modalità Thin (fetch_df_batches → Arrow)
- sqlserver         → mssql-python, il driver di Microsoft (arrow_reader → Arrow)
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any, Callable, Iterator, Literal
from urllib.parse import quote as _urlquote

import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import BaseModel

from app.core.config import get_settings
from app.ingest.converters import IngestError
from app.ingest.db_errors import describe_db_error

logger = logging.getLogger(__name__)

# righe per batch nel percorso DBAPI (Postgres/ClickHouse usano i batch nativi)
BATCH_ROWS = 50_000

DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "clickhouse": 8123,  # porta HTTP (clickhouse-connect), non la nativa 9000
    "trino": 8080,
    "oracle": 1521,
    "sqlserver": 1433,
}

DbType = Literal["postgresql", "mysql", "mariadb", "clickhouse", "trino", "oracle", "sqlserver"]


class DbSourceError(IngestError):
    """Errore parlante da mostrare all'utente (connessione, query, tipi)."""


class DbConnectionSpec(BaseModel):
    db_type: DbType
    host: str
    port: int | None = None
    username: str = ""
    # una delle due: `password_encrypted` (Fernet, come arriva dal gateway) o
    # `password` in chiaro (solo per prove dirette sull'engine in sviluppo)
    password: str = ""
    password_encrypted: str = ""
    database: str = ""   # Oracle: il SERVICE NAME (es. FREEPDB1, ORCLPDB1)
    db_schema: str = ""  # schema Postgres / schema Trino / owner Oracle; vuoto = default
    # opzioni del tipo, dal campo `extra` della connessione (SQL Server: {"tls": …})
    extra: dict = {}

    @property
    def port_or_default(self) -> int:
        return self.port or DEFAULT_PORTS[self.db_type]

    def resolve_password(self) -> str:
        if self.password_encrypted:
            from app.core.crypto import decrypt_secret

            return decrypt_secret(self.password_encrypted)
        return self.password


class DbSourceSpec(BaseModel):
    mode: Literal["table", "sql"]
    ref: str  # nome tabella (anche schema.tabella) oppure il testo SQL
    # colonne per l'ORDER BY finale: il parquet esce ordinato → pruning a valle.
    # Vuoto = nessun ordine (come prima). Sono nomi di colonna del RISULTATO.
    sort_keys: list[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# Costruzione della query
# ─────────────────────────────────────────────────────────────────────────────
_IDENT_QUOTE = {"postgresql": '"', "trino": '"', "mysql": "`", "mariadb": "`", "clickhouse": "`", "oracle": '"'}


def _tsql_ident(parte: str) -> str:
    """Un identificatore T-SQL fra parentesi quadre: `dbo` → `[dbo]`; uno già fra
    quadre (`[Ordini 2024]`, come lo restituisce l'elenco) resta com'è."""
    if len(parte) >= 2 and parte.startswith("[") and parte.endswith("]"):
        interno = parte[1:-1]
        if not interno or "]" in interno.replace("]]", ""):
            raise DbSourceError(f"Invalid name: {parte}")
        return parte
    return "[" + parte.replace("]", "]]") + "]"


def _tsql_query(sql: str, conn: DbConnectionSpec, source: DbSourceSpec) -> str:
    """La SQL dell'utente su SQL Server. Non si avvolge in `SELECT * FROM (…)`: in
    T-SQL una tabella derivata non può contenere una CTE né un ORDER BY, e sono
    frequenti. Si controlla invece che sia UNA sola query di lettura — un batch T-SQL
    potrebbe portare dietro `; DELETE …` — e si manda così com'è. Con le chiavi di
    ordinamento: si avvolge se si può, altrimenti (CTE) l'ORDER BY va in coda."""
    import sqlglot
    from sqlglot import exp

    try:
        statement = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except Exception:  # noqa: BLE001 — sintassi che sqlglot non conosce: decide il server, nell'involucro
        statement = None
    if statement is not None:
        if len(statement) != 1 or not isinstance(statement[0], exp.Query):
            raise DbSourceError("SQL Server: the SQL source must be a single SELECT (a WITH … SELECT is fine).")
        if statement[0].find(exp.Into) is not None:  # per sqlglot è una SELECT, per SQL Server crea una tabella
            raise DbSourceError("SQL Server: the SQL source must only read: SELECT … INTO creates a table, remove the INTO.")
    keys = [k.strip() for k in (source.sort_keys or []) if k and k.strip()]
    if not keys:
        return sql if statement is not None else f"SELECT * FROM ({sql}) AS _q"
    ordine = " ORDER BY " + ", ".join(_tsql_ident(k) for k in keys)
    if statement is not None and statement[0].find(exp.With) is not None:
        return sql + ordine
    return f"SELECT * FROM ({sql}) AS _q" + ordine


def _oracle_ident(parte: str) -> str:
    """Un identificatore Oracle, con la regola di Oracle: SENZA virgolette si
    legge in maiuscolo (`hr.employees` è HR.EMPLOYEES, come in SQL Developer);
    FRA virgolette resta com'è (`HR."conti 2024"`). L'elenco delle tabelle
    restituisce i nomi già in questa forma, quindi scegliere dalla lista torna."""
    if len(parte) >= 2 and parte.startswith('"') and parte.endswith('"'):
        interno = parte[1:-1]
        if not interno or '"' in interno:
            raise DbSourceError(f"Nome non valido: {parte}")
        return f'"{interno}"'
    if '"' in parte:
        raise DbSourceError(f"Nome non valido: {parte}")
    return f'"{parte.upper()}"'


def build_query(conn: DbConnectionSpec, source: DbSourceSpec) -> str:
    """Query finale da eseguire sul database.

    - mode=sql: il testo dell'utente viene avvolto in `SELECT * FROM (…) AS _q`.
      Non è una barriera di sicurezza (la barriera è la capability CONNECT e
      un'utenza DB read-only): serve a rifiutare statement non-SELECT e a poter
      aggiungere LIMIT senza toccare il testo.
    - mode=table: identificatori quotati per dialetto; `schema.tabella` accettato,
      altrimenti si antepone lo schema della connessione se impostato.
    """
    if source.mode == "sql":
        sql = source.ref.strip().rstrip(";").strip()
        if not sql:
            raise DbSourceError("La query SQL è vuota")
        if conn.db_type == "sqlserver":
            return _tsql_query(sql, conn, source)
        # Oracle non vuole AS davanti all'alias di una tabella, né un alias con «_»
        alias = " q" if conn.db_type == "oracle" else " AS _q"
        return _with_order_by(f"SELECT * FROM ({sql}){alias}", conn, source)

    if conn.db_type == "oracle":
        parts = [p.strip() for p in source.ref.split(".") if p.strip()]
        if not parts:
            raise DbSourceError("Il nome della tabella è vuoto")
        if len(parts) == 1 and conn.db_schema:
            parts = [conn.db_schema, parts[0]]
        return _with_order_by("SELECT * FROM " + ".".join(_oracle_ident(p) for p in parts), conn, source)

    if conn.db_type == "sqlserver":
        parts = [p.strip() for p in source.ref.split(".") if p.strip()]
        if not parts:
            raise DbSourceError("Il nome della tabella è vuoto")
        if len(parts) == 1 and conn.db_schema:
            parts = [conn.db_schema, parts[0]]
        return _with_order_by("SELECT * FROM " + ".".join(_tsql_ident(p) for p in parts), conn, source)

    q = _IDENT_QUOTE[conn.db_type]
    parts = [p.strip() for p in source.ref.split(".") if p.strip()]
    if not parts:
        raise DbSourceError("Il nome della tabella è vuoto")
    if any(q in p for p in parts):
        raise DbSourceError(f"Nome tabella non valido: contiene {q!r}")
    if len(parts) == 1 and conn.db_schema:
        parts = [conn.db_schema, parts[0]]
    qualified = ".".join(f"{q}{p}{q}" for p in parts)
    return _with_order_by(f"SELECT * FROM {qualified}", conn, source)


def _with_order_by(base: str, conn: DbConnectionSpec, source: DbSourceSpec) -> str:
    """Appende `ORDER BY <chiavi>` (quotate per dialetto) se ci sono sort_keys.
    Le chiavi sono nomi di colonna del risultato; il DB dà un errore parlante se
    non esistono. Vuoto = query invariata."""
    keys = [k.strip() for k in (source.sort_keys or []) if k and k.strip()]
    if not keys:
        return base
    if conn.db_type == "oracle":
        return f"{base} ORDER BY " + ", ".join(_oracle_ident(k) for k in keys)
    if conn.db_type == "sqlserver":
        return f"{base} ORDER BY " + ", ".join(_tsql_ident(k) for k in keys)
    q = _IDENT_QUOTE[conn.db_type]
    for k in keys:
        if q in k:
            raise DbSourceError(f"Chiave di ordinamento non valida: contiene {q!r}")
    cols = ", ".join(f"{q}{k}{q}" for k in keys)
    return f"{base} ORDER BY {cols}"


# ─────────────────────────────────────────────────────────────────────────────
# Driver: generatori "schema poi batch"
# ─────────────────────────────────────────────────────────────────────────────
def _batches_postgresql(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    from adbc_driver_postgresql import dbapi as adbc_pg

    uri = (
        f"postgresql://{_urlquote(conn.username, safe='')}:"
        f"{_urlquote(conn.resolve_password(), safe='')}@"
        f"{conn.host}:{conn.port_or_default}/{conn.database}"
    )
    with adbc_pg.connect(uri) as c:
        with c.cursor() as cur:
            cur.execute(query)
            reader = cur.fetch_record_batch()  # RecordBatchReader: streaming vero
            yield _schema_senza_estensioni(reader.schema)
            for batch in reader:
                yield _batch_senza_estensioni(batch)


# Il driver ADBC di Postgres restituisce i tipi che Arrow non ha (numeric, e gli
# altri tipi «di casa» di Postgres) come ESTENSIONE opaca (`arrow.opaque`) sopra
# un testo. Scritta così nel parquet, Polars la legge come testo ma il suo
# lettore in streaming va in PANIC appena un filtro o un controllo dei NULL viene
# spinto dentro la lettura di quella colonna — e il worker usciva a metà lavoro.
# Si scrive senza involucro (trovato dall'oracolo dell'export dbt il 2026-10-10):
# · `numeric` è un NUMERO (Float64): importi e prezzi si sommano, come quelli di un
#   CSV o di un Excel (scelta dell'utente, 2026-10-10; prima erano testo e `sum` li
#   rifiutava). Float64 e non decimal: il driver non dice precisione e scala, e i
#   cinque motori calcolano i double allo stesso modo (ClickHouse tronca le divisioni
#   fra decimali alla scala della colonna, gli altri no). NaN e ±Infinity restano tali.
# · gli altri tipi opachi restano il loro testo.
def _tipo_senza_estensione(t):
    if isinstance(t, pa.OpaqueType) and t.type_name == "numeric":
        return pa.float64()
    if isinstance(t, pa.BaseExtensionType):
        base = t.storage_type
        return pa.string() if (pa.types.is_string(base) or pa.types.is_large_string(base) or str(base) in ("string_view", "utf8_view")) else base
    return t


def _schema_senza_estensioni(schema):
    if not any(isinstance(f.type, pa.BaseExtensionType) for f in schema):
        return schema
    return pa.schema([pa.field(f.name, _tipo_senza_estensione(f.type), nullable=f.nullable) for f in schema])


def _batch_senza_estensioni(batch):
    if not any(isinstance(f.type, pa.BaseExtensionType) for f in batch.schema):
        return batch
    cols = []
    for arr in batch.columns:
        if isinstance(arr.type, pa.BaseExtensionType):
            arr = pc.cast(arr.storage, _tipo_senza_estensione(arr.type))
        cols.append(arr)
    return pa.RecordBatch.from_arrays(cols, schema=_schema_senza_estensioni(batch.schema))


# ClickHouse esporta in Arrow i tipi temporali come INTERI grezzi (Date→uint16
# giorni, Date32→int32, DateTime→uint32 secondi, DateTime64→int64 in `scale`
# decimali): senza riconvertirli finirebbero numerici nel parquet (es. il datetime
# 1714546800). A livello Arrow un uint32 è indistinguibile da un intero vero,
# quindi usiamo i TIPI ClickHouse (nome della classe datatype, che ignora i
# wrapper Nullable/LowCardinality) per sapere quali colonne convertire.
def _clickhouse_temporal_targets(ch_types) -> dict:
    """Indice colonna → (tipo Arrow di destinazione, scala DateTime64 | None)."""
    targets: dict[int, tuple] = {}
    for i, t in enumerate(ch_types):
        cls = type(t).__name__
        if cls in ("Date", "Date32"):
            targets[i] = (pa.date32(), None)
        elif cls == "DateTime":
            targets[i] = (pa.timestamp("s"), None)  # NAIVE = istante UTC (come ADBC)
        elif cls == "DateTime64":
            scale = int(getattr(t, "scale", 3) or 0)
            unit = {0: "s", 3: "ms", 6: "us", 9: "ns"}.get(scale, "us")
            targets[i] = (pa.timestamp(unit), scale)
    return targets


def _cast_ch_temporal(arr, target):
    """Reinterpreta l'intero grezzo di ClickHouse come date32/timestamp."""
    arrow_t, scale = target
    if pa.types.is_date(arrow_t):
        return pc.cast(pc.cast(arr, pa.int32()), arrow_t)  # giorni dal 1970
    raw = pc.cast(arr, pa.int64())
    if scale is not None and scale not in (0, 3, 6, 9):
        # DateTime64 con precisione non standard → riscala all'unità scelta (us)
        raw = pc.multiply(raw, 10 ** (6 - scale)) if scale < 6 else pc.divide(raw, 10 ** (scale - 6))
        raw = pc.cast(raw, pa.int64())
    return pc.cast(raw, arrow_t)


def _fix_clickhouse_temporals(batch, targets: dict):
    """Riscrive il RecordBatch convertendo le colonne temporali intere."""
    if not targets:
        return batch
    cols, fields = [], []
    for i, field in enumerate(batch.schema):
        arr, tgt = batch.column(i), targets.get(i)
        if tgt is not None:
            arr = _cast_ch_temporal(arr, tgt)
            field = pa.field(field.name, tgt[0], nullable=field.nullable)
        cols.append(arr)
        fields.append(field)
    return pa.RecordBatch.from_arrays(cols, schema=pa.schema(fields))


def _batches_clickhouse(conn: DbConnectionSpec, query: str, read_timeout: int | None = None) -> Iterator[Any]:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=conn.host,
        port=conn.port_or_default,
        username=conn.username or "default",
        password=conn.resolve_password(),
        database=conn.database or "default",
        connect_timeout=10,
        # None = il default della libreria (300 s): prova ed elenco tabelle
        **({"send_receive_timeout": read_timeout} if read_timeout else {}),
    )
    try:
        # tipi ClickHouse da uno schema LIMIT 0: individuano le colonne temporali
        # (che l'export Arrow rende interi grezzi) da riconvertire.
        targets = _clickhouse_temporal_targets(
            client.query(f"SELECT * FROM ({query}) AS _s LIMIT 0").column_types
        )
        sent_schema = False
        with client.query_arrow_stream(query) as stream:
            for chunk in stream:  # a seconda della versione: Table o RecordBatch
                batches = chunk.to_batches() if isinstance(chunk, pa.Table) else [chunk]
                for batch in batches:
                    batch = _fix_clickhouse_temporals(batch, targets)
                    if not sent_schema:
                        yield batch.schema
                        sent_schema = True
                    yield batch
        if not sent_schema:
            # risultato vuoto: schema da LIMIT 0, con i tipi temporali corretti
            empty = client.query_arrow(f"SELECT * FROM ({query}) AS _s LIMIT 0")
            yield pa.schema([
                pa.field(f.name, targets[i][0], nullable=f.nullable) if i in targets else f
                for i, f in enumerate(empty.schema)
            ])
    finally:
        client.close()


def _batches_mysql(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    import pymysql
    import pymysql.cursors

    c = pymysql.connect(
        host=conn.host,
        port=conn.port_or_default,
        user=conn.username,
        password=conn.resolve_password(),
        database=conn.database or None,
        cursorclass=pymysql.cursors.SSCursor,  # unbuffered: il server streama
        connect_timeout=10,
        charset="utf8mb4",
    )
    try:
        with c.cursor() as cur:
            cur.execute(query)
            yield from _dbapi_batches(cur)
    finally:
        c.close()


def _batches_trino(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    import trino

    password = conn.resolve_password()
    kwargs: dict[str, Any] = {
        "host": conn.host,
        "port": conn.port_or_default,
        "user": conn.username or "tabularia",
        "catalog": conn.database or None,
        "schema": conn.db_schema or "default",
    }
    if password:
        # il client Trino rifiuta la basic auth su http: con password si va in https
        kwargs["http_scheme"] = "https"
        kwargs["auth"] = trino.auth.BasicAuthentication(conn.username, password)
    c = trino.dbapi.connect(**kwargs)
    try:
        cur = c.cursor()
        cur.execute(query)
        yield from _dbapi_batches(cur)
    finally:
        c.close()


# ── Oracle ──────────────────────────────────────────────────────────────────
# python-oracledb in modalità Thin: puro Python, nessun Oracle Instant Client
# nell'immagine. `fetch_df_batches` legge dal server un blocco di righe alla volta
# e lo consegna come data frame Arrow (PyCapsule): niente righe Python di mezzo.
# Prima si DESCRIVE la query (WHERE 1 = 0), per due cose che vanno decise prima
# di leggere: i timestamp con fuso, che il driver consegnerebbe senza fuso (si
# portano in UTC sul server, lo standard dei motori sono istanti UTC naive), e i
# tipi che non arrivano in Arrow o in Parquet, rifiutati nominando le colonne.
_ORACLE_CON_FUSO = {"DB_TYPE_TIMESTAMP_TZ", "DB_TYPE_TIMESTAMP_LTZ"}
_ORACLE_RIFIUTATI = {
    "DB_TYPE_INTERVAL_DS", "DB_TYPE_INTERVAL_YM", "DB_TYPE_OBJECT", "DB_TYPE_XMLTYPE",
    "DB_TYPE_BFILE", "DB_TYPE_CURSOR",
}
# il nostro ORDER BY finale (solo identificatori quotati, a livello esterno: la SQL
# dell'utente sta sempre fra parentesi), da rimettere fuori dall'involucro
_ORDER_BY_FINALE = re.compile(r'^(.*) ORDER BY ((?:"[^"]+")(?:, "[^"]+")*)$', re.S)


def _oracle_tipo(t) -> str:
    return getattr(t, "name", str(t))


def _oracle_connect(conn: DbConnectionSpec):
    import oracledb

    if not conn.database:
        raise DbSourceError("Oracle: the connection needs the service name in the Database field (e.g. FREEPDB1, ORCLPDB1).")
    return oracledb.connect(
        user=conn.username,
        password=conn.resolve_password(),
        host=conn.host,
        port=conn.port_or_default,
        service_name=conn.database,
        tcp_connect_timeout=10,
    )


def _oracle_tipo_arrow(colonna) -> pa.DataType:
    """Il tipo Arrow di una colonna dalla sola descrizione: serve quando il risultato
    è vuoto e non c'è un batch da cui prenderlo. Stessa mappa del driver."""
    nome, precisione, scala = _oracle_tipo(colonna[1]), colonna[4], colonna[5]
    if nome == "DB_TYPE_NUMBER":
        return pa.int64() if scala == 0 and precisione and 0 < precisione <= 18 else pa.float64()
    if nome == "DB_TYPE_BINARY_FLOAT":
        return pa.float32()
    if nome == "DB_TYPE_BINARY_DOUBLE":
        return pa.float64()
    if nome == "DB_TYPE_BINARY_INTEGER":
        return pa.int64()
    if nome in ("DB_TYPE_DATE", "DB_TYPE_TIMESTAMP") or nome in _ORACLE_CON_FUSO:
        return pa.timestamp("us")
    if nome == "DB_TYPE_BOOLEAN":
        return pa.bool_()
    if nome in ("DB_TYPE_RAW", "DB_TYPE_LONG_RAW", "DB_TYPE_BLOB"):
        return pa.large_binary()
    if nome in ("DB_TYPE_CLOB", "DB_TYPE_NCLOB", "DB_TYPE_LONG"):
        return pa.large_string()
    return pa.string()


def _oracle_normalizza(tabella: pa.Table) -> pa.Table:
    """DATE arriva in secondi, TIMESTAMP(0..3) in secondi o millisecondi: tutto in
    microsecondi come gli altri driver (i nanosecondi di TIMESTAMP(7..9) restano)."""
    campi = []
    for f in tabella.schema:
        if pa.types.is_timestamp(f.type) and f.type.unit in ("s", "ms"):
            campi.append(pa.field(f.name, pa.timestamp("us"), f.nullable))
        else:
            campi.append(f)
    schema = pa.schema(campi)
    return tabella if schema.equals(tabella.schema) else tabella.cast(schema)


def _batches_oracle(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    c = _oracle_connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(f"SELECT * FROM ({query}) d WHERE 1 = 0")
            descrizione = list(cur.description or [])
        nomi = [d[0] for d in descrizione]
        if len(set(nomi)) != len(nomi):
            raise DbSourceError("Oracle: the query returns columns with the same name: give them distinct aliases (e.g. a.id AS a_id).")
        rifiutate = [f"{d[0]} ({_oracle_tipo(d[1]).removeprefix('DB_TYPE_')})" for d in descrizione if _oracle_tipo(d[1]) in _ORACLE_RIFIUTATI]
        if rifiutate:
            raise DbSourceError(
                "Oracle: these columns have a type that cannot be imported: " + ", ".join(rifiutate)
                + ". Convert them in a SQL source, e.g. TO_CHAR(col) or CAST(col AS VARCHAR2(4000))."
            )
        finale = query
        con_fuso = {d[0] for d in descrizione if _oracle_tipo(d[1]) in _ORACLE_CON_FUSO}
        if con_fuso:
            base, ordine = query, None
            m = _ORDER_BY_FINALE.match(query)
            if m:
                base, ordine = m.group(1), m.group(2)
            colonne = ", ".join(
                f'SYS_EXTRACT_UTC(o."{n}") AS "{n}"' if n in con_fuso else f'o."{n}"' for n in nomi
            )
            finale = f"SELECT {colonne} FROM ({base}) o" + (f" ORDER BY {ordine}" if ordine else "")

        primo = True
        for blocco in c.fetch_df_batches(statement=finale, size=BATCH_ROWS):
            tabella = _oracle_normalizza(pa.table(blocco))
            if tabella.num_rows == 0 and not primo:
                continue
            if primo:
                yield tabella.schema
                primo = False
            yield from tabella.to_batches()
        if primo:  # nessuna riga: lo schema dalla sola descrizione
            yield pa.schema([pa.field(d[0], _oracle_tipo_arrow(d)) for d in descrizione])
    finally:
        c.close()


# ── SQL Server ──────────────────────────────────────────────────────────────
# mssql-python, il driver ufficiale di Microsoft: porta con sé lo strato ODBC (DDBC),
# niente driver da installare. `arrow_reader` converte il risultato in Arrow nel suo
# C++ e lo consegna un blocco alla volta. I tipi si allineano agli altri driver:
# decimali interi fino a 18 cifre → Int64, gli altri decimali e money → Float64
# (come NUMBER di Oracle e numeric di Postgres); datetimeoffset arriva già in UTC e
# perde solo il fuso (lo standard dei motori: istanti UTC naive).
_TLS_SQLSERVER = {
    "verify": "Encrypt=yes;TrustServerCertificate=no",   # default: cifrato, certificato verificato
    "trust": "Encrypt=yes;TrustServerCertificate=yes",   # certificato autofirmato del server
    "none": "Encrypt=no",
}


def _odbc_valore(v: str) -> str:
    """Un valore di stringa di connessione ODBC: fra graffe, con le graffe chiuse
    raddoppiate, così `;` `=` e `}` in una password non rompono niente."""
    return "{" + str(v).replace("}", "}}") + "}"


def _sqlserver_connect(conn: DbConnectionSpec):
    import mssql_python

    tls = (conn.extra or {}).get("tls") or "verify"
    if tls not in _TLS_SQLSERVER:
        raise DbSourceError(f"SQL Server: unknown TLS option {tls!r} (verify, trust, none).")
    # un'istanza con nome (`host\\SQLEXPRESS`) si raggiunge senza porta (SQL Browser)
    server = conn.host if ("\\" in conn.host and not conn.port) else f"{conn.host},{conn.port_or_default}"
    parti = [f"Server={_odbc_valore(server)}", f"UID={_odbc_valore(conn.username)}", f"PWD={_odbc_valore(conn.resolve_password())}"]
    if conn.database:
        parti.append(f"Database={_odbc_valore(conn.database)}")
    stringa = ";".join(parti) + ";" + _TLS_SQLSERVER[tls]
    return mssql_python.connect(stringa, attrs_before={mssql_python.SQL_ATTR_LOGIN_TIMEOUT: 10})


def _sqlserver_tipo(t: pa.DataType) -> pa.DataType:
    if pa.types.is_decimal(t):
        return pa.int64() if t.scale == 0 and t.precision <= 18 else pa.float64()
    if pa.types.is_timestamp(t) and t.tz is not None:
        return pa.timestamp(t.unit)
    return t


def _sqlserver_schema(schema: pa.Schema) -> pa.Schema:
    return pa.schema([pa.field(f.name, _sqlserver_tipo(f.type), f.nullable) for f in schema])


def _batches_sqlserver(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    c = _sqlserver_connect(conn)
    try:
        cur = c.cursor()
        cur.execute(query)
        nomi = [d[0] for d in (cur.description or [])]
        if len(set(nomi)) != len(nomi):
            raise DbSourceError("SQL Server: the query returns columns with the same name: give them distinct aliases (e.g. a.id AS a_id).")
        with cur.arrow_reader(batch_size=BATCH_ROWS) as reader:
            schema = _sqlserver_schema(reader.schema)
            yield schema
            for batch in reader:
                yield batch if batch.schema.equals(schema) else batch.cast(schema)
    finally:
        c.close()


_DRIVERS = {
    "postgresql": _batches_postgresql,
    "mysql": _batches_mysql,
    "mariadb": _batches_mysql,  # protocollo condiviso: stesso driver
    "clickhouse": _batches_clickhouse,
    "trino": _batches_trino,
    "oracle": _batches_oracle,
    "sqlserver": _batches_sqlserver,
}


def _open_batches(conn: DbConnectionSpec, query: str, read_timeout: int | None = None) -> Iterator[Any]:
    # fra i driver delle sorgenti solo clickhouse-connect ha un tempo massimo di
    # lettura suo (vedi IngestSettings.db_read_timeout_seconds)
    driver = _DRIVERS[conn.db_type]
    if read_timeout and conn.db_type == "clickhouse":
        return driver(conn, query, read_timeout=read_timeout)
    return driver(conn, query)


# ─────────────────────────────────────────────────────────────────────────────
# Percorso DBAPI generico: righe Python → batch Arrow
# ─────────────────────────────────────────────────────────────────────────────
def _dbapi_batches(cursor, batch_rows: int = BATCH_ROWS) -> Iterator[Any]:
    """fetchmany → RecordBatch. Lo schema viene inferito dal PRIMO batch e poi
    imposto a tutti i successivi (il ParquetWriter non può cambiare schema in
    corsa): valori incompatibili più avanti producono un errore parlante."""
    names = [d[0] for d in (cursor.description or [])]
    if len(set(names)) != len(names):
        raise DbSourceError(
            "La query restituisce colonne con lo stesso nome: usa alias distinti (es. a.id AS a_id)"
        )

    rows = cursor.fetchmany(batch_rows)
    if not rows:
        # nessuna riga: il DBAPI non dà i tipi in modo affidabile → tutte stringhe
        yield pa.schema([pa.field(n, pa.string()) for n in names])
        return

    first = _rows_to_table(rows, names, schema=None)
    schema = _null_types_to_string(first.schema)
    yield schema
    yield from first.cast(schema).to_batches()

    while True:
        rows = cursor.fetchmany(batch_rows)
        if not rows:
            break
        yield from _rows_to_table(rows, names, schema=schema).to_batches()


def _rows_to_table(rows, names: list[str], schema: pa.Schema | None) -> pa.Table:
    pylist = [dict(zip(names, row)) for row in rows]
    try:
        return pa.Table.from_pylist(pylist, schema=schema)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise DbSourceError(
            f"Tipi non uniformi nel risultato: {e}. "
            "Suggerimento: forza il tipo con un CAST esplicito nella query."
        ) from e


def _null_types_to_string(schema: pa.Schema) -> pa.Schema:
    """Colonne tutte-NULL nel primo batch → string (il tipo null non regge
    valori veri nei batch successivi)."""
    fields = [
        pa.field(f.name, pa.string()) if pa.types.is_null(f.type) else f for f in schema
    ]
    return pa.schema(fields)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point: ingest completo verso parquet su storage
# ─────────────────────────────────────────────────────────────────────────────
def _guarded(gen: Iterator[Any], conn: DbConnectionSpec) -> Iterator[Any]:
    """Itera il generatore del driver traducendo i suoi errori nel messaggio del
    SERVER (vedi db_errors). Riguarda solo le chiamate al driver: un errore di
    scrittura del parquet o dello storage resta quello originale. La causa
    originale resta agganciata (`from e`) e finisce nel traceback del run."""
    while True:
        try:
            item = next(gen)
        except StopIteration:
            return
        except IngestError:
            raise
        except Exception as e:
            raise DbSourceError(describe_db_error(e, conn.db_type, conn.host, conn.port_or_default)) from e
        yield item


def ingest_db_to_parquet(
    conn: DbConnectionSpec,
    source: DbSourceSpec,
    bucket: str,
    key: str,
    storage=None,
    progress: Callable[..., None] | None = None,
) -> dict:
    """Esegue la sorgente e scrive `bucket/key` (parquet). Ritorna righe e schema.

    `progress(fase, rows=…)`, per chi guarda l'import: «waiting» finché il database
    non manda la prima riga (connessione, calcolo, ordinamento), «reading» a ogni
    batch col conto delle righe, «saving» mentre il parquet va sullo storage."""
    avanza = progress or (lambda *a, **k: None)
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()

    query = build_query(conn, source)
    logger.info("db-ingest %s@%s → %s (%s)", conn.db_type, conn.host, key, query[:200])

    # errori del driver → messaggio del server del database, non l'eccezione Python
    gen = _guarded(_open_batches(conn, query, get_settings().ingest.db_read_timeout_seconds), conn)
    avanza("waiting")
    try:
        schema = next(gen)
    except StopIteration:  # nessun driver arriva qui, ma il contratto va difeso
        raise DbSourceError("Il driver non ha restituito uno schema")

    fd, tmp = tempfile.mkstemp(suffix=".parquet", prefix="db_ingest_")
    os.close(fd)
    try:
        rows = 0
        # I batch del driver sono piccoli (~8k righe) e `write_batch` scrive UN
        # ROW GROUP PER BATCH: cosi' un parquet da 25M righe finiva con 3.063 row
        # group e un footer di 16,6 MB. Con blocchi tanto piccoli il motore non
        # puo' leggere solo le colonne che servono — sarebbero migliaia di
        # richieste da poche decine di KB — e finisce per scaricare tutto il file.
        # Qui i batch si ACCUMULANO fino alla soglia e si scrivono insieme.
        # Il blocco si scrive di ESATTAMENTE `soglia` righe e il resto passa al
        # blocco dopo: un batch del driver piu' grande della soglia (Postgres ne
        # consegna anche decine di migliaia insieme) usciva come un row group
        # solo, e uno appena sopra lasciava ogni volta un row group minuscolo.
        soglia = max(1, get_settings().ingest.parquet_row_group_rows)
        with pq.ParquetWriter(tmp, schema, compression="zstd") as writer:
            blocco: list = []
            in_blocco = 0

            def _scarica(fine: bool = False) -> None:
                nonlocal blocco, in_blocco
                if not blocco:
                    return
                tabella = pa.Table.from_batches(blocco, schema)
                while tabella.num_rows >= soglia or (fine and tabella.num_rows):
                    n = min(soglia, tabella.num_rows)
                    writer.write_table(tabella.slice(0, n), row_group_size=n)
                    tabella = tabella.slice(n)
                blocco, in_blocco = tabella.to_batches(), tabella.num_rows

            for batch in gen:
                blocco.append(batch)
                in_blocco += batch.num_rows
                rows += batch.num_rows
                avanza("reading", rows=rows)
                if in_blocco >= soglia:
                    _scarica()
            _scarica(fine=True)  # l'ultimo blocco, quasi sempre piu' corto della soglia

        avanza("saving", rows=rows)
        storage.create_bucket(bucket)
        storage.upload_file(tmp, bucket, key)

        # dtype come stringhe Polars: coerenti col resto del catalogo/frontend
        scan_schema = pl.scan_parquet(tmp).collect_schema()
        columns = [{"name": n, "dtype": str(t)} for n, t in scan_schema.items()]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    logger.info("db-ingest completato: %s (%d righe, %d colonne)", key, rows, len(columns))
    return {"rows_written": rows, "columns": columns}


# ─────────────────────────────────────────────────────────────────────────────
# Ispezione: test di connessione e lista tabelle (per la UI)
# ─────────────────────────────────────────────────────────────────────────────
_TABLES_QUERY = {
    "postgresql": (
        "SELECT table_schema || '.' || table_name AS t FROM information_schema.tables "
        "WHERE table_type IN ('BASE TABLE', 'VIEW') "
        "AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"
    ),
    "mysql": (
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY 1"
    ),
    "mariadb": (
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY 1"
    ),
    "clickhouse": "SELECT name AS t FROM system.tables WHERE database = currentDatabase() ORDER BY 1",
    "trino": (
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = current_schema ORDER BY 1"
    ),
    # tabelle e viste degli schemi non di sistema (Oracle 12c+: oracle_maintained),
    # nella forma che build_query rilegge: maiuscolo semplice nudo, il resto fra
    # virgolette
    "oracle": (
        "SELECT CASE WHEN REGEXP_LIKE(owner, '^[A-Z][A-Z0-9_$#]*$') THEN owner ELSE '\"' || owner || '\"' END"
        " || '.' || CASE WHEN REGEXP_LIKE(nome, '^[A-Z][A-Z0-9_$#]*$') THEN nome ELSE '\"' || nome || '\"' END AS t"
        " FROM (SELECT owner, table_name AS nome FROM all_tables WHERE dropped = 'NO'"
        " UNION ALL SELECT owner, view_name FROM all_views)"
        " WHERE owner IN (SELECT username FROM all_users WHERE oracle_maintained = 'N') ORDER BY 1"
    ),
    # tabelle e viste del database della connessione; un nome con caratteri speciali
    # torna fra parentesi quadre, la forma che build_query rilegge
    "sqlserver": (
        "SELECT CASE WHEN TABLE_SCHEMA LIKE '%[^A-Za-z0-9_]%' THEN QUOTENAME(TABLE_SCHEMA) ELSE TABLE_SCHEMA END"
        " + '.' + CASE WHEN TABLE_NAME LIKE '%[^A-Za-z0-9_]%' OR TABLE_NAME LIKE '[0-9]%' THEN QUOTENAME(TABLE_NAME) ELSE TABLE_NAME END AS t"
        " FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW')"
        " AND TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA') ORDER BY 1"
    ),
}


def _collect_first_column(conn: DbConnectionSpec, query: str) -> list[str]:
    gen = _open_batches(conn, query)
    next(gen)  # schema
    out: list[str] = []
    for batch in gen:
        out.extend(str(v) for v in batch.column(0).to_pylist())
    return out


def test_connection(conn: DbConnectionSpec) -> None:
    """Apre la connessione ed esegue una query banale sullo stesso percorso
    dell'ingest: se non solleva, la connessione è utilizzabile."""
    _collect_first_column(conn, "SELECT 1 AS ok FROM DUAL" if conn.db_type == "oracle" else "SELECT 1 AS ok")


def list_tables(conn: DbConnectionSpec) -> list[str]:
    return _collect_first_column(conn, _TABLES_QUERY[conn.db_type])
