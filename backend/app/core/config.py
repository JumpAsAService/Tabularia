from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field, computed_field, field_validator, model_validator, SecretStr
from functools import lru_cache
from pathlib import Path
from typing import Optional


# Note: the nested blocks are *plain* BaseModels. All the loading (environment +
# TOML) happens on the top-level `Settings` through `env_nested_delimiter="__"`,
# so the environment always wins over the TOML, field by field (REDIS__HOST beats
# the value of config.toml). See the note on priorities in `Settings`.


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
# Storage Configuration (any S3-compatible endpoint: MinIO, AWS, R2…)
# ─────────────────────────────────────────────────────────────────────────────
class StorageSettings(BaseModel):
    endpoint: str = Field(default="http://localhost:9002", description="Storage endpoint")
    access_key: str = Field(default="minioadmin", description="Access key")
    secret_key: SecretStr = Field(default=SecretStr("minioadmin"), description="Secret key")
    bucket: str = "data-prep"
    region: str = "us-east-1"
    # env: STORAGE__DIRECT_READ — the duckdb and chdb engines read sources and cached
    # steps in place, only the parts a query needs (app/engine/storage_direct.py).
    # false = download each file first, as before.
    direct_read: bool = True

    @field_validator("secret_key", mode="before")
    @classmethod
    def _ensure_secret(cls, v: object) -> SecretStr:
        # pydantic-settings does not wrap the values coming from the TOML in
        # SecretStr (they stay str): coerce them, whatever the source.
        return v if isinstance(v, SecretStr) else SecretStr(str(v))



class BackupStorageSettings(BaseModel):
    """Where the backups of Tabularia's database go (the gateway makes them, the engine
    stores them: it holds the storage credentials). Empty fields fall back to STORAGE__*:
    by default the backups are in Tabularia's bucket, under their own prefix. A bucket of
    their own is advised in production, so that losing one does not lose both."""
    # env: BACKUP__BUCKET — empty = STORAGE__BUCKET
    bucket: str = ""
    # env: BACKUP__PREFIX — the folder of the backups in the bucket
    prefix: str = "backups/postgres/"
    # env: BACKUP__ENDPOINT / BACKUP__REGION / BACKUP__ACCESS_KEY / BACKUP__SECRET_KEY —
    # another object storage, or other keys; empty = STORAGE__*'s
    endpoint: str = ""
    region: str = ""
    access_key: str = ""
    secret_key: SecretStr = Field(default=SecretStr(""))

    @field_validator("secret_key", mode="before")
    @classmethod
    def _ensure_secret(cls, v: object) -> SecretStr:
        return v if isinstance(v, SecretStr) else SecretStr(str(v))

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        v = v.strip().lstrip("/")
        return v if not v or v.endswith("/") else v + "/"

