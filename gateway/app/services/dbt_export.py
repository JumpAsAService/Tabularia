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

import json
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, func, select

from app.models import Connection, Datasource, Flow, FlowVersion, Project, User
from app.services import contracts as contract_service
from app.services.flow_resolver import FlowResolveError, resolve_output_chains

# db_type federabili da DuckDB (scanner ufficiali) + porta di default + funzione di query remota
_FEDERABLE = {"postgresql": (5432, "postgres_query"), "mysql": (3306, "mysql_query"), "mariadb": (3306, "mysql_query")}
# db_type con un adapter dbt NATIVO (via traduzione sqlglot) + porta default
_NATIVE_PORT = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "clickhouse": 8123}
MAX_UPSTREAM_DEPTH = 8          # flussi a monte inclusi, al più
# regole di contratto → test dbt
_COLUMN_TESTS = {"not_null", "unique", "accepted_values"}
_MODEL_TESTS = {"range", "pattern", "row_count", "expression"}


class DbtExportError(ValueError):
    """Un rifiuto dell'export. Il testo (italiano) resta nei log; `code` e `params`
    lo fanno dire nella lingua di chi lo legge (services/messaggi_dbt.py)."""

    def __init__(self, messaggio: str, code: str = "internal", **params):
        super().__init__(messaggio)
        self.code = code
        self.params = params


