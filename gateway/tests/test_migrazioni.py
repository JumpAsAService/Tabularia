"""Le migrazioni dello schema (Alembic), su un Postgres VERO: i test del gateway
girano su SQLite con create_all, ma le migrazioni sono DDL di Postgres. Ogni test
crea un database temporaneo sul Postgres configurato (DB__*) e lo toglie alla fine;
senza un Postgres raggiungibile i test si saltano.

Quello che si prova (docs/database-migrations.md):
· un database nuovo, migrato, è uguale ai modelli — se un modello cambia senza la
  sua revisione, questo test fallisce (sostituisce la regola a mano «ogni colonna
  nuova vuole il suo ALTER»);
· un database di PRIMA di Alembic (lo schema vero del database di sviluppo, con la
  deriva che lasciava il vecchio meccanismo) si adotta e diventa uguale ai modelli;
· una differenza fra database e modelli ferma il passo, e non lascia mezze modifiche;
· senza migrazione automatica il gateway si ferma su un database indietro, non su
  uno più nuovo (rolling update);
· le revisioni sono una catena sola."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db import migra as m

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "schema_prima_di_alembic.sql")


def _url_base():
    url = make_url(get_settings().db.dsn)
    return url if url.drivername.startswith("postgresql") else None


@pytest.fixture
def database_temporaneo():
    """Un database vuoto sul Postgres configurato; via alla fine."""
    base = _url_base()
    if base is None:
        pytest.skip("serve un Postgres (DB__*)")
    amministrazione = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = f"zzmig_{uuid.uuid4().hex[:10]}"
    try:
        with amministrazione.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:  # noqa: BLE001 — niente Postgres raggiungibile: si salta
        pytest.skip(f"Postgres non raggiungibile ({e.__class__.__name__})")
    eng = create_engine(base.set(database=name))
    try:
        yield eng
    finally:
        eng.dispose()
        with amministrazione.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        amministrazione.dispose()


def test_un_database_nuovo_migrato_e_uguale_ai_modelli(database_temporaneo):
    outcome = m.migra(database_temporaneo)
    assert outcome == {"prima": None, "dopo": m.head(), "adottato": False}
    with database_temporaneo.connect() as c:
        assert m.differences(c) == []
        assert m.revision(c) == m.head()
    # di nuovo: niente da fare
    assert m.migra(database_temporaneo) == {"prima": m.head(), "dopo": m.head(), "adottato": False}


def test_un_database_di_prima_di_alembic_si_adotta_e_diventa_uguale_ai_modelli(database_temporaneo):
    with database_temporaneo.begin() as c:
        c.exec_driver_sql(open(FIXTURE).read())
    with database_temporaneo.connect() as c:
        assert m.revision(c) is None and m.differences(c)          # la deriva del vecchio meccanismo c'è
    outcome = m.migra(database_temporaneo)
    assert outcome["adottato"] and outcome["prima"] is None and outcome["dopo"] == m.head()
    with database_temporaneo.connect() as c:
        assert m.differences(c) == []
        insp = inspect(c)
        assert not insp.has_table("schema_state")                   # l'impronta del vecchio meccanismo se ne va
        fk = {f["name"] for f in insp.get_foreign_keys("flows")}
        assert "flows_notify_connection_id_fkey" in fk              # la chiave esterna che l'ALTER non metteva
        assert "uq_users_oidc_identity" in {i["name"] for i in insp.get_indexes("users")}
    assert m.migra(database_temporaneo)["adottato"] is False        # adottato una volta sola


def test_una_differenza_coi_modelli_ferma_il_passo_senza_mezze_modifiche(database_temporaneo):
    m.migra(database_temporaneo)
    with database_temporaneo.begin() as c:
        c.execute(text("ALTER TABLE users ADD COLUMN zz_a_mano INTEGER"))      # toccato a mano, o un modello senza revisione
    with database_temporaneo.connect() as c:
        assert any("zz_a_mano" in str(d) for d in m.differences(c))
    with pytest.raises(RuntimeError, match="differs from the models"):
        m.migra(database_temporaneo)
    # un database di prima di Alembic che non torna: l'adozione si annulla tutta.
    # «Di prima di Alembic» = lo schema della BASE, senza tabelle né colonne delle
    # revisioni successive: lo si costruisce così, non togliendo pezzi allo schema
    # di oggi (che a ogni revisione nuova lascerebbe indietro qualcosa)
    with database_temporaneo.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    with database_temporaneo.begin() as c:
        m.command.upgrade(m._config(c), m.BASE)
        c.execute(text("DROP TABLE alembic_version"))
        c.execute(text("ALTER TABLE users ADD COLUMN zz_a_mano INTEGER"))
    with pytest.raises(RuntimeError, match="differs from the models"):
        m.migra(database_temporaneo)
    with database_temporaneo.connect() as c:
        assert m.revision(c) is None and not inspect(c).has_table("alembic_version")
        # niente a metà: nemmeno le tabelle delle revisioni dopo la base
        assert not inspect(c).has_table("user_sessions")


def test_senza_migrazione_automatica_il_gateway_si_ferma_solo_se_il_database_e_indietro(database_temporaneo):
    with pytest.raises(m.DatabaseBehind, match="python -m app.db.migra"):
        m.check_revision(database_temporaneo)                 # vuoto: indietro
    m.migra(database_temporaneo)
    assert m.check_revision(database_temporaneo) == m.head()
    with database_temporaneo.begin() as c:                          # una revisione che questo codice non conosce: più nuova
        c.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    assert m.check_revision(database_temporaneo) == "9999"


def test_le_revisioni_sono_una_catena_sola_numerata():
    script = m._script()
    assert len(script.get_heads()) == 1
    revisioni = list(script.walk_revisions())
    numeri = sorted(int(r.revision) for r in revisioni)
    assert numeri == list(range(1, len(numeri) + 1)) and all(len(r.revision) == 4 for r in revisioni)
    assert m.BASE == "0001" and script.get_revision(m.BASE).down_revision is None


def test_un_database_piu_nuovo_del_codice_non_ferma_il_passo(database_temporaneo, monkeypatch):
    """The previous release after a `helm rollback`: its `migrate` step found a revision it
    does not know and stopped every new pod (Init:CrashLoopBackOff), so the rollback the
    upgrade guide promises never completed (tried on kind, 1.2.2 → 1.2.1, 2026-10-23)."""
    m.migra(database_temporaneo)
    with database_temporaneo.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    assert m.migra(database_temporaneo) == {"prima": "9999", "dopo": "9999", "adottato": False}
    assert m.check_revision(database_temporaneo) == "9999"
