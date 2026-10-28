"""The notebooks' own address (NOTEBOOKS__ORIGIN): services/notebook_frames.py,
routes/notebook_proxy.py, POST /notebooks/{id}/ticket; docs/specs/notebooks.md («An
origin of their own»), bug hunt 2026-10-28 #1.

marimo shows whatever HTML a notebook's code writes: on the interface's address the
notebook's author ran JavaScript with the session of whoever opened the app. Fixed here:
- without an address of their own the app and the editor do not open, saying why;
  NOTEBOOKS__ALLOW_SAME_ORIGIN opens them on the interface's, as before;
- a ticket, asked by Tabularia's page signed in in the browser with the rights that start
  the session, opens the frame once, within a minute, for that notebook, mode and person;
  the frame then holds with the notebook's cookie (HttpOnly, Secure, SameSite=None,
  Partitioned, the notebook's path), only while the Tabularia session that asked holds;
- on the notebooks' host only the proxy answers (not the API, not even «Save version»,
  which shares its path), and only with that cookie: never Tabularia's session cookie,
  never a token; on the interface's host the proxy does not answer;
- WebSockets and requests that change something come from the notebooks' own page;
- the answers may be framed only by the page that asked for the ticket;
- a frame never starts an editor; the cookie is written again while it is used; an open
  WebSocket's minute check does not mind its age;
- the setting: an origin, https (http only for localhost), never among the CORS origins.
"""
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect

from app.core.config import NotebooksSettings, Settings, get_settings
from app.models import Notebook, NotebookSession, NotebookTicket
from app.services import notebook_frames as frames
from tests.conftest import make_permission
from tests.test_notebook_runtime import (  # noqa: F401 — the fixtures, by name
    _ready, _row, _setup, app, cluster, pod,
)
from tests.test_notebooks import CSRF, _create, _login

NB = "https://notebooks.test"  # the notebooks' own address in these tests
UI = {"Origin": "http://testserver"}  # a page of the interface (TestClient's host)
FROM_NB = {"Origin": NB}


@pytest.fixture
def own_address(cluster, monkeypatch):
    monkeypatch.setenv("NOTEBOOKS__ORIGIN", NB)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ticket(client, nid, mode="app", headers=UI):
    return client.post(f"/notebooks/{nid}/ticket", json={"mode": mode}, headers={**CSRF, **headers})


def _frame(app, client, nid, mode="app") -> TestClient:
    """A browser's frame on the notebooks' address, opened with a ticket of `client`'s page."""
    r = _ticket(client, nid, mode)
    assert r.status_code == 200, r.text
    frame = TestClient(app, base_url=NB)
    opened = frame.get(r.json()["url"], follow_redirects=False)
    assert opened.status_code == 303, opened.text
    return frame


def _running(app, session, cluster, mode="app"):
    """ana's notebook with cleo's app (or ana's editor) running."""
    _, ana, ben, cleo, env_id, folder, nid = _setup(app, session)
    who = cleo if mode == "app" else ana
    assert who.post(f"/notebooks/{nid}/sessions", json={"mode": mode}, headers=CSRF).status_code == 202
    cluster.pod = "ready"
    _ready(who, nid)
    return ana, ben, cleo, env_id, folder, nid


def _ws(client, path, headers):
    with client.websocket_connect(f"wss://notebooks.test{path}", headers=headers) as ws:
        return ws.receive_text()


# ── without an address of their own ──────────────────────────────────────────
def test_without_an_address_of_their_own_notebooks_do_not_open(app, session, cluster, pod, monkeypatch):
    monkeypatch.delenv("NOTEBOOKS__ALLOW_SAME_ORIGIN")
    get_settings.cache_clear()
    ana, _, cleo, _, _, nid = _running(app, session, cluster)
    r = _ticket(cleo, nid)
    assert r.status_code == 409 and r.json()["detail"] == (
        "Notebooks need an address of their own (NOTEBOOKS__ORIGIN): an administrator must set it")
    page = cleo.get(f"/notebooks/{nid}/app/")
    assert page.status_code == 409 and "NOTEBOOKS__ORIGIN" in page.text and "not set up" in page.text
    assert cleo.get(f"/notebooks/{nid}/app/assets/index.js").status_code == 409
    with pytest.raises(WebSocketDisconnect) as closed:
        with cleo.websocket_connect(f"/notebooks/{nid}/app/ws", headers=UI) as ws:
            ws.receive_text()
    assert closed.value.code == 1008
    assert not pod.requests
    # allowed on the interface's address: as before, and the page is told so
    monkeypatch.setenv("NOTEBOOKS__ALLOW_SAME_ORIGIN", "true")
    get_settings.cache_clear()
    assert _ticket(cleo, nid).json() == {"url": None, "same_origin": True, "expires_in": 0}
    assert cleo.get(f"/notebooks/{nid}/app/").json()["marimo"] == f"/notebooks/{nid}/app/"


