"""data APIs: datasources published as a read-only HTTP API, answered by pods of their own
on Kubernetes (docs/specs/data-apis.md).

Only additions: five tables, empty, that the previous version neither writes nor reads. The
creator's foreign key cascades (a deleted account takes its data APIs, their resources,
versions, keys and usage with it); a datasource a resource publishes cannot be deleted
(RESTRICT); who saved a version or made a key is forgotten with the account (SET NULL).

Revision 0040, after 0039 — 2026-11-07.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0040'
down_revision = '0039'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('data_apis',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('slug', sqlmodel.sql.sqltypes.AutoString(length=63), nullable=False),
    sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('creator_id', sa.Integer(), nullable=False),
    sa.Column('state', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('runtime', sa.Text(), nullable=False),
    sa.Column('pod_token_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'name', name='uq_data_api_project_name'),
    sa.UniqueConstraint('slug')
    )
    op.create_index(op.f('ix_data_apis_creator_id'), 'data_apis', ['creator_id'], unique=False)
    op.create_index(op.f('ix_data_apis_project_id'), 'data_apis', ['project_id'], unique=False)
    op.create_table('data_api_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('data_api_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('note', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['data_api_id'], ['data_apis.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('data_api_id', 'version', name='uq_data_api_version')
    )
    op.create_index(op.f('ix_data_api_versions_data_api_id'), 'data_api_versions', ['data_api_id'], unique=False)
    op.create_table('data_api_resources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('data_api_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=63), nullable=False),
    sa.Column('source_kind', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('datasource_id', sa.Integer(), nullable=True),
    sa.Column('columns', sa.Text(), nullable=False),
    sa.Column('filters', sa.Text(), nullable=False),
    sa.Column('default_sort', sa.Text(), nullable=False),
    sa.Column('max_page', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['data_api_id'], ['data_apis.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['datasource_id'], ['datasources.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('data_api_id', 'name', name='uq_data_api_resource_name')
    )
    op.create_index(op.f('ix_data_api_resources_data_api_id'), 'data_api_resources', ['data_api_id'], unique=False)
    op.create_index(op.f('ix_data_api_resources_datasource_id'), 'data_api_resources', ['datasource_id'], unique=False)
    op.create_table('data_api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('data_api_id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('prefix', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('rate_per_minute', sa.Integer(), nullable=False),
    sa.Column('resources', sa.Text(), nullable=False),
    sa.Column('cidrs', sa.Text(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('grace_until', sa.DateTime(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['data_api_id'], ['data_apis.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('hash')
    )
    op.create_index(op.f('ix_data_api_keys_data_api_id'), 'data_api_keys', ['data_api_id'], unique=False)
    op.create_table('data_api_usage',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('data_api_id', sa.Integer(), nullable=False),
    sa.Column('key_id', sa.Integer(), nullable=False),
    sa.Column('minute', sa.DateTime(), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.Column('rows', sa.Integer(), nullable=False),
    sa.Column('errors', sa.Integer(), nullable=False),
    sa.Column('throttled', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['data_api_id'], ['data_apis.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['key_id'], ['data_api_keys.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_id', 'minute', name='uq_data_api_usage_key_minute')
    )
    op.create_index(op.f('ix_data_api_usage_data_api_id'), 'data_api_usage', ['data_api_id'], unique=False)
    op.create_index(op.f('ix_data_api_usage_minute'), 'data_api_usage', ['minute'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
