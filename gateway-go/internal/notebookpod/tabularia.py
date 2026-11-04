"""Tabularia's datasources inside a marimo notebook.

This file is NOT part of the gateway: the gateway mounts it into every notebook pod
(a ConfigMap on PYTHONPATH), so `import tabularia` works in the notebook without
rebuilding any image. docs/specs/notebooks.md, «Data».

    import tabularia
    tabularia.datasources()                 # what this notebook may read
    df = tabularia.read("Sample/orders")    # a polars DataFrame
    df = tabularia.read(42, columns=["order_id", "amount"])
    tabularia.reports()                     # the reports it may render, with their parameters
    pdf = tabularia.report("Sales/Monthly", region="North", since="2026-09-01")  # the PDF's bytes

Every read goes to the gateway with the pod's notebook token, and the gateway checks,
at every call, that the person whose rights the notebook carries (the author of the
version the app runs, or whoever is editing) may read that datasource. The answer is a
short-lived link to the datasource's own parquet, downloaded from the object storage:
nothing is copied, and the data is as fresh as the datasource. A report is rendered by
the gateway with the same rights, every dataset it reads checked, and comes back whole.
"""
from __future__ import annotations

import datetime
import decimal
import http.client
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request

__all__ = ["TabulariaError", "datasources", "read", "report", "reports"]

_TIMEOUT = 60  # the gateway's answers
# a report: the gateway waits for the render (REPORTS__API_WAIT_SECONDS, 120 s by default)
_REPORT_TIMEOUT = 600
# the download: as long as it keeps coming, but a storage that stops sending fails the
# read after this long instead of holding the cell forever
_DOWNLOAD_TIMEOUT = 60
# the gateway is called at its address in the cluster, never through a proxy of the
# environment's variables (HTTP_PROXY, for the internet): the gateway takes the token only
# from the pod's own address. The download follows them: the storage may be outside
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TabulariaError(Exception):
    """What the gateway answered, in words: not found, not allowed, the session ended."""


def _send(method: str, path: str, body: dict | None = None, timeout: float = _TIMEOUT) -> bytes:
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
        with _DIRECT.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, dict):  # a parameter that does not fit: {"message", "parameter"}
            detail = detail.get("message")
        raise TabulariaError(detail if isinstance(detail, str) else f"Tabularia answered {e.code}") from None
    except (OSError, http.client.HTTPException) as e:  # URLError, no answer in time, a body cut short
        raise TabulariaError(f"Tabularia cannot be reached: {getattr(e, 'reason', None) or e}") from None


def _call(method: str, path: str, body: dict | None = None) -> dict | list:
    return json.loads(_send(method, path, body) or b"null")


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


def reports() -> list[dict]:
    """The reports this notebook may render: id, name, folder, path, and their parameters
    (name, type, multi, required, default)."""
    return _call("GET", "/notebook-data/reports")


def _value(v):
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()[:10]  # a report's dates are days
    if isinstance(v, decimal.Decimal):
        return str(v)
    if not isinstance(v, (str, bytes)):
        # numpy's and pandas' values (`df["year"].max()`, an array, a Series), polars' Series
        if callable(getattr(v, "tolist", None)):
            return _value(v.tolist())
        if callable(getattr(v, "to_list", None)):
            return _value(v.to_list())
    if isinstance(v, (list, tuple, set)):
        return [_value(x) for x in v]
    return v


def report(report: str | int, /, format: str = "pdf", **params) -> bytes:
    """A report rendered with these parameters, as bytes: by its id or its path
    ("Folder/Name"; the name alone when only one report it may render has it). `format`
    pdf, xlsx or html (the email's summary, as MHTML). A parameter left out takes the
    report's default; a list for one that takes several; a date as a date or yyyy-mm-dd.

        with open("sales.pdf", "wb") as f:
            f.write(tabularia.report("Sales/Monthly", region=["North", "South"]))
    """
    if format not in ("pdf", "xlsx", "html"):
        raise TabulariaError("format is pdf, xlsx or html")
    body = {"report": report, "format": format, "params": {k: _value(v) for k, v in params.items()}}
    return _send("POST", "/notebook-data/report", body, timeout=_REPORT_TIMEOUT)
