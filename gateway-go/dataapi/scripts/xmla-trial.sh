#!/bin/sh
# A disposable stack to try a spreadsheet's pivot table against a data API's XMLA endpoint
# (docs/specs/xmla.md), on a trusted local network, in plain HTTP. Same stack as standalone.sh
# (PostgreSQL, MinIO, the engine, the Go gateway, a pod of one data API), with two small
# datasources of a pasta and bakery company (orders, customers) as the two cubes of one data API,
# and the pod's port published on BIND:PORT. The stack is LEFT UP: the last lines say how to
# remove it. It is NOT a way to publish a data API: the key travels in clear text.
#
#   OUT=/path/to/connection.txt [BIND=192.168.1.10] [PORT=8099] gateway-go/dataapi/scripts/xmla-trial.sh
#
# The connection details (address, catalog, key, commands) go to the file OUT (mode 600), never
# to the screen. Images: GATEWAY_IMAGE (also the migrations'), ENGINE_IMAGE.
set -eu
here=$(cd "$(dirname "$0")/../.." && pwd)
repo=$(cd "$here/.." && pwd)
bind=${BIND:-127.0.0.1}
port=${PORT:-8099}
out=${OUT:-}
[ -n "$out" ] || { echo "OUT=<file> is required: the connection details are written there, not shown"; exit 2; }
gateway_image=${GATEWAY_IMAGE:-tabularia-gateway:latest}
engine_image=${ENGINE_IMAGE:-tabularia-backend:latest}
id=dapi-xt-$$
net=$id-net
go_gateway=tabularia-gateway-go:$id
pod_image=tabularia-data-api:$id
work=$(mktemp -d)
secret=$(openssl rand -hex 32)
fernet=$(openssl rand -base64 32 | tr '+/' '-_')
s3_user=dapi$(openssl rand -hex 6)
s3_password=$(openssl rand -hex 20)
password=$(openssl rand -hex 16)
pod_token=tdp_$(openssl rand -base64 60 | tr -dc 'A-Za-z0-9' | head -c 40)
bucket=tabularia
slug=pasta
catalog_name="Pasta and bakery"
containers="$id-pg $id-minio $id-engine $id-gw $id-pod"
passed=
cleanup() {
    rm -f "$work/data.py"; rmdir "$work" 2>/dev/null || true
    if [ -n "$passed" ] || [ -n "${KEEP:-}" ]; then return; fi
    echo "failed: removing the stack"
    docker rm -f -v $containers >/dev/null 2>&1 || true
    docker network rm "$net" >/dev/null 2>&1 || true
    docker rmi "$go_gateway" "$pod_image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 1' INT TERM
step() { printf '\n== %s\n' "$*"; }
fail() { echo "FAILED: $*"; exit 1; }
started=$(date +%s)

step "images"
docker build -q -t "$go_gateway" "$here" >/dev/null
docker build -q -t "$pod_image" -f "$here/dataapi/Dockerfile" "$here" >/dev/null
docker network create "$net" >/dev/null

step "PostgreSQL, MinIO, the engine"
docker run -d --name "$id-pg" --network "$net" --memory 512m -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t postgres:16-alpine >/dev/null
docker run -d --name "$id-minio" --network "$net" --memory 512m -e MINIO_ROOT_USER="$s3_user" -e MINIO_ROOT_PASSWORD="$s3_password" \
    pgsty/minio:RELEASE.2026-08-04T00-00-00Z server /data >/dev/null
until docker exec "$id-pg" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
sleep 1
until docker exec "$id-pg" pg_isready -U t -d t >/dev/null 2>&1; do sleep 0.5; done
docker run --rm --network "$net" --memory 512m -e DB__URL="postgresql://t:t@$id-pg:5432/t" -v "$repo/gateway/app:/app/app:ro" \
    "$gateway_image" python -m app.db.migra >/dev/null
storage="-e STORAGE__ENDPOINT=http://$id-minio:9000 -e STORAGE__ACCESS_KEY=$s3_user -e STORAGE__SECRET_KEY=$s3_password -e STORAGE__BUCKET=$bucket"
docker run -d --name "$id-engine" --network "$net" --memory 1g $storage -e SECURITY__FERNET_KEY="$fernet" -e REDIS__HOST=redis.invalid \
    "$engine_image" uvicorn app.api.main:app --host 0.0.0.0 --port 8000 >/dev/null

# the datasources' parquet: a few thousand rows at most, values that can be read and added up by eye
cat > "$work/data.py" <<'EOF'
import json, os, sys, boto3, duckdb
which, key = sys.argv[1], sys.argv[2]
s3 = boto3.client("s3", endpoint_url=os.environ["STORAGE__ENDPOINT"], aws_access_key_id=os.environ["STORAGE__ACCESS_KEY"],
                  aws_secret_access_key=os.environ["STORAGE__SECRET_KEY"], region_name="us-east-1")
bucket = os.environ["STORAGE__BUCKET"]
try:
    s3.create_bucket(Bucket=bucket)
except s3.exceptions.BucketAlreadyOwnedByYou:
    pass
countries = "['Italy', 'France', 'Germany', 'Spain', 'United Kingdom']"
if which == "orders":
    # 3000 orders over 2023-2025; price per product is fixed, amount = quantity * price;
    # channel is null on every 50th order, discount on every 10th
    query = f"""SELECT DATE '2023-01-01' + ((i * 37) % 1096)::INTEGER AS order_date,
        {countries}[1 + (i % 5)::INTEGER] AS country,
        CASE WHEN i % 50 = 0 THEN NULL ELSE ['Web', 'Retail', 'Wholesale'][1 + (i % 3)::INTEGER] END AS channel,
        ['Spaghetti', 'Penne', 'Fusilli', 'Tagliatelle', 'Grissini', 'Taralli', 'Focaccia', 'Biscotti'][1 + p] AS product,
        CASE WHEN p < 4 THEN 'Pasta' ELSE 'Bakery' END AS category,
        (1 + (i * 11) % 12)::BIGINT AS quantity,
        ([1.20, 1.30, 1.30, 2.50, 2.00, 2.40, 3.00, 3.50]::DECIMAL(12,2)[])[1 + p] AS price,
        ((1 + (i * 11) % 12) * ([1.20, 1.30, 1.30, 2.50, 2.00, 2.40, 3.00, 3.50]::DECIMAL(12,2)[])[1 + p])::DECIMAL(12,2) AS amount,
        (i % 5 = 0) AS promo,
        CASE WHEN i % 10 = 0 THEN NULL ELSE ([0, 5, 10, 15]::DECIMAL(5,2)[])[1 + (i % 4)::INTEGER] END AS discount
        FROM (SELECT i, ((i * 7 + i // 5) % 8)::INTEGER AS p FROM range(1, 3001) t(i)) ORDER BY i"""
else:
    query = f"""SELECT {countries}[1 + (i % 5)::INTEGER] AS country,
        ['Retail', 'Restaurant', 'Distributor'][1 + (i % 3)::INTEGER] AS segment,
        DATE '2020-01-01' + ((i * 29) % 1800)::INTEGER AS signup_date,
        (1 + (i * 7) % 40)::BIGINT AS stores
        FROM range(1, 121) t(i) ORDER BY i"""
duckdb.sql(f"COPY ({query}) TO '/tmp/data.parquet' (FORMAT parquet)")
rows, total = duckdb.sql("SELECT count(*), " + ("sum(amount)" if which == "orders" else "sum(stores)") + " FROM '/tmp/data.parquet'").fetchone()
s3.upload_file("/tmp/data.parquet", bucket, key)
print(json.dumps({"rows": rows, "total": str(total)}))
EOF
upload() {  # which key
    docker run --rm --network "$net" $storage -v "$work/data.py:/w/data.py:ro" --memory 1g "$engine_image" python /w/data.py "$1" "$2"
}
step "the datasources"
info_o=$(upload orders datasets/orders-1.parquet)
info_c=$(upload customers datasets/customers-1.parquet)
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
rows_o=$(printf '%s' "$info_o" | json 'd["rows"]'); total_o=$(printf '%s' "$info_o" | json 'd["total"]')
rows_c=$(printf '%s' "$info_c" | json 'd["rows"]'); total_c=$(printf '%s' "$info_c" | json 'd["total"]')
echo "orders: $rows_o rows, sum of amount $total_o"
echo "customers: $rows_c rows, sum of stores $total_c"

step "the gateway (XMLA on, Basic over plain HTTP, refused statements kept)"
docker run -d --name "$id-gw" --network "$net" --memory 512m -p 127.0.0.1::8000 -e DB__URL="postgresql://t:t@$id-pg:5432/t" -e JWT__SECRET="$secret" \
    -e SECURITY__FERNET_KEY="$fernet" -e APP__ROLE=api -e ENGINE__BASE_URL="http://$id-engine:8000" -e ENGINE__BUCKET=$bucket \
    -e GATEWAY_GO__AI_SERVICE=http://ai.invalid:8000 -e SCRIPTS__ENABLED=true -e SCRIPTS__NAMESPACE=tabularia-scripts \
    -e SCRIPTS__REGISTRY=registry.invalid:5000 -e DATA_API__ENABLED=true -e DATA_API__POLL_SECONDS=5 \
    -e DATA_API__XMLA_ENABLED=true -e DATA_API__XMLA_INSECURE_BASIC=true -e DATA_API__XMLA_KEEP_REFUSED=true "$go_gateway" >/dev/null
gw=http://127.0.0.1:$(docker port "$id-gw" 8000/tcp | head -1 | sed 's/.*://')
for _ in $(seq 60); do curl -fs -o /dev/null "$gw/health" && break; sleep 0.5; done
sql() { docker exec -i "$id-pg" psql -U t -d t -qtA -v ON_ERROR_STOP=1 "$@"; }
hash=$(docker run --rm -e P="$password" "$gateway_image" python -c 'import bcrypt, os; print(bcrypt.hashpw(os.environ["P"].encode(), bcrypt.gensalt()).decode())')
user=$(sql -c "INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
    VALUES ('analyst@example.test', 'Analyst', '$hash', true, false, false, false, now()) RETURNING id")
folder=$(sql -c "INSERT INTO projects (name, description, created_at) VALUES ('Pasta', '', now()) RETURNING id")
sql -c "INSERT INTO permissions (project_id, user_id, capability) VALUES ($folder, $user, 'manage')" >/dev/null
cols_o='[{"name": "order_date", "dtype": "Date"}, {"name": "country", "dtype": "String"}, {"name": "channel", "dtype": "String"}, {"name": "product", "dtype": "String"}, {"name": "category", "dtype": "String"}, {"name": "quantity", "dtype": "Int64"}, {"name": "price", "dtype": "Decimal(12, 2)"}, {"name": "amount", "dtype": "Decimal(12, 2)"}, {"name": "promo", "dtype": "Boolean"}, {"name": "discount", "dtype": "Decimal(5, 2)"}]'
cols_c='[{"name": "country", "dtype": "String"}, {"name": "segment", "dtype": "String"}, {"name": "signup_date", "dtype": "Date"}, {"name": "stores", "dtype": "Int64"}]'
ds_o=$(sql -c "INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns, sort_keys, column_descriptions, kind,
    created_at, updated_at) VALUES ('Orders', '', $folder, $user, '$bucket', 'datasets/orders-1.parquet', $rows_o, '$cols_o', '[]', '{}',
    'flow', now(), now()) RETURNING id")
ds_c=$(sql -c "INSERT INTO datasources (name, description, project_id, owner_id, bucket, key, rows, columns, sort_keys, column_descriptions, kind,
    created_at, updated_at) VALUES ('Customers', '', $folder, $user, '$bucket', 'datasets/customers-1.parquet', $rows_c, '$cols_c', '[]', '{}',
    'flow', now(), now()) RETURNING id")
login=$(curl -fsS "$gw/auth/login" -H 'Content-Type: application/json' -d "{\"email\": \"analyst@example.test\", \"password\": \"$password\"}")
session=$(printf '%s' "$login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
api() { curl -fsS -H "Authorization: Bearer $session" -H 'Content-Type: application/json' "$@"; }

step "the data API (two cubes) and its key"
made=$(api "$gw/projects/$folder/data-apis" -d "{\"name\": \"$catalog_name\", \"slug\": \"$slug\", \"xmla\": {\"enabled\": true}, \"resources\": [
    {\"name\": \"orders\", \"source\": {\"kind\": \"datasource\", \"id\": $ds_o}, \"columns\": [\"order_date\", \"country\", \"channel\", \"product\", \"category\", \"quantity\", \"price\", \"amount\", \"promo\", \"discount\"],
     \"filters\": [{\"column\": \"country\", \"operators\": [\"eq\", \"in\"]}], \"max_page\": 1000},
    {\"name\": \"customers\", \"source\": {\"kind\": \"datasource\", \"id\": $ds_c}, \"columns\": [\"country\", \"segment\", \"signup_date\", \"stores\"],
     \"filters\": [{\"column\": \"country\", \"operators\": [\"eq\", \"in\"]}], \"max_page\": 1000}]}")
apid=$(printf '%s' "$made" | json 'd["id"]')
expires=$(date -u -d '+7 days' +%Y-%m-%dT%H:%M:%SZ)
key=$(api "$gw/data-apis/$apid/keys" -d "{\"name\": \"Spreadsheet trial\", \"expires_at\": \"$expires\", \"rate_per_minute\": 100000}" | json 'd["secret"]')
api -X POST "$gw/data-apis/$apid/activate" >/dev/null
sql -c "UPDATE data_apis SET pod_token_hash = encode(sha256(convert_to('$pod_token', 'UTF8')), 'hex') WHERE id = $apid" >/dev/null

step "the pod (1 GB, 2 CPUs) on $bind:$port"
docker run -d --name "$id-pod" --network "$net" --memory 1g --cpus 2 --read-only -v /data -p "$bind:$port:8000" \
    -e DATA_API__ID="$apid" -e DATA_API__GATEWAY_URL="http://$id-gw:8000" -e DATA_API__POD_TOKEN="$pod_token" "$pod_image" >/dev/null
local_pod=http://127.0.0.1:$port
[ "$bind" = 0.0.0.0 ] || local_pod=http://$bind:$port
for _ in $(seq 600); do curl -fs -o /dev/null "$local_pod/readyz" && break; sleep 0.5; done
curl -fs -o /dev/null "$local_pod/readyz" || { docker logs "$id-pod" | tail -20; fail "the pod is not ready"; }
endpoint=$local_pod/data/$slug/xmla

step "two checks with curl, the key as HTTP Basic"
check() { if [ "$2" != "$3" ]; then fail "$1: '$2', expected '$3'"; fi; echo "ok  $1"; }
ns='xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
soap() {  # body: POST it to the endpoint
    curl -s -u "trial:$key" -H 'Content-Type: text/xml' -H 'SOAPAction: "urn:schemas-microsoft-com:xml-analysis:Discover"' --data "$1" "$endpoint"
}
discover() {  # rowset catalog
    soap "<soap:Envelope $ns><soap:Body><Discover xmlns=\"urn:schemas-microsoft-com:xml-analysis\"><RequestType>$1</RequestType><Restrictions><RestrictionList>$3</RestrictionList></Restrictions><Properties><PropertyList><Catalog>$2</Catalog><Content>SchemaData</Content><Format>Tabular</Format></PropertyList></Properties></Discover></soap:Body></soap:Envelope>"
}
cats=$(discover DBSCHEMA_CATALOGS "" "")
printf '%s' "$cats" | grep -q "<CATALOG_NAME>$catalog_name</CATALOG_NAME>" || fail "the catalog '$catalog_name' is not listed"
cubes=$(discover MDSCHEMA_CUBES "$catalog_name" "<CATALOG_NAME>$catalog_name</CATALOG_NAME>")
for c in orders customers; do
    printf '%s' "$cubes" | grep -q "<CUBE_NAME>$c</CUBE_NAME>" || fail "Discover MDSCHEMA_CUBES does not list the cube $c: $(printf '%s' "$cubes" | head -c 400)"
done
echo "ok  Discover MDSCHEMA_CUBES lists orders and customers"
mdx='SELECT {[Measures].[amount]} ON COLUMNS, DrilldownLevel({[country].[country].[All]}) ON ROWS FROM [orders]'
answer=$(curl -s -u "trial:$key" -H 'Content-Type: text/xml' -H 'SOAPAction: "urn:schemas-microsoft-com:xml-analysis:Execute"' --data \
    "<soap:Envelope $ns><soap:Body><Execute xmlns=\"urn:schemas-microsoft-com:xml-analysis\"><Command><Statement>$mdx</Statement></Command><Properties><PropertyList><Catalog>$catalog_name</Catalog><Format>Multidimensional</Format><AxisFormat>TupleFormat</AxisFormat></PropertyList></Properties></Execute></soap:Body></soap:Envelope>" "$endpoint")
for want in Italy France Germany Spain "United Kingdom" "<Value xsi:type=\"xsd:decimal\">$total_o</Value>"; do
    printf '%s' "$answer" | grep -qF "$want" || fail "Execute (amount by country): '$want' is missing from: $(printf '%s' "$answer" | head -c 600)"
done
echo "ok  Execute: amount by country with the total $total_o"
check "no key gets 401 with a Basic challenge" "$(curl -s -o /dev/null -D - -X POST "$endpoint" | tr -d '\r' | awk 'NR==1{c=$2} tolower($1)=="www-authenticate:"{h=$2} END{print c" "h}')" "401 Basic"

step "the connection details"
umask 077
shown=$bind; [ "$bind" = 0.0.0.0 ] && shown=$(hostname -I | awk '{print $1}')
cat > "$out" <<EOF
XMLA trial: pivot tables on the data API "$catalog_name" (plain HTTP, trusted network only)

Server name (endpoint): http://$shown:$port/data/$slug/xmla
Catalog:                $catalog_name
Cubes:                  orders, customers
User name:              anything (for example: trial)
Password (the key):     $key

Excel: Data > Get Data > From Database > From Analysis Services. If it refuses a plain-HTTP
address, the connection has to be tried from a client that accepts it; the key travels in
clear text, so make no other use of it. The key expires in 7 days.

Statements the pod refused (kept for a day), to send back:
  curl -s -u trial:$key http://$shown:$port/data/$slug/xmla/refused

Remove the whole stack (containers, network, images):
  docker rm -f -v $containers; docker network rm $net; docker rmi $go_gateway $pod_image
EOF
chmod 600 "$out"
passed=1
echo "written to $out"
echo "ready in $(( $(date +%s) - started )) s; the stack is UP: $containers"
docker stats --no-stream --format '{{.Name}}: {{.MemUsage}}' $containers
echo
echo "PASSED"
