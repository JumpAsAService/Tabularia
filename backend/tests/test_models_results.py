"""The check of a model's results (`app/engine/models/results.py`): every fixture of
`fixtures/models/` passes (one per model and variant at least: the report is built on them
before the runners exist), and a table with a wrong type, a missing column, a duplicated
key, a row the catalogue does not declare for the variant, a number that is not finite or a
verdict that is not one is refused with a sentence.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.engine.models import catalog, results
from app.engine.models.results import ResultsError, check_file, check_table, read_fixture, table_of_rows
from app.engine.stats.output import OUTPUT_NAMES

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "models").glob("*-*.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_every_fixture_passes(path):
    model, params, table = read_fixture(path)
    assert check_table(model, params, table) == json.loads(path.read_text(encoding="utf-8"))["variant"]


def test_there_is_a_fixture_per_model_and_variant():
    have = {p.stem for p in FIXTURES}
    for model, spec in catalog.MODELS.items():
        for v in spec["variants"]:
            assert f"{model}-{v['id']}" in have, f"{model}-{v['id']}"


def _linear():
    model, params, table = read_fixture(next(p for p in FIXTURES if p.stem == "linear-unpenalized"))
    rows = [list(r.values()) for r in table.to_pylist()]
    return model, params, rows


def _refused(rows, match, params=None):
    model, base, _ = _linear()
    with pytest.raises(ResultsError, match=match):
        check_table(model, params or base, table_of_rows(rows))


def test_a_parquet_file_round_trips():
    model, params, rows = _linear()
    buf = io.BytesIO()
    pq.write_table(table_of_rows(rows), buf)
    buf.seek(0)
    assert check_file(model, params, buf) == "unpenalized"
    with pytest.raises(ResultsError, match="not a Parquet file"):
        check_file(model, params, io.BytesIO(b"nope"))


def test_a_column_missing_or_of_another_type_is_refused():
    model, params, rows = _linear()
    table = table_of_rows(rows)
    with pytest.raises(ResultsError, match="missing x1"):
        check_table(model, params, table.drop(["x1"]))
    with pytest.raises(ResultsError, match="the column ord must be Int64, not double"):
        check_table(model, params, table.set_column(8, "ord", pa.array([float(v) for v in table.column("ord").to_pylist()])))
    with pytest.raises(ResultsError, match="in this order"):
        check_table(model, params, table.select(list(reversed(OUTPUT_NAMES))))
    # a text written as large_string is the same column
    large = table.set_column(0, "section", table.column("section").cast(pa.large_string()))
    assert check_table(model, params, large) == "unpenalized"


def test_a_key_twice_is_refused():
    _, _, rows = _linear()
    rows.append([*rows[3][:8], len(rows), rows[3][9]])
    _refused(rows, "is there twice")


def test_ord_numbers_the_rows():
    _, _, rows = _linear()
    rows[0][8] = 5
    _refused(rows, "ord must number the rows")


def test_a_row_the_variant_does_not_declare_is_refused():
    _, _, rows = _linear()
    rows.append(["coef", "price", None, "odds_ratio", 1.0, None, None, None, len(rows), "coef|price||odds_ratio"])
    _refused(rows, "writes no 'odds_ratio' in the section coef")
    _, _, rows = _linear()
    rows.append(["cv", None, None, "cv_mean", 1.0, None, -2.0, None, len(rows), "cv|||cv_mean|0"])
    _refused(rows, "has no section 'cv'")
    _, _, rows = _linear()
    rows.append(["assumption", "ljung_box_10", None, "p_value", 0.5, None, None, None, len(rows),
                 "assumption|ljung_box_10||p_value"])
    _refused(rows, "'ljung_box_10' is not one of the assumption rows of linear")
    _, _, rows = _linear()
    rows.append(["nope", None, None, "x", 1.0, None, None, None, len(rows), "nope|||x"])
    _refused(rows, "there is no section 'nope'")


def test_the_penalized_variant_has_no_p_value():
    model, params, rows = _linear()
    penalized = catalog.check_params("linear", {"outcome": "quantity", "predictors": ["price"], "penalty": "ridge"})
    rows = [r for r in rows if r[0] == "model" and r[3] in ("model", "n")]
    rows.append(["model", None, None, "variant", None, "penalized", None, None, 2, "model|||variant"])
    rows = [[*r[:8], i, r[9]] for i, r in enumerate(rows)]
    assert check_table("linear", penalized, table_of_rows(rows)) == "penalized"
    rows.append(["coef", "price", None, "p_value", 0.01, None, None, None, 3, "coef|price||p_value"])
    with pytest.raises(ResultsError, match="writes no 'p_value' in the section coef"):
        check_table("linear", penalized, table_of_rows(rows))


def test_keys_levels_and_texts_follow_the_section_s_shape():
    _, _, rows = _linear()
    rows.append(["coef", "price", None, "estimate", 1.0, None, None, None, len(rows), "coef|price|estimate"])
    _refused(rows, "the key must be 'coef\\|price\\|\\|estimate'")
    _, _, rows = _linear()
    rows.append(["model", "x", None, "r_squared", 0.5, None, None, None, len(rows), "model|x||r_squared"])
    _refused(rows, "level must be null in the section model")
    _, _, rows = _linear()
    rows.append(["point", "qq", None, "y", 0.5, None, 1.0, None, len(rows), "point|qq||y"])
    _refused(rows, "the key of a chart's point is point\\|qq\\|\\|y\\|<index>")
    _, _, rows = _linear()
    rows.append(["coef", "promo", None, "significance", 0.5, "*", None, None, len(rows), "coef|promo||significance"])
    _refused(rows, "significance is a text, its value must be null")
    _, _, rows = _linear()
    i = next(i for i, r in enumerate(rows) if r[9] == "model|||aic")
    rows[i][5] = "three"
    _refused(rows, "aic is a number, its text must be null")


def test_a_verdict_is_one_of_four_and_numbers_are_finite():
    _, _, rows = _linear()
    i = next(i for i, r in enumerate(rows) if r[3] == "verdict")
    rows[i][5] = "fine"
    _refused(rows, "a verdict is one of ok, doubtful, failed, not_computed")
    _, _, rows = _linear()
    i = next(i for i, r in enumerate(rows) if r[3] == "r_squared")
    rows[i][4] = float("nan")
    _refused(rows, "not finite: write null")


def test_the_rows_every_model_writes_are_required_and_say_the_truth():
    _, _, rows = _linear()
    rows = [r for r in rows if r[9] != "model|||n"]
    _refused([[*r[:8], i, r[9]] for i, r in enumerate(rows)], "the row 'model\\|\\|\\|n' is missing")
    _, _, rows = _linear()
    i = next(i for i, r in enumerate(rows) if r[9] == "model|||variant")
    rows[i][5] = "penalized"
    _refused(rows, "must say 'unpenalized', the variant the parameters make")


def test_too_many_rows_are_refused(monkeypatch):
    model, params, rows = _linear()
    monkeypatch.setitem(catalog.LIMITS, "max_result_rows", len(rows) - 1)
    with pytest.raises(ResultsError, match="more than the"):
        check_table(model, params, table_of_rows(rows))


def test_a_fixture_s_columns_are_the_ten():
    assert results.REQUIRED_KEYS == ("model|||model", "model|||variant", "model|||n")
    for path in FIXTURES:
        assert json.loads(path.read_text(encoding="utf-8"))["columns"] == list(OUTPUT_NAMES)


@pytest.mark.parametrize("name", ["clv-value", "clv-purchases", "bg_bb-value", "bg_bb-purchases"])
def test_the_customer_lifetime_fixtures_refuse_what_their_variant_does_not_declare(name):
    path = next(p for p in FIXTURES if p.stem == name)
    model, params, table = read_fixture(path)
    rows = [list(r.values()) for r in table.to_pylist()]
    assert check_table(model, params, table) == name.split("-")[1]
    # a stat that no variant declares
    extra = [*rows, ["model", None, None, "clv_forecast", 1.0, None, None, None, len(rows), "model|||clv_forecast"]]
    with pytest.raises(ResultsError, match="writes no 'clv_forecast' in the section model"):
        check_table(model, params, table_of_rows(extra))
    # the top customers' table: the customer's id is its level
    top = next(i for i, r in enumerate(rows) if r[0] == "customer")
    nolevel = [list(r) for r in rows]
    nolevel[top][1], nolevel[top][9] = None, "customer||" + "|" + nolevel[top][3]
    with pytest.raises(ResultsError, match="level is required in the section customer"):
        check_table(model, params, table_of_rows(nolevel))
    # the spend part belongs to the variant `value` alone
    if name.endswith("purchases"):
        spend = [*rows, ["model", None, None, "clv_total", 1.0, None, None, None, len(rows), "model|||clv_total"]]
        with pytest.raises(ResultsError, match="writes no 'clv_total'"):
            check_table(model, params, table_of_rows(spend))


@pytest.mark.parametrize("name", [f"{m}-{t}" for m in ("ab_test", "ab_plan") for t in ("binary", "continuous", "ratio")])
def test_the_experiments_fixtures_refuse_what_their_variant_does_not_declare(name):
    path = next(p for p in FIXTURES if p.stem == name)
    model, params, table = read_fixture(path)
    rows = [list(r.values()) for r in table.to_pylist()]
    assert check_table(model, params, table) == name.split("-")[1]
    # a key no variant declares, in the model section and in the reading
    extra = [*rows, ["model", None, None, "secret_number", 1.0, None, None, None, len(rows), "model|||secret_number"]]
    with pytest.raises(ResultsError, match="writes no 'secret_number' in the section model"):
        check_table(model, params, table_of_rows(extra))
    block = "decision" if model == "ab_test" else "plan"
    extra = [*rows, ["reading", block, None, "paragraph_1", None, "x", None, None, len(rows), f"reading|{block}||paragraph_1"]]
    with pytest.raises(ResultsError, match="writes no 'paragraph_1' in the section reading"):
        check_table(model, params, table_of_rows(extra))
    # a block of the reading that the variant does not declare
    extra = [*rows, ["reading", "tarot", None, "line_1", None, "x", None, None, len(rows), "reading|tarot||line_1"]]
    with pytest.raises(ResultsError, match="'tarot' is not one of the reading rows"):
        check_table(model, params, table_of_rows(extra))
    # a section's level is required where its shape says so: a variant, a comparison's two ends
    section = "variant" if model == "ab_test" else "plan"
    first = next(i for i, r in enumerate(rows) if r[0] == section)
    nolevel = [list(r) for r in rows]
    nolevel[first][1], nolevel[first][9] = None, f"{section}||" + "|" + nolevel[first][3]
    with pytest.raises(ResultsError, match=f"level is required in the section {section}"):
        check_table(model, params, table_of_rows(nolevel))
    if model == "ab_test":
        first = next(i for i, r in enumerate(rows) if r[0] == "comparison")
        one = [list(r) for r in rows]
        one[first][2], one[first][9] = None, "comparison|" + one[first][1] + "||" + one[first][3]
        with pytest.raises(ResultsError, match="level_2 is required in the section comparison"):
            check_table(model, params, table_of_rows(one))
    # a section of another variant: a plan's rows in a test's table
    alien = [*rows, ["plan", "0.02", None, "lift", 0.02, None, None, None, len(rows), "plan|0.02||lift"]]
    if model == "ab_test":
        with pytest.raises(ResultsError, match="has no section 'plan'"):
            check_table(model, params, table_of_rows(alien))


def test_the_experiments_fixtures_say_the_decision_the_spec_names_and_keep_the_sum_of_the_shares():
    seen = {}
    for name in ("ab_test-binary", "ab_test-continuous", "ab_test-ratio"):
        model, params, table = read_fixture(next(p for p in FIXTURES if p.stem == name))
        rows = {r["key"]: r for r in table.to_pylist()}
        seen[name] = rows["model|||decision"]["text"]
        assert abs(sum(r["value"] for k, r in rows.items() if k.startswith("variant|") and k.endswith("|share")) - 1) < 1e-9
        assert abs(sum(r["value"] for k, r in rows.items() if k.startswith("bayes|") and k.endswith("|prob_best")) - 1) < 1e-6
        # the first page's rows: the SRM check and the reading of the decision are there
        assert "assumption|srm||verdict" in rows and "reading|decision||line_1" in rows
    assert seen["ab_test-binary"] == "better" and seen["ab_test-ratio"] == "srm_failed"
