-- `ref`: dati di riferimento che chiunque potrebbe pubblicare (un calendario,
-- le regioni). Sono le tabelle «pubbliche» della cartella Sample: il contrario
-- dei dati aziendali di `catalogo` e `vendite`, che sono «privati». File a sé
-- perché si possono aggiungere a un database già generato senza rifarlo.
CREATE SCHEMA IF NOT EXISTS ref;
DROP TABLE IF EXISTS ref.calendario CASCADE;
DROP TABLE IF EXISTS ref.regioni CASCADE;

-- ── dati di riferimento pubblici ──────────────────────────────────────────────
CREATE TABLE ref.calendario (
    giorno            date PRIMARY KEY,
    anno              integer NOT NULL,
    trimestre         integer NOT NULL,
    mese              integer NOT NULL,
    anno_mese         integer NOT NULL,   -- AAAAMM, la chiave con cui si aggrega
    settimana_iso     integer NOT NULL,
    giorno_settimana  integer NOT NULL,   -- 1 = lunedì … 7 = domenica
    nome_giorno       varchar(12) NOT NULL,
    fine_settimana    boolean NOT NULL,
    festivo           boolean NOT NULL,
    nome_festa        varchar(40)
);

CREATE TABLE ref.regioni (
    regione           varchar(30) PRIMARY KEY,
    macroarea         varchar(12) NOT NULL,   -- Nord-Ovest, Nord-Est, Centro, Sud, Isole
    capoluogo         varchar(30) NOT NULL,
    popolazione       integer NOT NULL,       -- abitanti, arrotondati (ISTAT, ordine di grandezza)
    superficie_kmq    integer NOT NULL
);
