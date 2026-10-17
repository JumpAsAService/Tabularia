"""Proof of revision 0013 on a COPY of the dev database: definitions shaped as they
were stored before the placeholder language resolve, after the migration, to the
same bytes as the code of before produced.

    # a copy of the dev DB, put back at 0012
    docker exec dataprep-postgres sh -c 'createdb -U tabularia zzph_mig && pg_dump -U tabularia tabularia | psql -q -U tabularia zzph_mig'
    docker exec dataprep-postgres psql -U tabularia zzph_mig -c "update alembic_version set version_num='0012'"
    docker cp e2e/placeholders/migration_proof.py dataprep-gateway:/tmp/
    docker exec -e DB__NAME=zzph_mig dataprep-gateway python /tmp/migration_proof.py seed     # legacy rows → /tmp/zz_mig_before.json
    docker exec -e DB__NAME=zzph_mig dataprep-gateway python -m app.db.migra                 # 0012 → 0013
    docker exec -e DB__NAME=zzph_mig dataprep-gateway python /tmp/migration_proof.py dump     # → /tmp/zz_mig_after.json
    # compare in the backend container, with the SFTP module of before (git show 981396d:backend/app/ingest/sftp.py)
    docker exec dataprep-backend python /tmp/migration_proof.py compare /tmp/zz_mig_before.json /tmp/zz_mig_after.json /tmp/old_sftp.py
    docker exec dataprep-postgres dropdb -U tabularia zzph_mig

Every brace of the fields that were literal before is in the corpus, adjacent ones too.
"""
from __future__ import annotations

import json
import sys

LITERAL = ["Vendite {Nord}", "Report {{ month }}", "{date}}", "{{date}", "{{{", "}}}", "{{x}}}", "a}b{c", "plain", "{}"]
SFTP_NAMES = ["v_{date}_{run_id}", "{{date}}", "{date}}", "{{date}", "}{run_id}{", "x{flow}y{datetime}", "{{", "plain"]
TAG = "zz-mig-proof"


def seed() -> None:
    sys.path.insert(0, "/app")
    from sqlmodel import Session, select

    from app.db.session import engine
    from app.models import Datasource, Flow, Project

    nodes = []
    for i, t in enumerate(LITERAL):
        nodes += [
            {"id": f"e{i}", "type": "output", "data": {"destType": "email", "emailSubject": t, "attachmentName": t.replace("/", "_")}},
            {"id": f"s{i}", "type": "output", "data": {"destType": "s3", "s3Key": f"exp/{t}/k.parquet"}},
            {"id": f"m{i}", "type": "output", "data": {"destType": "datasource", "name": "x", "mirrorEnabled": True, "mirrorKey": f"copy/{t}.parquet"}},
            {"id": f"f{i}", "type": "output", "data": {"destType": "sftp", "sftpFolder": f"/out/{t}", "sftpFileName": "n"}},
        ]
    for i, t in enumerate(SFTP_NAMES):
        nodes.append({"id": f"n{i}", "type": "output", "data": {"destType": "sftp", "sftpFolder": "/out", "sftpFileName": t}})
    with Session(engine) as s:
        p = Project(name=TAG)
        s.add(p)
        s.commit()
        s.refresh(p)
        flow = Flow(name="Report mensile", project_id=p.id, definition=json.dumps({"nodes": nodes, "edges": []}, separators=(",", ":")))
        s.add(flow)
        refs = [("sftp", {"folder": f"in/{t}", "pattern": f"x{t}.csv", "format": "auto"}) for t in LITERAL]
        refs += [("sharepoint", {"path": f"B/{t}/f.xlsx", "sheet": "D"}) for t in LITERAL]
        dss = [Datasource(name=f"{TAG}-{i}", project_id=p.id, bucket="b", key="", kind="database", source_type=st,
                          source_ref=json.dumps(r)) for i, (st, r) in enumerate(refs)]
        dss += [Datasource(name=f"{TAG}-null", project_id=p.id, bucket="b", key="", kind="database", source_type="sftp", source_ref=None),
                Datasource(name=f"{TAG}-empty", project_id=p.id, bucket="b", key="", kind="database", source_type="sftp", source_ref="")]
        for d in dss:
            s.add(d)
        s.commit()
    dump("/tmp/zz_mig_before.json")


