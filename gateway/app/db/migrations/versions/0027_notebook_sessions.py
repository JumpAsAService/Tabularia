"""notebook sessions: the live pods of the notebooks, one per notebook and mode
(docs/specs/notebooks.md, «Runtime»)

Only additions: one table, empty — which pod serves a notebook's app or editor, whose
rights it reads with, its state and when somebody last used it. The code of the previous
version neither writes nor reads it.

Revision 0027, after 0026 — 2026-10-27.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notebook_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('notebook_id', sa.Integer(), nullable=False),
    sa.Column('mode', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('namespace', sqlmodel.sql.sqltypes.AutoString(length=63), nullable=False),
    sa.Column('job_name', sqlmodel.sql.sqltypes.AutoString(length=63), nullable=False),
    sa.Column('pod_ip', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('token_fp', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('ready_at', sa.DateTime(), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['notebook_id'], ['notebooks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('notebook_id', 'mode', name='uq_notebook_session_mode')
    )
    op.create_index(op.f('ix_notebook_sessions_notebook_id'), 'notebook_sessions', ['notebook_id'], unique=False)
    op.create_index(op.f('ix_notebook_sessions_token_fp'), 'notebook_sessions', ['token_fp'], unique=False)
    op.create_index(op.f('ix_notebook_sessions_user_id'), 'notebook_sessions', ['user_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
