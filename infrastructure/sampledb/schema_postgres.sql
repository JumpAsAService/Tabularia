-- Order management of a CURED-MEATS company (database "shop", several schemas).
-- A test source for Tabularia's Postgres connector. The fact tables
-- (orders/lines/costs) are UNLOGGED: a much faster bulk load (no WAL) —
-- throwaway data. The secondary indexes are created by `indexes_postgres.sql`
-- AFTER the load (COPY into a table without indexes = much faster).
--
-- The data is NOT in this file: `generate.py` generates it (Faker + numpy).

CREATE SCHEMA IF NOT EXISTS catalogo;
CREATE SCHEMA IF NOT EXISTS vendite;
-- the «public» reference tables (calendario, regioni) are in schema_ref.sql

-- ── Product catalogue ───────────────────────────────────────────────────────
DROP TABLE IF EXISTS vendite.costi_commerciali CASCADE;
DROP TABLE IF EXISTS vendite.righe_ordine CASCADE;
DROP TABLE IF EXISTS vendite.ordini CASCADE;
DROP TABLE IF EXISTS catalogo.costi_produzione CASCADE;
DROP TABLE IF EXISTS catalogo.listino_prezzi CASCADE;
DROP TABLE IF EXISTS catalogo.prodotti CASCADE;

CREATE TABLE catalogo.prodotti (
    id                int PRIMARY KEY,
    codice            text NOT NULL,          -- SKU
    nome              text NOT NULL,
    categoria         text NOT NULL,          -- Prosciutti, Salami, Insaccati cotti, …
    tipo              text NOT NULL,          -- crudo, cotto, stagionato, fresco
    formato           text NOT NULL,          -- intero, mezzo sottovuoto, affettato 100g, …
    stagionatura_mesi int  NOT NULL,
    peso_medio_kg     numeric(6,3) NOT NULL,
    unita_misura      text NOT NULL           -- kg | pz
);

-- List price history (several periods per product): it lets the price applied on a
-- line be compared with the list price in force at the order date.
CREATE TABLE catalogo.listino_prezzi (
    id             int PRIMARY KEY,
    prodotto_id    int NOT NULL,
    valido_dal     date NOT NULL,
    valido_al      date,                       -- NULL = still valid
    prezzo_listino numeric(10,4) NOT NULL      -- per the product's unit of measure
);

-- PRODUCTION costs per product (with history): raw material, processing and
-- packaging. One of the two terms of the line margin.
CREATE TABLE catalogo.costi_produzione (
    id                        int PRIMARY KEY,
    prodotto_id               int NOT NULL,
    valido_dal                date NOT NULL,
    costo_materia_prima_kg    numeric(10,4) NOT NULL,
    costo_lavorazione_kg      numeric(10,4) NOT NULL,
    costo_confezionamento_pz  numeric(10,4) NOT NULL
);

-- ── Sales ───────────────────────────────────────────────────────────────────
-- Order header. cliente_id and agente_id are LOGICAL keys to the CRM on
-- ClickHouse (clienti / agenti): the cross-database join is the point of the dataset.
CREATE UNLOGGED TABLE vendite.ordini (
    id          bigint PRIMARY KEY,
    numero      text   NOT NULL,
    data_ordine date   NOT NULL,
    cliente_id  int    NOT NULL,     -- → analytics.clienti.id (ClickHouse)
    agente_id   int    NOT NULL,     -- → analytics.agenti.id  (ClickHouse)
    canale      text   NOT NULL,     -- GDO, HoReCa, Grossista, Dettaglio, Gastronomia
    stato       text   NOT NULL,     -- evaso, in_lavorazione, annullato
    deposito    text   NOT NULL
);

CREATE UNLOGGED TABLE vendite.righe_ordine (
    id             bigint PRIMARY KEY,
    ordine_id      bigint NOT NULL,
    prodotto_id    int    NOT NULL,     -- → catalogo.prodotti.id
    quantita       numeric(10,3) NOT NULL,
    prezzo_unitario numeric(10,4) NOT NULL,   -- price applied (it may differ from the list price)
    sconto_pct     numeric(5,2) NOT NULL      -- line discount in %
);

-- COMMERCIAL costs per order line: the agent's commission, logistics,
-- promotions. The second term of the line margin.
CREATE UNLOGGED TABLE vendite.costi_commerciali (
    id                  bigint PRIMARY KEY,
    riga_ordine_id      bigint NOT NULL,
    provvigione_pct     numeric(5,2)  NOT NULL,
    provvigione_importo numeric(12,4) NOT NULL,
    costo_logistica     numeric(12,4) NOT NULL,
    costo_promozionale  numeric(12,4) NOT NULL
);
