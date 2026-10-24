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
from app.core.placeholders import has_placeholders
from app.ingest import file_readers
from app.ingest import file_templates as ft
from app.ingest.converters import IngestError
from app.ingest.models import CsvOptions
from app.ingest.sheets import sheets_of_first
from app.ingest.sharepoint_source import FILE_COLUMN, MODIFIED_COLUMN

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


def find_files(client, folder: str, pattern: str, rel_to: str | None = None, missing_ok: bool = False) -> list[RemoteFile]:
    """The files under `folder` that match `pattern`, segment by segment: `*` and
    `?` within a name, `**` for any depth (zero included). Case-sensitive, as
    file systems on servers are. Hidden files are left out. `rel` is the path
    below `rel_to` (default: the folder); `missing_ok`: a folder that does not
    exist matches nothing instead of being an error (a dated folder)."""
    max_files = get_settings().sftp.max_files
    _no_dot_dot(pattern, "file pattern")
    if missing_ok:
        _no_dot_dot(folder, "folder")
        try:
            home = client.normalize(".")
            path = posixpath.normpath(posixpath.join(home, (folder or "").strip().replace("\\", "/")))
            if not stat.S_ISDIR(client.stat(path).st_mode or 0):
                return []
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                return []
            raise _io_error(e, f"folder '{folder}'") from e
    base = resolve_folder(client, folder)
    rel_base = base if rel_to is None else rel_to
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
                                          rel=posixpath.relpath(child, rel_base))
                if len(found) > max_files:
                    raise SftpError(f"the pattern matches more than {max_files} files: narrow it")

    walk(base, parts)
    return [found[k] for k in sorted(found)]


def list_files(conn: SftpConnectionSpec, folder: str, pattern: str) -> list[RemoteFile]:
    with open_sftp(conn) as client:
        return find_files(client, folder, pattern)


# ── reading ────────────────────────────────────────────────────────────────────
SftpFormat = Literal["auto", "csv", "parquet", "excel", "ndjson"]


class SftpSourceSpec(ft.FileTemplates):
    """Folder and pattern may hold placeholders and the datasource's variables
    (see file_templates): the timezone, variables, last_refresh_initial, mode and
    keys come from there."""
    folder: str = ""  # empty = the login directory
    pattern: str = Field(min_length=1)
    # auto = from each file's extension (a glob may mix formats)
    format: SftpFormat = "auto"
    csv: CsvOptions = Field(default_factory=CsvOptions)
    sheet: str = ""  # Excel: empty = the first sheet


def _format_of(path: str, chosen: str) -> str:
    return file_readers.format_of(path, chosen, SftpError)


def read_file(local: str, shown_as: str, fmt: str, source: SftpSourceSpec) -> pl.DataFrame:
    return file_readers.read_file(local, shown_as, fmt, source.csv, source.sheet, SftpError)


def _expansions(source: SftpSourceSpec, now: datetime | None, last_refresh: str | None) -> list[ft.Expansion]:
    _no_dot_dot(source.folder, "folder")
    _no_dot_dot(source.pattern, "file pattern")
    exps = ft.expand({"folder": source.folder, "pattern": source.pattern}, source, SftpError, now, last_refresh)
    for e in exps:  # a value cannot take the pattern out of its folder either
        _no_dot_dot(e.values["folder"], "folder")
        _no_dot_dot(e.values["pattern"], "file pattern")
    return exps


def validate_source(source: SftpSourceSpec, now: datetime | None = None, last_refresh: str | None = None) -> dict[str, Any]:
    """Everything that can be said without the server: the placeholders, the
    variables, the keys of the append, how many expansions; the first and last
    one resolved."""
    exps = _expansions(source, now, last_refresh)
    return {"ok": True, "expansions": len(exps), "first": exps[0].values, "last": exps[-1].values}


def _gather(client, source: SftpSourceSpec, now: datetime | None, last_refresh: str | None) -> tuple[ft.Gathered, list[ft.Expansion]]:
    exps = _expansions(source, now, last_refresh)
    dated_folder = has_placeholders(source.folder)
    rel_to = None
    if dated_folder:
        # `_file` below the folder's fixed part: with `in/{day}` it keeps the day
        home = client.normalize(".")
        rel_to = posixpath.normpath(posixpath.join(home, ft.static_prefix(source.folder.strip().replace("\\", "/"))))

    def find(e: ft.Expansion) -> list[RemoteFile]:
        return find_files(client, e.values["folder"], e.values["pattern"], rel_to=rel_to, missing_ok=dated_folder)

    g = ft.gather(exps, find, lambda f: f.path, lambda e: posixpath.join(e.values["folder"], e.values["pattern"]),
                  get_settings().sftp.max_files, SftpError)
    return g, exps


