# Sample databases — a cured-meats company

Two realistic **external** databases to try Tabularia's connectors (Postgres and
ClickHouse) and to build a real case: the **margin of every order line**,
attributed to the sales network.

They are not part of the app's stack: opt-in, throwaway data. Table and column names
are in Italian, as a real Italian ERP and CRM would have them.

## What they hold

**Postgres `shop` — the order system** (schemas `catalogo` and `vendite`)

| table | description |
|---|---|
| `catalogo.prodotti` | cured-meat SKUs (Parma, San Daniele, Mortadella, Salame Milano, Culatello…) by format |
| `catalogo.listino_prezzi` | price-list history (two periods, a 2025 increase) |
| `catalogo.costi_produzione` | raw-material, processing and packaging costs per product |
| `vendite.ordini` | order headers (customer, agent, channel, status) |
| `vendite.righe_ordine` | order lines with quantity, applied price, discount |
| `vendite.costi_commerciali` | commission, logistics, promotion per line |

**ClickHouse `analytics` — the CRM**

| table | description |
|---|---|
| `agenti` | a hierarchical sales network: `agent → supervisor → area manager` (self-reference `responsabile_id`) |
| `clienti` | customer records (company name, VAT number, channel, geography), each assigned to an agent |
| `attivita_crm` | visits, calls and reminders (the big table) |

`ordini.cliente_id` and `ordini.agente_id` are **logical** keys into the CRM on
ClickHouse: the cross-database join is the point of the dataset.

## The line margin

```
revenue    = righe_ordine.quantita * prezzo_unitario * (1 - sconto_pct/100)
cost_prod  = catalogo.costi_produzione  (per product, at the order date)
cost_comm  = vendite.costi_commerciali  (per line)
margin     = revenue - cost_prod - cost_comm
```

Joining `clienti`/`agenti` from the CRM then gives the margin by agent, supervisor,
area manager, region and channel.

## Seasons and anomalies (the data is NOT flat)

The facts follow realistic trends and seasons (all in `generate.py`), so the pivots
tell a story:

| signal | where | effect |
|---|---|---|
| company growth +18%/year · basket +5%/year | `GROWTH_ANNUO`, `BASKET_GROWTH_ANNUO` | volumes rising over time |
| monthly **volume** season | `MONTH_MULT` | Christmas ×1.8, Easter up, August down |
| weekly B2B season | `build_day_weights` | full weekdays, quiet weekends |
| prices and costs +6% from 2025 | `gen_catalogo` | two price-list and cost regimes |
| **product mix × season** | `CATEGORY_SEASON`, `PRODUCT_SEASON` | cotechino and zampone peak in December, mortadella and würstel in summer, bresaola in the warm months, the finest hams at Christmas and Easter |
| **channel mix × season** | `CHANNEL_SEASON` | HoReCa peaks in summer (tourism), large retail and shops at Christmas |
| **fulfilment anomalies by depot** | `CANCEL_CHRONIC`, `CANCEL_INCIDENT` | **Napoli**: a chronic cancelled share, growing (~4% → ~24%); **Modena**: a one-off incident (autumn 2025, ~30%), then back to normal |

Good analyses to try: *category share by month* (product mix), *channel share by
month* (channel mix), *% fulfilled / cancelled by depot over time* (anomalies), and
the line margin by sales network.

## Turning them on and off

They are on in the Compose stack of `infrastructure/`: `up -d` starts the two databases
and the `sampledb-init` container, which fills them if they are empty and then builds
the «Sample» folder in Tabularia (below). With no administrator yet, it waits for the
one created at the sign-in page. To leave them out:
`docker compose up -d --scale sampledb-init=0 --scale sampledb-postgres=0 --scale sampledb-clickhouse=0`.

```bash
# in infrastructure/.env (optional)
SAMPLES_SCALE=small        # small | medium | robust | massive
#SAMPLES_ENGINE=           # engine of the flows; empty = the first one the policy allows, DuckDB first
#SAMPLES_RUN_FLOWS=true    # runs the flows once, so the tables they produce exist

docker compose -f infrastructure/docker-compose.yml up -d
docker compose -f infrastructure/docker-compose.yml logs -f sampledb-init
```

Scales (approximate):

| preset | customers | orders | lines (~) | CRM activities | notes |
|---|---:|---:|---:|---:|---|
| `small`   | 3,000 | 30,000 | ~100 k | 60 k | a smoke test, seconds |
| `medium`  | 30,000 | 1 M | ~3.5 M | 2 M | a few minutes |
| `robust`  | 150,000 | 12 M | ~42 M | 60 M | ~10–20 GB, longer |
| `massive` | 500,000 | 12 M | ~42 M | **1 billion** | about 50 GB free, a couple of hours |

`massive` puts the billion rows in the ClickHouse CRM log, where Tabularia reads large
tables best; the order system stays at `robust`'s size. Measured on an 8-core machine:
the generator writes about 260,000 CRM activities a second (about an hour for the
billion, ~15 bytes each in ClickHouse, ~15 GB), and the «Sample» folder's import copies
them into Tabularia at about a million rows a second (~15 minutes, ~12 GB of Parquet in
the object storage); the seed waits up to eight hours for the imports and three for each
flow's first run. `robusta`, the earlier name of `robust`, is still accepted.

The data stays in the volumes `sampledb_pg` and `sampledb_ch`: at the next start the
container finds them filled and does not generate them again. To change the scale,
remove the volumes (`docker volume rm`) and start again. The generator alone, without
the Sample folder, is `python generate.py` with the same variables.

## The «Sample» folder in Tabularia

`seed_tabularia.py` signs in as the administrator and creates, looking each object up
by name first (running it again duplicates nothing):

| where | what |
|---|---|
| `Sample` | the two connections (order system, CRM), four saved views, the dashboard «Sales overview», with two parameters that follow each other and a click that filters, and two pixel-perfect reports: «Margin review» (for the directors: a period against the same months a year earlier, by category and month) and «Agent scorecard» (for a sales manager: an area and its agents, on revenue and on the contacts of the CRM) |
| `Sample / Flows` | eleven flows; three scheduled (`0 6 1 * *`, `30 5 * * *`, `0 7 * * 1`) |
| `Sample / Private tables` | the company's nine tables and the tables the flows produce |
| `Sample / Public tables` | `ref.calendario` (days, ISO weeks, Italian holidays) and `ref.regioni` |

Eight tables of eleven have column descriptions and three do not, on purpose: it
shows the difference between a datasource ready for the assistant and one that is
not. The flows use portable SQL only (`EXTRACT`, `CASE WHEN`, `||`, `round`,
`coalesce`) and run on the engine the installation's policy allows.

The reports hold no AI comment: without a model configured it would stay empty, and
[the guide](../../docs/capabilities/reports.md#ai-comments) says how to add one. A report or
a dashboard someone has changed stays as it is when the container runs again; one deleted
comes back as the seed makes it.

**No permission is granted**: the folder starts with no grants, like any other. Who
sees it is the administrator's call.

## Connections

| | host | port | database | user / password |
|---|---|---|---|---|
| Postgres | `sampledb-postgres` | `5432` | `shop` | `shop` / `shop` |
| ClickHouse | `sampledb-clickhouse` | `8123` | `analytics` | `analytics` / `analytics` |

(The hosts work from inside `dataprep-network`; from the host machine the mapped
ports are `5434` and `8124`.)
