"""The gateway's request/response schemas. Kept apart from the DB models so that
`hashed_password` never goes out, and to validate the input."""
from datetime import datetime, timezone
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar
from pydantic import AfterValidator, BaseModel, PlainSerializer, Field, field_validator, model_validator

# ── Outgoing datetimes: always with the offset ────────────────────────────────
# The DB's TIMESTAMP columns are naive and in UTC by convention (see scheduler.py).
# Serialized as they were, they reached the browser WITHOUT an offset
# ("2026-09-16T20:57:02") and `new Date()` read them as LOCAL time: every run time
# showed shifted by the client's offset (in Rome, two hours earlier) — in the Gantt a
# run just started seemed to last two hours. Here what the DB implies is made
# explicit: +00:00. Only in JSON: in Python they stay datetimes, so comparisons and
# tests do not change.
def _utc_iso(d: datetime) -> str:
    return (d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)).isoformat()


UtcDateTime = Annotated[datetime, PlainSerializer(_utc_iso, return_type=str, when_used="json")]

from app.models.permission import Capability

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """A page of results: the items + the total that matches the filter (to show 'X–Y of
    Z' and navigate). The `q` filter ALWAYS runs over the whole dataset on the server,
    not over the current page."""
    items: list[T]
    total: int


# Note: the email is a plain `str`, not `EmailStr`. It is an internal tool: admins
# often use reserved domains (e.g. *.local, *.internal) the deliverability validator
# would refuse. Here the email is only a login identifier.


# ── Auth ──────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── Users ─────────────────────────────────────────────────────────────────────
class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    is_active: bool
    is_superuser: bool
    # Informative fields for the Admin page. ALL with a default: `MeOut` inherits from
    # here and is built by hand in routes/auth.py, which passes only the first five.
    created_at: Optional[UtcDateTime] = None
    last_seen_at: Optional[UtcDateTime] = None  # the last authenticated activity
    sso_only: bool = False  # no local password: signs in from the IdP only
    groups: list[str] = []
    # The admin groups they belong to: they are admin even when `is_superuser` (the
    # PERSONAL flag, the one the switch changes) is false.
    admin_groups: list[str] = []
    # Observer: READS the admin panels, writes nothing. As above, here the flag is the
    # PERSONAL one and the groups are the other way. An administrator is an observer by
    # definition, without the flag being on: whoever reads this field is looking at a
    # switch, not a permission — for the permission there is `permissions.is_observer`.
    is_observer: bool = False
    observer_groups: list[str] = []
    # a service account: an integration, it signs in with an API token only
    is_service: bool = False


class MeOut(UserOut):
    """Oneself. Beware: unlike `UserOut`, here `is_superuser` and `is_observer` are
    EFFECTIVE — `routes/auth.py` computes them with `permissions.is_admin`/`is_observer`,
    so they account for the groups. It is the field the frontend decides what to show
    on, and it must answer «may I?», not «do I have the flag?»."""

    groups: list[str] = []
    # AUTH__SESSION_IDLE_MINUTES: the page signs out by itself after that many minutes
    # without anyone touching it (0 = never)
    session_idle_minutes: int = 0
    # AUTH__PERSONAL_TOKENS: users may create API tokens for their scripts
    personal_tokens: bool = False
    # AUTH__API_TOKEN_MAX_DAYS: the farthest expiry a token may have
    api_token_max_days: int = 365
    # the language last chosen in the interface (None = never): the emails follow it
    locale: Optional[str] = None


# A new password, wherever one is set (user creation, an administrator's change,
# one's own change): the one rule of core/passwords.py, also the bootstrap
# administrator's. Refused with a 422 that says why.
def _check_new_password(v: str) -> str:
    from app.core.passwords import password_problem

    problem = password_problem(v)
    if problem:
        raise ValueError(problem)
    return v


NewPassword = Annotated[str, AfterValidator(_check_new_password)]


class UserCreate(BaseModel):
    email: str
    password: NewPassword
    full_name: str = ""
    is_superuser: bool = False
    is_observer: bool = False


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    password: Optional[NewPassword] = None
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None
    is_observer: Optional[bool] = None


class LocaleChange(BaseModel):
    """The language chosen in the interface: one of its five."""
    locale: Literal["en", "it", "fr", "de", "es"]


class PasswordChange(BaseModel):
    """One's own password: the current one proves it is the owner at the keyboard,
    not someone who found the session open."""
    current_password: str
    new_password: NewPassword



# ── Groups ────────────────────────────────────────────────────────────────────
class GroupOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: Optional[UtcDateTime] = None
    member_count: int = 0
    is_admin: bool = False  # the members are administrators
    is_observer: bool = False  # the members read the panels, without writing


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupUpdate(BaseModel):
    description: Optional[str] = None
    is_admin: Optional[bool] = None
    is_observer: Optional[bool] = None


