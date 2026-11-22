# The self-test of the decomposition by STL, the classical method and MSTL (decomposition.R, lot D2): added to
# TAB_COMMON_TESTS. The reference cases (reference/expected.json, ids «dec_*») run by the common self-test; these check what
# a row table cannot: the components against the direct calls, the identities, the explanation's ids, the windows, the output's
# types and the refusals.

.dec_run <- function(df, params) {
  params <- utils::modifyList(list(output = "components"), params)
  fit_decomposition(list(protocol = TAB_PROTOCOL, mode = "fit", group = "timeseries", group_version = "selftest", model = "decomposition",
                         variant = params$method %||% "stl", params = params, output = "components", write_output = FALSE, rows_from = "run",
                         limits = tab_catalog()$limits, .frame = df))
}

.dec_months <- function(x, start) data.frame(month = seq(as.Date(start), by = "month", length.out = length(x)), y = as.numeric(x))

# the largest difference, in units of the series' own size
.dec_gap <- function(a, b) {
  both <- !is.na(a) & !is.na(b)
  if (!identical(is.na(a), is.na(b))) return(Inf)
  if (!any(both)) return(0)
  max(abs(a[both] - b[both])) / max(1, max(abs(b[both])))
}

TAB_COMMON_TESTS[["timeseries: STL, classical and MSTL equal stats::stl, stats::decompose and forecast::mstl called directly on co2, AirPassengers and taylor, and add up to the series"]] <- function(code) {
  seen <- 0L
  look <- function(name, got, want, tol = 1e-12) {
    gap <- .dec_gap(got, want)
    .tab_expect(gap <= tol, name, ": ", gap, " from the direct call")
    seen <<- seen + 1L
  }
  co2 <- .dec_months(datasets::co2, "1959-01-01")
  ap <- .dec_months(datasets::AirPassengers, "1949-01-01")
  param <- function(...) list(time = "month", value = "y", frequency = "month", ...)
  # STL: the seasonal smoother's window, periodic, robust, a window of the trend
  for (p in list(list(s_window = 11), list(s_window = "periodic", robust = TRUE), list(s_window = 7, t_window = 25))) {
    out <- as.data.frame(.dec_run(co2, do.call(param, c(list(method = "stl"), p)))$output)
    sw <- if (identical(p$s_window, "periodic")) "periodic" else p$s_window
    f <- stats::stl(datasets::co2, s.window = sw, t.window = p$t_window, robust = isTRUE(p$robust))$time.series
    look("co2 STL trend", out$trend, f[, "trend"]); look("co2 STL seasonal", out$seasonal_12, f[, "seasonal"]); look("co2 STL remainder", out$remainder, f[, "remainder"])
    look("co2 STL adds up", out$trend + out$seasonal_12 + out$remainder, out$observed, 1e-10)
  }
  # classical, both types
  for (type in c("additive", "multiplicative")) {
    out <- as.data.frame(.dec_run(ap, param(method = "classical", type = type))$output)
    f <- stats::decompose(datasets::AirPassengers, type = type)
    look(paste("AirPassengers", type, "trend"), out$trend, f$trend); look(paste("AirPassengers", type, "seasonal"), out$seasonal_12, f$seasonal)
    look(paste("AirPassengers", type, "remainder"), out$remainder, f$random)
    whole <- if (type == "additive") out$trend + out$seasonal_12 + out$remainder else out$trend * out$seasonal_12 * out$remainder
    ok <- !is.na(whole)
    look(paste("AirPassengers", type, "adds up"), whole[ok], out$observed[ok], 1e-10)
    look(paste("AirPassengers", type, "adjusted"), out$deseasonalized, if (type == "additive") out$observed - out$seasonal_12 else out$observed / out$seasonal_12, 1e-10)
  }
  # STL on the log: the parts add up on the logarithm, the adjusted series is back on the original scale
  out <- as.data.frame(.dec_run(ap, param(method = "stl", transform = "log", robust = TRUE))$output)
  f <- stats::stl(log(datasets::AirPassengers), s.window = 11, robust = TRUE)$time.series
  look("AirPassengers log STL trend", out$trend, f[, "trend"]); look("AirPassengers log STL remainder", out$remainder, f[, "remainder"])
  look("AirPassengers log STL adds up", out$trend + out$seasonal_12 + out$remainder, out$transformed, 1e-10)
  look("AirPassengers log STL transformed", out$transformed, log(out$observed), 1e-12)
  look("AirPassengers log STL adjusted", out$deseasonalized, exp(log(out$observed) - f[, "seasonal"]), 1e-10)
  # MSTL on taylor (half-hours: periods 48 and 336), as an index
  y <- as.numeric(forecast::taylor)
  tay <- data.frame(t = seq_along(y) - 1L, y = y)
  for (robust in c(FALSE, TRUE)) {
    out <- as.data.frame(.dec_run(tay, list(time = "t", value = "y", frequency = "index", method = "mstl", periods = list(48, 336), robust = robust))$output)
    args <- list(forecast::msts(y, seasonal.periods = c(48, 336)), iterate = 2, s.window = c(11, 15))
    if (robust) args$robust <- TRUE
    f <- do.call(forecast::mstl, args)
    look("taylor MSTL trend", out$trend, f[, "Trend"]); look("taylor MSTL seasonal_48", out$seasonal_48, f[, "Seasonal48"])
    look("taylor MSTL seasonal_336", out$seasonal_336, f[, "Seasonal336"]); look("taylor MSTL remainder", out$remainder, f[, "Remainder"])
    look("taylor MSTL adds up", out$trend + out$seasonal_48 + out$seasonal_336 + out$remainder, out$observed, 1e-10)
  }
  .tab_expect(seen >= 35L, "only ", seen, " comparisons")
}

