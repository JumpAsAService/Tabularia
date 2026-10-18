#!/usr/bin/env bash
# The test suites of the engine and of the gateway, inside their development images (the
# published ones have no pytest), next to the real servers the suites can use. The same
# script runs in CI (.github/workflows/ci.yml) and on a laptop:
#
#   docker build --build-arg WITH_DEV=true --build-arg WITH_CHDB=true -t tabularia-backend:ci backend
#   .github/scripts/ci_tests.sh engine tabularia-backend:ci
#   docker build --build-arg WITH_DEV=true -t tabularia-gateway:ci gateway
#   .github/scripts/ci_tests.sh gateway tabularia-gateway:ci
#
# engine:  ClickHouse, PostgreSQL (dbt native SQL), MinIO (S3 read in place) and Valkey
#          (broker priorities); only BigQuery, Oracle and SQL Server stay out (credentials
#          or proprietary servers), and any other skip fails the run (ci_skips.py).
# gateway: PostgreSQL (migrations, queue locks, retention); the rest runs on SQLite.
#
# The servers live on a network of their own, with names and passwords made up for this
# run, and are removed at the end. Exit code: pytest's, or 1 for an unexpected skip.
set -euo pipefail

SUITE=${1:?usage: ci_tests.sh engine|gateway IMAGE}
IMAGE=${2:?usage: ci_tests.sh engine|gateway IMAGE}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ID=ci-$(openssl rand -hex 3)
NET=$ID-net
PW=$(openssl rand -hex 12)
OUT=$(mktemp -d)
chmod 777 "$OUT"

cleanup() {
  docker rm -f "$ID-clickhouse" "$ID-postgres" "$ID-minio" "$ID-valkey" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_for() {  # wait_for NAME COMMAND…: retries the command in the container for 90 s
  local name=$1; shift
  for _ in $(seq 90); do
    if docker exec "$ID-$name" "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  echo "::error::$name did not become ready"; docker logs --tail 30 "$ID-$name"; return 1
}

docker network create "$NET" >/dev/null

# a default collation that is not "C", or the byte-order tests of the dbt export prove nothing
docker run -d --name "$ID-postgres" --network "$NET" -e POSTGRES_USER=tab -e POSTGRES_PASSWORD="$PW" \
  -e POSTGRES_DB=tab postgres:18 >/dev/null

case "$SUITE" in
  engine)
    docker run -d --name "$ID-clickhouse" --network "$NET" -e CLICKHOUSE_USER=tab -e CLICKHOUSE_PASSWORD="$PW" \
      -e CLICKHOUSE_DB=tab clickhouse/clickhouse-server:24.8 >/dev/null
    docker run -d --name "$ID-minio" --network "$NET" -e MINIO_ROOT_USER=ciadmin -e MINIO_ROOT_PASSWORD="$PW" \
      pgsty/minio:RELEASE.2026-08-04T00-00-00Z server /data >/dev/null
    docker run -d --name "$ID-valkey" --network "$NET" valkey/valkey:8-alpine >/dev/null
    wait_for clickhouse clickhouse-client --user tab --password "$PW" -q "SELECT 1"
    wait_for valkey valkey-cli ping
    ENV=(
      -e CLICKHOUSE_TEST_HOST="$ID-clickhouse" -e CLICKHOUSE_TEST_PORT=8123 -e CLICKHOUSE_TEST_USER=tab
      -e CLICKHOUSE_TEST_PASSWORD="$PW" -e CLICKHOUSE_TEST_DB=tab
      -e POSTGRES_TEST_URI="postgresql://tab:$PW@$ID-postgres:5432/tab"
      -e TABULARIA_TEST_REQUIRE=postgres,clickhouse
      -e STORAGE__ENDPOINT="http://$ID-minio:9000" -e STORAGE__ACCESS_KEY=ciadmin -e STORAGE__SECRET_KEY="$PW"
      -e S3_TEST_BUCKET=ci-tests
      -e REDIS__HOST="$ID-valkey" -e VALKEY_TEST_URL="redis://$ID-valkey:6379/0"
    )
    SRC=$ROOT/backend
    # what the CI cannot have: live BigQuery, Oracle, SQL Server (and its driver)
    ALLOWED=("BIGQUERY_LIVE" "ORACLE_TEST_HOST" "SQLSERVER_TEST_HOST" "mssql_python")
    ;;
  gateway)
    ENV=(-e DB__HOST="$ID-postgres" -e DB__PORT=5432 -e DB__USER=tab -e DB__PASSWORD="$PW" -e DB__NAME=tab)
    SRC=$ROOT/gateway
    ALLOWED=()
    ;;
  *) echo "unknown suite: $SUITE"; exit 2 ;;
esac
wait_for postgres pg_isready -h 127.0.0.1 -U tab

status=0
docker run --rm --network "$NET" "${ENV[@]}" -v "$SRC:/app" -v "$OUT:/out" -w /app "$IMAGE" \
  python -m pytest tests -q -p no:cacheprovider --junitxml=/out/junit.xml || status=$?
if [[ -f "$OUT/junit.xml" ]]; then
  python3 "$ROOT/.github/scripts/ci_skips.py" "$OUT/junit.xml" "${ALLOWED[@]}" || { [[ $status -ne 0 ]] || status=1; }
fi
exit $status
