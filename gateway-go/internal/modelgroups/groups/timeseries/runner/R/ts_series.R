# The series of the group «timeseries» (docs/specs/ts-decomposition.md, «The series»; lot D1): what ARIMA and the
# decomposition share about time — the grid of a frequency, the refusals of a time held twice or off the grid — and
# what the decomposition adds: the periods inside the span without a value (refused, or filled by interpolation and
# flagged), the transform and its λ, the input's own types kept (an Int64 time stays Int64).
#
#   ts_period(t, kind, frequency)        the period of each time as a number (whole on the grid)
#   ts_time_after(t, k, kind, frequency) a time k periods after t, of t's class
#   ts_x0(t, kind)                       the x of a chart: days since 1970-01-01, or the integer itself
#   ts_time_kind(x, time, frequency)     date, datetime or integer, or a refusal
#   ts_grid(df, time, kind, frequency, max_periods)   the rows sorted and placed on the grid, or a refusal
#   ts_series(task, …)                   the decomposition's series: read, checked, on its grid, filled, transformed
#   ts_time_array(times, type)           the times as an Arrow array of the input field's own type
#   ts_positions(times, kind, frequency, p)   the position of each instant in a cycle of p, named by the calendar
#   ts_envelope(y, max)                  the points of a long series kept for its chart: each bucket's lowest and highest
#
# ARIMA (arima.R) calls the first five; its results are those it gave before they moved here.

TS_MAX_PERIODS <- 200000L  # places of the grid, gaps included: the group's rows

TS_UNIT <- c(hour = "hours", day = "days", week = "weeks", month = "months", quarter = "quarters", year = "years",
             index = "steps")

# The season of a frequency, in steps (catalogue: SEASON_OF_FREQUENCY): none for year and index.
TS_SEASONS <- c(hour = 24L, day = 7L, week = 52L, month = 12L, quarter = 4L)

# ── the time: periods, the grid, the axis ───────────────────────────────────────
.ts_ym <- function(t) {
  lt <- as.POSIXlt(t, tz = "UTC")
  (lt$year + 1900) * 12 + lt$mon
}

# The period of each time as a number (whole for times on the frequency's grid): days for a
# daily date, weeks, months (year·12 + month), hours… An integer time is its own period.
ts_period <- function(t, kind, frequency) {
  if (kind == "integer") return(as.numeric(t))
  days <- if (kind == "date") as.numeric(t) else as.numeric(t) / 86400
  switch(frequency,
    hour = days * 24,
    day = days,
    week = days / 7,
    month = .ts_ym(t),
    quarter = .ts_ym(t) %/% 3,
    year = .ts_ym(t) %/% 12,
    index = rank(as.numeric(t), ties.method = "first") - 1,
    stop("unknown frequency ", frequency))
}

# A time k periods after `t` (k may be a vector), of t's class.
ts_time_after <- function(t, k, kind, frequency) {
  if (kind == "integer") return(t + as.integer(k))
  months <- c(month = 1L, quarter = 3L, year = 12L)[frequency]
  if (!is.na(months)) {
    lt <- as.POSIXlt(t, tz = "UTC")
    m <- lt$mon + k * months
    y <- lt$year + 1900 + m %/% 12
    m <- m %% 12
    first <- as.Date(sprintf("%04d-%02d-01", y, m + 1L))
    nextm <- as.Date(sprintf("%04d-%02d-01", y + (m + 1L) %/% 12, (m + 1L) %% 12 + 1L))
    day <- pmin(lt$mday, as.integer(nextm - first))
    d <- first + (day - 1L)
    if (kind == "date") return(d)
    secs <- lt$hour * 3600 + lt$min * 60 + lt$sec
    return(as.POSIXct(as.numeric(d) * 86400 + secs, origin = "1970-01-01", tz = "UTC"))
  }
  step <- c(hour = 3600, day = 86400, week = 604800)[frequency]
  if (is.na(step)) step <- 86400
  if (kind == "date") return(t + as.integer(k * step / 86400))
  t + k * step
}

