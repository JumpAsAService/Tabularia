"""A report's parameters (docs/specs/reports.md, «Parameters» and «Datasets»): values
from text, and their place in a dataset's SQL.

The rules these tests hold: a value never reaches the SQL as typed — a string is quoted
and escaped for its dialect and reads back as the same string, whatever quotes,
backslashes, comment marks or statements it holds; a number is a number (a negative
one cannot make a comment); a parameter inside quotes is refused, in a comment left
alone; a reading of the SQL that depends on the server (a backslash in MySQL, nested
comments in PostgreSQL) is refused before a value goes near it; an empty list stops;
a value that does not fit its type or its choices is a 400 naming the parameter.
"""
from datetime import date

import pytest
import sqlglot
from fastapi import HTTPException
from sqlglot import exp

from app.services import report_params as rp

pytestmark = pytest.mark.anyio

HOSTILE = ["it's", "a\\b", "a\\", "a\\'b", "x'; DROP TABLE t; --", "-- not a comment", "/* nor this */",
           "line\nbreak", "{region}", "{{", "$$", 'dq"x', "back`tick", "ò € 中"]
PARAMS = {"region": {"name": "region", "type": "text"}, "n": {"name": "n", "type": "number"},
          "since": {"name": "since", "type": "date"}, "regions": {"name": "regions", "type": "text", "multi": True},
          "flag": {"name": "flag", "type": "boolean"}}


def _refused(fn, *args) -> str:
    with pytest.raises(HTTPException) as e:
        fn(*args)
    assert e.value.status_code == 422
    return e.value.detail


def _fill(sql: str, key: str, **values) -> str:
    d = rp.dialect_of(key)
    rp.check_sql(sql, d, PARAMS)
    return rp.fill(sql, d, PARAMS, values, {"today": "2026-10-28", "today-30": "2026-09-28",
                                            "now": "2026-10-28T09:15:00+01:00", "today:dd/MM/yyyy": "28/10/2026"})


# ── literals ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", ["polars", "duckdb", "chdb", "bigquery", "postgresql", "mysql", "trino", "oracle",
                                 "sqlserver"])
@pytest.mark.parametrize("value", HOSTILE)
def test_a_text_reads_back_as_itself_in_its_dialect(key, value):
    d = rp.dialect_of(key)
    sql = rp.fill("SELECT * FROM t WHERE region = {region} AND 1 = 1", d, PARAMS, {"region": value}, {})
    tree = sqlglot.parse_one(sql, read=d.sqlglot)
    eq = tree.find(exp.EQ)
    literal = eq.expression
    if isinstance(literal, (exp.National, exp.ByteString)):  # SQL Server's N'…', PostgreSQL's E'…'
        literal = exp.Literal.string(literal.this)
    assert isinstance(literal, exp.Literal) and literal.is_string, sql
    assert literal.this == value, sql
    # the rest of the statement is the author's, intact: one statement, its condition after
    assert len(sqlglot.parse(sql, read=d.sqlglot)) == 1 and sql.endswith("AND 1 = 1")


def test_the_quote_is_never_escaped_with_a_backslash_where_the_server_decides():
    # MySQL with or without NO_BACKSLASH_ESCAPES, PostgreSQL with or without
    # standard_conforming_strings: a doubled quote is a quote either way, \' is not
    for key in ("mysql", "mariadb", "postgresql"):
        sql = rp.literal("a\\'b", "text", rp.dialect_of(key))
        assert "\\'" not in sql.replace("\\\\", ""), (key, sql)
    assert rp.literal("a\\b", "text", rp.dialect_of("postgresql")) == "E'a\\\\b'"  # an escape string either way
    assert rp.literal("it's", "text", rp.dialect_of("postgresql")) == "'it''s'"
    assert rp.literal("a\\b", "text", rp.dialect_of("mysql")) == "'a\\\\b'"
    assert rp.literal("òk", "text", rp.dialect_of("sqlserver")) == "N'òk'"
    assert rp.literal("it's", "text", rp.dialect_of("bigquery")) == "'it\\'s'"


def test_numbers_dates_booleans_and_lists():
    pg, ms, ch, bq = (rp.dialect_of(k) for k in ("postgresql", "sqlserver", "clickhouse", "bigquery"))
    assert rp.literal(-5, "number", pg) == "(-5)" and rp.literal(1.5e-7, "number", pg) == "0.00000015"
    assert rp.literal(12, "number", pg) == "12"
    assert rp.literal(date(2026, 1, 2), "date", pg) == "DATE '2026-01-02'"
    assert rp.literal(date(2026, 1, 2), "date", ms) == "CAST('2026-01-02' AS DATE)"
    assert rp.literal(date(2026, 1, 2), "date", ch) == "toDate('2026-01-02')"
    assert (rp.literal(True, "boolean", pg), rp.literal(False, "boolean", ms)) == ("TRUE", "0")
    assert rp.literal(["a", "b"], "text", pg) == "('a', 'b')"
    assert rp.literal(None, "text", pg) == "NULL"
    assert "has no value" in _refused(rp.literal, [], "text", pg, "regions")
    assert _fill("SELECT 1 WHERE d >= {now}", "bigquery") == "SELECT 1 WHERE d >= DATETIME '2026-10-28 09:15:00'"


