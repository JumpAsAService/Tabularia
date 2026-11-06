# The Go gateway (docs/specs/gateway-go.md). Built from the repository root's gateway-go/:
#   docker build -t tabularia-gateway-go:dev gateway-go
# PROGRAM=fakeengine builds the comparison's fake engine instead (scripts/diffcheck.sh).
FROM golang:1.27.3-bookworm AS build
ARG PROGRAM=gateway
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/gateway ./cmd/${PROGRAM}

# pg_dump and pg_restore for the database's backups (internal/api/jobs_backup.go), the same
# as the Python gateway's image (gateway/Dockerfile, «Stage 2: pg_dump»): every PostgreSQL
# version still supported, from PostgreSQL's own repository, only the two programs of each;
# a backup is made with the server's own version. 18 is the default, on the PATH.
FROM debian:bookworm-slim AS pgclient
ARG PG_VERSIONS="14 15 16 17 18"
RUN set -e; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSo /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    for v in $PG_VERSIONS; do apt-get install -y --no-install-recommends "postgresql-client-$v"; done; \
    for v in $PG_VERSIONS; do \
      install -d "/pg/$v/bin"; \
      cp "/usr/lib/postgresql/$v/bin/pg_dump" "/usr/lib/postgresql/$v/bin/pg_restore" "/pg/$v/bin/"; \
    done

FROM debian:bookworm-slim
# the libraries pg_dump needs (libpq of the same version from the same repository), the CA
# certificates and the time zones
COPY --from=pgclient /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc /usr/share/postgresql-common/pgdg/
COPY --from=pgclient /etc/apt/sources.list.d/pgdg.list /etc/apt/sources.list.d/
RUN set -e; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates tzdata libpq5 liblz4-1 libzstd1; \
    rm -rf /var/lib/apt/lists/*
COPY --from=pgclient /pg /usr/lib/postgresql
RUN ln -s /usr/lib/postgresql/18/bin/pg_dump /usr/lib/postgresql/18/bin/pg_restore /usr/local/bin/ \
# the same unprivileged user as the other images, and the directory of the secrets created
# at the first start (SECURITY__KEYS_DIR), as in the Python image
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /var/lib/tabularia/keys && chown 10001:10001 /var/lib/tabularia/keys && chmod 700 /var/lib/tabularia/keys
COPY --from=build /out/gateway /gateway
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/gateway"]
