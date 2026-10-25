"""The schema from BEFORE Alembic: it serves only to ADOPT a database created when the
gateway migrated by itself at start (`create_all` + this list of idempotent ALTERs).

Such a database has the tables but not `alembic_version`. To bring it into Alembic:
1. the list below, FROZEN as it was at the last version without Alembic, brings it
   to that version's state whatever point it started from;
2. the REALIGNMENT removes the drift between a database created from scratch and one
   that got here by way of ALTERs (measured on 2026-10-10): a foreign key the ALTER
   did not add, a different type, server-side defaults the models do not declare and
   that were there or not depending on when the column was born;
3. it is stamped at the base revision (0001) and compared with the models.
It must never be changed: the schema changes from here on are Alembic revisions
(gateway/migrations/versions)."""
import re
from pathlib import Path

from sqlalchemy import text

from app.models.run import LIVE_ORCHESTRATION, ORCHESTRATION_INTERRUPTED

ALTERS_BEFORE_ALEMBIC = [
    # Phase C: ingest runs (database datasource refresh) next to the flow runs
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'flow'",
    "ALTER TABLE runs ALTER COLUMN flow_id DROP NOT NULL",
    # Phase C: datasources kind="database" (connection + source definition)
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS connection_id INTEGER REFERENCES connections(id)",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS source_type VARCHAR",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS source_ref TEXT",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refreshed_at TIMESTAMP",
    # Output node: the run output's database destination (a JSON summary)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS destination TEXT",
    # Scheduled refresh of the database datasources
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refresh_schedule VARCHAR",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refresh_scheduled_by INTEGER REFERENCES users(id)",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS next_refresh_at TIMESTAMP",
    # Scheduled run of the flows
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS run_schedule VARCHAR",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS run_scheduled_by INTEGER REFERENCES users(id)",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMP",
    # Publishing a datasource in overwrite (republishes over the kind=flow one with the same name)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS publish_overwrite BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS publish_sort_keys VARCHAR NOT NULL DEFAULT '[]'",
    # Error detail (the engine's traceback) to debug failed runs
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS error_detail TEXT",
    # Where the start came from: 'manual' (user) | 'schedule' (scheduler)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS trigger_type VARCHAR NOT NULL DEFAULT 'manual'",
    # A run that is a child of an orchestration (so the calendar does not count duplicates)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS parent_run_id INTEGER REFERENCES runs(id)",
    # The run engine chosen for the flow (polars | duckdb)
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS engine VARCHAR NOT NULL DEFAULT 'polars'",
    # Descriptions of the datasources' fields (JSON name→text)
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS column_descriptions TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS sort_keys TEXT NOT NULL DEFAULT '[]'",
    # The flow's production engine (scheduled runs); NULL = as development
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS production_engine VARCHAR",
    # The engine each run ran with
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine VARCHAR",
    # Audit: the last authenticated activity (for the "active sessions")
    # SSO-only users: no local password (see services/sso.py)
    "ALTER TABLE users ALTER COLUMN hashed_password DROP NOT NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_ip VARCHAR",
    # The REAL start of the run on the worker: the staleness timeout must not count the
    # wait in the queue (see models/run.py)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine_started_at TIMESTAMP",
    # best-effort copy of the output to an external S3: a JSON outcome, NULL = not asked
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS mirror TEXT",
    # Options specific to the connection's kind (JSON). SMTP needs it, which has three
    # settings without a natural column to sit in — sender, TLS mode and allowed domains —
    # and squeezing `database`/`db_schema` as is done for S3 would have left one out
    # anyway. The other kinds do not use it.
    "ALTER TABLE connections ADD COLUMN IF NOT EXISTS extra TEXT NOT NULL DEFAULT '{}'",
    # Sending the output by email (Output node destType=email): a JSON outcome, NULL = not asked
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS email TEXT",
    # The run that produced the current snapshot: refuses swaps from older runs (see
    # models/datasource.py). A plain integer, no FK: it would make a runs ↔ datasources
    # cycle
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS snapshot_run_id INTEGER",
    # Stable SSO identity (the token's issuer + subject). SSO recognizes the user from
    # here, not from the email — see models/user.py and services/sso.py.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_issuer VARCHAR",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_subject VARCHAR",
    # The same IdP identity cannot belong to two accounts. A PARTIAL index: accounts
    # without SSO have both columns null and must not collide with each other.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_oidc_identity ON users (oidc_issuer, oidc_subject)"
    " WHERE oidc_subject IS NOT NULL",
    # Admin group: the members are admins (services/permissions.is_admin)
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE",
    # Failure notice of a scheduled run (see models/flow.py)
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS notify_emails TEXT",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS notify_connection_id INTEGER",
    # Observer role: reads the admin panels, does not write (see models/user.py)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_observer BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_observer BOOLEAN NOT NULL DEFAULT FALSE",
    # The orchestrations' queue (gateway in several replicas): the engine mode, who runs
    # it and its heartbeat
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine_mode VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS claimed_by VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP",
    # The orchestrations left STARTED without a heartbeat come from a version that did not
    # write it: that process is gone (there was only one, and it was stopped to get here).
    # They are closed, as the check at start did — and before the unique index, which
    # could not be created with two live rows of the same flow. From here on every STARTED
    # row has its heartbeat, and this statement finds nothing any more.
    "UPDATE runs SET status = 'FAILURE', finished_at = now() AT TIME ZONE 'utc', "
    f"error = '{ORCHESTRATION_INTERRUPTED}' "
    f"WHERE {LIVE_ORCHESTRATION} AND heartbeat_at IS NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_one_live_orchestration ON runs (flow_id) "
    f"WHERE {LIVE_ORCHESTRATION}",
    "CREATE INDEX IF NOT EXISTS ix_runs_orchestration_queue ON runs (id) "
    "WHERE kind = 'orchestration' AND status = 'PENDING'",
    # data contracts: whom to notify, and the queue of the notices to send
    "ALTER TABLE data_contracts ADD COLUMN IF NOT EXISTS notify_emails TEXT",
    "ALTER TABLE data_contracts ADD COLUMN IF NOT EXISTS notify_connection_id INTEGER",
    "ALTER TABLE data_contract_results ADD COLUMN IF NOT EXISTS notify VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_contract_results_to_notify ON data_contract_results (id) WHERE notify = 'pending'",
]


