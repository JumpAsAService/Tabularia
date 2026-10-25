"""SFTP: the datasource «SFTP folder» (read) and the SFTP Output (write).

Against an SFTP server that runs in the test process (tests/fake_sftp), so the
client speaks real SSH: what these tests fix is our side of the contract —
trust on first use of the host key, credentials never offered to a server whose
key does not match, the glob, the stacking with `_file`, the refusal of files
that are not machine readable, the caps, and the atomic write of the Output.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone

import paramiko
import polars as pl
import pytest

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.ingest import sftp
from tests.conftest import BUCKET, upload_df
from tests.fake_graph import xlsx
from tests.fake_sftp import FakeSftpServer
from tests.fakes import FakeStorage


# ── helpers ────────────────────────────────────────────────────────────────────
def _write(root, rel, data: bytes | str, mtime: float | None = None):
    path = os.path.join(root, rel.lstrip("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf)
    return buf.getvalue()


def _conn(srv: FakeSftpServer, pin: bool = True, **kw) -> sftp.SftpConnectionSpec:
    base = dict(host="127.0.0.1", port=srv.port, username="tab", auth="password",
                password_encrypted=encrypt_secret("pw"), timeout_seconds=10)
    if pin:
        base.update(host_key_type=srv.host_key.get_name(), host_key_fingerprint=sftp.fingerprint(srv.host_key))
    base.update(kw)
    return sftp.SftpConnectionSpec(**base)


def _key_blob(private_key: str, passphrase: str = "") -> str:
    """What the gateway stores and sends: the key and its passphrase, encrypted together."""
    return encrypt_secret(json.dumps({"private_key": private_key, "passphrase": passphrase}))


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "srv"
    d.mkdir()
    return str(d)


@pytest.fixture
def srv(root):
    with FakeSftpServer(root) as s:
        yield s


# ── host key: fingerprint and trust on first use ───────────────────────────────
def test_the_fingerprint_is_the_openssh_sha256_one():
    """Oracle: `ssh-keygen -lf vec.pub -E sha256` on the same public key."""
    key = paramiko.Ed25519Key(data=paramiko.Message(
        __import__("base64").b64decode("AAAAC3NzaC1lZDI1NTE5AAAAIE33au8LtDzVmJGBt7uo2tYwHt3cxj29QDLc+cWIskfv")).asbytes())
    assert sftp.fingerprint(key) == "SHA256:dCX0QNAAsgeHu1/ojT9s4/eY92V+3GDSbGJEIqpGA68"


def test_the_test_shows_an_unaccepted_key_and_sends_no_credential(srv):
    """Step one of trust on first use: the server's key is SHOWN, nothing is sent."""
    out = sftp.inspect_connection(_conn(srv, pin=False))
    assert out["ok"] is False and out["needs_acceptance"] is True
    assert out["host_key"] == {"type": srv.host_key.get_name(), "fingerprint": sftp.fingerprint(srv.host_key)}
    assert out["changed"] is False and out["previous"] == ""
    assert srv.auth_attempts == 0


def test_accepting_the_key_shown_authenticates_only_then(srv):
    """Step two: the caller names the key it accepts; the credentials leave only if
    the server offers exactly that key."""
    fp = sftp.fingerprint(srv.host_key)
    out = sftp.inspect_connection(_conn(srv, pin=False), expected_fingerprint=fp, expected_type=srv.host_key.get_name())
    assert out["ok"] is True and out["needs_acceptance"] is False and out["host_key"]["fingerprint"] == fp
    assert srv.auth_attempts == 1


def test_accepting_another_key_than_the_one_offered_sends_nothing(srv):
    out = sftp.inspect_connection(_conn(srv, pin=False), expected_fingerprint="SHA256:what-i-saw-before")
    assert out["ok"] is False and out["needs_acceptance"] is True
    assert srv.auth_attempts == 0


def test_a_changed_key_is_shown_with_the_previous_one_and_nothing_is_sent(srv):
    out = sftp.inspect_connection(_conn(srv, pin=True, host_key_fingerprint="SHA256:old"))
    assert out["needs_acceptance"] is True and out["changed"] is True and out["previous"] == "SHA256:old"
    assert out["host_key"]["fingerprint"] == sftp.fingerprint(srv.host_key)
    assert srv.auth_attempts == 0


