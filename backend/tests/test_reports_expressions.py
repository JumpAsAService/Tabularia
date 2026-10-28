"""The expression language of reports (app/reports/expressions.py): a grammar of its own,
checked with the position of the mistake, evaluated by walking the tree — never eval."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core import placeholders as ph
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import formats

ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 28, 10, 30, tzinfo=ROME)
FIELDS = {"amount": "Float64", "qty": "Int64", "region": "String", "day": "Date", "at": "Datetime(time_unit='us')",
          "ok": "Boolean", "weird]name": "String"}
ENV = ex.Env(fields=FIELDS, dataset="orders", datasets={"orders": FIELDS, "targets": {"goal": "Float64"}},
             params={"region": ex.TEXT, "regions": ex.LIST, "since": ex.DATE})
ROWS = [{"region": "N", "amount": 10.0, "qty": 1}, {"region": "N", "amount": None, "qty": 2},
        {"region": "S", "amount": 30.0, "qty": 3}, {"region": "S", "amount": -5.0, "qty": None}]


def ev(text, row=None, rows=None, params=None, loc="en", datasets=None, **ctx):
    node = rd.parse(text)
    context = ex.Context(loc=formats.locale(loc), now=NOW, row=row, scopes=[rows] if rows is not None else [],
                         params=params or {}, datasets=datasets or {}, ph_context=ph.Context(now=NOW, tz=ROME), **ctx)
    return ex.Evaluator()(node, context)


def error(text, env=ENV):
    with pytest.raises(ex.ExpressionError) as e:
        ex.check(rd.parse(text), env)
    return e.value


# ── grammar ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, value", [
    ("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("10 - 4 - 3", 3), ("2 * 3 / 4", 1.5), ("-2 * 3", -6), ("- -2", 2),
    ("1 + 2 & 3", "33"), ('"a" & 1 + 1', "a2"), ("1 < 2 = TRUE", None), ("NOT 1 > 2 AND 2 > 1", True),
    ("TRUE OR FALSE AND FALSE", True), ("(TRUE OR FALSE) AND FALSE", False), ("NOT NOT TRUE", True),
    ('"say ""hi"""', 'say "hi"'), (".5 + 1.5e1", 15.5), ("not true", False), ("if(1 > 0, \"y\", \"n\")", "y"),
])
def test_precedence_and_literals(text, value):
    if text == "1 < 2 = TRUE":
        with pytest.raises(ex.ExpressionError, match="one comparison at a time"):
            rd.parse(text)
        return
    assert ev(text) == value


def test_a_field_takes_any_name_and_double_brackets_escape():
    assert ev("[weird]]name]", row={"weird]name": "x"}) == "x"
    assert ev("[region] & [qty]", row={"region": "N", "qty": 3}) == "N3"


@pytest.mark.parametrize("text, message, pos", [
    ('"open', "a text is not closed", 0),
    ("[open", "a field is not closed", 0),
    ("1 + ", "ends too early", 4),
    ("(1 + 2", "is not closed", 0),
    ("1 2", "an operator", 2),
    ("1 != 2", "write <> for «not equal»", 2),
    ("a == b", "write = to compare", 2),
    ("x AND", "unknown name «x»", 0),
    ("SUM(1, 2", "is not closed", 0),
    ("1 + #", "unexpected character «#»", 4),
    ("'text'", "double quotes", 0),
    ("[]", "an empty field", 0),
    ("{today", "placeholder is not closed", 0),
    ("", "the expression is empty", 0),
])
def test_syntax_errors_say_where(text, message, pos):
    with pytest.raises(ex.ExpressionError) as e:
        rd.parse(text)
    assert message in e.value.message
    assert e.value.pos == pos


def test_never_eval_a_python_name_is_an_unknown_function():
    for text in ('__import__("os")', "EVAL(1)", "EXEC(1)", "OPEN(\"/etc/passwd\")"):
        err = error(text)
        assert "unknown function" in err.message and err.pos == 0


def test_a_leading_equals_is_allowed_and_positions_stay():
    assert ev("=1 + 1") == 2
    assert error("= [amont]").pos == 2


def test_nesting_is_bounded():
    with pytest.raises(ex.ExpressionError, match="nested too deeply"):
        rd.parse("(" * 200 + "1" + ")" * 200)


# ── checks ───────────────────────────────────────────────────────────────────
def test_a_field_that_does_not_exist_is_named_with_a_suggestion():
    err = error("[amount] + [amont]")
    assert err.pos == 11 and err.length == 7 and err.name == "amont"
    assert "did you mean [amount]" in err.message and "«orders»" in err.message


def test_wrong_arguments_and_types():
    assert "ROUND takes 1 to 2 arguments, 3 given" in error("ROUND(1, 2, 3)").message
    assert "join texts with &" in error('[region] + 1').message
    assert error('[region] + 1').pos == 0
    assert "compares a text with a number" in error("[region] = 1").message
    assert "condition" in error("IF([amount], 1, 2)").message
    assert "unknown function SUMM: did you mean SUM?" in error("SUMM([amount])").message
    assert "DATEADD" in error('DATEADD("q", 1, [day])').message
    assert "there is no parameter «regoin»: did you mean \"region\"?" in error('PARAM("regoin")').message
    assert 'PARAM("region")' in error("{region}").message
    assert "unknown placeholder" in error("{yesterdy}").message
    assert "is not a format" in error('FORMAT([amount], "abc")').message


