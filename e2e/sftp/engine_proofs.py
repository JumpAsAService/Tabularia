"""Real proofs of the SFTP engine code against an OpenSSH server (a throwaway container).

Runs INSIDE the backend container (it has paramiko and the code):

    docker cp e2e/sftp/engine_proofs.py dataprep-backend:/tmp/
    docker cp <client key> dataprep-backend:/tmp/zz_client_key
    docker exec -e SFTP_HOST=zzsftp-a -e SFTP_PORT=2222 -e SFTP_USER=tab -e SFTP_PASSWORD=... \\
        -e SFTP_KEY=/tmp/zz_client_key -e SFTP_FINGERPRINTS="SHA256:… SHA256:…" \\
        dataprep-backend python /tmp/engine_proofs.py [phase]

Phases: `fixtures` (data files for e2e/sftp/api_proofs.py), `main` (password, key, TOFU, glob over CSV+Excel+parquet+NDJSON, cap, Output in
three formats with placeholders and the atomic rename) and `changed` (after the server's
keys were regenerated: the old pin is refused before any credential).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

import paramiko
import polars as pl

sys.path.insert(0, "/app")
from app.core.config import get_settings  # noqa: E402
from app.core.crypto import encrypt_secret  # noqa: E402
from app.ingest import sftp  # noqa: E402

HOST, PORT = os.environ["SFTP_HOST"], int(os.environ.get("SFTP_PORT", "22"))
USER, PASSWORD = os.environ["SFTP_USER"], os.environ["SFTP_PASSWORD"]
KEY = open(os.environ["SFTP_KEY"]).read()
ORACLE = set(os.environ.get("SFTP_FINGERPRINTS", "").split())
PIN_FILE = "/tmp/zz_sftp_pin.json"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(("  ok  " if cond else "  KO  ") + name + (f" — {detail}" if detail and not cond else ""), flush=True)


class LocalStorage:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="zz_storage_")

    def _p(self, bucket, key):
        p = os.path.join(self.root, bucket, key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def upload_file(self, local, bucket, key):
        shutil.copy(local, self._p(bucket, key))

    def download_file(self, bucket, key, local):
        shutil.copy(self._p(bucket, key), local)


def conn(**kw) -> sftp.SftpConnectionSpec:
    base = dict(host=HOST, port=PORT, username=USER, auth="password", password_encrypted=encrypt_secret(PASSWORD))
    base.update(kw)
    return sftp.SftpConnectionSpec(**base)


def xlsx_bytes(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_excel(buf)
    return buf.getvalue()


def main_phase() -> None:
    print("— test connection (trust on first use)")
    out = sftp.inspect_connection(conn())
    fp, kt = out["host_key"]["fingerprint"], out["host_key"]["type"]
    check("step 1: the test shows the server key and does not log in", out["needs_acceptance"] and not out["ok"] and fp.startswith("SHA256:"), str(out))
    check("the fingerprint is one ssh-keygen prints on the server", fp in ORACLE, f"{fp} not in {ORACLE}")
    out = sftp.inspect_connection(conn(), expected_fingerprint=fp, expected_type=kt)
    check("step 2: accepting that key logs in", out["ok"] and not out["needs_acceptance"], str(out))
    out = sftp.inspect_connection(conn(), expected_fingerprint="SHA256:not-this-one")
    check("accepting a key the server does not offer: not logged in", out["needs_acceptance"] and not out["ok"], str(out))
    json.dump({"type": kt, "fingerprint": fp}, open(PIN_FILE, "w"))
    pinned = dict(host_key_type=kt, host_key_fingerprint=fp)

    key_conn = conn(auth="key", password_encrypted="",
                    private_key_encrypted=encrypt_secret(json.dumps({"private_key": KEY, "passphrase": ""})), **pinned)
    check("key authentication (OpenSSH ed25519)", sftp.inspect_connection(key_conn)["ok"])
    try:
        sftp.inspect_connection(conn(password_encrypted=encrypt_secret("wrong"), host_key_type=kt, host_key_fingerprint=fp))
        check("a wrong password is refused", False)
    except sftp.SftpError as e:
        check("a wrong password is refused", "authentication rejected" in str(e), str(e))
    try:
        sftp.list_files(conn(), "in", "*")
        check("no pinned key: refused", False)
    except sftp.SftpHostKeyError as e:
        check("no pinned key: refused", e.code == "host_key_unknown", str(e))

    print("— fixtures on the server (plain paramiko)")
    c = conn(**pinned)
    with sftp.open_sftp(c) as cl:
        home = cl.normalize(".")
        for d in ("in", "out", "big", "tree"):
            try:
                cl.mkdir(d)
            except OSError:
                pass
        put = lambda path, data: cl.putfo(io.BytesIO(data), path)  # noqa: E731
        put("in/vendite_01.csv", b"id,importo,citta\n1,10.5,Milano\n2,20,Roma\n")
        put("in/vendite_02.xlsx", xlsx_bytes(pl.DataFrame({"id": [3], "importo": [30.0], "citta": ["Torino"]})))
        buf = io.BytesIO(); pl.DataFrame({"id": [4, 5], "importo": [40.0, 50.0]}).write_parquet(buf)
        put("in/vendite_03.parquet", buf.getvalue())
        put("in/vendite_04.ndjson", b'{"id": 6, "importo": 60.0, "citta": "Bari"}\n')
        put("in/.vendite_hidden.csv", b"id\n99\n")
        put("in/altro.csv", b"id\n98\n")
        put("big/huge.csv", b"id\n" + b"1\n" * 600_000)
        put("out/fixed.csv", b"old\n")
        put("tree/a.csv", b"id\n1\n")
        try:
            cl.mkdir("tree/sub")
        except OSError:
            pass
        put("tree/sub/b.csv", b"id\n2\n")
        try:
            cl.symlink(".", "tree/loop")  # a link to its own folder
        except OSError:
            pass
    print(f"  login dir {home}")

    print("— the glob and the import")
    files = sftp.list_files(c, "in", "vendite_*")
    check("glob takes the 4 data files, not the hidden one nor altro.csv",
          [f.path.rsplit("/", 1)[1] for f in files] == ["vendite_01.csv", "vendite_02.xlsx", "vendite_03.parquet", "vendite_04.ndjson"],
          str([f.path for f in files]))
    st = LocalStorage()
    res = sftp.ingest_sftp_to_parquet(c, sftp.SftpSourceSpec(folder="in", pattern="vendite_*"), "b", "datasets/zz.parquet", st)
    df = pl.read_parquet(st._p("b", "datasets/zz.parquet"))
    check("CSV + Excel + parquet + NDJSON stacked: 6 rows", res["rows_written"] == 6 and df.height == 6, str(res))
    check("_file holds each file's path below the folder, on every row",
          sorted(df["_file"].unique().to_list()) == ["vendite_01.csv", "vendite_02.xlsx", "vendite_03.parquet", "vendite_04.ndjson"]
          and df["_file"].null_count() == 0, str(df["_file"].unique().to_list()))
    check("_file and _file_modified_at are the last two columns", df.columns[-2:] == ["_file", "_file_modified_at"], str(df.columns))
    print("  sample rows:\n" + str(df.select("id", "importo", "_file", "_file_modified_at").sort("id")))
    st1 = LocalStorage()
    sftp.ingest_sftp_to_parquet(c, sftp.SftpSourceSpec(folder="in", pattern="vendite_01.csv"), "b", "datasets/one.parquet", st1)
    one = pl.read_parquet(st1._p("b", "datasets/one.parquet"))
    check("a glob matching ONE file still fills _file on every row", one["_file"].to_list() == ["vendite_01.csv", "vendite_01.csv"],
          str(one))
    check("total of importo", abs(df["importo"].sum() - 210.5) < 1e-9, str(df["importo"].sum()))
    check("schema drift reported (citta missing in parquet)", res["schema_drift"] == ["citta"], str(res["schema_drift"]))
    with sftp.open_sftp(c) as cl:
        check("the files are still on the server", "vendite_01.csv" in cl.listdir("in"))

    tree = sftp.list_files(c, "tree", "**/*.csv")
    check("** does not follow a link to a folder (loop -> .)", [f.rel for f in tree] == ["a.csv", "sub/b.csv"], [f.rel for f in tree])
    for folder, pattern in (("in", "../*.csv"), ("in/..", "*.csv")):
        try:
            sftp.list_files(c, folder, pattern)
            check(f"'..' refused ({folder!r}, {pattern!r})", False)
        except sftp.SftpError as e:
            check(f"'..' refused ({folder!r}, {pattern!r})", "'..'" in str(e), str(e))

    print("— the cap")
    settings = get_settings().sftp
    old = settings.max_file_bytes
    settings.max_file_bytes = 1_000_000
    try:
        sftp.ingest_sftp_to_parquet(c, sftp.SftpSourceSpec(folder="big", pattern="*.csv"), "b", "datasets/big.parquet", st)
        check("a 1.2 MB file over a 1 MB cap is refused", False)
    except sftp.SftpError as e:
        check("a 1.2 MB file over a 1 MB cap is refused", "huge.csv" in str(e) and "over the limit" in str(e), str(e))
    finally:
        settings.max_file_bytes = old
    settings.max_total_bytes, old_total = 150, settings.max_total_bytes
    try:
        sftp.ingest_sftp_to_parquet(c, sftp.SftpSourceSpec(folder="in", pattern="vendite_*"), "b", "datasets/t.parquet", st)
        check("the total cap refuses before any download", False)
    except sftp.SftpError as e:
        check("the total cap refuses before any download", "per refresh" in str(e), str(e))
    finally:
        settings.max_total_bytes = old_total

    print("— the Output")
    renames = []
    orig = paramiko.SFTPClient.posix_rename

    def spy(self, a, b):
        renames.append((a, b))
        return orig(self, a, b)

    paramiko.SFTPClient.posix_rename = spy
    src = pl.DataFrame({"paese": ["IT", "FR", "DE"], "vendite": [1.5, 2.0, 3.25]})
    tmp = tempfile.mkdtemp()
    src.write_parquet(os.path.join(tmp, "o.parquet"))
    st.upload_file(os.path.join(tmp, "o.parquet"), "b", "out/o.parquet")
    now = datetime(2026, 10, 17, 9, 5, 7, tzinfo=timezone.utc)
    readers = {"csv": pl.read_csv, "parquet": pl.read_parquet, "xlsx": pl.read_excel}
    for fmt in ("csv", "parquet", "xlsx"):
        dest = sftp.SftpDestinationSpec(folder="out", file_name="vendite_{date}_{datetime}_{run_id}_{flow}", format=fmt,
                                        run_id=314, flow="Report mensile")
        r = sftp.write_output_to_sftp(c, dest, "b", "out/o.parquet", storage=st, now=now)
        expected = f"{home.rstrip('/')}/out/vendite_2026-10-17_2026-10-17_090507_314_Report_mensile.{fmt}"
        local = os.path.join(tmp, f"back.{fmt}")
        with sftp.open_sftp(c) as cl:
            cl.get(r["path"], local)
        back = readers[fmt](local)
        check(f"{fmt}: written with the placeholders and readable back", r["path"] == expected and back["vendite"].to_list() == [1.5, 2.0, 3.25],
              f"{r} / {back}")
    check("every write went through posix-rename from a hidden temporary name in the same folder",
          len(renames) == 3 and all(a.rsplit("/", 1)[1].startswith(".vendite_") and a.endswith(".tmp")
                                    and a.rsplit("/", 1)[0] == b.rsplit("/", 1)[0] for a, b in renames), str(renames))
    r = sftp.write_output_to_sftp(c, sftp.SftpDestinationSpec(folder="out", file_name="fixed.csv", format="csv"), "b", "out/o.parquet",
                                  storage=st, now=now)
    with sftp.open_sftp(c) as cl:
        cl.get(r["path"], os.path.join(tmp, "fixed.csv"))
        names = cl.listdir("out")
    check("an existing file with the final name is replaced", pl.read_csv(os.path.join(tmp, "fixed.csv"))["paese"].to_list() == ["IT", "FR", "DE"])
    check("no temporary file left in the folder", not any(n.startswith(".") for n in names), str(names))
    try:
        sftp.write_output_to_sftp(c, sftp.SftpDestinationSpec(folder="nowhere", file_name="x", format="csv"), "b", "out/o.parquet", storage=st)
        check("a missing folder is refused", False)
    except sftp.SftpError as e:
        check("a missing folder is refused", "not found" in str(e), str(e))


def fixtures_phase() -> None:
    """Data files for the end-to-end proof through the API, put with plain paramiko."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # a throwaway server: fixtures only
    client.connect(HOST, PORT, USER, PASSWORD, look_for_keys=False, allow_agent=False)
    cl = client.open_sftp()
    for d in ("in", "out"):
        try:
            cl.mkdir(d)
        except OSError:
            pass
    cl.putfo(io.BytesIO(b"id,importo,citta\n1,10.5,Milano\n2,20,Roma\n"), "in/vendite_01.csv")
    cl.putfo(io.BytesIO(xlsx_bytes(pl.DataFrame({"id": [3], "importo": [30.0], "citta": ["Torino"]}))), "in/vendite_02.xlsx")
    buf = io.BytesIO(); pl.DataFrame({"id": [4, 5], "importo": [40.0, 50.0], "citta": ["Napoli", "Pisa"]}).write_parquet(buf)
    cl.putfo(io.BytesIO(buf.getvalue()), "in/vendite_03.parquet")
    cl.close(); client.close()
    check("fixtures uploaded", True)


