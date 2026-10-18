"""The API documentation pages load pinned scripts, checked by hash, under a CSP
(review 2026-10-14, sec-authn-2).

/docs and /redoc are served on the app's own origin, and loaded
`swagger-ui-dist@5` and `redoc@2` from a CDN: floating versions, no integrity
check, no Content-Security-Policy. A bad release of either, or a compromised CDN,
would have run with the session of every administrator who opened the docs (one
fetch makes a 365-day write token)."""
import base64
import hashlib
import re

from fastapi.testclient import TestClient

from app.main import app


def _page(path):
    r = TestClient(app).get(path)
    assert r.status_code == 200
    return r


def test_swagger_loads_exact_versions_with_integrity():
    html = _page("/docs").text
    for src in re.findall(r'<(?:script|link)[^>]+(?:src|href)="(https://[^"]+)"', html):
        assert re.search(r"@\d+\.\d+\.\d+/", src), src  # an exact version, never @5
    assert html.count('integrity="sha384-') == 2 and html.count('crossorigin="anonymous"') == 2


def test_redoc_loads_an_exact_version_with_integrity():
    html = _page("/redoc").text
    assert "redoc@2.5.4/" in html and html.count('integrity="sha384-') == 1


def test_the_pages_have_a_csp_that_allows_only_their_own_inline_script():
    r = _page("/docs")
    csp = r.headers["content-security-policy"]
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    inline = re.search(r"<script>(.*?)</script>", r.text, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(inline.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "content-security-policy" in _page("/redoc").headers


def test_the_schema_is_still_served():
    assert TestClient(app).get("/openapi.json").status_code == 200
