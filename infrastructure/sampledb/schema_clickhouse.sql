-- CRM dell'azienda di salumi (database "analytics" su ClickHouse). Sorgente di
-- test per il connettore ClickHouse di Tabularia. Contiene la rete commerciale
-- gerarchica (agente → ispettore → capo area), l'anagrafica clienti e una
-- tabella-fatto di attività CRM (il grosso del volume).
--
-- I dati NON sono qui: li genera `generate.py` (Faker + numpy).

DROP TABLE IF EXISTS analytics.attivita_crm;
DROP TABLE IF EXISTS analytics.clienti;
DROP TABLE IF EXISTS analytics.agenti;

-- Rete commerciale: gerarchia a 3 livelli via `responsabile_id` (self-ref).
--   ruolo = 'capo_area'  → responsabile_id = 0 (vertice)
--   ruolo = 'ispettore'  → responsabile_id = un capo_area
--   ruolo = 'agente'     → responsabile_id = un ispettore
CREATE TABLE analytics.agenti (
    id                 UInt32,
    nome               String,
    cognome            String,
    ruolo              LowCardinality(String),   -- agente | ispettore | capo_area
    responsabile_id    UInt32,                   -- 0 se al vertice
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
    agente_id         UInt32,                    -- agente di riferimento
    data_acquisizione Date,
    attivo            UInt8
) ENGINE = MergeTree ORDER BY id;

-- Attività CRM (visite, telefonate, solleciti…): la tabella grande del CRM.
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
