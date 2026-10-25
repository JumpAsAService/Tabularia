"""The gateway's schema migrations, with Alembic.

The revisions are in `app/db/migrations/versions`, generated from the models and read
by a person before the commit. A SEPARATE STEP applies them before the gateways start
(a Helm Job, a one-off service in the compose):

    python -m app.db.migra                 brings the database to the latest revision
    python -m app.db.migra stato           which revision the database is at
    python -m app.db.migra controlla       is the database the same as the models? (exits 1 if not)
    python -m app.db.migra nuova "text"    generates a revision from the models (development)

In development (`DB__AUTO_MIGRATE=true`) the gateway migrates by itself at start.
Otherwise it only checks: a database OLDER than the code stops the start with a clear
message; a NEWER one does not — it is the previous version's pod during a rolling
update, and the revisions are written so that it works (expand/contract).

A database from BEFORE Alembic (the tables are there, `alembic_version` is not) is
adopted: see `app/db/legacy.py`. After every migration the schema is compared with the
models, and a difference stops the step: two installations of the same version must
have the same schema (before, it was not so)."""
from __future__ import annotations

import logging
import os
import re
import sys

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

logger = logging.getLogger(__name__)
# the comparison with the models writes an INFO line per sequence and per plugin (some
# twenty at every `controlla` and every start): the warnings stay; «Running upgrade …»
# (runtime.migration) stays
for _noisy in ("alembic.ddl.postgresql", "alembic.runtime.plugins"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
BASE = "0001"                 # the base revision: the schema of the last version without Alembic


def _include(obj, name, kind, mirrored, comparison) -> bool:
    # `alembic_version` is Alembic's, not the models'
    return not (kind == "table" and name == "alembic_version")


COMPARE_OPTIONS = {"compare_type": True, "compare_server_default": True, "include_object": _include}


def _config(conn=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", FOLDER)
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    if conn is not None:
        cfg.attributes["connection"] = conn
    return cfg


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_config())


def head() -> str | None:
    """The latest revision this code knows."""
    return _script().get_current_head()


def revision(conn) -> str | None:
    """The database's revision (None: none — empty, or from before Alembic)."""
    return MigrationContext.configure(conn).get_current_revision()


def differences(conn) -> list:
    """What separates the database's schema from the models (empty = the same)."""
    from sqlmodel import SQLModel

    import app.models  # noqa: F401

    return compare_metadata(MigrationContext.configure(conn, opts=COMPARE_OPTIONS), SQLModel.metadata)


def migra(eng=None, verify: bool = True) -> dict:
    """Brings the database to the latest revision, in ONE transaction (in Postgres the
    schema is transactional too: all or nothing). To call under the start lock."""
    from app.db.session import engine

    eng = eng or engine
    with eng.begin() as conn:
        before = revision(conn)
        if before is not None and before not in {r.revision for r in _script().walk_revisions()}:
            # a database newer than this code: the previous release rolled back to with
            # `helm rollback` (docs/deploy/upgrading.md). Every revision keeps the previous
            # release working (expand/contract), so it starts on it; going further back
            # means restoring a backup
            logger.warning("the database is at revision %s, newer than this gateway's (%s): nothing to migrate, "
                           "this release runs on it (a rollback of one release)", before, head())
            return {"prima": before, "dopo": before, "adottato": False}
        adopted = False
        if before is None and inspect(conn).has_table("users"):
            # a database from before Alembic: bring it to the base's state and stamp it
            from app.db.legacy import adopt

            adopt(conn)
            command.stamp(_config(conn), BASE)
            adopted = True
        command.upgrade(_config(conn), "head")
        after = revision(conn)
        if verify:
            diff = differences(conn)
            if diff:
                raise RuntimeError(
                    "after the migration the database schema differs from the models (a revision is missing, "
                    f"or the database was changed by hand): {diff[:8]}"
                )
    if before != after or adopted:
        logger.info("database schema: %s → %s%s", before or "no revision", after, " (adopted from before Alembic)" if adopted else "")
    return {"prima": before, "dopo": after, "adottato": adopted}


class DatabaseBehind(RuntimeError):
    """The database is older than this version of the gateway."""


def check_revision(eng=None) -> str | None:
    """Without automatic migration: the database must be at least at the revision this code
    knows. Newer is fine (rolling update, expand/contract)."""
    from app.db.session import engine

    eng = eng or engine
    with eng.connect() as conn:
        rev = revision(conn)
    wanted = head()
    if rev == wanted:
        return rev
    known = {r.revision for r in _script().walk_revisions()}
    if rev is not None and rev not in known:
        logger.warning("the database is at revision %s, newer than this gateway's (%s): "
                       "fine during a rolling update", rev, wanted)
        return rev
    raise DatabaseBehind(
        f"the database is at revision {rev or 'none (empty, or from before Alembic)'} but this version of the gateway "
        f"needs {wanted}: run the migration step first (`python -m app.db.migra`), "
        "or, in development, DB__AUTO_MIGRATE=true"
    )


def prepare_database() -> None:
    """At the gateway's start: migrates (development) or checks (production), under the lock."""
    from app.core.config import get_settings
    from app.db.session import startup_lock, wait_for_db

    wait_for_db()
    with startup_lock():
        if get_settings().db.auto_migrate:
            migra()
        else:
            check_revision()


def new_revision(message: str, eng=None) -> str:
    """Generates a revision from the models, comparing them with the database (which must be
    at the latest revision). The file must be read and corrected before the commit."""
    from app.db.session import engine

    existing = [int(m.group(1)) for f in os.listdir(os.path.join(FOLDER, "versions"))
                 if (m := re.match(r"^(\d{4})_", f))]
    number = f"{(max(existing) + 1) if existing else 1:04d}"
    with (eng or engine).begin() as conn:
        script = command.revision(_config(conn), message=message, autogenerate=True, rev_id=number)
    return script.path if hasattr(script, "path") else number


def _main(arguments: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from app.db.session import engine, startup_lock, wait_for_db

    action = arguments[0] if arguments else "migra"
    wait_for_db()
    if action == "migra":
        with startup_lock():
            outcome = migra()
        print(f"database at revision {outcome['dopo']}" + (f" (was {outcome['prima'] or 'without a revision'})" if outcome["prima"] != outcome["dopo"] else "")
              + (", adopted from before Alembic" if outcome["adottato"] else ""))
        return 0
    if action == "stato":
        with engine.connect() as conn:
            print(f"database: {revision(conn) or 'no revision'} · code: {head()}")
        return 0
    if action == "controlla":
        with engine.connect() as conn:
            diff = differences(conn)
        for d in diff:
            print(d)
        print("schema equal to the models" if not diff else f"{len(diff)} differences")
        return 1 if diff else 0
    if action == "nuova" and len(arguments) > 1:
        print(new_revision(arguments[1]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
