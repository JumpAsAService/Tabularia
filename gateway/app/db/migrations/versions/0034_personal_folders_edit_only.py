"""personal folders: EDIT instead of MANAGE (services/personal_folders.py)

MANAGE holds CONNECT, and a connection may point at any host the workers reach: with a
personal folder every account could create one, and copy out whatever it may read. The
person's own grant goes down to EDIT; the MANAGE and CONNECT grants the person gave others
on it go. The grants to VIEW, RUN and EDIT stay: they reach nothing outside. Connections
already made in these folders stay too, for the administrators to judge: the person no
longer sees them.

Only data changes. A personal folder is told by the line its creation left in the audit
(`"personal": true`), not by its shape: this revision also runs on installations that never
had one, where the same shape can be a company folder and its grants somebody's decision.

Revision 0034, after 0033 — 2026-10-30.
Forward only, and compatible with the code of the previous version (expand/contract):
see docs/database-migrations.md.
"""
from alembic import op
import sqlalchemy as sa


revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    folders = bind.execute(sa.text(
        "SELECT DISTINCT p.id, p.owner_id FROM projects p JOIN audit_logs a ON a.target_id = p.id "
        "WHERE a.action = 'folder.create' AND a.target_type = 'folder' AND a.detail LIKE '%\"personal\": true%' "
        "AND p.parent_id IS NULL AND p.owner_id IS NOT NULL"
    )).all()
    for folder, owner in folders:
        keys = {"folder": folder, "owner": owner}
        own = sa.text("SELECT 1 FROM permissions WHERE project_id = :folder AND user_id = :owner AND capability = 'manage'")
        if bind.execute(own, keys).first():
            bind.execute(sa.text("DELETE FROM permissions WHERE project_id = :folder AND user_id = :owner "
                                 "AND capability = 'edit'"), keys)
            bind.execute(sa.text("UPDATE permissions SET capability = 'edit' WHERE project_id = :folder "
                                 "AND user_id = :owner AND capability = 'manage'"), keys)
        bind.execute(sa.text("DELETE FROM permissions WHERE project_id = :folder "
                             "AND capability IN ('manage', 'connect')"), keys)


def downgrade() -> None:
    # forward only (the project's choice): a mistake is fixed by a new revision
    raise NotImplementedError("gateway migrations only go forward")