# ── Banners ───────────────────────────────────────────────────────────────────
class BannerOut(BaseModel):
    id: int
    message: str
    level: str
    created_at: Optional[UtcDateTime] = None
    created_by: Optional[int] = None


class BannerCreate(BaseModel):
    """The level decides only the icon and the colour: a banner is always visible to all."""
    message: str = Field(min_length=1, max_length=500)
    level: Literal["info", "warning", "danger"] = "warning"


# ── Engine policy (the engines the installation allows) ───────────────────────
class EnginePolicyOut(BaseModel):
    """The state of an engine for the installation.

    `flows_using` is the migration's work list: disabling an engine does not stop the
    flows that use it already, so the number says how many are still off standard —
    otherwise the standardization would stay cosmetic."""
    engine_id: str
    allowed: bool
    flows_using: int = 0


class EnginePolicyUpdate(BaseModel):
    allowed: bool


# ── Saved views (Viewer configurations saved in a folder) ─────────────────────
class SavedViewOut(BaseModel):
    """`datasource_name` is resolved on the server: a folder's listing must be able to say
    WHAT the view is about without the client resolving N ids."""
    id: int
    name: str
    description: str = ""
    project_id: int
    owner_id: Optional[int] = None
    datasource_id: int
    datasource_name: Optional[str] = None
    # opaque JSON: the shape belongs to the Viewer, the gateway checks only its syntax
    # (see models/saved_view.py)
    spec: str = "{}"
    created_at: Optional[UtcDateTime] = None
    updated_at: Optional[UtcDateTime] = None


class SavedViewCreate(BaseModel):
    name: str
    description: str = ""
    datasource_id: int
    spec: str = "{}"


class SavedViewUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    spec: Optional[str] = None
    project_id: Optional[int] = None  # set = move to another folder


# ── Flow groups (steps in order, cells of a step in parallel) ──────────────────
MAX_GROUP_STEPS = 50
MAX_GROUP_COLUMNS = 20


PUSHMAIL_MAX_ROWS = 1_000_000  # under the Excel format's own limit
PUSHMAIL_MAX_ATTACHMENTS = 20
PUSHMAIL_MAX_RECIPIENTS = 50


class PushmailAttachmentCell(BaseModel):
    """A datasource of a Pushmail cell: its file's name (a template with the Outputs'
    placeholders, `.xlsx` added) and the rows it may hold at most."""
    # left empty while the cell is being set up: services/pushmail.py says what is missing,
    # in words, instead of a bare validation error
    datasourceId: Optional[int] = None
    fileName: str = Field(default="", max_length=255)
    maxRows: int = Field(default=10_000, ge=1, le=PUSHMAIL_MAX_ROWS)


class PushmailCell(BaseModel):
    """A Pushmail cell's settings: an email through an SMTP connection, one .xlsx per
    datasource (docs/pushmail.md)."""
    label: str = Field(default="", max_length=200)
    connectionId: Optional[int] = None
    to: list[str] = Field(default_factory=list, max_length=PUSHMAIL_MAX_RECIPIENTS)
    cc: list[str] = Field(default_factory=list, max_length=PUSHMAIL_MAX_RECIPIENTS)
    subject: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=20_000)
    bodyIsHtml: bool = False
    timezone: str = Field(default="UTC", max_length=64)  # of the placeholders' dates
    attachments: list[PushmailAttachmentCell] = Field(default_factory=list, max_length=PUSHMAIL_MAX_ATTACHMENTS)


class FlowGroupCell(BaseModel):
    # run a flow, refresh a database datasource, run a saved script, run an image,
    # send datasources by email
    kind: Literal["flow", "refresh", "script", "container", "pushmail"]
    id: int  # of the flow, datasource or script; a container or pushmail cell's own number in the group
    # a container cell's settings, as a Container node's data (registryId, repository,
    # tag, command, args, variables, cpu, memory, timeoutSeconds, retries…)
    container: Optional[dict[str, Any]] = None
    pushmail: Optional[PushmailCell] = None


class FlowGroupDefinition(BaseModel):
    """Rows are steps, run one after another; the cells of a row run in parallel.
    None keeps an empty cell in its column, so the grid comes back as drawn."""
    steps: list[Annotated[list[Optional[FlowGroupCell]], Field(max_length=MAX_GROUP_COLUMNS)]] = Field(
        default_factory=list, max_length=MAX_GROUP_STEPS,
    )


class FlowGroupCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=4000)
    definition: FlowGroupDefinition = Field(default_factory=FlowGroupDefinition)


class FlowGroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    definition: Optional[FlowGroupDefinition] = None
    project_id: Optional[int] = None  # set = moves the group to another folder


class FlowGroupCellLabel(BaseModel):
    """What the reader may know about a cell: its name and folder, or that the
    object is gone, or that it sits in a folder the reader cannot see."""
    name: Optional[str] = None
    project_id: Optional[int] = None
    missing: bool = False
    hidden: bool = False


