# The reference cases of the decomposition by STL, the classical method and MSTL (docs/specs/ts-decomposition.md,
# «Self-test, reference values, witnesses»; lot D2), computed OUTSIDE the runner: by stats::stl, stats::decompose and
# forecast::mstl called directly on the series built here from the CSV, and the checks by their own packages
# (stats::friedman.test, kruskal.test, Box.test, urca::ur.df, tseries::kpss.test) and by their definitions (the
# strengths). In the group's image, from the repository's root:
#
#   docker run --rm -v "$PWD/gateway-go/internal/modelgroups/groups/timeseries/reference:/ref" -w /ref \
#     tabularia-models-timeseries:dev Rscript make_reference_decomposition.R
#
# It writes data/dec_monthly_additive.csv, dec_monthly_multiplicative.csv, dec_daily_two.csv, dec_no_season.csv (fixed
# seeds) and MERGES its cases (ids «dec_*») into expected.json by id, textually: every other case stays byte for byte,
# whoever wrote it. Its items are written compactly (one line an expected number), so the group's ConfigMap stays small.
#
#   dec_monthly_additive        144 months: a trend, a fixed monthly pattern, noise
#   dec_monthly_multiplicative  144 months: a pattern proportional to the level
#   dec_daily_two               1,100 days: a weekly and a yearly pattern
#   dec_no_season              200 months: a random walk with noise, crossing zero
#
# Each case expects the node's numbers and a few points of each curve (those, for the witness: make_witness_decomposition.py
# recomputes them with statsmodels), and the ids of the explanation, row by row. The second witness is statsmodels'.

suppressMessages({
  library(forecast)
  library(jsonlite)
})

here <- if (length(grep("^--file=", commandArgs(FALSE)))) {
  dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
} else "."
data_dir <- file.path(here, "data")
w <- function(df, name) utils::write.csv(df, file.path(data_dir, name), row.names = FALSE, na = "", fileEncoding = "UTF-8")

# ── the tables ──────────────────────────────────────────────────────────────────────────────────────────────────
set.seed(20261210)
n <- 144
months <- format(seq(as.Date("2012-01-01"), by = "month", length.out = n), "%Y-%m-%d")
pattern <- c(-8, -9, -4, 2, 6, 10, 12, 9, 3, -2, -7, -12)
w(data.frame(month = months, sales = round(100 + 0.8 * seq_len(n) + rep(pattern, length.out = n) + stats::rnorm(n, sd = 1.5), 2)),
  "dec_monthly_additive.csv")
season <- 1 + 0.25 * sin(2 * pi * seq_len(n) / 12) + 0.08 * cos(2 * pi * seq_len(n) / 6)
w(data.frame(month = months, boxes = round(200 * exp(0.012 * seq_len(n)) * season * exp(stats::rnorm(n, sd = 0.03)), 2)),
  "dec_monthly_multiplicative.csv")
n <- 1100
t <- seq_len(n)
w(data.frame(day = format(seq(as.Date("2023-01-01"), by = "day", length.out = n), "%Y-%m-%d"),
             orders = round(200 + 0.05 * t + rep(c(8, 10, 9, 7, 4, -14, -18), length.out = n) + 20 * sin(2 * pi * t / 365.25) +
                              stats::rnorm(n, sd = 3), 0)),
  "dec_daily_two.csv")
n <- 200
w(data.frame(month = format(seq(as.Date("2008-01-01"), by = "month", length.out = n), "%Y-%m-%d"),
             level = round(cumsum(stats::rnorm(n, sd = 1)) + stats::rnorm(n, sd = 0.5), 2)), "dec_no_season.csv")

read <- function(file, types) {
  d <- utils::read.csv(file.path(data_dir, file), stringsAsFactors = FALSE)
  if (types[[1L]] == "date") d[[1L]] <- as.Date(d[[1L]])
  d
}

# ── the pieces, by hand ─────────────────────────────────────────────────────────────────────────────────────────
nextodd <- function(x) { x <- round(x); as.integer(if (x %% 2 == 0) x + 1 else x) }
odd <- function(w) { w <- as.integer(w); max(3L, if (w %% 2L == 0L) w + 1L else w) }
strength <- function(a, r) { ok <- !is.na(a) & !is.na(r); max(0, 1 - var(r[ok]) / var(a[ok] + r[ok])) }