# ─────────────────────────────────────────────────────────────────────────────
# Celery Configuration
# ─────────────────────────────────────────────────────────────────────────────
class CelerySettings(BaseModel):
    broker_url: Optional[str] = None
    result_backend: Optional[str] = None
    worker_concurrency: int = 2
    # env: CELERY__TASK_TIME_LIMIT / CELERY__TASK_SOFT_TIME_LIMIT — the longest a
    # task may last. At the SOFT limit the task gets an exception and can close in
    # order (temporary files, connections); at the HARD limit the worker kills its
    # process.
    #
    # TIED TO THE GATEWAY: `ENGINE__RUN_STALE_TIMEOUT_SECONDS` must stay GREATER
    # than `task_time_limit`, because the gateway calls a run lost only after Celery
    # has already killed it — the invariant behind not enabling `task_acks_late`
    # (see celery_app.py). Raising only one of the two does not lengthen the time a
    # run is allowed: it only changes how long one waits before seeing it fail.
    task_time_limit: int = 3600
    task_soft_time_limit: int = 3300
    task_serializer: str = "json"
    result_serializer: str = "json"
    timezone: str = "UTC"
    enable_utc: bool = True
    # env: CELERY__MAX_TASKS_PER_CHILD — after how many tasks a prefork child
    # process is recycled. The Polars/Arrow tasks allocate through glibc malloc,
    # which does NOT give freed memory back to the OS (fragmented arenas): a
    # recycled child releases everything → the RSS does not grow in steps. Kept
    # high so that the light periodic tasks (storage stats every 60 s, eviction) do
    # not recycle the child all the time: the real cap is the memory below. Def 50.
    max_tasks_per_child: int = 50
    # env: CELERY__MAX_MEMORY_PER_CHILD_KB — RSS ceiling (KB) above which the
    # child is recycled at the END of its task (a clean release of what is left).
    # None (default) = DERIVED from the container's memory limit (cgroup) and the
    # concurrency, so it stays right whatever WORKER_MEM_LIMIT is in production —
    # see resolve_max_memory_per_child_kb(). Set it only to force a fixed value.
    max_memory_per_child_kb: Optional[int] = None
    # share of the container's limit kept for the worker's children (the rest is a
    # margin for the main process, beat, page cache): per child = limit*frac/concurrency
    max_memory_headroom_frac: float = 0.75
    # fallback when the cgroup limit is unreadable or unlimited (KB, ~1.5 GB)
    max_memory_per_child_fallback_kb: int = 1_500_000
    # floor: never recycle below this (avoids recycling too often)
    max_memory_per_child_floor_kb: int = 512_000


def cgroup_mem_limit_bytes() -> Optional[int]:
    """The container's memory limit read from the cgroup (the ceiling the kernel
    really enforces, however it was configured). None when unlimited or unreadable
    (outside a container)."""
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
        # v1's "unlimited" is a huge integer (~2^63): no limit
        if val <= 0 or val >= (1 << 62):
            return None
        return val
    return None


def cgroup_mem_used_bytes() -> Optional[int]:
    """Memory the container uses now, as the kernel counts it against the limit above
    (page cache included). None outside a container."""
    for path in ("/sys/fs/cgroup/memory.current",                # cgroup v2
                 "/sys/fs/cgroup/memory/memory.usage_in_bytes"):  # cgroup v1
        try:
            return int(Path(path).read_text().strip())
        except (OSError, ValueError):
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Workers: processes per worker container
# ─────────────────────────────────────────────────────────────────────────────
class WorkersSettings(BaseModel):
    # env: WORKERS__RUN_CONCURRENCY / WORKERS__PREVIEW_CONCURRENCY — processes per
    # worker container, for the run queue (`celery`) and for the preview queue.
    # Usually set from Admin → Workers: every worker resizes its pool within seconds,
    # without stopping the tasks it is running (app/tasks/worker_pool.py).
    # 0 = keep the size the container started with (CELERY__WORKER_CONCURRENCY).
    run_concurrency: int = 0
    preview_concurrency: int = 0


def resolve_max_memory_per_child_kb(celery: CelerySettings) -> int:
    """RSS ceiling per worker child (KB). Unless forced from the environment, derived
    from the cgroup limit and the concurrency: `limit * frac / concurrency`. Two (or
    N) resident children then stay under the container's limit with a margin, and the
    value follows WORKER_MEM_LIMIT and the concurrency by itself."""
    if celery.max_memory_per_child_kb:  # set explicitly
        return celery.max_memory_per_child_kb
    limit = cgroup_mem_limit_bytes()
    if not limit:
        return celery.max_memory_per_child_fallback_kb
    concurrency = max(1, celery.worker_concurrency)
    per_child_kb = int(limit * celery.max_memory_headroom_frac / concurrency / 1024)
    return max(per_child_kb, celery.max_memory_per_child_floor_kb)


