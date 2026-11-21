-- The web shop's experiments (database "analytics" on ClickHouse): what the company tried on its
-- visitors, who saw what, and what each did. `generate.py` fills them (its own generator, apart from
-- the CRM's); the schema here is created after schema_clickhouse.sql, and alone for a database that was
-- filled before the experiments existed. Names are Italian, like the rest of the CRM.

DROP TABLE IF EXISTS analytics.esperimenti;
DROP TABLE IF EXISTS analytics.esperimenti_utenti;
DROP TABLE IF EXISTS analytics.esperimenti_storico;
DROP TABLE IF EXISTS analytics.esperimenti_giornaliero;

-- One row per experiment: its variants and the split planned for them, in the same order.
CREATE TABLE analytics.esperimenti (
    id               String,
    nome             String,
    descrizione      String,
    inizio           Date,
    fine             Date,                        -- the last day visitors could enter
    varianti         String,                      -- 'A,B': the first is the control
    quote_previste   String                       -- '0.5,0.5': the planned share of each
) ENGINE = MergeTree ORDER BY id;

-- One row per visitor and experiment (a few visitors of the checkout have a row in each variant).
CREATE TABLE analytics.esperimenti_utenti (
    esperimento_id   LowCardinality(String),
    utente_id        Int64,                       -- BIGINT: some ids are above 2^31
    variante         LowCardinality(String),
    data_ingresso    Date,
    sessioni         UInt32,
    impression       UInt32,                      -- banners shown
    click            UInt32,
    convertito       Bool,
    ricavo           Decimal(12, 2),              -- 0 without a purchase
    ricavo_pre_28g   Nullable(Decimal(12, 2)),    -- the 28 days before the experiment; null for a new visitor
    sessioni_pre_28g Nullable(UInt32)
) ENGINE = MergeTree ORDER BY (esperimento_id, data_ingresso, utente_id);

-- The visitors of the eight weeks before the experiments, for planning.
CREATE TABLE analytics.esperimenti_storico (
    utente_id          Int64,
    data_prima_visita  Date,
    sessioni           UInt32,
    impression         UInt32,
    click              UInt32,
    convertito         Bool,
    ricavo             Decimal(12, 2),
    ricavo_pre_28g     Nullable(Decimal(12, 2))
) ENGINE = MergeTree ORDER BY (data_prima_visita, utente_id);

-- The aggregated form: per experiment, variant and day.
CREATE TABLE analytics.esperimenti_giornaliero (
    esperimento_id   LowCardinality(String),
    variante         LowCardinality(String),
    giorno           Date,
    utenti           UInt32,
    conversioni      UInt32,
    ricavo_totale    Decimal(18, 2),
    ricavo_quadrati  Decimal(18, 2)
) ENGINE = MergeTree ORDER BY (esperimento_id, variante, giorno);
