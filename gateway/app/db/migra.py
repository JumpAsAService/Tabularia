"""Le migrazioni dello schema del gateway, con Alembic.

Le revisioni stanno in `app/db/migrations/versions`, generate dai modelli e lette
da una persona prima del commit. Le applica un PASSO SEPARATO prima che i gateway
partano (un Job di Helm, un servizio una tantum nel compose):

    python -m app.db.migra                 porta il database all'ultima revisione
    python -m app.db.migra stato           a che revisione è il database
    python -m app.db.migra controlla       il database è uguale ai modelli? (esce con 1 se no)
    python -m app.db.migra nuova "testo"   genera una revisione dai modelli (sviluppo)

In sviluppo (`DB__AUTO_MIGRATE=true`) il gateway migra da sé all'avvio. Altrimenti
controlla soltanto: un database PIÙ VECCHIO del codice ferma l'avvio con un messaggio
chiaro; uno PIÙ NUOVO no — è il pod della versione precedente durante un rolling
update, e le revisioni sono scritte perché funzioni (expand/contract).

Un database di PRIMA di Alembic (le tabelle ci sono, `alembic_version` no) viene
adottato: vedi `app/db/legacy.py`. Dopo ogni migrazione lo schema si confronta coi
modelli, e una differenza ferma il passo: due installazioni della stessa versione
devono avere lo stesso schema (prima non era così)."""
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
# il confronto coi modelli scrive una riga INFO per sequenza e per plugin (una ventina a ogni
# `controlla` e a ogni avvio): restano gli avvisi; «Running upgrade …» (runtime.migration) resta
for _rumoroso in ("alembic.ddl.postgresql", "alembic.runtime.plugins"):
    logging.getLogger(_rumoroso).setLevel(logging.WARNING)

CARTELLA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
BASE = "0001"                 # la revisione di base: lo schema dell'ultima versione senza Alembic


def _includi(oggetto, nome, tipo, riflesso, confronto) -> bool:
    # `alembic_version` è di Alembic, non dei modelli
    return not (tipo == "table" and nome == "alembic_version")


OPZIONI_CONFRONTO = {"compare_type": True, "compare_server_default": True, "include_object": _includi}


def _config(conn=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", CARTELLA)
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    if conn is not None:
        cfg.attributes["connection"] = conn
    return cfg


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_config())


def testa() -> str | None:
    """L'ultima revisione che questo codice conosce."""
    return _script().get_current_head()


def revisione(conn) -> str | None:
    """La revisione del database (None: nessuna — vuoto, o di prima di Alembic)."""
    return MigrationContext.configure(conn).get_current_revision()


def differenze(conn) -> list:
    """Quello che separa lo schema del database dai modelli (vuoto = uguali)."""
    from sqlmodel import SQLModel

    import app.models  # noqa: F401

    return compare_metadata(MigrationContext.configure(conn, opts=OPZIONI_CONFRONTO), SQLModel.metadata)


def migra(eng=None, verifica: bool = True) -> dict:
    """Porta il database all'ultima revisione, in UNA transazione (in Postgres anche lo
    schema è transazionale: o tutto o niente). Va chiamata sotto il lucchetto di avvio."""
    from app.db.session import engine

    eng = eng or engine
    with eng.begin() as conn:
        prima = revisione(conn)
        adottato = False
        if prima is None and inspect(conn).has_table("users"):
            # un database di prima di Alembic: portarlo allo stato della base e marcarlo
            from app.db.legacy import adotta

            adotta(conn)
            command.stamp(_config(conn), BASE)
            adottato = True
        command.upgrade(_config(conn), "head")
        dopo = revisione(conn)
        if verifica:
            diff = differenze(conn)
            if diff:
                raise RuntimeError(
                    "after the migration the database schema differs from the models (a revision is missing, "
                    f"or the database was changed by hand): {diff[:8]}"
                )
    if prima != dopo or adottato:
        logger.info("database schema: %s → %s%s", prima or "no revision", dopo, " (adopted from before Alembic)" if adottato else "")
    return {"prima": prima, "dopo": dopo, "adottato": adottato}


class DatabaseIndietro(RuntimeError):
    """Il database è più vecchio di questa versione del gateway."""


def controlla_revisione(eng=None) -> str | None:
    """Senza migrazione automatica: il database deve essere almeno alla revisione che
    questo codice conosce. Più nuovo va bene (rolling update, expand/contract)."""
    from app.db.session import engine

    eng = eng or engine
    with eng.connect() as conn:
        rev = revisione(conn)
    voluta = testa()
    if rev == voluta:
        return rev
    conosciute = {r.revision for r in _script().walk_revisions()}
    if rev is not None and rev not in conosciute:
        logger.warning("the database is at revision %s, newer than this gateway's (%s): "
                       "fine during a rolling update", rev, voluta)
        return rev
    raise DatabaseIndietro(
        f"the database is at revision {rev or 'none (empty, or from before Alembic)'} but this version of the gateway "
        f"needs {voluta}: run the migration step first (`python -m app.db.migra`), "
        "or, in development, DB__AUTO_MIGRATE=true"
    )


def prepara_database() -> None:
    """All'avvio del gateway: migra (sviluppo) o controlla (produzione), sotto il lucchetto."""
    from app.core.config import get_settings
    from app.db.session import lucchetto_di_avvio, wait_for_db

    wait_for_db()
    with lucchetto_di_avvio():
        if get_settings().db.auto_migrate:
            migra()
        else:
            controlla_revisione()


def nuova_revisione(messaggio: str, eng=None) -> str:
    """Genera una revisione dai modelli, confrontandoli col database (che dev'essere
    all'ultima revisione). Il file va letto e corretto prima del commit."""
    from app.db.session import engine

    esistenti = [int(m.group(1)) for f in os.listdir(os.path.join(CARTELLA, "versions"))
                 if (m := re.match(r"^(\d{4})_", f))]
    numero = f"{(max(esistenti) + 1) if esistenti else 1:04d}"
    with (eng or engine).begin() as conn:
        script = command.revision(_config(conn), message=messaggio, autogenerate=True, rev_id=numero)
    return script.path if hasattr(script, "path") else numero


def _principale(argomenti: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    from app.db.session import engine, lucchetto_di_avvio, wait_for_db

    azione = argomenti[0] if argomenti else "migra"
    wait_for_db()
    if azione == "migra":
        with lucchetto_di_avvio():
            esito = migra()
        print(f"database at revision {esito['dopo']}" + (f" (was {esito['prima'] or 'without a revision'})" if esito["prima"] != esito["dopo"] else "")
              + (", adopted from before Alembic" if esito["adottato"] else ""))
        return 0
    if azione == "stato":
        with engine.connect() as conn:
            print(f"database: {revisione(conn) or 'no revision'} · code: {testa()}")
        return 0
    if azione == "controlla":
        with engine.connect() as conn:
            diff = differenze(conn)
        for d in diff:
            print(d)
        print("schema equal to the models" if not diff else f"{len(diff)} differences")
        return 1 if diff else 0
    if azione == "nuova" and len(argomenti) > 1:
        print(nuova_revisione(argomenti[1]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_principale(sys.argv[1:]))