def dump(path: str = "/tmp/zz_mig_after.json") -> None:
    sys.path.insert(0, "/app")
    from sqlmodel import Session, select

    from app.db.session import engine
    from app.models import Datasource, Flow, Project

    with Session(engine) as s:
        p = s.exec(select(Project).where(Project.name == TAG)).first()
        flow = s.exec(select(Flow).where(Flow.project_id == p.id)).first()
        dss = s.exec(select(Datasource).where(Datasource.project_id == p.id)).all()
        json.dump({"flow": flow.definition, "datasources": {d.name: [d.source_type, d.source_ref] for d in dss}}, open(path, "w"))
    print("dumped", path)


def compare(before_path: str, after_path: str, old_sftp_path: str) -> int:
    import importlib.util
    from datetime import datetime, timezone

    sys.path.insert(0, "/app")
    from app.core import placeholders as ph
    from app.ingest import email_destination as em
    from app.ingest import file_templates as ft
    from app.ingest import sftp
    from app.ingest.s3_destination import S3DestinationSpec, render_key
    from app.ingest.sftp import SftpError

    spec = importlib.util.spec_from_file_location("old_sftp", old_sftp_path)
    old_sftp = importlib.util.module_from_spec(spec)
    sys.modules["old_sftp"] = old_sftp  # its dataclasses look themselves up there
    spec.loader.exec_module(old_sftp)
    now = datetime(2026, 10, 17, 14, 30, 5, tzinfo=timezone.utc)
    before, after = json.load(open(before_path)), json.load(open(after_path))
    old_nodes = {n["id"]: n["data"] for n in json.loads(before["flow"])["nodes"]}
    new_nodes = {n["id"]: n["data"] for n in json.loads(after["flow"])["nodes"]}
    ctx = ph.output_context(now, "UTC", 7, "Report mensile")
    ok = bad = 0

    def same(what, old, new):
        nonlocal ok, bad
        if old == new:
            ok += 1
        else:
            bad += 1
            print(f"  KO  {what}: before {old!r}, after {new!r}")

    for nid, old in old_nodes.items():
        new = new_nodes[nid]
        if old["destType"] == "email":
            same(f"{nid} subject", old["emailSubject"], ph.render_output(new["emailSubject"], ctx, "text", "subject"))
            same(f"{nid} attachment", em._filename(em.EmailSpec(attachment_name=old["attachmentName"])),
                 em._filename(em.resolve_names(em.EmailSpec(attachment_name=new["attachmentName"], flow="Report mensile",
                                                            run_id=7), now)))
        elif old["destType"] == "s3":
            same(f"{nid} key", old["s3Key"].strip().strip("/"), render_key(S3DestinationSpec(key=new["s3Key"], run_id=7,
                                                                                              flow="Report mensile"), now))
        elif old["destType"] == "datasource":
            same(f"{nid} copy key", old["mirrorKey"], render_key(S3DestinationSpec(key=new["mirrorKey"]), now))
        elif old["sftpFileName"] == "n":
            same(f"{nid} folder", old["sftpFolder"], sftp.render_folder(new["sftpFolder"], now, 7, "Report mensile"))
        else:
            try:
                was = old_sftp.render_file_name(old["sftpFileName"], "csv", now, 7, "Report mensile")
            except old_sftp.SftpError:
                was = None  # the code of before refused it
            try:
                now_name = sftp.render_file_name(new["sftpFileName"], "csv", now, 7, "Report mensile")
            except SftpError:
                now_name = None
            same(f"{nid} file name {old['sftpFileName']!r}", was, now_name)
    for name, (st, old_ref) in before["datasources"].items():
        new_ref = after["datasources"][name][1]
        if not old_ref:
            same(f"{name} (NULL/empty) untouched", old_ref, new_ref)
            continue
        o, n = json.loads(old_ref), json.loads(new_ref)
        fields = ("folder", "pattern") if st == "sftp" else ("path",)
        exp = ft.expand({f: n[f] for f in fields}, ft.FileTemplates(), IngestErr, now)
        for f in fields:
            same(f"{name} {f}", o[f], exp[0].values[f])
    print(f"\n{ok}/{ok + bad} byte-identical")
    return 0 if bad == 0 else 1


class IngestErr(Exception):
    pass


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "seed":
        seed()
    elif action == "dump":
        dump()
    else:
        sys.exit(compare(*sys.argv[2:5]))