# The x of a chart: days since 1970-01-01 for a date or datetime, the integer itself.
ts_x0 <- function(t, kind) {
  if (kind == "date") return(as.numeric(t))
  if (kind == "datetime") return(as.numeric(t) / 86400)
  as.numeric(t)
}

# The kind of the time column: date, datetime or integer (a number whose values are all whole); anything else, and
# a date with frequency = hour, is refused.
ts_time_kind <- function(x, time, frequency) {
  kind <- tab_kind(x)
  if (kind == "number" && all(is.na(x) | x == trunc(x))) kind <- "integer"
  if (!kind %in% c("date", "datetime", "integer")) {
    tab_refuse("time_type", paste0("the column «", time, "» is not a date, a datetime or an integer: the time of a series ",
                                   "must be one of them"), list(column = time))
  }
  if (kind == "date" && frequency == "hour") {
    tab_refuse("frequency_hour", paste0("the column «", time, "» holds dates, without hours: choose frequency = day, or a ",
                                        "datetime column"), list(column = time))
  }
  kind
}

# The rows (no empty time) sorted by time and placed on the grid of the frequency, from the first time: `pos` the
# place of each row (1 = the first time), `periods` the grid's length. A time held twice, a time off the grid and a
# grid longer than `max_periods` are refused.
ts_grid <- function(df, time, kind, frequency, max_periods) {
  df <- df[order(as.numeric(df[[time]]), method = "radix"), , drop = FALSE]
  period <- ts_period(df[[time]], kind, frequency)
  twice <- which(duplicated(period) | duplicated(as.numeric(df[[time]])))
  if (length(twice)) {
    at <- tab_label(df[[time]][twice[[1L]]])
    tab_refuse("duplicate_time", paste0("the column «", time, "» holds two rows for the period of ", at, ": a series has one ",
                                        "row per period; aggregate first with Group by"), list(column = time, time = at))
  }
  pos <- period - period[[1L]]
  off <- which(abs(pos - round(pos)) > 1e-9)
  if (length(off)) {
    at <- tab_label(df[[time]][off[[1L]]])
    unit <- TS_UNIT[[frequency]]
    tab_refuse("irregular_time", paste0("the times of «", time, "» are not whole ", unit, " apart (", at, "): aggregate them ",
                                        "by ", sub("s$", "", unit), " first with Group by, or choose another frequency"),
               list(column = time, time = at))
  }
  pos <- as.integer(round(pos)) + 1L
  periods <- max(pos)
  if (periods > max_periods) {
    tab_refuse("too_many_periods", paste0("the times of «", time, "» span ", periods, " ", TS_UNIT[[frequency]],
                                          " (at most ", max_periods, "): is the frequency right?"), list(periods = periods))
  }
  list(df = df, pos = pos, periods = periods)
}

# ── the input, its types kept ───────────────────────────────────────────────────
# The Parquet schema of the step's input (NULL for a self-test's frame).
.ts_schema <- function(task) {
  if (!is.null(task$.frame)) return(NULL)
  path <- Sys.getenv("TABULARIA_INPUT")
  if (!nzchar(path)) return(NULL)
  tryCatch(arrow::ParquetFileReader$create(path)$GetSchema(), error = function(e) NULL)
}

# The node's columns, read with arrow's `arrow.int64_downcast` off for this read only: an Int64 arrives as a
# 64-bit integer (bit64::integer64), never an R integer or a double. The option is process-wide; set here, inside
# the fit, it is restored when the read returns, so ARIMA in the same image reads its input as it always did. A
# column missing from the input is refused before the read. `type`: the Arrow type of each column (NULL for a
# frame, which keeps its R classes).
ts_read <- function(task, cols) {
  withr::local_options(list(arrow.int64_downcast = FALSE))
  schema <- .ts_schema(task)
  have <- if (!is.null(task$.frame)) names(task$.frame) else if (!is.null(schema)) schema$names else NULL
  if (!is.null(have)) {
    for (name in cols) if (!name %in% have) tab_refuse("no_column", paste0("the input has no column «", name, "»"), list(column = name))
  }
  df <- tab_input(task, cols)
  for (name in cols) if (!name %in% names(df)) tab_refuse("no_column", paste0("the input has no column «", name, "»"), list(column = name))
  type <- lapply(stats::setNames(cols, cols), function(name) {
    if (is.null(schema)) NULL else tryCatch(schema$GetFieldByName(name)$type, error = function(e) NULL)
  })
  list(df = df, type = type)
}