def test_the_test_of_a_pinned_connection_checks_the_credentials(srv):
    assert sftp.inspect_connection(_conn(srv))["ok"] is True
    with pytest.raises(sftp.SftpError, match="authentication rejected"):
        sftp.inspect_connection(_conn(srv, password_encrypted=encrypt_secret("wrong")))


def test_without_a_pinned_key_nothing_is_read(srv, root):
    _write(root, "in/a.csv", "id\n1\n")
    with pytest.raises(sftp.SftpHostKeyError, match="not been accepted yet") as e:
        sftp.list_files(_conn(srv, pin=False), "in", "*.csv")
    assert e.value.code == "host_key_unknown"
    assert srv.auth_attempts == 0


def test_a_changed_host_key_is_refused_before_any_credential_is_sent(root):
    with FakeSftpServer(root) as first:
        conn = _conn(first)  # pinned on the first server's key
    with FakeSftpServer(root) as second:  # same address and files, another key: a new server, or someone in between
        conn = conn.model_copy(update={"port": second.port})
        with pytest.raises(sftp.SftpHostKeyError) as e:
            sftp.list_files(conn, "", "*")
        assert e.value.code == "host_key_changed"
        assert "Test connection" in str(e.value) and sftp.fingerprint(second.host_key) in str(e.value)
        assert str(e.value).startswith("SFTP: the host key of 127.0.0.1:")
        assert second.auth_attempts == 0, "the password must not reach a server whose key does not match"


def test_a_server_with_several_keys_is_asked_for_the_pinned_type(root):
    """A server has one key per type; the one it presents depends on what the client
    prefers. With the type pinned, the client asks for that one: a change in the
    preference order (a paramiko upgrade) must not read as a changed key."""
    rsa = paramiko.RSAKey.generate(2048)
    with FakeSftpServer(root, extra_host_keys=[rsa]) as srv:
        # unpinned, the client prefers ECDSA: that is what the test would show
        assert sftp.inspect_connection(_conn(srv, pin=False))["host_key"]["type"] == srv.host_key.get_name()
        # pinned on the RSA key, the same server must be recognised
        conn = _conn(srv, host_key_type="ssh-rsa", host_key_fingerprint=sftp.fingerprint(rsa))
        assert sftp.list_files(conn, "", "*") == []
        # and accepting the RSA key in a test asks for that type too
        out = sftp.inspect_connection(_conn(srv, pin=False), expected_fingerprint=sftp.fingerprint(rsa), expected_type="ssh-rsa")
        assert out["ok"] is True


def test_a_server_that_now_has_only_another_key_type_reads_as_a_changed_key(root):
    """Pinned on RSA, the server now offers ECDSA only: not «Incompatible ssh peer»
    but the clear refusal, and the test shows the new key."""
    rsa = paramiko.RSAKey.generate(2048)
    with FakeSftpServer(root) as srv:  # ECDSA only
        conn = _conn(srv, host_key_type="ssh-rsa", host_key_fingerprint=sftp.fingerprint(rsa))
        with pytest.raises(sftp.SftpHostKeyError) as e:
            sftp.list_files(conn, "", "*")
        assert e.value.code == "host_key_changed" and sftp.fingerprint(srv.host_key) in str(e.value)
        out = sftp.inspect_connection(conn)
        assert out["needs_acceptance"] is True and out["changed"] is True
        assert out["host_key"]["fingerprint"] == sftp.fingerprint(srv.host_key)
        assert srv.auth_attempts == 0


def test_a_server_that_stops_answering_after_login_does_not_hold_the_worker(root):
    _write(root, "in/a.csv", "id\n1\n")
    with FakeSftpServer(root, stall_listing=20) as srv:
        started = time.monotonic()
        with pytest.raises(sftp.SftpError, match="did not answer"):
            sftp.list_files(_conn(srv, timeout_seconds=2), "in", "*.csv")
        assert time.monotonic() - started < 10


# ── authentication ─────────────────────────────────────────────────────────────
def _openssh_ed25519() -> tuple[str, paramiko.PKey]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
    return pem, paramiko.Ed25519Key.from_private_key(io.StringIO(pem))


