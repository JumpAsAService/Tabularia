"""flow shortcuts: the transformations a person keeps in the flow editor's right-click menu,
in their order (PUT /users/me/flow-shortcuts).

Only an addition: one nullable column on `users` (a JSON list of operation types; None =
never chosen, the interface's defaults), which the previous version neither writes nor reads.

Revision 0038, after 0037 — 2026-11-06.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa


revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('flow_shortcuts', sa.Text(), nullable=True))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
