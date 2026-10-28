"""The dynamic placeholders of the «API call» datasource: our own tiny parser, no eval.

`{today}`, `{now-2h}`, `{start_of_month-1m:%Y%m%d}`, `{last_refresh}`, `{symbol}`…
resolved in the datasource's time zone. What is fixed here: the grammar, the
arithmetic (months clamped to the month's end), the formats, the time zone, the
escapes and the errors a person reads when saving.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.ingest import api_placeholders as ph

# a Saturday evening in UTC, already Sunday in Tokyo
NOW = datetime(2026, 10, 17, 22, 30, 5, tzinfo=timezone.utc)


def ctx(tz="UTC", last_refresh=None, variables=None, initial=""):
    return ph.make_context(NOW, tz, last_refresh, initial, variables or {})


def r(template, **kw):
    return ph.render(template, ctx(**kw))


def test_today_yesterday_now_in_iso():
    assert r("{today}") == "2026-10-17"
    assert r("{yesterday}") == "2026-10-16"
    assert r("{now}") == "2026-10-17T22:30:05+00:00"


def test_the_time_zone_defines_today():
    assert r("{today}", tz="Asia/Tokyo") == "2026-10-18"
    assert r("{now}", tz="Europe/Rome") == "2026-10-18T00:30:05+02:00"
    assert r("{today}", tz="America/New_York") == "2026-10-17"


@pytest.mark.parametrize("template,expected", [
    ("{today-1}", "2026-10-16"),
    ("{today-7d}", "2026-10-10"),
    ("{today+1d}", "2026-10-18"),
    ("{today-2w}", "2026-10-03"),
    ("{today-1m}", "2026-09-17"),
    ("{today-1y}", "2025-10-17"),
    ("{start_of_month}", "2026-10-01"),
    ("{start_of_month-1m}", "2026-09-01"),
    ("{start_of_week}", "2026-10-12"),
    ("{start_of_year}", "2026-01-01"),
    ("{now-2h}", "2026-10-17T20:30:05+00:00"),
    ("{now-30min}", "2026-10-17T22:00:05+00:00"),
    ("{now+15s}", "2026-10-17T22:30:20+00:00"),
    ("{now-1d}", "2026-10-16T22:30:05+00:00"),
])
def test_offsets(template, expected):
    assert r(template) == expected


def test_months_are_clamped_to_the_end_of_the_month():
    march31 = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    c = ph.make_context(march31, "UTC", None, "", {})
    assert ph.render("{today-1m}", c) == "2026-02-28"
    assert ph.render("{today+11m}", c) == "2027-02-28"
    leap = ph.make_context(datetime(2024, 2, 29, tzinfo=timezone.utc), "UTC", None, "", {})
    assert ph.render("{today+1y}", leap) == "2025-02-28"


@pytest.mark.parametrize("template,expected", [
    ("{today:%Y%m%d}", "20261017"),
    ("{today:%d/%m/%Y}", "17/10/2026"),
    ("{now:%Y-%m-%dT%H:%M:%SZ}", "2026-10-17T22:30:05Z"),
    ("{now:epoch}", "1792276205"),
    ("{now:epoch_ms}", "1792276205000"),
    ("{today:epoch}", "1792195200"),
    ("{today-1:%Y%m%d}", "20261016"),
])
def test_formats(template, expected):
    assert r(template) == expected


def test_epoch_of_a_date_is_its_midnight_in_the_time_zone():
    assert r("{today:epoch}", tz="Europe/Rome") == str(int(datetime(2026, 10, 18, tzinfo=timezone.utc).timestamp()) - 2 * 3600)


def test_text_around_and_several_placeholders():
    assert r("/v1/{today-30}..?d={yesterday}") == "/v1/2026-09-17..?d=2026-10-16"


def test_double_braces_are_literal_braces():
    assert r("{{today}} {{ and }}") == "{today} { and }"
    assert r("{{{today}}}") == "{2026-10-17}"


def test_variables():
    assert r("/quote/{symbol}", variables={"symbol": "ENI.MI"}) == "/quote/ENI.MI"


def test_a_quote_function_is_applied_to_values_only():
    from urllib.parse import quote

    c = ctx(variables={"symbol": "A/B C"})
    assert ph.render("/q/{symbol}/x", c, quote=lambda v: quote(v, safe="")) == "/q/A%2FB%20C/x"


def test_last_refresh_is_the_previous_successful_refresh_in_the_time_zone():
    assert r("{last_refresh}", last_refresh="2026-10-16T06:00:00+00:00", tz="Europe/Rome") == "2026-10-16T08:00:00+02:00"
    assert r("{last_refresh:%Y-%m-%d}", last_refresh="2026-10-16T23:30:00Z", tz="Europe/Rome") == "2026-10-17"
    assert r("{last_refresh-1h}", last_refresh="2026-10-16T06:00:00Z") == "2026-10-16T05:00:00+00:00"


def test_last_refresh_on_the_first_refresh_takes_the_initial_expression():
    assert r("{last_refresh}", initial="today-365") == "2025-10-17T00:00:00+00:00"
    assert r("{last_refresh:%Y-%m-%d}", initial="2024-01-01") == "2024-01-01"
    assert r("{last_refresh}", initial="now-2h") == "2026-10-17T20:30:05+00:00"
    assert r("{last_refresh}", initial="start_of_month", tz="Europe/Rome") == "2026-10-01T00:00:00+02:00"


def test_last_refresh_without_any_value_is_a_clear_error():
    with pytest.raises(ph.PlaceholderError, match="initial"):
        r("{last_refresh}")


@pytest.mark.parametrize("template,message", [
    ("{tomorrow}", "unknown placeholder"),
    ("{today-}", "bad placeholder"),
    ("{today*2}", "bad placeholder"),
    ("{today-2h}", "hours"),
    ("{now-2}", "unit"),
    ("{today:%}", None),
    ("{today:weird}", "format"),
    ("{symbol-1}", "variable"),
    ("{symbol:%Y}", "variable"),
    ("{today", "not closed"),
    ("today}", "single"),
    ("{}", "empty"),
    ("{today-99999999d}", "bad placeholder"),
])
def test_errors_say_what_is_wrong(template, message):
    with pytest.raises(ph.PlaceholderError) as e:
        ph.check(template, variables={"symbol"}, has_initial=True)
        r(template, variables={"symbol": "X"})
    if message:
        assert message in str(e.value)


def test_check_finds_the_variables_used():
    used = ph.check("/q/{symbol}?d={today}&x={{literal}}", variables={"symbol", "unused"}, has_initial=False)
    assert used == {"symbol"}


def test_check_wants_an_initial_value_for_last_refresh():
    with pytest.raises(ph.PlaceholderError, match="initial"):
        ph.check("{last_refresh}", variables=set(), has_initial=False)
    assert ph.check("{last_refresh}", variables=set(), has_initial=True) == set()


@pytest.mark.parametrize("expr", ["today-365", "now-2h", "start_of_year", "2024-01-01", "2024-01-01T10:00:00Z", "yesterday"])
def test_initial_expressions(expr):
    assert isinstance(ph.resolve_initial(expr, NOW, ph.zone("UTC")), datetime)


@pytest.mark.parametrize("expr", ["last_refresh", "today:%Y", "{today}", "banana", "today-2h", ""])
def test_bad_initial_expressions(expr):
    with pytest.raises(ph.PlaceholderError):
        ph.resolve_initial(expr, NOW, ph.zone("UTC"))


def test_an_unknown_time_zone_is_an_error():
    with pytest.raises(ph.PlaceholderError, match="time zone"):
        ph.zone("Mars/Olympus")
    with pytest.raises(ph.PlaceholderError, match="time zone"):
        ph.zone("../../etc/passwd")


def test_a_variable_cannot_shadow_a_builtin():
    with pytest.raises(ph.PlaceholderError, match="reserved"):
        ph.check_variable_name("today")
    with pytest.raises(ph.PlaceholderError):
        ph.check_variable_name("1abc")
    ph.check_variable_name("symbol")


def test_dates_do_not_leak_a_time():
    assert ph.evaluate(ph.parse("{today}")[0], ctx()) == date(2026, 10, 17)


# ── readable date formats (yyyy-MM-dd, ddmmyyyy…), besides strftime ─────────────
@pytest.mark.parametrize("template, expected", [
    ("{today:yyyy-MM-dd}", "2026-10-17"),
    ("{today:ddMMyyyy}", "17102026"),
    ("{today:ddmmyyyy}", "17102026"),           # Excel's rule: mm is the month unless next to hours or seconds
    ("{today:dd/MM/yy}", "17/10/26"),
    ("{today-1:yyyyMMdd}", "20261016"),
    ("{now:yyyy-MM-dd HH:mm:ss}", "2026-10-17 22:30:05"),
    ("{now:HHmm}", "2230"),                      # mm right after the hours: minutes
    ("{now:mm:ss}", "30:05"),                    # mm right before the seconds: minutes
    ("{now:yyyyMMdd'T'HHmmss}", "20261017T223005"),
    # the names of months and days, in English when the context has no language
    ("{today:MMMM yyyy}", "October 2026"),
    ("{today:dd MMM yy}", "17 Oct 26"),
    ("{today:EEEE dd MMMM}", "Saturday 17 October"),
    ("{today:EEE ddMMyyyy}", "Sat 17102026"),
    ("{today:MMMM mm}", "October 10"),             # mm is still the month away from hours and seconds
    ("{now:EEE HH:mm}", "Sat 22:30"),
])
def test_readable_date_formats(template, expected):
    assert ph.render(template, ctx()) == expected


@pytest.mark.parametrize("lang, expected", [
    ("en", "Saturday 17 October 2026 · Oct"), ("it", "sabato 17 ottobre 2026 · ott"),
    ("de", "Samstag 17 Oktober 2026 · Okt."), ("es", "sábado 17 octubre 2026 · oct"),
    ("fr", "samedi 17 octobre 2026 · oct."),
])
def test_names_of_months_and_days_are_written_in_the_context_s_language(lang, expected):
    c = ctx()
    c.lang = lang
    assert ph.render("{today:EEEE dd MMMM yyyy} · {today:MMM}", c) == expected
    assert ph.render("{today:%Y-%m}", c) == "2026-10"  # strftime is strftime, in every language


@pytest.mark.parametrize("template", ["{today:dd-qq-yyyy}", "{today:ddd}", "{today:yyy}", "{today:MMMMM}",
                                      "{today:EE}", "{today:E}"])
def test_an_unknown_readable_token_is_refused(template):
    with pytest.raises(ph.PlaceholderError):
        ph.check(template)


# ── review 2026-10-18 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("template,message", [
    ("{now:%s}", "epoch"),
    ("{today:%-s}", "epoch"),
    ("{today:%z}", "date"),
    ("{today:%Z}", "date"),
    ("{now:hh:mm}", "HH"),
    ("{today:dddd}", "twice"),
    ("{today:yyyy-MM-ddTHH:mm}", "'T'"),
    ("{today:''}", "no date"),
    ("{today:-}", "no date"),
])
def test_formats_the_review_refused(template, message):
    with pytest.raises(ph.PlaceholderError, match=message):
        ph.check(template)


def test_a_time_zone_offset_is_fine_on_a_date_time():
    assert r("{now:%z}", tz="Europe/Rome") == "+0200"


def test_quotes_in_readable_formats():
    assert r("{today:yyyy'''s'}") == "2026's"
    assert r("{today:'it''s' yyyy}") == "it's 2026"
    assert r("{today:'%s' yyyy}") == "%s 2026"  # a % inside quotes is text, not strftime
