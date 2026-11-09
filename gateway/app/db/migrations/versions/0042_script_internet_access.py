"""scripting environments: whether a version's scripts and notebooks may reach the internet
(Administration → Scripting environments → a version's «Internet access», docs/scripting.md).

Only an addition: one column on `script_environment_versions`, NOT NULL with the server
default true, so every existing version keeps reaching the internet as before and the
previous version's insert (without the column) still works. False makes the gateway put
the label tabularia.io/internet=off on the pods of the version's scripts and notebooks,
which the chart's NetworkPolicy confines to the cluster's DNS, the storage and what the
administrators allow.

Revision 0042, after 0041 — 2026-11-09.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa


revision = '0042'
down_revision = '0041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('script_environment_versions',
                  sa.Column('internet_access', sa.Boolean(), server_default='true', nullable=False))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
