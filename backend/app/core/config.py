from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field, computed_field, field_validator, SecretStr
from functools import lru_cache
from pathlib import Path
from typing import Optional


# Nota: i blocchi annidati sono BaseModel *puri*. Tutto il caricamento (env +
# TOML) avviene sul `Settings` top-level tramite `env_nested_delimiter="__"`,
# così l'env vince sempre sul TOML per ogni campo (es. REDIS__HOST batte il
# valore di config.toml). Vedi la nota sulle priorità in `Settings`.


# ─────────────────────────────────────────────────────────────────────────────
# Redis Configuration
# ─────────────────────────────────────────────────────────────────────────────
class RedisSettings(BaseModel):
    host: str = Field(default="localhost", description="Redis host")
    port: int = Field(default=6379, description="Redis port")
    db: int = Field(default=0, description="Redis database number")
    password: Optional[SecretStr] = Field(default=None, description="Redis password")

    @computed_field
    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password.get_secret_value()}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"

    def __str__(self):
        return f"RedisSettings(host={self.host}, port={self.port}, db={self.db})"


# ─────────────────────────────────────────────────────────────────────────────
# Storage Configuration (qualsiasi endpoint S3-compatibile: MinIO, AWS, R2…)
# ─────────────────────────────────────────────────────────────────────────────
class StorageSettings(BaseModel):
    endpoint: str = Field(default="http://localhost:9002", description="Storage endpoint")
    access_key: str = Field(default="minioadmin", description="Access key")
    secret_key: SecretStr = Field(default=SecretStr("minioadmin"), description="Secret key")
    bucket: str = "data-prep"
    region: str = "us-east-1"

    @field_validator("secret_key", mode="before")
    @classmethod
    def _ensure_secret(cls, v: object) -> SecretStr:
        # pydantic-settings non riavvolge in SecretStr i valori che arrivano dal
        # TOML (restano str): forziamo la coercizione da qualunque sorgente.
        return v if isinstance(v, SecretStr) else SecretStr(str(v))


# ─────────────────────────────────────────────────────────────────────────────
# Celery Configuration
# ─────────────────────────────────────────────────────────────────────────────
class CelerySettings(BaseModel):
    broker_url: Optional[str] = None
    result_backend: Optional[str] = None
    worker_concurrency: int = 2
    # env: CELERY__TASK_TIME_LIMIT / CELERY__TASK_SOFT_TIME_LIMIT — durata massima
    # di un task. Al limite SOFT il task riceve un'eccezione e può chiudere in
    # ordine (file temporanei, connessioni); al limite DURO il worker gli uccide
    # il processo.
    #
    # ACCOPPIATI AL GATEWAY: `ENGINE__RUN_STALE_TIMEOUT_SECONDS` deve restare
    # MAGGIORE di `task_time_limit`, perché il gateway dichiara perso un run solo
    # dopo che Celery lo ha già ucciso — è l'invariante su cui poggia la scelta di
    # non abilitare `task_acks_late` (vedi celery_app.py). Alzare uno solo dei due
    # non allunga la durata consentita ai run: cambia solo quanto si aspetta prima
    # di vederli fallire.
    task_time_limit: int = 3600
    task_soft_time_limit: int = 3300
    task_serializer: str = "json"
    result_serializer: str = "json"
    timezone: str = "UTC"
    enable_utc: bool = True
    # env: CELERY__MAX_TASKS_PER_CHILD — dopo quanti task il processo figlio
    # prefork viene riciclato. I task Polars/Arrow allocano via glibc malloc, che
    # NON restituisce all'OS la memoria liberata (frammentazione delle arene): il
    # figlio riciclato rilascia tutto → l'RSS non cresce a scalini. Tenuto alto
    # perché i task periodici leggeri (storage-stats ogni 60s, eviction) non
    # riciclino il figlio di continuo: il vero cap è la memoria qui sotto. Def 50.
    max_tasks_per_child: int = 50
    # env: CELERY__MAX_MEMORY_PER_CHILD_KB — tetto RSS (KB) oltre il quale il
    # figlio viene riciclato a FINE task (rilascio netto della memoria residua).
    # None (default) = DERIVATO automaticamente dal limite di memoria del container
    # (cgroup) e dalla concurrency, così resta coerente qualunque sia
    # WORKER_MEM_LIMIT in produzione — vedi resolve_max_memory_per_child_kb().
    # Impostalo solo per forzare un valore fisso.
    max_memory_per_child_kb: Optional[int] = None
    # frazione del limite container riservata ai figli worker (il resto è margine
    # per processo principale, beat, page cache): per-figlio = limite*frac/concurrency
    max_memory_headroom_frac: float = 0.75
    # fallback quando il limite cgroup non è leggibile/illimitato (KB, ~1.5 GB)
    max_memory_per_child_fallback_kb: int = 1_500_000
    # pavimento: mai riciclare sotto questa soglia (evita riciclo troppo frequente)
    max_memory_per_child_floor_kb: int = 512_000


