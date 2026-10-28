"""Numbers and dates as a report writes them, in its five languages (app/reports/formats.py),
and the same patterns as Excel number formats."""
from datetime import date, datetime

import pytest

from app.reports import formats as f

NBSP = " "


@pytest.mark.parametrize("code, pattern, value, text", [
    ("en", "#,##0.00", 1234567.891, "1,234,567.89"),
    ("it", "#,##0.00", 1234567.891, "1.234.567,89"),
    ("de", "#,##0.00", 1234567.891, "1.234.567,89"),
    ("fr", "#,##0.00", 1234567.891, f"1{NBSP}234{NBSP}567,89"),
    ("es", "#,##0.00", 1234567.891, "1.234.567,89"),
    ("es", "#,##0", 1234, "1234"),  # Spanish groups from five digits
    ("es", "#,##0", 12345, "12.345"),
    ("en", "0", 2.5, "3"),  # half away from zero
    ("en", "0", -2.5, "-3"),
    ("en", "0.00", 2.675, "2.68"),  # the decimal written, not the binary float below it
    ("en", "0.##", 2.5, "2.5"),
    ("en", "0.##", 2.0, "2"),
    ("en", "#.00", 0.5, ".50"),
    ("it", "0%", 0.256, "26%"),
    ("it", "0.0%", 0.256, "25,6%"),
    ("it", "€ #,##0.00", 1234.5, "€ 1.234,50"),
    ("it", "€ #,##0.00", -1234.5, "-€ 1.234,50"),
    ("en", "#,##0 pcs", 1500, "1,500 pcs"),
    ("en", '#,##0" kg"', 1500, "1,500 kg"),
    ("en", "#,##0;(#,##0)", -1500, "(1,500)"),
    ("en", "#,##0;(#,##0);-", 0, "-"),
    ("en", "0.00", -0.001, "0.00"),  # no «-0.00»
    ("en", "#,##0", float("nan"), ""),
])
def test_numbers(code, pattern, value, text):
    assert f.format_number(value, pattern, f.locale(code)) == text


@pytest.mark.parametrize("code, pattern, text", [
    ("en", "yyyy-MM-dd", "2026-03-05"),
    ("it", "dd/MM/yyyy", "05/03/2026"),
    ("en", "d MMM yyyy", "5 Mar 2026"),
    ("it", "d MMMM yyyy", "5 marzo 2026"),
    ("de", "d. MMMM yyyy", "5. März 2026"),
    ("es", "d 'de' MMMM 'de' yyyy", "5 de marzo de 2026"),
    ("fr", "EEEE d MMMM yyyy", "jeudi 5 mars 2026"),
    ("it", "MMMM yyyy", "marzo 2026"),
    ("de", "EEE dd.MM.yy", "Do. 05.03.26"),
    ("en", "HH:mm", "14:07"),
    ("en", "d/M/yyyy H:mm:ss", "5/3/2026 14:07:09"),
    ("en", "'Q'''yy", "Q'26"),
])
def test_dates(code, pattern, text):
    assert f.format_date(datetime(2026, 3, 5, 14, 7, 9), pattern, f.locale(code)) == text


def test_a_date_has_no_time():
    assert f.format_date(date(2026, 3, 5), "dd/MM/yyyy HH:mm", f.locale("it")) == "05/03/2026 00:00"


@pytest.mark.parametrize("code, value, text", [
    ("en", True, "Yes"), ("it", False, "No"), ("de", True, "Ja"), ("fr", True, "Oui"), ("es", True, "Sí"),
    # a whole number as it is, as a spreadsheet's General: a year, a month code, an id
    # (2026 written «2,026» was a classic); a decimal one grouped
    ("it", 1234, "1234"), ("en", 2026, "2026"), ("en", -202401, "-202401"), ("it", 1234.567, "1.234,57"), ("en", None, ""), ("en", ["a", "b"], "a, b"),
    ("it", date(2026, 3, 5), "05/03/2026"), ("de", date(2026, 3, 5), "05.03.2026"),
    ("en", date(2026, 3, 5), "5 Mar 2026"), ("fr", datetime(2026, 3, 5, 9, 0), "05/03/2026 09:00"),
])
def test_a_value_without_a_format_is_written_the_locale_s_way(code, value, text):
    assert f.display(value, None, f.locale(code)) == text


def test_a_pattern_that_does_not_fit_the_value_is_set_aside():
    loc = f.locale("en")
    assert f.display("North", "#,##0.00", loc) == "North"
    assert f.display(1234.5, "dd/MM/yyyy", loc) == "1,234.5"
    assert f.display(date(2026, 3, 5), "#,##0", loc) == "5 Mar 2026"


def test_texts_the_renderer_writes():
    assert f.locale("it").texts["page"].format(page=1, pages=3) == "Pagina 1 di 3"
    assert f.locale("de").texts["total"] == "Summe"
    assert f.locale("xx").code == "en"


def test_bad_patterns_are_named():
    with pytest.raises(f.FormatError, match="no digits"):
        f.number_pattern("abc")
    with pytest.raises(f.FormatError, match="three sections"):
        f.number_pattern("0;0;0;0")
    with pytest.raises(f.FormatError, match="not a format"):
        f.check_pattern("hello")
    f.check_pattern("dd/MM/yyyy")
    f.check_pattern("€ #,##0.00")


@pytest.mark.parametrize("pattern, excel", [
    ("#,##0.00", '#,##0.00;[Red]-#,##0.00'),
    ("€ #,##0.00", '"€ "#,##0.00;[Red]-"€ "#,##0.00'),
    ("#,##0 pcs", '#,##0" pcs";[Red]-#,##0" pcs"'),
    ("0.0%", "0.0%;[Red]-0.0%"),
    ("#,##0;(#,##0)", "#,##0;\"(\"#,##0\")\""),
    ("#,##0;(#,##0);-", "#,##0;\"(\"#,##0\")\";\"-\""),
    (None, "#,##0.##;[Red]-#,##0.##"),
])
def test_excel_number_formats(pattern, excel):
    assert f.excel_number_format(pattern) == excel


def test_excel_date_formats():
    loc = f.locale("it")
    assert f.excel_date_format("dd/MM/yyyy HH:mm", loc, True) == "dd/mm/yyyy hh:mm"
    assert f.excel_date_format("d MMMM yyyy", loc, False) == "d mmmm yyyy"
    assert f.excel_date_format("d 'de' MMMM", loc, False) == 'd "de" mmmm'
    assert f.excel_date_format(None, loc, False) == "dd/mm/yyyy"