# ── the ticket and the cookie ────────────────────────────────────────────────
def test_a_ticket_opens_the_frame_once_and_its_cookie_does_the_rest(app, session, cluster, pod, own_address):
    _, _, cleo, _, _, nid = _running(app, session, cluster)
    r = _ticket(cleo, nid)
    out = r.json()
    assert out["url"].startswith(f"{NB}/notebooks/{nid}/app/?ticket=") and out["expires_in"] == 60
    assert out["same_origin"] is False
    (row,) = session.exec(select(NotebookTicket)).all()
    ticket = out["url"].split("ticket=", 1)[1]
    assert row.token_fp == frames.fingerprint(ticket) and ticket not in row.token_fp  # only its fingerprint
    frame = TestClient(app, base_url=NB)
    opened = frame.get(out["url"] + "&kiosk=true", follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == "./?kiosk=true"
    assert opened.headers["referrer-policy"] == "no-referrer" and opened.headers["cache-control"] == "no-store"
    cookie = opened.headers["set-cookie"]
    for part in ("tab_notebook=", "HttpOnly", "Secure", "SameSite=None", "Partitioned",
                 f"Path=/notebooks/{nid}/app/", "Max-Age=3600"):
        assert part in cookie, part
    session.expire_all()
    assert session.exec(select(NotebookTicket)).all() == []  # used up
    # the frame goes on with its cookie: marimo, framed only by the page that asked
    r = frame.get(f"/notebooks/{nid}/app/")
    assert r.status_code == 200 and r.json()["marimo"] == f"/notebooks/{nid}/app/"
    assert r.headers["content-security-policy"] == "default-src 'self'; frame-ancestors http://testserver"
    assert "x-frame-options" not in r.headers and "set-cookie" not in r.headers
    seen = pod.requests[-1]["headers"]
    assert "cookie" not in seen and "origin" not in seen
    # once only: another browser with the same ticket gets nothing
    again = TestClient(app, base_url=NB).get(out["url"], follow_redirects=False)
    assert again.status_code == 403 and "Open the notebook again from Tabularia" in again.text
    assert "set-cookie" not in again.headers and "content-security-policy" not in again.headers
    # the frame reloaded with its used ticket: its cookie holds, it goes on
    reloaded = frame.get(out["url"], follow_redirects=False)
    assert reloaded.status_code == 303 and "set-cookie" not in reloaded.headers
    # a ticket past its minute is nothing
    late = _ticket(cleo, nid).json()["url"]
    for t in session.exec(select(NotebookTicket)).all():
        t.expires_at = t.expires_at - timedelta(seconds=61)
        session.add(t)
    session.commit()
    assert TestClient(app, base_url=NB).get(late, follow_redirects=False).status_code == 403
    # and without any: the page says where to open it from
    page = TestClient(app, base_url=NB).get(f"/notebooks/{nid}/app/")
    assert page.status_code == 401 and "Open the notebook again from Tabularia" in page.text


def test_a_ticket_is_for_its_notebook_its_mode_and_whoever_asked(app, session, cluster, pod, own_address):
    ana, ben, cleo, env_id, folder, nid = _running(app, session, cluster)
    other = _create(ana, folder, env_id, name="Other").json()["id"]
    url = _ticket(cleo, nid).json()["url"]
    ticket = url.split("ticket=", 1)[1]
    browser = TestClient(app, base_url=NB)
    for wrong in (f"/notebooks/{other}/app/?ticket={ticket}", f"/notebooks/{nid}/edit/?ticket={ticket}"):
        assert browser.get(wrong, follow_redirects=False).status_code == 403, wrong
    assert browser.get(url, follow_redirects=False).status_code == 303  # not spent by the wrong ones
    # the rights that start the session: VIEW for the app, EDIT for the editor
    assert _ticket(ben, nid).status_code == 404  # may not use the environment: no such notebook
    dan, dan_id = _login(app, session, "dan@x.local")
    from app.models import Group, UserGroupLink

    session.add(UserGroupLink(user_id=dan_id, group_id=session.exec(select(Group.id)).one()))
    session.commit()
    make_permission(session, user_id=dan_id, project_id=folder, capability="view")
    assert _ticket(dan, nid).status_code == 200
    assert _ticket(dan, nid, "edit").status_code == 403
    # only from a page of Tabularia, signed in in the browser
    assert _ticket(cleo, nid, headers={}).status_code == 403
    assert _ticket(cleo, nid, headers={"Origin": "https://evil.example"}).status_code == 403
    # the frame is whoever asked: cleo's, while ana edits, shows ana editing
    assert ana.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF).status_code == 202
    _ready(ana, nid)
    cleo_editor = _frame(app, cleo, nid, "edit")
    busy = cleo_editor.get(f"/notebooks/{nid}/edit/")
    assert busy.status_code == 409 and "being edited by ana@x.local" in busy.text
    assert _frame(app, ana, nid, "edit").get(f"/notebooks/{nid}/edit/").json()["marimo"] == f"/notebooks/{nid}/edit/"


