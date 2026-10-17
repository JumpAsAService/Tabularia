"""SFTP: files in a folder of a remote server, read as a datasource («SFTP folder»)
and written by an Output node.

Reading follows the SharePoint datasource: a folder and a glob (`vendite_*.csv`),
every matching file read at each refresh and stacked into one table. Every row
says the file it comes from in `_file` — always, a single matching file too —
as the path below the folder (`vendite_01.csv`, `2026/q1/a.csv` with `**`), just
as SharePoint writes the path below the library root; `_file_modified_at` after
it, the two last columns. The files are left where they are — the
server is only read. The contract on the files is the same strict one: they must
be MACHINE READABLE (a header in the first row, one name per column, no
repeats), and anything else is refused, naming the file, instead of being
guessed at.

Writing puts the output of a run in a folder of the server under a name built
from a template (`vendite_{date}.csv`): first under a hidden temporary name in
the same folder, then renamed onto the final one, so whoever polls the folder
never reads half a file. An existing file with the final name is replaced.

THE HOST KEY. SSH has no certificate authority: what proves the server is the
right one is its key. The connection keeps the key's fingerprint, accepted by
trust on first use in two steps: a «Test connection» SHOWS the key the server
offers and closes without authenticating; the user compares it and accepts it,
and only that second call — naming the key — sends the credentials, if the
server still offers exactly that key. Every other connection checks the
accepted key BEFORE sending any credential: a different key (of the same type or
another) is refused, a server reinstalled or someone in between. No path sends a
secret to a key that is neither the accepted one nor the one being accepted.

Credentials arrive encrypted (Fernet, shared with the gateway) and are decrypted
only to authenticate: the password, or a private key (PEM or OpenSSH; Ed25519,
ECDSA, RSA) with its optional passphrase.

Every message for the user starts with «SFTP:», the prefix the gateway knows
(it never replaces such a message with its own, e.g. the out-of-memory one).
"""
from __future__ import annotations

import base64
import errno
import fnmatch
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shutil
import socket
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Literal
from uuid import uuid4

import polars as pl
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.ingest.converters import IngestError
from app.ingest.models import CsvOptions
from app.ingest.sharepoint_source import FILE_COLUMN, MODIFIED_COLUMN, check_header, read_sheet

logger = logging.getLogger(__name__)

PREFIX = "SFTP: "
# folders a glob may walk through: `**` on a big tree must stop, not list it all
MAX_FOLDERS = 2000
# the Excel format holds 1,048,576 rows, one of them the header
XLSX_MAX_ROWS = 1_048_575

# the SSH host key algorithms a client can ask for, per key type: an RSA key is
# offered under its SHA-2 signature names
_KEY_ALGORITHMS = {"ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")}


class SftpError(IngestError):
    """A message for the user (it ends up in the run or in the response)."""

    def __init__(self, message: str):
        super().__init__(message if message.startswith(PREFIX) else PREFIX + message)


class SftpHostKeyError(SftpError):
    """The server's key is not the accepted one, or none was ever accepted.
    `code`: host_key_changed | host_key_unknown — the gateway translates by it."""

    def __init__(self, message: str, code: str, offered: str = "", expected: str = ""):
        super().__init__(message)
        self.code = code
        self.offered = offered
        self.expected = expected


# ── connection ─────────────────────────────────────────────────────────────────
class SftpConnectionSpec(BaseModel):
    host: str
    port: int = 22
    username: str = ""
    # which secret authenticates: the password, or the private key
    auth: Literal["password", "key"] = "password"
    password_encrypted: str = ""
    # Fernet of {"private_key": "...", "passphrase": "..."}
    private_key_encrypted: str = ""
    # the accepted host key (trust on first use); empty = never accepted
    host_key_type: str = ""
    host_key_fingerprint: str = ""
    timeout_seconds: float = 0  # 0 = SFTP__TIMEOUT_SECONDS

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def timeout(self) -> float:
        return self.timeout_seconds or get_settings().sftp.timeout_seconds


