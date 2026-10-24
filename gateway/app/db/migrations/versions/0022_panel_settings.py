"""panel settings: the gateway's settings changed from Administration → Settings, and the
engine's that are not whole numbers

Only additions: two tables the previous version neither writes nor reads (the settings,
the rollouts of the components that read some only at start), and a nullable column of
engine_settings it ignores (it keeps writing and reading `value`), and the heartbeat of a
running backup (`database_backups.alive_at`, which the previous version neither writes nor
reads: its claims age as before).
docs/specs/panel-settings.md.

Revision 0022, after 0021 — 2026-10-23.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('gateway_settings',
    sa.Column('key', sqlmodel.sql.sqltypes.AutoString(length=80), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('settings_rollouts',
    sa.Column('component', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
    sa.Column('changed_at', sa.DateTime(), nullable=True),
    sa.Column('requested_at', sa.DateTime(), nullable=True),
    sa.Column('requested_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.PrimaryKeyConstraint('component')
    )
    op.add_column('engine_settings', sa.Column('value_json', sa.Text(), nullable=True))
    # the running backup's heartbeat (services/db_backup.py)
    op.add_column('database_backups', sa.Column('alive_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