def test_the_cookie_holds_only_for_its_notebook_its_mode_and_its_key(app, session, cluster, pod, own_address):
    ana, _, cleo, env_id, folder, nid = _running(app, session, cluster)
    other = _create(ana, folder, env_id, name="Other").json()["id"]
    value = _frame(app, cleo, nid).cookies["tab_notebook"]
    browser = TestClient(app, base_url=NB)
    mine = {"Cookie": f"tab_notebook={value}"}
    assert browser.get(f"/notebooks/{nid}/app/", headers=mine).status_code == 200
    for path in (f"/notebooks/{other}/app/", f"/notebooks/{nid}/edit/"):
        r = browser.get(path, headers=mine)
        assert r.status_code == 401 and "Open the notebook again from Tabularia" in r.text, path
    assert browser.get(f"/notebooks/{other}/app/assets/x.js", headers=mine).status_code == 401
    # signed with the session tokens' key, or a session token itself: not a notebook's cookie
    claims = jwt.decode(value, options={"verify_signature": False})
    forged = jwt.encode(claims, get_settings().jwt.secret.get_secret_value(), algorithm="HS256")
    for fake in (forged, cleo.cookies["tab_token"]):
        assert browser.get(f"/notebooks/{nid}/app/", headers={"Cookie": f"tab_notebook={fake}"}).status_code == 401


def test_on_the_notebooks_host_only_the_proxy_answers(app, session, cluster, pod, own_address):
    _, _, cleo, _, _, nid = _running(app, session, cluster)
    frame = _frame(app, cleo, nid)
    # even with Tabularia's session cookie and the anti-CSRF header along
    frame.cookies.set("tab_token", cleo.cookies["tab_token"], domain="notebooks.test")
    sent = {**CSRF, **FROM_NB}
    for method, path in (("GET", "/notebooks"), ("GET", "/auth/me"), ("GET", f"/notebooks/{nid}"),
                         ("GET", f"/notebooks/{nid}/sessions"), ("POST", f"/notebooks/{nid}/ticket"),
                         ("GET", "/notebook-data/datasources"), ("DELETE", f"/notebooks/{nid}/sessions/app")):
        r = frame.request(method, path, headers=sent, json={"mode": "app"} if method == "POST" else None)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}, (method, path, r.text)
    # «Save version» shares the editor's paths: there it is the proxy's (marimo's), which
    # wants the editor's own cookie, never the API's
    r = frame.post(f"/notebooks/{nid}/edit/save", headers=sent, json={})
    assert r.status_code == 401 and r.json() == {"detail": "Invalid credentials"}
    assert session.get(Notebook, nid).current_version == 1
    assert frame.get(f"/notebooks/{nid}/app/").status_code == 200
    # and on the interface's host the proxy does not answer, the API does
    for path in (f"/notebooks/{nid}/app/", f"/notebooks/{nid}/app/assets/index.js", f"/notebooks/{nid}/app"):
        assert cleo.get(path, follow_redirects=False).status_code == 404, path
    with pytest.raises(WebSocketDisconnect) as closed:
        with cleo.websocket_connect(f"/notebooks/{nid}/app/ws", headers=UI) as ws:
            ws.receive_text()
    assert closed.value.code == 1008
    saved = cleo.post(f"/notebooks/{nid}/edit/save", json={}, headers={**CSRF, **UI})
    assert saved.status_code == 409 and "editor" in saved.json()["detail"]  # the API's answer, not the proxy's
    assert cleo.get(f"/notebooks/{nid}/sessions").status_code == 200