def test_a_negative_number_cannot_turn_the_rest_into_a_comment():
    sql = _fill("SELECT * FROM t WHERE a = 1 -{n} AND secret = 0", "postgresql", n=-5)
    assert sql == "SELECT * FROM t WHERE a = 1 -(-5) AND secret = 0"
    assert "--" not in sql


# ── where a brace is ──────────────────────────────────────────────────────────
def test_parameters_strings_comments_and_placeholders():
    sql = ("SELECT '{today}' AS d, \"{today}\" AS q, x -- {region} stays\n"
           "FROM t /* {region} too */ WHERE r IN {regions} AND d >= {today-30} AND f = {flag} AND j = '{\"a\": 1}' "
           "AND k = '{{today}}' AND l = '{{\"b\": {{\"c\": 2}}}}' AND m = {{literal}}")
    assert _fill(sql, "postgresql", regions=["N", "S"], flag=True) == (
        "SELECT '2026-10-28' AS d, \"2026-10-28\" AS q, x -- {region} stays\n"
        "FROM t /* {region} too */ WHERE r IN ('N', 'S') AND d >= DATE '2026-09-28' AND f = TRUE AND j = '{\"a\": 1}' "
        "AND k = '{today}' AND l = '{{\"b\": {{\"c\": 2}}}}' AND m = {literal}")
    # a placeholder with a format outside quotes is text
    assert _fill("SELECT {today:dd/MM/yyyy}", "postgresql") == "SELECT '28/10/2026'"


@pytest.mark.parametrize("sql", ["SELECT * FROM t WHERE r = '{region}'", 'SELECT * FROM t WHERE r = "{region}"',
                                 "SELECT * FROM t WHERE r = $${region}$$", "SELECT * FROM t WHERE r = $q$ {region} $q$",
                                 "SELECT * FROM t WHERE r = E'{region}'", "SELECT * FROM t WHERE r LIKE '%{region}%'"])
def test_a_parameter_inside_quotes_is_refused(sql):
    assert _refused(rp.check_sql, sql, rp.dialect_of("postgresql"), PARAMS) == \
        "Write {region} without quotes: Tabularia quotes it"


def test_dollar_quotes_hide_what_they_hold():
    # the apostrophe inside the dollar quotes opens no string: the parameter after is code
    sql = "SELECT $tag$ it's $tag$ AS x FROM t WHERE r = {region}"
    assert _fill(sql, "postgresql", region="N") == "SELECT $tag$ it's $tag$ AS x FROM t WHERE r = 'N'"
    # in a dialect without them, the same text is not trusted that way
    assert "without quotes" in _refused(rp.check_sql, sql, rp.dialect_of("mysql"), PARAMS)


def test_what_is_neither_a_parameter_nor_a_placeholder():
    d = rp.dialect_of("postgresql")
    assert "neither a parameter" in _refused(rp.check_sql, "SELECT {nope}", d, PARAMS)
    assert "no offset nor format" in _refused(rp.check_sql, "SELECT {region:upper}", d, PARAMS)
    assert "A single }" in _refused(rp.check_sql, "SELECT 1 }", d, PARAMS)
    assert "is not closed" in _refused(rp.check_sql, "SELECT {region", d, PARAMS)
    # ClickHouse's own query parameters are not Tabularia's
    assert "neither a parameter" in _refused(rp.check_sql, "SELECT {id:UInt32}", rp.dialect_of("clickhouse"), PARAMS)


@pytest.mark.parametrize("key, sql", [
    # MySQL: whether the backslash escapes depends on the server's sql_mode
    ("mysql", "SELECT * FROM t WHERE path = 'C:\\' AND r = {region} -- '"),
    ("mysql", "SELECT * FROM t WHERE a = 'it\\'s' AND r = {region}"),
    # PostgreSQL nests block comments, the gateway's own reading does not
    ("postgresql", "SELECT * FROM t /* a /* b */ WHERE r = {region} */"),
])
def test_a_reading_that_depends_on_the_server_is_refused(key, sql):
    assert "unclear" in _refused(rp.check_sql, sql, rp.dialect_of(key), PARAMS)


