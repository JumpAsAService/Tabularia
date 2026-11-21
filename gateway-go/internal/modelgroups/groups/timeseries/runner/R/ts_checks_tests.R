# The self-test of the checks (ts_checks.R, lot D1): added to TAB_COMMON_TESTS. The witnesses are
# reference/data/series_witness.json (make_witness_series.py: statsmodels, SciPy, numpy) on the columns of
# reference/data/dec_checks.csv; each verdict is tested on both sides of the p-value that decides it.

.ts_checks_frame <- function(code) {
  tab_read_csv(file.path(code, "reference", "data", "dec_checks.csv"),
               list(day = "date", white = "number", ar = "number", walk = "number", weekly = "number", noseason = "number",
                    mild = "number"))
}

TAB_COMMON_TESTS[["timeseries: Ljung–Box, ADF and KPSS equal statsmodels' on the remainder's checks (dec_checks)"]] <- function(code) {
  w <- .ts_witness(code)$checks
  d <- .ts_checks_frame(code)
  for (col in names(w)) {
    x <- d[[col]]
    for (h in c(10L, 14L)) {
      lb <- ts_ljung_box(x, h, 0.05)
      want <- w[[col]]$ljung_box[[as.character(h)]]
      .tab_expect(.ts_near(lb$statistic, want[[1L]]) && .ts_near(lb$p_value, want[[2L]], abs = 1e-300) && lb$df == h,
                  sprintf("%s: Ljung–Box at %d %.17g p %.17g, statsmodels %.17g p %.17g", col, h, lb$statistic, lb$p_value, want[[1L]], want[[2L]]))
      # the statistic is stats::Box.test's to the bit, its p-value the upper tail Box.test computes as 1 − pchisq
      b <- stats::Box.test(x, lag = h, type = "Ljung-Box")
      .tab_expect(identical(lb$statistic, unname(b$statistic)) && abs(lb$p_value - b$p.value) <= 1e-15, col, ": Box.test")
      big <- ts_largest_acf(ts_acf(x, h), h)
      want <- w[[col]]$largest_acf[[as.character(h)]]
      .tab_expect(.ts_near(big$value, want[[1L]]) && big$lag == want[[2L]] && identical(big$kind, "measure"), col, ": the largest autocorrelation")
    }
    a <- ts_adf(x, 0.05)
    want <- w[[col]]$adf
    .tab_expect(.ts_near(a$statistic, want$tau, rel = 1e-10) && a$count == want$nobs && a$lag == want$k,
                sprintf("%s: ADF τ %.17g on %d, statsmodels %.17g on %d", col, a$statistic, a$count, want$tau, want$nobs))
    # τ is urca::ur.df's tau2 to the bit
    ur <- urca::ur.df(x, type = "drift", lags = want$k, selectlags = "Fixed")@teststat[1L, "tau2"]
    .tab_expect(identical(a$statistic, unname(ur)), sprintf("%s: τ %.17g, ur.df %.17g", col, a$statistic, ur))
    .tab_expect(identical(a$p_value, urca::punitroot(a$statistic, N = want$nobs, trend = "c", statistic = "t")), col, ": punitroot")
    k <- ts_kpss(x, 0.05)
    want <- w[[col]]$kpss
    .tab_expect(.ts_near(k$statistic, want$eta, rel = 1e-10) && k$lag == want$lags,
                sprintf("%s: KPSS η %.17g lags %d, statsmodels %.17g lags %d", col, k$statistic, k$lag, want$eta, want$lags))
    if (!identical(k$verdict, "not_computed")) .tab_expect(.ts_near(k$p_value, want$p, rel = 1e-10), col, ": KPSS p ", k$p_value, " against ", want$p)
  }
}

TAB_COMMON_TESTS[["timeseries: ADF's p-value is MacKinnon's: at his 2010 finite-sample critical values punitroot gives back 1 %, 5 %, 10 %"]] <- function(code) {
  w <- .ts_witness(code)$checks
  for (col in names(w)) {
    a <- w[[col]]$adf
    got <- vapply(unlist(a$crit_nobs), function(q) urca::punitroot(q, N = a$nobs, trend = "c", statistic = "t"), 0)
    # MacKinnon's 1996 response surfaces (urca) against his 2010 critical values (statsmodels): two estimations of
    # the same distribution, close, not equal
    .tab_expect(all(abs(got - c(0.01, 0.05, 0.10)) <= TS_MACKINNON_GAP * c(0.01, 0.05, 0.10)),
                sprintf("%s on %d: %s", col, a$nobs, paste(sprintf("%.6f", got), collapse = " ")))
  }
}

