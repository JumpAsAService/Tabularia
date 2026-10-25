"""`/tasks/transform-data` is a run: it asks for RUN, not only VIEW.

Found with the demo guest's credentials (audit 2026-09-30): a read-only observer ran a
whole run and wrote a parquet, without a Run row and without audit, along a way the
official path denies.
"""
import pytest
from fastapi import HTTPException

from app.models import Datasource, Permission, Upload
from app.models.permission import Capability
from app.services.objects import ensure_can_read_keys, ensure_can_run_keys
from tests.conftest import make_project, make_user


def _datasource(session, project, key="datasets/1/abc.parquet"):
    ds = Datasource(name="d", project_id=project.id, bucket="b", key=key)
    session.add(ds)
    session.commit()
    return ds


def test_view_only_can_read_but_not_run(session):
    """The heart of the finding: reading yes, running no."""
    p = make_project(session, name="Marketing")
    guest = make_user(session, email="ospite@x.it")
    guest.is_observer = True
    session.add(Permission(project_id=p.id, user_id=guest.id, capability=Capability.VIEW))
    session.commit()
    ds = _datasource(session, p)

    ensure_can_read_keys(session, guest, {ds.key})            # the preview stays theirs
    with pytest.raises(HTTPException) as e:
        ensure_can_run_keys(session, guest, {ds.key})
    assert e.value.status_code == 403
    assert "run" in e.value.detail.lower()


def test_run_on_the_project_is_enough(session):
    p = make_project(session, name="Marketing")
    someone = make_user(session, email="tizio@x.it")
    session.add(Permission(project_id=p.id, user_id=someone.id, capability=Capability.RUN))
    session.commit()
    ds = _datasource(session, p)
    ensure_can_run_keys(session, someone, {ds.key})  # does not raise


def test_run_elsewhere_does_not_carry_over(session):
    """RUN on one project does not give RUN on another's data."""
    mine, other = make_project(session, name="Mio"), make_project(session, name="Altro")
    someone = make_user(session, email="tizio2@x.it")
    session.add(Permission(project_id=mine.id, user_id=someone.id, capability=Capability.RUN))
    session.add(Permission(project_id=other.id, user_id=someone.id, capability=Capability.VIEW))
    session.commit()
    ds = _datasource(session, other, "datasets/2/def.parquet")
    with pytest.raises(HTTPException):
        ensure_can_run_keys(session, someone, {ds.key})


def test_own_upload_can_be_run(session):
    """Whoever uploaded a file may transform it: it is the editor's way on an upload not in a
    flow yet."""
    someone = make_user(session, email="tizio3@x.it")
    session.add(Upload(owner_id=someone.id, dataset_id="mio", bucket="b", parquet_key="raw/mio.parquet"))
    session.commit()
    ensure_can_run_keys(session, someone, {"raw/mio.parquet"})


def test_an_administrator_runs_anywhere(session):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    ensure_can_run_keys(session, boss, {"datasets/9/qualunque.parquet"})


def test_no_keys_means_nothing_to_check(session):
    someone = make_user(session, email="tizio4@x.it")
    ensure_can_run_keys(session, someone, set())
