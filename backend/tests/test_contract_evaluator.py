"""The data contracts' evaluator: every rule, when it holds and when it does not.

A wrong report here is the costliest defect of the whole feature: a «passes» too many
publishes broken data, a «does not pass» too many stops good data.
"""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.contracts.evaluator import evaluate, family, profile, propose

NOW = datetime(2026, 10, 6, 12, 0, 0)


def data() -> pl.LazyFrame:
    return pl.LazyFrame({
        "id": [1, 2, 3, 4, 5],
        "riga": [1, 2, 3, 1, 1],
        "ordine": [10, 10, 10, 11, 12],
        "stato": ["aperto", "spedito", "aperto", None, "perso"],
        "quantita": [1.0, 2.5, -3.0, None, 40.0],
        "sku": ["ABC-1", "ABC-22", "abc-3", "XYZ-9", None],
        "creato": [datetime(2026, 10, 1), datetime(2026, 10, 2), datetime(2026, 10, 3), datetime(2026, 10, 4), datetime(2026, 10, 5, 9)],
        "spedito": [datetime(2026, 10, 2), datetime(2026, 10, 1), None, datetime(2026, 10, 6), datetime(2026, 10, 5, 10)],
        "giorno": [date(2026, 10, 1)] * 5,
        "attivo": [True, False, True, True, False],
    })


def one(rule: dict, lf: pl.LazyFrame | None = None, **kw) -> dict:
    report = evaluate(lf if lf is not None else data(), {"rules": [{"id": "r", "severity": "error", **rule}]}, now=NOW, **kw)
    return report["rules"][0]


# ── the rules, one by one ───────────────────────────────────────────────────
@pytest.mark.parametrize("rule, passes", [
    ({"kind": "column", "column": "id"}, True),
    ({"kind": "column", "column": "non_esiste"}, False),
    ({"kind": "column", "column": "id", "dtype": "integer"}, True),
    ({"kind": "column", "column": "id", "dtype": "number"}, True),      # an integer is a number too
    ({"kind": "column", "column": "quantita", "dtype": "integer"}, False),
    ({"kind": "column", "column": "quantita", "dtype": "number"}, True),
    ({"kind": "column", "column": "stato", "dtype": "string"}, True),
    ({"kind": "column", "column": "stato", "dtype": "integer"}, False),
    ({"kind": "column", "column": "creato", "dtype": "datetime"}, True),
    ({"kind": "column", "column": "giorno", "dtype": "date"}, True),
    ({"kind": "column", "column": "giorno", "dtype": "datetime"}, False),
    ({"kind": "column", "column": "attivo", "dtype": "boolean"}, True),
    ({"kind": "not_null", "column": "id"}, True),
    ({"kind": "not_null", "column": "stato"}, False),
    ({"kind": "unique", "columns": ["id"]}, True),
    ({"kind": "unique", "column": "id"}, True),                          # short form
    ({"kind": "unique", "columns": ["ordine"]}, False),
    ({"kind": "unique", "columns": ["ordine", "riga"]}, True),           # composite key
    ({"kind": "unique", "columns": ["ordine", "attivo"]}, False),
    ({"kind": "accepted_values", "column": "stato", "values": ["aperto", "spedito", "perso"]}, True),   # null is not a wrong value
    ({"kind": "accepted_values", "column": "stato", "values": ["aperto", "spedito"]}, False),
    ({"kind": "accepted_values", "column": "riga", "values": [1, 2, 3]}, True),
    ({"kind": "accepted_values", "column": "riga", "values": ["1", "2", "3"]}, True),   # written as text, a numeric column
    ({"kind": "accepted_values", "column": "riga", "values": [1]}, False),
    ({"kind": "accepted_values", "column": "attivo", "values": [True, False]}, True),      # booleans: the defect found live
    ({"kind": "accepted_values", "column": "attivo", "values": ["true", "FALSE"]}, True),
    ({"kind": "accepted_values", "column": "attivo", "values": [True]}, False),
    ({"kind": "accepted_values", "column": "quantita", "values": [1, 2.5, -3, "40"]}, True),   # 1 matches 1.0, «40» matches 40.0
    ({"kind": "accepted_values", "column": "quantita", "values": [1, 2.5]}, False),
    ({"kind": "accepted_values", "column": "giorno", "values": ["2026-10-01"]}, True),
    ({"kind": "range", "column": "quantita", "min": -3}, True),
    ({"kind": "range", "column": "quantita", "min": 0}, False),
    ({"kind": "range", "column": "quantita", "max": 40}, True),
    ({"kind": "range", "column": "quantita", "min": 0, "max": 10}, False),
    ({"kind": "range", "column": "creato", "min": "2026-10-01"}, True),
    ({"kind": "range", "column": "creato", "min": "2026-10-02T00:00:00"}, False),
    ({"kind": "range", "column": "giorno", "max": "2026-10-01"}, True),
    ({"kind": "pattern", "column": "sku", "regex": "[A-Za-z]{3}-\\d+"}, True),
    ({"kind": "pattern", "column": "sku", "regex": "[A-Z]{3}-\\d+"}, False),
    ({"kind": "pattern", "column": "sku", "regex": "[A-Z]{3}"}, False),   # WHOLE, not «starts with»
    ({"kind": "row_count", "min": 1}, True),
    ({"kind": "row_count", "min": 6}, False),
    ({"kind": "row_count", "max": 5}, True),
    ({"kind": "row_count", "min": 1, "max": 4}, False),
    ({"kind": "expression", "sql": "id > 0"}, True),
    ({"kind": "expression", "sql": "quantita >= 0"}, False),              # -3, and a null
    ({"kind": "expression", "sql": "spedito >= creato"}, False),
    ({"kind": "expression", "sql": "ordine * 2 >= riga"}, True),
])
def test_every_rule_when_it_holds_and_when_not(rule, passes):
    assert one(rule)["passed"] is passes, one(rule)