def fingerprint(key) -> str:
    """The OpenSSH SHA256 fingerprint (`ssh-keygen -lf`): what an administrator
    reads on the server to compare it, e.g. `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _load_private_key(conn: SftpConnectionSpec):
    import paramiko

    from app.core.crypto import decrypt_secret

    if not conn.private_key_encrypted:
        raise SftpError("the connection has no private key: add one, or use the password")
    raw = decrypt_secret(conn.private_key_encrypted)
    try:
        data = json.loads(raw)
        text, passphrase = str(data.get("private_key") or ""), str(data.get("passphrase") or "")
    except (json.JSONDecodeError, AttributeError):
        text, passphrase = raw, ""
    text = text.strip().replace("\r\n", "\n") + "\n"
    if "PRIVATE KEY" not in text:
        raise SftpError("the stored text is not a private key (expected a PEM or OpenSSH private key)")
    encrypted_hint = False
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            encrypted_hint = True
        except (paramiko.SSHException, ValueError, TypeError) as e:
            if passphrase and any(w in str(e).lower() for w in ("decrypt", "password", "passphrase", "bad")):
                encrypted_hint = True
    if encrypted_hint and not passphrase:
        raise SftpError("the private key is protected by a passphrase: enter it in the connection")
    if encrypted_hint or passphrase:
        raise SftpError("cannot read the private key: wrong passphrase, or a key type that is not supported (Ed25519, ECDSA and RSA are)")
    raise SftpError("cannot read the private key: unsupported format (Ed25519, ECDSA and RSA keys, PEM or OpenSSH, are)")


def _connect(conn: SftpConnectionSpec, key_type: str = ""):
    """TCP + SSH key exchange, asking for `key_type` when given. Returns the
    transport and the server's key; no credential is involved."""
    import paramiko

    timeout = conn.timeout()
    try:
        sock = socket.create_connection((conn.host, conn.port), timeout=timeout)
    except socket.gaierror as e:
        raise SftpError(f"cannot resolve the host name '{conn.host}' ({e.strerror or e})") from e
    except ConnectionRefusedError as e:
        raise SftpError(f"{conn.address} refused the connection: is an SSH server listening on this port?") from e
    except (socket.timeout, TimeoutError) as e:
        raise SftpError(f"{conn.address} did not answer within {timeout:g} s") from e
    except OSError as e:
        raise SftpError(f"cannot reach {conn.address}: {e.strerror or e}") from e
    transport = paramiko.Transport(sock)
    transport.banner_timeout = timeout
    transport.handshake_timeout = timeout
    transport.auth_timeout = timeout
    if key_type:
        # ask for that key's type: a server has one key per type, and which one it
        # presents depends on the client's preferences, which can change
        opts = transport.get_security_options()
        wanted = _KEY_ALGORITHMS.get(key_type, (key_type,))
        usable = [a for a in opts.key_types if a in wanted]
        if usable:
            opts.key_types = usable
    try:
        transport.start_client(timeout=timeout)
    except paramiko.IncompatiblePeer:
        transport.close()
        raise
    except (paramiko.SSHException, EOFError, OSError) as e:
        transport.close()
        raise SftpError(f"the SSH handshake with {conn.address} failed: {e or type(e).__name__}") from e
    return transport, transport.get_remote_server_key()


def _offered_key(conn: SftpConnectionSpec, key_type: str):
    """The key the server presents, asking first for `key_type`. A server that no
    longer has a key of that type answers «incompatible»: then it is asked for
    any key, which is how a changed key of another type is seen at all."""
    import paramiko

    if key_type:
        try:
            return _connect(conn, key_type)
        except paramiko.IncompatiblePeer:
            logger.info("sftp %s: no %s host key any more, reading the one it offers", conn.address, key_type)
    try:
        return _connect(conn)
    except paramiko.IncompatiblePeer as e:
        raise SftpError(f"the SSH handshake with {conn.address} failed: {e}") from e


