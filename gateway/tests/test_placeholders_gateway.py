"""Placeholders, ranges and the «S3 folder» datasource: the gateway's part.

The grammar is the engine's (app.core.placeholders); the gateway stores what the
forms send — and only what differs from a plain datasource or Output, so the old
definitions travel exactly as before —, asks the engine's checker before saving
a datasource and before launching a run, sends {last_refresh} and the previous
snapshot to the refreshes that need them, and records what a refresh skipped.
"""
import json
from datetime import datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import select

from app.core.crypto import encrypt_secret
from app.models import Connection, Datasource, Run
from app.routes.connections import list_s3_files, list_sftp_files, preview_placeholders
from app.routes.datasources import (
    create_db_datasource,
    create_s3_datasource,
    create_sftp_datasource,
    create_sharepoint_datasource,
    refresh_datasource,
    update_datasource,
)
from app.routes.runs import _launch_flow_run, _reconcile, run_out
from app.schemas.models import (
    ApiDatasourceCreate,
    DatasourceUpdate,
    DbDatasourceCreate,
    PlaceholderPreviewQuery,
    RunCreate,
    RunDestinationSpec,
    RunEmailSpec,
    S3DatasourceCreate,
    S3FilesQuery,
    SftpDatasourceCreate,
    SftpFilesQuery,
    SharePointDatasourceCreate,
)
from app.services.flow_resolver import _output_body
from tests.conftest import make_flow, make_project, make_run, make_user

pytestmark = pytest.mark.anyio

