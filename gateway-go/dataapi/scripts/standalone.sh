#!/bin/sh
# A data API's pod on its own against MinIO (docs/specs/data-apis.md, piece 2), on a
# disposable stack: PostgreSQL brought to the schema by this branch's migrations, MinIO, the
# engine (for the storage's signed links), the Go gateway and the pod, in a container of 1 GB
# and 2 CPUs. A real datasource (ROWS rows) in the storage, a data API and a key made through
# the gateway's routes; then lookups, filters, cursors, the count, CSV, a revoked key and a
# refresh reaching the pod within the poll, and the latency of a lookup by key. Nothing of an
# installation is touched; everything is removed at the end.
#
#   gateway-go/dataapi/scripts/standalone.sh            20 million rows
#   ROWS=100000 gateway-go/dataapi/scripts/standalone.sh
#   KEEP=1 …   leave the stack up at the end (remove it with: docker rm -f $(docker ps -aq -f name=dapi-sa-))
set -eu
here=$(cd "$(dirname "$0")/../.." && pwd)
repo=$(cd "$here/.." && pwd)
rows=${ROWS:-20000000}
lookups=${LOOKUPS:-500}
gateway_image=${GATEWAY_IMAGE:-tabularia-gateway:latest}
engine_image=${ENGINE_IMAGE:-tabularia-backend:latest}
id=dapi-sa-$$
net=$id-net
go_gateway=tabularia-gateway-go:$id
pod_image=tabularia-data-api:$id
work=$(mktemp -d)
# the stack's own secrets, made here and never printed
secret=$(openssl rand -hex 32)
fernet=$(openssl rand -base64 32 | tr '+/' '-_')
s3_user=dapi$(openssl rand -hex 6)
s3_password=$(openssl rand -hex 20)
password=$(openssl rand -hex 16)
pod_token=tdp_$(openssl rand -base64 60 | tr -dc 'A-Za-z0-9' | head -c 40)
bucket=tabularia
containers="$id-pg $id-minio $id-engine $id-gw $id-pod"
cleanup() {
    if [ -n "${KEEP:-}" ]; then echo "kept: $containers"; return; fi
    docker rm -f -v $containers >/dev/null 2>&1 || true
    docker network rm "$net" >/dev/null 2>&1 || true
    docker rmi "$go_gateway" "$pod_image" >/dev/null 2>&1 || true
    docker run --rm -v "$work:/w" alpine rm -rf /w/files >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 1' INT TERM
step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAILED: $*"; exit 1; }

step "images"
docker build -q -t "$go_gateway" "$here" >/dev/null
docker build -q -t "$pod_image" -f "$here/dataapi/Dockerfile" "$here" >/dev/null
docker network create "$net" >/dev/null

step "PostgreSQL, MinIO, the engine"
docker run -d --name "$id-pg" --network "$net" -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t postgres:16-alpine >/dev/null
docker run -d --name "$id-minio" --network "$net" -e MINIO_ROOT_USER="$s3_user" -e MINIO_ROOT_PASSWORD="$s3_password" \
    pgsty/minio:RELEASE.2026-08-04T00-00-00Z server /data >/dev/null
until docker exec "$id-pg" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
sleep 1
until docker exec "$id-pg" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
docker run --rm --network "$net" -e DB__URL="postgresql://t:t@$id-pg:5432/t" -v "$repo/gateway/app:/app/app:ro" \
    "$gateway_image" python -m app.db.migra >/dev/null
storage="-e STORAGE__ENDPOINT=http://$id-minio:9000 -e STORAGE__ACCESS_KEY=$s3_user -e STORAGE__SECRET_KEY=$s3_password -e STORAGE__BUCKET=$bucket"
docker run -d --name "$id-engine" --network "$net" $storage -e SECURITY__FERNET_KEY="$fernet" -e REDIS__HOST=redis.invalid \
    "$engine_image" uvicorn app.api.main:app --host 0.0.0.0 --port 8000 >/dev/null

# the datasource's parquet, written by DuckDB in the engine's image and put in the storage
cat > "$work/data.py" <<'EOF'
import os, sys, boto3, duckdb
rows, key, seed = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
s3 = boto3.client("s3", endpoint_url=os.environ["STORAGE__ENDPOINT"], aws_access_key_id=os.environ["STORAGE__ACCESS_KEY"],
                  aws_secret_access_key=os.environ["STORAGE__SECRET_KEY"], region_name="us-east-1")
bucket = os.environ["STORAGE__BUCKET"]
try:
    s3.create_bucket(Bucket=bucket)
except s3.exceptions.BucketAlreadyOwnedByYou:
    pass
duckdb.sql("SET memory_limit='1GB'; SET threads=2; SET temp_directory='/tmp/spill'")
# as a datasource is written: groups of a million rows, in no useful order; customer 1 to rows
duckdb.sql(f"""COPY (SELECT i AS customer_id, 'customer ' || i AS name,
    ['North', 'South', 'Centre'][1 + (i % 3)::INTEGER] AS region, ((i * {seed}) % 100000 / 100)::DECIMAL(12,2) AS score,
    DATE '2020-01-01' + (i % 2000)::INTEGER AS signup FROM range(1, {rows} + 1) t(i) ORDER BY hash(i + {seed}))
    TO '/tmp/data.parquet' (FORMAT parquet, ROW_GROUP_SIZE 1000000)""")
s3.upload_file("/tmp/data.parquet", bucket, key)
print(os.path.getsize("/tmp/data.parquet"))
os.remove("/tmp/data.parquet")
EOF
mkdir -p "$work/files" && chmod 0777 "$work/files"
upload() {  # rows key seed: the files on the host's disk, not in the container's memory
    docker run --rm --network "$net" $storage -v "$work/data.py:/w/data.py:ro" -v "$work/files:/tmp" --memory 3g \
        "$engine_image" python /w/data.py "$1" "$2" "$3"
}
step "the datasource: $rows rows"
size=$(upload "$rows" datasets/customers-1.parquet 7)
echo "parquet: $((size / 1048576)) MB"

step "the gateway"
docker run -d --name "$id-gw" --network "$net" -p 127.0.0.1::8000 -e DB__URL="postgresql://t:t@$id-pg:5432/t" -e JWT__SECRET="$secret" \
    -e SECURITY__FERNET_KEY="$fernet" -e APP__ROLE=api -e ENGINE__BASE_URL="http://$id-engine:8000" -e ENGINE__BUCKET=$bucket \
    -e GATEWAY_GO__AI_SERVICE=http://ai.invalid:8000 -e SCRIPTS__ENABLED=true -e SCRIPTS__NAMESPACE=tabularia-scripts \
    -e SCRIPTS__REGISTRY=registry.invalid:5000 -e DATA_API__ENABLED=true -e DATA_API__POLL_SECONDS=5 "$go_gateway" >/dev/null
gw=http://127.0.0.1:$(docker port "$id-gw" 8000/tcp | head -1 | sed 's/.*://')
for _ in $(seq 60); do curl -fs -o /dev/null "$gw/health" && break; sleep 0.5; done
sql() { docker exec -i "$id-pg" psql -U t -d t -qtA -v ON_ERROR_STOP=1 "$@"; }
hash=$(docker run --rm -e P="$password" "$gateway_image" python -c 'import bcrypt, os; print(bcrypt.hashpw(os.environ["P"].encode(), bcrypt.gensalt()).decode())')
user=$(sql -c "INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
    VALUES ('analyst@example.test', 'Analyst', '$hash', true, false, false, false, now()) RETURNING id")
folder=$(sql -c "INSERT INTO projects (name, description, created_at) VALUES ('CRM', '', now()) RETURNING id")
sql -c "INSERT INTO permissions (project_id, user_id, capability) VALUES ($folder, $user, 'manage')" >/dev/null
columns='[{"name": "customer_id", "dtype": "Int64"}, {"name": "name", "dtype": "String"}, {"name": "region", "dtype": "String"}, {"name": "score", "dtype": "Decimal(12, 2)"}, {"name": "signup", "dtype": "Date"}]'
ds=$(sql -c "INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns, sort_keys, column_descriptions, kind,
    created_at, updated_at) VALUES ('Customers', '', $folder, $user, '$bucket', 'datasets/customers-1.parquet', $rows, '$columns', '[]', '{}',
    'flow', now(), now()) RETURNING id")
login=$(curl -fsS "$gw/auth/login" -H 'Content-Type: application/json' -d "{\"email\": \"analyst@example.test\", \"password\": \"$password\"}")
session=$(printf '%s' "$login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
api() { curl -fsS -H "Authorization: Bearer $session" -H 'Content-Type: application/json' "$@"; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

step "the data API and its key, through the gateway's routes"
made=$(api "$gw/projects/$folder/data-apis" -d "{\"name\": \"CRM customers\", \"slug\": \"crm\", \"resources\": [{\"name\": \"customers\",
    \"source\": {\"kind\": \"datasource\", \"id\": $ds}, \"columns\": [\"customer_id\", \"name\", \"region\", \"score\", \"signup\"],
    \"filters\": [{\"column\": \"customer_id\", \"operators\": [\"eq\", \"in\", \"gte\", \"lt\"]}, {\"column\": \"region\", \"operators\": [\"eq\", \"in\"]},
    {\"column\": \"signup\", \"operators\": [\"gte\", \"lte\"]}, {\"column\": \"score\", \"operators\": [\"gte\"]}], \"max_page\": 1000}]}")
apid=$(printf '%s' "$made" | json 'd["id"]')
expires=$(date -u -d '+30 days' +%Y-%m-%dT%H:%M:%SZ)
key=$(api "$gw/data-apis/$apid/keys" -d "{\"name\": \"CRM production\", \"expires_at\": \"$expires\", \"rate_per_minute\": 100000}" | json 'd["secret"]')
keyid=$(sql -c "SELECT id FROM data_api_keys WHERE data_api_id = $apid")
api -X POST "$gw/data-apis/$apid/activate" >/dev/null
# the pod token: what the reconcile loop will do at the activation (dataapis.RotatePodToken)
sql -c "UPDATE data_apis SET pod_token_hash = encode(sha256(convert_to('$pod_token', 'UTF8')), 'hex') WHERE id = $apid" >/dev/null

step "the pod (1 GB, 2 CPUs)"
start=$(date +%s)
docker run -d --name "$id-pod" --network "$net" --memory 1g --cpus 2 --read-only -v /data -p 127.0.0.1::8000 \
    -e DATA_API__ID="$apid" -e DATA_API__GATEWAY_URL="http://$id-gw:8000" -e DATA_API__POD_TOKEN="$pod_token" "$pod_image" >/dev/null
pod=http://127.0.0.1:$(docker port "$id-pod" 8000/tcp | head -1 | sed 's/.*://')
for _ in $(seq 600); do curl -fs -o /dev/null "$pod/readyz" && break; sleep 0.5; done
curl -fs -o /dev/null "$pod/readyz" || { docker logs "$id-pod" | tail -20; fail "the pod is not ready"; }
echo "ready in $(( $(date +%s) - start )) s"
docker logs "$id-pod" 2>&1 | grep '"prepared"' | tail -1

get() { curl -s -H "Authorization: Bearer $key" "$@"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
check() { if [ "$2" != "$3" ]; then fail "$1: '$2', expected '$3'"; fi; echo "ok  $1"; }

step "answers"
check "no key" "$(code "$pod/data/crm/customers?customer_id=1")" 401
check "lookup" "$(get "$pod/data/crm/customers?customer_id=4242" | json 'd["data"][0]["name"]')" "customer 4242"
check "lookup through the gateway's key header" "$(curl -s -H "X-API-Key: $key" "$pod/data/crm/customers?customer_id=7&_columns=region" | json 'd["data"]')" "[{'region': 'South'}]"
check "in" "$(get "$pod/data/crm/customers?customer_id__in=5,6,7,99999999999&_columns=customer_id" | json 'len(d["data"])')" 3
check "filters and count" "$(get "$pod/data/crm/customers?region=North&customer_id__lt=3001&signup__gte=2020-01-01&_count=true&_limit=10" | json 'd["count"]')" 1000
check "wrong type" "$(get "$pod/data/crm/customers?customer_id=abc" | json 'd["code"]')" bad_value
first=$(get "$pod/data/crm/customers?region=South&customer_id__lt=2500&_limit=300&_columns=customer_id")
next=$(printf '%s' "$first" | json 'd["next"]')
second=$(get "$pod/data/crm/customers?_cursor=$next")
check "cursor" "$(printf '%s' "$second" | json 'd["data"][0]["customer_id"] > 0 and len(d["data"]) > 0')" True
check "csv" "$(get "$pod/data/crm/customers?customer_id=10&_format=csv" | head -1)" "customer_id,name,region,score,signup"
check "openapi" "$(get "$pod/data/crm/openapi.json" | json 'd["openapi"]')" 3.1.0
version=$(get "$pod/data/crm/customers?customer_id=1" | json 'd["version"]')

step "latency of a lookup by key ($lookups lookups, random customers)"
python3 - "$pod" "$key" "$rows" "$lookups" <<'EOF'
import http.client, random, sys, time, urllib.parse
base, key, rows, n = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
u = urllib.parse.urlparse(base)
c = http.client.HTTPConnection(u.hostname, u.port)
lat = []
for i in range(n + 20):
    cid = random.randint(1, rows)
    t = time.perf_counter()
    c.request("GET", f"/data/crm/customers?customer_id={cid}", headers={"Authorization": "Bearer " + key})
    r = c.getresponse(); body = r.read()
    if r.status != 200 or f'"customer_id":{cid}'.encode() not in body:
        sys.exit(f"lookup {cid}: {r.status} {body[:200]}")
    if i >= 20:
        lat.append((time.perf_counter() - t) * 1000)
lat.sort()
print(f"client side: avg {sum(lat)/len(lat):.2f} ms, p50 {lat[len(lat)//2]:.2f}, p99 {lat[int(len(lat)*0.99)]:.2f}, max {lat[-1]:.2f}")
t = time.perf_counter()
c.request("GET", "/data/crm/customers?region=North&signup__gte=2024-01-01&_limit=1000", headers={"Authorization": "Bearer " + key})
r = c.getresponse(); r.read()
print(f"filter on two other columns, page of 1000: {(time.perf_counter() - t) * 1000:.1f} ms")
t = time.perf_counter()
c.request("GET", "/data/crm/customers?region=North&_count=true&_limit=1", headers={"Authorization": "Bearer " + key})
r = c.getresponse(); r.read()
print(f"count on a column: {(time.perf_counter() - t) * 1000:.1f} ms")
EOF
docker logs "$id-pod" 2>/dev/null | python3 -c '
import json, sys
ms = sorted(json.loads(l)["ms"] for l in sys.stdin if "\"customer_id=" not in l and "\"request\"" in l and "\"rows\":1," in l)
if ms: print(f"server side (the pod'"'"'s log): avg {sum(ms)/len(ms):.2f} ms, p50 {ms[len(ms)//2]:.2f}, p99 {ms[int(len(ms)*0.99)]:.2f}")'
# the program's own memory (the cgroup's also counts the page cache of the files it wrote)
docker exec "$id-pod" sh -c 'grep -E "^Vm(RSS|HWM)" /proc/1/status | tr -s " \t" " "; echo "cgroup: anon $(( $(sed -n "s/^anon //p" /sys/fs/cgroup/memory.stat) / 1048576 )) MB, file cache $(( $(sed -n "s/^file //p" /sys/fs/cgroup/memory.stat) / 1048576 )) MB (limit 1024)"; du -sm /data | sed "s/\t.*/ MB on disk/"'

step "a revoked key is refused within the poll"
api -X POST "$gw/data-apis/$apid/keys/$keyid/revoke" >/dev/null
t0=$(date +%s)
for _ in $(seq 60); do [ "$(code -H "Authorization: Bearer $key" "$pod/data/crm/customers?customer_id=1")" = 401 ] && break; sleep 0.5; done
check "revoked" "$(get "$pod/data/crm/customers?customer_id=1" | json 'd["code"]')" key_revoked
echo "refused after $(( $(date +%s) - t0 )) s (poll 5 s)"
key=$(api "$gw/data-apis/$apid/keys" -d "{\"name\": \"CRM production 2\", \"expires_at\": \"$expires\", \"rate_per_minute\": 100000}" | json 'd["secret"]')
for _ in $(seq 60); do [ "$(code -H "Authorization: Bearer $key" "$pod/data/crm/customers?customer_id=1")" = 200 ] && break; sleep 0.5; done

step "a refresh reaches the pod within the poll"
old=$(get "$pod/data/crm/customers?customer_id=4242" | json 'd["data"][0]["score"]')
upload "$rows" datasets/customers-2.parquet 13 >/dev/null
sql -c "UPDATE datasources SET key = 'datasets/customers-2.parquet', updated_at = now() + interval '1 second' WHERE id = $ds" >/dev/null
t0=$(date +%s)
for _ in $(seq 600); do
    [ "$(get "$pod/data/crm/customers?customer_id=1" | json 'd.get("version")')" != "$version" ] && break; sleep 0.5
done
new=$(get "$pod/data/crm/customers?customer_id=4242" | json 'd["data"][0]["score"]')
[ "$new" != "$old" ] || fail "the refresh did not arrive"
check "the old cursor after the refresh" "$(get "$pod/data/crm/customers?_cursor=$next" | json 'd["code"]')" data_changed
echo "the new version served $(( $(date +%s) - t0 )) s after the refresh (poll 5 s, plus the download and the preparation)"
docker logs "$id-pod" 2>&1 | grep '"prepared"' | tail -1

step "usage"
sleep 6
sql -c "SELECT 'usage rows ' || count(*) || ', requests ' || coalesce(sum(requests), 0) || ', rows ' || coalesce(sum(rows), 0) FROM data_api_usage WHERE data_api_id = $apid"

step "SIGTERM"
t0=$(date +%s)
docker stop -t 30 "$id-pod" >/dev/null
echo "stopped in $(( $(date +%s) - t0 )) s, exit $(docker inspect -f '{{.State.ExitCode}}' "$id-pod")"
docker logs "$id-pod" 2>&1 | tail -2
echo
echo "PASSED"