def _slug(s: str) -> str:
    out = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(s).lower()).strip("_")
    out = out or "x"
    return out if not out[0].isdigit() else f"m_{out}"


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
    dialogo (`tab_`) vale per modelli e seed: tiene i nomi lontani da quelli del team."""

    def __init__(self, prefisso: str = ""):
        self.usati: set[str] = set()
        self.prefisso = prefisso

    def nuovo(self, base: str, suffisso: str = "") -> str:
        base = f"{self.prefisso}{base}"
        nome = _slug(base)
        if nome in self.usati and suffisso:
            nome = _slug(f"{base}_{suffisso}")
        n, cand = 2, nome
        while cand in self.usati:
            cand = f"{nome}_{n}"
            n += 1
        self.usati.add(cand)
        return cand


class _Progetto:
    """Lo stato dell'export mentre si attraversano il flusso e i flussi a monte."""

    def __init__(self, session: Session, default_bucket: str, target: str,
                 esportato_da: str | None = None, opzioni: OpzioniDbt | None = None):
        self.session = session
        self.bucket = default_bucket
        self.target = target
        self.opzioni = opzioni or OpzioniDbt(target=target)
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

    def nome_sorgente(self, conn: Connection, schema: str, predefinito: str) -> str:
        scelta = self.opzioni.sources.get(chiave_sorgente(conn.id, schema))
        return scelta.name if scelta and scelta.name else predefinito

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
        if ds is not None and ds.kind == "database" and ds.source_type == "sharepoint":
            # un file Excel su SharePoint: per dbt non c'è un database da cui leggerlo → seed
            rif = {"seed": self._seed(ds, key)}
        elif ds is not None and ds.kind == "database":
            conn = self.session.get(Connection, ds.connection_id) if ds.connection_id else None
            if conn is None:
                raise DbtExportError(f"la sorgente «{ds.name}» non ha una connessione valida", "source_no_connection", source=ds.name)
            if ds.source_type == "sql":
                rif = {"model": self._modello_query(ds, conn)}
            else:
                default_schema = conn.database if conn.db_type in ("clickhouse", "mysql", "mariadb") else "public"
                schema, table = _parse_ref(ds.source_ref, default_schema)
                self.tables[key] = (ds, conn, schema, table)
                rif = {"key": key}
        elif ds is not None and ds.kind == "flow" and ds.flow_id:
            rif = {"model": self._flusso_a_monte(ds, profondita)}
        else:
            rif = {"seed": self._seed(ds, key)}
        self.riferimenti[key] = rif
        return rif

    def _modello_query(self, ds: Datasource, conn: Connection) -> str:
        """Una datasource da QUERY: un modello ephemeral con la query. In federazione
        la query gira nel DB di origine tramite la funzione di query remota di DuckDB;
        in nativo è già nel dialetto del warehouse."""
        nome = self.nomi.nuovo(f"src_{ds.name}")
        if self.target == "duckdb":
            if conn.db_type not in _FEDERABLE:
                raise DbtExportError(
                    f"la connessione «{conn.name}» è {conn.db_type}: non federabile da DuckDB "
                    "(federazione: postgresql/mysql/mariadb). Prova l'export «nativo».",
                    "not_federable", connection=conn.name, db_type=conn.db_type,
                )
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
        """L'uscita di un altro flusso: si includono i suoi modelli e si referenzia
        quello che pubblica questa datasource."""
        flow = self.session.get(Flow, ds.flow_id)
        if flow is None:
            raise DbtExportError(f"la sorgente «{ds.name}» è l'uscita di un flusso che non esiste più", "upstream_missing", source=ds.name)
        if profondita >= MAX_UPSTREAM_DEPTH:
            raise DbtExportError(f"troppi flussi a monte in catena (oltre {MAX_UPSTREAM_DEPTH}): la sorgente «{ds.name}» non si può includere",
                                 "upstream_too_deep", max=MAX_UPSTREAM_DEPTH, source=ds.name)
        if flow.id in self.flussi_visti:
            # già incluso (o ciclo): si cerca il suo modello
            for m in self.models:
                if m.get("publishes") == (ds.name, ds.project_id):
                    return m["name"]
            raise DbtExportError(f"ciclo fra flussi: «{flow.name}» dipende da sé stesso attraverso «{ds.name}»", "flow_cycle", flow=flow.name, source=ds.name)
        self.includi_flusso(flow, profondita + 1)
        for m in self.models:
            if m.get("publishes") == (ds.name, ds.project_id):
                return m["name"]
        raise DbtExportError(f"il flusso «{flow.name}» non ha un'uscita che pubblica «{ds.name}»: la datasource va rigenerata da lì",
                             "upstream_no_output", flow=flow.name, source=ds.name)

    def _seed(self, ds: Datasource | None, key: str) -> str:
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
        """I modelli di un flusso: uno per Output, più gli intermedi condivisi."""
        self.flussi_visti.add(flow.id)
        definition = json.loads(flow.definition or "{}")

        def resolve_ds(ds_id: int) -> Optional[tuple[str, str]]:
            ds = self.session.get(Datasource, ds_id)
            return (ds.bucket, ds.key) if ds and ds.key else None

        try:
            catene = resolve_output_chains(definition, resolve_ds, self.bucket)
        except FlowResolveError as e:
            raise DbtExportError(str(e), "flow_resolve", detail=str(e))
        by_id = {n["id"]: n for n in definition.get("nodes") or []}
        suffisso = _slug(flow.name)
        origine = self._origine(flow)
        for n in by_id.values():
            data = n.get("data") or {}
            if n.get("type") == "source" and data.get("parquetKey") and data.get("filename"):
                self.nomi_file[data["parquetKey"]] = str(data["filename"])

        uscite = []
        for c in catene:
            req, node = c["body"], c["node"]
            dest = req.get("destination") or {}
            if req.get("email") or dest.get("type") not in (None, "database", "s3"):
                continue   # un'email non è un dataset: nessun modello
            key = req["input_key"]
            rif = self.sorgente(key, profondita)
            # le sorgenti dei rami destri (join/union) vanno risolte anche loro
            self._sorgenti_annidate(req.get("operations") or [], profondita)
            ops = list(req.get("operations") or [])
            ids = list(c["op_ids"])
            pub_keys = [k.strip() for k in ((req.get("publish") or {}).get("sort_keys") or []) if k and k.strip()]
            if pub_keys:
                # l'ORDER BY del publish lo inietta il gateway al lancio: senza, la tabella
                # dbt sarebbe fisicamente diversa da quella che l'app pubblica
                ops.append({"type": "sort", "params": {"by": pub_keys, "ignore_missing": True}})
                ids.append(None)
            uscite.append({"req": req, "node": node, "rif": rif, "key": key, "ops": ops, "ids": ids})
        if not uscite:
            raise DbtExportError(f"il flusso «{flow.name}» non ha uscite esportabili (solo email?)", "no_outputs", flow=flow.name)

        tagli = self._tagli(uscite)   # prefissi condivisi → modelli intermedi
        intermedi: dict[tuple, str] = {}
        for prefisso in sorted(tagli, key=lambda t: (len(t[1]), t)):
            u = next(u for u in uscite if tuple(u["ids"][: len(prefisso[1])]) == prefisso[1] and u["key"] == prefisso[0])
            ultimo = by_id.get(prefisso[1][-1].split("#")[0], {})
            etichetta = (ultimo.get("data") or {}).get("label") or ultimo.get("id") or "step"
            nome = self.nomi.nuovo(f"int_{etichetta}", suffisso)
            base, ops_rest = self._base(u, prefisso[1], intermedi)
            intermedi[prefisso] = nome
            self.models.append(self._traccia({
                "name": nome, "kind": "intermediate", "materialized": "ephemeral", "source": base, "operations": ops_rest,
                "description": f"Shared steps of the flow «{flow.name}», up to the node «{etichetta}».", "columns": [], "tests": [],
            }, origine, output=f"shared steps up to the node «{etichetta}»"))

        for u in uscite:
            modello = self._modello_uscita(flow, u, intermedi, suffisso)
            self._materializzazione(flow, u, modello)
            self.models.append(self._traccia(modello, origine, output=modello.pop("_uscita", None),
                                             node=(u["node"] or {}).get("id")))

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
            raise DbtExportError(f"l'uscita «{modello['name']}» non può essere «{scelta}»: si può scegliere fra {', '.join(ammesse)}",
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
            schema, nome_t = _parse_ref(tabella, "")
            modello["name"] = self.nomi.nuovo(nome_t or "model", suffisso)
            modello["materialized"] = "incremental" if (dest.get("mode") or "append") == "append" else "table"
            if self.target == "native":
                # nel warehouse di origine il modello PUÒ essere la tabella di destinazione
                if schema:
                    modello["schema"] = schema
                modello["alias"] = nome_t
            modello["description"] = f"Output table `{tabella}` of the flow «{flow.name}» ({dest.get('mode') or 'append'})."
            modello["_uscita"] = f"output table `{tabella}` ({dest.get('mode') or 'append'})"
            if dest.get("post_sql"):
                self.note.append(f"The output `{tabella}` runs a post-SQL after each write in Tabularia; dbt does not: add it as a `post-hook` on the model `{modello['name']}` if you need it.")
        else:   # s3
            nome_f = (dest.get("key") or "model").rsplit("/", 1)[-1].split(".")[0]
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
        raise DbtExportError(f"target sconosciuto: '{target}'", "unknown_target", target=target)
    p = _Progetto(session, default_bucket, target, esportato_da=esportato_da, opzioni=opzioni)
    p.includi_flusso(flow)

    conns: dict[int, Connection] = {}
    for key, (ds, conn, schema, table) in p.tables.items():
        conns[conn.id] = conn
    if target == "native":
        payload = _native_payload(flow, p, conns)
    elif target == "clickhouse":
        payload = _clickhouse_payload(flow, p, conns)
    else:
        payload = _duckdb_payload(flow, p, conns)
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
        return DbtExportError("il nodo «foreach» non si esporta: dbt non ha un ciclo a runtime", "foreach_not_exportable")
    if "pivot" in tipi and target in ("native", "clickhouse"):
        return DbtExportError("il nodo «pivot» produce colonne decise dai dati: si esporta solo col target federato (dbt-duckdb), "
                              "oppure al suo posto un nodo sql che elenchi le colonne", "pivot_not_portable")
    return None


def piano_export(session: Session, flow: Flow, default_bucket: str, lingua: str = "en") -> dict:
    """Quello che serve al dialogo di download: per ogni target se si può (e perché
    no), le sue sorgenti col nome di default; le uscite con le materializzazioni
    ammesse. Non chiama l'engine: quello che solo l'engine sa (un ClickHouse che non
    è il suo) lo dice il download."""
    targets, uscite = [], None
    for target in ("clickhouse", "duckdb", "native"):
        try:
            payload, p = _costruisci(session, flow, default_bucket, target)
        except DbtExportError as e:
            from app.services.messaggi_dbt import messaggio_dell_errore

            targets.append({"id": target, "available": False, "reason": messaggio_dell_errore(e, lingua), "sources": []})
            continue
        targets.append({"id": target, "available": True, "reason": None, "sources": [
            {"key": s["key"], "connection": s["connection"], "schema": s["schema"], "default_name": s["name"],
             "tables": sorted(t["name"] for t in s["tables"])}
            for s in payload["sources"]
        ]})
        if uscite is None:
            uscite = p.uscite
    return {"flow": flow.name, "default_folder": _slug(flow.name), "targets": targets, "outputs": uscite or []}


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
            raise DbtExportError(
                f"la connessione «{conn.name}» è {conn.db_type}: non federabile da DuckDB "
                "(federazione: postgresql/mysql/mariadb). Prova l'export «nativo» per ClickHouse.",
                "not_federable", connection=conn.name, db_type=conn.db_type,
            )
        attachments[conn.id] = {
            "alias": f"db_{conn.id}", "db_type": conn.db_type, "pw_env": f"TABULARIA_DB_{conn.id}_PASSWORD",
            "conn": {"host": conn.host, "port": conn.port or _FEDERABLE[conn.db_type][0], "database": conn.database, "username": conn.username},
        }
    nome = lambda conn, schema: p.nome_sorgente(conn, schema, _slug(f"db_{conn.id}_{schema}"))
    sources = _sources_yaml(p, nome, lambda conn: f"db_{conn.id}")
    source_map = {}
    for key, (ds, conn, schema, table) in p.tables.items():
        if table:
            source_map[key] = {"ref": "{{ source('%s', '%s') }}" % (nome(conn, schema), table), "columns": _columns(ds), "types": _dtypes(ds), "label": table}
    return {
        "flow_name": flow.name, "flow_description": flow.description or "", "mode": "duckdb",
        "models": p.models, "seeds": p.seeds, "source_map": source_map,
        "attachments": list(attachments.values()), "sources": sources, "notes": p.note,
        "layout": p.layout(), "schema": p.opzioni.schema_, "exported_by": p.esportato_da,
    }


def _native_payload(flow: Flow, p: _Progetto, conns: dict[int, Connection]) -> dict:
    if len(conns) > 1:
        raise DbtExportError(
            "l'export nativo richiede che tutte le sorgenti siano sulla STESSA connessione "
            "(un solo warehouse). Questo flusso ne usa più di una — usa l'export «federato».",
            "native_one_connection",
        )
    if not conns:
        raise DbtExportError("l'export nativo vuole almeno una sorgente da database: questo flusso legge solo file", "native_needs_database")
    conn = next(iter(conns.values()))
    if conn.db_type not in _NATIVE_PORT:
        raise DbtExportError(f"nessun adapter dbt nativo per '{conn.db_type}'", "no_native_adapter", db_type=conn.db_type)
    nome = lambda c, schema: p.nome_sorgente(c, schema, _slug(f"src_{schema}"))
    sources = _sources_yaml(p, nome, lambda c: c.database)
    source_map = {}
    for i, (key, (ds, c, schema, table)) in enumerate(p.tables.items()):
        if table:
            source_map[key] = {"ident": f"__s_{i}", "macro": "{{ source('%s', '%s') }}" % (nome(c, schema), table), "columns": _columns(ds), "types": _dtypes(ds), "label": table}
    native = {
        "db_type": conn.db_type, "pw_env": f"TABULARIA_DB_{conn.id}_PASSWORD", "target_schema": p.opzioni.schema_ or "dbt_tabularia",
        "conn": {"host": conn.host, "port": conn.port or _NATIVE_PORT[conn.db_type], "database": conn.database, "username": conn.username},
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
    conosce il ClickHouse su cui gira."""
    nome = lambda c, schema: p.nome_sorgente(c, schema, _slug(f"db_{c.id}_{schema}"))
    sources = _sources_yaml(p, nome, lambda c: None)
    source_map = {}
    for i, (key, (ds, c, schema, table)) in enumerate(p.tables.items()):
        if table:
            source_map[key] = {"ident": f"__s_{i}", "macro": "{{ source('%s', '%s') }}" % (nome(c, schema), table),
                               "columns": _columns(ds), "types": _dtypes(ds), "label": table}
    connections = [{
        "id": c.id, "name": c.name, "db_type": c.db_type, "host": c.host, "port": c.port or _NATIVE_PORT.get(c.db_type),
        "database": c.database, "username": c.username, "pw_env": f"TABULARIA_DB_{c.id}_PASSWORD",
    } for c in conns.values()]
    return {
        "flow_name": flow.name, "flow_description": flow.description or "", "mode": "clickhouse",
        "models": p.models, "seeds": p.seeds, "source_map": source_map, "sources": sources, "notes": p.note,
        "clickhouse": {"connections": connections, "target_schema": p.opzioni.schema_ or "dbt_tabularia"},
        "layout": p.layout(), "exported_by": p.esportato_da,
    }