def check_files(conn: SftpConnectionSpec, source: SftpSourceSpec, now: datetime | None = None,
                last_refresh: str | None = None) -> ft.Gathered:
    """«Check the files»: every file the expansions take, and the values without one;
    the sheets of the first workbook among them, to choose the sheet from a list."""
    with open_sftp(conn) as client:
        g, exps = _gather(client, source, now, last_refresh)
        if len(exps) == 1 and not g.files:
            resolve_folder(client, exps[0].values["folder"])  # a folder that is not there says so
        if source.format in ("auto", "excel"):
            g.sheets = sheets_of_first(g.files, lambda f: f.path, lambda f: f.size,
                                       lambda f, local: client.get(f.path, local), lambda f: f.rel or f.path)
        return g


def ingest_sftp_to_parquet(
    conn: SftpConnectionSpec,
    source: SftpSourceSpec,
    bucket: str,
    key: str,
    storage,
    progress: Callable[..., None] | None = None,
    last_refresh: str | None = None,
    previous: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reads every matching file (of every expansion of the folder and pattern),
    stacks them and writes the parquet; in append mode merged with the previous
    snapshot (`previous` = {bucket, key}). `last_refresh` = the start of the last
    successful refresh (ISO, UTC). `progress` as for SharePoint: «files» at each
    file, «saving» while the parquet goes up."""
    report = progress or (lambda *a, **k: None)
    cap = get_settings().sftp.max_file_bytes
    with open_sftp(conn) as client:
        g, exps = _gather(client, source, now, last_refresh)
        files = g.files
        if not files:
            if len(exps) > 1:
                raise ft.nothing_found(SftpError, g, f"'{posixpath.join(source.folder, source.pattern)}'")
            only = exps[0].values
            raise SftpError(f"No file matches '{only['pattern']}' in '{resolve_folder(client, only['folder'])}'")
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
        fmt_of = {f.path: fmt for f, fmt in zip(files, formats)}

        def fetch(f: RemoteFile, local: str) -> None:
            def guard(done: int, _total: int) -> None:
                if done > cap:  # the file grew after it was listed
                    raise SftpError(f"{f.path} grew over the limit of {cap / 1024**2:.0f} MB per file while being read")

            try:
                client.get(f.path, local, callback=guard)
            except SftpError:
                raise
            except OSError as e:
                raise _io_error(e, f"file '{f.path}'") from e

        with tempfile.TemporaryDirectory(prefix="sftp_in_") as tmp:
            table, drift = ft.stack_files(
                files, fetch=fetch, read=lambda local, f: read_file(local, f.path, fmt_of[f.path], source),
                meta=lambda f: (f.rel or f.path,
                                datetime.fromtimestamp(f.mtime, timezone.utc).replace(tzinfo=None) if f.mtime else None),
                params=g.params, key_of=lambda f: f.path, shown_as=lambda f: f.path, report=report, tmp=tmp,
                error=SftpError,
            )
            if drift:
                logger.warning("sftp %s/%s: columns not in every file: %s", source.folder, source.pattern, drift)
            table, replaced, repeated = ft.finish(table, source, storage, previous, tmp, SftpError, [FILE_COLUMN, MODIFIED_COLUMN])
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
        "missing": ft.missing_report(g),
        "missing_count": len(g.missing),
        "expansions": g.expansions,
        "mode": source.mode,
        "rows_replaced": replaced,
        "duplicate_keys": repeated,
    }


# ── writing (Output node) ──────────────────────────────────────────────────────
OutputFormat = Literal["csv", "parquet", "xlsx"]
_EXTENSION = {"csv": ".csv", "parquet": ".parquet", "xlsx": ".xlsx"}
_DATA_EXTENSIONS = (".csv", ".tsv", ".txt", ".parquet", ".pq", ".xlsx", ".xlsm", ".xls", ".json", ".ndjson", ".jsonl")


class SftpDestinationSpec(BaseModel):
    folder: str = ""  # empty = the login directory; a template (see render_folder)
    file_name: str  # template: the placeholders of app.core.placeholders for Outputs
    format: OutputFormat = "csv"
    # the zone of the dates in the templates (UTC unless set)
    timezone: str = Field(default="UTC", max_length=64)
    # filled in by the gateway: the run's number and the flow's name
    run_id: int | None = None
    flow: str = ""


def _output_context(now: datetime, tz: str, run_id: int | None, flow: str):
    from app.core import placeholders as ph

    try:
        return ph.output_context(now, tz, run_id, flow)
    except ph.PlaceholderError as e:
        raise SftpError(str(e)) from e


def render_file_name(template: str, fmt: str, now: datetime, run_id: int | None, flow: str, tz: str = "UTC") -> str:
    """The final file name: the template with the Outputs' placeholders resolved
    in the Output's zone (app.core.placeholders) — {date} = 2026-10-17,
    {datetime} = 2026-10-17_143005, {run_id}, {flow} made safe for a file name,
    {today:yyyyMMdd}, {yesterday}… A value cannot put a folder in the name. The
    format's extension is added when missing; a name ending in ANOTHER data
    format's extension is refused (a CSV called report.parquet would break
    whoever reads it)."""
    from app.core import placeholders as ph

    raw = (template or "").strip()
    if not raw:
        raise SftpError("the file name is empty")
    try:
        text = [p for p in ph.parse(raw) if isinstance(p, str)]
    except ph.PlaceholderError as e:
        raise SftpError(str(e)) from e
    if any("/" in t or "\\" in t for t in text):
        raise SftpError("the file name cannot contain a folder: put the folder in its own field")
    templated = "{" in raw or "}" in raw
    try:
        name = ph.render_output(raw, _output_context(now, tz, run_id, flow), "file", "file name") if templated else raw
    except ph.PlaceholderError as e:
        raise SftpError(str(e)) from e
    if name in (".", "..") or not name.strip(". "):
        raise SftpError(f"invalid file name '{name}'")
    ext = _EXTENSION[fmt]
    lower = name.lower()
    if not lower.endswith(ext):
        other = next((e for e in _DATA_EXTENSIONS if lower.endswith(e)), None)
        if other:
            raise SftpError(f"the file name ends in {other} but the format is {fmt}: change one of the two")
        name += ext
    if templated and len(name.encode()) > 255:  # a name written as it is goes as before
        raise SftpError(f"the file name is longer than 255 bytes ({len(name.encode())}): shorten it")
    return name


def render_folder(template: str, now: datetime, run_id: int | None, flow: str, tz: str = "UTC") -> str:
    """The destination folder: a template too (`/outbox/{today:yyyy}/{today:MM}`);
    its values may add levels, never go up. Without braces it is used as written."""
    from app.core import placeholders as ph

    folder = (template or "").strip()
    _no_dot_dot(folder, "folder")
    if "{" not in folder and "}" not in folder:
        return folder
    try:
        return ph.render_output(folder, _output_context(now, tz, run_id, flow), "path", "folder")
    except ph.PlaceholderError as e:
        raise SftpError(str(e)) from e


def _ensure_folder(client, folder: str, fixed: str) -> str:
    """The folder as an absolute path, the levels its placeholders add created when
    missing (a dated folder exists only from its first day). `fixed`, the part
    before the first placeholder, must exist: a typo there is an error."""
    base = resolve_folder(client, fixed)
    home = client.normalize(".")
    path = posixpath.normpath(posixpath.join(home, folder.replace("\\", "/"))) if folder else home
    if path != base and not path.startswith(base.rstrip("/") + "/"):
        raise SftpError(f"the folder '{path}' is not below '{base}'")
    current = base
    for seg in [p for p in path[len(base):].split("/") if p]:
        current = posixpath.join(current, seg)
        try:
            attr = client.stat(current)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise _io_error(e, f"folder '{current}'") from e
            try:
                client.mkdir(current)
            except OSError as e2:
                # another run may have created it in the meantime: fine if it is a folder now
                try:
                    if stat.S_ISDIR(client.stat(current).st_mode or 0):
                        continue
                except OSError:
                    pass
                raise _io_error(e2, f"creating the folder '{current}'") from e2
            continue
        if not stat.S_ISDIR(attr.st_mode or 0):
            raise SftpError(f"'{current}' is not a folder")
    return path


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
    moment = now or datetime.now(timezone.utc)
    name = render_file_name(dest.file_name, dest.format, moment, dest.run_id, dest.flow, dest.timezone)
    folder_name = render_folder(dest.folder, moment, dest.run_id, dest.flow, dest.timezone)
    dated = has_placeholders(dest.folder)
    workdir = tempfile.mkdtemp(prefix="sftp_out_")
    try:
        src = os.path.join(workdir, "in.parquet")
        storage.download_file(bucket, key, src)
        rows = int(pl.scan_parquet(src).select(pl.len()).collect(engine="streaming").item())
        local = _render_local(src, dest.format, workdir)
        size = os.path.getsize(local)
        with open_sftp(conn) as client:
            # a dated folder is created the first time; a fixed one must exist (a typo
            # in it is an error, not a new folder)
            folder = (_ensure_folder(client, folder_name, ft.static_prefix(dest.folder.strip())) if dated
                      else resolve_folder(client, folder_name))
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