def test_tabularias_session_is_nothing_on_the_notebooks_host(app, session, cluster, pod, own_address):
    _, _, cleo, _, _, nid = _running(app, session, cluster)
    token = cleo.cookies["tab_token"]
    browser = TestClient(app, base_url=NB)
    browser.cookies.set("tab_token", token, domain="notebooks.test")
    page = browser.get(f"/notebooks/{nid}/app/")
    assert page.status_code == 401 and "Open the notebook again from Tabularia" in page.text
    assert browser.get(f"/notebooks/{nid}/app/assets/x.js", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    with pytest.raises(WebSocketDisconnect) as closed:
        _ws(browser, f"/notebooks/{nid}/app/ws", FROM_NB)
    assert closed.value.code == 1008
    assert not pod.requests


def test_websockets_and_changes_come_from_the_notebooks_own_page(app, session, cluster, pod, own_address):
    _, _, cleo, _, _, nid = _running(app, session, cluster)
    frame = _frame(app, cleo, nid)
    assert _ws(frame, f"/notebooks/{nid}/app/ws?session_id=s1", FROM_NB) == f"hello /notebooks/{nid}/app/ws?session_id=s1"
    for headers in ({}, UI, {"Origin": "https://evil.example"}, {"Origin": "http://notebooks.test"}):
        with pytest.raises(WebSocketDisconnect) as closed:
            _ws(frame, f"/notebooks/{nid}/app/ws", headers)
        assert closed.value.code == 1008, headers
    run = f"/notebooks/{nid}/app/api/kernel/run"
    assert frame.post(run, content=b"{}", headers=FROM_NB).status_code == 200
    for headers in ({}, UI):
        assert frame.post(run, content=b"{}", headers=headers).status_code == 403, headers


def test_a_frame_never_starts_an_editor(app, session, cluster, pod, own_address):
    _, ana, _, _, _, _, nid = _setup(app, session)
    frame = _frame(app, ana, nid, "edit")
    page = frame.get(f"/notebooks/{nid}/edit/")
    assert page.status_code == 409 and "The editor is not running" in page.text
    assert page.headers["content-security-policy"] == "frame-ancestors http://testserver"
    assert cluster.jobs() == [] and session.exec(select(NotebookSession)).all() == []
    # Tabularia's page starts it; then the frame shows it
    assert ana.post(f"/notebooks/{nid}/sessions", json={"mode": "edit"}, headers=CSRF).status_code == 202
    cluster.pod = "ready"
    _ready(ana, nid)
    assert frame.get(f"/notebooks/{nid}/edit/").json()["marimo"] == f"/notebooks/{nid}/edit/"
    # the app, read with its author's rights, still starts from its frame
    app_frame = _frame(app, ana, nid, "app")
    starting = app_frame.get(f"/notebooks/{nid}/app/")
    assert starting.status_code == 200 and "Starting" in starting.text
    assert starting.headers["content-security-policy"] == "frame-ancestors http://testserver"
    assert _row(session, nid, "app").status == "starting"


def test_signing_out_closes_the_frames(app, session, cluster, pod, own_address):
    _, _, cleo, _, _, nid = _running(app, session, cluster)
    frame = _frame(app, cleo, nid)
    assert frame.get(f"/notebooks/{nid}/app/").status_code == 200
    assert cleo.post("/auth/logout", headers=CSRF).status_code == 204
    assert frame.get(f"/notebooks/{nid}/app/").status_code == 401


def test_the_cookie_is_written_again_while_it_is_used(app, session, cluster, pod, own_address, monkeypatch):
    _, _, cleo, _, _, nid = _running(app, session, cluster)
    frame = _frame(app, cleo, nid)
    run = f"/notebooks/{nid}/app/api/kernel/run"
    assert "set-cookie" not in frame.post(run, content=b"{}", headers=FROM_NB).headers  # young
    monkeypatch.setattr(frames, "RENEW_AFTER", 0)
    assert "tab_notebook=" in frame.post(run, content=b"{}", headers=FROM_NB).headers["set-cookie"]
    assert "tab_notebook=" in frame.get(f"/notebooks/{nid}/app/").headers["set-cookie"]
    assert "set-cookie" not in frame.get(f"/notebooks/{nid}/app/assets/index.js").headers  # a file a cache may keep


def test_an_expired_cookie_opens_nothing_but_an_open_socket_minds_its_session(monkeypatch):
    monkeypatch.setattr(frames, "COOKIE_SECONDS", -3600)
    value = frames.cookie_value(user_id=7, sid="s", notebook_id=3, mode="app", frame_origin="https://app.example.com")
    conn = HTTPConnection({"type": "http", "headers": [(b"cookie", f"tab_notebook={value}".encode())]})
    assert frames.read_cookie(conn, 3, "app") is None
    claims = frames.read_cookie(conn, 3, "app", any_age=True)
    assert (claims.user_id, claims.sid, claims.frame_origin) == (7, "s", "https://app.example.com")
    assert frames.read_cookie(conn, 3, "edit", any_age=True) is None


def test_tickets_go_with_their_notebook_and_their_account(app, session, cluster, own_address):
    _, ana, _, cleo, env_id, folder, nid = _setup(app, session)
    other = _create(ana, folder, env_id, name="Other").json()["id"]
    for client, n in ((cleo, nid), (ana, other), (cleo, other)):
        assert _ticket(client, n).status_code == 200
    assert ana.delete(f"/notebooks/{nid}", headers=CSRF).status_code == 204
    assert {t.notebook_id for t in session.exec(select(NotebookTicket)).all()} == {other}
    from app.models import User
    from app.routes.users import remove_user

    remove_user(session, session.exec(select(User.id).where(User.email == "cleo@x.local")).one())
    assert [t.notebook_id for t in session.exec(select(NotebookTicket)).all()] == [other]


# ── the setting ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value, expected", [
    ("https://Notebooks.Example.com/", "https://notebooks.example.com"),
    ("https://notebooks.example.com:443", "https://notebooks.example.com"),
    ("https://notebooks.example.com:8443", "https://notebooks.example.com:8443"),
    ("http://notebooks.localhost:8080", "http://notebooks.localhost:8080"),
    ("", ""),
])
def test_the_notebooks_address_is_an_origin(value, expected):
    assert NotebooksSettings(origin=value).origin == expected


