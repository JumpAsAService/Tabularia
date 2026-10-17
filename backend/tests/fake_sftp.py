"""An SFTP server for the tests, in process, over a temporary directory.

Built with paramiko's own server classes, so the client code under test talks
real SSH and real SFTP: host keys, password and public-key authentication,
directory listings, partial reads, renames. It is not a substitute for the
proofs against an OpenSSH server (the throwaway container): it fixes our
behaviour — trust on first use, the glob, the caps, the atomic write.

    with FakeSftpServer(root, username="tab", password="pw") as srv:
        srv.port, srv.host_key ...
"""
from __future__ import annotations

import os
import socket
import threading

import paramiko
from paramiko import (
    AUTH_FAILED,
    AUTH_SUCCESSFUL,
    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
    OPEN_SUCCEEDED,
    SFTP_FAILURE,
    SFTP_OK,
    SFTP_OP_UNSUPPORTED,
    SFTPAttributes,
    SFTPHandle,
    SFTPServer,
    SFTPServerInterface,
)


class _Auth(paramiko.ServerInterface):
    def __init__(self, cfg: "FakeSftpServer"):
        self.cfg = cfg

    def check_auth_password(self, username, password):
        self.cfg.auth_attempts += 1
        ok = self.cfg.password is not None and (username, password) == (self.cfg.username, self.cfg.password)
        return AUTH_SUCCESSFUL if ok else AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.cfg.auth_attempts += 1
        ok = (
            username == self.cfg.username
            and self.cfg.authorized_key is not None
            and key.asbytes() == self.cfg.authorized_key.asbytes()
        )
        return AUTH_SUCCESSFUL if ok else AUTH_FAILED

    def get_allowed_auths(self, username):
        allowed = []
        if self.cfg.password is not None:
            allowed.append("password")
        if self.cfg.authorized_key is not None:
            allowed.append("publickey")
        return ",".join(allowed) or "none"

    def check_channel_request(self, kind, chanid):
        return OPEN_SUCCEEDED if kind == "session" else OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class _Handle(SFTPHandle):
    def stat(self):
        try:
            return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)


class _Sftp(SFTPServerInterface):
    """paramiko's stub server, trimmed: the virtual "/" is `root`."""

    def __init__(self, server, cfg: "FakeSftpServer"):
        super().__init__(server)
        self.cfg = cfg

    def _real(self, path):
        return self.cfg.root + self.canonicalize(path)

    def canonicalize(self, path):
        # a relative path starts at the login directory, as on OpenSSH
        if not path.startswith("/"):
            path = self.cfg.home.rstrip("/") + "/" + path
        return os.path.normpath(path).replace("//", "/")

    def list_folder(self, path):
        if self.cfg.stall_listing:
            # a server that accepted the login and then stops answering
            import time

            time.sleep(self.cfg.stall_listing)
        real = self._real(path)
        try:
            out = []
            for name in os.listdir(real):
                attr = SFTPAttributes.from_stat(os.lstat(os.path.join(real, name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            fd = os.open(real, flags, 0o644)
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        f = os.fdopen(fd, mode)
        handle = _Handle(flags)
        handle.filename = real
        handle.readfile = f
        handle.writefile = f
        self.cfg.opened.append(self.canonicalize(path))
        return handle

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rename(self, oldpath, newpath):
        # SFTP v3: a rename onto an existing file fails (OpenSSH does the same)
        if os.path.exists(self._real(newpath)):
            return SFTP_FAILURE
        try:
            os.rename(self._real(oldpath), self._real(newpath))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        self.cfg.renames.append(("rename", self.canonicalize(oldpath), self.canonicalize(newpath)))
        return SFTP_OK

    def posix_rename(self, oldpath, newpath):
        if not self.cfg.posix_rename:
            return SFTP_OP_UNSUPPORTED
        if self.cfg.posix_rename == "fail":  # supported, but this rename fails
            return SFTP_FAILURE
        try:
            os.replace(self._real(oldpath), self._real(newpath))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        self.cfg.renames.append(("posix", self.canonicalize(oldpath), self.canonicalize(newpath)))
        return SFTP_OK

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as e:
            return SFTPServer.convert_errno(e.errno)
        return SFTP_OK

    def chattr(self, path, attr):
        return SFTP_OK


class FakeSftpServer:
    """A listening SFTP server on 127.0.0.1, one thread per connection."""

    def __init__(self, root: str, username: str = "tab", password: str | None = "pw",
                 authorized_key: paramiko.PKey | None = None, host_key: paramiko.PKey | None = None,
                 home: str = "/", posix_rename: bool | str = True, extra_host_keys: list[paramiko.PKey] | None = None,
                 stall_listing: float = 0):
        self.root = os.path.abspath(root).rstrip("/")
        self.username = username
        self.password = password
        self.authorized_key = authorized_key
        self.host_key = host_key or paramiko.ECDSAKey.generate()
        # more keys of other types, as a real server has: which one the client gets
        # depends on the types it asks for
        self.extra_host_keys = list(extra_host_keys or [])
        self.home = home
        self.posix_rename = posix_rename
        self.stall_listing = stall_listing
        self.opened: list[str] = []
        self.renames: list[tuple[str, str, str]] = []
        self.handshakes = 0
        self.auth_attempts = 0  # credentials offered: none may reach a server whose key does not match
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._transports: list[paramiko.Transport] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeSftpServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        for t in self._transports:
            try:
                t.close()
            except Exception:
                pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.host_key)
            for k in self.extra_host_keys:
                t.add_server_key(k)
            t.set_subsystem_handler("sftp", SFTPServer, _Sftp, self)
            self._transports.append(t)
            try:
                t.start_server(server=_Auth(self))
            except Exception:
                continue
            self.handshakes += 1
