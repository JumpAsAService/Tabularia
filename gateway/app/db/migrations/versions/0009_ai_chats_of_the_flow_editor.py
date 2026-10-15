"""ai_chats: the flow editor's conversations, beside the data assistant's

The flow editor has an assistant of its own that builds flows on the canvas. Its
conversations are kept like the data assistant's (the server holds the history,
the spending caps count them), marked by `kind` and tied to the flow they were
about, so the editor reopens the latest one and the assistant's list leaves them
out.

Only additions with defaults: the code of the previous version neither reads nor
needs them.

Revision 0009, after 0008 — 2026-10-15.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
import sqlalchemy as sa
from alembic import op


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ai_chats', sa.Column('kind', sa.String(), nullable=False, server_default='assistant'))
    op.add_column('ai_chats', sa.Column('flow_id', sa.Integer(), nullable=True))
    op.create_index('ix_ai_chats_user_id_flow_id', 'ai_chats', ['user_id', 'flow_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
