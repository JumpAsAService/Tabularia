"""${message}

Revisione ${up_revision}${", dopo " + down_revision if down_revision else ", la prima"} — ${create_date}
Solo in avanti, e compatibile col codice della versione precedente (expand/contract):
vedi docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — i tipi di SQLModel (AutoString) nelle colonne generate
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    # solo in avanti (scelta del progetto): si corregge con una revisione nuova
    raise NotImplementedError("le migrazioni del gateway vanno solo in avanti")
