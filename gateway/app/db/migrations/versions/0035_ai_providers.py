"""AI providers added from the panel (services/ai_models.py)

Only a new table. `ai_models` keeps its key: a model of the environment's provider stays
under its bare name, so its rows, the conversations, the spending ledger and
`ai.default_model` need no change; a model of a provider of this table is written
`provider:<id>/<name>`, which is also how the same name on two providers stays two rows.

The previous version of the code, during a rolling update, does not read the new table. It
would list a model enabled from here among its own, and the environment's endpoint would
refuse it: an administrator would have to add a provider during the update to see that.

Revision 0035, after 0034 — 2026-10-30.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel


revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ai_providers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=80), nullable=False),
        sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
        sa.Column('base_url', sqlmodel.sql.sqltypes.AutoString(length=500), nullable=False),
        sa.Column('secret_encrypted', sa.Text(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_ai_providers_name', 'ai_providers', ['name'], unique=True)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
