"""sql scripts' datasource and schedule: a run names the SQL script it ran, and a
script has a schedule of its own (docs/specs/sql-scripts.md, «Saving as a datasource»)

Only additions: one nullable column on runs with its index, three nullable columns on
sql_scripts. The code of the previous version neither writes nor reads them.

Revision 0025, after 0024 — 2026-10-27.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('sql_script_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_runs_sql_script_id'), 'runs', ['sql_script_id'], unique=False)
    op.create_foreign_key(None, 'runs', 'sql_scripts', ['sql_script_id'], ['id'])
    op.add_column('sql_scripts', sa.Column('run_schedule', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('sql_scripts', sa.Column('run_scheduled_by', sa.Integer(), nullable=True))
    op.add_column('sql_scripts', sa.Column('next_run_at', sa.DateTime(), nullable=True))
    op.create_index(op.f('ix_sql_scripts_next_run_at'), 'sql_scripts', ['next_run_at'], unique=False)
    op.create_foreign_key(None, 'sql_scripts', 'users', ['run_scheduled_by'], ['id'])


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
