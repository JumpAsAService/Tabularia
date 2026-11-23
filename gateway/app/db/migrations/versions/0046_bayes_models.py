"""Bayesian models: the saved models of the Bayesian editor and their versions
(docs/specs/bayes-model-store.md, «The schema»).

Only additions: two empty tables, which only the Go gateway reads and writes, so the code
of the previous version neither meets nor misses them:
· `bayes_models`: a model as an object of a folder — name (unique in its folder),
  description, folder, owner, the newest version, the template it was opened from;
· `bayes_model_versions`: each version, written once: the canonical graph, its semantic
  hash, the validator's findings (warnings only), the generated Stan program and its
  sha256 (indexed: the compile cache's key), the mapping table, the contract's two
  manifests, the generator's and the graph's versions, the catalogue's sha256 and the
  family derived from the graph.

Revision 0046, after 0045 — 2026-11-23.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0046'
down_revision = '0045'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bayes_models',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('current_version', sa.Integer(), nullable=False),
    sa.Column('template_id', sqlmodel.sql.sqltypes.AutoString(length=40), nullable=True),
    sa.Column('template_version', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'name', name='uq_bayes_model_project_name')
    )
    op.create_index(op.f('ix_bayes_models_project_id'), 'bayes_models', ['project_id'], unique=False)
    op.create_table('bayes_model_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model_id', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('graph', sa.Text(), nullable=False),
    sa.Column('format_version', sa.Integer(), nullable=False),
    sa.Column('semantic_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('findings', sa.Text(), nullable=False),
    sa.Column('program', sa.Text(), nullable=False),
    sa.Column('program_sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('mapping', sa.Text(), nullable=False),
    sa.Column('data_manifest', sa.Text(), nullable=False),
    sa.Column('quantity_manifest', sa.Text(), nullable=False),
    sa.Column('codegen_version', sa.Integer(), nullable=False),
    sa.Column('family', sqlmodel.sql.sqltypes.AutoString(length=12), nullable=False),
    sa.Column('catalog_sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('note', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['model_id'], ['bayes_models.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('model_id', 'version', name='uq_bayes_model_version')
    )
    op.create_index(op.f('ix_bayes_model_versions_model_id'), 'bayes_model_versions', ['model_id'], unique=False)
    op.create_index(op.f('ix_bayes_model_versions_program_sha256'), 'bayes_model_versions', ['program_sha256'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