class FlowGroupOut(BaseModel):
    id: int
    name: str
    description: str = ""
    project_id: int
    owner_id: Optional[int] = None
    definition: FlowGroupDefinition
    run_schedule: Optional[str] = None
    run_scheduled_by: Optional[int] = None
    next_run_at: Optional[UtcDateTime] = None
    created_at: Optional[UtcDateTime] = None
    updated_at: Optional[UtcDateTime] = None


class FlowGroupScheduleUpdate(BaseModel):
    cron: Optional[str] = None  # 5-field cron; empty = not scheduled


class FlowGroupRunOut(BaseModel):
    """A group run, with the state of every cell parallel to the group's grid:
    {"state": waiting|running|succeeded|failed|skipped, "run_id", "error", "waited"}."""
    id: int
    status: str
    trigger_type: str = "manual"
    launched_by: Optional[int] = None
    error: Optional[str] = None
    started_at: Optional[UtcDateTime] = None
    finished_at: Optional[UtcDateTime] = None
    cells: list[list[Optional[dict]]] = Field(default_factory=list)


class FlowGroupDetail(FlowGroupOut):
    # parallel to definition.steps: the label of every cell, resolved for the reader
    cells: list[list[Optional[FlowGroupCellLabel]]] = Field(default_factory=list)
    # what the reader may do with it, so the page offers only what will work
    can_edit: bool = False
    can_run: bool = False


# ── Unified search (by name, over every kind of resource) ─────────────────────
SearchKind = Literal["folder", "flow", "group", "datasource", "view", "script", "connection"]


class SearchHit(BaseModel):
    """A resource found. `project_*` says WHERE it is: for a folder it is the folder that
    contains it, for everything else the folder that hosts it — without it, a result
    with a common name cannot be told from another."""
    kind: SearchKind
    id: int
    name: str
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    detail: str = ""  # a context line, depends on the kind (e.g. db_type/host)
    rows: Optional[int] = None  # datasource: its row count (the interface formats it)
    # a flow the person asking cannot open (images or scripts the panel did not grant
    # them): shown greyed out with this reason; None = it opens
    locked_reason: Optional[str] = None


class SearchOut(BaseModel):
    """`counts` feeds the kind selector with the numbers: it is computed anyway to sort,
    so it does not cost one more query."""
    items: list[SearchHit]
    total: int
    counts: dict[str, int] = Field(default_factory=dict)


# ── Projects ──────────────────────────────────────────────────────────────────
class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    parent_id: Optional[int]
    owner_id: Optional[int]


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    parent_id: Optional[int] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None


# ── Flows ─────────────────────────────────────────────────────────────────────
class FlowOut(BaseModel):
    """A list item: without `definition` (it can be heavy, only the editor needs it)."""
    id: int
    name: str
    description: str
    project_id: int
    owner_id: Optional[int]
    owner_name: Optional[str] = None  # the name of whoever created the flow (resolved)
    engine: str = "polars"  # the DEVELOPMENT engine (editor, manual runs)
    production_engine: Optional[str] = None  # the engine of the SCHEDULED runs; null = as engine
    run_schedule: Optional[str] = None  # cron; null = not scheduled
    next_run_at: Optional[UtcDateTime] = None
    # a notice when a SCHEDULED run fails (see services/notifier.py)
    notify_emails: Optional[str] = None
    notify_connection_id: Optional[int] = None
    created_at: Optional[UtcDateTime] = None
    updated_at: Optional[UtcDateTime] = None
    # the state of the LAST run (for the indicator in the list, without opening the
    # expander): SUCCESS | FAILURE | STARTED | PENDING | null (never run)
    last_run_status: Optional[str] = None
    last_run_at: Optional[UtcDateTime] = None
    # why the person asking cannot open it (it runs images of a container registry
    # they have no access to): the lists show it, greyed out; None = it opens
    locked_reason: Optional[str] = None


class FlowDetail(FlowOut):
    definition: str


class FlowCreate(BaseModel):
    name: str
    description: str = ""
    definition: str = "{}"
    engine: str = "polars"  # chosen at creation
    production_engine: Optional[str] = None  # optional; null/"" = as engine


class FlowScheduleUpdate(BaseModel):
    """Sets/disables the flow's scheduled run. An empty `cron` = off.
    `production_engine`: omitted = unchanged; "" = back to the same as development."""
    cron: Optional[str] = None
    production_engine: Optional[str] = None
    # Failure notice: addresses separated by commas and the SMTP connection that sends
    # them. `""` turns the notice off; omitted = unchanged.
    notify_emails: Optional[str] = None
    notify_connection_id: Optional[int] = None



class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    project_id: Optional[int] = None  # set = move to another folder
    engine: Optional[str] = None  # set = change the development engine
    production_engine: Optional[str] = None  # set = change the production engine ("" = as development)