def _handshake(conn: SftpConnectionSpec, verify: bool):
    """TCP + SSH up to the server's key, and the key check. No credential has
    been sent yet when this returns or raises."""
    transport, key = _offered_key(conn, conn.host_key_type if verify else "")
    offered = fingerprint(key)
    if verify:
        if not conn.host_key_fingerprint:
            transport.close()
            raise SftpHostKeyError(
                f"the host key of {conn.address} has not been accepted yet: run «Test connection» on the "
                f"connection to see it and accept it (the server offers {key.get_name()} {offered})",
                "host_key_unknown", offered=offered,
            )
        if offered != conn.host_key_fingerprint:
            transport.close()
            raise SftpHostKeyError(
                f"the host key of {conn.address} has changed: accepted {conn.host_key_fingerprint}, "
                f"offered now {key.get_name()} {offered}. Nothing was sent to the server. If the change is "
                "expected (the server was reinstalled or its key renewed), run «Test connection» again to "
                "accept the new key; otherwise ask the server's administrator.",
                "host_key_changed", offered=offered, expected=conn.host_key_fingerprint,
            )
    return transport, key


def _authenticate(transport, conn: SftpConnectionSpec) -> None:
    import paramiko

    from app.core.crypto import decrypt_secret

    what = "private key" if conn.auth == "key" else "password"
    try:
        if conn.auth == "key":
            transport.auth_publickey(conn.username, _load_private_key(conn))
        else:
            password = decrypt_secret(conn.password_encrypted) if conn.password_encrypted else ""
            transport.auth_password(conn.username, password)
    except paramiko.BadAuthenticationType as e:
        allowed = ", ".join(e.allowed_types or []) or "none"
        raise SftpError(
            f"{conn.address} does not accept the {what} for user '{conn.username}': it accepts {allowed}"
        ) from e
    except paramiko.AuthenticationException as e:
        raise SftpError(
            f"authentication rejected by {conn.address} for user '{conn.username}': check the {what}"
        ) from e
    except paramiko.SSHException as e:
        raise SftpError(f"authentication with {conn.address} failed: {e}") from e
    if not transport.is_authenticated():
        raise SftpError(f"{conn.address} asks for more than the {what} (multi-factor login is not supported)")


def _sftp_client(transport, conn: SftpConnectionSpec):
    """The SFTP session, with a timeout on every answer: a server that accepted the
    login and then stops answering must not hold a worker (or the API thread of
    «Check files») forever."""
    import paramiko

    try:
        client = paramiko.SFTPClient.from_transport(transport)
    except (paramiko.SSHException, EOFError, OSError) as e:
        raise SftpError(f"{conn.address} accepts SSH but does not offer SFTP: {e}") from e
    client.get_channel().settimeout(conn.timeout())
    return client


@contextmanager
def open_sftp(conn: SftpConnectionSpec, verify: bool = True) -> Iterator[Any]:
    """An SFTP client on a verified, authenticated connection, closed on exit."""
    transport, _key = _handshake(conn, verify)
    try:
        _authenticate(transport, conn)
        client = _sftp_client(transport, conn)
        try:
            yield client
        finally:
            client.close()
    finally:
        transport.close()


def inspect_connection(conn: SftpConnectionSpec, expected_fingerprint: str = "", expected_type: str = "") -> dict[str, Any]:
    """«Test connection», in two steps, and no credential ever goes to a key that is
    not accepted.

    The key the server offers is compared with the ACCEPTABLE one: the key the
    caller is accepting now (`expected_fingerprint`, the one the user was shown),
    or else the connection's accepted key. If it is not that key — never accepted,
    changed, or not the one the user saw — the connection is closed WITHOUT
    authenticating and the answer says `needs_acceptance` with the key offered
    (and the previous one): the user compares it and accepts it, which is a
    second call with `expected_fingerprint`. Only a matching key goes on to the
    credentials and SFTP; the gateway stores an accepted key only after that."""
    acceptable = expected_fingerprint or conn.host_key_fingerprint
    transport, key = _offered_key(conn, expected_type if expected_fingerprint else conn.host_key_type)
    offered = fingerprint(key)
    previous = conn.host_key_fingerprint
    answer: dict[str, Any] = {
        "host_key": {"type": key.get_name(), "fingerprint": offered},
        "changed": bool(previous) and previous != offered,
        "previous": previous,
    }
    if offered != acceptable:
        transport.close()
        return {"ok": False, "needs_acceptance": True, **answer}
    try:
        _authenticate(transport, conn)
        client = _sftp_client(transport, conn)
        try:
            home = client.normalize(".")
        except OSError as e:
            raise _io_error(e, "the login folder") from e
        finally:
            client.close()
    finally:
        transport.close()
    return {"ok": True, "needs_acceptance": False, **answer, "home": home}


