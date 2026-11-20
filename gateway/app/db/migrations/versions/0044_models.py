"""statistical models: the groups an administrator enables, the managed scripting
environments their images are built as, and what a model step's run keeps
(docs/specs/statistical-models.md, «Data model»).

Only additions, all nullable or with a server default, so the code of the previous
version neither meets nor misses them:
· `script_environments.managed_by` (unique): `model_group:<id>` for the environment a
  model group is built as — hidden from the Scripting section, the Script node and the
  notebooks; NULL for every environment an administrator made;
· `script_environment_versions.recipe_hash`, `runner_hash` (what the version was built
  and self-tested from) and `selftest` (the self-test's report);
· `model_groups`, empty: one row per group an administrator enabled once (`enabled`
  false when disabled since), who may use it (`everyone`, or the user groups of
  `script_environment_groups` on its environment), its row limit (NULL: the recipe's);
· `runs`: the key of a model step's results, its model card (JSON text, at most 1 MiB,
  checked by the gateway) and its MLflow references and error.

Revision 0044, after 0043 — 2026-11-19.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('script_environments',
                  sa.Column('managed_by', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.create_unique_constraint('uq_script_environment_managed_by', 'script_environments', ['managed_by'])
    op.add_column('script_environment_versions',
                  sa.Column('recipe_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('script_environment_versions',
                  sa.Column('runner_hash', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('script_environment_versions', sa.Column('selftest', sa.Text(), nullable=True))
    op.create_table('model_groups',
    sa.Column('id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('environment_id', sa.Integer(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('everyone', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('max_rows', sa.BigInteger(), nullable=True),
    sa.Column('enabled_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.Column('enabled_at', sa.DateTime(), nullable=True),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['environment_id'], ['script_environments.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('environment_id', name='uq_model_group_environment')
    )
    op.add_column('runs', sa.Column('model_results_key', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('runs', sa.Column('model_card', sa.Text(), nullable=True))
    op.add_column('runs', sa.Column('mlflow_connection_id', sa.Integer(), nullable=True))
    op.add_column('runs', sa.Column('mlflow_experiment_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('runs', sa.Column('mlflow_run_id', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True))
    op.add_column('runs', sa.Column('mlflow_model_name', sqlmodel.sql.sqltypes.AutoString(length=256), nullable=True))
    op.add_column('runs', sa.Column('mlflow_model_version', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True))
    op.add_column('runs', sa.Column('mlflow_error', sa.Text(), nullable=True))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