class FlowVersionOut(BaseModel):
    """A version of a flow's definition (history + promotion)."""
    version: int
    note: str
    created_at: Optional[UtcDateTime]
    created_by: Optional[int]
    created_by_name: Optional[str] = None  # name/email of whoever released the version
    is_current: bool


class FlowStatsOut(BaseModel):
    """A flow's run statistics (from the run history)."""
    run_count: int
    success_count: int
    failure_count: int
    last_run_at: Optional[UtcDateTime]
    avg_duration_seconds: Optional[float]


# ── Connections (connections to external databases) ───────────────────────────
class ConnectionOut(BaseModel):
    """The password NEVER leaves the API: only a flag saying whether it is set."""
    id: int
    name: str
    description: str
    project_id: int
    owner_id: Optional[int]
    db_type: str
    host: str
    port: Optional[int]
    username: str
    database: str
    db_schema: str
    # the kind's own options (JSON). Today only SMTP: sender, TLS mode and allowed
    # domains. It holds no secrets, so it may go back to the client — it serves to fill
    # the form again when the connection is edited.
    extra: str = "{}"
    has_password: bool = False
    # SFTP: whether a private key is stored (never the key), and the host key
    # accepted at the first test — empty until a test accepts one
    has_private_key: bool = False
    host_key_type: str = ""
    host_key_fingerprint: str = ""
    updated_at: Optional[UtcDateTime] = None


class ConnectionCreate(BaseModel):
    name: str
    description: str = ""
    db_type: str
    host: str
    port: Optional[int] = None
    username: str = ""
    password: str = ""  # in clear only in the request's body; encrypted at rest
    database: str = ""
    db_schema: str = ""
    extra: str = "{}"  # the kind's options (SMTP: from_address, tls, allowed_domains; SFTP: auth)
    # SFTP with the key method: the private key (PEM or OpenSSH) and its passphrase,
    # in clear only in the request's body; stored encrypted, together
    private_key: str = Field(default="", max_length=32_000)
    private_key_passphrase: str = Field(default="", max_length=1_000)


class ConnectionTest(ConnectionCreate):
    """A connection tested BEFORE it is saved (the form). It only uses the secret
    typed in the form, never a stored one. SFTP: without `expected_fingerprint` the
    test shows the server's host key and sends no credential; with it (the key the
    user accepts), the credentials go only if the server offers exactly that key."""
    expected_fingerprint: str = Field(default="", max_length=200)
    expected_type: str = Field(default="", max_length=100)


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None  # set = replaces the encrypted one
    database: Optional[str] = None
    db_schema: Optional[str] = None
    extra: Optional[str] = None
    project_id: Optional[int] = None  # set = move to another folder
    # SFTP: set = replaces the stored key ("" removes it); a passphrase alone
    # keeps the stored key and replaces its passphrase
    private_key: Optional[str] = Field(default=None, max_length=32_000)
    private_key_passphrase: Optional[str] = Field(default=None, max_length=1_000)


# ── Runs (the flows' runs) ────────────────────────────────────────────────────
class PublishSpec(BaseModel):
    """A request to publish the run's output as a named datasource."""
    name: str
    project_id: int
    description: str = ""
    # when a datasource with this name exists in the folder already: overwrite it (only
    # when it is kind="flow") instead of failing with 409
    overwrite: bool = False
    # columns for the ORDER BY: the result is sorted before being written (a sorted
    # parquet → pruning downstream) and the keys stay on the datasource
    sort_keys: list[str] = Field(default_factory=list)


class RunDestinationSpec(BaseModel):
    """The output's destination (the flow's Output node): a database table, an
    object/dataset on S3 or a file on an SFTP server. The connection is referenced by
    id, the credentials NEVER pass through the client."""
    type: Literal["database", "s3", "sftp"] = "database"
    connection_id: int
    # type="database"
    table: str = ""
    mode: Literal["append", "replace"] = "append"
    post_sql: str = ""
    # type="s3"
    bucket: str = ""  # empty = the connection's default bucket
    key: str = ""  # the file's key, or the prefix when partitioned
    # s3: parquet | csv; sftp: csv | parquet | xlsx
    format: Literal["parquet", "csv", "xlsx"] = "parquet"
    partition_by: list[str] = Field(default_factory=list)  # hive: column=value/…
    # type="sftp": the folder on the server (empty = the login directory) and the
    # file-name template ({date}, {datetime}, {run_id}, {flow})
    folder: str = Field(default="", max_length=1000)
    file_name: str = Field(default="", max_length=255)
    # the zone of the dates in the templates (file name, folder, key); empty = UTC
    timezone: str = Field(default="", max_length=64)