# The times as an Arrow array of `type`, the input field's own (Int64, Int32, Int16, date32, timestamp with its
# unit and zone, double): the array R's class gives (integer64 → int64, integer → int32, Date → date32, POSIXct →
# timestamp[us], double → double) cast to it, safely (a value that does not fit is an error, never a silent
# change). Without a type (a self-test's frame) the array of R's class.
ts_time_array <- function(times, type = NULL) {
  a <- arrow::Array$create(times)
  if (is.null(type) || a$type == type) return(a)
  a$cast(type)
}

# ── the series of the decomposition ─────────────────────────────────────────────
# The node's series, from the first to the last observed value, one place a period of `frequency`:
#
#   kind, frequency, times (the span's times: the row's own where there is one, of the input's R class), x0, type
#   (the time field's Arrow type, for ts_time_array), n (the span's length), y (the observed values, NA where
#   filled), filled (logical), z (the series the methods read: transformed, and filled when asked), lambda (NULL
#   without a transform, 0 for the log, Box–Cox's λ), n_missing (rows without a time, rows before the first and
#   after the last value), n_filled, missing (the option), transform.
#
# `missing`: refuse (a period inside the span without a value stops the node with `gaps`), linear (stats::approx
# between the two neighbours), seasonal (forecast::na.interp of the series as a ts of frequency `period`). The fill
# is made on the transformed scale (log, Box–Cox), where the decomposition is additive; λ is estimated first, on the
# observed values with their gaps (Guerrero, forecast::BoxCox.lambda as ARIMA calls it). `period`: the smallest
# seasonal period (NULL: none), the frequency of the ts that λ and the seasonal fill read.
ts_series <- function(task, time, value, frequency, missing = "refuse", transform = "none", box_cox_lambda = "auto",
                      period = NULL, max_periods = TS_MAX_PERIODS) {
  read <- ts_read(task, c(time, value))
  df <- read$df
  kind <- ts_time_kind(df[[time]], time, frequency)
  if (!tab_kind(df[[value]]) %in% c("number", "integer")) {
    tab_refuse("not_a_number", paste0("the column «", value, "» is not a number"), list(column = value))
  }
  df[[value]] <- as.numeric(df[[value]])
  tab_complete_rows(df, value)  # an infinite value stops the node and names the column
  no_time <- sum(is.na(df[[time]]))
  df <- df[!is.na(df[[time]]), , drop = FALSE]
  if (!nrow(df) || all(is.na(df[[value]]))) {
    tab_refuse("no_values", paste0("the column «", value, "» has no value to model"), list(column = value))
  }
  grid <- ts_grid(df, time, kind, frequency, max_periods)
  df <- grid$df
  y_all <- rep(NA_real_, grid$periods)
  y_all[grid$pos] <- df[[value]]
  observed <- which(!is.na(y_all))
  first <- min(observed)
  last <- max(observed)
  span <- seq.int(first, last)
  outside <- sum(grid$pos < first | grid$pos > last)
  # the span's times: the row's own value (its class, an Int64's every digit), the grid's where there is no row
  first_time <- df[[time]][[1L]]
  times <- ts_time_after(first_time, span - 1L, kind, frequency)
  at <- match(span, grid$pos)
  times[!is.na(at)] <- df[[time]][at[!is.na(at)]]
  if (kind == "datetime") attr(times, "tzone") <- attr(df[[time]], "tzone")
  y <- y_all[span]
  gaps <- which(is.na(y))
  if (length(gaps) && identical(missing, "refuse")) {
    label <- tab_label(times[gaps[[1L]]])
    unit <- if (length(gaps) == 1L) "period" else "periods"
    tab_refuse("gaps", paste0("the series has ", length(gaps), " ", unit, " without a value inside its span (the first: ", label,
                              "): fill them in the flow (a calendar Left join and Fill nulls with 0 when an empty ",
                              .ts_one(frequency), " means none), or choose missing = linear or seasonal"),
               list(n = length(gaps), time = label))
  }
  lambda <- ts_lambda(y, value, transform, box_cox_lambda, period)
  z <- if (is.null(lambda)) y else as.numeric(forecast::BoxCox(y, lambda))
  filled <- rep(FALSE, length(y))
  if (length(gaps)) {
    filled[gaps] <- TRUE
    z <- ts_fill(z, missing, period)
  }
  list(kind = kind, frequency = frequency, time = time, value = value, times = times, x0 = ts_x0(times, kind),
       type = read$type[[time]], n = length(y), y = y, z = z, filled = filled, lambda = lambda, transform = transform,
       missing = missing, n_missing = no_time + outside, n_filled = length(gaps))
}