# ─────────────────────────────────────────────────────────────────────────────
# Cache Configuration (the engine's step cache)
# ─────────────────────────────────────────────────────────────────────────────
class CacheSettings(BaseModel):
    # env: CACHE__TTL_SECONDS — how long after its last access a cache entry is
    # removed (parquet blob + Valkey index). Default 1 day.
    ttl_seconds: int = 1 * 24 * 3600
    # env: CACHE__SWEEP_INTERVAL_SECONDS — how often the eviction runs. Default 1 h.
    sweep_interval_seconds: int = 3600
    # env: CACHE__MAX_STEP_ROWS — a step with more rows than this is NOT cached: a
    # copy as large as the source gains nothing over reading the original parquet
    # again, and costs gigabytes of writing (25M rows = 2.4 GB and 40 s on the
    # external ClickHouse). The step is computed again from the source. 0 = no cap.
    max_step_rows: int = 1_000_000


# ─────────────────────────────────────────────────────────────────────────────
# Metrics Configuration (observability)
# ─────────────────────────────────────────────────────────────────────────────
class MetricsSettings(BaseModel):
    # env: METRICS__STORAGE_STATS_INTERVAL_SECONDS — how often the size of the
    # storage prefixes (cache/, datasets/, ...) is sampled. Default 60 s.
    storage_stats_interval_seconds: int = 60


# ─────────────────────────────────────────────────────────────────────────────
# Security (secrets shared with the gateway)
# ─────────────────────────────────────────────────────────────────────────────
class SecuritySettings(BaseModel):
    # env: SECURITY__FERNET_KEY — the key gateway and engine share, with which the
    # connections' credentials travel and rest encrypted. REQUIRED everywhere:
    # without it the API and the workers do not start (check_required_secrets).
    fernet_key: str = ""
    # env: SECURITY__KEYS_DIR — a directory the services share (one machine, Compose)
    # where the secrets left empty are created at the first start and read afterwards:
    # the Fernet key (fernet.key) and, with AUTH__FIRST_RUN_SETUP, the sample-data
    # container's start-up token (bootstrap.token). Empty = no such directory: the key
    # must be set. Back it up with the database: without fernet.key the connection
    # passwords stored in Tabularia cannot be read.
    keys_dir: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# External ClickHouse (an optional engine: a remote or cloud ClickHouse server)
