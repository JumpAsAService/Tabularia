"""users.locale: the language of the emails the product sends

The language each user last chose in the interface (en, it, fr, de, es), so the
flow-failure and data-contract notices reach them in it (services/email_texts.py).
A nullable column (NULL = never chose: English): the code of the previous version
does not read it, so it can run beside this schema (expand only).

Revision 0014, after 0013 — 2026-10-18. Forward only: see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — i tipi di SQLModel (AutoString) nelle colonne generate


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('locale', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=True))


def downgrade() -> None:
    # solo in avanti (scelta del progetto): si corregge con una revisione nuova
    raise NotImplementedError("le migrazioni del gateway vanno solo in avanti")
