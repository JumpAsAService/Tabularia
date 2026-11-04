#!/bin/sh
# The two gateways compared on twin disposable stacks (cmd/diffcheck): in stack A the Python
# gateway answers; in stack B the Go gateway, with a Python gateway behind it for the routes
# not ported yet and the AI service (the same image, APP__ROLE=ai) for the assistant's routes
# and the SQL parsing. Each stack has its own PostgreSQL, brought to the schema by this branch's
# migrations, and its own fake engine. Nothing of an installation is touched; everything is
# removed at the end.
#
#   gateway-go/scripts/diffcheck.sh                 every suite
#   DIFF_ONLY=flows,projects gateway-go/scripts/diffcheck.sh
#   DIFF_ENV="SCRIPTS__ENABLED=true KUBERNETES__API_URL=http://{engine}:8000" …
#                    more variables for every gateway of both stacks; {engine} is the
#                    stack's fake engine (to stand in for another API, as Kubernetes')
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
containers="$id-pga $id-pgb $id-enga $id-engb $id-pya $id-pyb $id-ai $id-go $id-ticka $id-tickb"
bin=$(mktemp -d)  # cmd/jobsonce, built for the round server of stack B
# images of this run only: comparisons run side by side, each with its own build
go_gateway=tabularia-gateway-go:$id
fake_engine=tabularia-fakeengine:$id
cleanup() {
    docker rm -f $containers >/dev/null 2>&1; docker network rm "$net" >/dev/null 2>&1
    docker rmi "$go_gateway" "$fake_engine" >/dev/null 2>&1 || true
    rm -rf "$bin"
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
    -e AUTH__ADMIN_PASSWORD=$admin_password -e APP__ROLE=api -e AUTH__FIRST_RUN_SETUP=true -e MONITORING__CACHE_SECONDS=0"
extra() {  # DIFF_ENV for the stack whose engine is $1
    for kv in ${DIFF_ENV:-}; do printf -- '-e %s ' "$(printf '%s' "$kv" | sed "s/{engine}/$1/g")"; done
}
python_gateway() {  # name, its database, its engine, its Splunk[, more options]
    docker run -d --name "$1" --network "$net" $common $4 ${5:-} $(extra "$3") -e DB__HOST="$2" -e ENGINE__BASE_URL="http://$3:8000" \
        -e MONITORING__NODE_EXPORTER_URL="http://$3:8000/metrics" \
        -p 127.0.0.1::8000 -v "$repo/gateway/app:/app/app:ro" "$gateway_image" \
        uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' >/dev/null
}
for s in a b; do
    docker run --rm --network "$net" $common -e DB__HOST="$id-pg$s" -v "$repo/gateway/app:/app/app:ro" \
        "$gateway_image" python -m app.db.migra >/dev/null 2>&1
    if [ "$s" = a ]; then python_gateway "$id-py$s" "$id-pg$s" "$id-eng$s" "$siem_a"
    else python_gateway "$id-py$s" "$id-pg$s" "$id-eng$s" "$siem_b"; fi
done
# stack B's AI service: the same image on B's database (docs/specs/gateway-go.md, «The AI service»)
python_gateway "$id-ai" "$id-pgb" "$id-engb" "$siem_b" "-e APP__ROLE=ai"
docker run -d --name "$id-go" --network "$net" $common $siem_b $(extra "$id-engb") -e DB__HOST="$id-pgb" -e ENGINE__BASE_URL="http://$id-engb:8000" \
    -e MONITORING__NODE_EXPORTER_URL="http://$id-engb:8000/metrics" \
    -p 127.0.0.1::8000 -e GATEWAY_GO__UPSTREAM="http://$id-pyb:8000" -e GATEWAY_GO__FORWARDED_ALLOW_IPS='*' \
    -e GATEWAY_GO__AI_SERVICE="http://$id-ai:8000" \
    "$go_gateway" >/dev/null

# the comparison «a un giro» (cmd/diffcheck/ticks.go): ONE round of a background loop, in A
# by the Python service (ticks/ticks.py), in B by cmd/jobsonce — both in the gateway's image,
# which has the programs a loop runs (pg_dump), on the stack's own database and engine, with
# the same signature as the orchestrator that runs them
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e GOCACHE=/tmp/gocache -e CGO_ENABLED=0 -v "$here":/src -v "$bin":/out \
    -v tabularia-gomod:/go/pkg/mod -w /src "$go_image" go build -o /out/jobsonce ./cmd/jobsonce
instance="-e GATEWAY_GO__INSTANCE=diffcheck:1:0000beef"
docker run -d --name "$id-ticka" --network "$net" $common $siem_a $(extra "$id-enga") $instance -e DB__HOST="$id-pga" \
    -e ENGINE__BASE_URL="http://$id-enga:8000" -p 127.0.0.1::8000 -v "$repo/gateway/app:/app/app:ro" \
    -v "$here/cmd/diffcheck/ticks:/ticks:ro" "$gateway_image" python /ticks/ticks.py >/dev/null
docker run -d --name "$id-tickb" --network "$net" $common $siem_b $(extra "$id-engb") $instance -e DB__HOST="$id-pgb" \
    -e ENGINE__BASE_URL="http://$id-engb:8000" -e GATEWAY_GO__AI_SERVICE="http://$id-ai:8000" -p 127.0.0.1::8000 -v "$bin/jobsonce:/jobsonce:ro" \
    "$gateway_image" /jobsonce serve >/dev/null

port() { docker port "$1" "$2/tcp" | head -1 | sed 's/.*://'; }
for c in "$id-pya" "$id-pyb" "$id-ai" "$id-go" "$id-ticka"; do
    for _ in $(seq 90); do curl -sf "http://127.0.0.1:$(port "$c" 8000)/health" >/dev/null && break; sleep 1; done
done

docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -e GOCACHE=/tmp/gocache \
    -e DIFF_SECRET="$secret" -e DIFF_FERNET="$fernet" -e DIFF_ONLY="${DIFF_ONLY:-}" -e DIFF_VERBOSE="${DIFF_VERBOSE:-}" -e DIFF_CLIP="${DIFF_CLIP:-}" \
    -e DIFF_A_DSN="postgresql://t:t@127.0.0.1:$(port "$id-pga" 5432)/t" -e DIFF_B_DSN="postgresql://t:t@127.0.0.1:$(port "$id-pgb" 5432)/t" \
    -e DIFF_A_URL="http://127.0.0.1:$(port "$id-pya" 8000)" -e DIFF_B_URL="http://127.0.0.1:$(port "$id-go" 8000)" \
    -e DIFF_A_ENGINE="http://127.0.0.1:$(port "$id-enga" 8000)" -e DIFF_B_ENGINE="http://127.0.0.1:$(port "$id-engb" 8000)" \
    -e DIFF_A_TICKS="http://127.0.0.1:$(port "$id-ticka" 8000)" -e DIFF_B_TICKS="http://127.0.0.1:$(port "$id-tickb" 8000)" \
    -e DIFF_A_ENGINE_HOST="$id-enga:8000" -e DIFF_B_ENGINE_HOST="$id-engb:8000" \
    -v "$here":/src -v tabularia-gomod:/go/pkg/mod -w /src "$go_image" go run ./cmd/diffcheck || {
        echo "--- the Go gateway's log"; docker logs --tail 20 "$id-go"
        echo "--- the Python gateway A's log"; docker logs --tail 20 "$id-pya"
        echo "--- the AI service's log"; docker logs --tail 20 "$id-ai"
        echo "--- the Python round server's log"; docker logs --tail 30 "$id-ticka"
        echo "--- the Go round server's log"; docker logs --tail 30 "$id-tickb"; exit 1; }
