# Sample databases — a pasta and bakery company

Two realistic **external** databases to try Tabularia's connectors (Postgres and
ClickHouse) and to build a real case: the **margin of every order line**,
attributed to the sales network.

They are not part of the app's stack: opt-in, throwaway data. Table and column names
are in Italian, as a real Italian ERP and CRM would have them.

## What they hold

**Postgres `shop` — the order system** (schemas `catalogo` and `vendite`)

| table | description |
|---|---|
| `catalogo.prodotti` | pasta and bakery SKUs (spaghetti, tortellini, frollini, grissini, panettone…) by format; `conservazione_mesi` holds the shelf life in months |
| `catalogo.listino_prezzi` | price-list history (two periods, a 2025 increase) |
| `catalogo.costi_produzione` | raw-material, processing and packaging costs per product |
| `vendite.ordini` | order headers (customer, agent, channel, status) |
| `vendite.righe_ordine` | order lines with quantity, applied price, discount |
| `vendite.costi_commerciali` | commission, logistics, promotion per line |

**ClickHouse `analytics` — the CRM**

| table | description |
|---|---|
| `agenti` | a hierarchical sales network: `agent → supervisor → area manager` (self-reference `responsabile_id`) |
| `clienti` | customer records (company name, VAT number, channel, geography), each assigned to an agent, with the acquisition date `data_acquisizione` and the commercial status `stato` (`attivo`, `inattivo`, `nuovo`) |
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

## The customers' lives

The customers are not a fixed list that orders at random: each one is simulated, in the
process behind the «buy till you die» models (Pareto/NBD, with BG/NBD and Gamma–Gamma
fitting it too), so that the data suits those models and survival analysis alike.

| what | how it is generated | what you can find in it |
|---|---|---|
| **acquisition** | about 30% of the customers are in the portfolio when the window opens (1 July 2023; their `data_acquisizione` lies before it, and what they ordered before is not in the data); the others arrive over the three years, at a rate that grows every year, with a season (a push in January, almost nothing in August, a quiet December) and a pace of its own for each channel (`Online` takes off, `Negozi` stays flat) | new customers per month; cohorts by acquisition date; the company's growth coming from the base, not from every customer ordering more |
| **frequency** | each customer orders at a Poisson rate of its own: a gamma across customers (shape 1.5, so a few order a lot and many order rarely) whose mean depends on the channel (`Supermercati` and `Ristorazione` often, `Ingrosso` and `Online` rarely), modulated by the month, weekday and channel seasons above. The first order follows the acquisition by a few days | orders per customer a long-tailed count; the intervals between a customer's orders are exponential, with its own mean |
| **churn** | the lifetime of a customer is exponential, with a hazard a year equal to a base (0.25) × the channel's (`Supermercati` 0.55 … `Online` 1.6) × the macro-area's (`Nord-Ovest` 0.75 … `Isole` 1.7) × the agent's (a lognormal factor: some agents lose more customers) × a gamma frailty that nothing explains. **Nobody writes it down**: the database holds no churn flag, no end date | a Kaplan–Meier by channel that separates (about 65% of the supermarkets still there after two years, about 30% of the online ones); a Cox model with channel, area and agent as covariates; a Weibull fit; the hazard of the whole base falling over time (the frail leave first) |
| **spend** | each customer has an average order size of its own (a gamma across customers, a CV of 0.5), independent of how often it orders, and the order's own noise on top (the lines, the quantities, the products); the basket still grows 5% a year | a correlation of frequency and spend close to 0 inside a channel (the Gamma–Gamma assumption); the channels differ in size (`Supermercati` and `Ingrosso` order in bulk) and in frequency (`Supermercati` often, `Ingrosso` rarely), so **over the whole base the two do move together**: the customer lifetime node's check «Spend independent of frequency» fails there, as it should, and holds inside each channel (fit one node per channel when the CLV matters) |
| **status** | `clienti.stato` is computed like a CRM would, at the end of the window, from the last order that was not cancelled: `attivo` (an order in the last six months), `inattivo` (none), `nuovo` (acquired in the last six months and nothing delivered yet). `attivo` is 0 for `inattivo`, 1 otherwise | an `inattivo` customer is **not** necessarily lost: a slow one looks the same. The difference between «inactive» and «lost» is what the buy till you die models estimate |
| **CRM activity** | none before the acquisition; while the customer buys, the usual contacts; after it has churned a small fraction, mostly reminders and calls that end badly (the attempts to win it back) | contacts that follow the customer's life; the `ordine` outcome among the active ones; the change in the mix of contacts around the churn is a signal left on purpose (a model with the CRM's activity as a predictor can find it, the order data alone cannot) |

