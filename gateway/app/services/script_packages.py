"""Search for packages while defining a scripting environment (Admin → Scripting
environments), in the indexes the builds install from: PyPI or its mirror for Python,
CRAN or its mirror for R. Community indexes only, nothing of a company's.

- Names. PyPI has no search API: its simple index (PEP 503) lists every project, about
  900,000, one link per line; CRAN's `src/contrib/PACKAGES` lists its 25,000 packages
  with their current version. The gateway downloads the list once a day per process,
  keeps only the names (and CRAN's versions) in one string, and searches that.
- Versions. A Python project's page of the same index lists its files: each file gives a
  version, whether it was yanked, and, for wheels, which Pythons it was built for. A
  version without a wheel for the environment's Python compiles from source during the
  build, which often fails: the panel says so before anyone builds. For R, the current
  version comes from PACKAGES and the older ones from `src/contrib/Archive/<name>/`.

Requests go out from the gateway: with a mirror on an internal network, the addresses
are the ones the builds use (SCRIPTS__PYPI_INDEX_URL, SCRIPTS__CRAN_URL).
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from app.core.config import get_settings

PYPI = "https://pypi.org/simple/"
REFRESH_SECONDS = 24 * 3600
MAX_INDEX_BYTES = 256 * 1024 * 1024
MAX_RESULTS = 20
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# for the tests: a transport standing in for the indexes
_transport: Optional[httpx.AsyncBaseTransport] = None


class PackageIndexError(Exception):
    """The index could not be read; the message says which and why."""


@dataclass
class _Index:
    fetched_at: float = 0.0
    # one entry per line: "name" (Python, normalized) or "name\tversion" (R)
    blob: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_indexes: dict[tuple[str, str], _Index] = {}


def python_index() -> str:
    url = get_settings().scripts.pypi_index_url.strip() or PYPI
    return url if url.endswith("/") else url + "/"


def cran() -> str:
    return get_settings().scripts.cran_url.strip().rstrip("/")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, transport=_transport,
                             headers={"User-Agent": "Tabularia (scripting environments)"})


async def _get(url: str, accept: str = "*/*") -> httpx.Response:
    """The page at `url`; a 404 is returned, any other failure raises."""
    try:
        async with _client() as c:
            r = await c.get(url, headers={"Accept": accept})
    except httpx.HTTPError as e:
        raise PackageIndexError(f"{url} is not reachable ({type(e).__name__})") from None
    if r.status_code >= 400 and r.status_code != 404:
        raise PackageIndexError(f"{url} answered {r.status_code}")
    if len(r.content) > MAX_INDEX_BYTES:
        raise PackageIndexError(f"{url} is larger than {MAX_INDEX_BYTES // 2**20} MB")
    return r


_HREF_NAME = re.compile(r'<a\s[^>]*href="[^"]*?/([^/"]+)/?"[^>]*>', re.I)


def parse_python_index(text: str) -> str:
    names = {canonicalize_name(html.unescape(m.group(1))) for m in _HREF_NAME.finditer(text)}
    return "\n".join(sorted(names))


def parse_cran_packages(text: str) -> str:
    rows, name = [], None
    for line in text.splitlines():
        if line.startswith("Package:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Version:") and name:
            rows.append(f"{name}\t{line.split(':', 1)[1].strip()}")
            name = None
    return "\n".join(sorted(rows, key=str.lower))


async def _index(language: str) -> str:
    url = python_index() if language == "python" else f"{cran()}/src/contrib/PACKAGES"
    entry = _indexes.setdefault((language, url), _Index())
    if entry.blob and time.monotonic() - entry.fetched_at < REFRESH_SECONDS:
        return entry.blob
    async with entry.lock:
        if entry.blob and time.monotonic() - entry.fetched_at < REFRESH_SECONDS:
            return entry.blob
        try:
            r = await _get(url, "text/html" if language == "python" else "text/plain")
            if r.status_code == 404:
                raise PackageIndexError(f"{url} answered 404")
            text = r.text
            # parsing 900,000 links takes a moment: not on the event loop
            blob = await asyncio.to_thread(parse_python_index if language == "python" else parse_cran_packages, text)
        except PackageIndexError:
            if entry.blob:  # yesterday's list is better than none
                entry.fetched_at = time.monotonic() - REFRESH_SECONDS + 600
                return entry.blob
            raise
        entry.blob, entry.fetched_at = blob, time.monotonic()
        return blob


async def search(language: str, query: str) -> list[dict]:
    """The packages whose name holds `query`: the exact name first, then the names it
    starts, then the others; shorter names first."""
    q = query.strip()
    if language == "python":
        q = canonicalize_name(q)
    if len(q) < 2:
        return []
    blob = await _index(language)
    flags = re.M | (re.I if language == "r" else 0)
    # a few hundred milliseconds over PyPI's list: not on the event loop
    hits = await asyncio.to_thread(re.findall, rf"^[^\n]*{re.escape(q)}[^\n]*$", blob, flags)
    key = q.lower()

    def rank(line: str):
        name = line.split("\t", 1)[0]
        low = name.lower()
        return (low != key, not low.startswith(key), len(name), low)

    out = []
    for line in sorted(hits, key=rank)[:MAX_RESULTS]:
        name, _, version = line.partition("\t")
        out.append({"name": name, "version": version or None})
    return out


def _python_tag(version: str) -> Optional[tuple[int, int]]:
    m = re.fullmatch(r"(\d+)\.(\d+)", version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def wheel_fits(filename: str, python: tuple[int, int]) -> bool:
    """A wheel the environment's image can install: this CPython (or any Python 3), on
    Linux with glibc (the images are Debian and Ubuntu) or on any platform."""
    try:
        _, _, _, tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        return False
    major, minor = python
    for tag in tags:
        platform_ok = tag.platform == "any" or tag.platform.startswith(("manylinux", "linux"))
        if not platform_ok:
            continue
        interp, abi = tag.interpreter, tag.abi
        if interp in (f"cp{major}{minor}", f"py{major}", f"py{major}{minor}"):
            return True
        # the stable ABI: a wheel for cp3X with abi3 works on every later CPython
        if abi == "abi3" and interp.startswith("cp") and interp[2:].isdigit():
            built = int(interp[3:]) if interp[2] == str(major) else -1
            if 0 <= built <= minor:
                return True
    return False


_FILE = re.compile(r'<a\s([^>]*)>([^<]+)</a>', re.I)


def _python_files(r: httpx.Response) -> list[dict]:
    if "json" in r.headers.get("content-type", ""):
        return [{"filename": f["filename"], "yanked": bool(f.get("yanked"))} for f in r.json().get("files") or []]
    files = []
    for attrs, text in _FILE.findall(r.text):
        files.append({"filename": html.unescape(text.strip()), "yanked": "data-yanked" in attrs})
    return files


def _file_version(name: str, filename: str) -> Optional[str]:
    if filename.endswith(".whl"):
        try:
            return str(parse_wheel_filename(filename)[1])
        except InvalidWheelFilename:
            return None
    for ext in (".tar.gz", ".zip", ".tar.bz2", ".tgz"):
        if filename.endswith(ext):
            stem = filename[: -len(ext)]
            # the sdist is <name>-<version>, the name spelled in any normalization
            m = re.fullmatch(r"(.+?)-(\d[^-]*)", stem)
            if m and canonicalize_name(m.group(1)) == name:
                return m.group(2)
    return None


async def python_versions(name: str, python_version: str) -> list[dict]:
    name = canonicalize_name(name)
    r = await _get(f"{python_index()}{name}/", "application/vnd.pypi.simple.v1+json, text/html;q=0.1")
    if r.status_code == 404:
        return []
    python = _python_tag(python_version)
    found: dict[str, dict] = {}
    for f in _python_files(r):
        v = _file_version(name, f["filename"])
        if not v:
            continue
        try:
            parsed = Version(v)
        except InvalidVersion:
            continue
        row = found.setdefault(str(parsed), {"version": str(parsed), "pre": parsed.is_prerelease, "yanked": True,
                                             "wheel": False if python else None})
        row["yanked"] = row["yanked"] and f["yanked"]  # yanked only if every file is
        if python and not f["yanked"] and f["filename"].endswith(".whl") and wheel_fits(f["filename"], python):
            row["wheel"] = True
    rows = [x for x in found.values() if not x["yanked"]]
    for x in rows:
        del x["yanked"]
    return sorted(rows, key=lambda x: Version(x["version"]), reverse=True)


def _r_key(v: str):
    return [int(p) for p in re.split(r"[.-]", v) if p.isdigit()]


async def r_versions(name: str) -> list[dict]:
    blob = await _index("r")
    m = re.search(rf"^{re.escape(name)}\t([^\n]+)$", blob, re.M)
    if not m:
        return []
    versions = {m.group(1)}
    r = await _get(f"{cran()}/src/contrib/Archive/{name}/", "text/html")
    if r.status_code != 404:
        versions |= set(re.findall(rf'href="{re.escape(name)}_([0-9][0-9.\-]*)\.tar\.gz"', r.text))
    return [{"version": v, "pre": False, "wheel": None} for v in sorted(versions, key=_r_key, reverse=True)]
