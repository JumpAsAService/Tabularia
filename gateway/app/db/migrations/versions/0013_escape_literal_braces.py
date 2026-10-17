"""escape literal braces: stored definitions keep their meaning under the placeholder language

A DATA migration, no schema change. Before the placeholder language, braces were
literal text in some fields of the flows' Output nodes (email subject and
attachment name, S3 key, S3 copy key, SFTP folder; the SFTP file name knew only
{date} {datetime} {run_id} {flow}) and of the SFTP and SharePoint datasources
(folder, pattern, path). They are written `{{` `}}` here, in `flows`,
`flow_versions` and `datasources`, so every stored definition resolves to the same
text as before (see app/db/brace_escape.py): every brace doubled, `{{` included.
Rows without such braces, and rows that are NULL, empty or not JSON, are not
rewritten. Alembic runs it once: it is not idempotent.

Revision 0013, after 0012 — 2026-10-18. Forward only. The code of the previous
version reads `{{` literally too (it had no escapes), so a rollback of the code
would see the doubled braces: roll back only together with a database backup.
downgrade() is a documented no-op: un-escaping cannot tell the braces this revision
doubled from braces written doubled afterwards.
"""
import json

import sqlalchemy as sa
from alembic import op

from app.db.brace_escape import escape_flow_definition, escape_source_ref

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("flows", "flow_versions"):
        rows = bind.execute(sa.text(f"SELECT id, definition FROM {table} WHERE definition LIKE '%{{%' OR definition LIKE '%}}%'")).all()
        for row_id, definition in rows:
            if not definition:
                continue
            try:
                before = json.loads(definition)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(before, dict):
                continue
            after = escape_flow_definition(json.loads(definition))
            if after != before:
                bind.execute(sa.text(f"UPDATE {table} SET definition = :d WHERE id = :i"), {"d": _dump(after), "i": row_id})
    rows = bind.execute(sa.text("SELECT id, source_type, source_ref FROM datasources "
                                "WHERE source_type IN ('sftp', 'sharepoint')")).all()
    for row_id, source_type, ref in rows:
        if not ref:  # NULL or empty: nothing to escape
            continue
        try:
            before = json.loads(ref)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(before, dict):
            continue
        after = escape_source_ref(source_type, json.loads(ref))
        if after != before:
            bind.execute(sa.text("UPDATE datasources SET source_ref = :r WHERE id = :i"), {"r": json.dumps(after), "i": row_id})


def downgrade() -> None:
    # a documented no-op (see the docstring): the escaped braces stay
    pass