# ─────────────────────────────────────────────────────────────────────────────
class ClickHouseExternalSettings(BaseModel):
    """Engine `clickhouse`: the transformations run on an external ClickHouse
    server (Scaleway, ClickHouse Cloud, self-hosted) instead of in the worker. The
    same dialect and operations as chDB; only WHERE it runs changes, and how the
    data reaches the server (see `transport`). Off while `host` is empty.

    env: CLICKHOUSE_EXTERNAL__HOST, __PORT, __USERNAME, __PASSWORD, __DATABASE,
    __SECURE, __TRANSPORT, __S3_ENDPOINT, __S3_NAMED_COLLECTION, …"""

    host: str = ""  # empty = engine not available
    # ClickHouse's HTTP(S) port (clickhouse-connect): 8123 in clear, 8443 TLS
    port: int = 8123
    username: str = "default"
    password: SecretStr = SecretStr("")
    # A user DEDICATED to the AI assistant's queries (env: CLICKHOUSE_EXTERNAL__
    # AI_USERNAME / __AI_PASSWORD). A second barrier after the sql node's allowlist:
    # read only, no `system`. Used only with BOTH set; otherwise the assistant uses
    # the user above, as before. The grants it needs: docs/engines/clickhouse-ai-user.md
    ai_username: str = ""
    ai_password: SecretStr = SecretStr("")
    # the working database: it MUST exist; with `push` it holds the temporary
    # staging tables (the user needs CREATE/INSERT/DROP on it)
    database: str = "default"
    secure: bool = False  # TLS (managed clouds require it: port 8443)
    connect_timeout: int = 10
    # How the server reaches the data:
    # - "s3":   ClickHouse reads and writes the parquet files DIRECTLY on the object
    #           storage with the s3() table function → no data goes through the
    #           worker (the production choice: ClickHouse and object storage of the
    #           same cloud). The storage must be reachable from the server.
    # - "push": the worker loads the source into a staging table and streams the
    #           result back → works with any ClickHouse (even one that cannot see
    #           the storage), but the data goes through the worker.
    transport: str = "s3"
    # The storage endpoint AS CLICKHOUSE SEES IT (path style: <endpoint>/<bucket>/<key>).
    # Empty = STORAGE__ENDPOINT (right only when the server is on the same network,
    # http://minio:9000 in Docker); on a cloud, the public URL, such as
    # https://s3.fr-par.scw.cloud. For transport=s3 only.
    s3_endpoint: str = ""
    # A named collection defined ON THE SERVER with the storage's credentials: when
    # set, the S3 keys do not travel in the queries (nor end up in the query_log).
    # `CREATE NAMED COLLECTION tabularia_s3 AS access_key_id='…', secret_access_key='…'`.
    s3_named_collection: str = ""
    # the longest one query may run (seconds, 0 = no limit); the interactive
    # previews have one anyway, from the API's timeout
    max_execution_time: int = 0
    # ── Materialization for the viewer (transport s3 only) ────────────────────
    # env: __MATERIALIZE_MIN_ROWS — above this many rows, a dataset read by the
    # viewer is COPIED once into a MergeTree on the server, and the next queries
    # read it there instead of reading the parquet again with s3(): on full scans
    # (charts, pivots, sorts) 2–6× faster. 0 = off. Best effort: if the copy fails
    # the queries stay on s3().
    # OFF by default (0). The copy is SYNCHRONOUS within the request: a table of
    # 25M rows x 50 columns did not fit in the 60 s cap, timed out, and every Apply
    # of the viewer paid a minute only to read from s3() anyway. With parquet files
    # written in large row groups (INGEST__PARQUET_ROW_GROUP_ROWS) reading in place
    # is fast already: an aggregation went from 18.7 s to 0.64 s without any copy.
    # Whoever wants it turns it on with a value > 0.
    materialize_min_rows: int = 0
    # env: __MATERIALIZE_DATABASE — the database for those tables; empty = the same
    # `database`. A dedicated one keeps the short-lived copies apart.
    materialize_database: str = ""
    # env: __MATERIALIZE_TTL_SECONDS — after how many seconds UNUSED a copy is
    # dropped (the count starts again at every access).
    materialize_ttl_seconds: int = 1800
    # env: __PARQUET_SCAN_MAX_THREADS — on the SCAN queries (aggregations, pivots,
    # sorts, joins) at least this many threads. ClickHouse's default is the number of
    # cores, too low when reading from S3 is I/O-bound (the threads wait on the
    # network): oversubscription hides the latency. Does NOT touch the queries with
    # a LIMIT (the table view), which slow down with too many threads; never below
    # the server's cores. 0 = leave max_threads alone.
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
            raise ValueError("clickhouse_external.transport must be 's3' or 'push'")
        return v

    @property
    def enabled(self) -> bool:
        return bool(self.host.strip())

    @property
    def materialize_enabled(self) -> bool:
        # with transport s3 only: with push the source is ALREADY a staging MergeTree
        return self.enabled and self.transport == "s3" and self.materialize_min_rows > 0

    @property
    def matview_sweep_enabled(self) -> bool:
        """The SWEEP of the copies (expired and orphaned) does not depend on new ones
        being made: turning the materialization off (min_rows=0) turned the sweeper
        off too, and a 5.77 GiB temporary table left by a build killed half way stayed
        on the server for ever. ClickHouse configured and the s3 transport are enough
        (with push there are no copies)."""
        return self.enabled and self.transport == "s3"

    @property
    def matview_database(self) -> str:
        return (self.materialize_database or self.database).strip()


