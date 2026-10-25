"""A copy of the output to an external S3, AS WELL AS the published datasource.

The copy is BEST EFFORT by explicit choice: the datasource is the primary result, the
copy is a delivery downstream (data engineering reading the agreed prefix). Its
failure — expired credentials, full bucket, network — must not undo a successful run
nor prevent the publishing.

Here the part that lives in the gateway is checked:
  1. the Output node of kind "datasource" emits `mirror` NEXT TO `publish` (not in
     its place: `destination` stays free, so the S3 path where the writing IS the
     result keeps failing hard);
  2. the outcome the worker reports is recorded on the run — otherwise a failure
     would stay only in the logs, invisible in the history.
"""
import json

import pytest

from app.routes.runs import _reconcile
from app.services.flow_resolver import _output_body
from tests.conftest import make_run

pytestmark = pytest.mark.anyio


def _node(**data) -> dict:
    base = {"type": "output", "data": {"destType": "datasource", "name": "vendite", "projectId": 1}}
    base["data"].update(data)
    return base


# ── resolving the Output node ────────────────────────────────────────────────
def test_mirror_lives_alongside_publish():
    """The copy is ADDED to the datasource: both fields in the run body."""
    body = _output_body(
        _node(mirrorEnabled=True, mirrorConnectionId=7, mirrorBucket="lake",
              mirrorKey="published/vendite/latest.parquet"),
        ("data-prep", "datasets/in.parquet"), [], "data-prep",
    )
    assert body["publish"]["name"] == "vendite"  # the primary result stays
    # no `format`: the copy is always parquet, the choice does not exist
    assert body["mirror"] == {
        "connection_id": 7,
        "bucket": "lake",
        "key": "published/vendite/latest.parquet",
    }
    # `destination` is NOT used: there an error would be fatal, here it must not be
    assert "destination" not in body


def test_mirror_missing_when_not_asked():
    body = _output_body(_node(), ("data-prep", "datasets/in.parquet"), [], "data-prep")
    assert "mirror" not in body and body["publish"]["name"] == "vendite"


def test_mirror_ignored_without_connection():
    """Tick on but connection not chosen: ignored instead of launching a run that would fail
    the validation downstream."""
    body = _output_body(
        _node(mirrorEnabled=True, mirrorKey="published/x.parquet"),
        ("data-prep", "datasets/in.parquet"), [], "data-prep",
    )
    assert "mirror" not in body


def test_mirror_does_not_touch_the_other_output_kinds():
    """On an Output of kind s3 the writing IS the result: no mirror, and the destination
    stays the usual one (a binding failure)."""
    body = _output_body(
        {"type": "output", "data": {"destType": "s3", "connectionId": 3, "s3Key": "out/x.parquet",
                                    "mirrorEnabled": True, "mirrorConnectionId": 9}},
        ("data-prep", "datasets/in.parquet"), [], "data-prep",
    )
    assert body["destination"]["type"] == "s3"
    assert "mirror" not in body and "publish" not in body


# ── recording the outcome on the run ─────────────────────────────────────────
async def test_failed_copy_does_not_fail_the_run(session, fake_engine):
    """The heart of the requirement: the run first, then the copy. When the copy fails the
    run stays SUCCESS and the error is readable in the history."""
    run = make_run(session, kind="flow", status="STARTED", task_id="t-mirror-ko", flow_id=1)
    fake_engine.set_task(
        "t-mirror-ko", "SUCCESS",
        result={
            "rows_written": 42,
            "mirror": {"ok": False, "bucket": "lake", "key": "published/v.parquet",
                       "error": "EndpointConnectionError: impossibile raggiungere l'endpoint"},
        },
    )

    await _reconcile(session, run)
    session.refresh(run)

    assert run.status == "SUCCESS"  # the primary result is NOT compromised
    assert run.rows_written == 42
    outcome = json.loads(run.mirror)
    assert outcome["ok"] is False
    assert "EndpointConnectionError" in outcome["error"]


async def test_successful_copy_recorded(session, fake_engine):
    run = make_run(session, kind="flow", status="STARTED", task_id="t-mirror-ok", flow_id=1)
    fake_engine.set_task(
        "t-mirror-ok", "SUCCESS",
        result={"rows_written": 7,
                "mirror": {"ok": True, "bucket": "lake", "key": "published/v.parquet", "files": 1}},
    )

    await _reconcile(session, run)
    session.refresh(run)

    assert run.status == "SUCCESS"
    assert json.loads(run.mirror)["ok"] is True


async def test_run_without_copy_leaves_the_field_empty(session, fake_engine):
    """No copy asked: the column stays NULL, not a fake JSON."""
    run = make_run(session, kind="flow", status="STARTED", task_id="t-no-mirror", flow_id=1)
    fake_engine.set_task("t-no-mirror", "SUCCESS", result={"rows_written": 3})

    await _reconcile(session, run)
    session.refresh(run)

    assert run.status == "SUCCESS" and run.mirror is None
