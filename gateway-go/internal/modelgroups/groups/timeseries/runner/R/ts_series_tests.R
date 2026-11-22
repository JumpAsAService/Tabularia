# The self-test of the series (ts_series.R, lot D1): added to TAB_COMMON_TESTS. The witnesses are
# reference/data/series_witness.json (make_witness_series.py: pandas, SciPy), the tables make_reference_series.R's.

.ts_witness <- function(code) tab_ref_json(file.path(code, "reference", "data", "series_witness.json"), simplifyVector = FALSE)

.ts_near <- function(got, want, rel = 1e-12, abs = 1e-15) {
  got <- as.numeric(got)
  want <- as.numeric(unlist(want))
  length(got) == length(want) && all(abs(got - want) <= pmax(abs, rel * abs(want)))
}

.ts_task <- function(df, params = list()) {
  list(protocol = TAB_PROTOCOL, mode = "fit", group = "timeseries", group_version = "selftest", model = "decomposition",
       variant = "stl", params = params, output = "results", write_output = FALSE, rows_from = "run",
       limits = tab_catalog()$limits, .frame = df)
}

.ts_gaps_frame <- function(code) tab_read_csv(file.path(code, "reference", "data", "dec_gaps.csv"), list(month = "date", boxes = "number"))

TAB_COMMON_TESTS[["timeseries: the series' gaps are refused with a sentence, or filled linearly or seasonally and flagged (dec_gaps)"]] <- function(code) {
  w <- .ts_witness(code)$gaps
  task <- .ts_task(.ts_gaps_frame(code))
  series <- function(missing, transform = "none", lambda = "auto") ts_series(task, "month", "boxes", "month", missing = missing,
                                                                              transform = transform, box_cox_lambda = lambda, period = 12L)
  msg <- .tab_refused_with("gaps", series("refuse"))
  .tab_expect(identical(msg, paste0("the series has 3 periods without a value inside its span (the first: 2016-05-01): fill them in the flow ",
                                    "(a calendar Left join and Fill nulls with 0 when an empty month means none), or choose missing = linear or ",
                                    "seasonal")), msg)
  gaps <- unlist(w$gaps)
  s <- series("linear")
  .tab_expect(s$n == w$n && s$n_missing == w$n_missing && s$n_filled == 3L && identical(which(s$filled), as.integer(gaps)) &&
              identical(format(s$times[[1L]]), w$first) && all(is.na(s$y[gaps])) && identical(s$z[-gaps], s$y[-gaps]) && is.null(s$lambda),
              "the counts, the flags, the span: n ", s$n, ", n_missing ", s$n_missing, ", filled ", paste(which(s$filled), collapse = " "))
  .tab_expect(.ts_near(s$z[gaps], w$linear), "linear: ", paste(s$z[gaps], collapse = " "), " against pandas' ", paste(unlist(w$linear), collapse = " "))
  # the filled times are the grid's: 2016-05-01, 2019-08-01, 2021-03-01
  .tab_expect(identical(format(s$times[gaps]), c("2016-05-01", "2019-08-01", "2021-03-01")) && identical(s$kind, "date"), "the filled times")
  # on the log scale, where the decomposition is additive
  s <- series("linear", "log")
  .tab_expect(identical(s$lambda, 0) && .ts_near(s$z[gaps], w$log_linear) && identical(s$z[-gaps], log(s$y[-gaps])), "linear on the logs")
  # seasonal: forecast::na.interp of the series as a ts of frequency 12, called here directly
  s <- series("seasonal")
  direct <- as.numeric(forecast::na.interp(stats::ts(s$y, frequency = 12)))
  .tab_expect(identical(s$z, direct) && all(is.finite(s$z)) && identical(s$z[-gaps], s$y[-gaps]), "seasonal: na.interp")
  .tab_expect(abs(s$z[[gaps[[1L]]]] - w$linear[[1L]]) > 1e-6, "seasonal is not the straight line")
  # Box–Cox: Guerrero's λ on the observed values with their gaps; its optimiser stops at optimize()'s tolerance
  s <- series("linear", "box_cox")
  .tab_expect(abs(s$lambda - w$lambda_r_path) <= 1e-6, sprintf("λ %.12g, the transcription on R's path %.12g", s$lambda, w$lambda_r_path))
  .tab_expect(abs(s$lambda - w$lambda_tight) <= 1e-4, sprintf("λ %.12g, the true minimum %.12g", s$lambda, w$lambda_tight))
  .tab_expect(identical(series("linear", "box_cox", 0.5)$lambda, 0.5), "a λ given")
  # a value at or below 0 with a transform: ARIMA's refusal
  bad <- .ts_gaps_frame(code)
  bad$boxes[[5L]] <- 0
  msg <- .tab_refused_with("not_positive", ts_series(.ts_task(bad), "month", "boxes", "month", missing = "linear", transform = "log"))
  .tab_expect(grepl("holds 0 or a negative value", msg, fixed = TRUE), msg)
}

