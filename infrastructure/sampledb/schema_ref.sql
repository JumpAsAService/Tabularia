-- `ref`: reference data anyone could publish (a calendar, the regions). They
-- are the «public» tables of the Sample folder: the opposite of the company data
-- of `catalogo` and `vendite`, which is «private». A file of its own because they
-- can be added to an already generated database without redoing it.
CREATE SCHEMA IF NOT EXISTS ref;
DROP TABLE IF EXISTS ref.calendario CASCADE;
DROP TABLE IF EXISTS ref.regioni CASCADE;

-- ── public reference data ─────────────────────────────────────────────────────
CREATE TABLE ref.calendario (
    giorno            date PRIMARY KEY,
    anno              integer NOT NULL,
    trimestre         integer NOT NULL,
    mese              integer NOT NULL,
    anno_mese         integer NOT NULL,   -- YYYYMM, the key to aggregate on
    settimana_iso     integer NOT NULL,
    giorno_settimana  integer NOT NULL,   -- 1 = Monday … 7 = Sunday
    nome_giorno       varchar(12) NOT NULL,
    fine_settimana    boolean NOT NULL,
    festivo           boolean NOT NULL,
    nome_festa        varchar(40)
);

CREATE TABLE ref.regioni (
    regione           varchar(30) PRIMARY KEY,
    macroarea         varchar(12) NOT NULL,   -- Nord-Ovest, Nord-Est, Centro, Sud, Isole
    capoluogo         varchar(30) NOT NULL,
    popolazione       integer NOT NULL,       -- inhabitants, rounded (ISTAT, order of magnitude)
    superficie_kmq    integer NOT NULL
);
