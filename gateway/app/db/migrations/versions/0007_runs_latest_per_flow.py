"""runs: index (flow_id, started_at) for the latest run of each flow

The lineage page read every run of every readable flow to find each flow's latest
run and last success: seconds and hundreds of megabytes once a year of history had
piled up. It now asks for the maximum per flow, which this index answers.

Only an addition: the code of the previous version does not need it and is not
slowed by it.

Revision 0007, after 0006 — 2026-10-14.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_runs_flow_id_started_at', 'runs', ['flow_id', 'started_at'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