def test_a_count_says_how_many_rows_violate_and_shows_some():
    r = one({"kind": "accepted_values", "column": "stato", "values": ["aperto", "spedito"]})
    assert (r["passed"], r["violations"], r["sample"]) == (False, 1, ["perso"])
    r = one({"kind": "unique", "columns": ["ordine"]})
    assert r["violations"] == 3 and r["sample"] == [10]
    r = one({"kind": "range", "column": "quantita", "min": 0})
    assert r["violations"] == 1 and r["sample"] == [-3.0]   # null is not out of range
    r = one({"kind": "not_null", "column": "stato"})
    assert r["violations"] == 1 and "sample" not in r        # there is nothing to show of a null


def test_in_an_expression_i_dont_know_is_not_a_yes():
    """`spedito >= creato` with `spedito` null gives null: that row violates."""
    r = one({"kind": "expression", "sql": "spedito >= creato"})
    assert r["violations"] == 2   # a date before, and a null


def test_the_samples_are_few_and_cut():
    long_one = pl.LazyFrame({"t": [f"{'x' * 200}{i}" for i in range(50)]})
    r = one({"kind": "pattern", "column": "t", "regex": "y+"}, long_one)
    assert r["violations"] == 50 and len(r["sample"]) == 5 and all(len(s) <= 80 for s in r["sample"])
    assert "sample" not in evaluate(long_one, {"rules": [{"id": "r", "severity": "error", "kind": "pattern", "column": "t", "regex": "y+"}]}, samples=False)["rules"][0]


# ── a rule that cannot be evaluated does NOT hold ─────────────────────────────
@pytest.mark.parametrize("rule, why", [
    ({"kind": "not_null", "column": "non_esiste"}, "does not exist"),
    ({"kind": "not_null"}, "names no column"),
    ({"kind": "unique", "columns": ["id", "boh"]}, "does not exist"),
    ({"kind": "range", "column": "stato", "min": 0}, "needs a number or a date"),
    ({"kind": "range", "column": "quantita"}, "neither a minimum nor a maximum"),
    ({"kind": "range", "column": "creato", "min": "ieri"}, "not a valid datetime"),
    ({"kind": "pattern", "column": "sku", "regex": "([A-Z"}, "invalid pattern"),
    ({"kind": "accepted_values", "column": "stato", "values": []}, "empty"),
    ({"kind": "accepted_values", "column": "attivo", "values": ["forse"]}, "not a boolean"),
    ({"kind": "row_count"}, "neither a minimum nor a maximum"),
    ({"kind": "expression", "sql": "questa non è sql ((("}, "invalid expression"),
    ({"kind": "expression", "sql": "colonna_che_non_ce > 1"}, ""),
    ({"kind": "freshness", "max_age_hours": 0}, "positive number"),
    ({"kind": "freshness", "max_age_hours": 24, "column": "stato"}, "needs a date"),
    ({"kind": "regola_inventata"}, "unknown rule kind"),
])
def test_a_rule_that_cannot_be_evaluated_does_not_hold_and_says_why(rule, why):
    r = one(rule)
    assert r["passed"] is False and r.get("error"), r
    assert why in r["error"], r["error"]


