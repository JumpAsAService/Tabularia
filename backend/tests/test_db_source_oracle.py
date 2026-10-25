"""Oracle come sorgente: la query che si manda al server e i tipi Arrow.

Le regole di Oracle, non quelle degli altri dialetti: un identificatore senza
virgolette si legge in MAIUSCOLO (come in SQL Developer), fra virgolette resta
com'è; un alias di tabella non vuole AS né può iniziare con «_». La prova contro
un Oracle vero sta in test_oracle_live.py."""
import pyarrow as pa
import pytest

from app.ingest import db_source as ds
from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, build_query


def _conn(schema=""):
    return DbConnectionSpec(db_type="oracle", host="h", database="FREEPDB1", db_schema=schema)


@pytest.mark.parametrize("ref, schema, atteso", [
    ("employees", "", 'SELECT * FROM "EMPLOYEES"'),
    ("hr.employees", "", 'SELECT * FROM "HR"."EMPLOYEES"'),
    ("Employees", "hr", 'SELECT * FROM "HR"."EMPLOYEES"'),
    ('HR."conti 2024"', "", 'SELECT * FROM "HR"."conti 2024"'),
])
def test_tabella_con_le_regole_di_oracle(ref, schema, atteso):
    assert build_query(_conn(schema), DbSourceSpec(mode="table", ref=ref)) == atteso


def test_query_avvolta_senza_as_e_ordinata_fuori():
    q = build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT * FROM v;", sort_keys=["id", '"Data"']))
    assert q == 'SELECT * FROM (SELECT * FROM v) q ORDER BY "ID", "Data"'


@pytest.mark.parametrize("ref", ['hr."a"b"', 'hr.a"b', '""'])
def test_virgolette_fuori_posto_rifiutate(ref):
    with pytest.raises(DbSourceError):
        build_query(_conn(), DbSourceSpec(mode="table", ref=ref))


def test_il_nostro_order_by_si_stacca_per_rimetterlo_fuori_dall_involucro():
    q = build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT a FROM t ORDER BY a", sort_keys=["a"]))
    m = ds._ORDER_BY_FINALE.match(q)
    assert m.group(1) == "SELECT * FROM (SELECT a FROM t ORDER BY a) q" and m.group(2) == '"A"'
    # senza chiavi l'ORDER BY dell'utente sta fra parentesi e non si tocca
    assert ds._ORDER_BY_FINALE.match(build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT a FROM t ORDER BY a"))) is None


class _Tipo:
    def __init__(self, name):
        self.name = name


@pytest.mark.parametrize("kind, precision, scala, atteso", [
    ("DB_TYPE_NUMBER", 10, 0, pa.int64()),
    ("DB_TYPE_NUMBER", 18, 0, pa.int64()),
    ("DB_TYPE_NUMBER", 19, 0, pa.float64()),     # oltre 18 cifre: Float64, come il driver
    ("DB_TYPE_NUMBER", 0, -127, pa.float64()),   # NUMBER senza precisione: Float64 (scelta del 2026-10-11)
    ("DB_TYPE_NUMBER", 10, 2, pa.float64()),
    ("DB_TYPE_DATE", None, None, pa.timestamp("us")),
    ("DB_TYPE_TIMESTAMP_TZ", None, None, pa.timestamp("us")),
    ("DB_TYPE_VARCHAR", None, None, pa.string()),
    ("DB_TYPE_CLOB", None, None, pa.large_string()),
    ("DB_TYPE_BLOB", None, None, pa.large_binary()),
    ("DB_TYPE_BOOLEAN", None, None, pa.bool_()),
])
def test_tipo_arrow_dalla_descrizione_per_un_risultato_vuoto(kind, precision, scala, atteso):
    assert ds._oracle_arrow_type(("C", _Tipo(kind), None, None, precision, scala, True)) == atteso


def test_date_e_timestamp_in_microsecondi_i_nanosecondi_restano():
    t = pa.table({
        "giorno": pa.array([0], pa.timestamp("s")), "ts3": pa.array([0], pa.timestamp("ms")),
        "ts9": pa.array([0], pa.timestamp("ns")), "n": pa.array([1], pa.int64()),
    })
    assert [f.type for f in ds._oracle_normalize(t).schema] == [pa.timestamp("us"), pa.timestamp("us"), pa.timestamp("ns"), pa.int64()]


def test_porta_ed_elenco_tabelle():
    assert ds.DEFAULT_PORTS["oracle"] == 1521 and "oracle_maintained = 'N'" in ds._TABLES_QUERY["oracle"]
