# syntax=docker/dockerfile:1.9
#
# The ONE backend image, for development and production (engine API, worker,
# preview worker, beat): light, non-root user, ready for a read-only
# filesystem. Development and production use the same file and the same
# rules: only what gets installed changes.
#
# The same image for the four roles, only the command changes (see
# docs/deploy/kubernetes.md):
#   API      uvicorn app.api.main:app --host 0.0.0.0 --port 8000      (default)
#   worker   celery -A app.tasks.celery_app worker -E -Q celery --loglevel=info
#   preview  celery -A app.tasks.celery_app worker -E -Q preview --loglevel=info
#   beat     celery -A app.tasks.celery_app beat --schedule /tmp/celerybeat-schedule --loglevel=info
#
# The only writable directory: /tmp (TMPDIR). Uploads, intermediate parquet
# files, DuckDB/Polars spills and chDB's state end up here. At runtime it is
# mounted as a volume (emptyDir) sized on the heaviest dataset.
#
# Three build options, all off by default, that is the production image:
#   WITH_CHDB=true  includes chDB, the embedded ClickHouse engine, with pandas and
#                   numpy that only it needs: almost 600 MB. In production the
#                   ClickHouse dialect is covered by the external ClickHouse engine.
#   WITH_DEV=true   includes the development dependencies (pytest).
#   WITH_MSSQL=true includes the SQL Server driver (the `sqlserver` extra:
#                   mssql-python and Microsoft's ODBC driver, mssql-python-odbc).
#                   That driver is under MICROSOFT'S licence, not an open-source one:
#                   the images Tabularia publishes never contain it, and whoever
#                   builds with this option accepts Microsoft's terms
#                   (docs/sqlserver.md). Without it a SQL Server connection says
#                   that the driver is not installed.
# The development docker-compose turns on the first two (WITH_MSSQL from the
# environment, off by default, so that development runs what is published):
#   docker build --build-arg WITH_CHDB=true --build-arg WITH_DEV=true .

########################
#   Stage 1: builder   #
########################
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Only the runtime dependencies (no dev group: pytest stays out), from the
# lockfile. The project is not an installable package: the code is copied.
# The virtualenv lives in /opt/venv and NOT in /app, so that a bind mount of the
# sources on /app does not hide it.
ARG WITH_CHDB=false
ARG WITH_DEV=false
ARG WITH_MSSQL=false

# Without chDB pandas is skipped too: only chDB requires it. numpy stays: the reports
# compute their columns with it (app/reports/frames.py). The test suites shipped in third-party packages
# (pyarrow, numpy, pandas…) are not needed at runtime and are removed; Tabularia's
# tests are not in the image, in development they come from the bind mount.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    set -e; \
    opts="--no-dev"; \
    if [ "$WITH_DEV" = "true" ]; then opts=""; fi; \
    if [ "$WITH_MSSQL" = "true" ]; then opts="$opts --extra sqlserver"; fi; \
    if [ "$WITH_CHDB" != "true" ]; then \
        opts="$opts --no-install-package chdb --no-install-package chdb-core --no-install-package pandas"; \
    fi; \
    uv sync --locked --no-install-project $opts; \
    find /opt/venv -type d -name tests -prune -exec rm -rf {} +

# DuckDB reads the object storage in place through its httpfs extension: installed
# here, at build time and for this platform, so that a worker never downloads code
# (see app/engine/duckdb_storage.py)
RUN /opt/venv/bin/python -c "import duckdb; c = duckdb.connect(); c.execute(\"SET extension_directory = '/opt/duckdb-extensions'\"); c.execute('INSTALL httpfs')"

########################
#   Stage 2: runtime   #
########################
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
# the directory of the secrets created at the first start (SECURITY__KEYS_DIR): a volume
# mounted here starts with the services' owner and nobody else's permissions
 && mkdir -p /var/lib/tabularia/keys && chown 10001:10001 /var/lib/tabularia/keys && chmod 700 /var/lib/tabularia/keys

# SQL Server (mssql-python, only with WITH_MSSQL=true): the driver brings its own ODBC
# layer, but on Linux it wants the dynamic loader and Kerberos. These libraries are
# open source (LGPL, MIT) and small: they stay in every image, so that adding the
# driver to a published image is a pip install and nothing else (docs/sqlserver.md).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
 && rm -rf /var/lib/apt/lists/*

# HOME=/tmp: the user has no home, and some libraries want to write caches there.
# MALLOC_ARENA_MAX=2: with the many Polars/Arrow threads it limits memory
# fragmentation, as in the compose.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TMPDIR=/tmp \
    HOME=/tmp \
    MALLOC_ARENA_MAX=2

WORKDIR /app

# Only the virtualenv comes from the builder: no uv, no toolchain, no cache.
COPY --from=builder /opt/venv /opt/venv
# and DuckDB's httpfs extension (see the builder)
COPY --from=builder /opt/duckdb-extensions /opt/duckdb-extensions
ENV DUCKDB_EXTENSION_DIRECTORY=/opt/duckdb-extensions

# Only the application's code: no tests, no development files.
# Local secrets stay out thanks to .dockerignore.
COPY --chown=10001:10001 app ./app

USER 10001:10001

EXPOSE 8000

# No ENTRYPOINT: each role's command replaces this default.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
