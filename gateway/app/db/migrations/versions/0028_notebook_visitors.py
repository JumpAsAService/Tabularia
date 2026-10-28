"""notebook visitors: how many people have a notebook's app open, counted across the
gateway replicas, and the cap a notebook may set for itself (docs/specs/notebooks.md,
«Visitors»)

Only additions: one nullable column on `notebooks` (None = the installation's default,
NOTEBOOKS__MAX_VISITORS_PER_APP) and one table, empty — an app open in a browser,
refreshed while its WebSocket lives. The code of the previous version neither writes nor
reads them.

Revision 0028, after 0027 — 2026-10-28.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa


revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('notebooks', sa.Column('max_visitors', sa.Integer(), nullable=True))
    op.create_table('notebook_visitors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('notebook_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('opened_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['notebook_id'], ['notebooks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notebook_visitors_notebook_id'), 'notebook_visitors', ['notebook_id'], unique=False)
    op.create_index(op.f('ix_notebook_visitors_user_id'), 'notebook_visitors', ['user_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
