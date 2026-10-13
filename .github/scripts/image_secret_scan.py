#!/usr/bin/env python3
"""Certify that a container image holds none of your secrets, without CI ever knowing them.

  fingerprint  on your machine: reads .env files, service-account keys and the credential
               files of cloud tools, and writes one fingerprint per secret (length, 32 bits
               of the first 8 bytes, SHA-256 of the value). A fingerprint does not give the
               value back. The output goes into the GitHub secret SECRET_FINGERPRINTS.

                 python3 .github/scripts/image_secret_scan.py fingerprint --standard \\
                     -o fingerprints.txt infrastructure/.env path/to/service-account.json

  scan         in CI: opens an image saved with `docker save` one layer at a time,
               including files a later layer deletes and the zip, gzip and tar archives
               inside it, and looks for any byte sequence whose fingerprint matches.
               Exits 1 when it finds one, 2 when it cannot certify (unreadable image,
               no fingerprints).

                 SECRET_FINGERPRINTS="$(cat fingerprints.txt)" \\
                     python3 .github/scripts/image_secret_scan.py scan image.tar

Only values of at least MIN_LENGTH characters are fingerprinted: below that, the fingerprint
would let anyone recover the value by brute force, and no cloud credential is that short.
Values already written in the repository (the public defaults) are left out too: they are
public anyway, and would fail every release.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from collections import defaultdict
from pathlib import Path

MIN_LENGTH = 16
# multiplier of the prefilter: odd, so every 8-byte window maps to its own image
MULTIPLIER = 0x9E3779B97F4A7C15
CHUNK = 16 * 1024 * 1024
# an archive inside the image is opened up to this size and this depth
ARCHIVE_LIMIT = 1024 * 1024 * 1024
MAX_DEPTH = 3

SECRET_NAME = re.compile(r"KEY|SECRET|PASSW|PWD|TOKEN|CREDENTIAL|DSN|PRIVATE", re.I)
# names that say where a secret lives, not the secret: token_uri, KEY_FILE, AUTH_HOST…
NOT_SECRET_NAME = re.compile(r"(URI|URL|URLS|ENDPOINT|HOST|PORT|FILE|PATH|DIR|TYPE)$", re.I)
# JSON keys that hold credentials even though their name does not say so
SECRET_JSON_KEYS = {"auth", "identitytoken", "registrytoken", "refresh_token", "access_token",
                    "client_secret", "private_key", "private_key_id", "client_email"}
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z0-9_.\-]+)\s*[=:]\s*(.*?)\s*$")
URL_PASSWORD = re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:([^@\s/]+)@", re.I)
NETRC_PASSWORD = re.compile(r"\bpassword\s+(\S+)")
PEM_KEY = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(.*?)-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)

# where cloud and developer tools keep credentials (--standard)
STANDARD_FILES = [
    "~/.aws/credentials", "~/.config/gcloud/application_default_credentials.json",
    "~/.config/scw/config.yaml", "~/.docker/config.json", "~/.kube/config", "~/.netrc",
    "~/.git-credentials", "~/.config/gh/hosts.yml", "~/.oci/config",
    "~/.config/doctl/config.yaml", "~/.config/hcloud/cli.toml",
]


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name)) and not NOT_SECRET_NAME.search(name)


def prefilter(value: bytes) -> int:
    """32 bits of the first 8 bytes: the same computation `scan` runs at every offset."""
    return ((int.from_bytes(value[:8], "little") * MULTIPLIER) % (1 << 64)) >> 32


# ── fingerprint ─────────────────────────────────────────────────────────────────

class Collector:
    def __init__(self) -> None:
        self.values: dict[bytes, str] = {}
        self.too_short: dict[str, bytes] = {}

    def add(self, label: str, value: str | bytes) -> None:
        if isinstance(value, str):
            value = value.strip().strip("'\"").encode()
        value = value.strip()
        if not value:
            return
        if len(value) < MIN_LENGTH:
            self.too_short[label] = value
            return
        self.values.setdefault(value, label)
        self._decode(label, value)

    def _decode(self, label: str, value: bytes) -> None:
        """A base64 value can hide a JSON credential file or a PEM key."""
        if len(value) < 24 or not re.fullmatch(rb"[A-Za-z0-9+/=_\-]+", value):
            return
        urlsafe = b"-" in value or b"_" in value
        try:
            plain = base64.b64decode(value + b"=" * (-len(value) % 4),
                                     altchars=b"-_" if urlsafe else None, validate=True)
        except (binascii.Error, ValueError):
            return
        if plain.lstrip()[:1] == b"{":
            # a JSON first: a service-account file holds a PEM key and the fields around it
            try:
                self.json(label + "(base64)", json.loads(plain))
                return
            except ValueError:
                pass
        if b"PRIVATE KEY-----" in plain:
            self.pem(label + "(base64)", plain)

    def pem(self, label: str, text: bytes) -> None:
        # the body lines one by one: they match both in a PEM file and in JSON with \n escapes
        for n, body in enumerate(PEM_KEY.findall(text)):
            for i, line in enumerate(body.replace(b"\\n", b"\n").split()):
                if len(line) >= 40:
                    self.add(f"{label}:key{n}:line{i}", line)

    def json(self, label: str, data) -> None:
        if isinstance(data, dict):
            for key, value in data.items():
                where = f"{label}:{key}"
                if isinstance(value, str) and (is_secret_name(key) or key.lower() in SECRET_JSON_KEYS):
                    if "PRIVATE KEY-----" in value:
                        self.pem(where, value.encode())
                    else:
                        self.add(where, value)
                    if key.lower() == "auth":  # docker: base64 of "user:token"
                        try:
                            _, _, token = base64.b64decode(value).decode().partition(":")
                            self.add(where + ":password", token)
                        except (binascii.Error, ValueError, UnicodeDecodeError):
                            pass
                elif isinstance(value, (dict, list)):
                    self.json(where, value)
        elif isinstance(data, list):
            for i, value in enumerate(data):
                self.json(f"{label}[{i}]", value)

    def file(self, path: Path) -> None:
        name = path.name
        raw = path.read_bytes()
        if b"PRIVATE KEY-----" in raw:
            self.pem(name, raw)
        text = raw.decode("utf-8", errors="replace")
        try:
            self.json(name, json.loads(text))
            return
        except ValueError:
            pass
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            # a password inside a URL (.git-credentials, a DSN) or netrc-style, on any line
            for password in URL_PASSWORD.findall(line) + NETRC_PASSWORD.findall(line):
                self.add(f"{name}:line{n}:password", password)
            m = ASSIGNMENT.match(line)
            if m and is_secret_name(m.group(1)):
                self.add(f"{name}:{m.group(1)}", m.group(2))


def repository_root() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def in_repository(repo: str, value: bytes) -> bool:
    """True when the value is written in a tracked file: it is public already."""
    try:
        return subprocess.run(["git", "-C", repo, "grep", "-qF", "-e", value.decode(), "--", "."],
                              capture_output=True).returncode == 0
    except (UnicodeDecodeError, OSError):
        return False


def fingerprint_command(args) -> int:
    collector = Collector()
    given = [Path(p).expanduser() for p in args.files]
    paths = list(given)
    if args.standard:
        paths += [Path(p).expanduser() for p in STANDARD_FILES]
        ssh = Path("~/.ssh").expanduser()
        if ssh.is_dir():
            paths += [p for p in sorted(ssh.glob("id_*")) if p.suffix != ".pub"]
    read = []
    for path in paths:
        if path.is_file():
            collector.file(path)
            read.append(path.name)
        elif path in given:
            print(f"not found: {path}", file=sys.stderr)
            return 2
    repo = args.repo if args.repo is not None else repository_root()
    public = []
    lines = [f"# fingerprints from {len(read)} files: {', '.join(read)}"]
    for value, label in collector.values.items():
        if repo and in_repository(repo, value):
            public.append(label)
            continue
        label = re.sub(r"\s+", "_", label)
        lines.append(f"{len(value)} {prefilter(value):08x} {hashlib.sha256(value).hexdigest()} {label}")
    text = "\n".join(lines) + "\n"
    if args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    print(f"fingerprints: {len(lines) - 1} from {len(read)} files", file=sys.stderr)
    if public:
        # usually the public defaults of a .env.example; if one is a real secret, it is on
        # GitHub already and that is the problem to fix, not the image
        print(f"WARNING, already written in the repository and therefore left out: {', '.join(sorted(public))}\n"
              "  if one of these is a real secret, it is public: rotate it", file=sys.stderr)
    if collector.too_short:
        # too short to fingerprint safely; the one way such a secret reaches an image built
        # in CI is being committed, so at least that is checked here
        committed = sorted(label for label, value in collector.too_short.items()
                           if repo and in_repository(repo, value))
        print(f"shorter than {MIN_LENGTH} characters, not fingerprinted: {', '.join(sorted(collector.too_short))}",
              file=sys.stderr)
        if committed:
            print(f"WARNING, short values written in the repository: {', '.join(committed)}\n"
                  "  fine if they are public defaults; if one is a real secret, rotate it", file=sys.stderr)
    return 0


# ── scan ────────────────────────────────────────────────────────────────────────

def parse_fingerprints(text: str) -> list[tuple[int, int, str, str]]:
    fingerprints = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        length, pre, sha, label = line.split(maxsplit=3)
        fingerprints.append((int(length), int(pre, 16), sha.lower(), label))
    return fingerprints


class Scanner:
    def __init__(self, fingerprints: list[tuple[int, int, str, str]], chunk: int = CHUNK) -> None:
        import numpy as np  # only here: `fingerprint` runs without numpy
        self.np = np
        self.by_prefilter: dict[int, list[tuple[int, str, str]]] = defaultdict(list)
        for length, pre, sha, label in fingerprints:
            self.by_prefilter[pre].append((length, sha, label))
        self.table = np.array(sorted(self.by_prefilter), dtype=np.uint64)
        self.longest = max(f[0] for f in fingerprints)
        self.shortest = min(f[0] for f in fingerprints)
        self.chunk = chunk
        self.found: list[tuple[str, str, int]] = []
        self.warnings: list[str] = []
        self.layers = 0
        self.files = 0
        self.bytes = 0
        self.archives = 0

    def _buffer(self, buf: bytes, where: str, base: int, seen: set[int]) -> None:
        if len(buf) < self.shortest:
            return
        np = self.np
        multiplier = np.uint64(MULTIPLIER)
        for start in range(8):
            # the 8-byte windows at start, start+8, start+16…: eight views cover every offset
            count = (len(buf) - start) // 8
            if count <= 0:
                break
            windows = np.frombuffer(buf, dtype="<u8", count=count, offset=start)
            hashes = (windows * multiplier) >> np.uint64(32)
            index = np.minimum(np.searchsorted(self.table, hashes), len(self.table) - 1)
            for j in np.nonzero(self.table[index] == hashes)[0]:
                pos = start + 8 * int(j)
                for length, sha, label in self.by_prefilter[int(hashes[j])]:
                    if pos + length <= len(buf) and hashlib.sha256(buf[pos:pos + length]).hexdigest() == sha:
                        if base + pos not in seen:
                            seen.add(base + pos)
                            self.found.append((label, where, base + pos))

    def stream(self, f, where: str) -> None:
        """Reads in chunks; the tail of each chunk is read again with the next one, so a
        secret straddling two chunks does not slip through."""
        tail, base, seen = b"", 0, set()
        while True:
            piece = f.read(self.chunk)
            if not piece:
                break
            buf = tail + piece
            self._buffer(buf, where, base, seen)
            self.bytes += len(piece)
            keep = min(len(buf), self.longest - 1)
            tail = buf[len(buf) - keep:] if keep else b""
            base += len(buf) - len(tail)

    def content(self, data: bytes, where: str, depth: int) -> None:
        self.stream(io.BytesIO(data), where)
        if depth >= MAX_DEPTH:
            return
        try:
            if data[:4] == b"PK\x03\x04":
                self._zip(data, where, depth)
            elif data[:2] == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                    plain = g.read(ARCHIVE_LIMIT + 1)
                if len(plain) > ARCHIVE_LIMIT:
                    self.warnings.append(f"{where}: over {ARCHIVE_LIMIT >> 20} MB uncompressed, scanned compressed only")
                    return
                self.archives += 1
                self.content(plain, where + "!gunzip", depth + 1)
            elif len(data) > 262 and data[257:262] == b"ustar":
                with tarfile.open(fileobj=io.BytesIO(data)) as t:
                    self.archives += 1
                    self.tar(t, where + "!", depth + 1)
        except (OSError, EOFError, zipfile.BadZipFile, tarfile.TarError, ValueError) as e:
            self.warnings.append(f"{where}: unreadable archive ({type(e).__name__}), scanned as is")

    def _zip(self, data: bytes, where: str, depth: int) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.archives += 1
            for entry in z.infolist():
                if entry.is_dir():
                    continue
                if entry.file_size > ARCHIVE_LIMIT:
                    self.warnings.append(f"{where}!{entry.filename}: over {ARCHIVE_LIMIT >> 20} MB, not opened")
                    continue
                self.content(z.read(entry), f"{where}!{entry.filename}", depth + 1)

    def tar(self, t: tarfile.TarFile, prefix: str, depth: int) -> None:
        for entry in t:
            if not entry.isfile():
                continue
            self.files += 1
            f = t.extractfile(entry)
            if f is None:
                continue
            where = prefix + entry.name
            if entry.size <= ARCHIVE_LIMIT:
                self.content(f.read(), where, depth)
            else:
                self.stream(f, where)

    def image(self, path: str) -> None:
        """The tar from `docker save`: each layer on its own, and every other file
        (the config with environment and history, the manifests) as it is."""
        with tarfile.open(path) as outer:
            entries = {e.name: e for e in outer.getmembers()}
            if "manifest.json" not in entries:
                raise ValueError("no manifest.json: not a docker save tar")
            manifest = json.load(outer.extractfile(entries["manifest.json"]))
            layers = [layer for m in manifest for layer in m["Layers"]]
            if not layers:
                raise ValueError("no layers in the manifest")
            for name, entry in entries.items():
                if not entry.isfile():
                    continue
                if name in layers:
                    with tarfile.open(fileobj=outer.extractfile(entry), mode="r:*") as layer:
                        self.tar(layer, f"layer {layers.index(name) + 1}:/", 0)
                else:
                    self.content(outer.extractfile(entry).read(), name, 0)
            self.layers = len(layers)


def scan_command(args) -> int:
    text = Path(args.fingerprints).read_text() if args.fingerprints else os.environ.get("SECRET_FINGERPRINTS", "")
    try:
        fingerprints = parse_fingerprints(text)
    except ValueError:
        print("the fingerprints are not in the format `fingerprint` writes", file=sys.stderr)
        return 2
    if not fingerprints:
        print("no fingerprints: nothing can be certified without them (is SECRET_FINGERPRINTS set?)", file=sys.stderr)
        return 2
    scanner = Scanner(fingerprints)
    try:
        scanner.image(args.image)
    except (OSError, tarfile.TarError, ValueError, KeyError) as e:
        print(f"unreadable image: {e}", file=sys.stderr)
        return 2
    print(f"{args.image}: {scanner.layers} layers, {scanner.files} files, {scanner.bytes / 1e9:.2f} GB read, "
          f"{scanner.archives} archives opened, {len(fingerprints)} fingerprints")
    for warning in scanner.warnings:
        print(f"  warning: {warning}")
    if scanner.found:
        for label, where, offset in scanner.found:
            print(f"  FOUND {label} in {where} (byte {offset})")
        print(f"{len(scanner.found)} secrets in the image: it must NOT be published")
        return 1
    print("no secrets: certified")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fingerprint", help="fingerprint your secrets (on your machine)")
    f.add_argument("files", nargs="*", help=".env files, service-account JSON, keys, credential files")
    f.add_argument("--standard", action="store_true", help="also read the cloud tools' credential files and SSH keys")
    f.add_argument("--repo", help="leave out values already written in this git repository "
                                  "(default: the one this script is in; '' to disable)")
    f.add_argument("-o", "--output", help="write here, with mode 600, instead of to stdout")
    s = sub.add_parser("scan", help="look for the fingerprints in an image saved with docker save (in CI)")
    s.add_argument("image", help="the docker save tar")
    s.add_argument("--fingerprints", help="fingerprint file; without it, the SECRET_FINGERPRINTS variable")
    args = parser.parse_args(argv)
    return fingerprint_command(args) if args.command == "fingerprint" else scan_command(args)


if __name__ == "__main__":
    sys.exit(main())