def test_key_authentication_with_an_openssh_ed25519_key(root):
    pem, key = _openssh_ed25519()
    with FakeSftpServer(root, password=None, authorized_key=key) as srv:
        _write(root, "a.csv", "id\n1\n")
        conn = _conn(srv, auth="key", password_encrypted="", private_key_encrypted=_key_blob(pem))
        assert [f.path for f in sftp.list_files(conn, "", "*.csv")] == ["/a.csv"]


def test_key_authentication_with_an_encrypted_pem_key(root):
    key = paramiko.ECDSAKey.generate()
    buf = io.StringIO()
    key.write_private_key(buf, password="s3cret")
    with FakeSftpServer(root, password=None, authorized_key=key) as srv:
        conn = _conn(srv, auth="key", password_encrypted="", private_key_encrypted=_key_blob(buf.getvalue(), "s3cret"))
        assert sftp.inspect_connection(conn)["ok"] is True
        wrong = conn.model_copy(update={"private_key_encrypted": _key_blob(buf.getvalue(), "nope")})
        with pytest.raises(sftp.SftpError, match="passphrase"):
            sftp.inspect_connection(wrong)
        missing = conn.model_copy(update={"private_key_encrypted": _key_blob(buf.getvalue(), "")})
        with pytest.raises(sftp.SftpError, match="passphrase"):
            sftp.inspect_connection(missing)


def test_a_text_that_is_not_a_private_key_is_refused_clearly(srv):
    conn = _conn(srv, auth="key", password_encrypted="", private_key_encrypted=_key_blob("hello"))
    with pytest.raises(sftp.SftpError, match="private key"):
        sftp.inspect_connection(conn)


def test_the_key_method_uses_the_key_and_not_the_stored_password(root):
    pem, key = _openssh_ed25519()
    with FakeSftpServer(root, password="pw", authorized_key=None) as srv:
        conn = _conn(srv, auth="key", private_key_encrypted=_key_blob(pem))  # the password is there too
        with pytest.raises(sftp.SftpError, match="does not accept the private key for user 'tab': it accepts password"):
            sftp.inspect_connection(conn)


def test_an_unreachable_host_gives_a_short_message():
    conn = sftp.SftpConnectionSpec(host="127.0.0.1", port=1, username="u", auth="password",
                                   password_encrypted=encrypt_secret("x"), timeout_seconds=3)
    with pytest.raises(sftp.SftpError, match=r"^SFTP: .*127\.0\.0\.1:1"):
        sftp.inspect_connection(conn)


# ── the glob ───────────────────────────────────────────────────────────────────
def test_the_glob_takes_the_matching_files_of_the_folder(srv, root):
    for rel in ("in/vendite_01.csv", "in/vendite_02.csv", "in/altro.csv", "in/.vendite_03.csv",
                "in/~$vendite_04.csv", "in/sub/vendite_05.csv"):
        _write(root, rel, "id\n1\n")
    assert [f.path for f in sftp.list_files(_conn(srv), "/in", "vendite_*.csv")] == ["/in/vendite_01.csv", "/in/vendite_02.csv"]


def test_double_star_descends_into_subfolders(srv, root):
    for rel in ("in/a.csv", "in/2026/b.csv", "in/2026/q1/c.csv", "in/2026/q1/c.txt"):
        _write(root, rel, "id\n1\n")
    assert [f.path for f in sftp.list_files(_conn(srv), "in", "**/*.csv")] == ["/in/2026/b.csv", "/in/2026/q1/c.csv", "/in/a.csv"]


def test_a_relative_folder_starts_at_the_login_directory(root):
    _write(root, "home/tab/drop/x.csv", "id\n1\n")
    with FakeSftpServer(root, home="/home/tab") as srv:
        assert [f.path for f in sftp.list_files(_conn(srv), "drop", "*.csv")] == ["/home/tab/drop/x.csv"]


def test_double_star_does_not_follow_a_link_to_a_folder(srv, root):
    """`loop -> .` would make `**` walk the same files again and again."""
    _write(root, "in/a.csv", "id\n1\n")
    _write(root, "in/sub/b.csv", "id\n2\n")
    os.symlink(".", os.path.join(root, "in", "loop"))
    os.symlink("sub", os.path.join(root, "in", "sub-link"))
    assert [f.rel for f in sftp.list_files(_conn(srv), "in", "**/*.csv")] == ["a.csv", "sub/b.csv"]


