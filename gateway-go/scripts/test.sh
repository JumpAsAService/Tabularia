#!/bin/sh
# The Go gateway's tests. The database ones run on a disposable PostgreSQL whose schema the
# Python gateway's migrations make, from this branch's own source: the schema is Alembic's,
# and this is how the Go code is tested on exactly the tables production has.
#
# The data APIs' program (gateway-go/dataapi, a module of its own, with cgo for DuckDB) is
# tested after the gateway, in the same image.
#
#   gateway-go/scripts/test.sh            all the tests
#   GATEWAY_IMAGE=… gateway-go/scripts/test.sh   the image whose Python runs the migrations
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
repo=$(cd "$here/.." && pwd)
go_image=golang:1.27.3-bookworm
gateway_image=${GATEWAY_IMAGE:-tabularia-gateway:latest}
name=tabularia-gwgo-test-$$

docker run -d --rm --name "$name" -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
    -p 127.0.0.1::5432 postgres:16-alpine >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1' EXIT
port=$(docker port "$name" 5432/tcp | head -1 | sed 's/.*://')
until docker exec "$name" pg_isready -U test -d test >/dev/null 2>&1; do sleep 0.5; done
sleep 1 # the image restarts the server once after its first initialisation
until docker exec "$name" pg_isready -U test -d test >/dev/null 2>&1; do sleep 0.5; done
dsn="postgresql://test:test@127.0.0.1:$port/test"

docker run --rm --network host -e DB__URL="$dsn" -v "$repo/gateway/app:/app/app:ro" \
    "$gateway_image" python -m app.db.migra

docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -e GOCACHE=/tmp/gocache \
    -e TEST_DATABASE_URL="$dsn" -v "$here":/src -v tabularia-gomod:/go/pkg/mod -w /src "$go_image" \
    sh -c 'test -z "$(gofmt -l .)" || { gofmt -l .; echo "gofmt: the files above"; exit 1; }; go vet ./... && go test -count=1 ./... &&
        cd dataapi && go vet ./... && go test -count=1 ./...'
