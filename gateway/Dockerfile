# syntax=docker/dockerfile:1.9
#
# The gateway's SINGLE image (control plane), for development and production: light,
# a non-root user, ready for a read-only filesystem. Development and production use the
# same file and the same rules: only what is installed changes.
#
# A build option, off by default, that is the production image:
#   WITH_DEV=true   includes the development dependencies (pytest).
# The development docker-compose turns it on.
#
# The gateway writes no files on disk: /tmp stays the only writable directory anyway,
# for the libraries that create temporary files.
#
# The same image does two jobs, chosen with APP__ROLE: `api` answers the requests (as
# many replicas as wanted), `orchestrator` does the background work — schedules,
# running the flows, cleanups — in several copies too. `all`, the default, does both in
# one process. See docs/deploy/kubernetes.md.

########################
#   Stage 1: builder   #
########################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

ARG WITH_DEV=false

# Only the dependencies, from the lockfile; the project is not an installable package.
# The virtualenv sits in /opt/venv and NOT in /app, so the sources' bind mount in
# development does not hide it. The third-party packages' test suites are not needed
# at runtime and are removed.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    set -e; \
    opts="--no-dev"; \
    if [ "$WITH_DEV" = "true" ]; then opts=""; fi; \
    uv sync --locked --no-install-project $opts; \
    find /opt/venv -type d -name tests -prune -exec rm -rf {} +

#############################
#   Stage 2: pg_dump        #
#############################
# pg_dump and pg_restore, for the backups of this database (services/db_backup.py), of
# every PostgreSQL version still supported, from PostgreSQL's own repository: a backup is
# made with the server's own version, so that the server's own pg_restore reads it (a
# newer pg_dump writes a format an older pg_restore refuses). Only the two programs of
# each version go on (a few hundred KB each): the client packages would also bring Perl
# for their wrappers. 18 is the default, on the PATH.
FROM python:3.12-slim-bookworm AS pgclient

ARG PG_VERSIONS="14 15 16 17 18"
RUN set -e; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl; \
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

########################
#   Stage 3: runtime   #
########################
FROM python:3.12-slim-bookworm AS runtime

# the libraries pg_dump needs, libpq of the same version from the same repository
COPY --from=pgclient /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc /usr/share/postgresql-common/pgdg/
COPY --from=pgclient /etc/apt/sources.list.d/pgdg.list /etc/apt/sources.list.d/
RUN set -e; \
    apt-get update; \
    apt-get install -y --no-install-recommends libpq5 liblz4-1 libzstd1; \
    rm -rf /var/lib/apt/lists/*
COPY --from=pgclient /pg /usr/lib/postgresql
RUN ln -s /usr/lib/postgresql/18/bin/pg_dump /usr/lib/postgresql/18/bin/pg_restore /usr/local/bin/

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
# the directory of the secrets created at the first start (SECURITY__KEYS_DIR): a volume
# mounted here starts with the services' owner and nobody else's permissions
 && mkdir -p /var/lib/tabularia/keys && chown 10001:10001 /var/lib/tabularia/keys && chmod 700 /var/lib/tabularia/keys

# HOME=/tmp: the user has no home, and some libraries want to write caches there.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TMPDIR=/tmp \
    HOME=/tmp

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Only the application's code: no tests, no development files.
COPY --chown=10001:10001 app ./app

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
