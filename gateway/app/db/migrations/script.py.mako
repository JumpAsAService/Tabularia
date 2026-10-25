"""${message}

Revision ${up_revision}${", after " + down_revision if down_revision else ", the first"} — ${create_date}
Forward only, and compatible with the previous version's code (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the generated columns
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    # forward only (the project's choice): corrected with a new revision
    raise NotImplementedError("the gateway's migrations go forward only")
