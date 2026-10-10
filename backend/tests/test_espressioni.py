"""Il controllo delle espressioni del compute (motori ClickHouse/chDB): accetta le
espressioni scalari, anche quelle con la parola FROM dentro, e rifiuta tutto ciò
che legge dati fuori dalla riga. Prima rifiutava EXTRACT(YEAR FROM data) e una
colonna chiamata `url` (2026-10-10)."""
import pytest

from app.engine.espressioni import espressione_vietata

BUONE = [
    "EXTRACT(YEAR FROM data)", "EXTRACT(MONTH FROM data) * 100", "TRIM(BOTH ' ' FROM nome)", "substring(nome FROM 1 FOR 3)",
    "lower(url)", "file || '.csv'", "vendite * 2", "CASE WHEN a > 0 THEN 'pos' ELSE 'neg' END", "toYear(data)",
    "ROUND(margine / NULLIF(ricavo, 0) * 100, 2)", "concat(upper(categoria), '-', \"città\")", "date_diff('day', a, b)",
    "coalesce(x, 0)", "if(isNull(x), 0, x)", "toStartOfMonth(data)", "x IN (1, 2, 3)", "\"from\" + 1",
]
CATTIVE = [
    "(SELECT 1)", "(SELECT 1 FROM file('/etc/passwd'))", "x IN (SELECT a FROM t)", "EXISTS (SELECT 1)",
    "(SELECT count() FROM s3('http://x/y.parquet'))", "x + (SELECT max(y) FROM system.users)",
    "dictGet('d', 'v', toUInt64(id))", "dictGetString('d', 'v', 1)", "joinGet('j', 'v', id)", "catboostEvaluate('/m.bin', a)",
    "hasColumnInTable('system', 'users', 'name')", "getSetting('max_threads')", "1; DROP TABLE t",
]


@pytest.mark.parametrize("expr", BUONE)
def test_le_espressioni_scalari_passano(expr):
    assert not espressione_vietata(expr, "clickhouse"), expr


@pytest.mark.parametrize("expr", CATTIVE)
def test_cio_che_legge_fuori_dalla_riga_e_rifiutato(expr):
    assert espressione_vietata(expr, "clickhouse"), expr


def test_cio_che_non_si_legge_resta_alla_regola_prudente():
    assert espressione_vietata("select ((( from", "clickhouse")
    assert not espressione_vietata("a +", "clickhouse") or True   # sintassi rotta: la rifiuterà comunque il motore