# ─────────────────────────────────────────────────────────────────────────────
# App Configuration
# ─────────────────────────────────────────────────────────────────────────────
class SharePointSettings(BaseModel):
    """Microsoft endpoints of the SharePoint source. A DEPLOYMENT choice, not one of
    whoever creates the connection (not allowed to them: it would be a way to make
    the backend call any address with a token in hand). For a sovereign cloud —
    `https://graph.microsoft.us/v1.0` + `https://login.microsoftonline.us`, or
    21Vianet — and for the integration tests."""

    graph_base: str = "https://graph.microsoft.com/v1.0"
    login_base: str = "https://login.microsoftonline.com"


class SftpSettings(BaseModel):
    """Caps of the SFTP datasource («SFTP folder»): a glob gone wrong (`**/*`) must
    not download a whole server, nor a single huge file fill the worker's disk.
    The import is refused, naming the file, before anything is downloaded.

    env: SFTP__MAX_FILE_BYTES, SFTP__MAX_FILES, SFTP__MAX_TOTAL_BYTES, SFTP__TIMEOUT_SECONDS"""

    # bytes of ONE file; every matching file is downloaded to the worker's /tmp
    # and read in memory to be stacked, so this is also a memory bound
    max_file_bytes: int = Field(default=512 * 1024**2, ge=1)
    # files read by one refresh
    max_files: int = Field(default=500, ge=1)
    # bytes of all the matching files together, checked on their listed sizes
    # before anything is downloaded (as SharePoint's 2 GB)
    max_total_bytes: int = Field(default=2 * 1024**3, ge=1)
    # connection and SSH handshake; a silent server fails within this time
    timeout_seconds: float = Field(default=30.0, gt=0)


class S3SourceSettings(BaseModel):
    """Caps of the «S3 folder» datasource, as the SFTP ones: checked on the sizes of
    the object listing BEFORE anything is downloaded; the import is refused naming
    the file.

    env: S3_SOURCE__MAX_FILE_BYTES, S3_SOURCE__MAX_FILES, S3_SOURCE__MAX_TOTAL_BYTES,
    S3_SOURCE__MAX_LISTED, S3_SOURCE__MAX_REQUEST_SECONDS"""

    # bytes of ONE object: downloaded to the worker's /tmp and read in memory
    max_file_bytes: int = Field(default=512 * 1024**2, ge=1)
    # files read by one refresh
    max_files: int = Field(default=500, ge=1)
    # bytes of all the matching objects together
    max_total_bytes: int = Field(default=2 * 1024**3, ge=1)
    # keys the globs of one refresh may scan under their fixed folders, all the
    # expansions together: `**` on a huge bucket must stop
    max_listed: int = Field(default=100_000, ge=1)
    # wall-clock time of ONE object's download (each wait on the network is bounded
    # by botocore's own read timeout, 60 s): a server that drips the bytes stops here
    max_request_seconds: float = Field(default=600.0, gt=0)


class ApiSettings(BaseModel):
    """Caps and manners of the «API call» datasource (HTTP APIs read at each refresh).

    A datasource can LOWER the per-call, per-refresh and row limits for itself, never
    raise them above these. Going over a limit fails the refresh with a message that
    names it: the data is never cut silently.

    env: API__TIMEOUT_SECONDS, API__MAX_PAGES, API__MAX_CALLS, API__MAX_ROWS,
    API__MAX_RESPONSE_BYTES, API__MAX_TOTAL_BYTES, API__MAX_RETRIES,
    API__MAX_RETRY_WAIT_SECONDS, API__MAX_REDIRECTS, API__MAX_REQUEST_SECONDS, API__MIN_INTERVAL_SECONDS"""

    # connection and read timeout of ONE request (the token request too)
    timeout_seconds: float = Field(default=30.0, gt=0)
    # pages of ONE call (one combination of the variables): a pagination that never
    # ends stops here
    max_pages: int = Field(default=1000, ge=1)
    # calls of one refresh: the combinations of the list variables
    max_calls: int = Field(default=500, ge=1)
    # rows received by one refresh (before an append merges them with the table)
    max_rows: int = Field(default=2_000_000, ge=1)
    # bytes of ONE response, decompressed: it is held in memory to be parsed
    max_response_bytes: int = Field(default=100 * 1024**2, ge=1)
    # bytes of all the responses of one refresh
    max_total_bytes: int = Field(default=1024**3, ge=1)
    # retries of a request that failed with 429, 500, 502, 503, 504 or a network error,
    # with an exponential backoff (1, 2, 4, 8… s) or the server's Retry-After
    max_retries: int = Field(default=4, ge=0)
    # a Retry-After longer than this fails the refresh instead of holding a worker
    max_retry_wait_seconds: float = Field(default=120.0, ge=0)
    max_redirects: int = Field(default=5, ge=0)
    # wall-clock time of ONE request, from sending it to the last byte of its answer:
    # the timeout above bounds each wait on the network, this one the whole request
    # (a server that drips its answer a byte at a time)
    max_request_seconds: float = Field(default=300.0, gt=0)
    # the minimum pause between two requests of one refresh; a datasource can ask for more
    min_interval_seconds: float = Field(default=0.0, ge=0)


