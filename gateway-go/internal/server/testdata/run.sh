#!/bin/sh
# Regenerates fastapi_routes_gen.go (the Python gateway's routes, in Starlette's order) from
# the REAL FastAPI app of the gateway's image. Run after a route is added or moved in Python.
# Usage: internal/server/testdata/run.sh [image]   (default tabularia-gateway:latest)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
server=$(dirname "$here")
repo=$(cd "$server/../../.." && pwd)
image=${1:-tabularia-gateway:latest}
docker run -i --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e JWT__SECRET=route-table-only-not-a-secret-00000000 \
  -v "$repo/gateway/app:/app/app:ro" -v "$server:/server" -v "$here:/td" -w /app \
  "$image" python /td/gen_routes.py
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$server:/server" -w /server golang:1.27.3-bookworm \
  gofmt -w fastapi_routes_gen.go
