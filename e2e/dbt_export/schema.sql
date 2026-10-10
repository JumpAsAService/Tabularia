DROP SCHEMA IF EXISTS zzdbt CASCADE;
CREATE SCHEMA zzdbt;
CREATE TABLE zzdbt.casi (
  id integer PRIMARY KEY, testo varchar, numero double precision, intero integer, importo numeric(10,2),
  data date, quando timestamp, flag boolean, categoria varchar, "Nome Strano" varchar, "select" integer, "città" varchar
);
INSERT INTO zzdbt.casi VALUES
 (1, ' 5 ',  12.7,    10,      1.50,     '2024-01-15', '2024-01-15 10:30:00', true,  'A',  'x y',  1, 'Milano'),
 (2, '12.7', -3.2,    NULL,    2.25,     '2024-02-29', '2024-02-29 23:59:59', false, 'B',  NULL,   2, 'Parma'),
 (3, 'abc',  NULL,    7,       NULL,     NULL,         NULL,                  NULL,  NULL, 'z',    3, NULL),
 (4, NULL,   0.5,     -4,      100.00,   '2023-12-31', '2023-12-31 00:00:00', true,  'A',  'a''b', 4, 'Città d''altri'),
 (5, '-8',   2.5,     2,       3.33,     '2024-06-01', '2024-06-01 12:00:00', false, 'C',  'w',    5, 'Milano'),
 (6, '+3',   -2.5,    3,       0.01,     '2024-06-02', '2024-06-02 12:00:00', true,  'B',  'v',    6, 'Torino'),
 (7, '1e3',  1e10,    1000000, 99999.99, '2024-07-04', '2024-07-04 04:04:04', NULL,  'A',  'u',    7, 'Roma'),
 (8, '  ',   3.5,     NULL,    -1.00,    '2024-08-08', NULL,                  true,  NULL, 't',    8, 'Roma');
CREATE TABLE zzdbt.destra (id integer, categoria varchar, peso double precision, numero double precision, extra varchar);
INSERT INTO zzdbt.destra VALUES
 (1, 'A', 1.5, 100, 'uno'), (2, 'B', 2.5, 200, 'due'), (9, 'D', 9.5, 900, 'nove'), (10, NULL, 0.5, NULL, 'dieci'), (3, 'A', 3.5, 300, NULL);
