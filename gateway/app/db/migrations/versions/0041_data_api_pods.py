"""data APIs, their pods: what the cluster says of each data API and of each of its pods, and
what every pod reports of itself (docs/specs/data-apis.md, «Runtime»).

Only additions: two tables, empty, that the previous version neither writes nor reads. The
gateway's reconcile loop writes `data_api_status` (one row per data API: the pods wanted and
ready, the disk given to each, the last thing the cluster refused) and the cluster's side of
`data_api_pods`; every pod writes its own report there with its poll (the data version each
resource serves, whether it is preparing one, its last error). Both go with their data API.

Revision 0041, after 0040 — 2026-11-07.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0041'
down_revision = '0040'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('data_api_status',
    sa.Column('data_api_id', sa.Integer(), nullable=False),
    sa.Column('cluster', sa.Text(), nullable=False),
    sa.Column('checked_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['data_api_id'], ['data_apis.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('data_api_id')
    )
    op.create_table('data_api_pods',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('data_api_id', sa.Integer(), nullable=False),
    sa.Column('pod', sqlmodel.sql.sqltypes.AutoString(length=253), nullable=False),
    sa.Column('cluster', sa.Text(), nullable=False),
    sa.Column('cluster_at', sa.DateTime(), nullable=True),
    sa.Column('report', sa.Text(), nullable=False),
    sa.Column('reported_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['data_api_id'], ['data_apis.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('data_api_id', 'pod', name='uq_data_api_pod')
    )
    op.create_index(op.f('ix_data_api_pods_data_api_id'), 'data_api_pods', ['data_api_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