# A series nothing can be computed on: the same value everywhere (`constant`), or values whose variance a double cannot
# hold — squares beyond 1.8e308 or below 4.9e-324 (`scale`: the tests would read an infinite or a zero variance and say
# «constant» of a series that is not). `y`: the observed values.
ts_refuse_degenerate <- function(y, value) {
  y <- y[!is.na(y)]
  if (length(y) < 2L || max(y) == min(y)) {
    tab_refuse("constant", paste0("the column «", value, "» has the same value everywhere: there is nothing to decompose"),
               list(column = value))
  }
  v <- stats::var(y)
  if (!is.finite(v) || v == 0) {
    tab_refuse("scale", paste0("the values of the column «", value, "» are too ", if (is.finite(v)) "small" else "large",
                               " to compute with: their squares leave the range of a number; rescale the column first ",
                               "(a Formula that ", if (is.finite(v)) "multiplies" else "divides", " it by a power of ten)"),
               list(column = value))
  }
  invisible(TRUE)
}

# «an empty day», «an empty month»…: one period of the frequency, in the refusal's advice.
.ts_one <- function(frequency) c(hour = "hour", day = "day", week = "week", month = "month", quarter = "quarter",
                                 year = "year", index = "step")[[frequency]]

# The transform's λ: NULL for none, 0 for the log, Box–Cox's given or estimated (Guerrero) on the observed values
# with their gaps, as a ts of frequency `period` (1 without one). A value at or below 0 stops the node, with ARIMA's
# sentence.
ts_lambda <- function(y, value, transform, box_cox_lambda = "auto", period = NULL) {
  if (!transform %in% c("log", "box_cox")) return(NULL)
  if (any(y <= 0, na.rm = TRUE)) {
    tab_refuse("not_positive", paste0("the column «", value, "» holds 0 or a negative value: a log or Box–Cox transform ",
                                      "needs positive values; use transform = none"), list(column = value))
  }
  if (transform == "log") return(0)
  if (identical(box_cox_lambda %||% "auto", "auto")) {
    return(forecast::BoxCox.lambda(stats::ts(y, frequency = max(1L, period %||% 1L)), method = "guerrero"))
  }
  as.numeric(box_cox_lambda)
}

# The gaps of z filled: `linear`, the straight line between the two observed neighbours (stats::approx); `seasonal`,
# forecast::na.interp of z as a ts of frequency `period` (what it does in forecast 9.0.2: «As built / D1» of the
# spec). A gap is inside the span, so it always has two neighbours.
ts_fill <- function(z, missing, period = NULL) {
  idx <- which(!is.na(z))
  gaps <- which(is.na(z))
  if (identical(missing, "seasonal")) {
    out <- as.numeric(forecast::na.interp(stats::ts(z, frequency = max(1L, period %||% 1L))))
    z[gaps] <- out[gaps]
    return(z)
  }
  z[gaps] <- stats::approx(idx, z[idx], xout = gaps)$y
  z
}

