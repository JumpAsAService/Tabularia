"""runs: indexes for a datasource's refreshes, the history by date and live refreshes

With a year of history (about half a million runs) three questions read the whole
table on every call: a datasource's refreshes, polled every 2.5 s while it imports
(36 ms each); the "importing…" flag of every datasource list (53 ms); the run
history, the performance window and the activity calendar, which sort or scan by
start time (72 ms per page). Each now reads its index (0.1-0.3 ms measured).

Only additions: the code of the previous version does not need them and is not
slowed by them.

Revision 0008, after 0007 — 2026-10-14.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
import sqlalchemy as sa
from alembic import op


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

# the same text as app.models.run.LIVE_INGEST: a query matches a partial index only
# when it writes the condition the same way (a migration does not import the models)
LIVE_INGEST = "kind = 'ingest' AND status NOT IN ('SUCCESS', 'FAILURE')"


def upgrade() -> None:
    op.create_index('ix_runs_datasource_id_started_at', 'runs', ['datasource_id', 'started_at'], unique=False)
    op.create_index('ix_runs_started_at', 'runs', ['started_at'], unique=False)
    op.create_index('ix_runs_live_ingest', 'runs', ['datasource_id'], unique=False,
                    postgresql_where=sa.text(LIVE_INGEST), sqlite_where=sa.text(LIVE_INGEST))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