class PlaceholderSettings(BaseModel):
    """The placeholders of the inputs and outputs (app.core.placeholders).

    env: PLACEHOLDERS__MAX_VALUES"""

    # values of ONE range variable (`day = today-400 .. today step 1d` is 400), and
    # combinations of all the variables of a file datasource (each one a listing of
    # the folder): checked when the datasource is saved and at every refresh. An API
    # call is bounded by API__MAX_CALLS as well.
    max_values: int = Field(default=1000, ge=1)


class BigQuerySettings(BaseModel):
    """Engine `bigquery`: the transformations run on Google BigQuery, which reads the
    parquet files DIRECTLY from the bucket as temporary external tables (gs://…) and
    returns the result to the worker (Storage Read API). The storage must be Google
    Cloud Storage (STORAGE__ENDPOINT=https://storage.googleapis.com), with a service
    account that is «BigQuery Job User» on the project and reads the bucket. Off
    while `project` or the credentials are empty.

    env: BIGQUERY__PROJECT, __CREDENTIALS_FILE (path of the JSON key) or
    __CREDENTIALS_B64 (the same key in base64, one line of .env),
    __LOCATION, __MAXIMUM_BYTES_BILLED"""

    project: str = ""
    credentials_file: str = ""
    credentials_b64: SecretStr = SecretStr("")
    # the jobs' region: it MUST match the bucket's (or be the multi-region that
    # holds it, such as EU). Empty = read from the bucket.
    location: str = ""
    # cap on the BILLABLE bytes of one query: BigQuery refuses BEFORE running a
    # query that would go over it. 20 GiB = about $0.12 a query ($6.25/TiB).
    # 0 = no cap.
    maximum_bytes_billed: int = 20 * 1024**3
    # NATIVE step cache: the output of the editor's intermediate steps is written to
    # tables of this dataset (created by the engine when missing; it needs «BigQuery
    # Data Editor» on the project, or the dataset made already with that role on it)
    # expiring after CACHE__TTL_SECONDS; the next previews read the native table
    # instead of the parquet. Empty = no cache.
    cache_dataset: str = "tabularia_cache"

    @field_validator("credentials_b64", mode="before")
    @classmethod
    def _ensure_secret_b64(cls, v: object) -> SecretStr:
        return v if isinstance(v, SecretStr) else SecretStr("" if v is None else str(v))

    @property
    def enabled(self) -> bool:
        return bool(self.project.strip()) and bool(self.credentials_file.strip() or self.credentials_b64.get_secret_value().strip())

    def credentials_info(self) -> dict:
        """The service account's JSON key as a dict (from the file or the base64)."""
        import base64
        import json

        raw = self.credentials_b64.get_secret_value().strip()
        if raw:
            try:
                return json.loads(base64.b64decode(raw))
            except Exception as e:
                raise ValueError(f"BIGQUERY__CREDENTIALS_B64 is not a base64-encoded JSON key: {e}") from e
        with open(self.credentials_file, encoding="utf-8") as f:
            return json.load(f)


