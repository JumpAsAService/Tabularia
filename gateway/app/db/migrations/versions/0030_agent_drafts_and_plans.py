"""agent drafts and plans: the work areas of the agent's flows and the changes it proposes,
in the chat and over MCP (docs/specs/mcp-agent.md)

Only additions: two tables, empty — `agent_drafts` (a flow being built or changed, gone
after a day unused) and `agent_plans` (the actions a person confirms, with their outcome).
The code of the previous version neither writes nor reads them.

Revision 0030, after 0029 — 2026-10-28.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('agent_drafts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('origin', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=True),
    sa.Column('token_name', sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
    sa.Column('flow_id', sa.Integer(), nullable=True),
    sa.Column('base_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=True),
    sa.Column('engine', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=False),
    sa.Column('definition', sa.Text(), nullable=False),
    sa.Column('counters', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_drafts_updated_at'), 'agent_drafts', ['updated_at'], unique=False)
    op.create_index(op.f('ix_agent_drafts_user_id'), 'agent_drafts', ['user_id'], unique=False)
    op.create_table('agent_plans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('origin', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('chat_id', sa.Integer(), nullable=True),
    sa.Column('token_name', sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
    sa.Column('actions', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('status', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('applied_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_plans_chat_id'), 'agent_plans', ['chat_id'], unique=False)
    op.create_index(op.f('ix_agent_plans_expires_at'), 'agent_plans', ['expires_at'], unique=False)
    op.create_index(op.f('ix_agent_plans_status'), 'agent_plans', ['status'], unique=False)
    op.create_index(op.f('ix_agent_plans_user_id'), 'agent_plans', ['user_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
