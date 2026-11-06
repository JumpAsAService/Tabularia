#!/bin/sh
# Python gateway vs Go gateway, one container each with the same limits, on the same database
# and the same data, under the same closed-loop load. Results in $OUT (a new temporary
# directory unless given): results.jsonl, the docker stats of each run, idle.txt.
# Usage: gateway-go/scripts/bench/bench.sh   (CPUS, MEM, DUR override the limits)
set -eu
B=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$(mktemp -d)}
REPO=$(cd "$B/../../.." && pwd)
NET=gwbench
CPUS=${CPUS:-2}
MEM=${MEM:-2g}
DUR=${DUR:-20s}
PY_IMAGE=tabularia-gateway:latest
GO_IMAGE=tabularia-gateway-go:bench
FAKE=tabularia-fakeengine:bench
secret=$(openssl rand -hex 32)
fernet=$(openssl rand -base64 32 | tr '+/' '-_')
admin_pw=$(openssl rand -hex 16)
editor_pw="Bench-$(openssl rand -hex 8)-x9"
C="gwb-pg gwb-eng gwb-py gwb-ai gwb-go"
cleanup() { docker rm -f $C >/dev/null 2>&1; docker network rm $NET >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker build -q -t $GO_IMAGE "$REPO/gateway-go" >/dev/null
docker build -q --build-arg PROGRAM=fakeengine -t $FAKE "$REPO/gateway-go" >/dev/null
docker run --rm -v "$REPO/gateway-go":/src -v tabularia-gomod:/go/pkg/mod -v "$OUT":/out -w /src -e CGO_ENABLED=0 \
    -e HOME=/tmp -e GOCACHE=/tmp/gc --user "$(id -u):$(id -g)" golang:1.27.3-bookworm go build -o /out/loadgen-bin ./scripts/bench/loadgen
docker network create $NET >/dev/null
docker run -d --name gwb-pg --network $NET --cpus 4 -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t \
    postgres:16-alpine -c max_connections=300 >/dev/null
until docker exec gwb-pg pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
sleep 2
docker run -d --name gwb-eng --network $NET $FAKE >/dev/null
common="-e DB__HOST=gwb-pg -e DB__USER=t -e DB__PASSWORD=t -e DB__NAME=t -e JWT__SECRET=$secret
 -e SECURITY__FERNET_KEY=$fernet -e AUTH__ADMIN_PASSWORD=$admin_pw -e ENGINE__BASE_URL=http://gwb-eng:8000"
docker run --rm --network $NET $common -v "$REPO/gateway/app:/app/app:ro" $PY_IMAGE python -m app.db.migra >/dev/null 2>&1
# the Python gateway as production runs it: one uvicorn process per container
docker run -d --name gwb-py --network $NET --cpus $CPUS --memory $MEM $common -e APP__ROLE=api \
    -v "$REPO/gateway/app:/app/app:ro" $PY_IMAGE \
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*' --timeout-keep-alive 130 >/dev/null
docker run -d --name gwb-ai --network $NET $common -e APP__ROLE=ai -v "$REPO/gateway/app:/app/app:ro" $PY_IMAGE \
    uvicorn app.main:app --host 0.0.0.0 --port 8000 >/dev/null
docker run -d --name gwb-go --network $NET --cpus $CPUS --memory $MEM $common -e APP__ROLE=api \
    -e GATEWAY_GO__AI_SERVICE=http://gwb-ai:8000 -e GATEWAY_GO__FORWARDED_ALLOW_IPS='*' $GO_IMAGE >/dev/null
health() { docker run --rm --network $NET $PY_IMAGE python -c "import urllib.request;urllib.request.urlopen('http://$1:8000/health',timeout=2)" >/dev/null 2>&1; }
for g in gwb-py gwb-ai gwb-go; do for _ in $(seq 120); do health $g && break; sleep 1; done; done

docker run --rm --network $NET -v "$B":/b:ro $PY_IMAGE python /b/seed.py http://gwb-py:8000 "$admin_pw" "$editor_pw" > "$OUT/seed.out"
eval "$(cat "$OUT/seed.out")"
[ -n "${FOLDER:-}" ] || { echo "seed failed"; exit 1; }
[ -z "${SEED_ONLY:-}" ] || { echo "seed ok"; exit 0; }
echo "idle memory: $(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' gwb-py gwb-go | tr '\n' ';')" | tee "$OUT/idle.txt"

run() {  # gateway label paths concurrency
    stats="$OUT/stats-$1-$2-$4.txt"; : > "$stats"
    ( while :; do docker stats --no-stream --format '{{.CPUPerc}} {{.MemUsage}}' "$1" >> "$stats" 2>/dev/null; done ) &
    sp=$!
    docker run --rm --network $NET -v "$OUT":/o:ro golang:1.27.3-bookworm /o/loadgen-bin \
        -base "http://$1:8000" -token "$EDITOR_TOKEN" -paths "$3" -c "$4" -d "$DUR" -w 5s -label "$1 $2" \
        | tee -a "$OUT/results.jsonl"
    kill $sp 2>/dev/null; wait $sp 2>/dev/null || true
}
: > "$OUT/results.jsonl"
MIX="/auth/me,/projects,/projects/$FOLDER/flows,/flows,/search?q=Flow"
if [ -n "${EXTRA:-}" ]; then
    for c in 1 16; do for g in gwb-py gwb-go; do run $g flows /flows $c; run $g search "/search?q=Flow" $c; done; done
else
for c in 1 16 64; do
    for g in gwb-py gwb-go; do
        run $g me /auth/me $c
        run $g projects /projects $c
        run $g folder "/projects/$FOLDER/flows" $c
        run $g mix "$MIX" $c
    done
done
fi
echo "done: $OUT"
