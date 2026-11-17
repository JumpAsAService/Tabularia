"""the home page: each person's grid of widgets, and the objects they pinned to it
(docs/specs/home.md).

Only additions: two tables, empty, that the previous version neither writes nor reads.
`user_home` holds one row per person who changed their home page (the widgets as JSON);
without a row the gateway answers its default layout. `user_favourites` holds the objects
a person pinned, by kind and id. Both go with the account (ON DELETE CASCADE).

Revision 0043, after 0042 — 2026-11-17.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('user_home',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('definition', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id')
    )
    op.create_table('user_favourites',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
    sa.Column('object_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'kind', 'object_id', name='uq_user_favourite')
    )
    op.create_index(op.f('ix_user_favourites_user_id'), 'user_favourites', ['user_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
