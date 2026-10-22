"""saved scripts: Python or R code kept as an object of a folder

Only additions: two tables, empty — the saved scripts (name, folder, scripting
environment, the version that runs, the schedule) and their versions (files,
entrypoint, inputs, output settings). The code of the previous version neither knows
nor touches them. docs/specs/saved-scripts.md.

Revision 0019, after 0018 — 2026-10-22.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the columns


revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('saved_scripts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('environment_id', sa.Integer(), nullable=False),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('run_schedule', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('run_scheduled_by', sa.Integer(), nullable=True),
    sa.Column('next_run_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['environment_id'], ['script_environments.id'], ),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.ForeignKeyConstraint(['run_scheduled_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'name', name='uq_saved_script_project_name')
    )
    op.create_index(op.f('ix_saved_scripts_environment_id'), 'saved_scripts', ['environment_id'], unique=False)
    op.create_index(op.f('ix_saved_scripts_next_run_at'), 'saved_scripts', ['next_run_at'], unique=False)
    op.create_index(op.f('ix_saved_scripts_project_id'), 'saved_scripts', ['project_id'], unique=False)
    op.create_table('saved_script_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('script_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('note', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['script_id'], ['saved_scripts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('script_id', 'version', name='uq_saved_script_version')
    )
    op.create_index(op.f('ix_saved_script_versions_script_id'), 'saved_script_versions', ['script_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
