"""report views: who opened a report in the viewer, for the Reports tab's «trending»
(services/report_views.py)

Only additions: one table, empty — a row per report, person and day — and two columns
of `reports` with their defaults (0 openings, never opened). The code of the previous
version neither writes nor reads them.

Revision 0033, after 0032 — 2026-10-29.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns


revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('reports', sa.Column('views_total', sa.Integer(), server_default='0', nullable=False))
    op.add_column('reports', sa.Column('last_viewed_at', sa.DateTime(), nullable=True))
    op.create_table('report_views',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('report_id', 'user_id', 'day', name='uq_report_view_day')
    )
    op.create_index(op.f('ix_report_views_day'), 'report_views', ['day'], unique=False)
    op.create_index(op.f('ix_report_views_user_id'), 'report_views', ['user_id'], unique=False)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