# the seasonality test: the series detrended by the centred moving average (stats::decompose's trend), then the rank test
seasonality <- function(y, p, test, mult) {
  ma <- as.numeric(stats::decompose(stats::ts(y, frequency = p))$trend)
  d <- if (mult) y / ma else y - ma
  valid <- which(!is.na(d))
  b <- length(valid) %/% p
  if (b < 2L) return(list(count = b))
  if (test == "kruskal_wallis") {
    k <- stats::kruskal.test(d[valid], (valid - 1L) %% p + 1L)
  } else {
    k <- stats::friedman.test(matrix(d[seq(valid[1L], length.out = b * p)], nrow = b, byrow = TRUE))
  }
  list(statistic = unname(k$statistic), df = unname(k$parameter), p = unname(k$p.value), count = b)
}

# The case: `params` as the node takes them; returns the expected rows and the explanation's ids.
decomp_case <- function(id, data, types, params, note = NULL, points = NULL) {
  o <- modifyList(list(method = "stl", robust = FALSE, s_window = 11, t_window = "auto", iterate = 2, type = "additive",
                       transform = "none", box_cox_lambda = "auto", missing = "refuse", seasonality_test = "friedman",
                       lb_lag = "auto", alpha = 0.05), params)
  d <- read(data, types)
  y <- as.numeric(d[[2L]])
  n <- length(y)
  periods <- if (o$method == "mstl") sort(unlist(o$periods)) else if (!is.null(o$period)) o$period else
    c(hour = 24, day = 7, week = 52, month = 12, quarter = 4)[[o$frequency]]
  mult <- o$method == "classical" && o$type == "multiplicative"
  lambda <- if (o$transform == "log") 0 else if (o$transform == "box_cox") o$box_cox_lambda else NULL
  z <- if (is.null(lambda)) y else as.numeric(BoxCox(y, lambda))
  if (o$method == "stl") {
    f <- stats::stl(stats::ts(z, frequency = periods), s.window = if (o$s_window == "periodic") "periodic" else odd(o$s_window),
                    t.window = if (o$t_window == "auto") NULL else odd(o$t_window), robust = o$robust)
    trend <- as.numeric(f$time.series[, "trend"]); S <- list(as.numeric(f$time.series[, "seasonal"]))
    r <- as.numeric(f$time.series[, "remainder"]); zero <- if (o$robust) sum(f$weights == 0) else NULL
    sw <- if (o$s_window == "periodic") 10L * n + 1L else odd(o$s_window)
    tw <- if (o$t_window == "auto") nextodd(ceiling(1.5 * periods / (1 - 1.5 / sw))) else odd(o$t_window)
  } else if (o$method == "classical") {
    f <- stats::decompose(stats::ts(z, frequency = periods), type = o$type)
    trend <- as.numeric(f$trend); S <- list(as.numeric(f$seasonal)); r <- as.numeric(f$random); zero <- NULL
  } else {
    sws <- if (is.null(o$s_windows)) 7 + 4 * seq_along(periods) else unlist(o$s_windows)
    args <- list(forecast::msts(z, seasonal.periods = periods), iterate = o$iterate, s.window = sws)
    if (o$robust) args$robust <- TRUE
    f <- do.call(forecast::mstl, args)
    trend <- as.numeric(f[, "Trend"]); S <- lapply(seq_along(periods), function(i) as.numeric(f[, 2L + i])); r <- as.numeric(f[, "Remainder"])
    zero <- NULL
    if (o$robust) {
      last <- length(periods)
      g <- stats::stl(stats::ts(z - Reduce(`+`, S[-last], 0), frequency = periods[[last]]), s.window = sws[[last]], robust = TRUE)
      zero <- sum(g$weights == 0)
    }
    tw <- nextodd(ceiling(1.5 * periods[[length(periods)]] / (1 - 1.5 / sws[[length(sws)]])))
  }
  deseason <- if (mult) z / S[[1L]] else z - Reduce(`+`, S)
  if (!is.null(lambda)) deseason <- as.numeric(InvBoxCox(deseason, lambda))
  ex <- list()
  num <- function(key, value, rel = NULL) ex[[length(ex) + 1L]] <<- c(list(key = key, value = unname(value)), if (!is.null(rel)) list(rel = rel))
  txt <- function(key, text) ex[[length(ex) + 1L]] <<- list(key = key, text = text)
  # the model
  num("model|||n", n)
  if (!is.null(lambda)) num("model|||box_cox_lambda", lambda)
  if (o$method != "classical") num("model|||t_window_used", tw)
  # the seasons
  for (i in seq_along(periods)) {
    p <- periods[[i]]
    cyc <- (seq_len(n) - 1L) %% p + 1L
    # the calendar names the positions of a week (1 = Monday) from the first date's weekday; the others count from the first instant
    pos <- if (o$frequency == "day" && p == 7) (((seq_len(n) - 1L) + (as.POSIXlt(d[[1L]][[1L]])$wday + 6L)) %% 7L) + 1L else cyc
    mean_shape <- tapply(S[[i]], pos, mean)
    hi <- which.max(mean_shape); lo <- which.min(mean_shape)
    num(paste0("season|", p, "||cycles"), n %/% p)
    num(paste0("season|", p, "||peak_value"), mean_shape[[hi]])
    num(paste0("season|", p, "||trough_value"), mean_shape[[lo]])
    num(paste0("season|", p, "||amplitude"), if (mult) mean_shape[[hi]] / mean_shape[[lo]] else mean_shape[[hi]] - mean_shape[[lo]])
    num(paste0("season|", p, "||peak_position"), hi)
    num(paste0("season|", p, "||trough_position"), lo)
  }
  # the checks of the remainder
  rr <- r[!is.na(r)]
  nr <- length(rr)
  h <- if (o$lb_lag == "auto") as.integer(min(1000, min(2 * max(periods), nr %/% 5))) else as.integer(o$lb_lag)
  verdict <- list()
  for (p in periods) {
    s <- seasonality(z, p, o$seasonality_test, mult)
    key <- paste0("assumption|seasonality|", p, "|")
      if (!is.null(s$p)) {
      num(paste0(key, "statistic"), s$statistic); num(paste0(key, "p_value"), s$p)
      txt(paste0(key, "verdict"), if (s$p < o$alpha) "ok" else "failed")
    } else txt(paste0(key, "verdict"), "not_computed")
    verdict[[paste0("s", p)]] <- s
  }
  lb <- stats::Box.test(rr, lag = h, type = "Ljung-Box")
  p_lb <- stats::pchisq(unname(lb$statistic), h, lower.tail = FALSE)
  a <- stats::acf(rr, lag.max = h, plot = FALSE)$acf[-1L]
  num("assumption|ljung_box||statistic", lb$statistic); num("assumption|ljung_box||p_value", p_lb)
  txt("assumption|ljung_box||verdict", if (p_lb < o$alpha) "failed" else "ok")
  num("assumption|largest_acf||value", max(abs(a))); num("assumption|largest_acf||lag", which.max(abs(a)))
  k <- floor((nr - 1)^(1 / 3))
  ur <- urca::ur.df(rr, type = "drift", lags = k, selectlags = "Fixed")
  tau <- unname(ur@teststat[1L, "tau2"])
  p_adf <- urca::punitroot(tau, N = nr - k - 1L, trend = "c", statistic = "t")
  num("assumption|adf||statistic", tau); num("assumption|adf||p_value", p_adf)
  txt("assumption|adf||verdict", if (p_adf < o$alpha) "ok" else "failed")
  kp <- suppressWarnings(tseries::kpss.test(rr, null = "Level", lshort = TRUE))
  num("assumption|kpss||statistic", kp$statistic); num("assumption|kpss||p_value", kp$p.value)
  txt("assumption|kpss||verdict", if (kp$p.value < o$alpha) "failed" else "ok")
  lg <- if (mult) log else identity
  num("assumption|trend_strength||value", strength(lg(trend), lg(r)))
  for (i in seq_along(periods)) num(paste0("assumption|seasonal_strength|", periods[[i]], "|value"), strength(lg(S[[i]]), lg(r)))
  if (!is.null(zero)) num("assumption|robust_weights||value", zero)
  # a few points of each curve: (instants, 0-based)
  at <- points %||% round(c(0.15, 0.5, 0.85) * (n - 1))
  at <- unique(at)
  # a point's index counts the kept points: the empty ends of a classical decomposition are not there
  curve <- function(chart, name, v) for (j in at) if (!is.na(v[[j + 1L]])) num(paste0("curve|", chart, "|", name, "|y|", sum(!is.na(v[seq_len(j + 1L)])) - 1L), v[[j + 1L]], 1e-12)
  curve("components", "trend", trend)
  for (i in seq_along(periods)) curve("components", paste0("seasonal_", periods[[i]]), S[[i]])
  curve("components", "remainder", r)
  if (!is.null(lambda) || mult) curve("adjusted", "deseasonalized", deseason)
  # the explanation: the template ids, row by row
  ids <- list()
  say <- function(block, level_2, id) ids[[length(ids) + 1L]] <<- list(paste0("reading|", block, "|", level_2, "|line_", sum(vapply(ids, function(x) startsWith(x[[1L]], paste0("reading|", block, "|", level_2, "|")), TRUE)) + 1L), id)
  say("summary", "", paste0("summary_", o$method))
  if (o$transform == "log") say("summary", "", "transform_log") else if (o$transform == "box_cox") say("summary", "", "transform_box_cox")
  if (!is.null(zero) && zero > 0) say("summary", "", "robust_weights")
  say("strength", "", "strength")
  for (p in periods) {
    s <- verdict[[paste0("s", p)]]
    say("seasonality", p, if (is.null(s$p)) "seasonality_not_computed" else if (s$p < o$alpha) "seasonality_supported" else "seasonality_not_supported")
  }
  for (p in periods) say("season", p, if (mult) "season_shape_ratio" else "season_shape")
  say("remainder", "", if (p_lb < o$alpha) "remainder_structure" else "remainder_white")
  if (p_lb < o$alpha && max(abs(a)) < qnorm(1 - o$alpha / 2) / sqrt(nr)) say("remainder", "", "remainder_large_n")
  eta <- unname(kp$statistic)
  side <- if (eta > 0.739) "below" else if (eta < 0.347) "above" else "inside"
  kpss_ok <- kp$p.value >= o$alpha
  sid <- if (p_adf < o$alpha && kpss_ok) "stationarity_both_ok" else if (p_adf < o$alpha) "stationarity_adf_only" else
    if (kpss_ok) "stationarity_kpss_only" else "stationarity_neither"
  say("stationarity", "", if (side == "inside") sid else paste0(sid, if (side == "above") "_kpss_above" else "_kpss_below"))
  say("method", "", paste0("method_", o$method))
  if (o$transform != "none") say("method", "", "method_transform")
  say("method", "", "method_seasonality"); say("method", "", "method_strength"); say("method", "", "method_remainder"); say("method", "", "method_stationarity")
  say("formulas", "", paste0("formula_", o$method))
  if (o$transform != "none") say("formulas", "", "formula_box_cox")
  say("formulas", "", "formula_strength"); say("formulas", "", if (o$seasonality_test == "friedman") "formula_friedman" else "formula_kruskal_wallis")
  say("formulas", "", "formula_ljung_box"); say("formulas", "", "formula_adf"); say("formulas", "", "formula_kpss")
  list(id = id, model = "decomposition", data = file.path("data", data), types = types, params = params, expect = ex, reading = ids,
       note = note)
}