# ── positions in a cycle, named by the calendar ─────────────────────────────────
# The position of each instant in a cycle of p (the cycles counted from the first instant), and what each position
# is called. Where the calendar names it — a day of the week (frequency day, p 7), a month (month, 12), an hour of
# the day (hour, 24), a weekday and an hour (hour, 168), a quarter (quarter, 4), a day of the year (day, 365) — the
# positions are numbered by the calendar (1 = Monday, 1 = January, 1 = 00:00, 1 = Monday 00:00, 1 = Q1, 1 = the
# first 1 January's place) and `codes[k]` names position k (weekday_1 … weekday_7, month_1 … month_12, hour_0 …
# hour_23, weekday_1_hour_0 … weekday_7_hour_23, quarter_1 … quarter_4, yearday_1 … yearday_365); elsewhere they are
# steps from the first instant (step_1 … step_p). The calendar is read in the time column's own zone (UTC for a
# timestamp without one); when the first cycle does not name every position once (an hourly series across a change
# of the clock), the positions are steps.
#
#   kind      weekday, month, hour, weekday_hour, quarter, yearday, step
#   position  integer, one per instant, 1…p
#   codes     character, one per position
ts_positions <- function(times, kind, frequency, p) {
  n <- length(times)
  cycle <- (seq_len(n) - 1L) %% p + 1L
  steps <- list(kind = "step", position = cycle, codes = paste0("step_", seq_len(p)))
  if (!kind %in% c("date", "datetime") || n < p) return(steps)
  which_kind <- if (frequency == "day" && p == 7L) "weekday" else if (frequency == "month" && p == 12L) "month" else
    if (frequency == "hour" && p == 24L) "hour" else if (frequency == "hour" && p == 168L) "weekday_hour" else
    if (frequency == "quarter" && p == 4L) "quarter" else if (frequency == "day" && p == 365L) "yearday" else NULL
  if (is.null(which_kind)) return(steps)
  tz <- attr(times, "tzone")
  tz <- if (is.null(tz) || !nzchar(tz[[1L]])) "UTC" else tz[[1L]]
  lt <- as.POSIXlt(times[seq_len(p)], tz = tz)
  wday <- (lt$wday + 6L) %% 7L + 1L  # 1 = Monday
  if (which_kind == "yearday") {
    # the place of the first 1 January is position 1: a year of 365 steps drifts one day in a leap year, so the
    # positions are steps from it, not the calendar's day of each year
    jan1 <- which(lt$yday == 0L)
    if (!length(jan1)) return(steps)
    calendar <- (seq_len(p) - jan1[[1L]]) %% p + 1L
    codes <- paste0("yearday_", seq_len(p))
  } else {
    calendar <- switch(which_kind,
      weekday = wday,
      month = lt$mon + 1L,
      hour = lt$hour + 1L,
      weekday_hour = (wday - 1L) * 24L + lt$hour + 1L,
      quarter = lt$mon %/% 3L + 1L)
    if (!identical(sort(calendar), seq_len(p))) return(steps)
    codes <- switch(which_kind,
      weekday = paste0("weekday_", seq_len(7L)),
      month = paste0("month_", seq_len(12L)),
      hour = paste0("hour_", seq_len(24L) - 1L),
      weekday_hour = paste0("weekday_", rep(seq_len(7L), each = 24L), "_hour_", rep(seq_len(24L) - 1L, 7L)),
      quarter = paste0("quarter_", seq_len(4L)))
  }
  list(kind = which_kind, position = calendar[cycle], codes = codes)
}

# ── a long series for its chart ─────────────────────────────────────────────────
# The instants of y to draw, in time order: all of them up to `max`; above it the instants are cut into ⌊max / 2⌋
# buckets of (nearly) equal length and each bucket keeps its lowest and its highest value (one instant when they are
# the same, the first of equal values), so that a spike or a dip a plain thinning would step over stays on the
# chart. Empty values are never kept (a bucket of them keeps nothing). No sample: the same series gives the same
# points.
ts_envelope <- function(y, max = 2000L) {
  n <- length(y)
  max <- max(2L, as.integer(max))
  if (n <= max) return(which(!is.na(y)))
  buckets <- max %/% 2L
  bucket <- ((seq_len(n) - 1L) * buckets) %/% n + 1L  # %/% binds tighter than *
  ok <- !is.na(y)
  i <- which(ok)
  b <- bucket[ok]
  v <- y[ok]
  up <- order(b, v, i, method = "radix")
  down <- order(b, -v, i, method = "radix")
  sort(unique(c(i[up][!duplicated(b[up])], i[down][!duplicated(b[down])])))
}
