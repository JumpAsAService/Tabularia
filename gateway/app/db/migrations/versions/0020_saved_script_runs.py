"""saved scripts' runs: a run names the saved script it ran

Only additions: one nullable column on runs and two indexes on it, the second of which
lets a saved script have one live run at a time, as a flow and a flow group. The code
of the previous version neither writes nor reads the column. docs/specs/saved-scripts.md.

Revision 0020, after 0019 — 2026-10-22.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa


revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('saved_script_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_runs_saved_script_id'), 'runs', ['saved_script_id'], unique=False)
    op.create_index('uq_runs_one_live_script', 'runs', ['saved_script_id'], unique=True, postgresql_where=sa.text("kind = 'orchestration' AND status = 'STARTED'"), sqlite_where=sa.text("kind = 'orchestration' AND status = 'STARTED'"))
    op.create_foreign_key(None, 'runs', 'saved_scripts', ['saved_script_id'], ['id'])


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
