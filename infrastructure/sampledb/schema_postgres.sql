-- Gestionale ordini di un'azienda di SALUMI (database "shop", multi-schema).
-- Sorgente di test per il connettore Postgres di Tabularia. Le tabelle-fatto
-- (ordini/righe/costi) sono UNLOGGED: caricamento bulk molto più veloce (niente
-- WAL) — dati usa-e-getta. Gli indici secondari li crea `indexes_postgres.sql`
-- DOPO il load (COPY su tabella senza indici = molto più veloce).
--
-- I dati NON sono in questo file: li genera `generate.py` (Faker + numpy).

CREATE SCHEMA IF NOT EXISTS catalogo;
CREATE SCHEMA IF NOT EXISTS vendite;

-- ── Catalogo prodotti ───────────────────────────────────────────────────────
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

-- Storico prezzi di listino (più periodi per prodotto): permette di confrontare
-- il prezzo applicato in riga con il listino vigente alla data dell'ordine.
CREATE TABLE catalogo.listino_prezzi (
    id             int PRIMARY KEY,
    prodotto_id    int NOT NULL,
    valido_dal     date NOT NULL,
    valido_al      date,                       -- NULL = ancora valido
    prezzo_listino numeric(10,4) NOT NULL      -- per unità di misura del prodotto
);

-- Costi di PRODUZIONE per prodotto (con storico): materia prima, lavorazione e
-- confezionamento. Uno dei due addendi del margine di riga.
CREATE TABLE catalogo.costi_produzione (
    id                        int PRIMARY KEY,
    prodotto_id               int NOT NULL,
    valido_dal                date NOT NULL,
    costo_materia_prima_kg    numeric(10,4) NOT NULL,
    costo_lavorazione_kg      numeric(10,4) NOT NULL,
    costo_confezionamento_pz  numeric(10,4) NOT NULL
);

-- ── Vendite ─────────────────────────────────────────────────────────────────
-- Testata ordine. cliente_id e agente_id sono chiavi LOGICHE verso il CRM su
-- ClickHouse (clienti / agenti): il join cross-database è il senso del dataset.
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
    prezzo_unitario numeric(10,4) NOT NULL,   -- prezzo applicato (può scostarsi dal listino)
    sconto_pct     numeric(5,2) NOT NULL      -- sconto di riga in %
);

-- Costi COMMERCIALI per riga d'ordine: provvigione dell'agente, logistica,
-- promo. Il secondo addendo del margine di riga.
CREATE UNLOGGED TABLE vendite.costi_commerciali (
    id                  bigint PRIMARY KEY,
    riga_ordine_id      bigint NOT NULL,
    provvigione_pct     numeric(5,2)  NOT NULL,
    provvigione_importo numeric(12,4) NOT NULL,
    costo_logistica     numeric(12,4) NOT NULL,
    costo_promozionale  numeric(12,4) NOT NULL
);