def test_a_broken_rule_does_not_stop_evaluating_the_others():
    report = evaluate(data(), {"rules": [
        {"id": "a", "kind": "expression", "sql": "boh > 1", "severity": "warning"},
        {"id": "b", "kind": "not_null", "column": "id", "severity": "error"},
        {"id": "c", "kind": "not_null", "column": "stato", "severity": "warning"},
    ]})
    assert [(x["id"], x["passed"]) for x in report["rules"]] == [("a", False), ("b", True), ("c", False)]
    assert report["rows"] == 5


# ── the outcome comes from the severities ─────────────────────────────────────────────
def _outcome(*rules) -> str:
    return evaluate(data(), {"rules": [{"id": f"r{i}", **r} for i, r in enumerate(rules)]})["outcome"]


def test_two_severities_three_outcomes():
    holds = {"kind": "not_null", "column": "id"}
    falls = {"kind": "not_null", "column": "stato"}
    assert _outcome({**holds, "severity": "error"}, {**holds, "severity": "warning"}) == "passed"
    assert _outcome({**holds, "severity": "error"}, {**falls, "severity": "warning"}) == "warning"
    assert _outcome({**falls, "severity": "error"}, {**holds, "severity": "warning"}) == "failed"
    assert _outcome({**falls, "severity": "error"}, {**falls, "severity": "warning"}) == "failed"
    assert _outcome() == "passed"   # an empty contract promises nothing


def test_the_report_counts_errors_and_warnings():
    r = evaluate(data(), {"rules": [
        {"id": "a", "kind": "not_null", "column": "stato", "severity": "error"},
        {"id": "b", "kind": "not_null", "column": "sku", "severity": "warning"},
        {"id": "c", "kind": "not_null", "column": "quantita", "severity": "warning"},
        {"id": "d", "kind": "not_null", "column": "id", "severity": "error"},
    ]})
    assert (r["outcome"], r["errors"], r["warnings"], r["rows"]) == ("failed", 1, 2, 5)


@pytest.mark.parametrize("severity", [None, "info", "blocker", "", 3])
def test_a_severity_neither_warning_nor_error_does_not_pass_silently(severity):
    r = evaluate(data(), {"rules": [{"id": "a", "kind": "not_null", "column": "id", "severity": severity}]})
    assert r["rules"][0]["passed"] is False and "severity" in r["rules"][0]["error"] and r["outcome"] == "failed"


# ── freshness ───────────────────────────────────────────────────────────────
def test_the_snapshots_freshness_looks_at_when_the_data_was_produced():
    assert one({"kind": "freshness", "max_age_hours": 24})["passed"] is True          # just written
    r = one({"kind": "freshness", "max_age_hours": 24}, snapshot_at=NOW - timedelta(hours=30))
    assert (r["passed"], r["observed"], r["expected"]) == (False, 30.0, 24)
    assert one({"kind": "freshness", "max_age_hours": 48}, snapshot_at=NOW - timedelta(hours=30))["passed"] is True


def test_a_columns_freshness_looks_at_the_most_recent_value():
    r = one({"kind": "freshness", "max_age_hours": 30, "column": "creato"})       # 5 October 09:00 → 27 hours before
    assert (r["passed"], r["observed"]) == (True, 27.0)
    assert one({"kind": "freshness", "max_age_hours": 24, "column": "creato"})["passed"] is False
    assert one({"kind": "freshness", "max_age_hours": 24 * 6, "column": "giorno"})["passed"] is True
    empty = data().filter(pl.col("id") < 0)
    assert one({"kind": "freshness", "max_age_hours": 24, "column": "creato"}, empty)["passed"] is False


# ── edge cases ──────────────────────────────────────────────────────────────
def test_an_empty_table():
    empty = data().filter(pl.col("id") < 0)
    assert one({"kind": "not_null", "column": "stato"}, empty)["passed"] is True
    assert one({"kind": "unique", "columns": ["id"]}, empty)["passed"] is True
    assert one({"kind": "row_count", "min": 1}, empty) == {"id": "r", "kind": "row_count", "severity": "error", "passed": False, "observed": 0, "expected": {"min": 1, "max": None}}
    r = one({"kind": "not_null", "column": "stato"})
    assert r["column"] == "stato"                       # the report says what the rule was on
    assert one({"kind": "unique", "columns": ["ordine", "riga"]})["columns"] == ["ordine", "riga"]
    assert evaluate(data(), {"version": 7, "rules": []})["version"] == 7   # and of which version of the contract
    assert one({"kind": "column", "column": "id", "dtype": "integer"}, empty)["passed"] is True


