"""Package search for the scripting environments (services/script_packages.py), against
fake PyPI and CRAN indexes.

Fixed here:
- the names come from PyPI's simple index or CRAN's PACKAGES, or the mirrors the
  builds use; downloaded once and kept for a day;
- yesterday's list is used when the index is down, a 502 that says why when there is
  none;
- ranking: the exact name, then the names it starts, then the others;
- Python versions say whether a wheel exists for the environment's Python;
- yanked versions are left out;
- R versions come from PACKAGES and the Archive;
- administrators only.
"""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import get_session
from app.routes import scripting as routes
from app.services import script_packages as packages
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"

PYPI_INDEX = """<!DOCTYPE html><html><body>
<a href="/simple/scikit-learn/">scikit-learn</a>
<a href="/simple/scikit-image/">scikit-image</a>
<a href="/simple/scikit/">scikit</a>
<a href="/simple/sklearn-extra/">sklearn-extra</a>
<a href="/simple/py-scikit-tools/">Py_Scikit.Tools</a>
<a href="/simple/requests/">requests</a>
</body></html>"""

SKLEARN = {"meta": {"api-version": "1.1"}, "name": "scikit-learn", "files": [
    {"filename": "scikit_learn-1.5.2.tar.gz", "yanked": False},
    {"filename": "scikit_learn-1.5.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", "yanked": False},
    {"filename": "scikit_learn-1.5.2-cp312-cp312-win_amd64.whl", "yanked": False},
    {"filename": "scikit_learn-1.9.1.tar.gz", "yanked": False},
    {"filename": "scikit_learn-1.9.1-cp314-cp314-manylinux_2_27_x86_64.whl", "yanked": False},
    {"filename": "scikit_learn-1.9.1-cp312-cp312-manylinux_2_27_x86_64.whl", "yanked": False},
    {"filename": "scikit_learn-1.10.0rc1-cp314-cp314-manylinux_2_27_x86_64.whl", "yanked": False},
    {"filename": "scikit_learn-1.6.0.tar.gz", "yanked": "broken metadata"},
]}

REQUESTS_HTML = """<html><body>
<a href="https://files.example/requests-2.32.3-py3-none-any.whl#sha256=00">requests-2.32.3-py3-none-any.whl</a><br />
<a href="https://files.example/requests-2.32.3.tar.gz#sha256=00">requests-2.32.3.tar.gz</a><br />
<a href="https://files.example/requests-2.31.0.tar.gz#sha256=00" data-yanked="">requests-2.31.0.tar.gz</a><br />
</body></html>"""

CRAN_PACKAGES = """Package: dplyr
Version: 1.2.1
Depends: R (>= 4.1)

Package: dplyrAssist
Version: 0.1.0

Package: multidplyr
Version: 0.1.4

Package: glue
Version: 1.8.1
"""
GLUE_ARCHIVE = """<html><body><a href="glue_1.6.2.tar.gz">glue_1.6.2.tar.gz</a>
<a href="glue_1.10.0-1.tar.gz">x</a><a href="glue_1.8.0.tar.gz">glue_1.8.0.tar.gz</a></body></html>"""