# The largest relative gap between urca::punitroot at MacKinnon (2010)'s critical values and their nominal levels,
# measured on dec_checks (n 392): 7.6e-4 at most, «As built / D1».
TS_MACKINNON_GAP <- 0.002

TAB_COMMON_TESTS[["timeseries: Friedman and Kruskal–Wallis on the detrended series equal SciPy's (dec_checks)"]] <- function(code) {
  d <- .ts_checks_frame(code)
  cases <- .ts_witness(code)$seasonality
  for (key in names(cases)) {
    case <- cases[[key]]
    col <- sub("_[0-9]+$", "", key)
    for (test in c("friedman", "kruskal_wallis")) {
      s <- ts_seasonality(d[[col]], case$period, test, 0.05)
      want <- case[[test]]
      .tab_expect(.ts_near(s$statistic, want[[1L]]) && .ts_near(s$p_value, want[[2L]], abs = 1e-300) && s$count == case$cycles &&
                  s$df == case$period - 1L && identical(s$level_2, as.character(case$period)),
                  sprintf("%s %d %s: %.17g p %.17g on %d cycles, SciPy %.17g p %.17g on %d", col, case$period, test, s$statistic,
                          s$p_value, s$count, want[[1L]], want[[2L]], case$cycles))
    }
  }
  # the blocks ranked at once equal stats::friedman.test, ties included
  set.seed(5)
  for (m in list(matrix(stats::rnorm(70), 10, 7), matrix(round(stats::rnorm(120), 0), 10, 12), matrix(stats::rnorm(6), 3, 2))) {
    f <- ts_friedman(m)
    r <- stats::friedman.test(m)
    .tab_expect(.ts_near(f$statistic, unname(r$statistic)) && .ts_near(f$p.value, r$p.value, abs = 1e-300) && f$parameter == r$parameter,
                sprintf("friedman.test %.17g, by blocks %.17g", r$statistic, f$statistic))
  }
  # the moving average is stats::decompose's trend to the bit, odd and even
  x <- d$weekly
  for (p in c(7L, 12L)) .tab_expect(identical(ts_ma(x, p), as.numeric(stats::decompose(stats::ts(x, frequency = p))$trend)), "ts_ma ", p)
}

TAB_COMMON_TESTS[["timeseries: the strengths equal statsmodels' decomposition read by numpy, and are measures"]] <- function(code) {
  want <- .ts_witness(code)$strengths$weekly_7
  x <- .ts_checks_frame(code)$weekly
  dec <- stats::decompose(stats::ts(x, frequency = 7))
  ft <- ts_strength(as.numeric(dec$trend), as.numeric(dec$random))
  fs <- ts_strength(as.numeric(dec$seasonal), as.numeric(dec$random))
  .tab_expect(.ts_near(ft, want$trend) && .ts_near(fs, want$seasonal), sprintf("F_T %.17g F_S %.17g, numpy %.17g %.17g", ft, fs, want$trend, want$seasonal))
  .tab_expect(is.na(ts_strength(rep(1, 10), rep(0, 10))) && ts_strength(c(1, 2, 3, 4), c(5, -5, 5, -5)) == 0, "not computed; at least 0")
  m <- ts_measure("trend_strength", ft)
  rows <- tab_rows()
  ts_check_rows(rows, m)
  f <- tab_results_frame(rows)
  .tab_expect(identical(f$key, "assumption|trend_strength||value"), "a measure's rows: ", paste(f$key, collapse = ", "))
}