class RunMirrorSpec(BaseModel):
    """A copy of the output to an external S3 storage, AS WELL AS the publishing as a
    datasource (not instead of it: for that there is `RunDestinationSpec` with
    type="s3", where the writing IS the result).

    Written at the path of its key at every run: the same object overwritten, or —
    when the key holds placeholders (`copia/{date}.parquet`) — one per day or per
    run. No partitions. Best effort: a failed copy, or a key whose placeholders
    cannot be resolved (then the copy is skipped), never fails the run nor the
    publish; the outcome is recorded on the run.
    The connection is referenced by id: the credentials do not pass through the client.

    Parquet ONLY, and not by oversight: the format is not a choice because the file's
    name and the format would be independent, and a CSV called `.parquet` would be
    opened as parquet by whoever reads it downstream. Removing the choice makes the
    mistake impossible instead of warning that it is possible."""
    connection_id: int
    bucket: str = ""  # empty = the connection's default bucket
    key: str  # the object's whole path (subfolder + file name)
    timezone: str = Field(default="", max_length=64)  # of the placeholders in the key; empty = UTC


class RunEmailSpec(BaseModel):
    """Sending the output as an email attachment (an Output node with destType="email").

    The recipients are free text in the flow's definition, so this is the only kind of
    destination that can send data to an arbitrary address. Two consequences, both
    applied by the gateway and not by the client: the recipients are validated against
    the connection's allowed domains, and every send ends up in the audit.

    `stop_on_failure` (on by default) stops the flow's SEQUENCE, not only fails the run:
    the steps downstream of a notification often take it for done.
    """
    connection_id: int
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    body_is_html: bool = False
    attachment_name: str = ""
    attachment_format: Literal["csv", "xlsx"] = "xlsx"
    stop_on_failure: bool = True
    # the zone of the dates in the subject and the attachment name; empty = UTC
    timezone: str = Field(default="", max_length=64)


class RunCreate(BaseModel):
    bucket: str
    input_key: str
    operations: list[dict] = Field(default_factory=list)
    publish: Optional[PublishSpec] = None
    destination: Optional[RunDestinationSpec] = None
    # lives alongside `publish`: the datasource is the result, this is the copy
    mirror: Optional[RunMirrorSpec] = None
    email: Optional[RunEmailSpec] = None


class RunProgress(BaseModel):
    """Where a run not finished yet stands, for whoever looks at it."""
    # queued · running (the engine says no more) · waiting (the database has not sent
    # rows yet) · reading (rows arriving) · files (SharePoint, file `file` of `files`) ·
    # calls (API: call `call` of `calls`, `pages` and `rows` so far) · saving (the parquet
    # goes to the storage) · checking (the data contract)
    phase: str
    # seconds from the start of the run (queued: from the launch), counted by the gateway:
    # the browser's clock may be wrong
    seconds: int
    rows: Optional[int] = None
    file: Optional[int] = None
    files: Optional[int] = None
    call: Optional[int] = None
    calls: Optional[int] = None
    pages: Optional[int] = None


class RunOut(BaseModel):
    id: int
    kind: str = "flow"
    flow_id: Optional[int] = None
    flow_group_id: Optional[int] = None  # the run of a flow group (no flow, no datasource)
    saved_script_id: Optional[int] = None  # a saved script's run (docs/specs/saved-scripts.md)
    status: str
    launched_by: Optional[int]
    trigger_type: str = "manual"  # "manual" | "schedule"
    engine: Optional[str] = None  # the engine it ran with
    output_key: str
    rows_written: Optional[int]
    error: Optional[str]
    error_detail: Optional[str] = None  # the whole traceback (the failure's detail)
    publish_name: Optional[str]
    datasource_id: Optional[int]
    destination: Optional[str] = None  # JSON: {db_type, host, database, table, mode}
    # JSON: {bucket, key, format, ok, error?} — a best-effort copy to an external S3
    mirror: Optional[str] = None
    # JSON: {connection_id, host, to, subject, attachment, ok, error?} — invio email
    email: Optional[str] = None
    # JSON: what a successful refresh has to say — {"missing": [{params, pattern}],
    # "missing_count", "expansions"}: the values of a range whose file was not there
    notes: Optional[str] = None
    # kind="script" or "container": JSON — environment and version or registry and
    # image, files or command, attempts, exit code, the end of the log, the variables
    # it was given (secret ones by name only)
    script: Optional[str] = None
    parent_run_id: Optional[int] = None
    started_at: Optional[UtcDateTime]
    finished_at: Optional[UtcDateTime]
    progress: Optional[RunProgress] = None  # only for running runs (see runs.run_out)


class RunSearchOut(RunOut):
    """A run in the global run search: as RunOut + the names of the flow / the datasource
    / whoever started it, to show them without resolving them on the client."""
    flow_name: Optional[str] = None
    source_name: Optional[str] = None
    group_name: Optional[str] = None
    script_name: Optional[str] = None
    launched_by_name: Optional[str] = None  # None when scheduled or the user was removed


