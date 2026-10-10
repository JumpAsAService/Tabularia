# Database migrations

The gateway keeps its metadata — users, folders, permissions, flows, datasources, runs,
contracts — in PostgreSQL. Its schema changes with the code, through **Alembic**
revisions in [`gateway/app/db/migrations/versions/`](../gateway/app/db/migrations/versions/).
The SQLModel models are the single source of truth: a revision is generated from them,
read by a person, and committed with the change that needs it.

## One command

```bash
python -m app.db.migra                  # bring the database to the latest revision
python -m app.db.migra stato            # the database's revision and the code's
python -m app.db.migra controlla        # is the database equal to the models? (exit 1 if not)
python -m app.db.migra nuova "message"  # generate a revision from the models
```

`migra` runs in **one transaction** (PostgreSQL's DDL is transactional: all or nothing),
under a Postgres advisory lock, so concurrent runs queue and the second finds the work
done. After migrating it compares the database with the models; a difference stops it and
rolls everything back — two installations of the same version have the same schema.

## Who runs it

| Where | How |
|---|---|
| Development compose | the gateway at startup (`DB__AUTO_MIGRATE=true`) |
| A compose deployment | a one-shot service running the command; gateways and orchestrators wait for it with `depends_on: {migrate: {condition: service_completed_successfully}}` |
| Helm | an init container `migrate` in every gateway and orchestrator pod (`migrations.enabled`) |
| Your own pipeline | the same command, before upgrading (and `migrations.enabled: false`) |

Outside development the application **never migrates**: at startup it checks the database
revision. Behind its code → it refuses to start and says to run the migration step. Ahead
of its code → it starts with a warning: that is the previous version during a rolling
update, and revisions are written so that it keeps working (below).

## Adding a revision

1. Change the models.
2. On a development database at the latest revision, generate the revision:
   `docker compose exec --user "$(id -u):$(id -g)" -e HOME=/tmp gateway python -m app.db.migra nuova "add X to Y"`
   (as your user: the container's user cannot write into the mounted sources).
3. **Read the file.** Autogenerate does not see everything — a renamed column looks like a
   drop and an add, a data change is not there at all — and it renders server defaults
   and types the way it understands them. Fix it by hand.
4. `python -m app.db.migra`, then `python -m app.db.migra controlla` → *equal to the models*.
5. Commit the model change and the revision together. The gateway test
   `test_migrazioni.py` fails if the models and the revisions do not agree.

Revisions are numbered `0002`, `0003`… in one chain. They go **forward only**: a mistake is
fixed by a new revision, and a rollback of the database is a restore from backup.

## Rules for rolling updates (expand / contract)

During an upgrade the new revision is applied while pods of the previous version still
serve requests. A revision must therefore work with **the previous version of the code**:

- **Add** columns as nullable, or with a server default (`server_default=`) — the previous
  code inserts rows without them. Add tables and indexes freely (a large index:
  `postgresql_concurrently=True` inside `op.get_context().autocommit_block()`).
- **Never rename or drop in one step.** A rename is three releases: add the new column and
  write both; move the readers and copy the data; drop the old one. A drop is two: stop
  using it; drop it in the next release.
- **Tighten constraints in two steps**: fill the column first, make it `NOT NULL` in the
  next release.
- **Data changes** go in the revision (`op.execute(...)`) and must be idempotent and quick;
  a long backfill belongs to the application, in batches.
- Keep locks short: every statement of a revision runs while the previous version works.

## Before Alembic

Until 2026-10 the gateway created its schema at startup: `create_all` plus a list of
idempotent `ALTER … IF NOT EXISTS`. That left drift between installations — a database
created from scratch and one upgraded over months had different foreign keys, types and
server defaults. A database from that time (tables, no `alembic_version`) is **adopted**
by the first `migra`: the frozen list brings it to the last pre-Alembic state
([`app/db/legacy.py`](../gateway/app/db/legacy.py)), the drift is realigned to the models,
it is stamped at the baseline `0001` and compared with the models. Nothing to do by hand.