TAB_COMMON_TESTS[["timeseries: KPSS at the ends of its table answers the alpha it can and is not computed for the others"]] <- function(code) {
  # tseries still reads the table TS_KPSS_LEVEL holds
  tab <- TS_KPSS_LEVEL
  set.seed(9)
  e <- stats::rnorm(200)
  k <- suppressWarnings(tseries::kpss.test(e, null = "Level", lshort = TRUE))
  .tab_expect(identical(unname(k$parameter), floor(4 * (200 / 100)^0.25)), "tseries' lags are ⌊4 (n/100)^¼⌋")
  for (i in seq_along(tab$statistic)) {
    .tab_expect(abs(stats::approx(tab$statistic, tab$p, tab$statistic[[i]], rule = 2)$y - tab$p[[i]]) < 1e-15, "the table")
  }
  d <- .ts_checks_frame(code)
  g <- function(x, alpha) ts_kpss(x, alpha)
  # the walk: η above 0.739, p ≤ 0.01
  .tab_expect(identical(g(d$walk, 0.05)$verdict, "failed") && identical(g(d$walk, 0.01)$verdict, "failed") &&
              identical(g(d$walk, 0.05)$p_side, "below"), "p ≤ 0.01 rejects at 0.05 and at 0.01")
  nc <- g(d$walk, 0.001)
  .tab_expect(identical(nc$verdict, "not_computed") && identical(nc$advice, "the KPSS table gives p ≤ 0.01; it cannot say whether p < 0.001") &&
              identical(nc$advice_id, "kpss_table_end") && is.null(nc$p_value) && is.finite(nc$statistic), "p ≤ 0.01 at 0.001: ", nc$advice)
  # white noise: η below 0.347, p ≥ 0.10
  .tab_expect(identical(g(d$white, 0.05)$verdict, "ok") && identical(g(d$white, 0.10)$verdict, "ok") &&
              identical(g(d$white, 0.10)$p_side, "above"), "p ≥ 0.10 keeps at 0.05 and at 0.10")
  nc <- g(d$white, 0.2)
  .tab_expect(identical(nc$verdict, "not_computed") && identical(nc$advice, "the KPSS table gives p ≥ 0.10; it cannot say whether p < 0.2"),
              "p ≥ 0.10 at 0.2: ", nc$advice)
  # inside the table: p against alpha, both sides (mild: p 0.068)
  p <- g(d$mild, 0.05)$p_value
  .tab_expect(p > 0.01 && p < 0.10 && identical(g(d$mild, p * 1.001)$verdict, "failed") && identical(g(d$mild, p * 0.999)$verdict, "ok") &&
              identical(g(d$mild, 0.2)$verdict, "failed") && identical(g(d$mild, 0.001)$verdict, "ok"), "inside the table: p ", p)
}

