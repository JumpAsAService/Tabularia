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
def chiavi(monkeypatch):
    """Chiavi finte al posto di quelle vere, per tutta la durata del test."""
    cfg = get_settings()
    ak, sk = "AK_FINTA_ACCESS_KEY", "SK_FINTA_SEGRETISSIMA_9z"
    monkeypatch.setattr(cfg.storage, "access_key", ak, raising=False)
    monkeypatch.setattr(type(cfg.storage.secret_key), "get_secret_value", lambda self: sk, raising=False)
    return ak, sk


def test_the_storage_keys_are_masked(chiavi):
    ak, sk = chiavi
    errore = (
        f"Code: 62. DB::Exception: Syntax error: failed at position 16: "
        f"('https://s3.example/bucket/x.parquet', '{ak}', '{sk}', 'Parquet')"
    )
    pulito = redact_secrets(errore)
    assert sk not in pulito and ak not in pulito
    assert MASK in pulito
    # il messaggio resta leggibile: si toglie il segreto, non il senso
    assert "Syntax error" in pulito and "x.parquet" in pulito


def test_a_message_without_secrets_is_untouched(chiavi):
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


def test_the_database_error_path_redacts(chiavi, monkeypatch):
    """`describe_db_error` è il punto unico da cui escono i messaggi: deve redigere."""
    from app.ingest.db_errors import describe_db_error

    _, sk = chiavi
    exc = Exception(f"server response: Code: 62. DB::Exception: Syntax error near '{sk}' (SYNTAX_ERROR)")
    fuori = describe_db_error(exc, "clickhouse", "host", 8443)
    assert sk not in fuori