TAB_COMMON_TESTS[["timeseries: every decomposition case's explanation holds the template ids of expected.json, row by row, as «@id name=value…», and its rows are the catalogue's"]] <- function(code) {
  ref <- tab_ref_json(file.path(code, "reference", "expected.json"), simplifyVector = FALSE)
  seen <- 0L
  for (case in ref$cases) {
    if (!identical(case$model, "decomposition") || is.null(case$reading)) next
    df <- tab_read_csv(file.path(code, "reference", case$data), case$types)
    rows <- tab_results_frame(.dec_run(df, case$params)$rows)
    r <- rows[rows$section == "reading", ]
    want <- unlist(lapply(names(case$reading), function(g) {
      ids <- unlist(case$reading[[g]])
      paste0("reading|", sub("\\|", "|", g, fixed = FALSE), "|line_", seq_along(ids), " ", ids)
    }))
    .tab_expect(identical(paste(r$key, sub("^@([^ ]+).*$", "\\1", r$text)), want), case$id, ": the reading differs")
    .tab_expect(all(grepl("^@[a-z0-9_]+( [a-z0-9_]+=[^ ]*)*$", r$text)), case$id, ": a line is not «@id name=value…»")
    problems <- tab_check_declared(rows, "decomposition", case$params$method)
    .tab_expect(!length(problems), case$id, ": ", problems[[1L]])
    seen <- seen + 1L
  }
  .tab_expect(seen >= 7L, "only ", seen, " cases")
}

TAB_COMMON_TESTS[["timeseries: a window made odd is passed odd and said in a note; a run with the odd windows is the same run"]] <- function(code) {
  d <- .dec_months(datasets::AirPassengers, "1949-01-01")
  p <- list(time = "month", value = "y", frequency = "month", method = "stl")
  a <- .dec_run(d, c(p, list(s_window = 12.5, t_window = 20)))
  b <- .dec_run(d, c(p, list(s_window = 13, t_window = 21)))
  rows <- tab_results_frame(a$rows)
  notes <- rows$text[startsWith(rows$stat, "note_")]
  .tab_expect(identical(notes, c("@window_made_odd given=12.5 used=13", "@window_made_odd given=20 used=21")), paste(notes, collapse = " / "))
  .tab_expect(identical(rows$value[rows$key == "season|12||s_window_used"], 13) && identical(rows$value[rows$key == "model|||t_window_used"], 21), "the windows used")
  .tab_expect(identical(as.data.frame(a$output), as.data.frame(b$output)), "the odd windows give another decomposition")
  rows <- tab_results_frame(b$rows)
  .tab_expect(!any(startsWith(rows$stat, "note_")), "a note for an odd window")
}

TAB_COMMON_TESTS[["timeseries: the components keep the input's time type, mark the filled instants and write the columns the catalogue declares"]] <- function(code) {
  ap <- .dec_months(datasets::AirPassengers, "1949-01-01")
  ap$y[c(20, 21, 70)] <- NA
  p <- list(time = "month", value = "y", frequency = "month", method = "stl", missing = "linear")
  out <- .dec_run(ap, p)$output
  .tab_expect(identical(names(out), c("time", "observed", "filled", "trend", "seasonal_12", "remainder", "deseasonalized")), paste(names(out), collapse = " "))
  d <- as.data.frame(out)
  .tab_expect(out$schema$GetFieldByName("time")$type == arrow::date32() && out$schema$GetFieldByName("filled")$type == arrow::boolean(), "types")
  .tab_expect(identical(which(d$filled), c(20L, 21L, 70L)) && all(is.na(d$observed[d$filled])) && !anyNA(d$trend) && !anyNA(d$remainder), "the filled instants")
  out <- .dec_run(ap, c(p, list(transform = "log")))$output
  .tab_expect(identical(names(out), c("time", "observed", "transformed", "filled", "trend", "seasonal_12", "remainder", "deseasonalized")), "transformed")
  # an Int64 time above 2^31 stays an Int64, an integer an Int32; the classical trend is empty at its ends
  big <- data.frame(t = bit64::as.integer64(5e9) + 0:59, y = 10 + sin(2 * pi * (0:59) / 4) + (0:59) / 10)
  out <- .dec_run(big, list(time = "t", value = "y", frequency = "index", method = "classical", period = 4))$output
  .tab_expect(out$schema$GetFieldByName("time")$type == arrow::int64() && identical(as.character(as.data.frame(out)$time[[60L]]), "5000000059"), "an Int64 time")
  d <- as.data.frame(out)
  .tab_expect(identical(which(is.na(d$trend)), c(1L, 2L, 59L, 60L)) && identical(which(is.na(d$remainder)), c(1L, 2L, 59L, 60L)), "the classical ends")
  small <- data.frame(t = 1:60, y = big$y)
  out <- .dec_run(small, list(time = "t", value = "y", frequency = "index", method = "stl", period = 4))$output
  .tab_expect(out$schema$GetFieldByName("time")$type == arrow::int32(), "an Int32 time")
  # the output `results` hands the results on, not the components
  .tab_expect(is.null(.dec_run(small, list(time = "t", value = "y", frequency = "index", method = "stl", period = 4, output = "results"))$output), "output = results")
}

