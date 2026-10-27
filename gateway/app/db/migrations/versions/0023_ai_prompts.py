"""ai prompts: the AI agents' instructions as the administrators changed them from
Administration → AI (services/ai_prompts.py).

Only an addition: a table the previous version neither writes nor reads.

Revision 0023, after 0022 — 2026-10-27.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ai_prompts',
    sa.Column('agent', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.PrimaryKeyConstraint('agent')
    )


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