# ── folders and the glob ───────────────────────────────────────────────────────
@dataclass
class RemoteFile:
    path: str  # absolute on the server
    size: int
    mtime: float
    # the path below the datasource's folder: what `_file` holds, as on SharePoint
    # (where it is the path from the library root the glob starts at)
    rel: str = ""

    @property
    def modified_at(self) -> str:
        return datetime.fromtimestamp(self.mtime, timezone.utc).isoformat() if self.mtime else ""


def _io_error(e: OSError, what: str) -> SftpError:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return SftpError(f"{what}: the server did not answer in time (SFTP__TIMEOUT_SECONDS)")
    if e.errno == errno.ENOENT:
        return SftpError(f"{what} not found on the server")
    if e.errno == errno.EACCES:
        return SftpError(f"permission denied on {what}")
    return SftpError(f"{what}: {e.strerror or e}")


def resolve_folder(client, folder: str) -> str:
    """The folder as an absolute path; a relative one starts at the login
    directory, as in any SFTP client. It must exist and be a folder."""
    folder = (folder or "").strip().replace("\\", "/")
    _no_dot_dot(folder, "folder")
    try:
        home = client.normalize(".")
    except OSError as e:
        raise _io_error(e, "the login folder") from e
    path = posixpath.normpath(posixpath.join(home, folder)) if folder else home
    try:
        attr = client.stat(path)
    except OSError as e:
        raise _io_error(e, f"folder '{path}'") from e
    if not stat.S_ISDIR(attr.st_mode or 0):
        raise SftpError(f"'{path}' is not a folder")
    return path


def _no_dot_dot(path: str, what: str) -> None:
    """A folder or a pattern says where it is: going up with '..' is refused, not
    resolved — it is how a pattern reaches outside the folder it names."""
    if ".." in (path or "").replace("\\", "/").split("/"):
        raise SftpError(f"'..' is not allowed in the {what} ('{path}'): write the folder you mean")


def _hidden(name: str) -> bool:
    # dot files (also the temporary names of our own writes) and Office lock files
    return name.startswith(".") or name.startswith("~$")


