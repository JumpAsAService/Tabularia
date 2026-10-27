from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Index, Text, text


def _now() -> datetime:
    return datetime.now(timezone.utc)


# the partial unique index's condition (in db/session.py's migrations too)
LIVE_ORCHESTRATION = "kind = 'orchestration' AND status = 'STARTED'"
# a refresh still running: the condition of a partial index, so a query that writes it
# the same way reads only the live rows (the "importing…" flag of every datasource list)
LIVE_INGEST = "kind = 'ingest' AND status NOT IN ('SUCCESS', 'FAILURE')"

# what is written on an orchestration nobody will finish (no quotes: it goes into an
# SQL statement of the migrations too)
ORCHESTRATION_INTERRUPTED = (
    "Orchestration interrupted: the process running it stopped and the remaining steps "
    "were not run. Check what has already been produced and run the flow again."
)
# a step of an interrupted orchestration that was still running: stopped with it
STEP_INTERRUPTED = (
    "Stopped: the orchestration this step belonged to was interrupted (the process running "
    "it stopped). Check what has already been produced and run the flow again."
)


class Run(SQLModel, table=True):
    """
    A flow's run: the row is born at launch (PENDING) and is reconciled lazily with the
    Celery task's state each time someone reads it (no background poller: whoever looks,
    updates).

    When publishing was asked at launch (`publish_name`), on the move to SUCCESS the
    output becomes a Datasource reusable as a source.
    """
    __tablename__ = "runs"
    __table_args__ = (
        # ONE orchestration of a flow runs at a time, and the database guarantees it: with
        # several processes running, a list in memory is seen by nobody else. Whoever tries to
        # start the second gets the index's error (see `orchestrator.claim`).
        Index(
            "uq_runs_one_live_orchestration",
            "flow_id",
            unique=True,
            postgresql_where=text(LIVE_ORCHESTRATION),
            sqlite_where=text(LIVE_ORCHESTRATION),
        ),
        # one live run per flow group, for the same reason
        Index(
            "uq_runs_one_live_group",
            "flow_group_id",
            unique=True,
            postgresql_where=text(LIVE_ORCHESTRATION),
            sqlite_where=text(LIVE_ORCHESTRATION),
        ),
        # one live run per saved script, for the same reason
        Index(
            "uq_runs_one_live_script",
            "saved_script_id",
            unique=True,
            postgresql_where=text(LIVE_ORCHESTRATION),
            sqlite_where=text(LIVE_ORCHESTRATION),
        ),
        # the queue of the orchestrations to claim: only the waiting rows
        Index(
            "ix_runs_orchestration_queue",
            "id",
            postgresql_where=text("kind = 'orchestration' AND status = 'PENDING'"),
            sqlite_where=text("kind = 'orchestration' AND status = 'PENDING'"),
        ),
        # the latest run of each flow (lineage health, flow lists): read from the
        # index instead of scanning the whole history
        Index("ix_runs_flow_id_started_at", "flow_id", "started_at"),
        # a datasource's refreshes, newest first (polled while it imports)
        Index("ix_runs_datasource_id_started_at", "datasource_id", "started_at"),
        # the run history, the performance window and the activity calendar
        Index("ix_runs_started_at", "started_at"),
        Index(
            "ix_runs_live_ingest",
            "datasource_id",
            postgresql_where=text(LIVE_INGEST),
            sqlite_where=text(LIVE_INGEST),
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    # kind="flow": a flow's run (flow_id set).
    # kind="ingest": refresh of a database datasource (datasource_id set).
    # kind="sql": a SQL script writing its datasource (sql_script_id set).
    kind: str = Field(default="flow", index=True)
    flow_id: Optional[int] = Field(default=None, foreign_key="flows.id", index=True)
    # set on a flow group's own run (kind="orchestration", flow_id None); the runs
    # of its cells point at that run through parent_run_id
    flow_group_id: Optional[int] = Field(default=None, foreign_key="flow_groups.id", index=True)
    # a saved script's runs (docs/specs/saved-scripts.md): its own orchestration row
    # (kind="orchestration", queued and claimed as a flow's) and the script run under
    # it (kind="script"), which holds the attempts, the log and the output
    saved_script_id: Optional[int] = Field(default=None, foreign_key="saved_scripts.id", index=True)
    # a SQL script writing its result into its datasource (kind="sql"): an engine task
    # whose output is published as a flow's Output is (docs/specs/sql-scripts.md)
    sql_script_id: Optional[int] = Field(default=None, foreign_key="sql_scripts.id", index=True)
    # a group run only: the state of every cell, parallel to the group's grid
    # ([[{"state", "run_id", "error"}, ...], ...]); written by the process running
    # the group, read by the editor
    group_cells: Optional[str] = Field(default=None, sa_column=Column("group_cells", Text))
    task_id: str = Field(index=True)  # id of the Celery task on the engine
    status: str = "PENDING"  # PENDING | STARTED | SUCCESS | FAILURE
    launched_by: Optional[int] = Field(default=None, foreign_key="users.id")
    # how it was started: "manual" (a user) or "schedule" (the scheduler); for the
    # scheduled ones `launched_by` is the schedule's author, but in the history we simply
    # show "schedule". Column `trigger_type`: "trigger" is an SQL reserved word.
    trigger_type: str = Field(default="manual")
    # the engine the run RAN with (the flow's dev or production one at launch time): the
    # history shows what it really ran on
    engine: Optional[str] = None
    # the orchestration run that generated this child run (output/refresh launched INSIDE
    # an orchestration). None = a TOP-LEVEL run (orchestration, the editor's direct run,
    # standalone refresh): so the calendar plot counts the runs only once, without the
    # child duplicates.
    parent_run_id: Optional[int] = Field(default=None, foreign_key="runs.id", index=True)

    input_key: str
    output_bucket: str
    output_key: str
    rows_written: Optional[int] = None
    error: Optional[str] = Field(default=None, sa_column=Column(Text))  # summary (for the list)
    # the engine's whole traceback (to understand the cause): shown in the detail
    error_detail: Optional[str] = Field(default=None, sa_column=Column("error_detail", Text))

    # the publishing request (optional, decided at launch); for kind="ingest" runs
    # `datasource_id` is the datasource the refresh updates
    publish_name: Optional[str] = None
    publish_project_id: Optional[int] = Field(default=None, foreign_key="projects.id")
    publish_description: str = ""
    # overwrite the datasource with the same name (kind="flow") instead of creating a new one
    publish_overwrite: bool = False
    # the ORDER BY columns promised to the published datasource (JSON): the result comes
    # out sorted and the datasource inherits them (see _publish_datasource)
    publish_sort_keys: str = "[]"
    datasource_id: Optional[int] = Field(default=None, foreign_key="datasources.id")

    # the output's database destination (Output node): a JSON summary for the history —
    # {connection_id, db_type, host, database, table, mode}
    destination: Optional[str] = Field(default=None, sa_column=Column("destination", Text))

    # a copy of the output to an external S3 storage, AS WELL AS the published datasource:
    # a JSON summary of the outcome — {bucket, key, format, ok, error?}. It is BEST EFFORT
    # by choice: the datasource is the primary result, this is a delivery downstream, and
    # its failure must not undo the publishing. The error ends up here because otherwise it
    # would stay only in the worker's logs, invisible to whoever looks at the history.
    mirror: Optional[str] = Field(default=None, sa_column=Column("mirror", Text))

    # sending the output as an email attachment (Output node destType="email"): a JSON
    # summary — {connection_id, host, to, subject, attachment, ok, error?}. Unlike the copy
    # it is NOT best effort: a report that does not go out is the result that is not there.
    # The detail is here because the audit records WHAT went out and to whom, while this
    # column says whether it arrived.
    email: Optional[str] = Field(default=None, sa_column=Column("email", Text))
    # what a successful refresh has to say, as JSON: the values of a range whose
    # file was not there ({"missing": [{params, pattern}], "missing_count",
    # "expansions"}). Skipped and listed, not failed: the run page shows them.
    notes: Optional[str] = Field(default=None, sa_column=Column("notes", Text))
    # kind="script" or "container" only: what the node ran and how, as JSON — the
    # environment and version or the registry and image (by digest), files or command,
    # attempts, exit code, the end of the log, the variables it was given (secret ones
    # by name only). See services/script_runs.py and services/container_runs.py.
    script: Optional[str] = Field(default=None, sa_column=Column("script", Text))

    # ── only for kind="orchestration" ───────────────────────────────────────
    # Born PENDING = queued; whoever runs it brings it to STARTED signing it.
    # "development" | "production": the engine to launch the outputs with
    engine_mode: Optional[str] = None
    # the process running it, and the last time it said «here». A STARTED orchestration
    # whose heartbeat has been still for too long belongs to a dead process: nobody will
    # finish it, and it must be closed saying so.
    claimed_by: Optional[str] = None
    heartbeat_at: Optional[datetime] = None

    started_at: datetime = Field(default_factory=_now)
    # the instant the task REALLY started running on the worker (Celery reports STARTED,
    # `task_track_started=True`). `started_at` is born with the launch and includes the
    # wait in the queue, so it does not measure the run: with busy workers a run just
    # started would show as expired already. The staleness timeout starts from here. None
    # = not seen running yet.
    engine_started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


TERMINAL_STATES = {"SUCCESS", "FAILURE"}