def stall_phase() -> None:
    """The caller pauses the server's container while this waits after the login."""
    pin = json.load(open(PIN_FILE))
    c = conn(timeout_seconds=5, host_key_type=pin["type"], host_key_fingerprint=pin["fingerprint"])
    with sftp.open_sftp(c) as cl:
        print("  logged in; pause the server now", flush=True)
        time.sleep(6)
        started = time.time()
        try:
            cl.listdir("in")
            check("a server that stops answering fails within the timeout", False)
        except Exception as e:  # noqa: BLE001
            took = time.time() - started
            check("a server that stops answering fails within the timeout", took < 15, f"{type(e).__name__} after {took:.1f}s")
            print(f"  raised {type(e).__name__} after {took:.1f} s")


def changed_phase() -> None:
    pin = json.load(open(PIN_FILE))
    out = sftp.inspect_connection(conn(host_key_type=pin["type"], host_key_fingerprint=pin["fingerprint"]))
    check("after regenerating the server keys the test shows another key, changed, without logging in",
          out["host_key"]["fingerprint"] != pin["fingerprint"] and out["changed"] and out["needs_acceptance"], str(out))
    try:
        sftp.list_files(conn(host_key_type=pin["type"], host_key_fingerprint=pin["fingerprint"]), "in", "*")
        check("the old pin is refused", False)
    except sftp.SftpHostKeyError as e:
        check("the old pin is refused", e.code == "host_key_changed" and "Test connection" in str(e), str(e))
        print("  message: " + str(e))


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "main"
    {"main": main_phase, "changed": changed_phase, "fixtures": fixtures_phase, "stall": stall_phase}[phase]()
    bad = [n for n, okay, _ in results if not okay]
    print(f"\n{len(results) - len(bad)}/{len(results)} ok" + (f" — failed: {bad}" if bad else ""))
    sys.exit(1 if bad else 0)