def test_aggregates_and_their_scopes_are_checked():
    assert "inside another aggregate" in error("SUM(SUM([amount]))").message
    assert "there is no dataset «tragets»" in error('SUM([goal], "tragets")').message
    assert ex.check(rd.parse('SUM([goal], "targets")'), ENV) == ex.NUM
    # the fields of a named dataset are that dataset's
    assert "there is no field [amount] of the dataset «targets»" in error('SUM([amount], "targets")').message
    assert "RUNNING takes an aggregate" in error("RUNNING([amount])").message
    assert "PERCENT takes an aggregate" in error("PERCENT([amount])").message
    no_agg = ex.Env(fields=FIELDS, datasets=ENV.datasets, aggregates=False)
    assert "an aggregate cannot be written here" in error("SUM([amount])", no_agg).message


def test_page_numbers_only_in_texts_and_only_joined():
    assert "only in a text" in error("PAGE()").message
    text = ex.Env(fields=FIELDS, datasets=ENV.datasets, page=True)
    assert ex.check(rd.parse('"Page " & PAGE() & " of " & PAGES()'), text) == ex.PAGETYPE
    assert "joined to text" in error("PAGE() + 1", text).message
    assert "joined to text" in error('IF(TRUE, PAGE(), "")', text).message


def test_the_page_header_shows_no_fields_but_aggregates_of_a_dataset():
    header = ex.Env(fields=FIELDS, datasets=ENV.datasets, page=True, header=True)
    assert "show no fields" in error("[region]", header).message
    assert "names its dataset" in error("SUM([amount])", header).message
    assert ex.check(rd.parse('"Total: " & SUM([amount], "orders")'), header) == ex.TEXT


def test_a_condition_is_wanted_where_a_condition_goes():
    cond = ex.Env(fields=FIELDS, datasets=ENV.datasets, want=ex.BOOL)
    assert "a condition is wanted" in error("[amount] * 2", cond).message
    assert ex.check(rd.parse("[amount] > 0 AND [ok]"), cond) == ex.BOOL


def test_unknown_fields_pass_when_the_columns_are_not_known():
    assert ex.check(rd.parse("[anything] + 1"), ex.Env(fields=None)) == ex.NUM


# ── NULL ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, value", [
    ("NULL + 1", None), ("-NULL", None), ("NULL * 0", None), ("1 / 0", None), ("0 / 0", None),
    ("NULL = NULL", None), ("NULL <> 1", None), ("NOT NULL", None),
    ("NULL AND FALSE", False), ("NULL AND TRUE", None), ("NULL OR TRUE", True), ("NULL OR FALSE", None),
    ('"a" & NULL & "b"', "ab"), ("ISNULL(NULL)", True), ("COALESCE(NULL, NULL, 3)", 3),
    ('IF(NULL, "y", "n")', "n"), ("UPPER(NULL)", None), ("ROUND(NULL, 2)", None), ('FORMAT(NULL, "0.00")', ""),
    ("TEXT(NULL)", ""), ("LEN(NULL)", None),
])
def test_null_propagates_through_arithmetic_and_aggregates_skip_it(text, value):
    assert ev(text) == value


def test_aggregates_over_the_scope():
    assert ev("SUM([amount])", rows=ROWS) == 35.0
    assert ev("AVG([amount])", rows=ROWS) == pytest.approx(35 / 3)
    assert ev("COUNT([amount])", rows=ROWS) == 3
    assert ev("COUNT()", rows=ROWS) == 4
    assert ev("COUNTDISTINCT([region])", rows=ROWS) == 2
    assert ev("MIN([amount])", rows=ROWS) == -5.0 and ev("MAX([amount])", rows=ROWS) == 30.0
    assert ev("FIRST([amount])", rows=ROWS) == 10.0 and ev("LAST([qty])", rows=ROWS) == 3
    assert ev("SUM([amount])", rows=[]) is None and ev("COUNT([amount])", rows=[]) == 0
    # a field outside a row is the scope's first row (a group's header)
    assert ev("[region]", rows=ROWS[2:]) == "S"


def test_an_aggregate_of_a_named_dataset_reads_the_whole_dataset():
    goals = [{"goal": 5.0}, {"goal": 7.0}]
    assert ev('SUM([goal], "targets")', rows=ROWS, datasets={"targets": goals}) == 12.0
    assert ev('COUNT("targets")', datasets={"targets": goals}) == 2


def test_percent_is_over_the_same_aggregate_one_level_up():
    north, south = ROWS[:2], ROWS[2:]
    # a detail row over its group
    assert ev("PERCENT(SUM([amount]))", row=ROWS[0], rows=north) == 1.0
    ctx_rows = [ROWS, south]
    node = rd.parse("PERCENT(SUM([amount]))")
    context = ex.Context(loc=formats.locale("en"), now=NOW, scopes=ctx_rows)
    assert ex.Evaluator()(node, context) == pytest.approx(25 / 35)  # a group over the table
    assert ev("PERCENT(SUM([amount]))", rows=ROWS) == 1.0  # nothing above: all of it