def find_files(client, folder: str, pattern: str) -> list[RemoteFile]:
    """The files under `folder` that match `pattern`, segment by segment: `*` and
    `?` within a name, `**` for any depth (zero included). Case-sensitive, as
    file systems on servers are. Hidden files are left out."""
    max_files = get_settings().sftp.max_files
    _no_dot_dot(pattern, "file pattern")
    base = resolve_folder(client, folder)
    parts = [p for p in (pattern or "").replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        raise SftpError("empty file pattern: enter a name or a glob, e.g. vendite_*.csv")
    listed = 0
    found: dict[str, RemoteFile] = {}

    def listing(path: str) -> list:
        nonlocal listed
        listed += 1
        if listed > MAX_FOLDERS:
            raise SftpError(f"the pattern goes through more than {MAX_FOLDERS} folders: narrow it")
        try:
            return client.listdir_attr(path)
        except OSError as e:
            if path != base and e.errno in (errno.ENOENT, errno.EACCES):
                return []  # a subfolder that vanished or is closed: nothing matches there
            raise _io_error(e, f"folder '{path}'") from e

    def kind(path: str, attr) -> tuple[bool, bool, Any]:
        mode = attr.st_mode or 0
        if stat.S_ISLNK(mode):
            try:
                attr = client.stat(path)  # follow the link; a broken one is skipped
            except OSError:
                return False, False, attr
            mode = attr.st_mode or 0
        return stat.S_ISDIR(mode), stat.S_ISREG(mode), attr

    def walk(path: str, rest: list[str]) -> None:
        if not rest:
            return
        seg, tail = rest[0], rest[1:]
        if seg == "**":
            walk(path, tail)
            for attr in listing(path):
                # real folders only: a link to a folder (`loop -> .`) would make `**`
                # walk the same tree again and again
                if _hidden(attr.filename) or not stat.S_ISDIR(attr.st_mode or 0):
                    continue
                walk(posixpath.join(path, attr.filename), rest)
            return
        if tail and not any(c in seg for c in "*?["):
            walk(posixpath.join(path, seg), tail)  # a fixed segment: no listing
            return
        for attr in listing(path):
            name = attr.filename
            if _hidden(name) or not fnmatch.fnmatchcase(name, seg):
                continue
            child = posixpath.join(path, name)
            is_dir, is_file, attr = kind(child, attr)
            if tail:
                if is_dir:
                    walk(child, tail)
            elif is_file:
                found[child] = RemoteFile(path=child, size=int(attr.st_size or 0), mtime=float(attr.st_mtime or 0),
                                          rel=posixpath.relpath(child, base))
                if len(found) > max_files:
                    raise SftpError(f"the pattern matches more than {max_files} files: narrow it")

    walk(base, parts)
    return [found[k] for k in sorted(found)]


def list_files(conn: SftpConnectionSpec, folder: str, pattern: str) -> list[RemoteFile]:
    with open_sftp(conn) as client:
        return find_files(client, folder, pattern)


# ── reading ────────────────────────────────────────────────────────────────────
SftpFormat = Literal["auto", "csv", "parquet", "excel", "ndjson"]
_FORMAT_BY_EXTENSION = {
    ".csv": "csv", ".txt": "csv", ".tsv": "tsv",
    ".parquet": "parquet", ".pq": "parquet",
    ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel",
    ".ndjson": "ndjson", ".jsonl": "ndjson",
}


class SftpSourceSpec(BaseModel):
    folder: str = ""  # empty = the login directory
    pattern: str = Field(min_length=1)
    # auto = from each file's extension (a glob may mix formats)
    format: SftpFormat = "auto"
    csv: CsvOptions = Field(default_factory=CsvOptions)
    sheet: str = ""  # Excel: empty = the first sheet


def _format_of(path: str, chosen: str) -> str:
    if chosen != "auto":
        return chosen
    fmt = _FORMAT_BY_EXTENSION.get(posixpath.splitext(path)[1].lower())
    if fmt is None:
        raise SftpError(
            f"{path}: the extension does not say the format. Narrow the pattern to the data files, or choose "
            f"the format on the datasource (recognised: {', '.join(sorted(_FORMAT_BY_EXTENSION))})"
        )
    return fmt


def _strip_names(df: pl.DataFrame) -> pl.DataFrame:
    return df.rename({c: c.strip() for c in df.columns if c != c.strip()})


def _read_csv(local: str, shown_as: str, opts: CsvOptions, separator: str) -> pl.DataFrame:
    common = dict(separator=separator, encoding=opts.encoding)
    if opts.has_header:
        try:
            head = pl.read_csv(local, has_header=False, n_rows=1, infer_schema_length=0, **common)
        except pl.exceptions.NoDataError as e:
            raise SftpError(f"{shown_as}: the file is empty") from e
        except Exception as e:
            raise SftpError(f"{shown_as}: unreadable CSV file ({e})") from e
        if head.height == 0:
            raise SftpError(f"{shown_as}: the file is empty")
        check_header(list(head.row(0)), shown_as, "the first row", SftpError)
    try:
        df = pl.read_csv(
            local, has_header=opts.has_header, infer_schema_length=opts.infer_schema_length,
            null_values=opts.null_values, try_parse_dates=opts.try_parse_dates, **common,
        )
    except Exception as e:
        raise SftpError(f"{shown_as}: unreadable CSV file ({e})") from e
    return _strip_names(df)


def _read_parquet(local: str, shown_as: str) -> pl.DataFrame:
    import pyarrow.parquet as pq

    try:
        names = pq.read_schema(local).names
    except Exception as e:
        raise SftpError(f"{shown_as}: unreadable parquet file ({e})") from e
    check_header(names, shown_as, "the schema", SftpError)
    return pl.read_parquet(local)


def _read_ndjson(local: str, shown_as: str, opts: CsvOptions) -> pl.DataFrame:
    try:
        df = pl.read_ndjson(local, infer_schema_length=opts.infer_schema_length or None)
    except Exception as e:
        raise SftpError(f"{shown_as}: unreadable NDJSON file ({e})") from e
    check_header(df.columns, shown_as, "the keys", SftpError)
    return df


def read_file(local: str, shown_as: str, fmt: str, source: SftpSourceSpec) -> pl.DataFrame:
    if fmt in ("csv", "tsv"):
        return _read_csv(local, shown_as, source.csv, "\t" if fmt == "tsv" else source.csv.separator)
    if fmt == "parquet":
        return _read_parquet(local, shown_as)
    if fmt == "excel":
        return read_sheet(local, source.sheet.strip() or None, shown_as, SftpError)
    if fmt == "ndjson":
        return _read_ndjson(local, shown_as, source.csv)
    raise SftpError(f"unsupported format '{fmt}'")  # pragma: no cover


def ingest_sftp_to_parquet(
    conn: SftpConnectionSpec,
    source: SftpSourceSpec,
    bucket: str,
    key: str,
    storage,
    progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Reads every matching file, stacks them and writes the parquet. `progress`
    as for SharePoint: «files» at each file, «saving» while the parquet goes up."""
    report = progress or (lambda *a, **k: None)
    cap = get_settings().sftp.max_file_bytes
    with open_sftp(conn) as client:
        files = find_files(client, source.folder, source.pattern)
        if not files:
            raise SftpError(f"No file matches '{source.pattern}' in '{resolve_folder(client, source.folder)}'")
        # every size is checked BEFORE downloading anything: a refresh that would
        # fail on the tenth file should not have downloaded nine first
        formats = [_format_of(f.path, source.format) for f in files]
        total, total_cap = sum(f.size for f in files), get_settings().sftp.max_total_bytes
        if total > total_cap:
            raise SftpError(
                f"the {len(files)} files total {total / 1024**2:.1f} MB, over the limit of {total_cap / 1024**2:.0f} MB "
                "per refresh (SFTP__MAX_TOTAL_BYTES): narrow the pattern"
            )
        big = [f for f in files if f.size > cap]
        if big:
            f = big[0]
            raise SftpError(
                f"{f.path} is {f.size / 1024**2:.1f} MB, over the limit of {cap / 1024**2:.0f} MB per file "
                "(SFTP__MAX_FILE_BYTES): narrow the pattern, or split the file at the source"
            )

        frames: list[pl.DataFrame] = []
        columns_by_file: dict[str, list[str]] = {}
        rows_read = 0
        with tempfile.TemporaryDirectory(prefix="sftp_in_") as tmp:
            for i, (f, fmt) in enumerate(zip(files, formats)):
                report("files", file=i + 1, files=len(files), rows=rows_read)
                local = os.path.join(tmp, f"{i}{posixpath.splitext(f.path)[1]}")

                def guard(done: int, _total: int, path=f.path) -> None:
                    if done > cap:  # the file grew after it was listed
                        raise SftpError(f"{path} grew over the limit of {cap / 1024**2:.0f} MB per file while being read")

                try:
                    client.get(f.path, local, callback=guard)
                except SftpError:
                    raise
                except OSError as e:
                    raise _io_error(e, f"file '{f.path}'") from e
                df = read_file(local, f.path, fmt, source)
                os.remove(local)
                columns_by_file[f.path] = df.columns
                rows_read += df.height
                if df.height == 0:
                    # header only: typed all as text, it would drag every numeric
                    # column of the other files to text in the relaxed union
                    continue
                modified = datetime.fromtimestamp(f.mtime, timezone.utc).replace(tzinfo=None) if f.mtime else None
                frames.append(df.with_columns(
                    # ALWAYS, one file or many: which file a number comes from, and
                    # often the month or the branch live only in its name
                    pl.lit(f.rel or f.path).alias(FILE_COLUMN),
                    pl.lit(modified, dtype=pl.Datetime("us")).alias(MODIFIED_COLUMN),
                ))
            if not frames:
                raise SftpError("All the matching files are empty (header only)")
            try:
                table = pl.concat(frames, how="diagonal_relaxed")
            except Exception as e:
                raise SftpError(f"The files have columns with the same name but incompatible types: {e}") from e
            prov = [FILE_COLUMN, MODIFIED_COLUMN]
            table = table.select([c for c in table.columns if c not in prov] + prov)
            every = set.intersection(*(set(c) for c in columns_by_file.values()))
            drift = sorted({c for cols in columns_by_file.values() for c in cols} - every)
            if drift:
                logger.warning("sftp %s/%s: columns not in every file: %s", source.folder, source.pattern, drift)

            report("saving", rows=table.height)
            out = os.path.join(tmp, "out.parquet")
            table.write_parquet(out, compression="zstd", row_group_size=get_settings().ingest.parquet_row_group_rows)
            storage.upload_file(out, bucket, key)

    return {
        "status": "success",
        "bucket": bucket,
        "output_key": key,
        "rows_written": table.height,
        "columns": [{"name": n, "dtype": str(t)} for n, t in table.schema.items()],
        "files": [{"path": f.rel or f.path, "modified_at": f.modified_at, "size": f.size} for f in files],
        "schema_drift": drift,
    }


# ── writing (Output node) ──────────────────────────────────────────────────────
OutputFormat = Literal["csv", "parquet", "xlsx"]
_EXTENSION = {"csv": ".csv", "parquet": ".parquet", "xlsx": ".xlsx"}
_DATA_EXTENSIONS = (".csv", ".tsv", ".txt", ".parquet", ".pq", ".xlsx", ".xlsm", ".xls", ".json", ".ndjson", ".jsonl")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class SftpDestinationSpec(BaseModel):
    folder: str = ""  # empty = the login directory; it must exist
    file_name: str  # template: {date}, {datetime}, {run_id}, {flow}
    format: OutputFormat = "csv"
    # filled in by the gateway: the run's number and the flow's name
    run_id: int | None = None
    flow: str = ""


def render_file_name(template: str, fmt: str, now: datetime, run_id: int | None, flow: str) -> str:
    """The final file name. Placeholders in UTC: {date} = 2026-10-17,
    {datetime} = 2026-10-17_143005; {run_id} = the run's number; {flow} = the
    flow's name made safe for a file name. The format's extension is added when
    missing; a name ending in ANOTHER data format's extension is refused (a CSV
    called report.parquet would break whoever reads it)."""
    raw = (template or "").strip()
    if not raw:
        raise SftpError("the file name is empty")
    if "/" in raw or "\\" in raw:
        raise SftpError("the file name cannot contain a folder: put the folder in its own field")

    def value(m: re.Match) -> str:
        name = m.group(1)
        if name == "date":
            return now.strftime("%Y-%m-%d")
        if name == "datetime":
            return now.strftime("%Y-%m-%d_%H%M%S")
        if name == "run_id":
            if run_id is None:
                raise SftpError("{run_id} is only known when the flow runs")
            return str(run_id)
        if name == "flow":
            return re.sub(r"[^\w.-]+", "_", flow or "").strip("._") or "flow"
        raise SftpError(f"unknown placeholder {{{name}}} in the file name: use {{date}}, {{datetime}}, {{run_id}} or {{flow}}")

    name = _PLACEHOLDER.sub(value, raw)
    if name in (".", "..") or not name.strip(". "):
        raise SftpError(f"invalid file name '{name}'")
    ext = _EXTENSION[fmt]
    lower = name.lower()
    if lower.endswith(ext):
        return name
    other = next((e for e in _DATA_EXTENSIONS if lower.endswith(e)), None)
    if other:
        raise SftpError(f"the file name ends in {other} but the format is {fmt}: change one of the two")
    return name + ext


def _render_local(src: str, fmt: str, workdir: str) -> str:
    """The file to upload, written from the run's parquet with the same calls as
    the editor's download (CSV streamed; Excel capped by its format)."""
    if fmt == "parquet":
        return src  # already parquet: uploaded as it is
    out = os.path.join(workdir, f"out.{fmt}")
    lf = pl.scan_parquet(src)
    if fmt == "csv":
        lf.sink_csv(out)
        return out
    n = lf.select(pl.len()).collect(engine="streaming").item()
    if n > XLSX_MAX_ROWS:
        raise SftpError(f"the output has {n:,} rows: over the limit of the Excel format (~1,048,575). Choose CSV or parquet.")
    lf.collect(engine="streaming").write_excel(out)
    return out


def _replace(client, tmp: str, final: str) -> None:
    """Renames the temporary file onto the final name, replacing an existing one.
    The OpenSSH extension does it atomically; a server without it gets the plain
    SFTP rename, which refuses an existing target: the old file is removed first
    (a short moment without the file, never half a file)."""
    try:
        client.posix_rename(tmp, final)
        return
    except OSError as e:
        # ONLY a server that does not know the extension falls back: any other failure
        # (a folder with that name, permissions…) must leave the readers' file alone
        unsupported = e.errno in (errno.ENOSYS, errno.EOPNOTSUPP) or (e.errno is None and "unsupported" in str(e).lower())
        if not unsupported:
            raise
        logger.info("sftp: posix-rename not offered (%s), plain rename", e)
    try:
        client.remove(final)
    except FileNotFoundError:
        pass
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise
    client.rename(tmp, final)


def write_output_to_sftp(
    conn: SftpConnectionSpec,
    dest: SftpDestinationSpec,
    bucket: str,
    key: str,
    storage=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Writes `bucket/key` (the run's parquet) to the server. Raises SftpError
    with a readable message; the run fails with it."""
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()
    name = render_file_name(dest.file_name, dest.format, now or datetime.now(timezone.utc), dest.run_id, dest.flow)
    _no_dot_dot(dest.folder, "folder")
    workdir = tempfile.mkdtemp(prefix="sftp_out_")
    try:
        src = os.path.join(workdir, "in.parquet")
        storage.download_file(bucket, key, src)
        rows = int(pl.scan_parquet(src).select(pl.len()).collect(engine="streaming").item())
        local = _render_local(src, dest.format, workdir)
        size = os.path.getsize(local)
        with open_sftp(conn) as client:
            folder = resolve_folder(client, dest.folder)
            final = posixpath.join(folder, name)
            # hidden, in the SAME folder (a rename across folders may not be atomic)
            tmp = posixpath.join(folder, f".{name}.{uuid4().hex[:12]}.tmp")
            try:
                client.put(local, tmp, confirm=True)
                _replace(client, tmp, final)
            except Exception as e:
                try:
                    client.remove(tmp)
                except Exception:  # noqa: BLE001 — already gone, or the connection with it
                    pass
                if isinstance(e, SftpError):
                    raise
                if isinstance(e, OSError):
                    raise _io_error(e, f"writing '{final}'") from e
                raise SftpError(f"writing '{final}' failed: {type(e).__name__}: {e}") from e
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    logger.info("sftp-dest done: %s %s (%d rows, %d bytes, %s)", conn.address, final, rows, size, dest.format)
    return {"host": conn.address, "path": final, "bytes": size, "rows": rows, "format": dest.format}
