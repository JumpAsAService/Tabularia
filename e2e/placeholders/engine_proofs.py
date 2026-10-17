"""Real proofs of the placeholders, the ranges and the «S3 folder» datasource, on the
engine's code against REAL servers: a throwaway MinIO, OpenSSH server, SMTP catcher
(mailpit) and the fake HTTP API of e2e/api — all on the dev network.

The throwaway servers (pinned images; passwords of your own):

    docker run -d --name zzph-minio --network dataprep-network -e MINIO_ROOT_USER=zzphadmin \
        -e MINIO_ROOT_PASSWORD=... pgsty/minio:RELEASE.2026-08-04T00-00-00Z server /data
    docker run -d --name zzph-sftp --network dataprep-network -e PUID=1000 -e PGID=1000 \
        -e PASSWORD_ACCESS=true -e USER_NAME=tab -e USER_PASSWORD=... \
        lscr.io/linuxserver/openssh-server:10.3_p1-r1-ls237
    docker run -d --name zzph-smtp --network dataprep-network -e MP_SMTP_AUTH_ACCEPT_ANY=1 \
        -e MP_SMTP_AUTH_ALLOW_INSECURE=1 \
        axllent/mailpit@sha256:74d609a42ec279aa63c6b4622a6fa9b5408d1ad5b1d76a1c4be40a265ce0863d  # v1.31.2
    docker run -d --name zzph-api --network dataprep-network -v "$PWD/e2e/api:/srv:ro" \
        python:3.12-slim python -u /srv/fake_api_server.py

Runs INSIDE the backend container (it has the code, boto3, paramiko):

    docker cp e2e/placeholders/engine_proofs.py dataprep-backend:/tmp/
    docker exec -e S3_ENDPOINT=http://zzph-minio:9000 -e S3_KEY=... -e S3_SECRET=... \\
        -e SFTP_HOST=zzph-sftp -e SFTP_PORT=2222 -e SFTP_USER=tab -e SFTP_PASSWORD=... \\
        -e SMTP_HOST=zzph-smtp -e API_BASE=http://zzph-api:8080/v1 \\
        dataprep-backend python /tmp/engine_proofs.py

Every expected value is computed here independently (datetime + zoneinfo), never by
the code under test. Exit code 1 if any check fails.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl

sys.path.insert(0, "/app")
from app.core.config import get_settings  # noqa: E402
from app.core.crypto import encrypt_secret  # noqa: E402
from app.ingest import email_destination as em  # noqa: E402
from app.ingest import http_api as api  # noqa: E402
from app.ingest import s3_source as s3  # noqa: E402
from app.ingest import sftp  # noqa: E402
from app.ingest.s3_destination import S3ConnectionSpec, S3DestinationSpec, write_output_to_s3  # noqa: E402

results: list[tuple[str, bool]] = []
FAR = "Pacific/Kiritimati"  # UTC+14: «today» there is tomorrow in UTC for 14 hours a day


def check(name: str, cond: bool, detail=None) -> None:
    results.append((name, bool(cond)))
    print(("  ok  " if cond else "  KO  ") + name + (f" — {str(detail)[:500]}" if detail is not None and not cond else ""), flush=True)


class LocalStorage:
    """The engine's storage, on the local disk (the datasource parquet goes here)."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="zz_ph_storage_")

    def _p(self, bucket, key):
        p = os.path.join(self.root, bucket, key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def upload_file(self, local, bucket, key):
        shutil.copy(local, self._p(bucket, key))

    def download_file(self, bucket, key, local):
        shutil.copy(self._p(bucket, key), local)

    def read(self, bucket, key) -> pl.DataFrame:
        return pl.read_parquet(self._p(bucket, key))


def csv(rows: list[tuple]) -> bytes:
    return ("id,v\n" + "".join(f"{a},{b}\n" for a, b in rows)).encode()


def parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


def xlsx(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_excel(buf)
    return buf.getvalue()


# ── S3 ─────────────────────────────────────────────────────────────────────────
def s3_conn(**kw) -> S3ConnectionSpec:
    base = dict(endpoint_url=os.environ["S3_ENDPOINT"], access_key=os.environ["S3_KEY"],
                secret_key_encrypted=encrypt_secret(os.environ["S3_SECRET"]), bucket="zzph-in")
    base.update(kw)
    return S3ConnectionSpec(**base)


def s3_phase(storage: LocalStorage, today: date) -> None:
    client = s3_conn().client()
    for b in ("zzph-in", "zzph-out", "zzph-other"):
        try:
            client.create_bucket(Bucket=b)
        except Exception as e:  # noqa: BLE001 — already there from a previous run
            if "BucketAlreadyOwnedByYou" not in str(e) and "BucketAlreadyExists" not in str(e):
                raise
    d = lambda n: today - timedelta(days=n)  # noqa: E731
    objects = {
        f"daily/{d(3).isoformat()}/sales.csv": csv([(1, 10), (2, 20)]),
        f"daily/{d(1).isoformat()}/sales.csv": csv([(3, 30)]),  # the day before yesterday is missing
        "mixed/a.csv": csv([(1, 1)]),
        "mixed/b.parquet": parquet(pl.DataFrame({"id": [2], "v": [2]})),
        "mixed/c.xlsx": xlsx(pl.DataFrame({"id": [3], "v": [3]})),
        "mixed/d.ndjson": b'{"id": 4, "v": 4}\n',
        "mixed/.hidden.csv": csv([(99, 99)]),
        "mixedbox/e.csv": csv([(98, 98)]),  # a folder whose name starts like «mixed»
        "big/huge.csv": b"id,v\n" + b"1,1\n" * 300_000,
    }
    for k, body in objects.items():
        client.put_object(Bucket="zzph-in", Key=k, Body=body)
    client.put_object(Bucket="zzph-other", Key="mixed/secret.csv", Body=csv([(66, 66)]))

    print("— S3 folder: a glob over CSV + parquet + Excel + NDJSON")
    res = s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(folder="mixed", pattern="*.*"), "eng", "s3/mixed.parquet",
                                  storage)
    df = storage.read("eng", "s3/mixed.parquet")
    check("four formats stacked, hidden and sibling folders left out", sorted(df["id"].to_list()) == [1, 2, 3, 4], df)
    check("_file and _file_modified_at are the last columns",
          df.columns[-2:] == ["_file", "_file_modified_at"] and sorted(df["_file"].to_list()) == ["a.csv", "b.parquet", "c.xlsx", "d.ndjson"])
    check("_file_modified_at is the object's time (today, UTC)",
          all(t.date() >= today - timedelta(days=1) for t in df["_file_modified_at"].to_list()), df["_file_modified_at"])
    check("nothing from another bucket", 66 not in df["id"].to_list())

    print("— S3 folder: a range over days with a missing day")
    days = {"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}
    res = s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(folder="daily/{day}", pattern="sales.csv", variables=[days]),
                                  "eng", "s3/daily.parquet", storage)
    df = storage.read("eng", "s3/daily.parquet")
    check("the two days with a file are read, with their _param_day",
          sorted(df.select("id", "_param_day").rows()) == [(1, d(3).isoformat()), (2, d(3).isoformat()), (3, d(1).isoformat())], df)
    check("_file keeps the day (below the folder's fixed part)",
          sorted(set(df["_file"].to_list())) == [f"{d(3).isoformat()}/sales.csv", f"{d(1).isoformat()}/sales.csv"], df["_file"])
    check("the missing day is listed, not failed",
          res["missing"] == [{"params": {"day": d(2).isoformat()}, "pattern": f"daily/{d(2).isoformat()}/sales.csv"}], res["missing"])

    print("— S3 folder: no file at all across the range fails")
    try:
        s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(folder="daily", pattern="none_{day}.csv", variables=[days]),
                                "eng", "s3/none.parquet", storage)
        check("no file at all fails the refresh", False)
    except s3.S3SourceError as e:
        check("no file at all fails the refresh, naming the values", "any of the 3 values" in str(e), e)

    print("— S3 folder: caps before any download, '..', secrets")
    cfg = get_settings().s3_source
    old = cfg.max_file_bytes
    cfg.max_file_bytes = 1_000_000
    try:
        s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(folder="big", pattern="*.csv"), "eng", "s3/big.parquet", storage)
        check("a file over the cap is refused", False)
    except s3.S3SourceError as e:
        check("a file over the cap is refused, from the listing", "over the limit" in str(e) and "huge.csv" in str(e), e)
    finally:
        cfg.max_file_bytes = old
    for kw in (dict(folder="mixed/..", pattern="*.csv"), dict(folder="mixed", pattern="../daily/*.csv")):
        try:
            s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(**kw), "eng", "s3/x.parquet", storage)
            check(f"'..' refused ({kw})", False)
        except s3.S3SourceError as e:
            check(f"'..' refused ({kw})", "'..'" in str(e), e)
    try:
        s3.check_files(s3_conn(secret_key_encrypted=encrypt_secret("wrong-" + os.environ["S3_SECRET"])),
                       s3.S3SourceSpec(folder="mixed", pattern="*.csv"))
        check("a wrong secret is refused", False)
    except s3.S3SourceError as e:
        check("a wrong secret is refused without showing it", "credentials" in str(e) and os.environ["S3_SECRET"] not in str(e), e)

    print("— S3 folder: append by key")
    spec = dict(folder="daily/{day}", pattern="sales.csv", mode="append", keys=["_param_day", "id"], variables=[days])
    s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(**spec), "eng", "s3/app1.parquet", storage)
    client.put_object(Bucket="zzph-in", Key=f"daily/{d(1).isoformat()}/sales.csv", Body=csv([(3, 31), (4, 40)]))
    res = s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(**spec), "eng", "s3/app2.parquet", storage,
                                  previous={"bucket": "eng", "key": "s3/app1.parquet"})
    df = storage.read("eng", "s3/app2.parquet")
    # the three rows of before all came again (both days re-read): replaced, never doubled
    check("the corrected row replaces the old one, the new is added, nothing doubled",
          sorted(df.select("id", "v").rows()) == [(1, 10), (2, 20), (3, 31), (4, 40)] and res["rows_replaced"] == 3,
          (df.select("id", "v").rows(), res["rows_replaced"]))

    print("— S3 folder: an hourly range over a day with a clock change (Rome, 25 October 2026)")
    for h in range(24):
        client.put_object(Bucket="zzph-in", Key=f"hourly/2026-10-25T{h:02}.csv", Body=csv([(h, h)]))
    hours = {"name": "hour", "type": "range", "start": "2026-10-25", "end": "2026-10-26", "step": "1h"}
    res = s3.ingest_s3_to_parquet(s3_conn(), s3.S3SourceSpec(folder="hourly", pattern="{hour:yyyy-MM-dd'T'HH}.csv",
                                                             variables=[hours], timezone="Europe/Rome"),
                                  "eng", "s3/hourly.parquet", storage)
    df = storage.read("eng", "s3/hourly.parquet")
    rome = ZoneInfo("Europe/Rome")
    first = datetime(2026, 10, 25, 0, 0, tzinfo=rome)
    expected = [(first.astimezone(timezone.utc) + timedelta(hours=k)).astimezone(rome) for k in range(25)]
    check("25 hourly values on the autumn change, from local midnight", res["expansions"] == 25
          and expected[0].isoformat() == "2026-10-25T00:00:00+02:00" and expected[-1].isoformat() == "2026-10-25T23:00:00+01:00", res)
    params = sorted(set(df["_param_hour"].to_list()))
    check("the 24 local hours each read once (the repeated 02 reads its file once), _param_ in the zone",
          df.height == 24 and res["missing"] == [] and "2026-10-25T00:00:00+02:00" in params
          and "2026-10-25T23:00:00+01:00" in params, (df.height, params[:3], params[-2:]))

    print("— S3 Output: a dated key in a far zone, and a plain key as before")
    now = datetime.now(timezone.utc)
    local_far = now.astimezone(ZoneInfo(FAR))
    out_storage = storage
    src = pl.DataFrame({"a": [1, 2]})
    src.write_parquet(out_storage._p("eng", "out/run.parquet"))
    res = write_output_to_s3(s3_conn(bucket="zzph-out"),
                             S3DestinationSpec(key="exp/{today:yyyy/MM}/v_{date}_{run_id}_{flow}.csv", format="csv",
                                               timezone=FAR, run_id=42, flow="Vendite mese"),
                             "eng", "out/run.parquet", storage=out_storage, now=now)
    expected = f"exp/{local_far:%Y/%m}/v_{local_far:%Y-%m-%d}_42_Vendite_mese.csv"
    head = client.head_object(Bucket="zzph-out", Key=expected)
    check(f"the object is at the key resolved in {FAR} ({expected})", res["key"] == expected and head["ContentLength"] > 0, res)
    res = write_output_to_s3(s3_conn(bucket="zzph-out"), S3DestinationSpec(key="plain/v.parquet"), "eng", "out/run.parquet",
                             storage=out_storage)
    check("a key without placeholders is written as before", res["key"] == "plain/v.parquet")


