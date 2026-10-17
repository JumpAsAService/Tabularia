"""sftp connections: the private key and the accepted host key

Only additions to `connections`, each with an empty default: the encrypted private
key of an SFTP connection (with its passphrase), and the host key accepted at the
first test (type and SHA256 fingerprint). The code of the previous version neither
knows nor reads them: a rolling update changes nothing for the other connections.

Revision 0011, after 0010 — 2026-10-17.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel's types (AutoString) in the columns


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('connections', sa.Column('private_key_encrypted', sa.Text(), server_default='', nullable=False))
    op.add_column('connections', sa.Column('host_key_type', sqlmodel.sql.sqltypes.AutoString(), server_default='', nullable=False))
    op.add_column('connections', sa.Column('host_key_fingerprint', sqlmodel.sql.sqltypes.AutoString(), server_default='', nullable=False))


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
