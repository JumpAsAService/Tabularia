# Contributing to Tabularia

Thank you for taking the time. Bug reports, questions, documentation fixes and code are
all welcome.

## Reporting a bug or asking for a feature

Open an [issue](https://github.com/JumpAsAService/Tabularia/issues/new/choose) and pick
the form that fits. For a bug, the version (in the app: the gear menu, or
`GET /system/info` on the gateway), how you run it (Compose or Helm) and the steps that
reproduce it save a round of questions. Never paste passwords, tokens, connection
strings or private data: remove them from logs and screenshots first.

## Running it from the code

The development stack builds every image from this repository and reloads the API on
every change:

```bash
git clone https://github.com/JumpAsAService/Tabularia.git
cd Tabularia/infrastructure
docker compose up -d
```

Then open http://localhost:3000 and create the administrator. Settings are optional:
`cp .env.example .env` and edit the copy.

The [getting-started guide](docs/getting-started-docker-compose.md) has every step,
the sample data and what to do when something does not start. The parts of the
repository:

| folder | what |
|---|---|
| `backend/` | the engine: FastAPI and Celery workers, the five engines (Polars, DuckDB, chDB, ClickHouse, BigQuery) |
| `gateway/` | the control plane: sign-in, permissions, metadata in Postgres, scheduling, the AI assistant |
| `frontend/` | the app (Nuxt) |
| `infrastructure/` | the development Compose stack, the Helm chart, the sample databases |
| `website/` | the presentation site and the documentation site (GitHub Pages) |
| `docs/` | the documentation, also published on the site |
| `e2e/` | end-to-end checks against a running stack |

## Tests

CI runs on every push and pull request (`.github/workflows/ci.yml`); the same script
runs on a laptop:

```bash
docker build --build-arg WITH_DEV=true --build-arg WITH_CHDB=true -t tabularia-backend:ci backend
.github/scripts/ci_tests.sh engine tabularia-backend:ci
docker build --build-arg WITH_DEV=true -t tabularia-gateway:ci gateway
.github/scripts/ci_tests.sh gateway tabularia-gateway:ci
```

The script starts the real servers the suites use (ClickHouse, PostgreSQL, MinIO,
Valkey) on a network of their own and removes them at the end. The interface's type
check, from `frontend/`:

```bash
npm ci
npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

A change in behaviour comes with a test that fails without it.

## Pull requests

- One change per pull request, with the reason in its description.
- Code, comments, messages and documentation in English. Older parts of the code still
  carry Italian comments; translating the ones you touch is welcome.
- Commit messages in the style of the history: `feat(scope): …`, `fix(scope): …`,
  `docs: …`, `ci: …`, a first line that says what changes and a body that says why.
- If you change a setting, run `python3 .github/scripts/config_reference.py` so
  `docs/configuration.md` follows (CI checks it).
- A change to the metadata database comes with an Alembic migration:
  `python -m app.db.migra nuova "what it does"` in the gateway
  ([docs/database-migrations.md](docs/database-migrations.md)).

## Licence

Tabularia is licensed under the [GNU AGPL-3.0](LICENSE). By opening a pull request you
agree that your contribution is licensed under the same terms.
