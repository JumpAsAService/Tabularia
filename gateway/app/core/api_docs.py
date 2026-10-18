"""The API documentation pages (/docs, /redoc) with pinned, hash-checked scripts.

FastAPI's own pages load `swagger-ui-dist@5` and `redoc@2` from a CDN: versions
that move by themselves, with no integrity check, on the app's own origin and with
no Content-Security-Policy. A bad release of either package, or a compromised CDN,
would run with the session of whoever opens the docs, administrators first. Here
the versions are exact, the browser checks every file against its SHA-384, and a
CSP limits the page to those files, its own inline start-up script (by hash) and
requests to the API itself.

To move to a newer version: change the URL and the hash together
(`curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A`).
"""
from __future__ import annotations

import base64
import hashlib
import html
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

CDN = "https://cdn.jsdelivr.net/npm"
SWAGGER_JS = (f"{CDN}/swagger-ui-dist@5.33.0/swagger-ui-bundle.js",
              "sha384-YDALVcy8kj8yltLBVi1vBiBAUqdxvus673gM8XKwiy6aDUJFXivF/KCufekjYbVf")
SWAGGER_CSS = (f"{CDN}/swagger-ui-dist@5.33.0/swagger-ui.css",
               "sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW")
REDOC_JS = (f"{CDN}/redoc@2.5.4/bundles/redoc.standalone.js",
            "sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5")


def _sha256(script: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


def _csp(inline_scripts: list[str]) -> str:
    hashes = " ".join(_sha256(s) for s in inline_scripts)
    return "; ".join([
        "default-src 'none'",
        f"script-src {CDN}/ {hashes}".strip(),
        f"style-src {CDN}/ 'unsafe-inline'",
        "img-src 'self' data: https:",
        "font-src 'self' data: https:",
        "connect-src 'self'",
        "worker-src blob:",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ])


def _page(title: str, head: str, body: str, inline: list[str]) -> HTMLResponse:
    scripts = "".join(f"<script>{s}</script>" for s in inline)
    page = (f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">{head}</head>'
            f"<body>{body}{scripts}</body></html>")
    return HTMLResponse(page, headers={"Content-Security-Policy": _csp(inline),
                                       "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


def _tag_script(url_hash: tuple[str, str]) -> str:
    url, digest = url_hash
    return f'<script src="{url}" integrity="{digest}" crossorigin="anonymous"></script>'


def install(app: FastAPI) -> None:
    """Adds /docs and /redoc; the app is built with docs_url=None, redoc_url=None."""

    def schema_url(request: Request) -> str:
        return request.scope.get("root_path", "").rstrip("/") + app.openapi_url

    @app.get("/docs", include_in_schema=False)
    def swagger(request: Request) -> HTMLResponse:
        start = ("window.ui = SwaggerUIBundle({url: " + json.dumps(schema_url(request))
                 + ", dom_id: '#swagger-ui', deepLinking: true, presets: [SwaggerUIBundle.presets.apis,"
                 " SwaggerUIBundle.SwaggerUIStandalonePreset], layout: 'BaseLayout'});")
        css_url, css_hash = SWAGGER_CSS
        head = f'<link rel="stylesheet" href="{css_url}" integrity="{css_hash}" crossorigin="anonymous">'
        return _page(f"{app.title} — API", head, '<div id="swagger-ui"></div>' + _tag_script(SWAGGER_JS), [start])

    @app.get("/redoc", include_in_schema=False)
    def redoc(request: Request) -> HTMLResponse:
        body = (f'<redoc spec-url="{html.escape(schema_url(request))}"></redoc>' + _tag_script(REDOC_JS))
        return _page(f"{app.title} — API", "", body, [])