# the drift measured between a database created from scratch and one updated with the
# ALTERs: everything is brought to what the models say
_DEFAULTS_TO_DROP = [
    ("runs", "kind"), ("runs", "publish_overwrite"), ("runs", "publish_sort_keys"), ("runs", "trigger_type"),
    ("flows", "engine"), ("datasources", "column_descriptions"), ("datasources", "sort_keys"),
    ("connections", "extra"), ("groups", "is_admin"), ("groups", "is_observer"), ("users", "is_observer"),
]
REALIGNMENT = [
    # the server-side defaults: the models do not declare them (the ORM sets the value)
    *[f"ALTER TABLE {t} ALTER COLUMN {c} DROP DEFAULT" for t, c in _DEFAULTS_TO_DROP],
    # the ALTER added the column without the foreign key create_all puts in
    """DO $$ BEGIN
         IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'flows_notify_connection_id_fkey') THEN
           ALTER TABLE flows ADD CONSTRAINT flows_notify_connection_id_fkey
             FOREIGN KEY (notify_connection_id) REFERENCES connections(id);
         END IF;
       END $$""",
    # TEXT from the ALTER, VARCHAR from the model
    "ALTER TABLE flows ALTER COLUMN notify_emails TYPE VARCHAR",
    "ALTER TABLE data_contracts ALTER COLUMN notify_emails TYPE VARCHAR",
    # the fingerprint the gateway decided whether to migrate with: no longer needed
    "DROP TABLE IF EXISTS schema_state",
]


def _base_tables() -> set[str]:
    """The tables the base revision creates, read from its file (which no longer changes)."""
    base = Path(__file__).parent / "migrations" / "versions" / "0001_baseline.py"
    return set(re.findall(r"op\.create_table\('(\w+)'", base.read_text()))


def adopt(conn) -> None:
    """Brings a pre-Alembic database to the state of the base revision. `conn` is a
    connection in a transaction: all or nothing."""
    from sqlmodel import SQLModel

    import app.models  # noqa: F401 — registers the models

    conn.execute(text("SET LOCAL lock_timeout = '10s'"))
    # the tables born after the last start, when missing: ONLY those of the base. The
    # tables of later revisions are created by their revision, right after; create_all on
    # every model would have created them earlier, and the revision would have stopped
    # finding them there already (it happened with 0002, the sessions' table).
    # A limit that remains: a base table that is MISSING is created from today's models,
    # with the columns later revisions added. Today the only base table a revision changes
    # is `users` (0003), which a database to adopt always has; a future revision changing
    # another base table must be tried with the adoption of a database that lacks it
    base = _base_tables()
    SQLModel.metadata.create_all(conn, tables=[t for t in SQLModel.metadata.sorted_tables if t.name in base])
    for statement in ALTERS_BEFORE_ALEMBIC + REALIGNMENT:
        conn.execute(text(statement))
