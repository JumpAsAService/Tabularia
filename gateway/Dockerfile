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
# scrivibile, per le librerie che creano file temporanei. In produzione va
# eseguito con UNA sola replica, perché lo scheduler gira dentro il processo e
# non ha un lock distribuito: vedi docs/deploy/kubernetes.md.

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

########################
#   Stage 2: runtime   #
########################
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

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