@pytest.mark.parametrize("folder, pattern", [("in", "../*.csv"), ("in", "**/../x.csv"), ("in/../..", "*.csv")])
def test_dot_dot_is_refused_in_folder_and_pattern(srv, root, folder, pattern):
    _write(root, "in/a.csv", "id\n1\n")
    with pytest.raises(sftp.SftpError, match=r"'\.\.'"):
        sftp.list_files(_conn(srv), folder, pattern)


def test_the_files_together_over_the_total_cap_are_refused_before_any_download(srv, root, monkeypatch):
    monkeypatch.setattr(get_settings().sftp, "max_total_bytes", 1500)
    _write(root, "in/a.csv", "id\n" + "1\n" * 400)
    _write(root, "in/b.csv", "id\n" + "2\n" * 400)
    with pytest.raises(sftp.SftpError, match="2 files total"):
        _ingest(_conn(srv), "in", "*.csv")
    assert not any(p.endswith(".csv") for p in srv.opened)


def test_a_missing_folder_is_an_error_that_names_it(srv):
    with pytest.raises(sftp.SftpError, match="'/nowhere' not found"):
        sftp.list_files(_conn(srv), "/nowhere", "*.csv")


def test_too_many_files_is_refused(srv, root, monkeypatch):
    monkeypatch.setattr(get_settings().sftp, "max_files", 2)
    for i in range(3):
        _write(root, f"in/{i}.csv", "id\n1\n")
    with pytest.raises(sftp.SftpError, match="more than 2 files"):
        sftp.list_files(_conn(srv), "in", "*.csv")


# ── reading and stacking ───────────────────────────────────────────────────────
def _ingest(conn, folder, pattern, **kw):
    st = FakeStorage()
    res = sftp.ingest_sftp_to_parquet(conn, sftp.SftpSourceSpec(folder=folder, pattern=pattern, **kw), "b", "datasets/x.parquet", st)
    return res, pl.read_parquet(io.BytesIO(st.blobs[("b", "datasets/x.parquet")]))


def test_csv_excel_parquet_and_ndjson_are_stacked_with_the_file_they_come_from(srv, root):
    t = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    _write(root, "in/a.csv", "id,importo\n1,10.5\n2,20\n", mtime=t)
    _write(root, "in/b.xlsx", xlsx([["id", "importo"], [3, 30]]), mtime=t)
    _write(root, "in/c.parquet", _parquet(pl.DataFrame({"id": [4], "importo": [40.0]})), mtime=t)
    _write(root, "in/d.ndjson", '{"id": 5, "importo": 50.0}\n', mtime=t)
    res, df = _ingest(_conn(srv), "in", "*")
    assert df.columns == ["id", "importo", "_file", "_file_modified_at"]
    # as on SharePoint: the path from where the glob starts (here the folder), in the
    # second-to-last column, _file_modified_at last
    assert df.sort("id")["_file"].to_list() == ["a.csv", "a.csv", "b.xlsx", "c.parquet", "d.ndjson"]
    assert df["importo"].sum() == pytest.approx(150.5)
    assert df["_file_modified_at"][0] == datetime(2026, 10, 1, 12, 0)
    assert res["rows_written"] == 5 and len(res["files"]) == 4 and res["schema_drift"] == []


def test_every_row_says_its_file_even_when_the_glob_matches_one(srv, root):
    _write(root, "in/vendite_2026-10.csv", "id,v\n1,2\n3,4\n")
    res, df = _ingest(_conn(srv), "/in", "vendite_2026-10.csv")
    assert df.columns == ["id", "v", "_file", "_file_modified_at"]
    assert df["_file"].to_list() == ["vendite_2026-10.csv", "vendite_2026-10.csv"]
    assert res["files"][0]["path"] == "vendite_2026-10.csv"


def test_in_subfolders_the_file_keeps_its_path_below_the_folder(srv, root):
    _write(root, "in/2026/q1/a.csv", "id\n1\n")
    _write(root, "in/b.csv", "id\n2\n")
    _, df = _ingest(_conn(srv), "in", "**/*.csv")
    assert sorted(df["_file"].to_list()) == ["2026/q1/a.csv", "b.csv"]