`%||%` <- function(a, b) if (is.null(a)) b else a

M_TYPES <- list(month = "date", sales = "number")
X_TYPES <- list(month = "date", boxes = "number")
cases <- list(
  decomp_case("dec_stl_monthly", "dec_monthly_additive.csv", M_TYPES, list(time = "month", value = "sales", frequency = "month", method = "stl")),
  decomp_case("dec_stl_periodic_robust_kw", "dec_monthly_additive.csv", M_TYPES,
              list(time = "month", value = "sales", frequency = "month", method = "stl", s_window = "periodic", robust = TRUE,
                   seasonality_test = "kruskal_wallis")),
  decomp_case("dec_classical_multiplicative", "dec_monthly_multiplicative.csv", X_TYPES,
              list(time = "month", value = "boxes", frequency = "month", method = "classical", type = "multiplicative")),
  decomp_case("dec_stl_log_robust", "dec_monthly_multiplicative.csv", X_TYPES,
              list(time = "month", value = "boxes", frequency = "month", method = "stl", transform = "log", robust = TRUE)),
  decomp_case("dec_stl_box_cox", "dec_monthly_multiplicative.csv", X_TYPES,
              list(time = "month", value = "boxes", frequency = "month", method = "stl", transform = "box_cox", box_cox_lambda = 0.5)),
  decomp_case("dec_mstl_daily", "dec_daily_two.csv", list(day = "date", orders = "number"),
              list(time = "day", value = "orders", frequency = "day", method = "mstl", periods = list(7, 365), robust = TRUE),
              points = c(10, 500, 1090)),
  decomp_case("dec_stl_no_season", "dec_no_season.csv", list(month = "date", level = "number"),
              list(time = "month", value = "level", frequency = "month", method = "stl"))
)
refusal <- function(id, data, types, params, code, message) {
  list(id = id, model = "decomposition", data = file.path("data", data), types = types, params = params, expect = list(),
       refuses = list(code = code, message = message))
}
N <- list(month = "date", level = "number")
cases <- c(cases, list(
  refusal("dec_refuse_too_short", "dec_no_season.csv", N,
          list(time = "month", value = "level", frequency = "month", method = "mstl", periods = list(12, 150)), "too_short",
          "the series has 200 months; a period of 150 needs more than 300: drop it, or a longer series"),
  refusal("dec_refuse_windows_count", "dec_no_season.csv", N,
          list(time = "month", value = "level", frequency = "month", method = "mstl", periods = list(6, 12), s_windows = list(11)), "windows_count",
          "give one seasonal window per period (2 periods, 1 window), or none"),
  refusal("dec_refuse_not_positive", "dec_no_season.csv", N,
          list(time = "month", value = "level", frequency = "month", method = "classical", type = "multiplicative"), "not_positive",
          "the column «level» holds 0 or a negative value: a multiplicative decomposition needs positive values; use type = additive"),
  refusal("dec_refuse_multiplicative_transform", "dec_monthly_multiplicative.csv", X_TYPES,
          list(time = "month", value = "boxes", frequency = "month", method = "classical", type = "multiplicative", transform = "log"),
          "multiplicative_transform",
          "a multiplicative decomposition already works in ratios: choose no transform, or an additive decomposition of the log"),
  refusal("dec_refuse_period_needed", "dec_no_season.csv", N,
          list(time = "month", value = "level", frequency = "year", method = "stl"), "period_needed",
          "a yearly or index series has no season of its own: give the period, in steps ('period')")
))