def test_a_backslash_that_changes_nothing_is_fine():
    sql = "SELECT * FROM t WHERE path = 'C:\\temp' AND r = {region}"
    assert _fill(sql, "mysql", region="x") == "SELECT * FROM t WHERE path = 'C:\\temp' AND r = 'x'"


def test_brackets_quote_names_on_sql_server_only():
    assert _fill("SELECT [a{b}] FROM t WHERE r = {region}", "sqlserver", region="N") == \
        "SELECT [a{b}] FROM t WHERE r = N'N'"
    assert "without quotes" in _refused(rp.check_sql, "SELECT [{region}]", rp.dialect_of("sqlserver"), PARAMS)


def test_a_placeholder_whose_text_cannot_sit_in_quotes():
    d = rp.dialect_of("postgresql")
    with pytest.raises(HTTPException) as e:
        rp.fill("SELECT '{today:x}'", d, PARAMS, {}, {"today:x": "it's"})
    assert "cannot go inside quoted text" in e.value.detail


# ── values ────────────────────────────────────────────────────────────────────
DEFINITION = {"v": 1, "timezone": "Europe/Rome", "parameters": [
    {"name": "region", "type": "text", "required": True, "choices": {"values": ["North", "South"]}},
    {"name": "since", "type": "date", "default": "{today-30}"},
    {"name": "regions", "type": "text", "multi": True},
    {"name": "top", "type": "number", "default": 10},
    {"name": "net", "type": "boolean", "default": False},
]}


def _bad(e) -> tuple[int, str, str]:
    return e.value.status_code, e.value.detail["parameter"], e.value.detail["message"]


async def test_values_are_converted_and_defaults_resolved_in_the_report_s_zone(fake_engine):
    fake_engine.placeholder_values = {"{today-30}": "2026-09-28"}
    values = await rp.values_for(DEFINITION, {"region": ["North"], "regions": ["a", "b"], "net": ["yes"]})
    assert values == {"region": "North", "since": date(2026, 9, 28), "regions": ["a", "b"], "top": 10, "net": True}
    assert fake_engine.placeholder_previews[-1]["timezone"] == "Europe/Rome"
    # a placeholder as a value is resolved as well; `?since=` is no value: the default
    fake_engine.placeholder_values["{today}"] = "2026-10-28"
    values = await rp.values_for(DEFINITION, {"region": ["South"], "since": ["{today}"], "top": ["2.5"]})
    assert values["since"] == date(2026, 10, 28) and values["top"] == 2.5 and values["regions"] == []


async def test_no_value_said_out_loud_empties_an_optional_parameter_with_a_default(fake_engine):
    fake_engine.placeholder_values = {"{today-30}": "2026-09-28"}
    values = await rp.values_for(DEFINITION, {"region": ["North"], "since": ["{none}"], "top": ["{none}"],
                                              "regions": ["{none}"]})
    assert values["since"] is None and values["top"] is None and values["regions"] == []
    # a required one still needs its value, and an empty one still takes the default
    with pytest.raises(HTTPException) as e:
        await rp.values_for(DEFINITION, {"region": ["{none}"]})
    assert e.value.status_code == 400 and e.value.detail["parameter"] == "region"
    values = await rp.values_for(DEFINITION, {"region": ["North"], "since": [""], "top": []})
    assert values["since"] == date(2026, 9, 28) and values["top"] == 10
    rp.check_values(DEFINITION, {"region": ["North"], "since": ["{none}"]})  # as a Pushmail cell is checked


@pytest.mark.parametrize("given, parameter, words", [
    ({"region": ["North"], "nope": ["1"]}, "nope", "no parameter"),
    ({}, "region", "needs a value"),
    ({"region": ["West"]}, "region", "not one of its choices"),
    ({"region": ["North", "South"]}, "region", "takes one value"),
    ({"region": ["North"], "since": ["2026-02-30"]}, "since", "not a date"),
    ({"region": ["North"], "since": ["30/01/2026"]}, "since", "not a date"),
    ({"region": ["North"], "since": ["2026-01-01'; DROP TABLE t; --"]}, "since", "not a date"),
    ({"region": ["North"], "top": ["1,5"]}, "top", "not a number"),
    ({"region": ["North"], "top": ["1; DROP"]}, "top", "not a number"),
    ({"region": ["North"], "top": ["nan"]}, "top", "not a number"),
    ({"region": ["North"], "top": ["1e300"]}, "top", "too large"),
    ({"region": ["North"], "net": ["maybe"]}, "net", "not true or false"),
    ({"region": ["North\x00"]}, "region", "NUL"),
])
async def test_a_value_that_does_not_fit_is_a_400_naming_its_parameter(fake_engine, given, parameter, words):
    fake_engine.placeholder_values = {"{today-30}": "2026-09-28"}
    with pytest.raises(HTTPException) as e:
        await rp.values_for(DEFINITION, given)
    status, name, message = _bad(e)
    assert (status, name) == (400, parameter) and words in message


