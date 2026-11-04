#!/bin/sh
# The two gateways compared on twin disposable stacks (cmd/diffcheck): in stack A the Python
# gateway answers; in stack B the Go gateway, with a Python gateway behind it for the routes
# not ported yet. Each stack has its own PostgreSQL, brought to the schema by this branch's
# migrations, and its own fake engine. Nothing of an installation is touched; everything is
# removed at the end.
#
#   gateway-go/scripts/diffcheck.sh                 every suite
#   DIFF_ONLY=flows,projects gateway-go/scripts/diffcheck.sh
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
containers="$id-pga $id-pgb $id-enga $id-engb $id-pya $id-pyb $id-go"
# images of this run only: comparisons run side by side, each with its own build
go_gateway=tabularia-gateway-go:$id
fake_engine=tabularia-fakeengine:$id
cleanup() {
    docker rm -f $containers >/dev/null 2>&1; docker network rm "$net" >/dev/null 2>&1
    docker rmi "$go_gateway" "$fake_engine" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -q -t "$go_gateway" "$here" >/dev/null
docker build -q --build-arg PROGRAM=fakeengine -t "$fake_engine" "$here" >/dev/null
docker network create "$net" >/dev/null
for s in a b; do
    docker run -d --name "$id-pg$s" --network "$net" -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t \
        -p 127.0.0.1::5432 postgres:16-alpine >/dev/null
    docker run -d --name "$id-eng$s" --network "$net" -p 127.0.0.1::8000 "$fake_engine" >/dev/null
done
for s in a b; do
    until docker exec "$id-pg$s" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
    sleep 1
    until docker exec "$id-pg$s" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
done

# the fake engines stand in for Splunk too: «siem.test» is each gateway's own engine
ip() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"; }
siem_a="--add-host siem.test:$(ip "$id-enga")"
siem_b="--add-host siem.test:$(ip "$id-engb")"

common="-e DB__USER=t -e DB__PASSWORD=t -e DB__NAME=t -e JWT__SECRET=$secret -e SECURITY__FERNET_KEY=$fernet
    -e AUTH__ADMIN_PASSWORD=$admin_password -e APP__ROLE=api"
python_gateway() {  # name, its database, its engine, its Splunk
    docker run -d --name "$1" --network "$net" $common $4 -e DB__HOST="$2" -e ENGINE__BASE_URL="http://$3:8000" \
        -p 127.0.0.1::8000 -v "$repo/gateway/app:/app/app:ro" "$gateway_image" \
        uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' >/dev/null
}
for s in a b; do
    docker run --rm --network "$net" $common -e DB__HOST="$id-pg$s" -v "$repo/gateway/app:/app/app:ro" \
        "$gateway_image" python -m app.db.migra >/dev/null 2>&1
    if [ "$s" = a ]; then python_gateway "$id-py$s" "$id-pg$s" "$id-eng$s" "$siem_a"
    else python_gateway "$id-py$s" "$id-pg$s" "$id-eng$s" "$siem_b"; fi
done
docker run -d --name "$id-go" --network "$net" $common $siem_b -e DB__HOST="$id-pgb" -e ENGINE__BASE_URL="http://$id-engb:8000" \
    -p 127.0.0.1::8000 -e GATEWAY_GO__UPSTREAM="http://$id-pyb:8000" -e GATEWAY_GO__FORWARDED_ALLOW_IPS='*' \
    "$go_gateway" >/dev/null

port() { docker port "$1" "$2/tcp" | head -1 | sed 's/.*://'; }
for c in "$id-pya" "$id-pyb" "$id-go"; do
    for _ in $(seq 90); do curl -sf "http://127.0.0.1:$(port "$c" 8000)/health" >/dev/null && break; sleep 1; done
done

docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -e GOCACHE=/tmp/gocache \
    -e DIFF_SECRET="$secret" -e DIFF_ONLY="${DIFF_ONLY:-}" -e DIFF_VERBOSE="${DIFF_VERBOSE:-}" \
    -e DIFF_A_DSN="postgresql://t:t@127.0.0.1:$(port "$id-pga" 5432)/t" -e DIFF_B_DSN="postgresql://t:t@127.0.0.1:$(port "$id-pgb" 5432)/t" \
    -e DIFF_A_URL="http://127.0.0.1:$(port "$id-pya" 8000)" -e DIFF_B_URL="http://127.0.0.1:$(port "$id-go" 8000)" \
    -e DIFF_A_ENGINE="http://127.0.0.1:$(port "$id-enga" 8000)" -e DIFF_B_ENGINE="http://127.0.0.1:$(port "$id-engb" 8000)" \
    -v "$here":/src -v tabularia-gomod:/go/pkg/mod -w /src "$go_image" go run ./cmd/diffcheck || {
        echo "--- the Go gateway's log"; docker logs --tail 20 "$id-go"
        echo "--- the Python gateway A's log"; docker logs --tail 20 "$id-pya"; exit 1; }
