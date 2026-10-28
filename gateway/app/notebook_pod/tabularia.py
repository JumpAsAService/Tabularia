"""Tabularia's datasources inside a marimo notebook.

This file is NOT part of the gateway: the gateway mounts it into every notebook pod
(a ConfigMap on PYTHONPATH), so `import tabularia` works in the notebook without
rebuilding any image. docs/specs/notebooks.md, «Data».

    import tabularia
    tabularia.datasources()                 # what this notebook may read
    df = tabularia.read("Sample/orders")    # a polars DataFrame
    df = tabularia.read(42, columns=["order_id", "amount"])

Every read goes to the gateway with the pod's notebook token, and the gateway checks,
at every call, that the person whose rights the notebook carries (the author of the
version the app runs, or whoever is editing) may read that datasource. The answer is a
short-lived link to the datasource's own parquet, downloaded from the object storage:
nothing is copied, and the data is as fresh as the datasource.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request

__all__ = ["TabulariaError", "datasources", "read"]

_TIMEOUT = 60  # the gateway's answers
# the download: as long as it keeps coming, but a storage that stops sending fails the
# read after this long instead of holding the cell forever
_DOWNLOAD_TIMEOUT = 60
# the gateway is called at its address in the cluster, never through a proxy of the
# environment's variables (HTTP_PROXY, for the internet): the gateway takes the token only
# from the pod's own address. The download follows them: the storage may be outside
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TabulariaError(Exception):
    """What the gateway answered, in words: not found, not allowed, the session ended."""


def _call(method: str, path: str, body: dict | None = None) -> dict | list:
    url = os.environ.get("TABULARIA_URL", "").rstrip("/")
    token = os.environ.get("TABULARIA_NOTEBOOK_TOKEN", "")
    if not url or not token:
        raise TabulariaError("This notebook is not running in Tabularia: there is no data to read.")
    req = urllib.request.Request(
        url + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    try:
        with _DIRECT.open(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("detail")
        except ValueError:
            detail = None
        raise TabulariaError(detail if isinstance(detail, str) else f"Tabularia answered {e.code}") from None
    except OSError as e:  # URLError, or no answer within _TIMEOUT
        raise TabulariaError(f"Tabularia cannot be reached: {getattr(e, 'reason', None) or e}") from None


def datasources() -> list[dict]:
    """The datasources this notebook may read: id, name, folder, path, rows, columns."""
    return _call("GET", "/notebook-data/datasources")


def read(datasource: str | int, columns: list[str] | None = None):
    """A datasource as a polars DataFrame, by its id or by its path ("Folder/Name"; the
    name alone when only one readable datasource has it)."""
    import polars as pl

    found = _call("POST", "/notebook-data/read", {"datasource": datasource})
    fd, path = tempfile.mkstemp(suffix=".parquet")  # /tmp is the pod's scratch volume
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(found["url"], timeout=_DOWNLOAD_TIMEOUT) as src:
            shutil.copyfileobj(src, out, 1 << 20)
        return pl.read_parquet(path, columns=columns)
    except (OSError, http.client.HTTPException) as e:  # URLError, a timeout, a body cut short
        raise TabulariaError(f"The data of «{found.get('name', datasource)}» could not be downloaded: {e}") from None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
