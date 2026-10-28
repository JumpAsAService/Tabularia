# Report functions

Every function a [report](reports.md) can write in its expressions, family by family:
what it takes, what it gives, what it does with a missing value, and an example on the
sample data with the result it prints. The language itself (fields in brackets, texts
in double quotes, `&`, the operators) is in [Reports: expressions](reports.md#expressions).

![In the designer a KPI is added; its value is written with the completion: SUM is chosen among the functions with its arguments and what it does, then the field margine among the fields; a wrong field ricavoo is underlined and the message under it suggests ricavo; one letter removed, the message goes away; the format 0.0% is written](../media/guide/reports/designer-expression-editor.gif)

*The expression editor: the first letters, or **Ctrl+Space**, list the functions with
their arguments and what they do; `[` lists the dataset's fields with their types; the
line under the field shows the function the cursor is in, its current argument in bold.
A mistake is underlined, and the message says what to write instead.*

## The examples' data

Every example below was rendered on the sample data of a fresh installation
([the «Sample» folder](../getting-started-docker-compose.md#a-the-sample-data-already-there)),
in a report in English, with these two datasets:

- `margins`, **SQL** on the catalog with DuckDB: the first six months of 2025 of the
  datasource «Margin by Category and Month», 30 rows, five categories by six months.
  `LAG` brings each row the revenue of the month before, for the
  [growth](#growth-on-the-month-before):

  ```sql
  SELECT categoria, anno_mese, ricavo, costo, margine, margine_pct, ordini,
         LAG(ricavo) OVER (PARTITION BY categoria ORDER BY anno_mese) AS ricavo_prev
  FROM "Margin by Category and Month"
  WHERE ({category} IS NULL OR categoria = {category})
    AND anno_mese BETWEEN 202501 AND 202506
  ORDER BY categoria, anno_mese
  ```

  with three **calculated fields**:

  | Name | Expression | |
  |---|---|---|
  | `Month` | `DATE(FLOOR([anno_mese] / 100), [anno_mese] - FLOOR([anno_mese] / 100) * 100, 1)` | `202503` becomes the date 1 March 2025 |
  | `MarginPct` | `IF([ricavo] = 0, NULL, [margine] / [ricavo])` | the margin as a fraction, `0.307` |
  | `Growth` | `[ricavo] / [ricavo_prev] - 1` | the change on the month before, NULL in the first month |

- `order`, one order of the datasource `orders`, placed on Saturday 1 July 2023:
  `SELECT numero, data_ordine, canale, stato, deposito FROM orders WHERE numero = 'ORD-12902'`.

The report has two parameters: `category`, a text, optional, its choices the categories
(none chosen unless an example says so), and `since`, a date, `2025-01-01`.
`margine_pct` is a percentage as the sample flow writes it (`30.75`), `MarginPct` a
fraction (`0.3075`).

An expression goes in a **Text** that starts with `=`, or in any expression property (a
column's **Value** and **Total**, a KPI's **Value**…). In the tables, **Gives** is what
the report prints when the expression has no format of its own: a whole number with no
separators, other numbers with separators and up to two decimals, a date as `d MMM yyyy`,
true and false as **Yes** and **No**, NULL as nothing.

| Family | Functions |
|---|---|
| [Aggregates and scope](#aggregates-and-scope) | `SUM` `AVG` `MIN` `MAX` `COUNT` `COUNTDISTINCT` `FIRST` `LAST` `PERCENT` `RUNNING` `ROWNUMBER` |
| [Logic](#logic) | `IF` `IFS` `ISNULL` `COALESCE`, and `AND` `OR` `NOT` |
| [Maths](#maths) | `ROUND` `ABS` `FLOOR` `CEILING` `POWER` |
| [Text](#text) | `UPPER` `LOWER` `TRIM` `LEN` `LEFT` `RIGHT` `MID` `REPLACE` `CONTAINS` `STARTSWITH` `TEXT` `CONCAT` |
| [Dates](#dates) | `DATE` `YEAR` `MONTH` `DAY` `WEEKDAY` `DATEADD` `DATEDIFF` `TODAY` `NOW` |
| [Report and page](#report-and-page) | `PARAM` `REPORT` `PAGE` `PAGES` |
| [Formats](#formats) | `FORMAT` and its patterns |

Names are written in capitals or not: `sum([ricavo])` is `SUM([ricavo])`. Arguments in
brackets in the signatures are optional. [Recipes](#recipes) put them together for a
real report, and [When an expression is wrong](#when-an-expression-is-wrong) lists what
the checks say.

## Aggregates and scope

An aggregate works on a set of rows, and which rows depends on where it is written:

| Written in | Works on |
|---|---|
| a **Text**, a **KPI**, a **Chart**'s series in the body | the rows of the item's dataset (a chart: those of each category along its axis) |
| a column's **Total**, in a group's total row | the rows of that group |
| a column's **Total**, in the grand total | all the table's rows (after **Rows where**) |
| a group's **Label** | the rows of that group |
| a matrix's **Value of the cells** | the rows that meet in the cell |
| anywhere, with a dataset named last: `SUM([ricavo], "margins")` | the whole dataset; the only way in the page header and footer |

An aggregate cannot be written where the value of each row is wanted: a group's **Group
by**, the **Order of the rows**, a matrix's **One column per value of**, a chart's axis
and **One series per value of**, a sparkline's **Along**, the choices of a parameter.
An aggregate takes no other inside it: `SUM([margine]) / SUM([ricavo])`, not
`SUM(SUM(...))`. A field written where there is no current row (a group's total row, a
text) is the value of the first of its rows: in a text on `margins`, `[categoria]` gives
`Insaccati cotti`.

All aggregates leave NULLs out. On no rows, or only NULLs, `COUNT` gives 0 and the
others NULL.

### SUM

`SUM(value, [dataset])`: the sum of a number over the rows. A text is refused when the
report is checked: «SUM adds numbers: a field of type text is here».

| Expression | Gives |
|---|---|
| `SUM([ricavo])` | `2,921,909.2` |
| `FORMAT(SUM([ricavo]), "€ #,##0")` | `€ 2,921,909` |
| `FORMAT(SUM([ricavo], "margins"), "#,##0")` in the page header | `2,921,909` |
| `FORMAT(SUM(IF([categoria] = "Salami", [ricavo], 0)), "#,##0")` | `883,922`: a sum with a condition |

### AVG

`AVG(value, [dataset])`: the average of a number; NULLs are left out of the count as well.

| Expression | Gives |
|---|---|
| `AVG([margine_pct])` | `26.62` |
| `ROUND(AVG([margine_pct]), 1)` | `26.6` |
| `AVG([ordini])` as the **Total** of a column, format `#,##0.0` | `422.2` in the total of *Insaccati cotti* |

The average of the percentages is not the margin percentage of the whole, which is
`SUM([margine]) / SUM([ricavo])`, `24.5%` ([margin percentage](#margin-percentage)).

### MIN, MAX

`MIN(value, [dataset])`, `MAX(value, [dataset])`: the smallest and the largest value.
They compare numbers, texts or dates, one type per column.

| Expression | Gives |
|---|---|
| `MIN([ricavo])` | `64.72` (Insaccati da cuocere, June 2025) |
| `MAX([ricavo])` | `215,195.65` (Prosciutti, April 2025) |
| `FORMAT(MIN([Month]), "MMMM yyyy")` | `January 2025` |
| `FORMAT(MAX([Month]), "MMMM yyyy")` | `June 2025` |
| `FORMAT(MAX([margine_pct]) / 100, "0.0%")` | `34.2%` |

### COUNT

`COUNT([value], [dataset])`: with no argument, how many rows; with a value, how many
rows where it is not NULL. `COUNT("margins")` counts the rows of a named dataset.

| Expression | Gives |
|---|---|
| `COUNT()` | `30` |
| `COUNT([ricavo_prev])` | `25`: January has no month before in the dataset |
| `COUNT(IF([margine_pct] < 22, 1))` | `6`, the rows under 22%: `IF` with no third argument is NULL elsewhere |
| `COUNT("margins")` in the page header | `30` |
| `[categoria] & " · " & COUNT() & " months"` as a group's **Label** | `Insaccati cotti · 6 months` |

### COUNTDISTINCT

`COUNTDISTINCT(value, [dataset])`: how many different values, NULL not counted.

| Expression | Gives |
|---|---|
| `COUNTDISTINCT([categoria])` | `5` |
| the same with **Category** `Prosciutti` | `1` |

### FIRST, LAST

`FIRST(value, [dataset])`, `LAST(value, [dataset])`: the value of the first and of the
last row that has one, in the order of the rows: the dataset's `ORDER BY`, or in a table
its **Order of the rows**.

| Expression | Gives |
|---|---|
| `FIRST([categoria])` | `Insaccati cotti` |
| `LAST([categoria])` | `Salumi stagionati` |
| `FIRST([Month])` as a column's **Total**, format `MMM`, rows sorted by `[anno_mese]` | `Jan` in every group's total |

### PERCENT

`PERCENT(aggregate)`: the aggregate as a share of the same aggregate one level up. In a
row of a table, the row's share of its group (of the whole table when it has no groups);
in a group's total row, the group's share of the whole table; in the grand total, 100%.
It takes an aggregate: `PERCENT([ricavo])` is refused, «PERCENT takes an aggregate, as
PERCENT(SUM([amount]))». NULL when the level above adds up to zero.

On `margins`, a table grouped by `[categoria]`, sorted by `[anno_mese]`, with
`PERCENT(SUM([ricavo]))` as a column's **Value** and **Total**, format `0.0%`:

| Row | Revenue | `PERCENT(SUM([ricavo]))` |
|---|---|---|
| Insaccati cotti, January 2025 | 66,648 | `13.6%` of the category's six months |
| Insaccati cotti, March 2025 | 90,211 | `18.4%` |
| Total Insaccati cotti | 491,426 | `16.8%` of the whole |
| Total Prosciutti | 863,136 | `29.5%` |
| Total Salami | 883,922 | `30.3%` |
| Grand total | 2,921,909 | `100.0%` |

### RUNNING

`RUNNING(aggregate)`: the aggregate from the first row of the group to the current one,
in the order the rows are shown: a running total that starts again with every group, or
runs over the whole table when it has none. It takes an aggregate of the group,
`RUNNING(SUM([ricavo]))`; one of a named dataset is refused. When a reader sorts the
table by another column in the viewer, the running total follows the new order.

| Month (Insaccati cotti) | Revenue | `RUNNING(SUM([ricavo]))` |
|---|---|---|
| January 2025 | 66,648 | `66,648` |
| February 2025 | 64,693 | `131,341` |
| March 2025 | 90,211 | `221,552` |
| June 2025 | 86,809 | `491,426`, the group's total |

### ROWNUMBER

`ROWNUMBER()`: the number of the row within its group, from 1. In the first group of the
table above it goes from 1 to 6, and starts from 1 again in *Insaccati da cuocere*. The
page header and footer have no rows: there it is refused.

## Logic

A comparison (`=` `<>` `<` `>` `<=` `>=`) gives true, false, or NULL when either side is
NULL. Values of different types are not compared: `[categoria] = 2025` is refused,
«Compares a text with a number: they are of different types». `AND`, `OR` and `NOT` join
conditions, one comparison at a time. Wherever a condition decides (`IF`, **Rows
where**, a rule, **Shown when**), NULL counts as false.

| Expression | Gives |
|---|---|
| `3 > 1 AND NOT (2 > 5)` | `Yes` |
| `IF(NULL > 1, "true", "false or NULL")` | `false or NULL` |
| `[ricavo] > 1 AND [ricavo] < 9` | a range, two comparisons joined; `[ricavo] > 1 < 9` is refused |

### IF

`IF(condition, value if true, [value if false])`: one value or the other; without the
third argument, NULL when the condition is not true. The first argument must be a
condition: `IF([ricavo], 1, 0)` is refused.

| Expression | Gives |
|---|---|
| `IF(SUM([margine]) / SUM([ricavo]) < 0.25, "Below target", "On target")` | `Below target` (24.5%) |
| `IF([MarginPct] < 0.22, "Below", "OK")` in a column | `Below` in the six months of Prosciutti (20.1% to 21.9%), `OK` in the others |
| `IF(1 > 2, "yes")` | nothing (NULL) |
| `IF(ISNULL([Growth]), "first month", FORMAT([Growth], "+0.0%;-0.0%"))` in a column | `first month` in January, `-2.9%` in February for Insaccati cotti |

### IFS

`IFS(condition1, value1, condition2, value2, …, [else])`: the value of the first true
condition. With an odd number of arguments the last is the value when none is true;
otherwise that is NULL. Thresholds read from the highest down:

| Expression | Gives |
|---|---|
| `IFS(AVG([margine_pct]) >= 30, "High", AVG([margine_pct]) >= 22, "Medium", "Low")` | `Medium` (26.62) |
| `IFS([margine_pct] >= 30, "High", [margine_pct] >= 22, "Medium", "Low")` in a column | `High` for Insaccati cotti, `Medium` for Salami, `Low` for Prosciutti |

### ISNULL

`ISNULL(value)`: true when the value is missing.

| Expression | Gives |
|---|---|
| `ISNULL(PARAM("category"))` | `Yes` with no category chosen, `No` with `Prosciutti` |
| `ISNULL([ricavo_prev])` | `Yes` in January |
| `ISNULL(10 / 0)` | `Yes`: a division by zero is NULL, never an error |

### COALESCE

`COALESCE(value1, value2, …)`: the first value that is not missing; NULL when none is.

| Expression | Gives |
|---|---|
| `COALESCE(PARAM("category"), "All categories")` | `All categories`, or `Prosciutti` when it is chosen |
| `COALESCE([ricavo_prev], 0)` | `0` in January |

## Maths

`+` `-` `*` `/` work on numbers. NULL on either side gives NULL (`ISNULL(NULL + 1)` is
`Yes`), and a division by zero gives NULL. A text is refused: `"Total: " + [ricavo]`
says ««+» works on numbers, and this is a text: join texts with &». There is no `^` and
no `%`: write `POWER(x, y)`, and a format such as `0%`.

### ROUND

`ROUND(number, [decimals])`: rounded half away from zero, to 0 decimals by default;
negative decimals round to tens, hundreds…

| Expression | Gives |
|---|---|
| `ROUND(2.5)` | `3` |
| `ROUND(-2.5)` | `-3` |
| `ROUND(1234.5678, 2)` | `1,234.57` |
| `ROUND(1234.5678, -2)` | `1200` |
| `ROUND(SUM([margine]) / SUM([ricavo]) * 100, 1)` | `24.5` |

Round to compute; to print, a format is enough: `0.0%` writes `24.5%` from `0.2453`.

### ABS, FLOOR, CEILING

`ABS(number)`: the number without its sign. `FLOOR(number)`: the whole number at or
below it. `CEILING(number)`: the whole number at or above it.

| Expression | Gives |
|---|---|
| `ABS(-1500)` | `1500` |
| `FLOOR(202503 / 100)` | `2025`: the year of a month code |
| `FLOOR(-2.5)` | `-3` |
| `CEILING(5 / 3)` | `2` |
| `"Q" & CEILING(MONTH([Month]) / 3) & " " & YEAR([Month])` | `Q1 2025` |

### POWER

`POWER(number, exponent)`: the number raised to the exponent; NULL when the result is not
a real number.

| Expression | Gives |
|---|---|
| `POWER(1.05, 3)` | `1.16` (1.157625, printed with two decimals) |
| `POWER(9, 0.5)` | `3` |
| `ISNULL(POWER(-8, 0.5))` | `Yes` |

## Text

`&` joins values into a text, each written as the report writes it, NULL as nothing:

| Expression | Gives |
|---|---|
| `"Total " & "Salami"` | `Total Salami` |
| `"Revenue: " & 1234567.891` | `Revenue: 1,234,567.89` |
| `"[" & NULL & "]"` | `[]` |

The text functions take a text: a number goes through `TEXT()` first (`LEFT(202503, 4)`
is refused, `LEFT(TEXT(202503), 4)` is `2025`). A NULL argument gives NULL. Texts are
compared as written: capitals count.

| Function | Gives | Example | Gives |
|---|---|---|---|
| `UPPER(text)` | the text in capitals | `UPPER("Prosciutti")` | `PROSCIUTTI` |
| `LOWER(text)` | the text in lower case | `LOWER("Salumi STAGIONATI")` | `salumi stagionati` |
| `TRIM(text)` | the text without spaces at its ends | `"[" & TRIM("  Salami  ") & "]"` | `[Salami]` |
| `LEN(text)` | how many characters | `LEN("Prosciutti")` | `10` |
| `LEFT(text, count)` | the first characters | `LEFT("Insaccati cotti", 9)` | `Insaccati` |
| `RIGHT(text, count)` | the last characters | `RIGHT(TEXT(202503), 2)` | `03` |
| `MID(text, start, count)` | `count` characters from `start`, 1 the first | `MID("ORD-12902", 5, 5)` | `12902` |
| `REPLACE(text, old, new)` | every `old` replaced by `new` | `REPLACE("Insaccati cotti", "Insaccati", "Sausages,")` | `Sausages, cotti` |
| `CONTAINS(text, part)` | true when the text contains the part | `CONTAINS("Insaccati da cuocere", "cuocere")` | `Yes` |
| | | `CONTAINS("Prosciutti", "PROSC")` | `No` |
| `STARTSWITH(text, start)` | true when the text starts with it | `STARTSWITH("Insaccati cotti", "Insaccati")` | `Yes` |

### TEXT

`TEXT(value)`: the value as text, as the report prints it without a format, in the
report's language; NULL gives an empty text.

| Expression | In English | In Italian |
|---|---|---|
| `TEXT(1234.5)` | `1,234.5` | `1.234,5` |
| `TEXT(202503)` | `202503` | `202503` |
| `TEXT(DATE(2026, 3, 5))` | `5 Mar 2026` | `05/03/2026` |
| `TEXT(2 > 1)` | `Yes` | `Sì` |

### CONCAT

`CONCAT(value1, value2, …)`: the values joined, as `&` joins them:
`CONCAT("Q", CEILING(5 / 3), " ", 2025)` gives `Q2 2025`.

## Dates

A date plus or minus a number moves it by days; a date minus a date is the days between
them. `*`, `/` and a date plus a date are refused («use DATEADD and DATEDIFF»). A field
of type number is not a date: `YEAR([anno_mese])` is refused; a month code becomes a date
with `DATE`, as the calculated field `Month` above does.

| Expression | Gives |
|---|---|
| `DATE(2025, 3, 1) + 30` | `31 Mar 2025` |
| `DATE(2025, 3, 1) - DATE(2025, 1, 1)` | `59` |
| `{start_of_month}` | the first day of this month: a date [placeholder](../placeholders.md), in the report's time zone |

### DATE

`DATE(year, month, day)`: a date; NULL when it does not exist.

| Expression | Gives |
|---|---|
| `DATE(2025, 3, 1)` | `1 Mar 2025` |
| `ISNULL(DATE(2025, 2, 30))` | `Yes` |
| `DATE(FLOOR(202503 / 100), 202503 - FLOOR(202503 / 100) * 100, 1)` | `1 Mar 2025` |

### YEAR, MONTH, DAY, WEEKDAY

`YEAR(date)`, `MONTH(date)` from 1 to 12, `DAY(date)` of the month, `WEEKDAY(date)` from
1 (Monday) to 7 (Sunday). On the dataset `order`, placed on Saturday 1 July 2023:

| Expression | Gives |
|---|---|
| `YEAR([data_ordine])` | `2023` |
| `MONTH([data_ordine])` | `7` |
| `DAY([data_ordine])` | `1` |
| `WEEKDAY([data_ordine])` | `6` |
| `WEEKDAY([data_ordine]) >= 6` | `Yes`: a weekend |
| `[numero] & " · " & FORMAT([data_ordine], "EEEE d MMMM yyyy")` | `ORD-12902 · Saturday 1 July 2023` |

### DATEADD

`DATEADD(unit, count, date)`: the date moved by `count` units, `"d"` days, `"w"` weeks,
`"m"` months or `"y"` years; a negative count goes back. A month or a year that lands on
a day its month does not have ends on the month's last day. NULL when the count or the
date is.

| Expression | Gives |
|---|---|
| `DATEADD("m", 1, DATE(2025, 1, 31))` | `28 Feb 2025` |
| `DATEADD("y", 1, DATE(2024, 2, 29))` | `28 Feb 2025` |
| `DATEADD("w", 2, DATE(2025, 3, 1))` | `15 Mar 2025` |
| `DATEADD("d", -1, DATE(2025, 3, 1))` | `28 Feb 2025` |
| `DATEADD("d", -1, DATEADD("m", 1, DATE(2025, 2, 1)))` | `28 Feb 2025`: the last day of a month |
| `"vs " & FORMAT(DATEADD("m", -1, DATE(2025, 3, 1)), "MMMM yyyy")` | `vs February 2025` |
| `FORMAT(DATEADD("m", -1, [Month]), "MMM yy")` in a column | `Dec 24` in the January row |
| `DATEADD("month", 1, [Month])` | refused: the unit is `"d"`, `"w"`, `"m"` or `"y"` |

### DATEDIFF

`DATEDIFF(unit, start, end)`: how many whole units from start to end, negative when the
end comes first. A month counts once the same day of the month is reached.

| Expression | Gives |
|---|---|
| `DATEDIFF("d", DATE(2025, 1, 1), DATE(2025, 3, 1))` | `59` |
| `DATEDIFF("w", DATE(2025, 1, 1), DATE(2025, 3, 1))` | `8` |
| `DATEDIFF("m", DATE(2025, 1, 31), DATE(2025, 3, 30))` | `1` |
| `DATEDIFF("m", DATE(2025, 1, 31), DATE(2025, 3, 31))` | `2` |
| `DATEDIFF("y", DATE(2023, 7, 1), DATE(2026, 6, 30))` | `2` |
| `DATEDIFF("d", DATE(2025, 3, 1), DATE(2025, 1, 1))` | `-59` |
| `DATEDIFF("m", [data_ordine], DATE(2025, 12, 31))` | `29`, on the order of 1 July 2023 |

### TODAY, NOW

`TODAY()`: today's date; `NOW()`: the date and time of the render; both in the report's
**time zone** (Page setup). Rendered on 28 October 2026 at 22:03, `Europe/Rome`:

| Expression | Gives |
|---|---|
| `FORMAT(TODAY(), "yyyy-MM-dd")` | `2026-10-28` |
| `"Printed " & FORMAT(NOW(), "d MMM yyyy HH:mm")` | `Printed 28 Oct 2026 22:03` |
| `DATEDIFF("d", {today}, TODAY())` | `0`: `{today}` is the same day |
| `DATEADD("d", -1, {start_of_month})` | `30 Sep 2026`, the last day of the month before |

## Report and page

### PARAM

`PARAM("name")`: the value of a parameter, its name in quotes; NULL when an optional one
has no value. A parameter with **Several values** gives a list, and `=` and `<>` compare
a value with each of them. In an expression `{category}` is refused: that is how the SQL
writes a parameter.

| Expression | Gives |
|---|---|
| `"Monthly margin" & IF(ISNULL(PARAM("category")), "", " — " & PARAM("category"))` | `Monthly margin`, or `Monthly margin — Prosciutti` |
| `"From " & FORMAT(PARAM("since"), "MMMM yyyy")` | `From January 2025` |
| `PARAM("categry")` | refused: «There is no parameter «categry»: did you mean "category"?» |

### REPORT

`REPORT()`: the report's name as it is now, so a rename changes it on every page:
`UPPER(REPORT())` on «Monthly margin» gives `MONTHLY MARGIN`.

### PAGE, PAGES

`PAGE()` and `PAGES()`: the number of the page and how many pages there are. They are
known only when the pages are laid out, so they go only in a **Text**, joined with `&`:
`="Page " & PAGE() & " of " & PAGES()` prints `Page 1 of 4` on the first of four pages.
In a column, a KPI or an aggregate they are refused, and so is arithmetic on them
(`PAGE() + 1`).

## Formats

### FORMAT

`FORMAT(value, pattern)`: a number or a date as text. NULL gives an empty text; a value
the pattern does not fit (a text, a date with a number's pattern) is written as with no
format. The pattern is checked with the report: `"[Red]#,##0"` and `"abc"` are refused,
saying why. The same patterns go in the **Format** of a text, a column, a chart or a KPI,
which keeps the value a number (in Excel too); `FORMAT()` is for a value inside a text.

| Expression | English | Italian | German | Spanish | French |
|---|---|---|---|---|---|
| `FORMAT(202401, "0")` | 202401 | 202401 | 202401 | 202401 | 202401 |
| `FORMAT(10478825.4, "#,##0")` | 10,478,825 | 10.478.825 | 10.478.825 | 10.478.825 | 10 478 825 |
| `FORMAT(1234.5, "#,##0.00")` | 1,234.50 | 1.234,50 | 1.234,50 | 1234,50 | 1 234,50 |
| `FORMAT(0.2453, "0.0%")` | 24.5% | 24,5% | 24,5% | 24,5% | 24,5% |
| `FORMAT(2569181, "€ #,##0")` | € 2,569,181 | € 2.569.181 | € 2.569.181 | € 2.569.181 | € 2 569 181 |
| `FORMAT(1200, "#,##0 pcs")` | 1,200 pcs | 1.200 pcs | 1.200 pcs | 1200 pcs | 1 200 pcs |
| `FORMAT(-1500, "#,##0;(#,##0)")` | (1,500) | (1.500) | (1.500) | (1500) | (1 500) |
| `FORMAT(0, "#,##0;-#,##0;-")` | - | - | - | - | - |
| `FORMAT(0.0512, "+0.0%;-0.0%;0%")` | +5.1% | +5,1% | +5,1% | +5,1% | +5,1% |
| `FORMAT(7, "000")` | 007 | 007 | 007 | 007 | 007 |
| `FORMAT(DATE(2026, 3, 5), "d MMM yyyy")` | 5 Mar 2026 | 5 mar 2026 | 5 März 2026 | 5 mar 2026 | 5 mars 2026 |
| `FORMAT(DATE(2026, 3, 5), "MMMM yyyy")` | March 2026 | marzo 2026 | März 2026 | marzo 2026 | mars 2026 |
| `FORMAT(DATE(2026, 3, 5), "EEEE d MMMM")` | Thursday 5 March | giovedì 5 marzo | Donnerstag 5 März | jueves 5 marzo | jeudi 5 mars |
| `FORMAT(DATE(2026, 3, 5), "EEE d")` | Thu 5 | gio 5 | Do. 5 | jue 5 | jeu. 5 |
| `FORMAT(DATE(2026, 3, 5), "MMM yy")` | Mar 26 | mar 26 | März 26 | mar 26 | mars 26 |
| `FORMAT(DATE(2026, 3, 5), "yyyy-MM")` | 2026-03 | 2026-03 | 2026-03 | 2026-03 | 2026-03 |
| `FORMAT(DATE(2026, 3, 5), "'Week of' d MMM")` | Week of 5 Mar | Week of 5 mar | Week of 5 März | Week of 5 mar | Week of 5 mars |

The language is the report's (**Page setup**), not the reader's. Rounding is half away
from zero, on the decimal value as written: `FORMAT(1.005, "0.00")` gives `1.01`, and
`FORMAT(-0.004, "0.00")` gives `0.00`, without a minus. Every symbol of the patterns is
in [Reports: formats](reports.md#formats).

## Recipes

### Margin percentage

As a total, the ratio of the sums, never the sum or the average of the ratios:

```text
SUM([margine]) / SUM([ricavo])        format 0.0%
```

`24.5%` over the six months, `21.2%` in the total of *Prosciutti*. Row by row, the
calculated field `MarginPct`: `[margine] / [ricavo]` alone would also give NULL on a
month with no revenue, since a division by zero is NULL; the `IF` says it on purpose.

### Growth on the month before

The language works one row at a time and has no «previous row»: bring the month before
into the row in the SQL, with `LAG`, and divide in a calculated field:

```text
Growth    [ricavo] / [ricavo_prev] - 1          format +0.0%;-0.0%;0.0%
```

In the dataset above `Growth` is NULL in January, `-2.9%` in February and `+39.4%` in
March for Insaccati cotti; the NULL prints as nothing, with no error. `LAG` runs after
the `WHERE`, so the first month of the range has no month before. Computed before the
filter, in a subquery, January has its growth too (`-44.1%` on December 2024):

```sql
SELECT * FROM (
  SELECT categoria, anno_mese, ricavo, costo, margine,
         LAG(ricavo) OVER (PARTITION BY categoria ORDER BY anno_mese) AS ricavo_prev
  FROM "Margin by Category and Month") AS m
WHERE anno_mese >= 202501
ORDER BY categoria, anno_mese
```

For one number against another period, a KPI's **Compared with** needs no SQL: the card
shows the change in percent with its arrow.

### Labels with FORMAT

A text that mixes words and numbers formats each number:

```text
="Revenue " & FORMAT(SUM([ricavo]), "€ #,##0") & " · margin " & FORMAT(SUM([margine]) / SUM([ricavo]), "0.0%")
```

gives `Revenue € 2,921,909 · margin 24.5%`. A number joined with `&` and no `FORMAT`
keeps up to two decimals (`Revenue: 1,234,567.89`).

### The share of the category

A column with `PERCENT(SUM([ricavo]))` as **Value** and **Total**, format `0.0%`, in a
table grouped by `[categoria]`: each month's share of its category, and in the total row
the category's share of the whole ([PERCENT](#percent)).

### A running total

A column `RUNNING(SUM([ricavo]))` in a table sorted by `[anno_mese]` and grouped by
category: the revenue from the first month of the category to this one
([RUNNING](#running)). With no group, it runs over the whole table.

### Thresholds

A **Rule** colours cells: on the column `Margin %`, when `[MarginPct] < 0.22`, red and
bold. A column with a word: `IFS([MarginPct] >= 0.3, "High", [MarginPct] >= 0.22,
"On target", "Below target")`. A text that appears only when it matters: on the dataset
`margins`, **Shown when** `SUM([margine]) / SUM([ricavo]) < 0.25`; at 24.5% it is shown.

### Dates in titles

```text
="Monthly margin" & IF(ISNULL(PARAM("category")), "", " — " & PARAM("category"))
="From " & FORMAT(PARAM("since"), "MMMM yyyy") & " to " & FORMAT(DATEADD("d", -1, {start_of_month}), "d MMMM yyyy")
```

The first gives `Monthly margin — Prosciutti` when a category is chosen; the second, on
28 October 2026, `From January 2025 to 30 September 2026`.

## When an expression is wrong

The editor checks while you type and underlines the part that is wrong; the server
checks the whole report against the datasets' fields (**Refresh fields**) after a pause
and at every save, which lists what it refuses with the character where the mistake is.
Some of the messages:

| Written | The message |
|---|---|
| `SUM([ricavo]) / SUM([ricavoo])` | There is no field [ricavoo] of the dataset «margins»: did you mean [ricavo]? |
| `ricavo * 2` | Unknown name «ricavo»: fields are written in brackets, [ricavo], and functions with their parentheses, RICAVO(…) |
| `ROUNDD([ricavo], 2)` | Unknown function ROUNDD: did you mean ROUND? |
| `[ricavo] != 0` | «!=» is not part of the language: write <> for «not equal» |
| `'Salami'` | «'» is not part of the language: texts go in double quotes: "text" |
| `[ricavo] ^ 2` | «^» is not part of the language: use POWER(x, y) |
| `"Total: " + [ricavo]` | «+» works on numbers, and this is a text: join texts with & |
| `SUM([categoria])` | SUM adds numbers: a field of type text is here |
| `SUM(SUM([ricavo]))` | SUM inside another aggregate: write one at a time |
| `RUNNING([ricavo])` | RUNNING takes an aggregate of the group, as RUNNING(SUM([amount])) |
| `MID("abc", 1)` | MID takes 3 arguments, 2 given: MID(text, start, count) |
| `IF([ricavo] > 0, "yes"` | IF( is not closed: add «)» after its arguments, separated by commas |
| `SUM([ricavo], "margin")` | There is no dataset «margin»: did you mean "margins"? |
| `{category}` | {category} is a parameter: in an expression write PARAM("category") |
| `FORMAT([ricavo], "[Red]#,##0")` | «[Red]» is not part of a format: a colour goes in a column's rule (Excel writes negatives in red by itself), and text in double quotes |
| `SUM([ricavo])` in the page header | SUM in the page header or footer names its dataset: SUM([x], "orders") |
| `[ricavo]` as **Rows where** | A condition is wanted here (true or false), as [amount] > 0: this is a number |
| `SUM([ricavo])` as **Group by** | SUM: an aggregate cannot be written here (the value of each row is wanted) |

## Related

- [Pixel-perfect reports](reports.md): the designer, datasets and parameters, tables,
  charts, the viewer and the Reports tab
- [Date placeholders](../placeholders.md): `{today}`, `{start_of_month-1m}` and the others
- [SQL scripts](../sql-scripts.md): the SQL a dataset can read
