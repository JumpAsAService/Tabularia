"""run notes: what a successful refresh has to say

One nullable column added to `runs`: the notes of a refresh that succeeded with
something to tell — the values of a range of files whose file was not there
(skipped and listed instead of failing the refresh). The code of the previous
version neither knows nor reads it: a rolling update changes nothing.

Revision 0012, after 0011 — 2026-10-18.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('notes', sa.Text(), nullable=True))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
