# syntax=docker/dockerfile:1.9
#
# Immagine UNICA del backend, per sviluppo e produzione (engine API, worker,
# worker delle anteprime, beat): leggera, utente non root, pronta per un
# filesystem in sola lettura. Sviluppo e produzione usano lo stesso file e le
# stesse regole: cambia solo cosa viene installato.
#
# Stessa immagine per i quattro ruoli, cambia solo il comando (vedi
# docs/deploy/kubernetes.md):
#   API      uvicorn app.api.main:app --host 0.0.0.0 --port 8000      (default)
#   worker   celery -A app.tasks.celery_app worker -E -Q celery --loglevel=info
#   preview  celery -A app.tasks.celery_app worker -E -Q preview --loglevel=info
#   beat     celery -A app.tasks.celery_app beat --schedule /tmp/celerybeat-schedule --loglevel=info
#
# Unica directory scrivibile: /tmp (TMPDIR). Qui finiscono upload, parquet
# intermedi, riversamenti di DuckDB/Polars e lo stato di chDB. A runtime va
# montata come volume (emptyDir) dimensionato sul dataset più pesante.
#
# Tre opzioni di build, tutte spente di default, cioè l'immagine di produzione:
#   WITH_CHDB=true  include chDB, l'engine ClickHouse embedded, con pandas e
#                   numpy che servono solo a lui: quasi 600 MB. In produzione il
#                   dialetto ClickHouse lo copre l'engine ClickHouse esterno.
#   WITH_DEV=true   include le dipendenze di sviluppo (pytest).
#   WITH_MSSQL=true includes the SQL Server driver (the `sqlserver` extra:
#                   mssql-python and Microsoft's ODBC driver, mssql-python-odbc).
#                   That driver is under MICROSOFT'S licence, not an open-source one:
#                   the images Tabularia publishes never contain it, and whoever
#                   builds with this option accepts Microsoft's terms
#                   (docs/sqlserver.md). Without it a SQL Server connection says
#                   that the driver is not installed.
# Il docker-compose di sviluppo accende le prime due (WITH_MSSQL from the
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

# Solo le dipendenze di runtime (niente gruppo dev: pytest resta fuori), dal
# lockfile. Il progetto non è un pacchetto installabile: il codice si copia.
# Il virtualenv sta in /opt/venv e NON in /app, così un bind mount dei sorgenti
# su /app non lo nasconde.
ARG WITH_CHDB=false
ARG WITH_DEV=false
ARG WITH_MSSQL=false

# Senza chDB si saltano anche pandas e numpy: il codice dell'applicazione non li
# usa, li richiede solo chDB. Le suite di test incluse nei pacchetti di terzi
# (pyarrow, numpy, pandas…) non servono a runtime e vengono rimosse; i test di
# Tabularia non stanno nell'immagine, in sviluppo arrivano dal bind mount.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    set -e; \
    opts="--no-dev"; \
    if [ "$WITH_DEV" = "true" ]; then opts=""; fi; \
    if [ "$WITH_MSSQL" = "true" ]; then opts="$opts --extra sqlserver"; fi; \
    if [ "$WITH_CHDB" != "true" ]; then \
        opts="$opts --no-install-package chdb --no-install-package chdb-core --no-install-package pandas --no-install-package numpy"; \
    fi; \
    uv sync --locked --no-install-project $opts; \
    find /opt/venv -type d -name tests -prune -exec rm -rf {} +

########################
#   Stage 2: runtime   #
########################
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

# SQL Server (mssql-python, only with WITH_MSSQL=true): the driver brings its own ODBC
# layer, but on Linux it wants the dynamic loader and Kerberos. These libraries are
# open source (LGPL, MIT) and small: they stay in every image, so that adding the
# driver to a published image is a pip install and nothing else (docs/sqlserver.md).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
 && rm -rf /var/lib/apt/lists/*

# HOME=/tmp: l'utente non ha home, e qualche libreria vuole scrivere cache lì.
# MALLOC_ARENA_MAX=2: con i molti thread di Polars/Arrow limita la
# frammentazione della memoria, come nel compose.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TMPDIR=/tmp \
    HOME=/tmp \
    MALLOC_ARENA_MAX=2

WORKDIR /app

# Dal builder arriva solo il virtualenv: niente uv, niente toolchain, niente cache.
COPY --from=builder /opt/venv /opt/venv

# Solo il codice dell'applicazione: niente test, niente file di sviluppo.
# I segreti locali restano fuori grazie al .dockerignore.
COPY --chown=10001:10001 app ./app

USER 10001:10001

EXPOSE 8000

# Nessun ENTRYPOINT: il comando di ogni ruolo sostituisce questo default.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
