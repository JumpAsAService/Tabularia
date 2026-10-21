# Microsoft SQL Server: the driver you add yourself

Tabularia reads tables, views and SQL queries from Microsoft SQL Server (tried on
SQL Server 2022), refreshes them and schedules them like the other databases. It does
so through **mssql-python**, Microsoft's driver, which streams the results as Arrow
batches.

**The images Tabularia publishes do not contain that driver.** If you use SQL Server,
you add it to your own image, as described below. Everything else about SQL Server
works the same once the driver is there.

## Why it is not in the images

`mssql-python` itself is MIT-licensed, but it depends on `mssql-python-odbc`: the
**Microsoft ODBC Driver 18 for SQL Server**, which is *proprietary* software under
Microsoft's own licence (`MICROSOFT_ODBC_DRIVER_FOR_SQL_SERVER_LICENSE.txt`, shipped
with the package). That licence allows redistributing the driver only on conditions
an AGPL-3.0 project cannot meet: the end users must accept terms that protect
Microsoft, the distributor must indemnify Microsoft, and the driver must never be
distributed in a way that puts it under a licence requiring source disclosure, which
AGPL-3.0 is.

So the driver is **Microsoft's, under Microsoft's licence**, and **whoever installs it
accepts that licence directly with Microsoft**. Tabularia does not distribute it.
Read the licence before you install it: it is in the package, under
`mssql_python_odbc/licenses/`.

The release workflow enforces this: every release image is checked for packages
whose licence an AGPL-3.0 image cannot carry, and the release stops if it finds one
(`.github/scripts/image_licence_check.py`, see
[release-process.md](release-process.md)).

## Without the driver

- The connection type **Microsoft SQL Server** stays in the list, marked *not
  installed here*, and the connection form says so with a link to this page. You can
  still create and save a SQL Server connection, so the configuration is ready the day
  the driver is added.
- Testing the connection, listing its tables or importing from it gives one message,
  in the user's language: *the SQL Server driver is not installed in this
  installation*, with this page. Nothing else changes, and no other database is
  affected.
- The engine says which optional drivers it has at `GET /db/drivers` (internal); the
  gateway relays it at `GET /connections/drivers`.

## Adding the driver

The driver goes into the **backend** image, the one the engine API, the workers and
beat all run. The gateway and the frontend images do not change. Pick one of the three
ways below.

### a. Build the backend from source, with the driver

From a checkout of the release tag:

```bash
docker build --build-arg WITH_MSSQL=true -t registry.example.com/tabularia-backend:1.2.1-mssql backend/
```

`WITH_MSSQL=true` installs the `sqlserver` extra of `backend/pyproject.toml`
(`mssql-python` and, with it, Microsoft's ODBC driver) at the versions pinned in
`backend/uv.lock`. The system libraries the driver needs on Linux (`libltdl7`,
`libkrb5-3`, `libgssapi-krb5-2`, all open source) are already in every image.

For the **development** stack (`infrastructure/docker-compose.yml`) the same switch is
an environment variable, off by default so that development runs what is published:

```bash
cd infrastructure
WITH_MSSQL=true docker compose build backend worker preview-worker beat
docker compose up -d backend worker preview-worker beat
```

### b. Extend the published image

A short Dockerfile on top of the published backend image, with the version of the
driver the release was tested with (`mssql-python` 1.15.0):

```dockerfile
# the release you run, 1.1.1 or later
FROM jumpasaservice/tabularia-backend:1.2.1
USER root
RUN pip --python /opt/venv/bin/python install --no-cache-dir "mssql-python==1.15.0"
USER 10001:10001
```

```bash
docker build -t registry.example.com/tabularia-backend:1.2.1-mssql .
```

The image keeps everything else of the published one: user 10001, read-only root
filesystem, the same commands for the four roles. When you upgrade Tabularia, change
the `FROM` line and build again.

### c. Point your deployment at your image

Push the image to your registry, then run it everywhere the backend image runs: the
engine API, both workers and beat.

- **Docker Compose**: set `image:` of those four services to your image.
- **Helm**: the chart joins `image.registry` with each component's repository. To
  take only the backend from your registry and the rest from Docker Hub, leave the
  registry empty and give full repositories:

  ```yaml
  image:
    registry: ""
    backend:
      repository: registry.example.com/tabularia-backend
      tag: "1.2.1-mssql"
    gateway:
      repository: jumpasaservice/tabularia-gateway
    frontend:
      repository: jumpasaservice/tabularia-frontend
  ```

  (or mirror all three images into your registry and keep `image.registry`).

### Check it

```bash
docker run --rm registry.example.com/tabularia-backend:1.2.1-mssql python -c "import mssql_python; print('ok')"
```

Then, in Tabularia, the SQL Server type is no longer marked *not installed here*, and
**Test connection** reaches the server.

## The connection

Host, port (1433 by default; a named instance as `host\INSTANCE` without a port),
database, user and password, and how to encrypt: verify the server's certificate (the
default), trust a self-signed one, or no encryption. A SQL source is sent as written,
CTEs and `ORDER BY` included, but it must be one query that reads: a batch carrying
`; DELETE …`, or a `SELECT … INTO` that would create a table, is refused before it
reaches the server. Integer `decimal` up to 18 digits arrives as Int64, other decimals
and `money` as Float64, `datetimeoffset` as UTC. SQL Server is a source only: an
Output node cannot write to it.