def cgroup_mem_limit_bytes() -> Optional[int]:
    """Limite di memoria del container letto dal cgroup (il tetto che il kernel
    impone davvero, qualunque sia il modo in cui è stato configurato). None se
    illimitato o non leggibile (es. fuori da un container)."""
    for path in ("/sys/fs/cgroup/memory.max",                    # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):  # cgroup v1
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            val = int(raw)
        except ValueError:
            continue
        # v1 "illimitato" è un intero enorme (~2^63): trattalo come nessun limite
        if val <= 0 or val >= (1 << 62):
            return None
        return val
    return None


def resolve_max_memory_per_child_kb(celery: CelerySettings) -> int:
    """Tetto RSS per figlio worker (KB). Se non forzato via env, lo deriva dal
    limite cgroup e dalla concurrency: `limite * frac / concurrency`. Così due (o
    N) figli residenti restano sotto il limite del container con un margine, e il
    valore si adatta da solo se in produzione WORKER_MEM_LIMIT/concurrency cambiano."""
    if celery.max_memory_per_child_kb:  # override esplicito
        return celery.max_memory_per_child_kb
    limit = cgroup_mem_limit_bytes()
    if not limit:
        return celery.max_memory_per_child_fallback_kb
    concurrency = max(1, celery.worker_concurrency)
    per_child_kb = int(limit * celery.max_memory_headroom_frac / concurrency / 1024)
    return max(per_child_kb, celery.max_memory_per_child_floor_kb)


# ─────────────────────────────────────────────────────────────────────────────
# Cache Configuration (step cache dell'engine)
# ─────────────────────────────────────────────────────────────────────────────
class CacheSettings(BaseModel):
    # env: CACHE__TTL_SECONDS — dopo quanto tempo dall'ultimo accesso una voce
    # di cache viene rimossa (blob parquet + indice Valkey). Default 1 giorno.
    ttl_seconds: int = 1 * 24 * 3600
    # env: CACHE__SWEEP_INTERVAL_SECONDS — ogni quanto gira l'eviction. Default 1h.
    sweep_interval_seconds: int = 3600


# ─────────────────────────────────────────────────────────────────────────────
# Metrics Configuration (osservabilità)
# ─────────────────────────────────────────────────────────────────────────────
class MetricsSettings(BaseModel):
    # env: METRICS__STORAGE_STATS_INTERVAL_SECONDS — ogni quanto campionare la
    # dimensione dei prefissi di storage (cache/, datasets/, ...). Default 60s.
    storage_stats_interval_seconds: int = 60


# ─────────────────────────────────────────────────────────────────────────────
# Security (segreti condivisi col gateway)
# ─────────────────────────────────────────────────────────────────────────────
class SecuritySettings(BaseModel):
    # env: SECURITY__FERNET_KEY — chiave condivisa gateway↔engine con cui le
    # credenziali delle connessioni DB viaggiano/riposano cifrate. OBBLIGATORIA in
    # ogni ambiente: senza, API e worker non partono (check_required_secrets).
    fernet_key: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# ClickHouse esterno (engine opzionale: un server ClickHouse remoto/cloud)
# ─────────────────────────────────────────────────────────────────────────────
class ClickHouseExternalSettings(BaseModel):
    """Engine `clickhouse`: le trasformazioni girano su un server ClickHouse
    esterno (es. Scaleway/ClickHouse Cloud/self-hosted) invece che nel worker.
    Stesso dialetto e stesse operazioni di chDB; cambia solo DOVE gira e come i
    dati arrivano al server (vedi `transport`). Disattivato finché `host` è vuoto.

    env: CLICKHOUSE_EXTERNAL__HOST, __PORT, __USERNAME, __PASSWORD, __DATABASE,
    __SECURE, __TRANSPORT, __S3_ENDPOINT, __S3_NAMED_COLLECTION, …"""

    host: str = ""  # vuoto = engine non disponibile
    # porta HTTP(S) di ClickHouse (clickhouse-connect): 8123 in chiaro, 8443 TLS
    port: int = 8123
    username: str = "default"
    password: SecretStr = SecretStr("")
    # database di lavoro: DEVE esistere; in modalità `push` ospita le tabelle di
    # staging temporanee (l'utente ha bisogno di CREATE/INSERT/DROP su questo db)
    database: str = "default"
    secure: bool = False  # TLS (i cloud managed lo richiedono: porta 8443)
    connect_timeout: int = 10
    # Come il server raggiunge i dati:
    # - "s3":   ClickHouse legge/scrive i parquet DIRETTAMENTE sull'object storage
    #           con la table function s3() → nessun dato passa dal worker (scelta
    #           di produzione: es. ClickHouse + Object Storage dello stesso cloud).
    #           Richiede che lo storage sia raggiungibile dal server.
    # - "push": il worker carica la sorgente in una tabella di staging e riscarica
    #           il risultato in streaming → funziona con qualsiasi ClickHouse (anche
    #           se non vede lo storage), ma i dati transitano dal worker.
    transport: str = "s3"
    # Endpoint dello storage COME LO VEDE ClickHouse (path-style: <endpoint>/<bucket>/<chiave>).
    # Vuoto = STORAGE__ENDPOINT (giusto solo se il server è sulla stessa rete,
    # es. http://minio:9000 in Docker); in cloud va l'URL pubblico, es.
    # https://s3.fr-par.scw.cloud. Solo per transport=s3.
    s3_endpoint: str = ""
    # Named collection definita SUL SERVER con le credenziali dello storage: se
    # impostata, le chiavi S3 non viaggiano nelle query (né finiscono nel query_log).
    # Es. `CREATE NAMED COLLECTION tabularia_s3 AS access_key_id='…', secret_access_key='…'`.
    s3_named_collection: str = ""
    # tetto di esecuzione per singola query (secondi, 0 = nessuno); le preview
    # interattive lo hanno comunque dal timeout lato API
    max_execution_time: int = 0
    # ── Materializzazione per il viewer (solo transport s3) ───────────────────
    # env: __MATERIALIZE_MIN_ROWS — sopra questa soglia di righe un dataset letto
    # dal viewer viene COPIATO una volta in una MergeTree sul server, e le query
    # successive leggono da lì invece di rileggere il parquet con s3(): sulle
    # scansioni piene (grafici, pivot, ordinamenti) è 2–6× più veloce. 0 =
    # disattivato. Best-effort: se la copia non riesce si resta su s3().
    # SPENTA di default (0). La copia e' SINCRONA dentro la richiesta: su una
    # tabella da 25M righe x 50 colonne non stava nel tetto di 60 s, andava in
    # timeout, e ogni Apply del viewer pagava un minuto per poi leggere comunque
    # da s3(). Con parquet scritti a row group grandi (INGEST__PARQUET_ROW_GROUP_ROWS)
    # la lettura diretta e' gia' rapida: un'aggregazione da 18,7 s e' scesa a
    # 0,64 s senza alcuna copia. Chi la vuole la accende con un valore > 0.
    materialize_min_rows: int = 0
    # env: __MATERIALIZE_DATABASE — database dove creare quelle tabelle; vuoto =
    # lo stesso `database`. Un db dedicato tiene le copie effimere separate.
    materialize_database: str = ""
    # env: __MATERIALIZE_TTL_SECONDS — dopo quanti secondi di INUTILIZZO la copia
    # viene droppata (il conteggio riparte a ogni accesso).
    materialize_ttl_seconds: int = 1800
    # env: __PARQUET_SCAN_MAX_THREADS — sulle query di SCANSIONE (aggregazioni,
    # pivot, sort, join) impone almeno questi thread. Il default di ClickHouse è
    # il numero di core, troppo basso quando leggere da S3 è I/O-bound (i thread
    # aspettano la rete): l'oversubscription nasconde la latenza. NON tocca le
    # query con LIMIT (la vista tabella), che con troppi thread rallentano; non
    # scende mai sotto i core del server. 0 = non toccare max_threads.
    parquet_scan_max_threads: int = 8

    @field_validator("password", mode="before")
    @classmethod
    def _ensure_secret(cls, v: object) -> SecretStr:
        return v if isinstance(v, SecretStr) else SecretStr("" if v is None else str(v))

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: str) -> str:
        v = (v or "s3").strip().lower()
        if v not in ("s3", "push"):
            raise ValueError("clickhouse_external.transport deve essere 's3' o 'push'")
        return v

    @property
    def enabled(self) -> bool:
        return bool(self.host.strip())

    @property
    def materialize_enabled(self) -> bool:
        # solo con transport s3: in push la sorgente è GIÀ una MergeTree di staging
        return self.enabled and self.transport == "s3" and self.materialize_min_rows > 0

    @property
    def matview_sweep_enabled(self) -> bool:
        """Lo SWEEP delle copie (scadute + orfane) non dipende dal fatto che se ne
        creino di nuove: spegnere la materializzazione (min_rows=0) spegneva anche
        lo spazzino, e una tabella temporanea da 5,77 GiB lasciata da una build
        uccisa a metà restava sul server per sempre. Basta che ClickHouse sia
        configurato e che il trasporto sia s3 (in push non esistono copie)."""
        return self.enabled and self.transport == "s3"

    @property
    def matview_database(self) -> str:
        return (self.materialize_database or self.database).strip()