DAYS = {"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}


class _Req:
    client = None
    headers = {}


def _conn(session, project_id, db_type, **kw):
    base = dict(name=db_type, project_id=project_id, db_type=db_type, host="h.example.com", username="u",
                password_encrypted=encrypt_secret("pw"), extra="{}")
    if db_type == "sftp":
        base.update(extra=json.dumps({"auth": "password"}), host_key_type="ssh-ed25519", host_key_fingerprint="SHA256:k")
    if db_type == "s3":
        base.update(host="http://minio:9000", username="AK", password_encrypted=encrypt_secret("S3CR3T"),
                    database="lake", db_schema="")
    if db_type == "sharepoint":
        base.update(host="https://c.sharepoint.com/sites/F", username="client", database="tenant")
    base.update(kw)
    c = Connection(**base)
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _setup(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    return admin, p


# ── SFTP and SharePoint datasources ───────────────────────────────────────────
async def test_an_sftp_datasource_with_a_range_is_checked_then_sent_whole(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "sftp")
    body = SftpDatasourceCreate(name="v", connection_id=conn.id, folder="in", pattern="s_{day:yyyyMMdd}.csv",
                                variables=[DAYS], timezone="Europe/Rome", mode="append", keys=["_param_day", "id"])
    out = await create_sftp_datasource(p.id, body, _Req(), admin, session)
    (check,) = fake_engine.sftp_inspects
    assert check["action"] == "validate" and check["source"]["variables"][0]["type"] == "range"
    (_, sent), = fake_engine.ingests
    src = sent["source"]
    assert src["timezone"] == "Europe/Rome" and src["mode"] == "append" and src["keys"] == ["_param_day", "id"]
    assert src["variables"] == [{"name": "day", "type": "range", "values": [], "start": "today-3", "end": "today",
                                 "step": "1d"}]
    assert "previous" not in sent  # the first import has none
    ref = json.loads(session.get(Datasource, out.id).source_ref)
    assert ref["pattern"] == "s_{day:yyyyMMdd}.csv" and ref["timezone"] == "Europe/Rome"


async def test_a_plain_sftp_datasource_needs_no_check_and_stores_the_old_shape(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "sftp")
    out = await create_sftp_datasource(p.id, SftpDatasourceCreate(name="v", connection_id=conn.id, pattern="*.csv"),
                                       _Req(), admin, session)
    assert fake_engine.sftp_inspects == []
    assert set(json.loads(session.get(Datasource, out.id).source_ref)) == {"folder", "pattern", "format", "sheet", "csv"}


async def test_what_the_engine_refuses_saves_nothing(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "sftp")
    fake_engine.sftp_responses["validate"] = (400, {"detail": "SFTP: the range {day} is empty"})
    with pytest.raises(HTTPException) as e:
        await create_sftp_datasource(p.id, SftpDatasourceCreate(name="v", connection_id=conn.id, pattern="{day}.csv",
                                                                variables=[DAYS]), _Req(), admin, session)
    assert e.value.status_code == 422 and "empty" in e.value.detail
    assert session.exec(select(Datasource)).all() == [] and fake_engine.ingests == []


async def test_a_refresh_sends_last_refresh_and_the_previous_snapshot(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "sftp")
    out = await create_sftp_datasource(p.id, SftpDatasourceCreate(
        name="v", connection_id=conn.id, pattern="s_{day}.csv", variables=[DAYS], mode="append", keys=["id"]),
        _Req(), admin, session)
    ds = session.get(Datasource, out.id)
    ds.key = "datasets/1/snap.parquet"
    session.add(ds)
    for r in session.exec(select(Run)).all():
        r.status, r.started_at = "SUCCESS", datetime(2026, 10, 16, 6, 0, 0)
        session.add(r)
    session.commit()
    await refresh_datasource(ds.id, _Req(), admin, session)
    _, sent = fake_engine.ingests[-1]
    assert sent["previous"] == {"bucket": ds.bucket, "key": "datasets/1/snap.parquet"}
    assert sent["last_refresh"].startswith("2026-10-16T06:00:00")


async def test_a_plain_refresh_sends_neither(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "sftp")
    out = await create_sftp_datasource(p.id, SftpDatasourceCreate(name="v", connection_id=conn.id, pattern="*.csv"),
                                       _Req(), admin, session)
    for r in session.exec(select(Run)).all():
        r.status = "SUCCESS"
        session.add(r)
    session.commit()
    await refresh_datasource(out.id, _Req(), admin, session)
    _, sent = fake_engine.ingests[-1]
    assert "last_refresh" not in sent and "previous" not in sent


async def test_a_sharepoint_datasource_with_a_dated_path(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "sharepoint")
    body = SharePointDatasourceCreate(name="c", connection_id=conn.id, path="Chiusure/{month:yyyy-MM}.xlsx", sheet="D",
                                      variables=[{"name": "month", "type": "range", "start": "start_of_month-3m",
                                                  "end": "start_of_month", "step": "1m"}])
    await create_sharepoint_datasource(p.id, body, _Req(), admin, session)
    (check,) = fake_engine.inspects
    assert check["action"] == "validate" and check["source"]["path"] == "Chiusure/{month:yyyy-MM}.xlsx"
    (route, sent), = fake_engine.ingests
    assert route == "/sharepoint/ingest" and sent["source"]["variables"][0]["name"] == "month"


async def test_check_the_files_with_a_range(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "sftp")
    await list_sftp_files(conn.id, SftpFilesQuery(folder="in", pattern="s_{day}.csv", variables=[DAYS]), _Req(),
                          admin, session)
    (sent,) = fake_engine.sftp_inspects
    assert sent["action"] == "files" and sent["source"]["variables"][0]["name"] == "day"


# ── the «S3 folder» datasource ────────────────────────────────────────────────
async def test_an_s3_datasource_is_imported_through_the_s3_connection(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "s3")
    out = await create_s3_datasource(p.id, S3DatasourceCreate(name="lake", connection_id=conn.id, folder="/exports/",
                                                              pattern="*.parquet"), _Req(), admin, session)
    assert out.source_type == "s3" and out.kind == "database"
    (route, sent), = fake_engine.ingests
    assert route == "/s3/ingest"
    assert sent["source"] == {"bucket": "", "folder": "exports", "pattern": "*.parquet", "format": "auto", "sheet": "",
                              "csv": {"separator": ",", "has_header": True, "encoding": "utf8", "null_values": []}}
    c = sent["connection"]
    assert c["endpoint_url"] == "http://minio:9000" and c["bucket"] == "lake" and "S3CR3T" not in json.dumps(sent)
    await refresh_datasource(out.id, _Req(), admin, session)
    assert [r for r, _ in fake_engine.ingests] == ["/s3/ingest", "/s3/ingest"]


async def test_an_s3_datasource_needs_an_s3_connection_and_a_bucket(session, fake_engine):
    admin, p = _setup(session)
    sftp = _conn(session, p.id, "sftp")
    with pytest.raises(HTTPException) as e:
        await create_s3_datasource(p.id, S3DatasourceCreate(name="x", connection_id=sftp.id, pattern="*.csv"), _Req(),
                                   admin, session)
    assert e.value.status_code == 422
    no_bucket = _conn(session, p.id, "s3", name="nb", database="")
    with pytest.raises(HTTPException) as e:
        await create_s3_datasource(p.id, S3DatasourceCreate(name="x", connection_id=no_bucket.id, pattern="*.csv"),
                                   _Req(), admin, session)
    assert e.value.status_code == 422 and "bucket" in e.value.detail
    out = await create_s3_datasource(p.id, S3DatasourceCreate(name="x", connection_id=no_bucket.id, bucket="b2",
                                                              pattern="*.csv"), _Req(), admin, session)
    assert json.loads(session.get(Datasource, out.id).source_ref)["bucket"] == "b2"


@pytest.mark.parametrize("kw", [dict(folder="in/.."), dict(pattern="../*.csv")])
def test_an_s3_datasource_never_goes_up(kw):
    with pytest.raises(ValidationError):
        S3DatasourceCreate(**{"name": "x", "connection_id": 1, "pattern": "*.csv", **kw})


async def test_an_s3_datasource_with_placeholders_is_checked(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "s3")
    fake_engine.s3_responses["validate"] = (400, {"detail": "S3: unknown placeholder {tomorrow}"})
    with pytest.raises(HTTPException) as e:
        await create_s3_datasource(p.id, S3DatasourceCreate(name="x", connection_id=conn.id, pattern="{tomorrow}.csv"),
                                   _Req(), admin, session)
    assert e.value.status_code == 422 and "tomorrow" in e.value.detail and fake_engine.ingests == []


async def test_an_s3_datasource_has_no_sort_keys_and_the_db_path_refuses_s3(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "s3")
    out = await create_s3_datasource(p.id, S3DatasourceCreate(name="x", connection_id=conn.id, pattern="*.csv"),
                                     _Req(), admin, session)
    with pytest.raises(HTTPException) as e:
        update_datasource(out.id, DatasourceUpdate(sort_keys=["a"]), admin, session)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await create_db_datasource(p.id, DbDatasourceCreate(name="y", connection_id=conn.id, source_type="table",
                                                            source_ref="t"), _Req(), admin, session)
    assert e.value.status_code == 422 and "S3 folder" in e.value.detail


async def test_check_the_s3_files(session, fake_engine):
    admin, p = _setup(session)
    conn = _conn(session, p.id, "s3")
    await list_s3_files(conn.id, S3FilesQuery(folder="in", pattern="*.csv"), admin, session)
    (sent,) = fake_engine.s3_inspects
    assert sent["action"] == "files" and sent["source"] == {"bucket": "", "folder": "in", "pattern": "*.csv"}


# ── the run's notes ───────────────────────────────────────────────────────────
async def test_a_refresh_that_skipped_missing_files_keeps_the_list(session, fake_engine):
    run = make_run(session, kind="ingest", datasource_id=None, status="STARTED", task_id="t-r")
    missing = [{"params": {"day": "2026-10-15"}, "pattern": "in/s_20261015.csv"}]
    fake_engine.set_task("t-r", "SUCCESS", result={"rows_written": 3, "missing": missing, "missing_count": 1,
                                                    "expansions": 3})
    run = await _reconcile(session, run)
    assert run.status == "SUCCESS"
    assert json.loads(run.notes) == {"missing": missing, "missing_count": 1, "expansions": 3}
    assert json.loads(run_out(session, run).notes)["missing_count"] == 1


async def test_a_refresh_without_missing_files_has_no_notes(session, fake_engine):
    run = make_run(session, kind="ingest", datasource_id=None, status="STARTED", task_id="t-r2")
    fake_engine.set_task("t-r2", "SUCCESS", result={"rows_written": 3, "missing": []})
    assert (await _reconcile(session, run)).notes is None


# ── Outputs ───────────────────────────────────────────────────────────────────
def _run_body(**kw) -> RunCreate:
    return RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[], **kw)


async def test_an_sftp_output_with_a_zone(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Report", project_id=p.id)
    conn = _conn(session, p.id, "sftp")
    run = await _launch_flow_run(session, admin, flow, _run_body(destination=RunDestinationSpec(
        type="sftp", connection_id=conn.id, folder="/out/{today:yyyy}", file_name="v_{today:yyyyMMdd}", format="csv",
        timezone="Europe/Rome")))
    target = fake_engine.transforms[0]["destination"]["target"]
    assert target == {"folder": "/out/{today:yyyy}", "file_name": "v_{today:yyyyMMdd}", "format": "csv",
                      "run_id": run.id, "flow": "Report", "timezone": "Europe/Rome"}
    (check,) = fake_engine.placeholder_previews
    assert check["kind"] == "output" and check["timezone"] == "Europe/Rome" and check["flow"] == "Report"
    assert check["targets"] == {"file name": "file", "folder": "path"}
    assert json.loads(run.destination)["timezone"] == "Europe/Rome"


async def test_a_template_the_engine_refuses_launches_nothing(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Report", project_id=p.id)
    conn = _conn(session, p.id, "sftp")
    fake_engine.placeholder_errors["file name"] = "unknown placeholder {week}: use {today}, …"
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, admin, flow, _run_body(destination=RunDestinationSpec(
            type="sftp", connection_id=conn.id, file_name="v_{week}", format="csv")))
    assert e.value.status_code == 422 and e.value.detail.startswith("SFTP: file name: unknown placeholder {week}")
    assert fake_engine.transforms == [] and session.exec(select(Run)).all() == []


async def test_a_folder_inside_a_placeholder_is_not_a_folder_in_the_name(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Report", project_id=p.id)
    conn = _conn(session, p.id, "sftp")
    await _launch_flow_run(session, admin, flow, _run_body(destination=RunDestinationSpec(
        type="sftp", connection_id=conn.id, file_name="v_{today:yyyy'-'MM}", format="csv")))
    assert len(fake_engine.transforms) == 1


async def test_an_s3_key_with_placeholders_gets_the_flow_the_run_and_the_zone(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Vendite", project_id=p.id)
    conn = _conn(session, p.id, "s3")
    run = await _launch_flow_run(session, admin, flow, _run_body(destination=RunDestinationSpec(
        type="s3", connection_id=conn.id, key="exp/{today:yyyy/MM}/v_{run_id}.parquet", format="parquet",
        timezone="Asia/Tokyo")))
    target = fake_engine.transforms[0]["destination"]["target"]
    assert target["run_id"] == run.id and target["flow"] == "Vendite" and target["timezone"] == "Asia/Tokyo"
    assert run.task_id == "t-transform-1"


async def test_a_plain_s3_key_travels_as_before(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Vendite", project_id=p.id)
    conn = _conn(session, p.id, "s3")
    await _launch_flow_run(session, admin, flow, _run_body(destination=RunDestinationSpec(
        type="s3", connection_id=conn.id, key="exp/v.parquet", format="parquet")))
    assert fake_engine.transforms[0]["destination"]["target"] == {"bucket": "", "key": "exp/v.parquet",
                                                                  "format": "parquet", "partition_by": []}
    assert fake_engine.placeholder_previews == []


async def test_an_email_with_a_dated_subject_and_attachment(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Vendite", project_id=p.id)
    conn = _conn(session, p.id, "smtp", host="smtp.example.com", extra=json.dumps({"from_address": "r@x.it"}))
    run = await _launch_flow_run(session, admin, flow, _run_body(email=RunEmailSpec(
        connection_id=conn.id, to=["a@x.it"], subject="Report {flow} {today:dd/MM/yyyy}", attachment_name="v_{date}",
        timezone="Europe/Rome")))
    target = fake_engine.transforms[0]["email"]["target"]
    assert target["subject"] == "Report {flow} {today:dd/MM/yyyy}" and target["flow"] == "Vendite"
    assert target["timezone"] == "Europe/Rome" and target["run_id"] == run.id
    assert fake_engine.placeholder_previews[0]["targets"] == {"subject": "text", "attachment name": "file"}


async def test_a_plain_email_travels_as_before(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Vendite", project_id=p.id)
    conn = _conn(session, p.id, "smtp", host="smtp.example.com", extra=json.dumps({"from_address": "r@x.it"}))
    await _launch_flow_run(session, admin, flow, _run_body(email=RunEmailSpec(
        connection_id=conn.id, to=["a@x.it"], subject="Report", attachment_name="vendite")))
    target = fake_engine.transforms[0]["email"]["target"]
    assert not {"flow", "run_id", "timezone"} & set(target) and fake_engine.placeholder_previews == []


def test_the_node_zone_reaches_every_part_of_the_output():
    node = {"type": "output", "data": {"destType": "email", "connectionId": 3, "emailTo": ["a@x.it"],
                                       "emailSubject": "R {today}", "timezone": "Europe/Rome"}}
    assert _output_body(node, ("b", "k"), [], "b")["email"]["timezone"] == "Europe/Rome"
    node = {"type": "output", "data": {"destType": "s3", "connectionId": 3, "s3Key": "k"}}
    assert "timezone" not in _output_body(node, ("b", "k"), [], "b")["destination"]


async def test_the_written_s3_key_is_recorded(session, fake_engine):
    summary = {"type": "s3", "connection_id": 1, "bucket": "c", "key": "exp/{today}.parquet", "format": "parquet"}
    run = make_run(session, kind="flow", flow_id=1, status="STARTED", task_id="t-s3", destination=json.dumps(summary))
    fake_engine.set_task("t-s3", "SUCCESS", result={"rows_written": 1, "destination": {"key": "exp/2026-10-18.parquet"}})
    run = await _reconcile(session, run)
    d = json.loads(run.destination)
    assert d["key"] == "exp/{today}.parquet" and d["written_key"] == "exp/2026-10-18.parquet"


# ── the preview and the API variables ─────────────────────────────────────────
async def test_the_preview_goes_to_the_engines_checker(session, fake_engine):
    user = make_user(session, email="u@x.it")
    out = await preview_placeholders(PlaceholderPreviewQuery(templates={"pattern": "s_{day}.csv"}, variables=[DAYS]),
                                     user)
    assert out["fields"]["pattern"]["value"] == "s_{day}.csv"
    assert fake_engine.placeholder_previews[0]["variables"][0]["type"] == "range"


def test_an_api_variable_may_be_a_range():
    body = ApiDatasourceCreate(name="a", connection_id=1, path="/x/{day}", variables=[DAYS])
    assert body.variables[0].type == "range"
    with pytest.raises(ValidationError):
        ApiDatasourceCreate(name="a", connection_id=1, path="/x", variables=[{"name": "d", "type": "range",
                                                                             "start": "today"}])
    with pytest.raises(ValidationError):
        ApiDatasourceCreate(name="a", connection_id=1, path="/x", variables=[{"name": "d", "values": []}])


async def test_repeated_keys_of_an_append_are_a_note_too(session, fake_engine):
    run = make_run(session, kind="ingest", datasource_id=None, status="STARTED", task_id="t-r3")
    fake_engine.set_task("t-r3", "SUCCESS", result={"rows_written": 5, "missing": [], "duplicate_keys": 2})
    run = await _reconcile(session, run)
    assert json.loads(run.notes) == {"missing": [], "missing_count": 0, "expansions": None, "duplicate_keys": 2}


async def test_a_bad_copy_key_skips_the_copy_and_says_so_but_publishes(session, fake_engine):
    """The S3 copy never fails the publish: a key the engine refuses means no copy,
    recorded on the run — the datasource is still published."""
    from app.schemas.models import PublishSpec, RunMirrorSpec

    admin, p = _setup(session)
    flow = make_flow(session, name="Vendite", project_id=p.id)
    conn = _conn(session, p.id, "s3")
    fake_engine.placeholder_errors["copy's S3 key"] = "unknown placeholder {week}: use {today}, …"
    run = await _launch_flow_run(session, admin, flow, _run_body(
        publish=PublishSpec(name="v", project_id=p.id),
        mirror=RunMirrorSpec(connection_id=conn.id, key="copia/{week}.parquet")))
    (sent,) = fake_engine.transforms
    assert sent["mirror"] is None and run.publish_name == "v"
    m = json.loads(run.mirror)
    assert m["ok"] is False and "{week}" in m["error"] and m["key"] == "copia/{week}.parquet"


async def test_the_output_zone_is_checked_at_launch_even_without_placeholders(session, fake_engine):
    admin, p = _setup(session)
    flow = make_flow(session, name="Vendite", project_id=p.id)
    conn = _conn(session, p.id, "s3")
    fake_engine.placeholder_errors["*"] = "unknown time zone 'Mars/Base': use an IANA name such as Europe/Rome or UTC"
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, admin, flow, _run_body(destination=RunDestinationSpec(
            type="s3", connection_id=conn.id, key="exp/v.parquet", format="parquet", timezone="Mars/Base")))
    assert e.value.status_code == 422 and "Mars/Base" in e.value.detail and fake_engine.transforms == []
    assert fake_engine.placeholder_previews[0]["timezone"] == "Mars/Base"


async def test_a_copy_with_an_empty_key_is_skipped_and_said_not_the_publish_refused(session, fake_engine):
    """A scheduled run of an Output whose copy is on without a key: the datasource is
    published, the copy skipped and recorded (a copy never fails the publish)."""
    from app.schemas.models import PublishSpec, RunMirrorSpec

    admin, p = _setup(session)
    flow = make_flow(session, name="Vendite", project_id=p.id)
    conn = _conn(session, p.id, "s3")
    run = await _launch_flow_run(session, admin, flow, _run_body(
        publish=PublishSpec(name="v", project_id=p.id), mirror=RunMirrorSpec(connection_id=conn.id, key="  ")))
    (sent,) = fake_engine.transforms
    assert sent["mirror"] is None and run.publish_name == "v"
    m = json.loads(run.mirror)
    assert m["ok"] is False and "empty" in m["error"]