class IngestSettings(BaseModel):
    """How the parquet files the ingest makes are written."""

    # Rows per ROW GROUP of the parquet. The writer made one for each batch of the
    # driver (~8k rows): 25M rows x 50 columns made 3,063 row groups and 153,000
    # column chunks, with a 16.6 MB footer. With blocks that small, reading two
    # columns of fifty would take thousands of requests of a few tens of KB, so the
    # engine downloads the whole file: the column pruning the format promises never
    # pays. A million rows brings the blocks down to a few tens.
    # MIND THE MEMORY: a block builds up in RAM before it is written (~570 MB for 1M
    # rows x 50 wide columns), so raising it too far kills the ingest with an OOM
    # instead of making it faster.
    # The minimum is low on purpose: a small value is inefficient, not wrong, and
    # lets the tests reach the threshold without generating millions of rows
    parquet_row_group_rows: int = Field(default=1_000_000, ge=1, le=20_000_000)
    # How long ClickHouse's answer is awaited, receiving nothing, during an import or
    # a write (env: INGEST__DB_READ_TIMEOUT_SECONDS). ClickHouse sends nothing until
    # it has the first row: with sort keys on columns outside the table's key it must
    # read and sort EVERYTHING first (50M rows, 3.4 GB, one thread: 14 s of silence,
    # growing with the table). The client keeps the connection alive with progress
    # headers, but not with a `readonly=1` user (it cannot ask for them) nor behind a
    # proxy that holds them back: there clickhouse-connect's default, 300 s, failed
    # the imports of large tables after five minutes. The other sources' drivers have
    # no limit of their own. Kept BELOW `CELERY__TASK_SOFT_TIME_LIMIT`, so whoever
    # waits reads this error and not the killed task's. The connection test and the
    # table list keep 300 s: they answer at once.
    db_read_timeout_seconds: int = Field(default=3000, ge=10)


class ExportSettings(BaseModel):
    """Downloading a result (CSV/Excel) from the editor, the viewer and the chat."""

    # The most rows of a CSV export. The engine writes it as a stream, but the browser
    # gathers it all in memory before saving it (fetch → Blob): beyond a few million
    # rows the tab dies. Larger ones are written with an Output node (datasource,
    # table, bucket). 0 = no cap. env: EXPORT__MAX_ROWS
    max_rows: int = Field(default=5_000_000, ge=0)


class SmtpSettings(BaseModel):
    """Email destinations and notices (SMTP connections).

    env: SMTP__VERIFY_TLS — verify the SMTP server's certificate and name, with
    STARTTLS and with implicit TLS. false = the deployment's explicit opt-out for a
    relay with a self-signed certificate (the password then travels encrypted but
    to a server nobody vouched for)."""

    verify_tls: bool = True


class UploadSettings(BaseModel):
    """File uploads (POST /files), the same variable as the gateway's: it refuses
    first, the engine API is the second barrier (app/api/upload_limit.py).

    env: UPLOAD__MAX_BYTES — the largest file accepted, in bytes; 0 = no ceiling."""

    max_bytes: int = Field(default=1024 ** 3, ge=0)


class AppSettings(BaseModel):
    # NB: no code of the backend reads `env_name` — the production guard lives in
    # the gateway only. It stays because the shared configuration sets it on every
    # pod, and without the field pydantic would merely ignore it.
    env_name: str = "development"
    # origins allowed by CORS (the Nuxt frontend runs on :3000)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    # How many synchronous requests the engine API serves at once. Every preview in
    # progress holds a thread for as long as it lasts (it waits for the worker,
    # checking the result every quarter of a second): with the library's default,
    # 40, the forty-first waits in line even with the workers idle. Sleeping
    # threads, cheap. env: APP__ENGINE_API_THREADS
    engine_api_threads: int = Field(default=200, ge=40)


