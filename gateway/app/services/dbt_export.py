"""Prepara il payload di export dbt di un flusso: ciò che l'engine compila in un
progetto dbt (modelli, sorgenti, seed, schema.yml, test).

Che cosa decide QUI il gateway (che conosce il catalogo) e cosa l'engine (che
conosce il SQL):
· ogni nodo Output → un MODELLO, materializzato secondo il modo di scrittura
  (`table`; `incremental` in append per le tabelle DB in append);
· i passi che più uscite hanno in comune → un modello INTERMEDIO (`ephemeral`)
  referenziato con `ref()`: una sola definizione, come nell'editor;
· una sorgente da TABELLA → `source()` (sources.yml, con le descrizioni delle
  colonne); da QUERY SQL → un modello ephemeral con la query; l'uscita di un
  ALTRO flusso → i modelli di quel flusso, inclusi nel progetto (chiusura delle
  dipendenze); un FILE (caricato nell'editor, importato) → un SEED CSV;
· le colonne e le descrizioni della datasource pubblicata → schema.yml; le regole
  del suo DATA CONTRACT → test dbt (generici sulle colonne, singolari per il resto).

Due modalità: `duckdb` (dbt-duckdb FEDERA i DB di origine: postgresql/mysql) e
`native` (dbt gira nel warehouse di origine, tutte le sorgenti sulla stessa
connessione; SQL tradotto via sqlglot dall'engine).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from typing import Callable, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, func, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models import Connection, Datasource, Flow, FlowVersion, Project, User
from app.services import contracts as contract_service
from app.services.flow_resolver import FlowResolveError, resolve_output_chains

logger = logging.getLogger(__name__)

# db_type federabili da DuckDB (scanner ufficiali) + porta di default + funzione di query remota
_FEDERABLE = {"postgresql": (5432, "postgres_query"), "mysql": (3306, "mysql_query"), "mariadb": (3306, "mysql_query")}
# default port of each database dbt or ClickHouse connects to
_PORTA = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "clickhouse": 8123}
# db_type with a NATIVE dbt adapter (SQL translated by sqlglot). Not MySQL/MariaDB: the only dbt-mysql
# release needs dbt-core 1.7, which silently drops the project's tests; the federated and the ClickHouse
# targets read them live.
_NATIVE = ("postgresql", "clickhouse")
# what the ClickHouse target reads: Postgres/MySQL live through a table engine, and its own server's tables
_CLICKHOUSE_LEGGE = ("postgresql", "mysql", "mariadb", "clickhouse")
# what some dbt target reads live: the other types (Oracle, SQL Server, Trino…) are sources for Tabularia, not for dbt
_LEGGIBILI = frozenset(_FEDERABLE) | frozenset(_NATIVE) | frozenset(_CLICKHOUSE_LEGGE)
# the engine's cap on a seed (SEED_MAX_ROWS in backend/app/engine/dbt_export.py): the plan says it before
# the download, instead of the engine refusing after fetching the whole file
SEED_MAX_ROWS = 50_000
# the flow's engine → the dialect of its free text (compute expressions, sql nodes): the engine translates from it
_DIALETTO = {"polars": "polars", "duckdb": "duckdb", "chdb": "clickhouse", "clickhouse": "clickhouse", "bigquery": "bigquery"}
# model names: Postgres cuts identifiers at 63 bytes and dbt-postgres refuses a longer relation name. The
# dialog's prefix (at most 20 characters) goes in front of a name capped without it, so the dialog's
# preview `prefix + name` is exact; an ephemeral model becomes a CTE named `__dbt__cte__<name>`.
MAX_IDENTIFICATORE = 63
_MAX_PREFISSO = 20
_CTE = len("__dbt__cte__")
MAX_UPSTREAM_DEPTH = 8          # flussi a monte inclusi, al più
# what a dbt adapter cannot have in a name, by where the name is (the engine's _NON_QUOTABILI, same
# roles): dbt writes `<quote>name<quote>` without escaping (BaseRelation.quoted), and ClickHouse reads
# backslash escapes between backticks — every role. A model's OWN table (relation) and schema are also
# written between `'`, unescaped, in the queries that list a schema's relations and a table's columns,
# and dbt-postgres compares the schema with ILIKE, where a backslash escapes; a source name with
# a ' is refused too, as the engine does. The plan says it, instead of the download's 422 (or the
# team's `dbt run`).
_NON_QUOTABILI = {
    "dbt-postgres": {"source": "\"'", "relation": "\"'", "schema": "\"'\\"},
    "dbt-duckdb": {"source": "\"'", "relation": '"', "schema": '"'},
    "dbt-clickhouse": {"source": "`\\'", "relation": "`\\'", "schema": "`\\'"},
}
# regole di contratto → test dbt
_COLUMN_TESTS = {"not_null", "unique", "accepted_values"}
_MODEL_TESTS = {"range", "pattern", "row_count", "expression"}


class DbtExportError(ValueError):
    """The export refused. The English text stays in the logs; `code` and `params`
    say it in the reader's language (services/messaggi_dbt.py)."""

    # the sources a target needs declared by the team's own project (folder package) to work: the plan offers it there
    da_dichiarare: tuple[str, ...] = ()

    def __init__(self, messaggio: str, code: str = "internal", **params):
        super().__init__(messaggio)
        self.code = code
        self.params = params


def _slug(s: str) -> str:
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(s).lower()).strip("_")
    out = out or "x"
    return out if not out[0].isdigit() else f"m_{out}"


def _entro(nome: str, tetto: int) -> str:
    """`nome` within `tetto` bytes: a longer one keeps its start and ends with a short
    hash of the whole, so two long names stay two names."""
    if len(nome.encode()) <= tetto:
        return nome
    impronta = hashlib.sha256(nome.encode()).hexdigest()[:8]
    testa = nome.encode()[: tetto - len(impronta) - 1].decode("utf-8", "ignore").rstrip("_")
    return f"{testa}_{impronta}"


def _cartella_predefinita(nome: str) -> str:
    """The folder of the existing-project package when the dialog leaves it empty: one
    OpzioniDbt.folder accepts (ASCII a-z0-9_, at most 40), accents transliterated."""
    ascii_ = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", ascii_).strip("_")[:40].rstrip("_") or "tabularia"


def _schema_predefinito(conn: Connection) -> str:
    """The schema of a one-part table name, as Tabularia reads and writes it
    (ingest/db_source.build_query, ingest/db_destination._qualified_table): the
    connection's schema, else the server's default."""
    return conn.db_schema or (conn.database if conn.db_type in ("clickhouse", "mysql", "mariadb") else "public")


def _come_postgres(nome: str) -> str:
    """The identifier Postgres keeps of `nome`: its first 63 bytes, whole characters. A
    longer table name is created that way when Tabularia writes it, and dbt-postgres
    refuses a longer model relation."""
    return nome.encode()[:MAX_IDENTIFICATORE].decode("utf-8", "ignore")


def _ricostruibile(alias: str) -> bool:
    """Whether dbt-postgres can rebuild a table named `alias` (a 63-byte name at most). It
    builds `<alias>__dbt_tmp` and keeps `<alias>__dbt_backup`, cutting the alias to fit 63
    CHARACTERS, and Postgres cuts names to 63 BYTES: with multibyte characters the suffix
    is cut away and the temporary table is the table itself («already exists», seen with
    real dbt-postgres 1.11)."""
    return all(len((alias[: MAX_IDENTIFICATORE - len(s)] + s).encode()) <= MAX_IDENTIFICATORE for s in ("__dbt_tmp", "__dbt_backup"))


def _quotabile(nome: str, adapter: str, dove: str) -> None:
    """Refuses a name `adapter` would write into SQL its database rejects (seen with real
    dbt: «Cannot parse escape sequence» on ClickHouse, «zero-length delimited identifier»
    on DuckDB, an unclosed `'` in dbt's own queries): the plan says it, not the team's
    `dbt run`. `dove` is source (a source's schema or table), relation (a model's own
    table) or schema (a model's own schema), as in `_NON_QUOTABILI`."""
    for car in _NON_QUOTABILI[adapter][dove]:
        if car in nome:
            raise DbtExportError(f"the name «{nome}» has the character {car}, which {adapter} cannot quote in a relation name",
                                 "name_not_quotable", name=nome, character=car, adapter=adapter)


def _parse_ref(source_ref: str, default_schema: str) -> tuple[str, str]:
    """`schema.table` → (schema, table); `table` → (default_schema, table)."""
    parts = (source_ref or "").split(".")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return default_schema, parts[-1] if parts else source_ref


def _columns(ds: Datasource | None) -> list[str]:
    if ds is None:
        return []
    return [c.get("name") for c in json.loads(ds.columns or "[]") if c.get("name")]