def test_running_totals_follow_the_rows():
    node = rd.parse("RUNNING(SUM([amount]))")
    evaluate = ex.Evaluator()
    context = ex.Context(loc=formats.locale("en"), now=NOW, scopes=[ROWS], running=ROWS)
    out = []
    for i, r in enumerate(ROWS):
        context.row, context.running_index = r, i
        out.append(evaluate(node, context))
    assert out == [10.0, 10.0, 40.0, 35.0]
    assert ev("ROWNUMBER()", rownumber=3) == 3


def test_text_functions():
    assert ev('UPPER("abc") & LOWER("DEF") & TRIM("  x ")') == "ABCdefx"
    assert ev('LEFT("abcdef", 2) & RIGHT("abcdef", 2) & MID("abcdef", 2, 3)') == "abefbcd"
    assert ev('LEN("àèì")') == 3 and ev('REPLACE("a-b-c", "-", "+")') == "a+b+c"
    assert ev('CONTAINS("Nord-Ovest", "Ovest")') is True and ev('STARTSWITH("Nord", "S")') is False
    assert ev('CONCAT("a", 1, NULL, TRUE)') == "a1Yes"
    assert ev('TEXT(1234.5)', loc="it") == "1.234,5"


def test_maths():
    assert ev("ROUND(2.675, 2)") == 2.68 and ev("ROUND(-2.5)") == -3 and ev("ROUND(1234, -2)") == 1200
    assert ev("ABS(-3) + FLOOR(2.7) + CEILING(2.1) + POWER(2, 10)") == 3 + 2 + 3 + 1024
    assert ev("POWER(-8, 0.5)") is None


def test_dates():
    assert ev("DATE(2026, 2, 30)") is None
    assert ev("DATE(2026, 1, 31) + 1") == date(2026, 2, 1)
    assert ev('DATEADD("m", 1, DATE(2026, 1, 31))') == date(2026, 2, 28)
    assert ev('DATEADD("y", -1, DATE(2024, 2, 29))') == date(2023, 2, 28)
    assert ev('DATEADD("w", 2, DATE(2026, 1, 1))') == date(2026, 1, 15)
    assert ev('DATEDIFF("d", DATE(2026, 1, 1), DATE(2026, 3, 1))') == 59
    assert ev('DATEDIFF("m", DATE(2026, 1, 31), DATE(2026, 2, 28))') == 0
    assert ev('DATEDIFF("y", DATE(2020, 6, 1), DATE(2026, 5, 31))') == 5
    assert ev("YEAR(DATE(2026, 10, 28)) * 10000 + MONTH(DATE(2026, 10, 28)) * 100 + DAY(DATE(2026, 10, 28))") == 20261028
    assert ev("WEEKDAY(DATE(2026, 10, 28))") == 3  # Wednesday, Monday = 1
    assert ev("TODAY()") == date(2026, 10, 28) and ev("NOW()") == datetime(2026, 10, 28, 10, 30)
    assert ev("DATE(2026, 1, 2) - DATE(2026, 1, 1)") == 1
    assert ev("[at] > DATE(2026, 1, 1)", row={"at": datetime(2026, 1, 1, 8, 0)}) is True


def test_placeholders_of_tabularia_are_dates_in_the_report_time_zone():
    assert ev("{today}") == date(2026, 10, 28)
    assert ev("{today-7}") == date(2026, 10, 21)
    assert ev("{start_of_month}") == date(2026, 10, 1)
    assert ev("{today-1m}") == date(2026, 9, 28)
    assert ev("{now-2h}") == datetime(2026, 10, 28, 8, 30)
    assert ev("{today:yyyyMMdd}") == "20261028"


def test_parameters():
    assert ev('"Region " & PARAM("region")', params={"region": "Nord"}) == "Region Nord"
    assert ev('[region] = PARAM("regions")', row={"region": "N"}, params={"regions": ["N", "S"]}) is True
    assert ev('[region] <> PARAM("regions")', row={"region": "E"}, params={"regions": ["N", "S"]}) is True


def test_page_numbers_become_parts_for_the_template():
    v = ev('"Page " & PAGE() & " of " & PAGES()')
    assert isinstance(v, ex.PageText) and list(v) == ["Page ", ex.PAGE, " of ", ex.PAGES]


def test_a_text_added_to_a_number_fails_at_render_time_too():
    with pytest.raises(ex.EvalError, match="works on numbers"):
        ev("[a] + 1", row={"a": "text"})
    with pytest.raises(ex.EvalError, match="cannot compare a text with a number"):
        ev("[a] > 1", row={"a": "text"})


def test_format_follows_the_locale():
    assert ev('FORMAT(1234.5, "#,##0.00")', loc="it") == "1.234,50"
    assert ev('FORMAT(DATE(2026, 3, 5), "d MMMM yyyy")', loc="de") == "5 März 2026"
