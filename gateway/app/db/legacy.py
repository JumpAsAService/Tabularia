"""Lo schema di PRIMA di Alembic: serve solo ad ADOTTARE un database creato quando
il gateway migrava da sé all'avvio (`create_all` + questo elenco di ALTER idempotenti).

Un database così ha le tabelle ma non `alembic_version`. Per portarlo in Alembic:
1. l'elenco qui sotto, CONGELATO com'era all'ultima versione senza Alembic, lo porta
   allo stato di quella versione qualunque fosse il punto da cui partiva;
2. il RIALLINEAMENTO toglie la deriva fra un database creato da zero e uno arrivato
   fin qui a forza di ALTER (misurata il 2026-10-10): una chiave esterna che l'ALTER
   non metteva, un tipo diverso, default lato server che i modelli non dichiarano e
   che c'erano o no a seconda di quando la colonna era nata;
3. lo si marca alla revisione di base (0001) e lo si confronta con i modelli.
Non va mai modificato: le modifiche allo schema da qui in poi sono revisioni Alembic
(gateway/migrations/versions)."""
from sqlalchemy import text

from app.models.run import LIVE_ORCHESTRATION, ORCHESTRATION_INTERRUPTED

ALTER_PRIMA_DI_ALEMBIC = [
    # Fase C: run di ingest (refresh datasource database) accanto ai run di flusso
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'flow'",
    "ALTER TABLE runs ALTER COLUMN flow_id DROP NOT NULL",
    # Fase C: datasource kind="database" (connessione + definizione sorgente)
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS connection_id INTEGER REFERENCES connections(id)",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS source_type VARCHAR",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS source_ref TEXT",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refreshed_at TIMESTAMP",
    # Nodo Output: destinazione database dell'output del run (riassunto JSON)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS destination TEXT",
    # Refresh schedulato delle datasource database
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refresh_schedule VARCHAR",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refresh_scheduled_by INTEGER REFERENCES users(id)",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS next_refresh_at TIMESTAMP",
    # Esecuzione schedulata dei flussi
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS run_schedule VARCHAR",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS run_scheduled_by INTEGER REFERENCES users(id)",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMP",
    # Publish di una datasource in overwrite (ripubblica sopra l'omonima kind=flow)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS publish_overwrite BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS publish_sort_keys VARCHAR NOT NULL DEFAULT '[]'",
    # Dettaglio errore (traceback engine) per il debug dei run falliti
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS error_detail TEXT",
    # Origine dell'avvio: 'manual' (utente) | 'schedule' (scheduler)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS trigger_type VARCHAR NOT NULL DEFAULT 'manual'",
    # Run figlio di un'orchestrazione (per non contare i doppioni nel calendario)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS parent_run_id INTEGER REFERENCES runs(id)",
    # Engine di esecuzione scelto per il flusso (polars | duckdb)
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS engine VARCHAR NOT NULL DEFAULT 'polars'",
    # Descrizioni dei campi delle datasource (JSON nome→testo)
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS column_descriptions TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS sort_keys TEXT NOT NULL DEFAULT '[]'",
    # Engine di produzione del flusso (run schedulati); NULL = come lo sviluppo
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS production_engine VARCHAR",
    # Engine con cui ciascun run è stato eseguito
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine VARCHAR",
    # Audit: ultima attività autenticata (per le "sessioni attive")
    # utenti solo-SSO: nessuna password locale (vedi services/sso.py)
    "ALTER TABLE users ALTER COLUMN hashed_password DROP NOT NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_ip VARCHAR",
    # Inizio REALE dell'esecuzione sul worker: il timeout di staleness non deve
    # contare l'attesa in coda (vedi models/run.py)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine_started_at TIMESTAMP",
    # copia best-effort dell'output su S3 esterno: esito JSON, NULL = non richiesta
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS mirror TEXT",
    # Opzioni specifiche del tipo di connessione (JSON). Serve all'SMTP, che ha
    # tre impostazioni senza una colonna naturale dove stare — mittente, modalità
    # TLS e domini ammessi — e spremere `database`/`db_schema` come si fa per S3
    # ne avrebbe comunque lasciata fuori una. Gli altri tipi non la usano.
    "ALTER TABLE connections ADD COLUMN IF NOT EXISTS extra TEXT NOT NULL DEFAULT '{}'",
    # Invio email dell'output (nodo Output destType=email): esito JSON, NULL = non richiesto
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS email TEXT",
    # Run che ha prodotto lo snapshot corrente: rifiuta gli swap da run più
    # vecchi (vedi models/datasource.py). Intero semplice, nessuna FK: eviterebbe
    # un ciclo runs ↔ datasources
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS snapshot_run_id INTEGER",
    # Identità SSO stabile (issuer + subject del token). L'SSO riconosce l'utente
    # da qui, non dall'email — vedi models/user.py e services/sso.py.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_issuer VARCHAR",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_subject VARCHAR",
    # Una stessa identità dell'IdP non può appartenere a due account. Indice
    # PARZIALE: gli account senza SSO hanno entrambe le colonne nulle e non
    # devono collidere fra loro.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_oidc_identity ON users (oidc_issuer, oidc_subject)"
    " WHERE oidc_subject IS NOT NULL",
    # Gruppo di amministratori: i membri sono admin (services/permissions.is_admin)
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE",
    # Avviso di fallimento di un'esecuzione programmata (vedi models/flow.py)
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS notify_emails TEXT",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS notify_connection_id INTEGER",
    # Ruolo osservatore: legge i pannelli admin, non scrive (vedi models/user.py)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_observer BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_observer BOOLEAN NOT NULL DEFAULT FALSE",
    # Coda delle orchestrazioni (gateway in più repliche): modalità del motore,
    # chi la esegue e il suo battito
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine_mode VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS claimed_by VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP",
    # Le orchestrazioni rimaste STARTED senza battito sono di una versione che
    # non lo scriveva: quel processo non c'è più (era uno solo, e per arrivare
    # qui è stato fermato). Si chiudono, come faceva il controllo all'avvio —
    # e prima dell'indice unico, che con due righe vive dello stesso flusso non
    # si potrebbe creare. Da qui in poi ogni riga STARTED ha il battito, e
    # questa istruzione non trova più niente.
    "UPDATE runs SET status = 'FAILURE', finished_at = now() AT TIME ZONE 'utc', "
    f"error = '{ORCHESTRATION_INTERRUPTED}' "
    f"WHERE {LIVE_ORCHESTRATION} AND heartbeat_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_one_live_orchestration ON runs (flow_id) "
    f"WHERE {LIVE_ORCHESTRATION}",
    "CREATE INDEX IF NOT EXISTS ix_runs_orchestration_queue ON runs (id) "
    "WHERE kind = 'orchestration' AND status = 'PENDING'",
    # data contracts: chi avvisare, e la coda degli avvisi da mandare
    "ALTER TABLE data_contracts ADD COLUMN IF NOT EXISTS notify_emails TEXT",
    "ALTER TABLE data_contracts ADD COLUMN IF NOT EXISTS notify_connection_id INTEGER",
    "ALTER TABLE data_contract_results ADD COLUMN IF NOT EXISTS notify VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_contract_results_to_notify ON data_contract_results (id) WHERE notify = 'pending'",
]