@pytest.mark.parametrize("value", [
    "http://notebooks.example.com", "https://notebooks.example.com/api", "notebooks.example.com",
    "ftp://notebooks.example.com", "https://notebooks.example.com?x=1", "https://user@notebooks.example.com",
    "https://notebooks.example.com:port",
])
def test_what_is_not_an_origin_is_refused(value):
    with pytest.raises(ValueError):
        NotebooksSettings(origin=value)


def test_the_notebooks_address_is_never_a_cors_origin(monkeypatch):
    monkeypatch.setenv("APP__CORS_ORIGINS", '["https://app.example.com", "https://notebooks.example.com/"]')
    monkeypatch.setenv("NOTEBOOKS__ORIGIN", "https://notebooks.example.com")
    with pytest.raises(ValueError, match="cannot be in APP__CORS_ORIGINS"):
        Settings()


def test_the_host_the_address_and_the_cookie_behind_a_prefix(monkeypatch):
    monkeypatch.setenv("NOTEBOOKS__ORIGIN", "https://notebooks.example.com")
    monkeypatch.setenv("APP__ROOT_PATH", "/api")
    get_settings.cache_clear()
    assert frames.is_notebooks_host("notebooks.example.com") and frames.is_notebooks_host("Notebooks.Example.com:443")
    assert not frames.is_notebooks_host("app.example.com") and not frames.is_notebooks_host("notebooks.example.com:8443")
    assert frames.frame_url(4, "edit", "a/b") == "https://notebooks.example.com/api/notebooks/4/edit/?ticket=a%2Fb"
    assert "Path=/api/notebooks/4/edit/;" in frames.cookie_header("v", 4, "edit")
    started = time.time()
    claims = jwt.decode(frames.cookie_value(user_id=1, sid="s", notebook_id=4, mode="edit", frame_origin="x"),
                        options={"verify_signature": False})
    assert claims["exp"] - started == pytest.approx(frames.COOKIE_SECONDS, abs=5)
    assert datetime.fromtimestamp(claims["iat"], timezone.utc) <= datetime.now(timezone.utc)


def test_the_gateway_puts_the_notebooks_host_in_front_of_the_api(monkeypatch):
    """main.py, not only the tests' app: on the notebooks' host the API is not there,
    on the interface's it is."""
    from app.main import app as gateway
    from app.routes.notebook_proxy import NotebooksHost

    assert any(m.cls is NotebooksHost for m in gateway.user_middleware)
    monkeypatch.setenv("NOTEBOOKS__ORIGIN", NB)
    get_settings.cache_clear()
    assert TestClient(gateway, base_url=NB).get("/system/info").status_code == 404
    assert TestClient(gateway).get("/system/info").status_code != 404
