"""database backups: one row, the administrators' choices and how the backups go

Only an addition: a table the previous version neither writes nor reads
(services/db_backup.py, Administration → Backups).

Revision 0021, after 0020 — 2026-10-22.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('database_backups',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=True),
    sa.Column('schedule', sqlmodel.sql.sqltypes.AutoString(length=120), nullable=True),
    sa.Column('keep', sa.Integer(), nullable=True),
    sa.Column('next_at', sa.DateTime(), nullable=True),
    sa.Column('next_for', sqlmodel.sql.sqltypes.AutoString(length=120), nullable=True),
    sa.Column('requested_at', sa.DateTime(), nullable=True),
    sa.Column('requested_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.Column('running_since', sa.DateTime(), nullable=True),
    sa.Column('claimed_by', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=True),
    sa.Column('last_ok_at', sa.DateTime(), nullable=True),
    sa.Column('last_name', sqlmodel.sql.sqltypes.AutoString(length=120), nullable=True),
    sa.Column('last_size', sa.BigInteger(), nullable=True),
    sa.Column('last_seconds', sa.Float(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('last_error_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
