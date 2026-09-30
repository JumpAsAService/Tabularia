"""`/tasks/transform-data` è un run: chiede RUN, non solo VIEW.

Trovato con le credenziali dell'ospite della demo (audit 2026-09-30): un
osservatore di sola lettura eseguiva un run completo e scriveva un parquet,
senza riga Run e senza audit, per una strada che il percorso ufficiale nega.
"""
import pytest
from fastapi import HTTPException

from app.models import Datasource, Permission, Upload
from app.models.permission import Capability
from app.services.objects import ensure_can_read_keys, ensure_can_run_keys
from tests.conftest import make_project, make_user


def _datasource(session, progetto, chiave="datasets/1/abc.parquet"):
    ds = Datasource(name="d", project_id=progetto.id, bucket="b", key=chiave)
    session.add(ds)
    session.commit()
    return ds


def test_view_only_can_read_but_not_run(session):
    """Il cuore del rilievo: leggere sì, eseguire no."""
    p = make_project(session, name="Marketing")
    ospite = make_user(session, email="ospite@x.it")
    ospite.is_observer = True
    session.add(Permission(project_id=p.id, user_id=ospite.id, capability=Capability.VIEW))
    session.commit()
    ds = _datasource(session, p)

    ensure_can_read_keys(session, ospite, {ds.key})            # l'anteprima resta sua
    with pytest.raises(HTTPException) as e:
        ensure_can_run_keys(session, ospite, {ds.key})
    assert e.value.status_code == 403
    assert "run" in e.value.detail.lower()


def test_run_on_the_project_is_enough(session):
    p = make_project(session, name="Marketing")
    tizio = make_user(session, email="tizio@x.it")
    session.add(Permission(project_id=p.id, user_id=tizio.id, capability=Capability.RUN))
    session.commit()
    ds = _datasource(session, p)
    ensure_can_run_keys(session, tizio, {ds.key})  # non solleva


def test_run_elsewhere_does_not_carry_over(session):
    """RUN su un progetto non dà RUN sui dati di un altro."""
    mio, altro = make_project(session, name="Mio"), make_project(session, name="Altro")
    tizio = make_user(session, email="tizio2@x.it")
    session.add(Permission(project_id=mio.id, user_id=tizio.id, capability=Capability.RUN))
    session.add(Permission(project_id=altro.id, user_id=tizio.id, capability=Capability.VIEW))
    session.commit()
    ds = _datasource(session, altro, "datasets/2/def.parquet")
    with pytest.raises(HTTPException):
        ensure_can_run_keys(session, tizio, {ds.key})


def test_own_upload_can_be_run(session):
    """Chi ha caricato un file lo può trasformare: è la strada dell'editor su un
    upload non ancora in un flusso."""
    tizio = make_user(session, email="tizio3@x.it")
    session.add(Upload(owner_id=tizio.id, dataset_id="mio", bucket="b", parquet_key="raw/mio.parquet"))
    session.commit()
    ensure_can_run_keys(session, tizio, {"raw/mio.parquet"})


def test_an_administrator_runs_anywhere(session):
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    ensure_can_run_keys(session, capo, {"datasets/9/qualunque.parquet"})


def test_no_keys_means_nothing_to_check(session):
    tizio = make_user(session, email="tizio4@x.it")
    ensure_can_run_keys(session, tizio, set())
