"""Writes openapi.json: the Python gateway's OpenAPI schema exactly as GET /openapi.json sends
it (Starlette's JSONResponse: compact, ensure_ascii=False), with the application's name and
version as placeholders the Go gateway fills (APP__NAME, APP__VERSION), and no `servers` (the
Go gateway adds APP__ROOT_PATH's, as FastAPI does). Run in the gateway's image, from the
repository root, whenever a route, a schema or a docstring of the Python gateway changes:

    docker run --rm --user "$(id -u)" -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/apidocs:/out" \
        -w /app -e HOME=/tmp -e SECURITY__FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_') \
        -e APP__NAME=@@APP_NAME@@ -e APP__VERSION=@@APP_VERSION@@ -e APP__ROLE=api tabularia-gateway:latest \
        python /out/gen_openapi.py
"""
import json

from app.main import app

schema = app.openapi()
assert "servers" not in schema, "run it without APP__ROOT_PATH"
text = json.dumps(schema, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"))
assert text.count("@@APP_NAME@@") == 1 and text.count("@@APP_VERSION@@") == 1, "the placeholders appear elsewhere"
with open("/out/openapi.json", "w", encoding="utf-8") as f:
    f.write(text)