class ActivityBucket(BaseModel):
    """The counts of a bucket of the calendar plot: a day (key=YYYY-MM-DD) or an hour
    (key='00'..'23'). Each run is an event; the breakdown tells the outcome
    (successes/failures) and the origin (manual/scheduled) apart."""
    key: str
    total: int
    success: int
    failure: int
    scheduled: int
    manual: int


class RunActivityOut(BaseModel):
    """The runs' activity for the calendar plot of the Flows page: buckets by DAY (heatmap)
    or by HOUR (drill-down). The buckets are in the client's LOCAL time (passed via
    tz_offset); from_key/to_key bound the window."""
    granularity: Literal["day", "hour"]
    from_key: str
    to_key: str
    buckets: list[ActivityBucket]


# ── Datasources (named datasets in the catalog) ───────────────────────────────
class ContractSummary(BaseModel):
    """A datasource's data contract state, for the listings."""
    status: str  # pending | passed | warning | failed
    checked_at: Optional[UtcDateTime] = None
    errors: int = 0
    warnings: int = 0
    blocked: bool = False  # the last update was refused
    version: int = 1


class DatasourceOut(BaseModel):
    id: int
    name: str
    description: str
    project_id: int
    owner_id: Optional[int]
    bucket: str
    key: str
    rows: Optional[int]
    # [{name, dtype, description?}] — `description` there only when curated
    columns: list[dict] = Field(default_factory=list)
    # {column name: description} (also for columns no longer in the schema)
    column_descriptions: dict[str, str] = Field(default_factory=dict)
    # How documented it is: the count serves to show progress, the flag to say it is done.
    # Computed by the server (services/documented.py) because the rule must hold the same
    # in the catalog, the assistant and the search.
    described_columns: int = 0
    total_columns: int = 0
    ai_ready: bool = False
    # the ORDER BY columns the parquet is sorted by (empty = no order)
    sort_keys: list[str] = Field(default_factory=list)
    kind: str
    flow_id: Optional[int]
    # per kind="database"
    connection_id: Optional[int] = None
    source_type: Optional[str] = None
    source_ref: Optional[str] = None
    refreshed_at: Optional[UtcDateTime] = None
    # an ingest is running NOW (the server says it: it holds for scheduled refreshes too
    # and after a page reload). Only in the listings.
    refreshing: bool = False
    # scheduled refresh (cron); next_refresh_at = the next run expected
    refresh_schedule: Optional[str] = None
    next_refresh_at: Optional[UtcDateTime] = None
    updated_at: Optional[UtcDateTime] = None
    # The data contract, when one is turned on: what the state icon needs (see
    # services/contracts.summary). None = no contract.
    contract: Optional[ContractSummary] = None


class DatasourceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    project_id: Optional[int] = None  # set = move to another folder
    # set = REPLACES the {column: description} map; empty items dropped
    column_descriptions: Optional[dict[str, str]] = None
    # set = replaces the ORDER BY of the next imports ([] removes it). Database
    # datasources only: a flow sets them on its Output node, at every publish.
    sort_keys: Optional[list[str]] = Field(default=None, max_length=32)


class ScheduleUpdate(BaseModel):
    """Sets/updates the refresh's schedule. An empty/None `cron` = disable."""
    cron: Optional[str] = None


class DbDatasourceCreate(BaseModel):
    """A datasource from a database: the source's definition + the first ingest."""
    name: str
    description: str = ""
    connection_id: int
    source_type: str  # table | sql
    source_ref: str  # table name or SQL text
    # columns for the ingest's ORDER BY (empty = no order, as before)
    sort_keys: list[str] = Field(default_factory=list)


class ApiVariable(BaseModel):
    """A variable of a datasource (API call, SFTP, SharePoint, S3): a LIST —
    `symbol = AAPL, MSFT`, one call or one expansion per value (every combination
    of several) — or a RANGE — `day = today-7 .. today step 1d`, the values from
    the start (included) to the end (excluded), with {day_end} the end of each
    window. Checked by the engine (app.core.placeholders) before saving."""
    name: str = Field(min_length=1, max_length=64)
    type: Literal["list", "range"] = "list"
    values: list[str] = Field(default_factory=list, max_length=1000)
    start: str = Field(default="", max_length=100)
    end: str = Field(default="", max_length=100)
    step: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def _complete(self) -> "ApiVariable":
        if self.type == "list" and not self.values:
            raise ValueError(f"the variable '{self.name}' has no values")
        if self.type == "range" and not (self.start.strip() and self.end.strip() and self.step.strip()):
            raise ValueError(f"the range '{self.name}' needs a start, an end and a step")
        return self