TAB_COMMON_TESTS[["timeseries: the series keeps the time's own type (dec_types), and ARIMA still reads with its own setting"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  raw <- tab_ref_table(file.path(code, "reference", "data", "dec_types.csv"), colClasses = "character", encoding = "UTF-8")
  .tab_expect(nrow(raw) == 100L, "dec_types.csv has 100 rows")
  keep <- -60L  # a day without a row; the values of rows 30 and 31 are empty: three periods to fill
  hours <- as.POSIXct(raw$t_hour, tz = "UTC")
  rome <- hours
  attr(rome, "tzone") <- "Europe/Rome"
  types <- list(
    int64 = list(x = bit64::as.integer64(raw$t_int64), type = arrow::int64(), frequency = "day"),
    int32 = list(x = as.integer(raw$t_int), type = arrow::int32(), frequency = "day"),
    int16 = list(x = as.integer(raw$t_int), type = arrow::int16(), frequency = "day"),
    date32 = list(x = as.Date(raw$t_date), type = arrow::date32(), frequency = "day"),
    ts_us_utc = list(x = hours, type = arrow::timestamp("us", "UTC"), frequency = "hour"),
    ts_ns = list(x = hours, type = arrow::timestamp("ns"), frequency = "hour"),
    ts_ms_rome = list(x = rome, type = arrow::timestamp("ms", "Europe/Rome"), frequency = "hour"),
    double = list(x = as.numeric(raw$t_int), type = arrow::float64(), frequency = "day"))
  value <- as.numeric(raw$value)
  path <- tempfile(fileext = ".parquet")
  on.exit({ Sys.unsetenv("TABULARIA_INPUT"); unlink(path) }, add = TRUE)
  before <- getOption("arrow.int64_downcast")
  for (name in names(types)) {
    ty <- types[[name]]
    full <- arrow::Array$create(ty$x)$cast(ty$type)
    arrow::write_parquet(arrow::arrow_table(t = full$Take(seq_len(100L)[keep] - 1L), v = arrow::Array$create(value[keep])), path)
    Sys.setenv(TABULARIA_INPUT = path)
    task <- .ts_task(NULL)
    task$.frame <- NULL
    s <- ts_series(task, "t", "v", ty$frequency, missing = "linear")
    .tab_expect(s$n == 100L && s$n_filled == 3L && identical(which(s$filled), c(30L, 31L, 60L)), name, ": the gaps")
    arr <- ts_time_array(s$times, s$type)
    .tab_expect(arr$type == ty$type, name, ": the output's time is ", arr$type$ToString(), ", the input's ", ty$type$ToString())
    .tab_expect(arr$Equals(full), name, ": the times, filled ones included, are the grid's: ", paste(head(arr$as_vector(), 3L), collapse = " "))
    if (name == "int64") {
      got <- arr$as_vector()
      .tab_expect(inherits(got, "integer64") && identical(as.character(got), raw$t_int64), "an Int64 above 2^31 keeps every digit")
    }
    .tab_expect(identical(getOption("arrow.int64_downcast"), before), name, ": the option arrow.int64_downcast changed for the process")
  }
  # ARIMA in the same process: an Int64 that fits in 32 bits still arrives as an R integer (arrow's default), while
  # the decomposition's read gives a 64-bit integer
  arrow::write_parquet(arrow::arrow_table(t = arrow::Array$create(bit64::as.integer64(1:30)), v = arrow::Array$create(as.numeric(1:30))), path)
  task <- .ts_task(NULL)
  task$.frame <- NULL
  .tab_expect(is.integer(tab_input(task, c("t", "v"))$t), "ARIMA's read of a small Int64: not an R integer")
  .tab_expect(inherits(ts_read(task, c("t", "v"))$df$t, "integer64"), "the decomposition's read of a small Int64: not a 64-bit integer")
  .tab_expect(is.integer(tab_input(task, c("t", "v"))$t), "ARIMA's read after the decomposition's: not an R integer")
  # a column missing from the file is refused with a sentence, before the read
  msg <- .tab_refused_with("no_column", ts_read(task, c("t", "nope")))
  .tab_expect(identical(msg, "the input has no column «nope»"), msg)
}

TAB_COMMON_TESTS[["timeseries: the series refuses as ARIMA does (a time held twice, off the grid, of another type; dates by the hour)"]] <- function(code) {
  df <- .ts_gaps_frame(code)
  run <- function(d, frequency = "month") ts_series(.ts_task(d), "month", "boxes", frequency, missing = "linear")
  twice <- rbind(df, df[3L, ])
  msg <- .tab_refused_with("duplicate_time", run(twice))
  .tab_expect(grepl("holds two rows for the period of 2015-03-01", msg, fixed = TRUE), msg)
  days <- data.frame(month = seq(as.Date("2024-01-01"), by = "week", length.out = 20), boxes = 1:20 + 0.5)
  days$month[[4L]] <- days$month[[4L]] + 3
  msg <- .tab_refused_with("irregular_time", run(days, "week"))
  .tab_expect(grepl("are not whole weeks apart (2024-01-25)", msg, fixed = TRUE), msg)
  .tab_refused_with("frequency_hour", run(df, "hour"))
  text <- df
  text$month <- format(text$month)
  .tab_refused_with("time_type", run(text))
  none <- df
  none$boxes <- NA_real_
  .tab_refused_with("no_values", run(none))
  word <- df
  word$boxes <- as.character(word$boxes)
  .tab_refused_with("not_a_number", run(word))
}

TAB_COMMON_TESTS[["timeseries: the positions of a cycle are the calendar's where it names them (pandas' calendar)"]] <- function(code) {
  for (case in .ts_witness(code)$positions) {
    n <- case$n
    times <- switch(case$frequency,
      day = seq(as.Date(case$start), by = "day", length.out = n),
      month = seq(as.Date(case$start), by = "month", length.out = n),
      quarter = seq(as.Date(case$start), by = "quarter", length.out = n),
      hour = as.POSIXct(case$start, tz = "UTC") + 3600 * (seq_len(n) - 1))
    if (!is.null(case$tz)) attr(times, "tzone") <- case$tz
    kind <- if (case$frequency == "hour") "datetime" else "date"
    got <- ts_positions(times, kind, case$frequency, case$period)
    .tab_expect(identical(got$kind, case$kind) && identical(got$position, as.integer(unlist(case$position))) &&
                length(got$codes) == case$period, case$name, ": ", got$kind, " ", paste(head(got$position, 12L), collapse = " "))
  }
  # the names: 2024-01-03 is a Wednesday, 2023-11 a November; position 1 of a day of the year is 1 January
  p <- ts_positions(seq(as.Date("2024-01-03"), by = "day", length.out = 20), "date", "day", 7L)
  .tab_expect(identical(p$codes[p$position[1:2]], c("weekday_3", "weekday_4")), "the weekdays' codes")
  p <- ts_positions(as.POSIXct("2024-03-27 05:00", tz = "UTC") + 3600 * 0:399, "datetime", "hour", 168L)
  .tab_expect(identical(p$codes[[p$position[[1L]]]], "weekday_3_hour_5"), "a weekday and an hour: ", p$codes[[p$position[[1L]]]])
  p <- ts_positions(seq(as.Date("2023-07-01"), by = "day", length.out = 800), "date", "day", 365L)
  .tab_expect(p$position[[185L]] == 1L && identical(p$codes[[1L]], "yearday_1"), "the first 1 January is position 1")
  # an integer time has steps
  .tab_expect(identical(ts_positions(1:20, "integer", "day", 7L)$kind, "step"), "an integer time")
}

TAB_COMMON_TESTS[["timeseries: a long series keeps each bucket's lowest and highest instant for its chart (numpy)"]] <- function(code) {
  w <- .ts_witness(code)$envelope
  walk <- tab_read_csv(file.path(code, "reference", "data", "dec_checks.csv"), list(walk = "number"))$walk
  holed <- walk
  holed[11:16] <- NA
  .tab_expect(identical(ts_envelope(walk, 100L), as.integer(unlist(w$walk_100))), "walk, 100 points")
  .tab_expect(identical(ts_envelope(holed, 100L), as.integer(unlist(w$walk_holed_100))), "walk with empty values, 100 points")
  .tab_expect(identical(ts_envelope(walk, 401L), as.integer(unlist(w$walk_401))), "walk, all of it")
  # the spike a plain thinning steps over stays
  y <- rep(0, 100000L)
  y[54321L] <- 9
  .tab_expect(54321L %in% ts_envelope(y, 2000L) && length(ts_envelope(y, 2000L)) <= 2000L, "the spike")
}
