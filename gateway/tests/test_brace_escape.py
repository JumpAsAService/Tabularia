"""Revision 0013: the braces of the definitions stored before the placeholder
language keep their meaning.

Before it, braces were literal text in the email subject and attachment name, the
S3 Output's key and its copy's key, the SFTP Output's folder, and the folder,
pattern and path of the SFTP and SharePoint datasources; the SFTP Output's file
name knew only {date}, {datetime}, {run_id} and {flow}. The migration writes those
braces as `{{` `}}`, so every stored definition resolves to the same text as
before. Definitions shaped as the forms store them today.
"""
import json

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db import brace_escape as be


@pytest.mark.parametrize("text, expected", [
    ("Vendite {Nord}", "Vendite {{Nord}}"),
    ("report{1}.csv", "report{{1}}.csv"),
    ("a { b", "a {{ b"),
    ("plain", "plain"),
    # every brace was literal: each one doubled, adjacent ones too
    ("Report {{ month }}", "Report {{{{ month }}}}"),
    ("{date}}", "{{date}}}}"),
    ("{{date}", "{{{{date}}"),
    ("{{{", "{{{{{{"),
    ("}}}", "}}}}}}"),
    ("{{x}}}", "{{{{x}}}}}}"),
])
def test_every_literal_brace_is_doubled(text, expected):
    assert be.escape_literal(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("vendite_{date}_{run_id}", "vendite_{date}_{run_id}"),
    ("{flow}_{datetime}.csv", "{flow}_{datetime}.csv"),
    ("x_{week}_{date}", "x_{{week}}_{date}"),
    ("a{b", "a{{b"),
    # as the old code read them (re.sub of \{([^{}]*)\}): `{{date}}` was a brace,
    # the date, a brace; `{date}}` the date and a brace
    ("{{date}}", "{{{date}}}"),
    ("{date}}", "{date}}}"),
    ("{{date}", "{{{date}"),
    ("}{run_id}{", "}}{run_id}{{"),
])
def test_the_sftp_file_name_keeps_its_four_old_placeholders(text, expected):
    assert be.escape_sftp_file_name(text) == expected


def _definition():
    return {"nodes": [
        {"id": "s", "type": "source", "data": {"datasourceId": 1, "label": "{not touched}"}},
        {"id": "e", "type": "output", "data": {"destType": "email", "emailSubject": "Vendite {Nord}",
                                               "attachmentName": "rep{1}", "emailBody": "<style>p {color: red}</style>"}},
        {"id": "s3", "type": "output", "data": {"destType": "s3", "s3Key": "a/{x}.parquet"}},
        {"id": "d", "type": "output", "data": {"destType": "datasource", "name": "{n}", "mirrorEnabled": True,
                                               "mirrorKey": "copia/{y}.parquet"}},
        {"id": "f", "type": "output", "data": {"destType": "sftp", "sftpFolder": "/out/{z}", "sftpFileName": "v_{date}_{q}"}},
        {"id": "t", "type": "output", "data": {"destType": "database", "table": "t{1}"}},
    ], "edges": []}


def test_a_flow_definition():
    out = be.escape_flow_definition(_definition())
    data = {n["id"]: n["data"] for n in out["nodes"]}
    assert data["e"]["emailSubject"] == "Vendite {{Nord}}" and data["e"]["attachmentName"] == "rep{{1}}"
    assert data["e"]["emailBody"] == "<style>p {color: red}</style>"  # the body is not a template
    assert data["s3"]["s3Key"] == "a/{{x}}.parquet" and data["d"]["mirrorKey"] == "copia/{{y}}.parquet"
    assert data["d"]["name"] == "{n}" and data["t"]["table"] == "t{1}" and data["s"]["label"] == "{not touched}"
    assert data["f"]["sftpFolder"] == "/out/{{z}}" and data["f"]["sftpFileName"] == "v_{date}_{{q}}"


def test_datasource_definitions():
    assert be.escape_source_ref("sftp", {"folder": "in/{a}", "pattern": "x{1}.csv", "format": "auto"}) == \
        {"folder": "in/{{a}}", "pattern": "x{{1}}.csv", "format": "auto"}
    assert be.escape_source_ref("sharepoint", {"path": "B/{x}.xlsx", "sheet": "{s}"}) == {"path": "B/{{x}}.xlsx", "sheet": "{s}"}
    api = {"path": "/q/{today}"}
    assert be.escape_source_ref("api", api) == api  # already a template


def test_the_migration_on_stored_rows():
    engine = sa.create_engine("sqlite://")
    meta = sa.MetaData()
    flows = sa.Table("flows", meta, sa.Column("id", sa.Integer, primary_key=True), sa.Column("definition", sa.Text))
    versions = sa.Table("flow_versions", meta, sa.Column("id", sa.Integer, primary_key=True), sa.Column("definition", sa.Text))
    datasources = sa.Table("datasources", meta, sa.Column("id", sa.Integer, primary_key=True),
                           sa.Column("source_type", sa.String), sa.Column("source_ref", sa.Text))
    meta.create_all(engine)
    plain = json.dumps({"nodes": [{"id": "o", "type": "output", "data": {"destType": "s3", "s3Key": "k.parquet"}}], "edges": []})
    with engine.begin() as c:
        c.execute(flows.insert(), [{"id": 1, "definition": json.dumps(_definition())}, {"id": 2, "definition": plain},
                                   {"id": 3, "definition": "not json"}])
        c.execute(versions.insert(), [{"id": 1, "definition": json.dumps(_definition())}])
        c.execute(datasources.insert(), [
            {"id": 1, "source_type": "sftp", "source_ref": json.dumps({"folder": "{a}", "pattern": "*.csv"})},
            {"id": 2, "source_type": "table", "source_ref": "public.{odd}"},
            {"id": 3, "source_type": "sharepoint", "source_ref": json.dumps({"path": "B/*.xlsx", "sheet": "D"})},
            {"id": 4, "source_type": "sftp", "source_ref": None},
            {"id": 5, "source_type": "sftp", "source_ref": ""},
            {"id": 6, "source_type": "sharepoint", "source_ref": "{not json"}])
    import importlib

    mig = importlib.import_module("app.db.migrations.versions.0013_escape_literal_braces")
    with engine.begin() as c:
        ctx = MigrationContext.configure(c)
        with Operations.context(ctx):
            mig.upgrade()
    with engine.connect() as c:
        rows = dict(c.execute(sa.select(flows.c.id, flows.c.definition)).all())
        vers = dict(c.execute(sa.select(versions.c.id, versions.c.definition)).all())
        refs = dict(c.execute(sa.select(datasources.c.id, datasources.c.source_ref)).all())
    nodes = {n["id"]: n["data"] for n in json.loads(rows[1])["nodes"]}
    assert nodes["e"]["emailSubject"] == "Vendite {{Nord}}" and nodes["f"]["sftpFileName"] == "v_{date}_{{q}}"
    assert rows[2] == plain and rows[3] == "not json"  # untouched: byte for byte
    assert json.loads(vers[1])["nodes"][1]["data"]["emailSubject"] == "Vendite {{Nord}}"
    assert json.loads(refs[1])["folder"] == "{{a}}" and refs[2] == "public.{odd}"
    assert refs[3] == json.dumps({"path": "B/*.xlsx", "sheet": "D"})
    assert refs[4] is None and refs[5] == "" and refs[6] == "{not json"  # skipped, as the flows
