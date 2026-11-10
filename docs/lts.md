# Long-term support

**1.4 «Romulus» is the long-term support (LTS) line**, from 1.4.2.

| | |
|---|---|
| LTS line | 1.4 |
| First LTS release | 1.4.2 |
| Branch | `release/1.4`, cut from the `v1.4.2` tag |
| Image tag | `lts` — follows the newest 1.4.x on every image (`tabularia-backend`, `tabularia-gateway`, `tabularia-gateway-go`, `tabularia-frontend`, `tabularia-data-api`) |
| Helm chart | the chart version shipped with the 1.4.x tag |

## What the LTS line gets

- **Security fixes** and **data-correctness fixes** (a wrong result, a lost or duplicated
  row, a schema read wrong), released as patch versions after 1.4.2: 1.4.3, 1.4.4, …
- **Fixes that keep it running**: a crash, a leak, an upgrade or a migration that fails.
- **No new features** and no changes of behaviour, configuration or API. A patch release
  never needs a configuration change, and its database migrations, if any, only add.

New features land on `main` and ship in the next minor release (1.5, 1.6, …). A fix is
made on `main` first, then brought to `release/1.4` when it qualifies.

## Rolling back a patch

Since a patch's migrations only add, the gateway of an earlier patch runs on the database
a later one migrated: from 1.4.2 it accepts a database ahead of its own revision, with a
warning in its log (`database at 0043, this gateway reads 0042: running, the migrations of
a patch only add`), and still refuses one behind it. So 1.4.3 → 1.4.2 is a `helm rollback`
(or the earlier image tag), with nothing to do on the database. 1.4.2 → 1.4.1 is not:
1.4.1's gateway refuses a database newer than its own, so take a backup before upgrading
to 1.4.2 and restore it to go back. [Database migrations](database-migrations.md) has the
details.

## Which tag to run

| You want | Run |
|---|---|
| Stability: fixes only, no surprises | `lts`, or pin an exact `1.4.x` |
| The newest features | `latest`, or pin an exact version |

Pinning an exact version (`1.4.4`) is the safest choice in production; move to the next
patch after reading its [changelog](../CHANGELOG.md).

## How long

The 1.4 line is supported until the next LTS line is announced, and for six months after
that announcement, so there is always time to plan the upgrade.

## Cutting an LTS patch

As in [the release process](release-process.md), from the `release/1.4` branch: bump the
patch version, tag `v1.4.N` on that branch and push the tag. The images get `1.4.N` and
`1.4`; `latest` moves only when 1.4.N is the highest stable version. Then point `lts` at
the new images (`docker buildx imagetools create -t …:lts …:1.4.N`, for each image).