class FileTemplatesSpec(BaseModel):
    """The moving part of a file datasource (SFTP, SharePoint, S3): the zone of the
    placeholders in its folder and glob, its variables, the first value of
    {last_refresh}, and the refresh mode (snapshot, or append by key columns)."""
    timezone: str = Field(default="UTC", max_length=64)
    variables: list[ApiVariable] = Field(default_factory=list, max_length=10)
    last_refresh_initial: str = Field(default="", max_length=100)
    mode: Literal["snapshot", "append"] = "snapshot"
    keys: list[str] = Field(default_factory=list, max_length=20)

    def template_fields(self) -> dict:
        """The fields that differ from a plain datasource: only those are stored, so a
        datasource without placeholders keeps exactly the shape it always had."""
        out: dict = {}
        if (self.timezone or "UTC").strip() not in ("", "UTC"):
            out["timezone"] = self.timezone.strip()
        if self.variables:
            out["variables"] = [v.model_dump() for v in self.variables]
        if self.last_refresh_initial.strip():
            out["last_refresh_initial"] = self.last_refresh_initial.strip()
        if self.mode != "snapshot":
            out["mode"] = self.mode
        keys = [k.strip() for k in self.keys if k.strip()]
        if keys:
            out["keys"] = keys
        return out


class SftpCsvOptions(BaseModel):
    """How the CSV files of an SFTP folder are read (the options of the file sources)."""
    separator: str = Field(default=",", min_length=1, max_length=1)
    has_header: bool = True
    # utf8-lossy replaces invalid bytes; windows-1252 is the usual Excel export
    encoding: Literal["utf8", "utf8-lossy", "windows-1252"] = "utf8"
    null_values: list[str] = Field(default_factory=list, max_length=20)


class SharePointDatasourceCreate(FileTemplatesSpec):
    """A datasource from Excel and CSV files on SharePoint: a path (globs too), the format,
    the sheet of the workbooks and the reading options of the CSV. Several matching
    files are stacked into one table. The path may hold placeholders and variables
    (FileTemplatesSpec)."""
    name: str
    description: str = ""
    connection_id: int
    path: str = Field(min_length=1, max_length=1000)
    # auto = Excel and CSV by their extension; without it (callers of before) Excel only
    format: Literal["auto", "excel", "csv"] = "excel"
    sheet: str = Field(default="", max_length=255)  # Excel: empty = the first sheet
    csv: SftpCsvOptions = Field(default_factory=SftpCsvOptions)


def no_dot_dot(path: str) -> str:
    """SFTP folders and patterns say where they are: going up with '..' is refused."""
    if ".." in (path or "").replace("\\", "/").split("/"):
        raise ValueError("'..' is not allowed: write the folder you mean")
    return path


class SftpDatasourceCreate(FileTemplatesSpec):
    """Datasource from the files of a folder on an SFTP server: a folder and a glob.
    All matching files are read at each refresh and stacked into one table. Folder
    and glob may hold placeholders and variables (FileTemplatesSpec)."""
    name: str
    description: str = ""
    connection_id: int
    folder: str = Field(default="", max_length=1000)  # empty = the login directory
    pattern: str = Field(min_length=1, max_length=500)
    # auto = from each file's extension
    format: Literal["auto", "csv", "parquet", "excel", "ndjson"] = "auto"
    sheet: str = Field(default="", max_length=255)  # Excel: empty = the first sheet
    csv: SftpCsvOptions = Field(default_factory=SftpCsvOptions)

    @field_validator("folder", "pattern")
    @classmethod
    def _no_dot_dot(cls, v: str) -> str:
        return no_dot_dot(v)


class SftpFilesQuery(FileTemplatesSpec):
    folder: str = Field(default="", max_length=1000)
    pattern: str = Field(min_length=1, max_length=500)

    @field_validator("folder", "pattern")
    @classmethod
    def _no_dot_dot(cls, v: str) -> str:
        return no_dot_dot(v)


class S3DatasourceCreate(FileTemplatesSpec):
    """Datasource from the files of a folder of a bucket («S3 folder»), through an
    S3 connection: the bucket (empty = the connection's default), a folder (key
    prefix) and a glob; the reading options of the SFTP datasource. All matching
    objects are read at each refresh and stacked into one table."""
    name: str
    description: str = ""
    connection_id: int
    bucket: str = Field(default="", max_length=255)
    folder: str = Field(default="", max_length=1000)
    pattern: str = Field(min_length=1, max_length=500)
    format: Literal["auto", "csv", "parquet", "excel", "ndjson"] = "auto"
    sheet: str = Field(default="", max_length=255)
    csv: SftpCsvOptions = Field(default_factory=SftpCsvOptions)

    @field_validator("folder", "pattern")
    @classmethod
    def _no_dot_dot(cls, v: str) -> str:
        return no_dot_dot(v)

    def source_ref(self) -> dict:
        return {"bucket": self.bucket.strip(), "folder": self.folder.strip().strip("/"), "pattern": self.pattern.strip(),
                "format": self.format, "sheet": self.sheet.strip(), "csv": self.csv.model_dump(),
                **self.template_fields()}


