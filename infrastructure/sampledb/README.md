# Database di esempio — Azienda di salumi

Due database **esterni** e realistici per testare i connettori di Tabularia
(Postgres e ClickHouse) e per costruire un caso vero: il **margine a livello di
riga d'ordine** con attribuzione alla rete commerciale.

Non fanno parte dello stack dell'app: sono opt-in, dati usa-e-getta.

## Cosa contengono

**Postgres `shop` — gestionale ordini** (schemi `catalogo` e `vendite`)

| tabella | descrizione |
|---|---|
| `catalogo.prodotti` | SKU di salumi (Parma, San Daniele, Mortadella, Salame Milano, Culatello…) per formato |
| `catalogo.listino_prezzi` | storico prezzi di listino (due periodi, rincaro 2025) |
| `catalogo.costi_produzione` | costi materia prima / lavorazione / confezionamento per prodotto |
| `vendite.ordini` | testate ordine (cliente, agente, canale, stato) |
| `vendite.righe_ordine` | righe con quantità, prezzo applicato, sconto |
| `vendite.costi_commerciali` | provvigione, logistica, promo per riga |

**ClickHouse `analytics` — CRM**

| tabella | descrizione |
|---|---|
| `agenti` | rete commerciale gerarchica: `agente → ispettore → capo_area` (self-ref `responsabile_id`) |
| `clienti` | anagrafica (ragione sociale, P.IVA, canale, geografia), assegnata a un agente |
| `attivita_crm` | visite/telefonate/solleciti (la tabella grande) |

`ordini.cliente_id` e `ordini.agente_id` sono chiavi **logiche** verso il CRM su
ClickHouse: il join cross-database è il senso del dataset.

## Il margine di riga

```
ricavo   = righe_ordine.quantita * prezzo_unitario * (1 - sconto_pct/100)
costo_p  = catalogo.costi_produzione  (per prodotto, alla data)
costo_c  = vendite.costi_commerciali  (per riga)
margine  = ricavo - costo_p - costo_c
```

Unendo poi `clienti`/`agenti` dal CRM si ottiene il margine per
agente / ispettore / capo-area / regione / canale.

## Come popolarli

```bash
# 1) avvia i due database
docker compose -f infrastructure/docker-compose.sampledb.yml \
    up -d sampledb-postgres sampledb-clickhouse

# 2) genera i dati (scala small | medium | robusta; default robusta ≈ 10–20 GB)
SAMPLEDB_SCALE=small docker compose -f infrastructure/docker-compose.sampledb.yml \
    --profile generate run --rm sampledb-generator
```

Scale (indicative):

| preset | clienti | ordini | righe (~) | attività CRM | note |
|---|---:|---:|---:|---:|---|
| `small`   | 3 000 | 30 000 | ~100 k | 60 k | smoke test, secondi |
| `medium`  | 30 000 | 1 M | ~3,5 M | 2 M | qualche minuto |
| `robusta` | 150 000 | 12 M | ~42 M | 60 M | ~10–20 GB, più lungo |

Rigenerabile: lo schema viene ricreato da zero a ogni run (DROP + CREATE).

## Connessioni in Tabularia

| | host | porta | database | user / pass |
|---|---|---|---|---|
| Postgres | `sampledb-postgres` | `5432` | `shop` | `shop` / `shop` |
| ClickHouse | `sampledb-clickhouse` | `8123` | `analytics` | `analytics` / `analytics` |

(gli host valgono dall'interno di `dataprep-network`; dall'host le porte mappate
sono `5434` e `8124`.)