# ─────────────────────────────────────────────────────────────────────────────
# App Configuration
# ─────────────────────────────────────────────────────────────────────────────
class SharePointSettings(BaseModel):
    """Endpoint Microsoft per la sorgente SharePoint. Scelta di DEPLOYMENT, non di
    chi crea la connessione (a lui non è concesso: sarebbe un modo per far
    chiamare al backend un indirizzo qualunque con un token in mano). Servono a
    chi sta su un cloud sovrano — `https://graph.microsoft.us/v1.0` +
    `https://login.microsoftonline.us`, o 21Vianet — e ai test di integrazione."""

    graph_base: str = "https://graph.microsoft.com/v1.0"
    login_base: str = "https://login.microsoftonline.com"


class BigQuerySettings(BaseModel):
    """Engine `bigquery`: le trasformazioni girano su Google BigQuery, che legge i
    parquet DIRETTAMENTE dal bucket come tabelle esterne temporanee (gs://…) e
    restituisce il risultato al worker (Storage Read API). Richiede che lo
    storage sia Google Cloud Storage (STORAGE__ENDPOINT=https://storage.googleapis.com)
    e un service account con «BigQuery Job User» sul progetto e lettura sul
    bucket. Disattivato finche' `project` o le credenziali sono vuoti.

    env: BIGQUERY__PROJECT, __CREDENTIALS_FILE (path della chiave JSON) oppure
    __CREDENTIALS_B64 (la stessa chiave in base64, una riga di .env),
    __LOCATION, __MAXIMUM_BYTES_BILLED"""

    project: str = ""
    credentials_file: str = ""
    credentials_b64: SecretStr = SecretStr("")
    # regione dei job: DEVE combaciare con quella del bucket (o essere la
    # multi-regione che lo contiene, es. EU). Vuota = letta dal bucket.
    location: str = ""
    # tetto di byte FATTURABILI per singola query: BigQuery rifiuta PRIMA di
    # eseguire una query che lo supererebbe. 20 GiB = circa 0,12 $ a query
    # (6,25 $/TiB). 0 = nessun tetto.
    maximum_bytes_billed: int = 20 * 1024**3
    # Step-cache NATIVA: l'output dei passi intermedi dell'editor viene scritto
    # in tabelle di questo dataset (creato dal motore se manca; serve «BigQuery
    # Data Editor» sul progetto, o il dataset gia' creato con quel ruolo su di
    # esso) con scadenza = CACHE__TTL_SECONDS; le preview successive leggono la
    # tabella nativa invece di rileggere il parquet. Vuoto = nessuna cache.
    cache_dataset: str = "tabularia_cache"

    @field_validator("credentials_b64", mode="before")
    @classmethod
    def _ensure_secret_b64(cls, v: object) -> SecretStr:
        return v if isinstance(v, SecretStr) else SecretStr("" if v is None else str(v))

    @property
    def enabled(self) -> bool:
        return bool(self.project.strip()) and bool(self.credentials_file.strip() or self.credentials_b64.get_secret_value().strip())

    def credentials_info(self) -> dict:
        """La chiave JSON del service account come dict (file o base64)."""
        import base64
        import json

        raw = self.credentials_b64.get_secret_value().strip()
        if raw:
            try:
                return json.loads(base64.b64decode(raw))
            except Exception as e:
                raise ValueError(f"BIGQUERY__CREDENTIALS_B64 non e' una chiave JSON in base64: {e}") from e
        with open(self.credentials_file, encoding="utf-8") as f:
            return json.load(f)


