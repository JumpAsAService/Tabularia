-- The cured-meats company's CRM (database "analytics" on ClickHouse). A test
-- source for Tabularia's ClickHouse connector. It holds the hierarchical sales
-- network (agent → inspector → area manager), the customer registry and a
-- fact table of CRM activities (the bulk of the volume).
--
-- The data is NOT here: `generate.py` generates it (Faker + numpy).

DROP TABLE IF EXISTS analytics.attivita_crm;
DROP TABLE IF EXISTS analytics.clienti;
DROP TABLE IF EXISTS analytics.agenti;

-- Sales network: a 3-level hierarchy through `responsabile_id` (self-reference).
--   ruolo = 'capo_area'  → responsabile_id = 0 (top)
--   ruolo = 'ispettore'  → responsabile_id = a capo_area
--   ruolo = 'agente'     → responsabile_id = an ispettore
CREATE TABLE analytics.agenti (
    id                 UInt32,
    nome               String,
    cognome            String,
    ruolo              LowCardinality(String),   -- agente | ispettore | capo_area
    responsabile_id    UInt32,                   -- 0 at the top
    area               LowCardinality(String),   -- Nord-Ovest, Nord-Est, Centro, Sud, Isole
    provvigione_base_pct Decimal(5, 2),
    data_assunzione    Date,
    email              String
) ENGINE = MergeTree ORDER BY id;

CREATE TABLE analytics.clienti (
    id                UInt32,
    ragione_sociale   String,
    partita_iva       String,
    canale            LowCardinality(String),    -- GDO, HoReCa, Grossista, Dettaglio, Gastronomia
    indirizzo         String,
    cap               String,
    citta             String,
    provincia         LowCardinality(String),
    regione           LowCardinality(String),
    agente_id         UInt32,                    -- the customer's agent
    data_acquisizione Date,
    attivo            UInt8
) ENGINE = MergeTree ORDER BY id;

-- CRM activities (visits, calls, reminders…): the CRM's big table.
CREATE TABLE analytics.attivita_crm (
    id          UInt64,
    cliente_id  UInt32,
    agente_id   UInt32,
    data        DateTime,
    tipo        LowCardinality(String),          -- visita, telefonata, email, sollecito, reclamo
    esito       LowCardinality(String),          -- positivo, neutro, negativo, ordine
    durata_min  UInt16,
    note        String
) ENGINE = MergeTree PARTITION BY toYYYYMM(data) ORDER BY (agente_id, data);