# la deriva misurata fra un database creato da zero e uno aggiornato con gli ALTER:
# si porta tutto a quello che dicono i modelli
_DEFAULT_DA_TOGLIERE = [
    ("runs", "kind"), ("runs", "publish_overwrite"), ("runs", "publish_sort_keys"), ("runs", "trigger_type"),
    ("flows", "engine"), ("datasources", "column_descriptions"), ("datasources", "sort_keys"),
    ("connections", "extra"), ("groups", "is_admin"), ("groups", "is_observer"), ("users", "is_observer"),
]
RIALLINEO = [
    # i default lato server: i modelli non li dichiarano (il valore lo mette l'ORM)
    *[f"ALTER TABLE {t} ALTER COLUMN {c} DROP DEFAULT" for t, c in _DEFAULT_DA_TOGLIERE],
    # l'ALTER aggiungeva la colonna senza la chiave esterna che create_all mette
    """DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'flows_notify_connection_id_fkey') THEN
           ALTER TABLE flows ADD CONSTRAINT flows_notify_connection_id_fkey
             FOREIGN KEY (notify_connection_id) REFERENCES connections(id);
         END IF;
       END $$""",
    # TEXT dall'ALTER, VARCHAR dal modello
    "ALTER TABLE flows ALTER COLUMN notify_emails TYPE VARCHAR",
    "ALTER TABLE data_contracts ALTER COLUMN notify_emails TYPE VARCHAR",
    # l'impronta con cui il gateway decideva se migrare: non serve più
    "DROP TABLE IF EXISTS schema_state",
]


def adotta(conn) -> None:
    """Porta un database di prima di Alembic allo stato della revisione di base.
    `conn` è una connessione in una transazione: tutto o niente."""
    from sqlmodel import SQLModel

    import app.models  # noqa: F401 — registra i modelli

    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    SQLModel.metadata.create_all(conn)          # le tabelle nate dopo l'ultimo avvio, se mancano
    for istruzione in ALTER_PRIMA_DI_ALEMBIC + RIALLINEO:
        conn.execute(text(istruzione))