class IngestSettings(BaseModel):
    """Scrittura dei parquet prodotti dall'ingest."""

    # Righe per ROW GROUP del parquet. Il writer ne scriveva uno per ogni batch
    # del driver (~8k righe): su 25M righe x 50 colonne faceva 3.063 row group e
    # 153.000 pezzi di colonna, con un footer di 16,6 MB. Con blocchi cosi'
    # piccoli leggere due colonne su cinquanta richiederebbe migliaia di richieste
    # da poche decine di KB, quindi il motore scarica tutto il file: il pruning
    # per colonna che il formato promette non conviene mai. Un milione di righe
    # riporta i blocchi a poche decine.
    # ATTENZIONE alla memoria: il blocco si accumula in RAM prima di essere
    # scritto (~570 MB per 1M righe x 50 colonne larghe), quindi alzarlo troppo
    # fa morire di OOM l'ingest invece di renderlo piu' veloce.
    # il minimo e' basso di proposito: un valore piccolo e' inefficiente, non
    # scorretto, e serve a poter esercitare la soglia nei test senza generare
    # milioni di righe
    parquet_row_group_rows: int = Field(default=1_000_000, ge=1, le=20_000_000)


class AppSettings(BaseModel):
    # NB: nessun codice del backend legge `env_name` — il guard di produzione
    # vive solo nel gateway. Resta qui perché la configurazione condivisa la
    # imposta su tutti i pod, e senza il campo pydantic la ignorerebbe soltanto.
    env_name: str = "development"
    # origini permesse per il CORS (il frontend Nuxt gira su :3000)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main Settings (composes all service settings)