class Indexes:
    def __init__(self):
        self.calls: list[str] = []
        self.down = False

    def handle(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.calls.append(url)
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        pages = {
            "https://pypi.org/simple/": (PYPI_INDEX, "text/html"),
            "https://mirror.example/pypi/simple/": (PYPI_INDEX, "text/html"),
            "https://pypi.org/simple/scikit-learn/": (json.dumps(SKLEARN), "application/vnd.pypi.simple.v1+json"),
            "https://pypi.org/simple/requests/": (REQUESTS_HTML, "text/html"),
            "https://cran.r-project.org/src/contrib/PACKAGES": (CRAN_PACKAGES, "text/plain"),
            "https://cran.r-project.org/src/contrib/Archive/glue/": (GLUE_ARCHIVE, "text/html"),
        }
        if url not in pages:
            return httpx.Response(404, text="not found")
        body, ctype = pages[url]
        return httpx.Response(200, text=body, headers={"content-type": ctype})


@pytest.fixture
def indexes(monkeypatch):
    i = Indexes()
    monkeypatch.setattr(packages, "_transport", httpx.MockTransport(i.handle))
    monkeypatch.setattr(packages, "_indexes", {})
    get_settings.cache_clear()
    yield i
    get_settings.cache_clear()


def run(coro_):
    return asyncio.run(coro_)


def test_python_search_ranks_the_exact_name_then_the_prefixes(indexes):
    assert [x["name"] for x in run(packages.search("python", "scikit"))] == [
        "scikit", "scikit-image", "scikit-learn", "py-scikit-tools"]
    # any spelling of a name finds it, as pip would
    assert run(packages.search("python", "Scikit_Learn"))[0] == {"name": "scikit-learn", "version": None}
    assert run(packages.search("python", "s")) == []  # one letter: too many
    assert indexes.calls.count("https://pypi.org/simple/") == 1  # downloaded once


def test_the_list_is_kept_a_day_and_yesterdays_serves_when_the_index_is_down(indexes):
    run(packages.search("python", "requests"))
    entry = packages._indexes[("python", "https://pypi.org/simple/")]
    entry.fetched_at -= packages.REFRESH_SECONDS + 1
    indexes.down = True
    assert run(packages.search("python", "requests"))[0]["name"] == "requests"
    packages._indexes.clear()
    with pytest.raises(packages.PackageIndexError, match="not reachable"):
        run(packages.search("python", "requests"))


def test_the_mirror_of_the_builds_is_searched(indexes, monkeypatch):
    monkeypatch.setenv("SCRIPTS__PYPI_INDEX_URL", "https://mirror.example/pypi/simple")
    get_settings.cache_clear()
    assert run(packages.search("python", "sklearn"))[0]["name"] == "sklearn-extra"
    assert indexes.calls == ["https://mirror.example/pypi/simple/"]


def test_python_versions_say_whether_a_wheel_fits_the_environment(indexes):
    on_314 = run(packages.python_versions("scikit-learn", "3.14"))
    assert [(v["version"], v["wheel"], v["pre"]) for v in on_314] == [
        ("1.10.0rc1", True, True), ("1.9.1", True, False), ("1.5.2", False, False)]  # 1.6.0 was yanked
    on_312 = {v["version"]: v["wheel"] for v in run(packages.python_versions("scikit-learn", "3.12"))}
    assert on_312 == {"1.10.0rc1": False, "1.9.1": True, "1.5.2": True}
    # an index that answers in HTML only (PEP 503), a universal wheel, a yanked file
    req = run(packages.python_versions("requests", "3.14"))
    assert req == [{"version": "2.32.3", "pre": False, "wheel": True}]
    assert run(packages.python_versions("no-such-thing", "3.12")) == []


@pytest.mark.parametrize("filename, python, fits", [
    ("pkg-1.0-cp312-cp312-manylinux_2_17_x86_64.whl", (3, 12), True),
    ("pkg-1.0-cp312-cp312-manylinux_2_17_x86_64.whl", (3, 13), False),
    ("pkg-1.0-cp39-abi3-manylinux_2_17_x86_64.whl", (3, 14), True),  # the stable ABI
    ("pkg-1.0-cp313-abi3-manylinux_2_17_x86_64.whl", (3, 12), False),
    ("pkg-1.0-py3-none-any.whl", (3, 11), True),
    ("pkg-1.0-cp312-cp312-macosx_11_0_arm64.whl", (3, 12), False),
    ("pkg-1.0-cp312-cp312-musllinux_1_2_x86_64.whl", (3, 12), False),  # the images use glibc
    ("not-a-wheel.whl", (3, 12), False),
])
def test_wheel_fits(filename, python, fits):
    assert packages.wheel_fits(filename, python) is fits


def test_r_search_and_versions(indexes):
    assert run(packages.search("r", "dply")) == [
        {"name": "dplyr", "version": "1.2.1"}, {"name": "dplyrAssist", "version": "0.1.0"},
        {"name": "multidplyr", "version": "0.1.4"}]
    assert [v["version"] for v in run(packages.r_versions("glue"))] == ["1.10.0-1", "1.8.1", "1.8.0", "1.6.2"]
    assert run(packages.r_versions("NoSuch")) == []


# ── the routes ───────────────────────────────────────────────────────────────
@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _login(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def test_the_routes(app, session, indexes):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    assert boss.get("/admin/scripting/packages", params={"language": "python", "q": "requests"}).json() == [
        {"name": "requests", "version": None}]
    out = boss.get("/admin/scripting/packages/python/scikit-learn", params={"python": "3.14"}).json()
    assert out["name"] == "scikit-learn" and out["versions"][-1] == {"version": "1.5.2", "pre": False, "wheel": False}
    assert boss.get("/admin/scripting/packages/r/glue").json()["versions"][0]["version"] == "1.10.0-1"
    assert boss.get("/admin/scripting/packages/python/nothing-here").status_code == 404
    assert boss.get("/admin/scripting/packages/r/..%2Fetc").status_code in (404, 422)
    assert boss.get("/admin/scripting/packages", params={"language": "julia", "q": "x"}).status_code == 422
    eye = _login(app, session, "eye@x.local", is_observer=True)
    assert eye.get("/admin/scripting/packages", params={"language": "r", "q": "glue"}).status_code == 403
    indexes.down = True
    packages._indexes.clear()
    r = boss.get("/admin/scripting/packages", params={"language": "r", "q": "glue"})
    assert r.status_code == 502 and "cran.r-project.org" in r.json()["detail"]
