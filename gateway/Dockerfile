# syntax=docker/dockerfile:1.9
#
# Immagine UNICA del gateway (control plane), per sviluppo e produzione:
# leggera, utente non root, pronta per un filesystem in sola lettura. Sviluppo
# e produzione usano lo stesso file e le stesse regole: cambia solo cosa viene
# installato.
#
# Opzione di build, spenta di default, cioè l'immagine di produzione:
#   WITH_DEV=true   include le dipendenze di sviluppo (pytest).
# Il docker-compose di sviluppo la accende.
#
# Il gateway non scrive file su disco: /tmp resta comunque l'unica directory
# scrivibile, per le librerie che creano file temporanei.
#
# La stessa immagine fa due mestieri, scelti con APP__ROLE: `api` risponde alle
# richieste (quante repliche si vuole), `orchestrator` fa il lavoro di fondo —
# schedulazioni, esecuzione dei flussi, pulizie — anche lui in più copie. `all`,
# il predefinito, li fa entrambi in un processo solo. Vedi docs/deploy/kubernetes.md.

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

# Solo le dipendenze, dal lockfile; il progetto non è un pacchetto installabile.
# Il virtualenv sta in /opt/venv e NON in /app, così il bind mount dei sorgenti
# in sviluppo non lo nasconde. Le suite di test dei pacchetti di terzi non
# servono a runtime e vengono rimosse.
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

# HOME=/tmp: l'utente non ha home, e qualche libreria vuole scrivere cache lì.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    TMPDIR=/tmp \
    HOME=/tmp

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Solo il codice dell'applicazione: niente test, niente file di sviluppo.
COPY --chown=10001:10001 app ./app

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