# ── the file: compact items, merged by id ───────────────────────────────────────────────────────────────────────
j <- function(x) as.character(toJSON(x, auto_unbox = TRUE, null = "null", na = "null", digits = NA))
item <- function(x) paste0("  ", j(x))
case_text <- function(cs) {
  head <- c(sprintf('      "id": %s,', j(cs$id)), sprintf('      "model": %s,', j(cs$model)), sprintf('      "data": %s,', j(cs$data)),
            sprintf('      "types": %s,', j(cs$types)), sprintf('      "params": %s,', j(cs$params)))
  if (!is.null(cs$note)) head <- c(head, sprintf('      "note": %s,', j(cs$note)))
  body <- if (length(cs$expect)) c('      "expect": [', paste0(vapply(cs$expect, item, ""), c(rep(",", length(cs$expect) - 1L), "")), "      ],") else
    '      "expect": [],'
  # the explanation's ids by block: «block|level_2» → the ids of its lines, in the order the rows hold them
  blocks <- vapply(cs$reading, function(x) sub("\\|line_[0-9]+$", "", sub("^reading\\|", "", x[[1L]])), "")
  groups <- unique(blocks)
  lines <- vapply(groups, function(g) paste0("  ", j(g), ": ", j(lapply(cs$reading[blocks == g], `[[`, 2L))), "")
  tail <- c(if (!is.null(cs$reading)) c('      "reading": {', paste0(lines, c(rep(",", length(lines) - 1L), "")), "      },"),
            sprintf('      "refuses": %s', if (is.null(cs$refuses)) "null" else j(cs$refuses)))
  c("    {", head, body, tail, "    }")
}
path <- file.path(here, "expected.json")
txt <- readLines(path, encoding = "UTF-8")
start <- grep('^  "cases": \\[$', txt)
stop_at <- max(grep("^  \\]$", txt))
body <- txt[(start + 1L):(stop_at - 1L)]
begin <- which(body == "    {")
finish <- which(body %in% c("    }", "    },"))
chunks <- Map(function(a, b) { x <- body[a:b]; x[length(x)] <- "    }"; x }, begin, finish)
ids <- vapply(chunks, function(x) sub('^ *"id": "([^"]*)",?$', "\\1", x[[2L]]), "")
mine <- vapply(cases, `[[`, "", "id")
keep <- chunks[!ids %in% mine & !startsWith(ids, "dec_")]
all <- c(keep, lapply(cases, case_text))
out <- c(txt[seq_len(start)], unlist(Map(function(x, last) if (last) x else { x[length(x)] <- "    },"; x }, all, seq_along(all) == length(all))),
         txt[stop_at:length(txt)])
writeLines(out, path, useBytes = TRUE)
cat(length(cases), "cases merged into expected.json (", length(keep), "others kept)\n")
