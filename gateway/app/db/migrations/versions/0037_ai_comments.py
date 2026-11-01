"""ai_comments: what the AI wrote on the numbers of a dashboard or a report, and when a screen last
asked (docs/specs/ai-comment.md).

Only additions: two tables, empty, that the previous version neither writes nor reads.

Revision 0037, after 0036 — 2026-11-01.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ai_comments',
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('key', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('state', sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
    sa.Column('model', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('numbers_at', sa.DateTime(), nullable=True),
    sa.Column('cost_usd', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=False),
    sa.Column('written_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_index(op.f('ix_ai_comments_expires_at'), 'ai_comments', ['expires_at'], unique=False)
    op.create_table('ai_comment_threads',
    sa.Column('thread', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('called_at', sa.DateTime(), nullable=False),
    sa.Column('comment_key', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('thread')
    )
    op.create_index(op.f('ix_ai_comment_threads_expires_at'), 'ai_comment_threads', ['expires_at'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
