"""Bayesian models: the compiles of the saved programs and the cache of their binaries
(docs/specs/bayes-stan-compile.md, «The schema», approved by the owner on 2026-11-24).

Only additions: two empty tables, which only the Go gateway reads and writes, so the code
of the previous version neither meets nor misses them:
· `bayes_binaries`: one row per cache key (the program's sha256, CmdStan's version, the
  flags, the architecture and the image's digest): the compile's state and reason, the
  person whose turn it counts in, the request that queued it, the attempt and the Job's
  name, the limits it ran under (a `timeout` is not compiled again under the same limits),
  and once ready the binary's sha256 from the termination message, its size, the sha256 of
  stanc's info, stanc's warnings, the compile's time and the last use (the cache's order);
· `bayes_settings`: one row, the administrator's compile limits, «Compiles at once», the
  cache's limit and the sampling Job's /tmp; a NULL column is the recipe's default.

Revision 0047, after 0046 — 2026-11-24.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bayes_binaries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cache_key', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('program_sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
    sa.Column('cmdstan', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('image_digest', sqlmodel.sql.sqltypes.AutoString(length=71), nullable=False),
    sa.Column('arch', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=False),
    sa.Column('group_version', sa.Integer(), nullable=False),
    sa.Column('state', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
    sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('requested_by', sa.Integer(), nullable=True),
    sa.Column('model_id', sa.Integer(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('attempt', sa.Integer(), server_default='0', nullable=False),
    sa.Column('job_name', sqlmodel.sql.sqltypes.AutoString(length=63), nullable=True),
    sa.Column('deadline_seconds', sa.Integer(), nullable=True),
    sa.Column('memory_bytes', sa.BigInteger(), nullable=True),
    sa.Column('binary_sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('binary_bytes', sa.BigInteger(), nullable=True),
    sa.Column('info_sha256', sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
    sa.Column('warnings', sa.Text(), nullable=True),
    sa.Column('seconds', sa.Float(), nullable=True),
    sa.Column('queued_at', sa.DateTime(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('cache_key', name='uq_bayes_binary_cache_key')
    )
    op.create_index(op.f('ix_bayes_binaries_image_digest'), 'bayes_binaries', ['image_digest'], unique=False)
    op.create_index(op.f('ix_bayes_binaries_program_sha256'), 'bayes_binaries', ['program_sha256'], unique=False)
    op.create_table('bayes_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('compile_cpu', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=True),
    sa.Column('compile_memory', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True),
    sa.Column('compile_seconds', sa.Integer(), nullable=True),
    sa.Column('compile_tmp', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True),
    sa.Column('compile_slots', sa.Integer(), nullable=True),
    sa.Column('cache_max_bytes', sa.BigInteger(), nullable=True),
    sa.Column('sampling_tmp', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True),
    sa.Column('updated_by', sqlmodel.sql.sqltypes.AutoString(length=320), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