# ─────────────────────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    """
    Main configuration class that composes all service settings.
    
    Priority order (dalla più alta alla più bassa):
    1. Init (argomenti al costruttore)
    2. Variabili d'ambiente (per docker/produzione) — es. `REDIS__HOST=redis`
    3. secrets.toml (secret locali)
    4. config.toml (default locali committabili)
    5. Default delle classi

    I campi annidati usano il delimitatore `__`: `REDIS__HOST` → `redis.host`,
    `STORAGE__ENDPOINT` → `storage.endpoint`, ecc. Così l'env sovrascrive sempre
    il TOML per singolo campo (deep-merge), come da priorità sopra.
    """
    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Nested service configurations
    # ─────────────────────────────────────────────────────────────────────────
    app: AppSettings = Field(default_factory=AppSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    clickhouse_external: ClickHouseExternalSettings = Field(default_factory=ClickHouseExternalSettings)
    bigquery: BigQuerySettings = Field(default_factory=BigQuerySettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    sharepoint: SharePointSettings = Field(default_factory=SharePointSettings)

    # ─────────────────────────────────────────────────────────────────────────
    # Helper properties for quick access
    # ─────────────────────────────────────────────────────────────────────────
    @computed_field
    @property
    def celery_broker_url(self) -> str:
        """Returns Celery broker URL (from celery config or fallback to redis)"""
        return self.celery.broker_url or self.redis.url

    @computed_field
    @property
    def celery_result_backend(self) -> str:
        """Returns Celery result backend (from celery config or fallback to redis)"""
        return self.celery.result_backend or self.redis.url

    def check_required_secrets(self) -> None:
        """La chiave Fernet è OBBLIGATORIA in ogni ambiente, anche in sviluppo:
        senza, le credenziali delle connessioni non si possono cifrare né
        decifrare e le API fallirebbero al primo uso. Meglio non partire, con
        un messaggio chiaro. Chiamata allo startup (API, worker, beat)."""
        from cryptography.fernet import Fernet

        hint = (
            'genera con: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())" e impostala in infrastructure/.env '
            "(identica in gateway ed engine)"
        )
        key = self.security.fernet_key
        if not key:
            raise RuntimeError(f"SECURITY__FERNET_KEY non impostata: {hint}")
        try:
            Fernet(key)
        except Exception as e:  # base64 malformato, lunghezza sbagliata…
            raise RuntimeError(f"SECURITY__FERNET_KEY non valida ({e}): {hint}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Singleton instance
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache
def get_settings() -> Settings:
    """
    Returns cached settings instance.
    
    Usage:
        settings = get_settings()
        settings.redis.url
        settings.storage.bucket
    """
    return Settings()