class S3FilesQuery(FileTemplatesSpec):
    bucket: str = Field(default="", max_length=255)
    folder: str = Field(default="", max_length=1000)
    pattern: str = Field(min_length=1, max_length=500)

    @field_validator("folder", "pattern")
    @classmethod
    def _no_dot_dot(cls, v: str) -> str:
        return no_dot_dot(v)


class SharePointFilesQuery(FileTemplatesSpec):
    path: str = Field(min_length=1, max_length=1000)
    format: Literal["auto", "excel", "csv"] = "excel"


class PlaceholderPreviewQuery(BaseModel):
    """The live preview of templates in a form: each one's value now («today:
    export_20261018.csv»), or what is wrong with it. `kind` input (a datasource's
    folder/glob, with its variables) or output (an Output's file name, folder,
    key or subject: `targets` says which)."""
    templates: dict[str, str] = Field(max_length=20)
    kind: Literal["input", "output"] = "input"
    timezone: str = Field(default="UTC", max_length=64)
    variables: list[ApiVariable] = Field(default_factory=list, max_length=10)
    last_refresh_initial: str = Field(default="", max_length=100)
    targets: dict[str, Literal["file", "path", "text"]] = Field(default_factory=dict)
    flow: str = Field(default="", max_length=500)  # an Output: the flow's name, for {flow}

    @field_validator("templates")
    @classmethod
    def _short(cls, v: dict[str, str]) -> dict[str, str]:
        if any(len(t) > 4000 or len(k) > 64 for k, t in v.items()):
            raise ValueError("template too long")
        return v


class ApiPair(BaseModel):
    name: str = Field(max_length=200)
    value: str = Field(default="", max_length=4000)


class ApiResponseSpec(BaseModel):
    format: Literal["json", "ndjson", "csv"] = "json"
    records_path: str = Field(default="", max_length=500)  # dot path to the records; empty = the root
    jmespath: str = Field(default="", max_length=2000)  # applied first, for odd shapes
    keyed: bool = False  # an object whose keys are rows: the key in `_key`
    explode: str = Field(default="", max_length=500)  # a list field exploded into rows
    csv_separator: str = Field(default=",", min_length=1, max_length=1)


class ApiPagination(BaseModel):
    type: Literal["none", "page", "offset", "cursor", "next_link"] = "none"
    param: str = Field(default="", max_length=200)
    start: Optional[int] = Field(default=None, ge=0)
    limit_param: str = Field(default="", max_length=200)
    limit: int = Field(default=100, ge=1, le=100_000)
    cursor_path: str = Field(default="", max_length=500)
    next_path: str = Field(default="", max_length=500)


class ApiLimits(BaseModel):
    """0 = the deployment's limit (API__… on the engine); a value can only lower it."""
    max_pages: int = Field(default=0, ge=0, le=1_000_000)
    max_calls: int = Field(default=0, ge=0, le=1_000_000)
    max_rows: int = Field(default=0, ge=0, le=1_000_000_000)
    min_interval_seconds: float = Field(default=0, ge=0, le=3600)


class ApiCallSpec(BaseModel):
    """The call of an «API call» datasource: what it asks, how the answer becomes
    rows, how it pages, and how a refresh lands (snapshot or append with keys).
    The shape is checked here; the placeholders, the variables, the JMESPath and
    the rest by the engine (`validate`) before anything is saved."""
    method: Literal["GET", "POST"] = "GET"
    path: str = Field(default="", max_length=2000)  # relative to the connection's base URL
    query: list[ApiPair] = Field(default_factory=list, max_length=100)
    headers: list[ApiPair] = Field(default_factory=list, max_length=50)
    body: str = Field(default="", max_length=100_000)  # JSON (POST)
    variables: list[ApiVariable] = Field(default_factory=list, max_length=10)
    timezone: str = Field(default="UTC", max_length=64)  # defines «today»
    last_refresh_initial: str = Field(default="", max_length=100)  # {last_refresh} on the first refresh
    response: ApiResponseSpec = Field(default_factory=ApiResponseSpec)
    pagination: ApiPagination = Field(default_factory=ApiPagination)
    limits: ApiLimits = Field(default_factory=ApiLimits)
    mode: Literal["snapshot", "append"] = "snapshot"
    keys: list[str] = Field(default_factory=list, max_length=20)  # append: the columns identifying a row


class ApiDatasourceCreate(ApiCallSpec):
    """Datasource from an HTTP API: the call is made at each refresh."""
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    connection_id: int


# ── Permissions ───────────────────────────────────────────────────────────────
class PermissionOut(BaseModel):
    id: int
    project_id: int
    user_id: Optional[int]
    group_id: Optional[int]
    capability: str


class PermissionCreate(BaseModel):
    capability: Capability
    user_id: Optional[int] = None
    group_id: Optional[int] = None
