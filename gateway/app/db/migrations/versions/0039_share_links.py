"""share links: a report or a dashboard opened from outside Tabularia with a link and its own
password, or embedded in the pages of listed origins (docs/specs/share-links.md).

Only additions: three tables that the previous version neither writes nor reads, and the one
row of the links' settings with their defaults (the feature on, 90 days, 20 AI comments an hour).

Revision 0039, after 0038 — 2026-11-07.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0039'
down_revision = '0038'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('share_links',
    sa.Column('origins', sa.Text(), nullable=False),
    sa.Column('params', sa.Text(), nullable=False),
    sa.Column('downloads', sa.Text(), nullable=False),
    sa.Column('ai_last', sa.Text(), nullable=True),
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('token_hint', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('object_id', sa.Integer(), nullable=False),
    sa.Column('label', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('creator_id', sa.Integer(), nullable=False),
    sa.Column('embeddable', sa.Boolean(), nullable=False),
    sa.Column('password_hash', sqlmodel.sql.sqltypes.AutoString(length=100), nullable=True),
    sa.Column('password_temporary', sa.Boolean(), nullable=False),
    sa.Column('failures', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_opened_at', sa.DateTime(), nullable=True),
    sa.Column('openings', sa.Integer(), nullable=False),
    sa.Column('ai_hour', sa.DateTime(), nullable=True),
    sa.Column('ai_calls', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_share_links_creator_id'), 'share_links', ['creator_id'], unique=False)
    op.create_index(op.f('ix_share_links_expires_at'), 'share_links', ['expires_at'], unique=False)
    op.create_index(op.f('ix_share_links_object_id'), 'share_links', ['object_id'], unique=False)
    op.create_table('share_link_sessions',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('link_id', sa.Integer(), nullable=False),
    sa.Column('purpose', sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('ip', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.ForeignKeyConstraint(['link_id'], ['share_links.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_share_link_sessions_expires_at'), 'share_link_sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_share_link_sessions_link_id'), 'share_link_sessions', ['link_id'], unique=False)
    op.create_table('share_link_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('max_days', sa.Integer(), nullable=False),
    sa.Column('ai_comments_per_hour', sa.Integer(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.execute("INSERT INTO share_link_settings (id, enabled, max_days, ai_comments_per_hour) VALUES (1, true, 90, 20)")


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
