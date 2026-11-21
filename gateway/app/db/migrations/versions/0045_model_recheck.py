"""statistical models: the recheck of a group's image in use that could not run
(docs/specs/statistical-models.md, «Model groups and their images»).

When an upgrade changes a group's runner, the gateway runs the group's self-test again on
the image in use, which keeps serving meanwhile. A recheck that the cluster stops before the
self-test reports (a Pod never scheduled, the deadline, a node gone, a log the cluster does
not give) says nothing of the image: it is tried again later, a few times, instead of failing
the version in use. Four nullable or defaulted columns on `script_environment_versions`:
· `recheck_runner`: the runner the attempts below were for (a new runner starts them again);
· `recheck_attempts` (default 0): how many in a row could not run;
· `recheck_after`: not before this moment, the next one;
· `recheck_error`: why the last one could not run.

Revision 0045, after 0044 — 2026-11-21.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0045'
down_revision = '0044'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('script_environment_versions',
                  sa.Column('recheck_runner', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('script_environment_versions',
                  sa.Column('recheck_attempts', sa.Integer(), server_default='0', nullable=False))
    op.add_column('script_environment_versions', sa.Column('recheck_after', sa.DateTime(), nullable=True))
    op.add_column('script_environment_versions', sa.Column('recheck_error', sa.Text(), nullable=True))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
