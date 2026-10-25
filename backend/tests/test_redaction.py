"""I segreti non escono nei messaggi d'errore (audit 2026-09-19, A2).

Senza `named collection` l'engine mette le chiavi dello storage dentro il SQL.
ClickHouse le maschera analizzando la query, ma su un errore di SINTASSI non
c'è nulla da analizzare e il frammento esce in chiaro: da lì arriva al toast
dell'editor, a `Run.error_detail` nel database e al fornitore del modello.
"""
import pytest

from app.core.config import get_settings
from app.core.redaction import MASK, redact_secrets


@pytest.fixture
def key_names(monkeypatch):
    """Chiavi finte al posto di quelle vere, per tutta la durata del test."""
    cfg = get_settings()
    ak, sk = "AK_FINTA_ACCESS_KEY", "SK_FINTA_SEGRETISSIMA_9z"
    monkeypatch.setattr(cfg.storage, "access_key", ak, raising=False)
    monkeypatch.setattr(type(cfg.storage.secret_key), "get_secret_value", lambda self: sk, raising=False)
    return ak, sk


def test_the_storage_keys_are_masked(key_names):
    ak, sk = key_names
    error = (
        f"Code: 62. DB::Exception: Syntax error: failed at position 16: "
        f"('https://s3.example/bucket/x.parquet', '{ak}', '{sk}', 'Parquet')"
    )
    clean = redact_secrets(error)
    assert sk not in clean and ak not in clean
    assert MASK in clean
    # il messaggio resta leggibile: si toglie il segreto, non il senso
    assert "Syntax error" in clean and "x.parquet" in clean


def test_a_message_without_secrets_is_untouched(key_names):
    msg = "ClickHouse: Unknown expression identifier `paese` (UNKNOWN_IDENTIFIER, code 47)."
    assert redact_secrets(msg) == msg


@pytest.mark.parametrize("vuoto", [None, ""])
def test_empty_input_survives(vuoto):
    assert redact_secrets(vuoto) == vuoto


def test_redaction_never_raises(monkeypatch):
    # se la configurazione è irraggiungibile il messaggio passa com'è:
    # una redazione rotta non deve trasformare un errore in un altro errore
    import app.core.redaction as r

    monkeypatch.setattr(r, "_secrets", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert r.redact_secrets("un messaggio") == "un messaggio"


def test_the_database_error_path_redacts(key_names, monkeypatch):
    """`describe_db_error` è il punto unico da cui escono i messaggi: deve redigere."""
    from app.ingest.db_errors import describe_db_error

    _, sk = key_names
    exc = Exception(f"server response: Code: 62. DB::Exception: Syntax error near '{sk}' (SYNTAX_ERROR)")
    outside = describe_db_error(exc, "clickhouse", "host", 8443)
    assert sk not in outside


# ── testo delle query nel pannello Prestazioni ───────────────────────────────

def test_the_arguments_of_a_storage_function_are_emptied():
    """Il query_log di ClickHouse maschera la chiave segreta ma NON quella di
    accesso, e lascia in chiaro endpoint, bucket e percorso — verificato su un
    ClickHouse 24.8. In un elenco di query lente non servono a nulla."""
    from app.core.redaction import redact_storage_args

    q = ("SELECT count() FROM s3('https://s3.it-mil.scw.cloud/bucket/datasets/1/x.parquet', "
         "'AK_VISIBILE', '[HIDDEN]', 'Parquet') WHERE paese = 'IT'")
    outside = redact_storage_args(q)
    assert "AK_VISIBILE" not in outside and "bucket/datasets" not in outside
    assert "s3(…)" in outside
    # il senso della query resta leggibile: è il motivo per cui la si mostra
    assert "SELECT count()" in outside and "paese = 'IT'" in outside


def test_nested_parentheses_do_not_defeat_it():
    from app.core.redaction import redact_storage_args

    q = "SELECT * FROM s3('https://x/y', 'AK', 'SK', 'Parquet') JOIN url('http://z/w', 'CSV') USING (id)"
    outside = redact_storage_args(q)
    assert "AK" not in outside and "http://z/w" not in outside
    assert outside.count("(…)") == 2


def test_a_query_without_storage_functions_is_untouched():
    from app.core.redaction import redact_storage_args

    q = "SELECT paese, count() FROM self GROUP BY paese"
    assert redact_storage_args(q) == q


# ── il testo TRONCATO: è lì che stava il buco ────────────────────────────────

def test_a_truncated_storage_call_is_redacted_to_the_end():
    """Il `query_log` si legge a fette: se il taglio cade dentro `s3(`, la
    parentesi non si chiude mai e la chiave d'accesso resta in chiaro. Trovato
    dal vivo il 2026-09-28 nel pannello Prestazioni."""
    from app.core.redaction import redact_storage_args

    tronca = ("INSERT INTO FUNCTION s3('https://s3.it-mil.scw.cloud/bucket/x.parquet', "
              "'SCWAWW2XAESEMPIO")
    outside = redact_storage_args(tronca)
    assert "SCWAWW2XAESEMPIO" not in outside
    assert "s3.it-mil.scw.cloud" not in outside and "bucket" not in outside
    assert outside.startswith("INSERT INTO FUNCTION s3(")


def test_a_storage_call_nested_two_levels_deep_is_redacted():
    from app.core.redaction import redact_storage_args

    q = ("SELECT * FROM (SELECT * FROM s3('https://x/y.parquet','K','S','Parquet') "
         "WHERE toYYYYMM(d) = 202601) t")
    outside = redact_storage_args(q)
    assert "y.parquet" not in outside and "'K'" not in outside
    assert "toYYYYMM(d) = 202601" in outside  # il resto della query resta leggibile


def test_the_name_inside_a_string_is_not_a_call():
    """`WHERE nome LIKE '%s3(%'` non è una chiamata: redigerla rovinerebbe una
    query legittima senza proteggere niente."""
    from app.core.redaction import redact_storage_args

    q = "SELECT * FROM t WHERE nome LIKE '%s3(%' AND a = 1"
    assert redact_storage_args(q) == q


def test_several_calls_in_one_query_all_go():
    from app.core.redaction import redact_storage_args

    q = "SELECT * FROM s3('https://a/1.parquet','K','S') UNION ALL SELECT * FROM s3('https://b/2.parquet','K','S')"
    outside = redact_storage_args(q)
    assert "1.parquet" not in outside and "2.parquet" not in outside
    assert outside.count("s3(…)") == 2
