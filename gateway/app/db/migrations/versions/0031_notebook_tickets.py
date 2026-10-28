"""notebook tickets: the one-minute, one-use tickets that open a notebook's app or editor
on the notebooks' own address (NOTEBOOKS__ORIGIN; docs/specs/notebooks.md, «An origin of
their own»)

Only additions: one table, empty. The code of the previous version neither writes nor
reads it.

Revision 0031, after 0030 — 2026-10-28.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notebook_tickets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_fp', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('notebook_id', sa.Integer(), nullable=False),
    sa.Column('mode', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('sid', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('frame_origin', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['notebook_id'], ['notebooks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notebook_tickets_expires_at'), 'notebook_tickets', ['expires_at'], unique=False)
    op.create_index(op.f('ix_notebook_tickets_notebook_id'), 'notebook_tickets', ['notebook_id'], unique=False)
    op.create_index(op.f('ix_notebook_tickets_token_fp'), 'notebook_tickets', ['token_fp'], unique=True)
    op.create_index(op.f('ix_notebook_tickets_user_id'), 'notebook_tickets', ['user_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