def test_the_files_stay_on_the_server_untouched(srv, root):
    path = _write(root, "in/a.csv", "id\n1\n")
    before = (os.path.getsize(path), os.path.getmtime(path))
    _ingest(_conn(srv), "in", "*.csv")
    assert os.path.exists(path) and (os.path.getsize(path), os.path.getmtime(path)) == before
    assert srv.renames == []


def test_different_columns_are_united_by_name_and_reported(srv, root):
    _write(root, "in/a.csv", "id,x\n1,a\n")
    _write(root, "in/b.csv", "id,y\n2,b\n")
    res, df = _ingest(_conn(srv), "in", "*.csv")
    assert set(df.columns) == {"id", "x", "y", "_file", "_file_modified_at"}
    assert res["schema_drift"] == ["x", "y"]


def test_csv_options_separator_and_encoding(srv, root):
    _write(root, "in/a.csv", "id;città\n1;Milano\n".encode("latin-1"))
    _, df = _ingest(_conn(srv), "in", "*.csv", csv={"separator": ";", "encoding": "utf8-lossy"})
    assert df.columns[:2] == ["id", "citt�"]


def test_a_chosen_format_reads_every_matching_file_that_way(srv, root):
    _write(root, "in/export.txt", "id|v\n1|2\n")
    _, df = _ingest(_conn(srv), "in", "*.txt", format="csv", csv={"separator": "|"})
    assert df["v"].to_list() == [2]


def test_excel_sheet_by_name_and_first_sheet_by_default(srv, root):
    _write(root, "in/a.xlsx", xlsx([["id"], [1]], sheet="Dati"))
    _, df = _ingest(_conn(srv), "in", "*.xlsx")
    assert df["id"].to_list() == [1]
    with pytest.raises(sftp.SftpError, match="sheet 'Altro' does not exist"):
        _ingest(_conn(srv), "in", "*.xlsx", sheet="Altro")


@pytest.mark.parametrize("content, message", [
    ("id,,v\n1,2,3\n", "columns without a name"),
    ("id,ID\n1,2\n", "repeated column names"),
    ("_file,v\n1,2\n", "reserved"),
])
def test_a_csv_that_is_not_machine_readable_is_refused(srv, root, content, message):
    _write(root, "in/a.csv", content)
    with pytest.raises(sftp.SftpError, match=message):
        _ingest(_conn(srv), "in", "*.csv")


def test_an_excel_with_a_title_above_the_table_is_refused(srv, root):
    _write(root, "in/a.xlsx", xlsx([["Report vendite"], ["id", "v"], [1, 2]]))
    with pytest.raises(sftp.SftpError, match="columns without a name"):
        _ingest(_conn(srv), "in", "*.xlsx")


def test_an_unknown_extension_in_auto_is_refused_with_its_name(srv, root):
    _write(root, "in/a.csv", "id\n1\n")
    _write(root, "in/b.docx", b"x")
    with pytest.raises(sftp.SftpError, match="/in/b.docx"):
        _ingest(_conn(srv), "in", "*")


def test_no_matching_file_is_an_error(srv, root):
    os.makedirs(os.path.join(root, "in"))
    with pytest.raises(sftp.SftpError, match="No file matches"):
        _ingest(_conn(srv), "in", "*.csv")


def test_a_file_over_the_byte_cap_is_refused_with_its_name(srv, root, monkeypatch):
    monkeypatch.setattr(get_settings().sftp, "max_file_bytes", 1000)
    _write(root, "in/small.csv", "id\n1\n")
    _write(root, "in/big.csv", "id\n" + "1\n" * 1000)
    with pytest.raises(sftp.SftpError, match=r"/in/big\.csv.*over the limit"):
        _ingest(_conn(srv), "in", "*.csv")
    assert "/in/big.csv" not in srv.opened, "refused from its size, before downloading it"


def test_the_import_reports_its_progress(srv, root):
    _write(root, "in/a.csv", "id\n1\n2\n")
    _write(root, "in/b.csv", "id\n3\n")
    seen = []
    sftp.ingest_sftp_to_parquet(_conn(srv), sftp.SftpSourceSpec(folder="in", pattern="*.csv"), "b", "k.parquet",
                                FakeStorage(), progress=lambda phase, **c: seen.append((phase, c)))
    assert seen == [("files", {"file": 1, "files": 2, "rows": 0}), ("files", {"file": 2, "files": 2, "rows": 2}),
                    ("saving", {"rows": 3})]