def _dtypes(ds: Datasource | None) -> dict[str, str]:
    """{colonna: dtype di Polars} con cui Tabularia ha letto la datasource: il
    progetto dbt la vedrà con questi tipi, come il flusso."""
    if ds is None:
        return {}
    return {c.get("name"): c.get("dtype") for c in json.loads(ds.columns or "[]") if c.get("name")}


def _descriptions(ds: Datasource | None) -> dict[str, str]:
    if ds is None:
        return {}
    try:
        d = json.loads(ds.column_descriptions or "{}")
    except ValueError:
        return {}
    return {k: str(v) for k, v in d.items() if v}


class OpzioneSorgente(BaseModel):
    """Una sorgente (connessione, schema) come la vuole il progetto del team data."""
    name: Optional[str] = Field(None, pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    declared: bool = False       # già nel loro sources.yml: niente voce nel nostro (dbt rifiuta i doppioni)


class OpzioniDbt(BaseModel):
    """Le scelte del dialogo di download (solo amministratori). Ogni valore libero
    finisce in un file YAML, SQL o Jinja: forme strette, niente da iniettare.
    I valori di default danno lo stesso progetto del download senza opzioni."""
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target: Literal["clickhouse", "duckdb", "native"] = "duckdb"
    package: Literal["project", "folder"] = "project"     # progetto completo | cartella per un progetto esistente
    folder: str = Field("", pattern=r"^[a-z0-9_]{0,40}$")
    prefix: str = Field("", pattern=r"^([a-z][a-z0-9_]{0,19})?$")
    layers: bool = False                                  # staging / intermediate / marts
    sources: dict[str, OpzioneSorgente] = {}              # "<connessione>:<schema>" → nome, già dichiarata
    schema_: Optional[str] = Field(None, alias="schema", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    materializations: dict[str, Literal["table", "view", "incremental"]] = {}   # "<flusso>:<nodo>" → materializzazione
    tests: bool = True                                    # i test che vengono dal data contract
    emails: bool = True                                   # chi ha costruito ed esportato il flusso, nei meta
    ai_descriptions: bool = False                         # descrizioni scritte dall'AI dove mancano (services/dbt_ai.py)
    ai_translations: bool = False                         # proposta dell'AI dove sqlglot non traduce, verificata


def chiave_sorgente(conn_id: int, schema: str) -> str:
    return f"{conn_id}:{schema}"


def chiave_uscita(flow_id: int, node_id: str) -> str:
    return f"{flow_id}:{node_id}"


class _Nomi:
    """Nomi di modello univoci nel progetto (dbt li vuole unici). Il prefisso del
    dialogo (`tab_`) vale per modelli e seed: tiene i nomi lontani da quelli del team.
    The name is made (slug, uniqueness, length cap) WITHOUT the prefix, which then goes
    in front: a prefixed name is always the prefix plus the name the plan shows."""

    def __init__(self, prefisso: str = ""):
        self.usati: set[str] = set()
        self.prefisso = prefisso

    def nuovo(self, base: str, suffisso: str = "", effimero: bool = False) -> str:
        tetto = MAX_IDENTIFICATORE - _MAX_PREFISSO - (_CTE if effimero else 0)
        nome = _entro(_slug(base), tetto)
        if nome in self.usati and suffisso:
            nome = _entro(_slug(f"{base}_{suffisso}"), tetto)
        n, cand = 2, nome
        while cand in self.usati:
            cand = _entro(f"{nome}_{n}", tetto)
            n += 1
        self.usati.add(cand)
        return f"{self.prefisso}{cand}"


class _Progetto:
    """Lo stato dell'export mentre si attraversano il flusso e i flussi a monte."""

    def __init__(self, session: Session, default_bucket: str, target: str,
                 esportato_da: str | None = None, opzioni: OpzioniDbt | None = None):
        self.session = session
        self.bucket = default_bucket
        self.target = target
        self.opzioni = _secondo_il_pacchetto(opzioni or OpzioniDbt(target=target))
        self.uscite: list[dict] = []          # per il dialogo: ogni uscita, la sua chiave e le materializzazioni possibili
        # chi esporta: nel README (e non nei modelli, che restano uguali da un export all'altro)
        self.esportato_da = esportato_da if (opzioni is None or opzioni.emails) else None
        self.nomi = _Nomi(self.opzioni.prefix)
        self.models: list[dict] = []          # in ordine di dipendenza
        self.seeds: list[dict] = []
        self.tables: dict[str, tuple[Datasource, Connection, str, str]] = {}   # key → (ds, conn, schema, table)
        self.flussi_visti: set[int] = set()
        self.note: list[str] = []
        self.nomi_file: dict[str, str] = {}   # chiave parquet di un file caricato → nome del file (dal nodo)
        # key di una sorgente → come la si referenzia: {"key"} (tabella), {"model"} (query/flusso a monte), {"seed"}
        self.riferimenti: dict[str, dict] = {}
        self.radice: int | None = None        # the exported flow
        # model → the database table an output of the exported flow writes: the native target decides
        # where the model lands once it knows the warehouse dbt runs on (_native_payload)
        self.destinazioni: dict[str, dict] = {}
        # outputs, by (flow id, node id): an upstream flow brings in only the outputs read downstream,
        # one at a time, so the cycle check is on outputs, not on flows
        self._contesti: dict[int, dict] = {}
        self._in_corso: set[tuple[int, str]] = set()    # their sources are being resolved
        self._in_attesa: dict[tuple[int, str], dict] = {}   # sources resolved, model not written yet
        self._fatte: dict[tuple[int, str], str] = {}    # → the model's name
        # one-part table names on a Postgres connection without Schema, by connection: dbt names them in
        # `public`, Tabularia resolves them through the role's search_path (_note_senza_schema)
        self.senza_schema: dict[int, tuple[Connection, list[str]]] = {}
        # (connection, schema) → the default source name before a collision moves it aside (`_2`): the
        # dialog names untyped sources from it, as _nomi_sorgenti does
        self.basi_sorgenti: dict[tuple[int, str], str] = {}

    def layout(self) -> dict:
        o = self.opzioni
        return {"package": o.package, "folder": o.folder, "layers": o.layers}

    # ── tracciabilità ────────────────────────────────────────────────────────
    def _origine(self, flow: Flow) -> dict:
        """Da quale flusso viene un modello: il team data, davanti a un modello,
        risale al flusso nell'app (id, versione esportata, cartella, autore)."""
        ultima = self.session.exec(select(FlowVersion).where(FlowVersion.flow_id == flow.id)
                                   .order_by(FlowVersion.version.desc()).limit(1)).first()
        cartella = self.session.get(Project, flow.project_id)
        autore = self.session.get(User, flow.owner_id) if flow.owner_id else None
        return {"flow": flow.name, "flow_id": flow.id, "flow_version": ultima.version if ultima else None,
                "version_saved_at": ultima.created_at.strftime("%Y-%m-%d %H:%M UTC") if ultima and ultima.created_at else None,
                "folder": cartella.name if cartella else None, "owner": autore.email if autore else None}

    def _traccia(self, modello: dict, origine: dict, **extra) -> dict:
        # solo fatti del FLUSSO (versione, quando è stata salvata, autore): riesportare la stessa
        # versione dà gli stessi file. Chi esporta sta nel solo README.
        modello["meta"] = {**origine, **{k: v for k, v in extra.items() if v}}
        if not self.opzioni.emails:
            modello["meta"].pop("owner", None)
        modello["tags"] = ["tabularia", _slug(origine["flow"])] if origine.get("flow") else ["tabularia"]
        return modello

    # ── sorgenti ─────────────────────────────────────────────────────────────
    def sorgente(self, key: str, profondita: int) -> dict:
        """Classifica una chiave parquet: tabella DB, query SQL, uscita di un altro
        flusso o file → e torna il riferimento da usare nei modelli."""
        if key in self.riferimenti:
            return self.riferimenti[key]
        ds = self.session.exec(select(Datasource).where(Datasource.key == key)).first()
        if ds is not None and ds.kind == "database" and ds.source_type in ("sharepoint", "sftp", "api", "s3"):
            # files (Excel on SharePoint, a folder on an SFTP server or in a bucket) or an
            # HTTP API: for dbt there is no database to read them from → seed
            rif = {"seed": self._seed(ds, key)}
        elif ds is not None and ds.kind == "database":
            conn = self.session.get(Connection, ds.connection_id) if ds.connection_id else None
            if conn is None:
                raise DbtExportError(f"the source «{ds.name}» has no valid connection", "source_no_connection", source=ds.name)
            if ds.source_type == "sql":
                rif = {"model": self._modello_query(ds, conn)}
            else:
                schema, table = _parse_ref(ds.source_ref, _schema_predefinito(conn))
                if "." not in (ds.source_ref or ""):
                    self.senza_schema_di(conn, table)
                self.tables[key] = (ds, conn, schema, table)
                rif = {"key": key}
        elif ds is not None and ds.kind == "flow" and ds.flow_id:
            rif = {"model": self._flusso_a_monte(ds, profondita)}
        else:
            rif = {"seed": self._seed(ds, key)}
        self.riferimenti[key] = rif
        return rif

    def senza_schema_di(self, conn: Connection, tabella: str) -> None:
        """A one-part table name: on Postgres, with no Schema on the connection, it is
        `public.<table>` for dbt but what the role's search_path finds for Tabularia."""
        if conn.db_type == "postgresql" and not conn.db_schema:
            tabelle = self.senza_schema.setdefault(conn.id, (conn, []))[1]
            if tabella not in tabelle:
                tabelle.append(tabella)

    def note_senza_schema(self) -> None:
        for conn, tabelle in self.senza_schema.values():
            elenco = ", ".join(f"`{t}`" for t in tabelle)
            self.note.append(
                f"The connection «{conn.name}» has no Schema: Tabularia reads and writes {elenco} through the search_path of "
                "the database role (by default a schema named like the role, when there is one, then `public`), while this "
                "project names them in `public`. They are the same tables unless the role has a schema of its own or another "
                "search_path: then set the Schema of the connection in Tabularia and export again.")

    def _modello_query(self, ds: Datasource, conn: Connection) -> str:
        """Una datasource da QUERY: un modello ephemeral con la query. In federazione
        la query gira nel DB di origine tramite la funzione di query remota di DuckDB;
        in nativo è già nel dialetto del warehouse."""
        nome = self.nomi.nuovo(f"src_{ds.name}", effimero=True)
        if self.target == "duckdb":
            if conn.db_type not in _FEDERABLE:
                raise _non_federabile(conn)
            self.tables.setdefault(f"__conn_{conn.id}", (ds, conn, "", ""))   # serve l'ATTACH anche senza tabelle
            funzione = _FEDERABLE[conn.db_type][1]
            query = (ds.source_ref or "").strip().rstrip(";").replace("'", "''")
            sql = f"SELECT * FROM {funzione}('db_{conn.id}', '{query}')"
        else:
            self.tables.setdefault(f"__conn_{conn.id}", (ds, conn, "", ""))
            sql = (ds.source_ref or "").strip().rstrip(";")
            if self.target == "native":
                self.note.append(f"The model `{nome}` is the SQL query of the datasource «{ds.name}», as written: its table references are not `source()` macros.")
        # target ClickHouse: la query è scritta per l'origine, l'engine la traduce in ClickHouse
        tradurre = {"connection_id": conn.id, "label": ds.name} if self.target == "clickhouse" else None
        self.models.append(self._traccia({
            "name": nome, "kind": "raw", "materialized": "ephemeral", "raw_sql": sql, "translate": tradurre,
            "columns": [{"name": c, "description": _descriptions(ds).get(c, ""), "dtype": _dtypes(ds).get(c)} for c in _columns(ds)],
            "description": f"Datasource «{ds.name}»: a SQL query on {conn.name}.",
            "tests": [],
        }, {"datasource": ds.name, "datasource_id": ds.id}))
        return nome

    def _flusso_a_monte(self, ds: Datasource, profondita: int) -> str:
        """L'uscita di un altro flusso: entra nel progetto solo il ramo letto qui — il
        modello dell'Output di QUEL flusso che pubblica questa datasource, con la sua
        catena a monte. The flow's other outputs stay out: exporting a flow never writes
        into another flow's tables (decision of 2026-10-16)."""
        flow = self.session.get(Flow, ds.flow_id)
        if flow is None:
            raise DbtExportError(f"the source «{ds.name}» is the output of a flow that no longer exists", "upstream_missing", source=ds.name)
        if profondita >= MAX_UPSTREAM_DEPTH:
            raise DbtExportError(f"too many upstream flows in a chain (more than {MAX_UPSTREAM_DEPTH}): the source «{ds.name}» cannot be included",
                                 "upstream_too_deep", max=MAX_UPSTREAM_DEPTH, source=ds.name)
        ctx = self._contesto(flow)
        pubblicate = ctx["pubblicate"]
        nodo = pubblicate.get((ds.name, ds.project_id))
        if nodo is None:
            # renamed or moved after the flow published it (the Output still writes the old name), or the
            # Output changed: running the flow again would not fix it
            elenco = ", ".join(dict.fromkeys(f"«{nome}»" for nome, _ in pubblicate)) or "—"
            raise DbtExportError(f"the flow «{flow.name}» has no output that publishes «{ds.name}» (it publishes {elenco}): the datasource "
                                 "was renamed or moved after the flow published it, or the flow's Output changed",
                                 "upstream_no_output", flow=flow.name, source=ds.name, published=elenco)
        if (ds.name, ds.project_id) in ctx["doppie"]:
            # which one wrote it last depends on the order they ran in: the project would carry one of the
            # two branches, maybe not the one whose data the downstream flow read
            raise DbtExportError(f"in the flow «{flow.name}» more than one Output publishes «{ds.name}»", "upstream_output_ambiguous",
                                 flow=flow.name, source=ds.name)
        chiave = (flow.id, nodo)
        if chiave in self._fatte:
            return self._fatte[chiave]
        if chiave in self._in_corso:
            raise DbtExportError(f"cycle between flows: «{flow.name}» depends on itself through «{ds.name}»", "flow_cycle", flow=flow.name, source=ds.name)
        if chiave in self._in_attesa:
            # its sources are resolved, its flow is still being included: its model is written now,
            # before the model that reads it (B: X1 and Y → X2, C: X1 → Y is not a cycle)
            self._scrivi_modelli(flow, [self._in_attesa[chiave]])
        else:
            self._includi(flow, profondita + 1, [nodo])
        return self._fatte[chiave]

    def _seed(self, ds: Datasource | None, key: str) -> str:
        if ds is not None and ds.rows is not None and ds.rows > SEED_MAX_ROWS:
            raise DbtExportError(
                f"the source «{ds.name}» has {ds.rows:,} rows: too many for a dbt seed (at most {SEED_MAX_ROWS:,}). "
                "Import it into a database and use that as the source.",
                "seed_too_large", source=ds.name, rows=f"{ds.rows:,}", max=f"{SEED_MAX_ROWS:,}",
            )
        base = ds.name if ds is not None else (self.nomi_file.get(key) or key.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
        nome = self.nomi.nuovo(f"seed_{base}")
        self.seeds.append({
            "name": nome, "bucket": (ds.bucket if ds is not None and ds.bucket else self.bucket), "key": key,
            "datasource_id": ds.id if ds is not None else None,
            "description": (f"Datasource «{ds.name}»" if ds is not None else f"File {self.nomi_file.get(key) or key}") + ", exported as a CSV seed: the data is a snapshot, not a live source.",
            "columns": [{"name": c, "description": _descriptions(ds).get(c, "")} for c in _columns(ds)],
        })
        self.note.append(f"The seed `{nome}` is a copy of the data at export time: reload it (`dbt seed`) when it changes.")
        return nome

    # ── flussi ───────────────────────────────────────────────────────────────
    def includi_flusso(self, flow: Flow, profondita: int = 0) -> None:
        """Il flusso esportato: un modello per Output, più gli intermedi condivisi."""
        if self.radice is None:
            self.radice = flow.id
        self._includi(flow, profondita, None)

    def _contesto(self, flow: Flow) -> dict:
        """What the export needs of a flow, read once: its definition and nodes, where
        it comes from, the dialect of its free text, and which Output publishes each
        (datasource name, folder)."""
        if flow.id not in self._contesti:
            try:
                definition = json.loads(flow.definition or "{}")
                by_id = {n["id"]: n for n in definition.get("nodes") or []}
                pubblicate: dict[tuple, str] = {}
                doppie: set[tuple] = set()      # published by two Outputs or more
                for n in by_id.values():
                    data = n.get("data") or {}
                    if n.get("type") == "source" and data.get("parquetKey") and data.get("filename"):
                        self.nomi_file[data["parquetKey"]] = str(data["filename"])
                    if n.get("type") == "output" and (data.get("destType") or "datasource") == "datasource" and (data.get("name") or "").strip():
                        chiave = ((data.get("name") or "").strip(), data.get("projectId"))
                        if chiave in pubblicate:
                            doppie.add(chiave)
                        pubblicate.setdefault(chiave, n["id"])
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                raise _definizione_non_valida(flow, e) from e
            self._contesti[flow.id] = {"definition": definition, "by_id": by_id, "pubblicate": pubblicate, "doppie": doppie,
                                       "suffisso": _slug(flow.name), "origine": self._origine(flow),
                                       "dialetto": _DIALETTO.get(flow.production_engine or flow.engine)}
        return self._contesti[flow.id]

    def _includi(self, flow: Flow, profondita: int, nodi: list[str] | None) -> None:
        """The models of the flow's Outputs (every one, or only `nodi`): first their
        sources, then one model per Output plus the steps they share."""
        self.flussi_visti.add(flow.id)
        ctx = self._contesto(flow)
        self._senza_script(flow, ctx["definition"], nodi)

        def resolve_ds(ds_id: int) -> Optional[tuple[str, str]]:
            try:
                ds = self.session.get(Datasource, int(ds_id))
            except (TypeError, ValueError):
                return None
            return (ds.bucket, ds.key) if ds and ds.key else None

        try:
            catene = resolve_output_chains(ctx["definition"], resolve_ds, self.bucket, node_ids=nodi)
        except FlowResolveError as e:
            raise _errore_di_lettura(flow, e)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise _definizione_non_valida(flow, e) from e

        uscite, esportabili = [], 0
        for c in catene:
            req, node = c["body"], c["node"]
            dest = req.get("destination") or {}
            if req.get("email") or dest.get("type") not in (None, "database", "s3", "sftp"):
                continue   # un'email non è un dataset: nessun modello
            esportabili += 1
            chiave = (flow.id, (node or {}).get("id"))
            if chiave in self._fatte or chiave in self._in_attesa or chiave in self._in_corso:
                continue   # already in the project, or on its way there
            key = req["input_key"]
            self._in_corso.add(chiave)
            rif = self.sorgente(key, profondita)
            # le sorgenti dei rami destri (join/union) vanno risolte anche loro
            self._sorgenti_annidate(req.get("operations") or [], profondita)
            self._in_corso.discard(chiave)
            ops = list(req.get("operations") or [])
            ids = list(c["op_ids"])
            pub_keys = [k.strip() for k in ((req.get("publish") or {}).get("sort_keys") or []) if k and k.strip()]
            if pub_keys:
                # l'ORDER BY del publish lo inietta il gateway al lancio: senza, la tabella
                # dbt sarebbe fisicamente diversa da quella che l'app pubblica
                ops.append({"type": "sort", "params": {"by": pub_keys, "ignore_missing": True}})
                ids.append(None)
            u = {"req": req, "node": node, "rif": rif, "key": key, "ops": ops, "ids": ids}
            self._in_attesa[chiave] = u
            uscite.append(u)
        if nodi is None and not esportabili:
            raise DbtExportError(f"the flow «{flow.name}» has no exportable outputs (only email?)", "no_outputs", flow=flow.name)
        # an output another flow needed meanwhile already has its model
        self._scrivi_modelli(flow, [u for u in uscite if (flow.id, (u["node"] or {}).get("id")) in self._in_attesa])

    def _senza_script(self, flow: Flow, definition: dict, nodi: list[str] | None) -> None:
        """Python, R and images are not dbt: an Output that reads what a Script or a
        Container node writes, however far down, refuses the export, saying which. One
        no exported Output reads from (a step that runs something elsewhere) is left
        out, with a note."""
        from app.services.flow_resolver import STEP_NODE_TYPES, _scripts_upstream

        nodes = {n["id"]: n for n in definition.get("nodes") or [] if isinstance(n, dict) and "id" in n}
        scripts = {i for i, n in nodes.items() if n.get("type") in STEP_NODE_TYPES}
        if not scripts:
            return
        edges = definition.get("edges") or []
        outputs = [n for n in nodes.values() if n.get("type") == "output" and (nodi is None or n["id"] in nodi)]
        feeding: set[str] = set()
        for out in outputs:
            upstream = _scripts_upstream(out["id"], nodes, edges)
            if upstream:
                script = nodes[sorted(upstream)[0]]
                label = str((script.get("data") or {}).get("label") or script["id"])
                d = out.get("data") or {}
                name = str(d.get("table") or d.get("name") or d.get("s3Key") or d.get("sftpFileName") or out["id"])
                if script.get("type") == "container":
                    raise DbtExportError(f"the output «{name}» of the flow «{flow.name}» reads what the Container node "
                                         f"«{label}» writes: an image runs on Tabularia's cluster, not in dbt",
                                         "container_not_exportable", flow=flow.name, output=name, container=label)
                raise DbtExportError(f"the output «{name}» of the flow «{flow.name}» reads what the Script node "
                                     f"«{label}» writes: Python or R runs on Tabularia's cluster, not in dbt",
                                     "script_not_exportable", flow=flow.name, output=name, script=label)
            feeding |= upstream
        if nodi is None:
            for i in sorted(scripts - feeding):
                label = str((nodes[i].get("data") or {}).get("label") or i)
                what = "Container node" if nodes[i].get("type") == "container" else "Script node"
                runs = "an image" if nodes[i].get("type") == "container" else "Python or R"
                self.note.append(f"The flow «{flow.name}» also runs the {what} «{label}» ({runs}): it is not "
                                 "part of this dbt project, and Tabularia keeps running it with the flow.")

    def _scrivi_modelli(self, flow: Flow, uscite: list[dict]) -> None:
        """The models of outputs whose sources are resolved: the steps two or more of
        them share become one intermediate model, then one model per output. Each
        carries the dialect of its flow's free text (compute, sql), for the engine."""
        if not uscite:
            return
        ctx = self._contesto(flow)
        by_id, suffisso, origine, dialetto = ctx["by_id"], ctx["suffisso"], ctx["origine"], ctx["dialetto"]
        for u in uscite:
            self._in_attesa.pop((flow.id, (u["node"] or {}).get("id")), None)
        tagli = self._tagli(uscite)   # prefissi condivisi → modelli intermedi
        intermedi: dict[tuple, str] = {}
        for prefisso in sorted(tagli, key=lambda t: (len(t[1]), t)):
            u = next(u for u in uscite if tuple(u["ids"][: len(prefisso[1])]) == prefisso[1] and u["key"] == prefisso[0])
            ultimo = by_id.get(prefisso[1][-1].split("#")[0], {})
            etichetta = (ultimo.get("data") or {}).get("label") or ultimo.get("id") or "step"
            nome = self.nomi.nuovo(f"int_{etichetta}", suffisso, effimero=True)
            base, ops_rest = self._base(u, prefisso[1], intermedi)
            intermedi[prefisso] = nome
            self.models.append(self._traccia({
                "name": nome, "kind": "intermediate", "materialized": "ephemeral", "source": base, "operations": ops_rest,
                "description": f"Shared steps of the flow «{flow.name}», up to the node «{etichetta}».", "columns": [], "tests": [],
                "dialect": dialetto,
            }, origine, output=f"shared steps up to the node «{etichetta}»"))

        for u in uscite:
            modello = self._modello_uscita(flow, u, intermedi, suffisso)
            modello["dialect"] = dialetto
            self._materializzazione(flow, u, modello)
            self.models.append(self._traccia(modello, origine, output=modello.pop("_uscita", None),
                                             node=(u["node"] or {}).get("id")))
            self._fatte[(flow.id, (u["node"] or {}).get("id"))] = modello["name"]

    def _materializzazione(self, flow: Flow, u: dict, modello: dict) -> None:
        """La materializzazione scelta nel dialogo per questa uscita, se ammessa:
        `incremental` solo per un'uscita in append (le altre sostituiscono i dati)."""
        chiave = chiave_uscita(flow.id, (u["node"] or {}).get("id"))
        ammesse = ["incremental", "table", "view"] if modello["materialized"] == "incremental" else ["table", "view"]
        self.uscite.append({"key": chiave, "model": modello["name"], "flow": flow.name, "label": modello.get("_uscita"),
                            "materialized": modello["materialized"], "materializations": ammesse})
        scelta = self.opzioni.materializations.get(chiave)
        if scelta is None:
            return
        if scelta not in ammesse:
            raise DbtExportError(f"the output «{modello['name']}» cannot be «{scelta}»: choose among {', '.join(ammesse)}",
                                 "materialization_not_allowed", model=modello["name"], choice=scelta, allowed=", ".join(ammesse))
        modello["materialized"] = scelta

    def _sorgenti_annidate(self, operations: list, profondita: int) -> None:
        """Le sorgenti dei rami destri (join/union): si classificano come la radice e,
        se non sono tabelle, il riferimento nel ramo diventa {seed}/{model} — la
        chiave del parquet il motore non saprebbe mapparla."""
        for op in operations or []:
            params = op.get("params") or {}
            for nested in ("right", "driver"):
                ref = params.get(nested)
                if isinstance(ref, dict) and isinstance(ref.get("source"), dict) and ref["source"].get("key"):
                    rif = self.sorgente(ref["source"]["key"], profondita)
                    if "key" not in rif:
                        ref["source"] = dict(rif)
                    self._sorgenti_annidate(ref.get("operations") or [], profondita)
            self._sorgenti_annidate(params.get("body") or [], profondita)

    @staticmethod
    def _tagli(uscite: list[dict]) -> list[tuple]:
        """I prefissi (sorgente, id dei nodi) condivisi da almeno due uscite, nel punto
        in cui le loro strade si separano — ognuno diventa un modello intermedio."""
        conteggio: dict[tuple, int] = {}
        for u in uscite:
            for k in range(1, len(u["ids"]) + 1):
                if u["ids"][k - 1] is None:
                    break
                sig = (u["key"], tuple(u["ids"][:k]))
                conteggio[sig] = conteggio.get(sig, 0) + 1
        tagli = []   # una LISTA, in ordine di flusso: l'export dev'essere lo stesso in ogni processo
        for sig, n in conteggio.items():
            if n < 2:
                continue
            # le uscite che condividono `sig` proseguono tutte allo stesso modo? allora il taglio è più avanti
            figli = [m for s2, m in conteggio.items() if s2[0] == sig[0] and len(s2[1]) == len(sig[1]) + 1 and s2[1][: len(sig[1])] == sig[1]]
            termina_qui = any(tuple(u["ids"]) == sig[1] and u["key"] == sig[0] for u in uscite)
            if termina_qui or not any(m == n for m in figli):
                tagli.append(sig)
        return tagli

    @staticmethod
    def _base(u: dict, ids_prefisso: tuple, intermedi: dict[tuple, str]) -> tuple[dict, list]:
        """Da dove parte un modello: il modello intermedio più lungo che è un prefisso
        della sua catena (se c'è), altrimenti la sorgente; e le operazioni restanti."""
        best = None
        for (key, ids), nome in intermedi.items():
            if key == u["key"] and len(ids) < len(ids_prefisso) and tuple(ids_prefisso[: len(ids)]) == ids:
                if best is None or len(ids) > len(best[0]):
                    best = (ids, nome)
        if best is None:
            return dict(u["rif"]), list(u["ops"][: len(ids_prefisso)])
        return {"model": best[1]}, list(u["ops"][len(best[0]): len(ids_prefisso)])

    def _modello_uscita(self, flow: Flow, u: dict, intermedi: dict, suffisso: str) -> dict:
        req, node = u["req"], u["node"]
        pub, dest = req.get("publish") or {}, req.get("destination") or {}
        # la catena intera: il prefisso è un intermedio? (l'uscita può stare ESATTAMENTE su un nodo condiviso)
        base, ops_rest = self._base(u, tuple(u["ids"]), {**intermedi, **({(u["key"], tuple(u["ids"])): intermedi[(u["key"], tuple(u["ids"]))]} if (u["key"], tuple(u["ids"])) in intermedi else {})})
        if (u["key"], tuple(u["ids"])) in intermedi:
            base, ops_rest = {"model": intermedi[(u["key"], tuple(u["ids"]))]}, []
        modello: dict = {"kind": "output", "source": base, "operations": ops_rest, "tests": [], "columns": []}
        if pub:
            nome_ds, project_id = pub.get("name") or "model", pub.get("project_id")
            modello["name"] = self.nomi.nuovo(nome_ds, suffisso)
            modello["materialized"] = "table"
            modello["publishes"] = (nome_ds, project_id)
            modello["description"] = f"Output «{nome_ds}» of the flow «{flow.name}»." + (f" {flow.description}" if flow.description else "")
            modello["_uscita"] = f"output «{nome_ds}» (datasource)"
            ds = self.session.exec(select(Datasource).where(Datasource.name == nome_ds, Datasource.project_id == project_id)).first()
            if ds is not None:
                self._schema_e_contratto(modello, ds)
        elif dest.get("type") == "database":
            tabella = (dest.get("table") or "").strip()
            _, nome_t = _parse_ref(tabella, "")
            modello["name"] = self.nomi.nuovo(nome_t or "model", suffisso)
            modello["materialized"] = "incremental" if (dest.get("mode") or "append") == "append" else "table"
            if flow.id == self.radice:
                # on the native target the model MAY be the destination table itself, when it is on the
                # warehouse dbt runs on: _native_payload decides, knowing that warehouse
                self.destinazioni[modello["name"]] = {"connection_id": dest.get("connection_id"), "table": tabella,
                                                      "mode": dest.get("mode") or "append"}
            modello["description"] = f"Output table `{tabella}` of the flow «{flow.name}» ({dest.get('mode') or 'append'})."
            modello["_uscita"] = f"output table `{tabella}` ({dest.get('mode') or 'append'})"
            if dest.get("post_sql"):
                self.note.append(f"The output `{tabella}` runs a post-SQL after each write in Tabularia; dbt does not: add it as a `post-hook` on the model `{modello['name']}` if you need it.")
        elif dest.get("type") == "sftp":
            nome_f = re.sub(r"\{[^{}]*\}", "", dest.get("file_name") or "model").split(".")[0].strip("_- ") or "model"
            modello["name"] = self.nomi.nuovo(nome_f, suffisso)
            modello["materialized"] = "table"
            modello["description"] = (f"Output file `{dest.get('file_name')}` written to an SFTP server by the flow «{flow.name}»: "
                                      "in dbt it is a table (export it from the warehouse).")
            modello["_uscita"] = f"output file `{dest.get('file_name')}` (SFTP)"
        else:   # s3
            # a key with placeholders: the model is named after its fixed text
            nome_f = (dest.get("key") or "model").rsplit("/", 1)[-1].split(".")[0]
            if "{" in (dest.get("key") or ""):
                nome_f = re.sub(r"\{[^{}]*\}", "", (dest.get("key") or "").rsplit("/", 1)[-1]).split(".")[0].strip("_- ") or "model"
            modello["name"] = self.nomi.nuovo(nome_f, suffisso)
            modello["materialized"] = "table"
            modello["description"] = f"Output file `{dest.get('key')}` of the flow «{flow.name}»: in dbt it is a table (export it from the warehouse)."
            modello["_uscita"] = f"output file `{dest.get('key')}`"
        return modello

    def _schema_e_contratto(self, modello: dict, ds: Datasource) -> None:
        modello["datasource_id"] = ds.id   # per le descrizioni AI (le colonne sono quelle della datasource pubblicata)
        descr = _descriptions(ds)
        colonne = {c: {"name": c, "description": descr.get(c, ""), "tests": []} for c in _columns(ds)}
        doc = contract_service.active_document(self.session, ds.id)
        regole = (doc or {}).get("rules") or []
        if not self.opzioni.tests:
            regole = [r for r in regole if r.get("kind") == "column"]   # senza test: restano i tipi dichiarati, nelle descrizioni
        for r in regole:
            kind, sev = r.get("kind"), ("warn" if r.get("severity") == "warning" else "error")
            col = r.get("column")
            if kind == "unique":
                # nel contratto `unique` porta una LISTA di colonne: una → test generico;
                # più d'una → test singolare sulla combinazione (dbt non ne ha uno pronto)
                cols = [c for c in (r.get("columns") or ([col] if col else [])) if c]
                if len(cols) == 1:
                    col = cols[0]
                elif cols:
                    modello["tests"].append({"id": r.get("id"), "kind": "unique_combination", "columns": cols, "severity": sev})
                    continue
                else:
                    continue
            if kind in _COLUMN_TESTS and col:
                voce = colonne.setdefault(col, {"name": col, "description": descr.get(col, ""), "tests": []})
                t = {"kind": kind, "severity": sev}
                if kind == "accepted_values":
                    t["values"] = list(r.get("values") or [])
                voce["tests"].append(t)
            elif kind == "column" and col:
                voce = colonne.setdefault(col, {"name": col, "description": descr.get(col, ""), "tests": []})
                if r.get("dtype"):
                    voce["description"] = (voce["description"] + " " if voce["description"] else "") + f"Contract: declared as {r['dtype']}."
            elif kind in _MODEL_TESTS:
                modello["tests"].append({k: r.get(k) for k in ("id", "kind", "column", "min", "max", "regex", "sql") if r.get(k) is not None} | {"severity": sev})
            elif kind == "freshness":
                self.note.append(f"The contract of «{ds.name}» has a freshness rule ({r.get('max_age_hours')} h): dbt checks freshness on sources, not on models — schedule the run accordingly.")
        modello["columns"] = list(colonne.values())
        if doc and doc.get("description"):
            modello["description"] += f" Contract: {doc['description']}"


def build_export_payload(session: Session, flow: Flow, default_bucket: str, target: str = "duckdb",
                         esportato_da: str | None = None, opzioni: OpzioniDbt | None = None) -> dict:
    """Il payload per l'engine. Senza opzioni: il progetto di sempre per quel target."""
    return _costruisci(session, flow, default_bucket, target, esportato_da, opzioni)[0]


def _costruisci(session: Session, flow: Flow, default_bucket: str, target: str = "duckdb",
                esportato_da: str | None = None, opzioni: OpzioniDbt | None = None) -> tuple[dict, _Progetto]:
    if opzioni is not None:
        target = opzioni.target
    if target not in ("duckdb", "native", "clickhouse"):
        raise DbtExportError(f"unknown target: '{target}'", "unknown_target", target=target)
    p = _Progetto(session, default_bucket, target, esportato_da=esportato_da, opzioni=opzioni)
    p.includi_flusso(flow)

    conns: dict[int, Connection] = {}   # the connections the SOURCES are on
    for key, (ds, conn, schema, table) in p.tables.items():
        conns[conn.id] = conn
    if target == "native":
        payload = _native_payload(flow, p, conns)
    elif target == "clickhouse":
        payload = _clickhouse_payload(flow, p, conns)
    else:
        payload = _duckdb_payload(flow, p, conns)
    p.note_senza_schema()   # payload["notes"] is p.note: after the native outputs, which add their tables
    rifiuto = _rifiuto_noto(payload["models"], target)
    if rifiuto:
        raise rifiuto
    return payload, p


def _operazioni(ops: list):
    """Le operazioni, anche quelle dei rami destri (join/union)."""
    for op in ops or []:
        yield op
        destra = ((op.get("params") or {}).get("right") or {}) if isinstance(op, dict) else {}
        yield from _operazioni(destra.get("operations") or [])


def _rifiuto_noto(models: list[dict], target: str) -> DbtExportError | None:
    """Quello che l'engine rifiuterebbe comunque, detto prima: il dialogo lo mostra
    accanto al target invece di scoprirlo al download."""
    tipi = {op.get("type") for m in models for op in _operazioni(m.get("operations") or []) if isinstance(op, dict)}
    if "foreach" in tipi:
        return DbtExportError("the «foreach» node cannot be exported: dbt has no loop at runtime", "foreach_not_exportable")
    if "pivot" in tipi and target in ("native", "clickhouse"):
        return DbtExportError("the «pivot» node produces columns decided by the data: it exports only with the federated target (dbt-duckdb), "
                              "or use in its place an sql node that lists the columns", "pivot_not_portable")
    for m in models:
        for op in _operazioni(m.get("operations") or []):
            errore = _passo_incompleto(op) if isinstance(op, dict) else None
            if errore:
                return errore
    return None


# The parameters without which the exporter cannot write a step: the `_require` calls of
# backend/app/engine/dbt_export.py `_compile_ops`, which refuses them with step_invalid
_RICHIESTI = {
    "select": ("columns",), "reorder": ("columns",), "drop": ("columns",), "rename": ("mapping",), "cast": ("columns",),
    "filter": ("column",), "sort": ("by",), "limit": ("n",), "fill_null": ("columns",), "group_by": ("by", "aggregations"),
    "compute": ("columns",), "sql": ("query",), "join": ("right",), "union": ("right",), "pivot": ("index", "on", "values"),
}
_VUOTO = (None, "", [])


def _passo_incompleto(op: dict) -> DbtExportError | None:
    """A step saved half done (a join without its right input or its keys, a filter
    without a column or a value, an aggregation without a column…), refused as the
    engine refuses it — step_invalid, filter_value_missing — so the plan shows it next
    to every target instead of the download (#11)."""
    tipo, params = str(op.get("type")), op.get("params") or {}

    def no(dettaglio: str) -> DbtExportError:
        return DbtExportError(f"{tipo}: the step is incomplete or not valid ({dettaglio})",
                              "step_invalid", step=tipo, detail=dettaglio)

    for k in _RICHIESTI.get(tipo, ()):
        if params.get(k) in _VUOTO:
            return no(f"required parameter missing: '{k}'")
    if tipo == "join" and params.get("how", "inner") != "cross" and params.get("on") in _VUOTO:
        sinistra, destra = params.get("left_on"), params.get("right_on")
        if sinistra in _VUOTO or destra in _VUOTO:
            return no("required parameter missing: 'on', or 'left_on' and 'right_on'")
        if len(sinistra if isinstance(sinistra, list) else [sinistra]) != len(destra if isinstance(destra, list) else [destra]):
            return no("join: left_on and right_on must have the same number of columns")
    if tipo == "group_by" and isinstance(params["aggregations"], list):
        if any(not isinstance(a, dict) or a.get("column") in _VUOTO for a in params["aggregations"]):
            return no("an aggregation has no column")
    if tipo == "filter":
        # the engines' own check (operations.check_filter_value): compared with NULL a filter keeps no row
        operatore, valore = params.get("operator", "eq"), params.get("value")
        if operatore not in ("is_null", "is_not_null") and (
                valore is None or (operatore in ("in", "not_in") and valore == [])
                or (operatore == "between" and (not isinstance(valore, list) or len(valore) != 2))):
            return DbtExportError(f"filter on «{params['column']}»: no value for the operator '{operatore}'",
                                  "filter_value_missing", column=str(params["column"]), operator=str(operatore))
    return None


async def engine_export(payload: dict) -> httpx.Response:
    """POST /dbt/export to the engine, bounded by ENGINE__DBT_EXPORT_TIMEOUT_SECONDS. The
    engine compiles in the request, and the shared client has no timeout (uploads of
    gigabytes go through it): an engine that hangs held the download until the ingress
    cut it with a bare 504, and an engine that cannot be reached was a 500. Both are
    refusals with a code now: `engine_timeout` (the route answers 504) and
    `engine_unreachable` (502). Any HTTP answer, an error included, is returned as it is."""
    limite = get_settings().engine.dbt_export_timeout_seconds
    try:
        # one bound for the whole call (connect, send, the compile, the zip): httpx's own
        # timeouts are per operation
        return await asyncio.wait_for(get_engine_client().post("/dbt/export", json=payload), timeout=limite)
    except (TimeoutError, httpx.TimeoutException) as e:
        logger.warning("dbt export: the engine did not answer within %g s (%s)", limite, type(e).__name__)
        raise DbtExportError(f"the engine did not build the dbt project within {limite:g} s", "engine_timeout",
                             seconds=f"{limite:g}") from e
    except httpx.TransportError as e:
        dettaglio = (str(e) or type(e).__name__)[:200]
        logger.warning("dbt export: the engine cannot be reached: %s", dettaglio)
        raise DbtExportError(f"the engine cannot be reached ({dettaglio})", "engine_unreachable", detail=dettaglio) from e


def piano_export(session: Session, flow: Flow, default_bucket: str, lingua: str = "en") -> dict:
    """Quello che serve al dialogo di download: per ogni target se si può (e perché
    no), le sue sorgenti col nome di default; le uscite con le materializzazioni
    ammesse. Non chiama l'engine: quello che solo l'engine sa (un ClickHouse che non
    è il suo) lo dice il download."""
    from app.services.messaggi_dbt import messaggio_dell_errore

    targets, uscite, uscite_se_dichiarate = [], None, None
    for target in ("clickhouse", "duckdb", "native"):
        try:
            payload, p = _costruisci(session, flow, default_bucket, target)
        except DbtExportError as e:
            voce = {"id": target, "available": False, "reason": messaggio_dell_errore(e, lingua), "sources": []}
            se_dichiarate = _se_dichiarate(session, flow, default_bucket, target, list(e.da_dichiarare)) if e.da_dichiarare else None
            if se_dichiarate is not None:
                voce.update(requires=se_dichiarate[0], sources=_sorgenti_del_piano(se_dichiarate[1], se_dichiarate[2]))
                uscite_se_dichiarate = uscite_se_dichiarate or se_dichiarate[2].uscite
            targets.append(voce)
            continue
        targets.append({"id": target, "available": True, "reason": None, "sources": _sorgenti_del_piano(payload, p)})
        if uscite is None:
            uscite = p.uscite
    return {"flow": flow.name, "default_folder": _cartella_predefinita(flow.name), "targets": targets,
            "outputs": uscite or uscite_se_dichiarate or []}


def _sorgenti_del_piano(payload: dict, p: _Progetto) -> list[dict]:
    """The sources of a target for the dialog: `default_name` is the name with nothing
    typed, `default_base` the default it was made from before a collision moved it aside."""
    return [{"key": s["key"], "connection": s["connection"], "schema": s["schema"], "default_name": s["name"],
             "default_base": p.basi_sorgenti.get((s["connection_id"], s["schema"]), s["name"]),
             "tables": sorted(t["name"] for t in s["tables"])} for s in payload["sources"]]


def _se_dichiarate(session: Session, flow: Flow, default_bucket: str, target: str,
                   chiavi: list[str]) -> tuple[dict, dict, _Progetto] | None:
    """A target that works only in the folder package, with these sources declared by the
    team's own project (ClickHouse with an Oracle source): what it requires, and the
    payload and project it would have, so the dialog can offer it there (its sources, the
    outputs' materializations). None when something else stops it too: the reason stands."""
    opzioni = OpzioniDbt(target=target, package="folder", sources={k: OpzioneSorgente(declared=True) for k in chiavi})
    try:
        payload, p = _costruisci(session, flow, default_bucket, target, opzioni=opzioni)
    except DbtExportError:
        return None
    return {"package": "folder", "declared": chiavi}, payload, p


def _secondo_il_pacchetto(o: OpzioniDbt) -> OpzioniDbt:
    """What the package makes meaningless is dropped: the folder package has no
    profiles.yml, so no schema; «already declared» sources exist only in the team's own
    project, so only in the folder package."""
    if o.package == "folder" and o.schema_ is not None:
        o = o.model_copy(update={"schema_": None})
    if o.package == "project" and any(s.declared for s in o.sources.values()):
        o = o.model_copy(update={"sources": {k: s.model_copy(update={"declared": False}) for k, s in o.sources.items()}})
    return o


def _errore_di_lettura(flow: Flow, e: FlowResolveError) -> DbtExportError:
    """A flow the resolver cannot read, said with the flow's name (it may be an
    upstream flow) and in the reader's language."""
    if e.code == "no_outputs":
        return DbtExportError(f"the flow «{flow.name}» has no exportable outputs: {e}", "no_outputs", flow=flow.name)
    if e.code == "output_no_source":
        return DbtExportError(f"the flow «{flow.name}»: {e}", "output_no_source", flow=flow.name, output=e.params.get("output", ""))
    if e.code == "flow_loop":
        return DbtExportError(f"the flow «{flow.name}»: {e}", "flow_loop", flow=flow.name, node=e.params.get("node", ""))
    return DbtExportError(f"the flow «{flow.name}» cannot be read: {e}", "flow_resolve", flow=flow.name, detail=str(e))


def _non_esportabile(conn: Connection) -> DbtExportError:
    """A database no dbt target reads live (Oracle, SQL Server, Trino…)."""
    return DbtExportError(f"the connection «{conn.name}» is {conn.db_type}, which no dbt target reads", "source_not_exportable",
                          connection=conn.name, db_type=conn.db_type)


def _non_federabile(conn: Connection) -> DbtExportError:
    """Why the federated target cannot attach `conn`: a ClickHouse goes to the ClickHouse or
    the native target; what no target reads is said as such, never «use another target»."""
    if conn.db_type not in _LEGGIBILI:
        return _non_esportabile(conn)
    return DbtExportError(f"the connection «{conn.name}» is {conn.db_type}: DuckDB cannot federate it (federation: postgresql/mysql/mariadb)",
                          "not_federable", connection=conn.name, db_type=conn.db_type)


def _definizione_non_valida(flow: Flow, e: Exception) -> DbtExportError:
    """A definition the editor would never save (written through the API: a node without
    an id, a node or its data that is not an object, text that is not JSON): a reason in
    the plan and a 422 at the download, not a 500."""
    logger.warning("dbt export: the definition of the flow %s is not valid", flow.id, exc_info=True)
    dettaglio = f"its definition is not valid ({type(e).__name__}: {str(e)[:120]})"
    return DbtExportError(f"the flow «{flow.name}» cannot be read: {dettaglio}", "flow_resolve", flow=flow.name, detail=dettaglio)


def _nomi_sorgenti(p: _Progetto, predefinito: Callable[[Connection, str], str]) -> Callable[[Connection, str], str]:
    """The name of each source (connection, schema) in sources.yml: the one chosen in
    the dialog, else the default — with a suffix when two defaults collide (`Sales`
    and `sales` slug alike). Two sources may share a name only with no table in
    common: otherwise dbt refuses the project, and `source()` could not tell them apart."""
    coppie: dict[tuple[int, str], tuple[Connection, list[str]]] = {}
    for ds, conn, schema, table in p.tables.values():
        if table:
            tabelle = coppie.setdefault((conn.id, schema), (conn, []))[1]
            if table not in tabelle:
                tabelle.append(table)
    nomi: dict[tuple[int, str], str] = {}
    for (conn_id, schema) in coppie:
        scelta = p.opzioni.sources.get(chiave_sorgente(conn_id, schema))
        if scelta and scelta.name:
            nomi[(conn_id, schema)] = scelta.name
    usati = set(nomi.values())
    for (conn_id, schema), (conn, _) in coppie.items():
        p.basi_sorgenti[(conn_id, schema)] = predefinito(conn, schema)
        if (conn_id, schema) not in nomi:
            base = cand = predefinito(conn, schema)
            n = 2
            while cand in usati:
                cand, n = f"{base}_{n}", n + 1
            nomi[(conn_id, schema)] = cand
            usati.add(cand)
    viste: dict[tuple[str, str], tuple[int, str]] = {}
    for k, (conn, tabelle) in coppie.items():
        for t in tabelle:
            altra = viste.setdefault((nomi[k], t), k)
            if altra != k:
                prima = f"{coppie[altra][0].name}.{altra[1]}"
                raise DbtExportError(f"two sources are both named «{nomi[k]}» and both have a table «{t}» ({prima}, {conn.name}.{k[1]})",
                                     "source_name_duplicate", name=nomi[k], table=t, sources=f"{prima}, {conn.name}.{k[1]}")
    return lambda conn, schema: nomi[(conn.id, schema)]


def _macro_source(nome: str, tabella: str) -> str:
    """`{{ source('<name>', '<table>') }}` that dbt reads with the exact names: a quote or
    a backslash in a table name ended the Jinja string, and dbt could not parse the model."""
    return "{{ source(%s, %s) }}" % (_stringa_jinja(nome), _stringa_jinja(tabella))


def _stringa_jinja(s: str) -> str:
    """`s` as a Jinja expression that dbt reads back exactly. A plain name keeps its
    `'…'`; with a quote only, `"…"`. dbt's static parser takes a string literal as
    written, escapes included, so a name that needs an escape (a backslash, or both
    quotes) is written as pieces joined with `~`: the static parser leaves it to Jinja,
    which reads `'\\\\'` as one backslash."""
    if "\\" not in s and "'" not in s:
        return f"'{s}'"
    if "\\" not in s and '"' not in s:
        return f'"{s}"'
    pezzi = [{"'": '"\'"', "\\": "'\\\\'"}.get(x, f"'{x}'") for x in re.split(r"(['\\\\])", s) if x]
    return " ~ ".join(pezzi + ([] if len(pezzi) > 1 else ["''"]))


def _sorgenti_quotabili(p: _Progetto, adapter: Callable[[Connection], str], con_schema: Callable[[Connection], bool]) -> None:
    """The schema (where the project uses it as it is) and the table of every source the
    project declares must be names the adapter can quote. A source the team's own
    project declares is theirs."""
    for _, conn, schema, table in p.tables.values():
        scelta = p.opzioni.sources.get(chiave_sorgente(conn.id, schema))
        if not table or (scelta and scelta.declared):
            continue
        for nome in ([schema] if con_schema(conn) else []) + [table]:
            _quotabile(nome, adapter(conn), "source")


def _sources_yaml(p: _Progetto, nome_per: callable, database_per: callable) -> list[dict]:
    """sources.yml: una voce per (connessione, schema), tabelle con colonne e descrizioni.
    Il nome è quello scelto nel dialogo, se c'è; una sorgente «già dichiarata» nel
    progetto del team resta nell'elenco (serve il suo nome) ma l'engine non la scrive."""
    sources: dict[tuple, dict] = {}
    for key, (ds, conn, schema, table) in p.tables.items():
        if not table:
            continue
        scelta = p.opzioni.sources.get(chiave_sorgente(conn.id, schema))
        s = sources.setdefault((conn.id, schema), {"name": nome_per(conn, schema), "database": database_per(conn), "schema": schema,
                                                   "connection_id": conn.id, "key": chiave_sorgente(conn.id, schema),
                                                   "connection": conn.name, "declared": bool(scelta and scelta.declared), "tables": []})
        if not any(t["name"] == table for t in s["tables"]):
            s["tables"].append({"name": table, "datasource_id": ds.id, "description": f"Datasource «{ds.name}»" + (f": {ds.description}" if ds.description else ""),
                                "columns": [{"name": c, "description": _descriptions(ds).get(c, "")} for c in _columns(ds)]})
    return list(sources.values())


def _duckdb_payload(flow: Flow, p: _Progetto, conns: dict[int, Connection]) -> dict:
    attachments = {}
    for conn in conns.values():
        if conn.db_type not in _FEDERABLE:
            raise _non_federabile(conn)
        attachments[conn.id] = {
            "alias": f"db_{conn.id}", "db_type": conn.db_type, "pw_env": f"TABULARIA_DB_{conn.id}_PASSWORD",
            "conn": {"host": conn.host, "port": conn.port or _FEDERABLE[conn.db_type][0], "database": conn.database, "username": conn.username},
        }
    _sorgenti_quotabili(p, lambda c: "dbt-duckdb", lambda c: True)
    nome = _nomi_sorgenti(p, lambda conn, schema: _slug(f"db_{conn.id}_{schema}"))
    sources = _sources_yaml(p, nome, lambda conn: f"db_{conn.id}")
    source_map = {}
    for key, (ds, conn, schema, table) in p.tables.items():
        if table:
            source_map[key] = {"ref": _macro_source(nome(conn, schema), table), "columns": _columns(ds), "types": _dtypes(ds), "label": table}
    return {
        "flow_name": flow.name, "flow_description": flow.description or "", "mode": "duckdb",
        "models": p.models, "seeds": p.seeds, "source_map": source_map,
        "attachments": list(attachments.values()), "sources": sources, "notes": p.note,
        "layout": p.layout(), "schema": p.opzioni.schema_, "exported_by": p.esportato_da,
    }


def _destinazioni_native(p: _Progetto, conn: Connection) -> None:
    """Where the database outputs of the exported flow land on the native target, whose
    profile is the SOURCE connection `conn`. An output Tabularia writes on `conn` keeps
    its real table (schema and alias; a one-part name in the schema Tabularia writes
    to). One written on ANOTHER connection is an ordinary model in the project's
    schema: dbt must never create or replace tables of the source warehouse that the
    flow does not write, and loading it into the other database is the data team's job.
    So is one materialized otherwise than Tabularia writes it (`table` or `view` over an
    append, `view` over a replace): dbt would rebuild the accumulated table with the last
    batch only, or drop it for a view."""
    for m in p.models:
        dest = p.destinazioni.get(m["name"])
        if dest is None:
            continue
        try:
            cid = int(dest.get("connection_id"))
        except (TypeError, ValueError):
            cid = None
        parti = [x.strip() for x in (dest.get("table") or "").split(".") if x.strip()]
        if cid == conn.id and parti:
            propria = "incremental" if dest["mode"] == "append" else "table"
            if m["materialized"] != propria:
                p.note.append(f"The model `{m['name']}` is the output table `{dest.get('table')}` ({dest['mode']}) of the flow, materialized "
                              f"as `{m['materialized']}`: dbt builds it in the project's schema, so that it never replaces the table "
                              "Tabularia writes.")
                continue
            schema, alias = (parti[-2] if len(parti) >= 2 else _schema_predefinito(conn)), parti[-1]
            if conn.db_type == "postgresql":
                schema, alias = _come_postgres(schema), _come_postgres(alias)
                if propria == "table" and not _ricostruibile(alias):
                    p.note.append(f"The model `{m['name']}` is the output table `{dest.get('table')}` of the flow, a name dbt-postgres cannot "
                                  "rebuild: it cuts the names of its temporary tables (`__dbt_tmp`, `__dbt_backup`) to 63 characters, "
                                  "Postgres cuts names to 63 bytes, and with accented letters they collide. dbt builds it in the "
                                  "project's schema.")
                    continue
            _quotabile(schema, _adapter_nativo(conn), "schema")
            _quotabile(alias, _adapter_nativo(conn), "relation")
            m["schema"], m["alias"] = schema, alias
            if len(parti) == 1:
                p.senza_schema_di(conn, parti[-1])
        elif cid != conn.id:
            altra = p.session.get(Connection, cid) if cid is not None else None
            dove = f"«{altra.name}»" if altra else "another connection"
            p.note.append(f"The model `{m['name']}` is the output table `{dest.get('table')}` that Tabularia writes on {dove}, not on the "
                          f"warehouse dbt runs on: dbt builds it in the project's schema, and loading it into {dove} is the data team's job.")


def _adapter_nativo(conn: Connection) -> str:
    return "dbt-clickhouse" if conn.db_type == "clickhouse" else "dbt-postgres"


def _native_payload(flow: Flow, p: _Progetto, conns: dict[int, Connection]) -> dict:
    for c in conns.values():
        if c.db_type not in _LEGGIBILI:
            raise _non_esportabile(c)   # before «one connection only»: that would be no way out either
    if len(conns) > 1:
        raise DbtExportError(
            "the native export requires all sources to be on the SAME connection "
            "(a single warehouse). This flow uses more than one — use the «federated» export.",
            "native_one_connection",
        )
    if not conns:
        raise DbtExportError("the native export needs at least one database source: this flow reads only files", "native_needs_database")
    conn = next(iter(conns.values()))
    if conn.db_type not in _NATIVE:
        raise DbtExportError(f"no native dbt adapter for '{conn.db_type}'", "no_native_adapter", db_type=conn.db_type)
    _sorgenti_quotabili(p, _adapter_nativo, lambda c: True)
    _destinazioni_native(p, conn)
    nome = _nomi_sorgenti(p, lambda c, schema: _slug(f"src_{schema}"))
    sources = _sources_yaml(p, nome, lambda c: c.database)
    source_map = {}
    for i, (key, (ds, c, schema, table)) in enumerate(p.tables.items()):
        if table:
            source_map[key] = {"ident": f"__s_{i}", "macro": _macro_source(nome(c, schema), table), "columns": _columns(ds), "types": _dtypes(ds), "label": table}
    native = {
        "db_type": conn.db_type, "pw_env": f"TABULARIA_DB_{conn.id}_PASSWORD", "target_schema": p.opzioni.schema_ or "dbt_tabularia",
        "conn": {"host": conn.host, "port": conn.port or _PORTA[conn.db_type], "database": conn.database, "username": conn.username},
    }
    return {
        "flow_name": flow.name, "flow_description": flow.description or "", "mode": "native",
        "models": p.models, "seeds": p.seeds, "source_map": source_map, "native": native, "sources": sources, "notes": p.note,
        "layout": p.layout(), "exported_by": p.esportato_da,
    }


def _clickhouse_payload(flow: Flow, p: _Progetto, conns: dict[int, Connection]) -> dict:
    """dbt-clickhouse sul ClickHouse del cliente. Le sorgenti possono stare su più
    connessioni: ClickHouse legge Postgres/MySQL dal vivo con un database federato per
    (connessione, schema). Nomi di quei database, rifiuti (un altro ClickHouse, un
    tipo che ClickHouse non legge) e traduzione delle query li decide l'engine, che
    conosce il ClickHouse su cui gira. What ClickHouse cannot read at all (Oracle, SQL
    Server, Trino…) is refused here already, so the plan disables the target — and says
    when declaring those sources in the team's own project would make it work."""
    illeggibili = [(c, schema, table) for _, c, schema, table in p.tables.values() if c.db_type not in _CLICKHOUSE_LEGGE]
    for c, _, table in illeggibili:
        if not table:
            raise _non_esportabile(c)   # a SQL query on it: nothing a project can declare
    # a source the team's own project declares (folder package) is theirs to read: the engine skips it too
    mancanti = [c for c, schema, _ in illeggibili if not (p.opzioni.sources.get(chiave_sorgente(c.id, schema)) or OpzioneSorgente()).declared]
    if mancanti:
        c = mancanti[0]
        e = DbtExportError(f"the connection «{c.name}» is {c.db_type}: ClickHouse can read it only if it is PostgreSQL, MySQL or MariaDB",
                           "clickhouse_cannot_read", connection=c.name, db_type=c.db_type)
        e.da_dichiarare = tuple(dict.fromkeys(chiave_sorgente(c.id, schema) for c, schema, _ in illeggibili))
        raise e
    # the schema of a Postgres/MySQL source becomes a database the engine names; a ClickHouse one is used as it is
    _sorgenti_quotabili(p, lambda c: "dbt-clickhouse", lambda c: c.db_type == "clickhouse")
    nome = _nomi_sorgenti(p, lambda c, schema: _slug(f"db_{c.id}_{schema}"))
    sources = _sources_yaml(p, nome, lambda c: None)
    source_map = {}
    for i, (key, (ds, c, schema, table)) in enumerate(p.tables.items()):
        if table:
            source_map[key] = {"ident": f"__s_{i}", "macro": _macro_source(nome(c, schema), table),
                               "columns": _columns(ds), "types": _dtypes(ds), "label": table}
    connections = [{
        "id": c.id, "name": c.name, "db_type": c.db_type, "host": c.host, "port": c.port or _PORTA.get(c.db_type),
        "database": c.database, "username": c.username, "pw_env": f"TABULARIA_DB_{c.id}_PASSWORD",
    } for c in conns.values()]
    return {
        "flow_name": flow.name, "flow_description": flow.description or "", "mode": "clickhouse",
        "models": p.models, "seeds": p.seeds, "source_map": source_map, "sources": sources, "notes": p.note,
        "clickhouse": {"connections": connections, "target_schema": p.opzioni.schema_ or "dbt_tabularia"},
        "layout": p.layout(), "exported_by": p.esportato_da,
    }
