"""The engine's routes of the file datasources reached by the gateway: `validate`
(placeholders, variables, keys — no server involved) answers or refuses with a
400 and the datasource's prefix; the ingest sends the newer kwargs only when set
(a worker of the previous version must still take the task)."""
from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from app.api.routes import s3_source as s3_route
from app.api.routes import sftp as sftp_route
from app.api.routes import sharepoint as sp_route

DAYS = {"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}
SFTP_CONN = {"host": "h", "username": "u"}
S3_CONN = {"endpoint_url": "http://minio:9000", "access_key": "a", "secret_key": "s", "bucket": "b"}
SP_CONN = {"tenant_id": "t", "client_id": "c", "client_secret_encrypted": "x", "site_url": "https://c.sharepoint.com/sites/F"}


def test_sftp_validate():
    ok = sftp_route.inspect(sftp_route.SftpInspectRequest(
        connection=SFTP_CONN, action="validate", source={"folder": "in", "pattern": "s_{day}.csv", "variables": [DAYS]}))
    assert ok["expansions"] == 3
    with pytest.raises(HTTPException) as e:
        sftp_route.inspect(sftp_route.SftpInspectRequest(connection=SFTP_CONN, action="validate",
                                                         source={"pattern": "s_{tomorrow}.csv"}))
    assert e.value.status_code == 400 and e.value.detail.startswith("SFTP: ")


def test_s3_validate():
    ok = s3_route.inspect(s3_route.S3InspectRequest(connection=S3_CONN, action="validate",
                                                    source={"folder": "in/{day}", "pattern": "*.csv", "variables": [DAYS]}))
    assert ok["first"]["folder"].startswith("in/")
    with pytest.raises(HTTPException) as e:
        s3_route.inspect(s3_route.S3InspectRequest(connection=S3_CONN, action="validate",
                                                   source={"folder": "in/..", "pattern": "*.csv"}))
    assert e.value.status_code == 400 and e.value.detail.startswith("S3: ")


def test_sharepoint_validate():
    ok = sp_route.inspect(sp_route.SharePointInspectRequest(connection=SP_CONN, action="validate",
                                                            source={"path": "C/{today}.xlsx", "sheet": "D"}))
    assert ok["expansions"] == 1
    with pytest.raises(HTTPException) as e:
        sp_route.inspect(sp_route.SharePointInspectRequest(connection=SP_CONN, action="validate",
                                                           source={"path": "C/*.xlsx", "sheet": "D", "variables": [DAYS]}))
    assert e.value.status_code == 400 and "not used" in e.value.detail


class _Spy:
    def __init__(self):
        self.kwargs = None

    def delay(self, **kw):
        self.kwargs = kw
        return type("R", (), {"id": "t"})()


def _yesterdays_worker(connection, source, bucket, output_key, contract=None):
    """The signature of the file ingests before last_refresh/previous."""


@pytest.mark.parametrize("route, name, request_cls, conn, source", [
    (sftp_route, "ingest_sftp_task", sftp_route.SftpIngestRequest, SFTP_CONN, {"pattern": "*.csv"}),
    (sp_route, "ingest_sharepoint_task", sp_route.SharePointIngestRequest, SP_CONN, {"path": "a.xlsx", "sheet": "D"}),
])
def test_a_plain_ingest_still_binds_to_the_previous_worker(monkeypatch, route, name, request_cls, conn, source):
    from app.tasks import jobs

    spy = _Spy()
    monkeypatch.setattr(jobs, name, spy)
    route.ingest(request_cls(connection=conn, source=source, output_key="k"))
    inspect.signature(_yesterdays_worker).bind(**spy.kwargs)
    route.ingest(request_cls(connection=conn, source=source, output_key="k", last_refresh="2026-10-01T00:00:00Z",
                             previous={"bucket": "b", "key": "p"}))
    assert spy.kwargs["last_refresh"] and spy.kwargs["previous"] == {"bucket": "b", "key": "p"}
