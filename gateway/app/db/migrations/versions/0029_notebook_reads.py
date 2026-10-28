"""notebook reads: the datasources each notebook has read, for the Lineage page and
OpenLineage (services/notebook_reads.py)

Only additions: one table, empty — a row per notebook and datasource, written the first
time a session of the notebook reads that datasource. The code of the previous version
neither writes nor reads it.

Revision 0029, after 0028 — 2026-10-28.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notebook_reads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('notebook_id', sa.Integer(), nullable=False),
    sa.Column('datasource_id', sa.Integer(), nullable=False),
    sa.Column('last_session', sqlmodel.sql.sqltypes.AutoString(length=63), nullable=False),
    sa.Column('mode', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('first_read_at', sa.DateTime(), nullable=False),
    sa.Column('read_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['datasource_id'], ['datasources.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['notebook_id'], ['notebooks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('notebook_id', 'datasource_id', name='uq_notebook_read')
    )
    op.create_index(op.f('ix_notebook_reads_datasource_id'), 'notebook_reads', ['datasource_id'], unique=False)
    op.create_index(op.f('ix_notebook_reads_notebook_id'), 'notebook_reads', ['notebook_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