The orders of the preset are the total of these lives: the rate of every customer is scaled
so that the orders add up to the preset's number (30,000 at `small`), and the same random
seed gives the same data at every run. At `medium`, `robust` and `massive` the same customers
simply order more often in the time they are alive.

## Seasons and anomalies (the data is NOT flat)

The facts follow realistic trends and seasons (all in `generate.py`), so the pivots
tell a story:

| signal | where | effect |
|---|---|---|
| company growth: new customers arrive faster each year (0% to +60% a year, by channel) · basket +5%/year | `ACQ_GROWTH`, `ANNUAL_BASKET_GROWTH` | volumes rising over time (about +25% orders a year at `small`), mostly because the customer base grows |
| monthly **volume** season | `MONTH_MULT` | Christmas ×1.8, Easter up, August down |
| weekly B2B season | `build_day_intensity` | full weekdays, quiet weekends |
| prices and costs +6% from 2025 | `gen_catalogue` | two price-list and cost regimes |
| **product mix × season** | `CATEGORY_SEASON`, `PRODUCT_SEASON` | panettone and pandoro peak in December, grissini and cracker in summer, taralli in the warm months, fresh pasta at Christmas and Easter, biscuits low in summer |
| **channel mix × season** | `CHANNEL_SEASON` | Ristorazione peaks in summer (tourism), Supermercati, Negozi and Online at Christmas |
| **fulfilment anomalies by depot** | `CANCEL_CHRONIC`, `CANCEL_INCIDENT` | **Bari**: a chronic cancelled share, growing (~4% → ~24%); **Torino**: a one-off incident (spring 2025, ~32%), then back to normal |
| **acquisition, frequency, churn, spend** | `INITIAL_SHARE`, `ACQ_GROWTH`, `ACQ_SEASON`, `CHANNEL_RATE`, `BASE_CHURN`, `CHANNEL_CHURN`, `AREA_CHURN`, `SPEND_SHAPE` | the customers' lives, above |

Good analyses to try: *category share by month* (product mix), *channel share by
month* (channel mix), *% fulfilled / cancelled by depot over time* (anomalies), and
the line margin by sales network. On the customers:

- **new customers per month** and **customers who ordered per month** (`data_acquisizione`
  against the orders): both grow, the first with a season;
- **retention by acquisition cohort**: the share of a half-year cohort with an order in each
  three-month window after the acquisition (customers with `data_acquisizione` from 1 July 2023
  on: the earlier ones are the portfolio of the start and their first orders are not in the data);
- **Kaplan–Meier and Cox** on the time to the last order: build a table with one row per
  customer (the first and the last order date, the number of orders), take as the event «`stato` is
  `inattivo`», as its time the days from `data_acquisizione` to the last order plus 183 (the
  six months the CRM waits), and as the censoring time the end of the window for the others; the
  strata or predictors are `canale`, the macro-area of `regione` and `agente_id`;
- **buy till you die** (BG/NBD, Pareto/NBD, Gamma–Gamma) on the same one-row-per-customer
  summary: frequency (orders after the first), recency (days from the first to the last order),
  age (days from the first order to the end of the window) and the average order value, for
  the customers acquired in the window, **keeping only those with an age above 0** (`age > 0`: a
  customer whose first order is on the last day has no time to be read, and the node refuses an
  age of 0). The «Customer Segments by Region (RFM)» flow already builds the recency, frequency and
  monetary value per customer. Over all the channels the spend check fails (the channels differ in
  both the size and the frequency of their orders, above); inside one channel it holds.

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
| `small`   | 3,000 | 30,000 | ~105 k | 60 k | a smoke test, seconds |
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