async def test_a_default_placeholder_the_engine_cannot_resolve(fake_engine):
    fake_engine.placeholder_errors = {"p0": "unknown placeholder {today-30}"}
    with pytest.raises(HTTPException) as e:
        await rp.values_for(DEFINITION, {"region": ["North"]})
    assert _bad(e)[:2] == (400, "since")


# ── the quotes each database has of its own ───────────────────────────────────
@pytest.mark.parametrize("key, sql", [
    # Oracle's q-quotes: the apostrophe inside does not close them
    ("oracle", "SELECT * FROM t WHERE note = q'[it's {region}]'"),
    ("oracle", "SELECT * FROM t WHERE note = Q'{it's {region}}'"),
    ("oracle", "SELECT * FROM t WHERE note = nq'<it's {region}>'"),
    ("oracle", "SELECT * FROM t WHERE note = q'!it's {region}!'"),
    # ClickHouse's heredocs, on a connection and on the catalog (chDB)
    ("clickhouse", "SELECT * FROM t WHERE note = $$ {region} it's $$"),
    ("chdb", "SELECT * FROM t WHERE note = $doc$ {region} it's $doc$"),
    # BigQuery's triple quotes
    ("bigquery", "SELECT * FROM t WHERE note = '''it's {region}'''"),
    ("bigquery", 'SELECT * FROM t WHERE note = """say "hi" {region}"""'),
])
def test_a_parameter_inside_a_database_s_own_quotes_is_refused(key, sql):
    # read as plain quotes, the parameter would seem outside them and its value would land
    # inside the database's string, where a value ending the quotes could follow
    assert _refused(rp.check_sql, sql, rp.dialect_of(key), PARAMS) == \
        "Write {region} without quotes: Tabularia quotes it"


@pytest.mark.parametrize("key, sql, filled", [
    ("oracle", "SELECT * FROM t WHERE note = q'[it's]' AND r = {region}",
     "SELECT * FROM t WHERE note = q'[it's]' AND r = 'N'"),
    ("oracle", "SELECT q'(a)' AS x, '{today}' AS d FROM t WHERE r = {region}",
     "SELECT q'(a)' AS x, '2026-10-28' AS d FROM t WHERE r = 'N'"),
    ("clickhouse", "SELECT $$it's$$ AS x FROM t WHERE r = {region}", "SELECT $$it's$$ AS x FROM t WHERE r = 'N'"),
    ("bigquery", "SELECT '''it's''' AS x FROM t WHERE r = {region}", "SELECT '''it's''' AS x FROM t WHERE r = 'N'"),
])
def test_a_database_s_own_quotes_hide_what_they_hold(key, sql, filled):
    assert _fill(sql, key, region="N") == filled
    if key != "oracle":  # sqlglot does not read Oracle's q-quotes
        tree = sqlglot.parse_one(filled, read=rp.dialect_of(key).sqlglot)
        assert tree.find(exp.EQ).expression.this == "N"


async def test_a_value_given_twice_counts_once(fake_engine):
    fake_engine.placeholder_values = {"{today-30}": "2026-09-28"}
    values = await rp.values_for(DEFINITION, {"region": ["North"], "regions": ["a", "b", "a", "b"]})
    assert values["regions"] == ["a", "b"]
    many = {**DEFINITION, "parameters": [{"name": "n", "type": "number", "multi": True}]}
    assert (await rp.values_for(many, {"n": ["1", "1.0", "2"]}))["n"] == [1, 2]


async def test_a_plus_in_a_query_string_is_a_space(fake_engine):
    # «?since={today+7}» typed in an address: the + arrives as a space
    with pytest.raises(HTTPException) as e:
        await rp.values_for(DEFINITION, {"region": ["North"], "since": ["{today 7}"]})
    assert _bad(e)[:2] == (400, "since") and "%2B" in _bad(e)[2]


async def test_the_names_of_months_and_days_are_in_the_report_s_language(fake_engine):
    # {today:MMMM} in a default, a value or a dataset's SQL: «ottobre» in an Italian report
    fake_engine.placeholder_values = {"{today-30}": "2026-09-28"}
    await rp.values_for({**DEFINITION, "locale": "it"}, {"region": ["North"]})
    assert fake_engine.placeholder_previews[-1]["lang"] == "it"
    await rp.resolve_placeholders({"today:MMMM"}, "Europe/Rome", "de")
    assert fake_engine.placeholder_previews[-1]["lang"] == "de"
    await rp.values_for({**DEFINITION, "locale": "xx"}, {"region": ["North"]})
    assert fake_engine.placeholder_previews[-1]["lang"] == "en"