# ── SFTP ───────────────────────────────────────────────────────────────────────
def sftp_conn() -> sftp.SftpConnectionSpec:
    base = dict(host=os.environ["SFTP_HOST"], port=int(os.environ.get("SFTP_PORT", "2222")),
                username=os.environ["SFTP_USER"], password_encrypted=encrypt_secret(os.environ["SFTP_PASSWORD"]))
    seen = sftp.inspect_connection(sftp.SftpConnectionSpec(**base))
    return sftp.SftpConnectionSpec(**base, host_key_type=seen["host_key"]["type"],
                                   host_key_fingerprint=seen["host_key"]["fingerprint"])


def sftp_phase(storage: LocalStorage, today: date) -> None:
    conn = sftp_conn()
    d = lambda n: today - timedelta(days=n)  # noqa: E731
    with sftp.open_sftp(conn) as c:
        for p in ("ph", "ph/in", "ph/out"):
            try:
                c.mkdir(p)
            except OSError:
                pass
        for name in c.listdir("ph/in"):
            c.remove(f"ph/in/{name}")
        for n, rows in ((3, [(1, 10)]), (1, [(2, 20), (3, 30)])):
            with c.open(f"ph/in/sales_{d(n):%Y%m%d}.csv", "wb") as f:
                f.write(csv(rows))
        with c.open("ph/in/menu.xlsx", "wb") as f:
            f.write(xlsx(pl.DataFrame({"id": [7], "v": [70]})))

    print("— SFTP folder: a range with a missing day")
    days = {"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}
    res = sftp.ingest_sftp_to_parquet(conn, sftp.SftpSourceSpec(folder="ph/in", pattern="sales_{day:yyyyMMdd}.csv",
                                                                variables=[days]), "eng", "sftp/r.parquet", storage)
    df = storage.read("eng", "sftp/r.parquet")
    check("two days read with _param_day", sorted(df.select("id", "_param_day").rows()) ==
          [(1, d(3).isoformat()), (2, d(1).isoformat()), (3, d(1).isoformat())], df)
    check("the missing day listed", [m["params"]["day"] for m in res["missing"]] == [d(2).isoformat()], res["missing"])
    try:
        sftp.ingest_sftp_to_parquet(conn, sftp.SftpSourceSpec(folder="ph/in", pattern="none_{day}.csv", variables=[days]),
                                    "eng", "sftp/n.parquet", storage)
        check("no file at all fails", False)
    except sftp.SftpError as e:
        check("no file at all fails the refresh", "any of the 3 values" in str(e), e)

    print("— SFTP Output: the old placeholders write the same bytes, a far zone, a dated folder")
    src = pl.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    src.write_parquet(storage._p("eng", "out/sftp.parquet"))
    now = datetime(2026, 10, 17, 14, 30, 5, tzinfo=timezone.utc)
    legacy = f"/ph/out/v_{now:%Y-%m-%d}_{now:%Y-%m-%d_%H%M%S}_9_Report_mensile.csv"  # the formula of before
    res = sftp.write_output_to_sftp(conn, sftp.SftpDestinationSpec(folder="/config/ph/out", file_name="v_{date}_{datetime}_{run_id}_{flow}",
                                                                   format="csv", run_id=9, flow="Report/mensile"),
                                    "eng", "out/sftp.parquet", storage=storage, now=now)
    check("{date}_{datetime}_{run_id}_{flow}: the same name as before", res["path"].endswith(legacy[len("/ph/out"):]), res)
    far = datetime.now(timezone.utc).astimezone(ZoneInfo(FAR))
    res = sftp.write_output_to_sftp(conn, sftp.SftpDestinationSpec(folder="/config/ph/out/{today:yyyy}/{today:MM}",
                                                                   file_name="x_{today:yyyyMMdd}", format="parquet",
                                                                   timezone=FAR, run_id=1, flow="f"),
                                    "eng", "out/sftp.parquet", storage=storage)
    expected = f"/config/ph/out/{far:%Y}/{far:%m}/x_{far:%Y%m%d}.parquet"
    with sftp.open_sftp(conn) as c:
        size = c.stat(expected).st_size
        names = c.listdir(f"/config/ph/out/{far:%Y}/{far:%m}")
    check(f"written in {FAR} in a dated folder created for it ({expected})", res["path"] == expected and size > 0, res)
    check("no temporary file left", names == [f"x_{far:%Y%m%d}.parquet"], names)
    try:
        sftp.write_output_to_sftp(conn, sftp.SftpDestinationSpec(folder="/config/ph/out", file_name="{today:yyyy/MM}.csv"),
                                  "eng", "out/sftp.parquet", storage=storage)
        check("a slash from a value is refused", False)
    except sftp.SftpError as e:
        check("a slash from a value cannot put a folder in the name", "'/'" in str(e), e)


# ── email ──────────────────────────────────────────────────────────────────────
def email_phase(storage: LocalStorage) -> None:
    print("— Email: subject and attachment name in a far zone, to a real SMTP server")
    host = os.environ["SMTP_HOST"]
    urllib.request.urlopen(urllib.request.Request(f"http://{host}:8025/api/v1/messages", method="DELETE"))
    pl.DataFrame({"a": [1]}).write_parquet(storage._p("eng", "out/mail.parquet"))
    import app.ingest.email_destination as mod

    real_build = mod.build_attachment

    def build(bucket, key, spec):  # the attachment from the local storage (the engine's export reads S3)
        path = storage._p(bucket, key)
        data = pl.read_parquet(path).write_csv().encode()
        return data, mod._filename(spec)

    mod.build_attachment = build
    try:
        now = datetime.now(timezone.utc)
        far = now.astimezone(ZoneInfo(FAR))
        em.send_output_email(em.SmtpConnectionSpec(host=host, port=1025, tls="none", from_address="zz@tabularia.test"),
                             em.EmailSpec(to=["capo@tabularia.test"], subject="Report {flow} {today:dd/MM/yyyy} #{run_id}",
                                          attachment_name="sales_{date}", attachment_format="csv", timezone=FAR,
                                          run_id=5, flow="Vendite/Italia"),
                             "eng", "out/mail.parquet", now=now)
    finally:
        mod.build_attachment = real_build
    msgs = json.load(urllib.request.urlopen(f"http://{host}:8025/api/v1/messages"))["messages"]
    subject = msgs[0]["Subject"] if msgs else ""
    check("the subject resolved in the zone, {flow} as it is",
          subject == f"Report Vendite/Italia {far:%d/%m/%Y} #5", subject)
    full = json.load(urllib.request.urlopen(f"http://{host}:8025/api/v1/message/{msgs[0]['ID']}")) if msgs else {}
    names = [a.get("FileName") for a in full.get("Attachments", [])]
    check("the attachment name resolved", names == [f"sales_{far:%Y-%m-%d}.csv"], names)


# ── API ────────────────────────────────────────────────────────────────────────
def api_phase(storage: LocalStorage, today_utc: date) -> None:
    print("— API: a range of days, one call per window with {day} and {day_end}")
    base = os.environ["API_BASE"]
    urllib.request.urlopen(urllib.request.Request(base.rsplit("/v1", 1)[0] + "/_reset", method="POST"))
    conn = api.ApiConnectionSpec(base_url=base)
    src = api.ApiSourceSpec(path="/echo", query=[api.Pair(name="from", value="{day}"),
                                                 api.Pair(name="to", value="{day_end:yyyyMMdd}")],
                            variables=[{"name": "day", "type": "range", "start": "today-3", "end": "today", "step": "1d"}])
    res = api.ingest_api_to_parquet(conn, src, "eng", "api/r.parquet", storage)
    df = storage.read("eng", "api/r.parquet")
    d = lambda n: today_utc - timedelta(days=n)  # noqa: E731
    expected = [(d(3).isoformat(), f"{d(2):%Y%m%d}"), (d(2).isoformat(), f"{d(1):%Y%m%d}"), (d(1).isoformat(), f"{d(0):%Y%m%d}")]
    got = df.select("query.from", "query.to").rows()
    check("three calls, consecutive half-open windows", res["calls"] == 3 and got == expected, got)
    check("_param_day on every row", df["_param_day"].to_list() == [e[0] for e in expected], df["_param_day"])


def main() -> int:
    storage = LocalStorage()
    today_utc = datetime.now(timezone.utc).date()
    s3_phase(storage, today_utc)
    sftp_phase(storage, today_utc)
    email_phase(storage)
    api_phase(storage, today_utc)
    ok = sum(1 for _, c in results if c)
    print(f"\n{ok}/{len(results)} checks passed")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
