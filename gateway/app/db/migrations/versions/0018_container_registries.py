"""container registries: Administration → Container registries

Only additions: three tables, empty — the registries the Container node's images come
from (their address, how to trust their certificate, the credentials, the allowed
repositories), the variables every container of a registry finds, and who may use it
(a group or a user, and which of its repositories). The code of the previous version neither knows nor touches them.

Revision 0018, after 0017 — 2026-10-21.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the columns


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('container_registries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=120), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('host', sqlmodel.sql.sqltypes.AutoString(length=260), nullable=False),
    sa.Column('protocol', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('tls', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('ca_cert', sa.Text(), server_default='', nullable=False),
    sa.Column('username', sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
    sa.Column('password_enc', sa.Text(), nullable=True),
    sa.Column('repositories', sa.Text(), server_default='', nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('last_test_at', sa.DateTime(), nullable=True),
    sa.Column('last_test_ok', sa.Boolean(), nullable=True),
    sa.Column('last_test_message', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('container_registry_access',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('registry_id', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('repositories', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ),
    sa.ForeignKeyConstraint(['registry_id'], ['container_registries.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_container_registry_access_group_id'), 'container_registry_access', ['group_id'], unique=False)
    op.create_index(op.f('ix_container_registry_access_registry_id'), 'container_registry_access', ['registry_id'], unique=False)
    op.create_index(op.f('ix_container_registry_access_user_id'), 'container_registry_access', ['user_id'], unique=False)
    op.create_table('container_registry_variables',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('registry_id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
    sa.Column('secret', sa.Boolean(), nullable=False),
    sa.Column('value', sa.Text(), server_default='', nullable=False),
    sa.Column('value_enc', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.ForeignKeyConstraint(['registry_id'], ['container_registries.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('registry_id', 'name', name='uq_container_registry_variable_name')
    )
    op.create_index(op.f('ix_container_registry_variables_registry_id'), 'container_registry_variables', ['registry_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
