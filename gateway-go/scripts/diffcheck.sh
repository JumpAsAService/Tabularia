#!/bin/sh
# The two gateways side by side on a disposable stack, and the same requests to both
# (cmd/diffcheck): PostgreSQL brought to the schema by this branch's migrations, the Python
# gateway from this branch's source, the Go gateway built from it, both reading the same
# database. Nothing of an installation is touched; everything is removed at the end.
#
#   gateway-go/scripts/diffcheck.sh
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
repo=$(cd "$here/.." && pwd)
go_image=golang:1.27.3-bookworm
gateway_image=${GATEWAY_IMAGE:-tabularia-gateway:latest}
id=gwgo-diff-$$
net=$id-net
secret=$(openssl rand -hex 32)
fernet=$(openssl rand -base64 32 | tr '+/' '-_')
admin_password=$(openssl rand -hex 16)
cleanup() { docker rm -f "$id-pg" "$id-py" "$id-go" >/dev/null 2>&1; docker network rm "$net" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker build -q -t tabularia-gateway-go:diff "$here" >/dev/null
docker network create "$net" >/dev/null
docker run -d --name "$id-pg" --network "$net" -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t \
    -p 127.0.0.1::5432 postgres:16-alpine >/dev/null
until docker exec "$id-pg" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
sleep 1
until docker exec "$id-pg" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
pg_port=$(docker port "$id-pg" 5432/tcp | head -1 | sed 's/.*://')

env="-e DB__HOST=$id-pg -e DB__USER=t -e DB__PASSWORD=t -e DB__NAME=t -e JWT__SECRET=$secret -e SECURITY__FERNET_KEY=$fernet -e AUTH__ADMIN_PASSWORD=$admin_password"
docker run --rm --network "$net" $env -v "$repo/gateway/app:/app/app:ro" "$gateway_image" python -m app.db.migra >/dev/null 2>&1
docker run -d --name "$id-py" --network "$net" $env -p 127.0.0.1::8000 -v "$repo/gateway/app:/app/app:ro" \
    "$gateway_image" uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' >/dev/null
docker run -d --name "$id-go" --network "$net" $env -p 127.0.0.1::8000 \
    -e GATEWAY_GO__UPSTREAM="http://$id-py:8000" -e GATEWAY_GO__FORWARDED_ALLOW_IPS='*' tabularia-gateway-go:diff >/dev/null
py_port=$(docker port "$id-py" 8000/tcp | head -1 | sed 's/.*://')
go_port=$(docker port "$id-go" 8000/tcp | head -1 | sed 's/.*://')
for port in "$py_port" "$go_port"; do
    for _ in $(seq 60); do curl -sf "http://127.0.0.1:$port/health" >/dev/null && break; sleep 1; done
done

docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -e GOCACHE=/tmp/gocache \
    -e DIFF_DSN="postgresql://t:t@127.0.0.1:$pg_port/t" -e DIFF_SECRET="$secret" \
    -e DIFF_PYTHON="http://127.0.0.1:$py_port" -e DIFF_GO="http://127.0.0.1:$go_port" \
    -v "$here":/src -v tabularia-gomod:/go/pkg/mod -w /src "$go_image" go run ./cmd/diffcheck || {
        echo "--- Go gateway's log"; docker logs --tail 20 "$id-go"; echo "--- Python gateway's log"; docker logs --tail 20 "$id-py"; exit 1; }