TAB_COMMON_TESTS[["timeseries: every check's verdict is its p-value against alpha, on both sides; what cannot be tested is not computed"]] <- function(code) {
  d <- .ts_checks_frame(code)
  both <- function(f, ok_below) {
    p <- f(0.05)$p_value
    lo <- f(p * 0.999)$verdict
    hi <- f(min(0.9999, p * 1.001))$verdict
    if (ok_below) identical(c(lo, hi), c("failed", "ok")) else identical(c(lo, hi), c("ok", "failed"))
  }
  # Ljung–Box and KPSS fail when p < alpha; ADF and the seasonality test pass (they reject what would be wrong)
  .tab_expect(both(function(a) ts_ljung_box(d$mild, 10L, a), FALSE), "Ljung–Box")
  .tab_expect(both(function(a) ts_adf(d$walk, a), TRUE), "ADF")
  .tab_expect(both(function(a) ts_seasonality(d$noseason, 7L, "friedman", a), TRUE), "Friedman")
  .tab_expect(both(function(a) ts_seasonality(d$white, 7L, "kruskal_wallis", a), TRUE), "Kruskal–Wallis")
  # the empty values at the ends of a classical remainder are left out
  x <- d$mild
  .tab_expect(identical(ts_ljung_box(c(NA, NA, x, NA), 10L, 0.05)$statistic, ts_ljung_box(x, 10L, 0.05)$statistic) &&
              identical(ts_adf(c(NA, x, NA, NA), 0.05)$statistic, ts_adf(x, 0.05)$statistic), "the ends")
  # not computed, with a sentence
  .tab_expect(identical(ts_adf(x[1:11], 0.05)$verdict, "not_computed") && identical(ts_kpss(x[1:11], 0.05)$advice_id, "kpss_short"), "too short")
  for (f in list(function(r) ts_ljung_box(r, 10L, 0.05), function(r) ts_adf(r, 0.05), function(r) ts_kpss(r, 0.05))) {
    .tab_expect(identical(f(rep(0, 50))$advice_id, "remainder_constant"), "a constant remainder")
  }
  .tab_expect(identical(ts_ljung_box(x[1:8], 10L, 0.05)$advice_id, "ljung_box_short"), "Ljung–Box's lag above the values")
  few <- ts_seasonality(x[1:15], 7L, "friedman", 0.05)
  .tab_expect(identical(few$verdict, "not_computed") && few$count == 1L && identical(few$advice_id, "seasonality_few_cycles"), "one cycle")
  .tab_expect(is.null(ts_seasonality(x, 7L, "none", 0.05)), "no seasonality test")
  .tab_expect(identical(ts_seasonality(rep(3, 70), 7L, "friedman", 0.05)$advice_id, "seasonality_constant"), "a constant series")
  # a multiplicative series is detrended by a ratio
  y <- exp(d$weekly / 10)
  s <- ts_seasonality(y, 7L, "kruskal_wallis", 0.05, type = "multiplicative")
  r <- stats::kruskal.test((y / ts_ma(y, 7L))[4:397], (3:396) %% 7L + 1L)
  .tab_expect(.ts_near(s$statistic, unname(r$statistic)), "the ratio to the moving average")
  # Ljung–Box's lag: auto = min(2·p_max, ⌊n/5⌋) with a season, min(10, ⌊n/5⌋) without, within max_lb_lags
  .tab_expect(ts_lb_lag("auto", 1111L, c(7L, 365L)) == 222L && ts_lb_lag("auto", 100L) == 10L && ts_lb_lag("auto", 30L) == 6L &&
              ts_lb_lag("auto", 200000L, 8766L) == 1000L && ts_lb_lag(25, 100L, 7L) == 25L, "the lag")
}

TAB_COMMON_TESTS[["timeseries: the checks' rows are the catalogue's (assumption, level the check, level_2 the period)"]] <- function(code) {
  d <- .ts_checks_frame(code)
  rows <- tab_rows_start(list(model = "decomposition", variant = "stl", rows_from = "run", group_version = "selftest"))
  tab_number(rows, "model", "n", nrow(d))
  checks <- list(ts_seasonality(d$weekly, 7L, "friedman", 0.05), ts_seasonality(d$noseason, 12L, "friedman", 0.05),
                 ts_ljung_box(d$mild, 14L, 0.05), ts_largest_acf(ts_acf(d$mild, 14L), 14L), ts_adf(d$walk, 0.05),
                 ts_kpss(d$walk, 0.001), ts_measure("trend_strength", 0.5), ts_measure("seasonal_strength", 0.25, level_2 = "7"))
  for (ch in checks) ts_check_rows(rows, ch)
  problems <- tab_check_declared(rows, "decomposition", "stl")
  .tab_expect(!length(problems), paste(head(problems, 3L), collapse = "; "))
  f <- tab_results_frame(rows)
  get <- function(key) f[f$key == key, , drop = FALSE]
  .tab_expect(identical(get("assumption|seasonality|7|verdict")$text, "ok") && identical(get("assumption|seasonality|12|verdict")$text, "failed") &&
              identical(get("assumption|seasonality|12|advice_id")$text, "seasonality_failed") && get("assumption|seasonality|7|count")$value == 56,
              "the seasonality rows")
  .tab_expect(identical(get("assumption|kpss||verdict")$text, "not_computed") && !nrow(get("assumption|kpss||p_value")) &&
              nrow(get("assumption|kpss||statistic")) == 1L && identical(get("assumption|ljung_box||significance")$text, "***"), "KPSS, Ljung–Box")
  measures <- f$level %in% c("largest_acf", "trend_strength", "seasonal_strength")
  .tab_expect(!any(measures & f$stat %in% c("h0", "verdict", "advice", "advice_id")) && get("assumption|largest_acf||lag")$value == 7,
              "a measure has no verdict")
}