# ── the Output ─────────────────────────────────────────────────────────────────
NOW = datetime(2026, 10, 17, 14, 30, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("template, fmt, expected", [
    ("vendite_{date}", "csv", "vendite_2026-10-17.csv"),
    ("vendite_{datetime}.csv", "csv", "vendite_2026-10-17_143005.csv"),
    ("run_{run_id}", "parquet", "run_42.parquet"),
    ("{flow}_{date}", "xlsx", "Report_mensile_2026-10-17.xlsx"),
    ("Report.XLSX", "xlsx", "Report.XLSX"),
])
def test_the_file_name_placeholders(template, fmt, expected):
    assert sftp.render_file_name(template, fmt, now=NOW, run_id=42, flow="Report/mensile") == expected


@pytest.mark.parametrize("template, message", [
    ("", "empty"),
    ("a/b.csv", "folder"),
    ("..", "file name"),
    ("x_{week}", "{week}"),
    ("x.parquet", "parquet"),
])
def test_a_bad_file_name_is_refused(template, message):
    with pytest.raises(sftp.SftpError, match=message.replace("{", r"\{").replace("}", r"\}")):
        sftp.render_file_name(template, "csv", now=NOW, run_id=1, flow="f")


@pytest.fixture
def output(storage):
    df = pl.DataFrame({"paese": ["IT", "FR"], "vendite": [1.5, 2.0]})
    return upload_df(storage, df, "out/sftp_out.parquet")


def _dest(**kw):
    base = dict(folder="/out", file_name="vendite_{date}_{run_id}", format="csv", run_id=7, flow="F")
    base.update(kw)
    return sftp.SftpDestinationSpec(**base)


@pytest.mark.parametrize("fmt, reader", [
    ("csv", lambda p: pl.read_csv(p)),
    ("parquet", lambda p: pl.read_parquet(p)),
    ("xlsx", lambda p: pl.read_excel(p)),
])
def test_the_output_writes_each_format(srv, root, storage, output, fmt, reader):
    os.makedirs(os.path.join(root, "out"))
    res = sftp.write_output_to_sftp(_conn(srv), _dest(format=fmt), BUCKET, output.key, storage=storage, now=NOW)
    assert res["path"] == f"/out/vendite_2026-10-17_7.{fmt}"
    written = os.path.join(root, "out", f"vendite_2026-10-17_7.{fmt}")
    assert reader(written)["vendite"].to_list() == [1.5, 2.0]
    assert res["rows"] == 2 and res["bytes"] == os.path.getsize(written) and res["format"] == fmt
    assert os.listdir(os.path.join(root, "out")) == [f"vendite_2026-10-17_7.{fmt}"], "no temporary file left behind"


def test_the_output_is_renamed_into_place_from_a_hidden_temporary_name(srv, root, storage, output):
    os.makedirs(os.path.join(root, "out"))
    sftp.write_output_to_sftp(_conn(srv), _dest(), BUCKET, output.key, storage=storage, now=NOW)
    (kind, old, new), = srv.renames
    assert kind == "posix" and new == "/out/vendite_2026-10-17_7.csv"
    assert old.startswith("/out/.vendite_2026-10-17_7.csv.") and old.endswith(".tmp")


def test_an_existing_file_with_the_final_name_is_replaced(srv, root, storage, output):
    _write(root, "out/fixed.csv", "old\n")
    sftp.write_output_to_sftp(_conn(srv), _dest(file_name="fixed.csv"), BUCKET, output.key, storage=storage, now=NOW)
    assert pl.read_csv(os.path.join(root, "out/fixed.csv"))["paese"].to_list() == ["IT", "FR"]


def test_without_posix_rename_the_old_file_is_still_replaced(root, storage, output):
    _write(root, "out/fixed.csv", "old\n")
    with FakeSftpServer(root, posix_rename=False) as srv:
        sftp.write_output_to_sftp(_conn(srv), _dest(file_name="fixed.csv"), BUCKET, output.key, storage=storage, now=NOW)
        assert [k for k, _, _ in srv.renames] == ["rename"]
    assert pl.read_csv(os.path.join(root, "out/fixed.csv"))["paese"].to_list() == ["IT", "FR"]
    assert os.listdir(os.path.join(root, "out")) == ["fixed.csv"]


def test_a_failing_posix_rename_never_deletes_the_existing_file(root, storage, output):
    """Only «unsupported» falls back to remove + rename; any other failure keeps the
    file readers already have, and the run fails."""
    _write(root, "out/fixed.csv", "old\n")
    with FakeSftpServer(root, posix_rename="fail") as srv:
        with pytest.raises(sftp.SftpError):
            sftp.write_output_to_sftp(_conn(srv), _dest(file_name="fixed.csv"), BUCKET, output.key, storage=storage, now=NOW)
    assert open(os.path.join(root, "out/fixed.csv")).read() == "old\n"
    assert os.listdir(os.path.join(root, "out")) == ["fixed.csv"]


def test_dot_dot_is_refused_in_the_destination_folder(srv, root, storage, output):
    os.makedirs(os.path.join(root, "out"))
    with pytest.raises(sftp.SftpError, match=r"'\.\.'"):
        sftp.write_output_to_sftp(_conn(srv), _dest(folder="out/.."), BUCKET, output.key, storage=storage, now=NOW)


def test_a_missing_destination_folder_is_an_error_and_leaves_nothing(srv, root, storage, output):
    with pytest.raises(sftp.SftpError, match="'/out' not found"):
        sftp.write_output_to_sftp(_conn(srv), _dest(), BUCKET, output.key, storage=storage, now=NOW)
    assert os.listdir(root) == []


def test_the_output_also_refuses_a_changed_host_key(srv, root, storage, output):
    os.makedirs(os.path.join(root, "out"))
    with pytest.raises(sftp.SftpHostKeyError):
        sftp.write_output_to_sftp(_conn(srv, host_key_fingerprint="SHA256:other"), _dest(), BUCKET, output.key,
                                  storage=storage, now=NOW)
    assert os.listdir(os.path.join(root, "out")) == [] and srv.auth_attempts == 0


# ── wiring: the task and the routes ────────────────────────────────────────────
def test_transform_data_task_hands_an_sftp_destination_to_the_writer(monkeypatch, storage, output):
    import app.tasks.jobs as jobs
    import app.utils as utils
    from tests.test_mirror_best_effort import _FakeEngine

    monkeypatch.setattr(jobs, "get_engine", lambda name=None: _FakeEngine(output.key))
    monkeypatch.setattr(utils, "get_storage_service", lambda: storage)
    seen = {}

    def fake_write(conn, dest, bucket, key, storage=None, now=None):
        seen.update(conn=conn, dest=dest, key=key)
        return {"path": "/out/x.csv", "bytes": 3, "rows": 2, "format": "csv"}

    monkeypatch.setattr(sftp, "write_output_to_sftp", fake_write)
    out = jobs.transform_data_task(
        bucket=BUCKET, input_key=output.key, operations=[], output_key=output.key,
        destination={"type": "sftp",
                     "connection": {"host": "h", "username": "u", "password_encrypted": encrypt_secret("p"),
                                    "host_key_type": "ssh-ed25519", "host_key_fingerprint": "SHA256:x"},
                     "target": {"folder": "/out", "file_name": "x", "format": "csv", "run_id": 3, "flow": "F"}},
    )
    assert out["destination"]["path"] == "/out/x.csv"
    assert seen["dest"].run_id == 3 and seen["conn"].host == "h" and seen["key"] == output.key


def test_the_ingest_task_runs_the_import(monkeypatch, srv, root):
    import app.tasks.jobs as jobs
    import app.utils as utils

    st = FakeStorage()
    monkeypatch.setattr(utils, "get_storage_service", lambda: st)
    monkeypatch.setattr(jobs, "get_storage_service", lambda: st)
    _write(root, "in/a.csv", "id\n1\n")
    res = jobs.ingest_sftp_task(connection=_conn(srv).model_dump(), source={"folder": "in", "pattern": "*.csv"},
                                bucket="b", output_key="datasets/s.parquet")
    assert res["rows_written"] == 1 and ("b", "datasets/s.parquet") in st.blobs


def test_the_inspect_route_answers_409_with_a_code_for_host_key_problems(srv):
    from fastapi import HTTPException

    from app.api.routes.sftp import SftpInspectRequest, inspect

    conn = _conn(srv, pin=False).model_dump()
    with pytest.raises(HTTPException) as e:
        inspect(SftpInspectRequest(connection=conn, action="files", folder="", pattern="*"))
    assert e.value.status_code == 409
    assert e.value.detail["code"] == "host_key_unknown" and e.value.detail["message"].startswith("SFTP:")
    assert e.value.detail["offered"] == sftp.fingerprint(srv.host_key)
    out = inspect(SftpInspectRequest(connection=conn, action="test"))
    assert out["needs_acceptance"] is True and out["host_key"]["fingerprint"] == sftp.fingerprint(srv.host_key)
    fp = out["host_key"]["fingerprint"]
    out = inspect(SftpInspectRequest(connection=conn, action="test", expected_fingerprint=fp, expected_type=out["host_key"]["type"]))
    assert out["ok"] is True
    with pytest.raises(HTTPException) as e:
        inspect(SftpInspectRequest(connection={**conn, "password_encrypted": encrypt_secret("no")}, action="test",
                                   expected_fingerprint=fp))
    assert e.value.status_code == 400 and "authentication rejected" in e.value.detail
    assert srv.auth_attempts == 2  # only the two accepted tests


# ── the Output's placeholders (the shared language) ────────────────────────────
def test_the_old_placeholders_write_the_same_bytes():
    """{date}, {datetime}, {run_id}, {flow} as the SFTP Output wrote them before the
    shared language (UTC when the node has no time zone)."""
    assert sftp.render_file_name("v_{date}_{datetime}_{run_id}_{flow}", "csv", now=NOW, run_id=3, flow="A b/c") == \
        "v_2026-10-17_2026-10-17_143005_3_A_b_c.csv"


def test_the_output_resolves_in_its_time_zone(srv, root, storage, output):
    os.makedirs(os.path.join(root, "out"))
    res = sftp.write_output_to_sftp(_conn(srv), _dest(file_name="v_{date}_{now:HHmm}", timezone="Asia/Tokyo"),
                                    BUCKET, output.key, storage=storage, now=NOW)
    assert res["path"] == "/out/v_2026-10-17_2330.csv"


def test_a_dated_folder_is_created_when_missing(srv, root, storage, output):
    os.makedirs(os.path.join(root, "out"))
    res = sftp.write_output_to_sftp(_conn(srv), _dest(folder="/out/{today:yyyy}/{today:MM}", file_name="x.csv"),
                                    BUCKET, output.key, storage=storage, now=NOW)
    assert res["path"] == "/out/2026/10/x.csv" and os.path.exists(os.path.join(root, "out/2026/10/x.csv"))


def test_a_value_with_a_slash_cannot_put_a_folder_in_the_file_name():
    with pytest.raises(sftp.SftpError, match="'/'"):
        sftp.render_file_name("{today:yyyy/MM}.csv", "csv", now=NOW, run_id=1, flow="f")


def test_a_folder_value_cannot_go_up(srv, root, storage, output):
    with pytest.raises(sftp.SftpError, match=r"'\.\.'"):
        sftp.write_output_to_sftp(_conn(srv), _dest(folder="/out/{flow}/..", file_name="x.csv"), BUCKET, output.key,
                                  storage=storage, now=NOW)


def test_a_dated_folder_needs_its_fixed_part_to_exist(srv, root, storage, output):
    """`/outbx/{today:yyyy}` with a typo in the fixed part is an error, not a new folder."""
    with pytest.raises(sftp.SftpError, match="not found"):
        sftp.write_output_to_sftp(_conn(srv), _dest(folder="/outbx/{today:yyyy}", file_name="x.csv"), BUCKET, output.key,
                                  storage=storage, now=NOW)
    assert not os.path.exists(os.path.join(root, "outbx"))


def test_a_file_name_over_255_bytes_is_refused():
    with pytest.raises(sftp.SftpError, match="255"):
        sftp.render_file_name("é" * 126 + "{date}", "csv", now=NOW, run_id=1, flow="f")


def test_a_long_file_name_without_placeholders_is_left_to_the_server_as_before():
    assert sftp.render_file_name("x" * 300, "csv", now=NOW, run_id=1, flow="f") == "x" * 300 + ".csv"
