"""notebooks: marimo notebooks kept as objects of a folder, with their versions
(docs/specs/notebooks.md)

Only additions: two tables, empty — the notebooks (name, folder, scripting environment,
the version the app runs, the editor's draft) and their versions (the marimo file). The
code of the previous version neither writes nor reads them.

Revision 0026, after 0025 — 2026-10-27.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('notebooks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('environment_id', sa.Integer(), nullable=False),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('draft', sa.Text(), nullable=True),
    sa.Column('draft_by', sa.Integer(), nullable=True),
    sa.Column('draft_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['draft_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['environment_id'], ['script_environments.id'], ),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'name', name='uq_notebook_project_name')
    )
    op.create_index(op.f('ix_notebooks_environment_id'), 'notebooks', ['environment_id'], unique=False)
    op.create_index(op.f('ix_notebooks_project_id'), 'notebooks', ['project_id'], unique=False)
    op.create_table('notebook_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('notebook_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('note', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['notebook_id'], ['notebooks.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('notebook_id', 'version', name='uq_notebook_version')
    )
    op.create_index(op.f('ix_notebook_versions_notebook_id'), 'notebook_versions', ['notebook_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
