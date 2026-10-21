"""script images: the architecture each environment version was built for

Only an addition: a nullable `arch` column on `script_environment_versions` (amd64,
arm64…). A build runs on one node and BuildKit builds for that node's architecture;
the check and the scripts are then sent to nodes of the same one, so a cluster with
amd64 and arm64 nodes never runs an image on the other. Versions built before stay
without it and run anywhere, as they did. The code of the previous version neither
knows nor touches it.

Revision 0017, after 0016 — 2026-10-21.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the columns


revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('script_environment_versions',
                  sa.Column('arch', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
