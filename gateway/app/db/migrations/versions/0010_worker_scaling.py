"""worker_scaling: the autoscaling bounds set from Admin → Workers (Kubernetes)

Only an addition: the table `worker_scaling`, empty (the cluster keeps what the
Helm chart set). The code of the previous version neither knows nor touches it: a
rolling update changes nothing until someone sets bounds from the new panel.

Revision 0010, after 0009 — 2026-10-15.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the columns
from alembic import op


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'worker_scaling',
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
        sa.Column('min_replicas', sa.Integer(), nullable=False),
        sa.Column('max_replicas', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
        sa.PrimaryKeyConstraint('kind'),
    )


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