def test_the_type_families():
    cases = {pl.Int8: "integer", pl.UInt64: "integer", pl.Float32: "number", pl.Decimal(10, 2): "number", pl.String: "string",
            pl.Boolean: "boolean", pl.Date: "date", pl.Datetime("ms"): "datetime", pl.Datetime("us", "UTC"): "datetime", pl.List(pl.Int8): "other"}
    for kind, wait in cases.items():
        assert family(kind) == wait, kind


def test_two_million_rows_in_a_few_seconds(tmp_path):
    import time

    n = 2_000_000
    pl.DataFrame({"id": range(n), "stato": ["a", "b", "c", "d"] * (n // 4), "v": [float(i % 1000) for i in range(n)]}).write_parquet(tmp_path / "g.parquet")
    t = time.monotonic()
    r = evaluate(pl.scan_parquet(tmp_path / "g.parquet"), {"rules": [
        {"id": "a", "kind": "unique", "columns": ["id"], "severity": "error"},
        {"id": "b", "kind": "not_null", "column": "stato", "severity": "error"},
        {"id": "c", "kind": "accepted_values", "column": "stato", "values": ["a", "b", "c"], "severity": "warning"},
        {"id": "d", "kind": "range", "column": "v", "min": 0, "max": 999, "severity": "error"},
        {"id": "e", "kind": "expression", "sql": "v >= 0 AND id >= 0", "severity": "error"},
    ]})
    assert time.monotonic() - t < 15
    assert (r["outcome"], r["rows"], r["rules"][2]["violations"], r["rules"][2]["sample"]) == ("warning", n, n // 4, ["d"])


# ── profile and proposal ───────────────────────────────────────────────────────
def test_the_profile_says_what_is_in_the_data():
    p = profile(data())
    by_name = {c["name"]: c for c in p["columns"]}
    assert p["rows"] == 5
    assert (by_name["id"]["family"], by_name["id"]["nulls"], by_name["id"]["distinct"], by_name["id"]["min"], by_name["id"]["max"]) == ("integer", 0, 5, 1, 5)
    assert (by_name["stato"]["nulls"], by_name["stato"]["family"]) == (1, "string")
    assert by_name["creato"]["min"] == "2026-10-01T00:00:00"


def test_the_proposal_is_careful_and_the_proposed_contract_holds_on_its_data():
    lf = pl.LazyFrame({"id": list(range(40)), "canale": ["web", "negozio"] * 20, "note": [None] * 40, "importo": [1.5] * 40})
    contract = propose(profile(lf))
    # with the types one really meets: dates, booleans, decimals, text with few values and nulls
    real = pl.LazyFrame({"giorno": [date(2026, 1, 1 + i % 28) for i in range(60)], "festivo": [True, False, False] * 20,
                         "nome": [None, "Natale", "Pasqua"] * 20, "anno": pl.Series([2026] * 60, dtype=pl.Int32),
                         "prezzo": pl.Series(["1.50"] * 60).cast(pl.Decimal(5, 2))})
    proposed = propose(profile(real))
    assert evaluate(real, proposed)["outcome"] == "passed", [r for r in evaluate(real, proposed)["rules"] if not r["passed"]]
    assert not any(r["kind"] == "accepted_values" and r["column"] == "festivo" for r in proposed["rules"])
    by_type = {}
    for r in contract["rules"]:
        by_type.setdefault(r["kind"], []).append(r)
    # structural = error; true today = warning
    assert all(r["severity"] == "error" for r in by_type["column"]) and len(by_type["column"]) == 4
    assert {r["column"] for r in by_type["not_null"]} == {"id", "canale", "importo"} and all(r["severity"] == "warning" for r in by_type["not_null"])
    assert [r["columns"] for r in by_type["unique"]] == [["id"]]
    assert [(r["column"], r["values"]) for r in by_type["accepted_values"]] == [("canale", ["negozio", "web"])]
    assert by_type["row_count"][0]["min"] == 1
    assert len({r["id"] for r in contract["rules"]}) == len(contract["rules"])
    assert evaluate(lf, contract)["outcome"] == "passed"