TAB_COMMON_TESTS[["timeseries: a decomposition that cannot be made is refused with its sentence"]] <- function(code) {
  d <- .dec_months(datasets::AirPassengers, "1949-01-01")
  p <- list(time = "month", value = "y", frequency = "month")
  said <- function(code_, params, df = d) .tab_refused_with(code_, .dec_run(df, utils::modifyList(p, params)))
  .tab_expect(identical(said("too_short", list(method = "stl", period = 72)), "the series has 144 months; a period of 72 needs more than 144: drop it, or a longer series"), "too short (stl)")
  .tab_expect(identical(said("too_short", list(method = "classical", period = 100)), "the series has 144 months; a period of 100 needs at least 200: drop it, or a longer series"), "too short (classical)")
  .tab_expect(identical(said("period_needed", list(method = "stl", frequency = "year")), "a yearly or index series has no season of its own: give the period, in steps ('period')"), "period needed")
  .tab_expect(identical(said("windows_count", list(method = "mstl", periods = list(6, 12), s_windows = list(11, 15, 19))),
                        "give one seasonal window per period (2 periods, 3 windows), or none"), "windows")
  neg <- d
  neg$y[5] <- -1
  said("not_positive", list(method = "classical", type = "multiplicative"), neg)
  said("not_positive", list(method = "stl", transform = "log"), neg)
  said("multiplicative_transform", list(method = "classical", type = "multiplicative", transform = "box_cox"))
  said("gaps", list(method = "stl"), d[-c(10, 11), ])
  .tab_expect(identical(tryCatch(dec_periods(list(method = "stl", period = 365.25, frequency = "day")), tabularia_refusal = function(e) conditionMessage(e)),
                        "a period is a whole number of steps: 365, not 365.25"), "a whole period")
  # a constant series has nothing to decompose
  flat <- d
  flat$y <- 5
  .tab_expect(identical(said("constant", list(method = "stl"), flat), "the column «y» has the same value everywhere: there is nothing to decompose"), "constant")
  # values whose squares leave the range of a double are not «the same value everywhere» (lot D9): their own sentence, for
  # every method, before any test reads an infinite or a zero variance
  for (method in c("stl", "classical", "emd")) {
    huge <- d
    huge$y <- huge$y * 1e300
    m <- said("scale", list(method = method), huge)
    .tab_expect(grepl("too large to compute with", m, fixed = TRUE), m)
    tiny <- d
    tiny$y <- tiny$y * 1e-300
    m <- said("scale", list(method = method), tiny)
    .tab_expect(grepl("too small to compute with", m, fixed = TRUE), m)
  }
  # a series no longer than the season the frequency implies: the seasonality test is not computed, the node runs (lot D9:
  # stats::filter stopped EMD on 4 daily values without a sentence)
  short <- data.frame(day = as.Date("2024-01-01") + 0:4, y = c(1, 3, 2, 4, 0))
  rows <- tab_results_frame(.dec_run(short, list(time = "day", value = "y", frequency = "day", method = "emd"))$rows)
  .tab_expect(identical(rows$text[rows$key == "assumption|seasonality|7|verdict"], "not_computed") &&
                identical(rows$text[rows$key == "assumption|seasonality|7|advice_id"], "seasonality_few_cycles"), "EMD of 5 days")
  .tab_expect(identical(ts_seasonality(c(1, 3, 2, 4), 7L, "kruskal_wallis", 0.05)$verdict, "not_computed") &&
                identical(ts_seasonality(c(1, 3, 2, 4, 5, 1), 6L, "friedman", 0.05)$count, 0L), "a series no longer than its period")
}
