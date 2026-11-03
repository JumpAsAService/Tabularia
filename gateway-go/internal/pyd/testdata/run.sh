#!/bin/sh
# Regenerates, with the REAL pydantic/FastAPI of the gateway's image, what the
# differential test of internal/pyd compares against:
#   - gatewaymodels/models_gen.go and synthetic_gen_test.go (gen_models.py);
#   - testdata/cases.json.gz (make_cases.py) and testdata/golden.json.gz (golden.py).
# Usage: internal/pyd/testdata/run.sh [image]   (default tabularia-gateway:latest)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
pyd=$(dirname "$here")
repo=$(cd "$pyd/../../.." && pwd)
image=${1:-tabularia-gateway:latest}
docker run -i --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$repo/gateway/app:/app/app:ro" -v "$pyd:/pyd" -v "$here:/td" -w /td \
  "$image" sh -c 'python gen_models.py && python make_cases.py && python golden.py'
# the generated Go, formatted as gofmt wants it
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$pyd:/pyd" -w /pyd golang:1.27.3-bookworm \
  gofmt -w gatewaymodels/models_gen.go synthetic_gen_test.go