# ─────────────────────────────────────────────────────────────────────────────
# Main Settings (composes all service settings)
# ─────────────────────────────────────────────────────────────────────────────

# How to make a Fernet key with the tools of any machine: the earlier hint imported
# the `cryptography` package, which a fresh machine does not have. Same commands as
# infrastructure/.env.example and the getting-started guide.
FERNET_KEY_HINT = (
    "generate one with: openssl rand -base64 32 | tr '+/' '-_'  "
    "(or: python3 -c \"import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())\")"
)

class Settings(BaseSettings):
    """
    Main configuration class that composes all service settings.
    
    Priority order (highest first):
    1. Init (constructor arguments)
    2. Environment variables (Docker, production) — `REDIS__HOST=redis`
    3. secrets.toml (local secrets)
    4. config.toml (local defaults that may be committed)
    5. The classes' defaults

    Nested fields use the `__` delimiter: `REDIS__HOST` → `redis.host`,
    `STORAGE__ENDPOINT` → `storage.endpoint`, and so on. The environment then
    overrides the TOML field by field (a deep merge), as the priorities above say.
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
    backup: BackupStorageSettings = Field(default_factory=BackupStorageSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    workers: WorkersSettings = Field(default_factory=WorkersSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    clickhouse_external: ClickHouseExternalSettings = Field(default_factory=ClickHouseExternalSettings)
    bigquery: BigQuerySettings = Field(default_factory=BigQuerySettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)
    sharepoint: SharePointSettings = Field(default_factory=SharePointSettings)
    sftp: SftpSettings = Field(default_factory=SftpSettings)
    s3_source: S3SourceSettings = Field(default_factory=S3SourceSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    placeholders: PlaceholderSettings = Field(default_factory=PlaceholderSettings)

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

    @model_validator(mode="after")
    def _key_from_keys_dir(self) -> "Settings":
        """No SECURITY__FERNET_KEY but a SECURITY__KEYS_DIR: the key is the one in that
        directory, created at the first start by whichever service gets there first."""
        if not self.security.fernet_key and self.security.keys_dir:
            from cryptography.fernet import Fernet

            from app.core.keys import read_or_create

            self.security.fernet_key = read_or_create(
                self.security.keys_dir, "fernet.key", lambda: Fernet.generate_key().decode())
        return self

    def check_required_secrets(self) -> None:
        """The Fernet key is REQUIRED everywhere, in development too: without it the
        connections' credentials can be neither encrypted nor decrypted, and the API
        would fail at the first use. Better not to start, with a clear message.
        Called at start-up (API, workers, beat)."""
        from cryptography.fernet import Fernet

        hint = (f"{FERNET_KEY_HINT}, and set it in infrastructure/.env (the same in gateway and engine), "
                "or give the services a shared SECURITY__KEYS_DIR where it is created")
        key = self.security.fernet_key
        if not key:
            raise RuntimeError(f"SECURITY__FERNET_KEY is not set: {hint}")
        try:
            Fernet(key)
        except Exception as e:  # malformed base64, wrong length…
            raise RuntimeError(f"SECURITY__FERNET_KEY is not valid ({e}): {hint}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Singleton instance
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache
def settings_from_env() -> Settings:
    """The settings of the environment (variables, `.env`), read once per process:
    the infrastructure (addresses, credentials) and the starting values of all the
    rest."""
    return Settings()


def get_settings() -> Settings:
    """The settings in force: the environment's, with the values an administrator
    changed from the panel on top (see core/panel_settings.py). While there are none,
    the same object as `settings_from_env()`.

    Usage:
        settings = get_settings()
        settings.redis.url
        settings.storage.bucket
    """
    from app.core import panel_settings

    return panel_settings.applied(settings_from_env())


def _clear_cache() -> None:
    """As `lru_cache.cache_clear()`, which the tests call after changing the environment."""
    from app.core import panel_settings

    settings_from_env.cache_clear()
    panel_settings.forget()


get_settings.cache_clear = _clear_cache  # type: ignore[attr-defined]

