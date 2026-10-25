"""The secrets do not get out in the error messages (audit 2026-09-19, A2).

Without a `named collection` the engine puts the storage's keys inside the SQL.
ClickHouse masks them by parsing the query, but on a SYNTAX error there is nothing to
parse and the fragment comes out in clear: from there it reaches the editor's toast,
`Run.error_detail` in the database and the model's provider.
"""
import pytest

from app.core.config import get_settings
from app.core.redaction import MASK, redact_secrets


@pytest.fixture
def key_names(monkeypatch):
    """Fake keys instead of the real ones, for the whole test."""
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
    # the message stays readable: the secret goes, not the meaning
    assert "Syntax error" in clean and "x.parquet" in clean


def test_a_message_without_secrets_is_untouched(key_names):
    msg = "ClickHouse: Unknown expression identifier `paese` (UNKNOWN_IDENTIFIER, code 47)."
    assert redact_secrets(msg) == msg


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_input_survives(empty):
    assert redact_secrets(empty) == empty


def test_redaction_never_raises(monkeypatch):
    # when the configuration cannot be reached the message passes as it is: a broken
    # redaction must not turn an error into another error
    import app.core.redaction as r

    monkeypatch.setattr(r, "_secrets", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert r.redact_secrets("un messaggio") == "un messaggio"


def test_the_database_error_path_redacts(key_names, monkeypatch):
    """`describe_db_error` is the one point the messages go out from: it must redact."""
    from app.ingest.db_errors import describe_db_error

    _, sk = key_names
    exc = Exception(f"server response: Code: 62. DB::Exception: Syntax error near '{sk}' (SYNTAX_ERROR)")
    outside = describe_db_error(exc, "clickhouse", "host", 8443)
    assert sk not in outside


# ── the queries' text in the Performance panel ───────────────────────────────

def test_the_arguments_of_a_storage_function_are_emptied():
    """ClickHouse's query_log masks the secret key but NOT the access key, and leaves the
    endpoint, bucket and path in clear — checked on a ClickHouse 24.8. In a list of slow
    queries they serve nothing."""
    from app.core.redaction import redact_storage_args

    q = ("SELECT count() FROM s3('https://s3.it-mil.scw.cloud/bucket/datasets/1/x.parquet', "
         "'AK_VISIBILE', '[HIDDEN]', 'Parquet') WHERE paese = 'IT'")
    outside = redact_storage_args(q)
    assert "AK_VISIBILE" not in outside and "bucket/datasets" not in outside
    assert "s3(…)" in outside
    # the query's meaning stays readable: the reason it is shown
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


# ── the TRUNCATED text: that is where the hole was ────────────────────────────────

def test_a_truncated_storage_call_is_redacted_to_the_end():
    """The `query_log` is read in slices: when the cut falls inside `s3(`, the parenthesis
    never closes and the access key stays in clear. Found live on 2026-09-28 in the
    Performance panel."""
    from app.core.redaction import redact_storage_args

    cut = ("INSERT INTO FUNCTION s3('https://s3.it-mil.scw.cloud/bucket/x.parquet', "
              "'SCWAWW2XAESEMPIO")
    outside = redact_storage_args(cut)
    assert "SCWAWW2XAESEMPIO" not in outside
    assert "s3.it-mil.scw.cloud" not in outside and "bucket" not in outside
    assert outside.startswith("INSERT INTO FUNCTION s3(")


def test_a_storage_call_nested_two_levels_deep_is_redacted():
    from app.core.redaction import redact_storage_args

    q = ("SELECT * FROM (SELECT * FROM s3('https://x/y.parquet','K','S','Parquet') "
         "WHERE toYYYYMM(d) = 202601) t")
    outside = redact_storage_args(q)
    assert "y.parquet" not in outside and "'K'" not in outside
    assert "toYYYYMM(d) = 202601" in outside  # the rest of the query stays readable


def test_the_name_inside_a_string_is_not_a_call():
    """`WHERE nome LIKE '%s3(%'` is not a call: redacting it would spoil a legitimate query
    protecting nothing."""
    from app.core.redaction import redact_storage_args

    q = "SELECT * FROM t WHERE nome LIKE '%s3(%' AND a = 1"
    assert redact_storage_args(q) == q


def test_several_calls_in_one_query_all_go():
    from app.core.redaction import redact_storage_args

    q = "SELECT * FROM s3('https://a/1.parquet','K','S') UNION ALL SELECT * FROM s3('https://b/2.parquet','K','S')"
    outside = redact_storage_args(q)
    assert "1.parquet" not in outside and "2.parquet" not in outside
    assert outside.count("s3(…)") == 2
