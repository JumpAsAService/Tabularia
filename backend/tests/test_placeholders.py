"""The one placeholder language of every input and output (app.core.placeholders).

The API datasource's grammar (test_api_placeholders) is the base; here what the
shared module adds: the Outputs' context ({run_id}, {flow}, the old {date} and
{datetime} byte-identical), the names an Output may never escape its folder
with, and the RANGE variables (`day = today-7 .. today step 1d`).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core import placeholders as ph

# a Saturday evening in UTC, already Sunday in Tokyo
NOW = datetime(2026, 10, 17, 22, 30, 5, tzinfo=timezone.utc)


def test_the_api_module_is_the_shared_one():
    from app.ingest import api_placeholders

    assert api_placeholders.render is ph.render
    assert api_placeholders.PlaceholderError is ph.PlaceholderError
    assert api_placeholders.make_context is ph.make_context


# ── outputs ───────────────────────────────────────────────────────────────────
def out(template, kind="file", tz="UTC", run_id=42, flow="Report/mensile", now=NOW):
    return ph.render_output(template, ph.output_context(now, tz, run_id, flow), kind=kind, what="file name")


@pytest.mark.parametrize("template, expected", [
    # the SFTP Output's placeholders before the shared language: the same bytes
    ("vendite_{date}", "vendite_2026-10-17"),
    ("vendite_{datetime}", "vendite_2026-10-17_223005"),
    ("run_{run_id}", "run_42"),
    ("{flow}_{date}", "Report_mensile_2026-10-17"),
    # and the whole grammar on top
    ("{today:yyyyMMdd}", "20261017"),
    ("{yesterday:ddMMyyyy}", "16102026"),
    ("{now:HH'h'mm}", "22h30"),
    ("{date-1d}", "2026-10-16"),
    ("{start_of_month:yyyy-MM}", "2026-10"),
    ("{{x}}_{today}", "{x}_2026-10-17"),
])
def test_output_placeholders(template, expected):
    assert out(template) == expected


def test_an_output_resolves_in_its_time_zone():
    assert out("{date}_{datetime}", tz="Asia/Tokyo") == "2026-10-18_2026-10-18_073005"
    assert out("{today}", tz="Europe/Rome") == "2026-10-18"
    assert out("{today}") == "2026-10-17"


def test_flow_is_kept_as_it_is_in_a_text():
    assert out("Report {flow} {today}", kind="text") == "Report Report/mensile 2026-10-17"


def test_last_refresh_has_no_meaning_in_an_output():
    with pytest.raises(ph.PlaceholderError, match="Output"):
        ph.check("{last_refresh}", output=True)


def test_run_id_and_flow_take_no_offset_or_format():
    with pytest.raises(ph.PlaceholderError, match="no offset or format"):
        ph.check("{run_id:%Y}", output=True)
    with pytest.raises(ph.PlaceholderError, match="no offset or format"):
        ph.check("{flow-1}", output=True)


def test_run_id_unknown_outside_a_run():
    with pytest.raises(ph.PlaceholderError, match="only known when the flow runs"):
        out("x_{run_id}", run_id=None)


def test_unknown_output_placeholder_names_the_ones_there_are():
    with pytest.raises(ph.PlaceholderError, match=r"unknown placeholder \{week\}.*\{run_id\}.*\{flow\}"):
        ph.check("x_{week}", output=True)


def test_output_only_names_are_not_builtins_of_an_input():
    with pytest.raises(ph.PlaceholderError, match=r"unknown placeholder \{run_id\}"):
        ph.check("{run_id}")
    assert ph.check("{date}", variables={"date"}) == {"date"}  # a variable may be called date in an input


def test_a_slash_in_a_value_cannot_make_a_file_name_a_path():
    with pytest.raises(ph.PlaceholderError, match="'/'"):
        out("{today:yyyy/MM/dd}.csv")
    with pytest.raises(ph.PlaceholderError, match="'/'"):
        out("{today:yyyy'\\'MM}x")


def test_a_path_may_hold_folders_from_values_but_never_go_up():
    assert out("exports/{today:yyyy/MM}/v.csv", kind="path") == "exports/2026/10/v.csv"
    with pytest.raises(ph.PlaceholderError, match=r"'\.\.'"):
        out("exports/../v.csv", kind="path")


def test_flow_in_a_path_is_made_safe():
    assert out("x/{flow}/y", kind="path", flow="../../etc") == "x/etc/y"
    assert out("{flow}", flow="..") == "flow"


def test_an_output_template_is_checked_like_the_api_ones():
    with pytest.raises(ph.PlaceholderError, match="not closed"):
        ph.check("x_{today", output=True)
    with pytest.raises(ph.PlaceholderError, match="%s is not supported"):
        ph.check("{now:%s}", output=True)


# ── range variables ───────────────────────────────────────────────────────────
def rng(start, end, step, tz="UTC", last_refresh=None, initial="", now=NOW, cap=1000):
    c = ph.make_context(now, tz, last_refresh, initial, {})
    return ph.expand_range("day", start, end, step, c, max_values=cap)


def test_a_day_range_is_half_open():
    values = rng("today-3", "today", "1d")
    assert [v for v, _ in values] == [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
    assert [e for _, e in values] == [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)]


def test_the_last_window_ends_at_the_range_end():
    values = rng("2026-10-01", "2026-10-10", "1w")
    assert values == [(date(2026, 10, 1), date(2026, 10, 8)), (date(2026, 10, 8), date(2026, 10, 10))]


def test_month_steps_are_counted_from_the_start_and_clamped():
    values = rng("2026-01-31", "2026-05-01", "1m")
    assert [v for v, _ in values] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_an_hour_range_gives_date_times_in_the_zone():
    values = rng("now-3h", "now", "1h", tz="Europe/Rome")
    assert [v.isoformat() for v, _ in values] == [
        "2026-10-17T21:30:05+02:00", "2026-10-17T22:30:05+02:00", "2026-10-17T23:30:05+02:00"]
    assert values[-1][1].isoformat() == "2026-10-18T00:30:05+02:00"


def test_a_range_from_a_date_by_hours_starts_at_midnight():
    values = rng("today", "today+1", "6h")
    assert [v.strftime("%H") for v, _ in values] == ["00", "06", "12", "18"]


def test_a_range_can_start_at_the_last_refresh():
    values = rng("last_refresh", "today", "1d", last_refresh="2026-10-14T03:00:00+00:00")
    assert [v.isoformat() for v, _ in values] == [
        "2026-10-14T03:00:00+00:00", "2026-10-15T03:00:00+00:00", "2026-10-16T03:00:00+00:00"]
    with pytest.raises(ph.PlaceholderError, match="initial value"):
        rng("last_refresh", "today", "1d")
    assert len(rng("last_refresh", "today", "1d", initial="today-2")) == 2


@pytest.mark.parametrize("start, end, step, message", [
    ("today", "today-3", "1d", "empty"),
    ("today", "today", "1d", "empty"),
    ("today-3", "today", "0d", "step"),
    ("today-3", "today", "1x", "step"),
    ("today-3", "today", "", "step"),
    ("{today-3}", "today", "1d", "without braces"),
    ("tomorrow", "today", "1d", "start"),
    ("today-3", "now:%Y", "1d", "end"),
    ("today-3", "today", "1s", "step"),
])
def test_bad_ranges_are_refused(start, end, step, message):
    with pytest.raises(ph.PlaceholderError, match=message):
        rng(start, end, step)


def test_the_number_of_values_is_capped():
    assert len(rng("today-30", "today", "1d", cap=30)) == 30
    with pytest.raises(ph.PlaceholderError, match="more than 29 values"):
        rng("today-30", "today", "1d", cap=29)


def test_range_values_take_formats_and_offsets():
    c = ph.make_context(NOW, "UTC", None, "", {"day": date(2026, 10, 14), "day_end": date(2026, 10, 15)})
    scope = dict(ranges={"day"})
    t = "s_{day:yyyyMMdd}_{day_end:ddMMyyyy}_{day-1d}_{day:epoch}"
    assert ph.check(t, **scope) == {"day"}
    assert ph.render(t, c) == "s_20261014_15102026_2026-10-13_1791936000"


def test_a_range_name_may_not_hide_another_variable():
    with pytest.raises(ph.PlaceholderError, match="day_end"):
        ph.check_variables([ph.Variable(name="day", type="range", start="today-1", end="today", step="1d"),
                            ph.Variable(name="day_end", values=["x"])])
    with pytest.raises(ph.PlaceholderError, match="twice"):
        ph.check_variables([ph.Variable(name="a", values=["x"]), ph.Variable(name="a", values=["y"])])


def test_combinations_of_lists_and_ranges():
    variables = [ph.Variable(name="branch", values=["MI", "RM"]),
                 ph.Variable(name="day", type="range", start="today-2", end="today", step="1d")]
    c = ph.make_context(NOW, "UTC", None, "", {})
    combos = ph.combinations(variables, c, max_values=1000)
    assert [(x["branch"], x["day"]) for x in combos] == [
        ("MI", date(2026, 10, 15)), ("MI", date(2026, 10, 16)), ("RM", date(2026, 10, 15)), ("RM", date(2026, 10, 16))]
    assert combos[0]["day_end"] == date(2026, 10, 16)
    assert ph.param_text(combos[0]["day"]) == "2026-10-15"
    with pytest.raises(ph.PlaceholderError, match="more than 3"):
        ph.combinations(variables, c, max_values=3)


def test_a_list_variable_needs_values():
    with pytest.raises(ValueError):
        ph.Variable(name="x", values=[])
    with pytest.raises(ValueError):
        ph.Variable(name="x", type="range", start="today", end="", step="1d")


# ── found by the review ───────────────────────────────────────────────────────
def test_a_range_across_the_autumn_change_keeps_every_hour():
    """02:30 CET on 25 October 2026 is the second 02:30 of the night: three hours
    before it are three windows, compared as instants, not as wall-clock times."""
    now = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
    c = ph.make_context(now, "Europe/Rome", None, "", {})
    values = ph.expand_range("h", "now-3h", "now", "1h", c, max_values=100)
    assert [v.astimezone(timezone.utc).hour for v, _ in values] == [22, 23, 0]
    assert values[-1][1].astimezone(timezone.utc) == now


def test_the_legacy_format_of_datetime_is_for_outputs_only():
    c = ph.make_context(NOW, "UTC", None, "", {"datetime": date(2026, 10, 15)})
    assert ph.render("{datetime}", c) == "2026-10-15"


@pytest.mark.parametrize("fmt", ["%n", "%t"])
def test_formats_cannot_put_control_characters_in_a_name(fmt):
    with pytest.raises(ph.PlaceholderError):
        ph.check("a{today:" + fmt + "}.csv", output=True)


def test_the_fixed_part_of_an_absolute_dated_folder_is_the_root():
    from app.ingest.file_templates import static_prefix

    assert static_prefix("/{day}") == "/"
    assert static_prefix("/data{day}") == "/"
    assert static_prefix("/in/{day}") == "/in"


# ── second review ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("day, hours", [("2026-03-29", ["00", "01"] + [f"{h:02}" for h in range(3, 24)]),
                                        ("2026-10-25", ["00", "01", "02", "02"] + [f"{h:02}" for h in range(3, 24)])])
def test_an_hourly_range_from_a_date_starts_at_local_midnight_across_dst(day, hours):
    """A day by hours in Rome: 23 values on the spring change, 25 in autumn, every
    value in the datasource's zone (local HH 00..23), `_param_` with the zone's offset."""
    c = ph.make_context(NOW, "Europe/Rome", None, "", {})
    nxt = (date.fromisoformat(day) + __import__("datetime").timedelta(days=1)).isoformat()
    values = ph.expand_range("h", day, nxt, "1h", c, max_values=100)
    assert [v.strftime("%H") for v, _ in values] == hours
    assert ph.param_text(values[0][0]) == f"{day}T00:00:00+0{1 if day.endswith('29') else 2}:00"
    assert all(str(v.tzinfo) == "Europe/Rome" for v, _ in values)


def test_iso_bounds_without_an_offset_are_in_the_datasource_zone():
    c = ph.make_context(NOW, "Europe/Rome", None, "", {})
    values = ph.expand_range("h", "2026-10-10T06:00", "2026-10-10T08:00", "1h", c, max_values=10)
    assert [ph.param_text(v) for v, _ in values] == ["2026-10-10T06:00:00+02:00", "2026-10-10T07:00:00+02:00"]


def test_every_window_end_has_the_type_of_the_values():
    """A date start and a date-time end (or the reverse) make date-times throughout:
    the last `_end` is never of another type than the others."""
    c = ph.make_context(NOW, "UTC", None, "", {})
    for start, end in (("today-3", "now"), ("now-50h", "today")):
        values = ph.expand_range("d", start, end, "1d", c, max_values=10)
        assert all(isinstance(v, datetime) and isinstance(e, datetime) for v, e in values), (start, end, values)
    dates = ph.expand_range("d", "today-3", "today", "1d", c, max_values=10)
    assert all(type(v) is date and type(e) is date for v, e in dates)


def test_a_line_break_cannot_reach_a_subject():
    with pytest.raises(ph.PlaceholderError, match="line break"):
        out("Report {flow}", kind="text", flow="a\nBcc: x@evil")


def test_names_too_long_are_refused_clearly():
    with pytest.raises(ph.PlaceholderError, match="255"):
        out("x" * 250 + "_{date}")
    with pytest.raises(ph.PlaceholderError, match="255"):
        out("a/" + "y" * 250 + "{date}/b", kind="path")
    assert out("a/{date}/b", kind="path") == "a/2026-10-17/b"
