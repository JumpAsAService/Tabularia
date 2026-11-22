# The self-test of the volatility (GARCH) node (garch.R, ts_nyblom.R, ts_risk.R; lot G1): added to TAB_COMMON_TESTS. The reference
# cases (reference/expected.json, ids «garch_*») run by the common self-test; these check what a row table cannot: the scale's exact
# back-transform, the filter and the forecasts against rugarch's, the checks against rugarch's and FinTS's own, the bridge law, the
# output's types, the refusals, the peak memory.

.garch_frame <- function(code, file = "garch_returns.csv") {
  types <- if (file == "garch_prices.csv") list(day = "date", price = "number") else list(t = "integer", ar_t = "number", gjr = "number", brk = "number")
  tab_read_csv(file.path(code, "reference", "data", file), types)
}

.garch_task <- function(df, params) {
  params <- utils::modifyList(list(time = "t", value = "gjr", frequency = "index"), params)
  task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "timeseries", group_version = "selftest", model = "garch", variant = "default",
               params = params, output = "volatility", write_output = FALSE, rows_from = "run", limits = tab_catalog()$limits, .frame = df)
  tab_seed(task)
  task
}

.garch_rel <- function(a, b) max(abs(a - b) / pmax(abs(b), 1e-300))

TAB_COMMON_TESTS[["garch: the fit is ugarchfit's, brought back to the series' units exactly; the filter and the forecasts are rugarch's"]] <- function(code) {
  loadNamespace("rugarch")
  df <- .garch_frame(code)
  for (p in list(list(value = "ar_t", ar_order = 1, holdout = 200), list(variance_model = "gjr", distribution = "sstd", holdout = 150),
                 list(variance_model = "egarch", distribution = "norm", ma_order = 1))) {
    task <- .garch_task(df, p)
    o <- garch_params(task$params)
    d <- garch_series(task, o)
    fit <- garch_fit(o, d)
    m <- garch_model(o, d, fit)
    what <- garch_name(o)
    # the direct call: rugarch's hybrid solver on r / s
    direct <- rugarch::ugarchfit(garch_spec(o), d$r / d$scale, out.sample = o$holdout, solver = "hybrid")
    .tab_expect(.garch_rel(m$est, rugarch::coef(direct)) < 1e-6, what, ": the estimates are not ugarchfit's")
    # the back-transform: rugarch's likelihood of the returns themselves at the parameters reported is ℓ(r / s) − n ln s, and its
    # σ_t is s times the fit's
    spec <- garch_spec(o, as.list(m$theta))
    flt <- rugarch::ugarchfilter(spec, d$r, n.old = d$train)
    ll <- sum(flt@filter$log.likelihoods[seq_len(d$train)])
    .tab_expect(abs(ll - m$log_lik) < 1e-8 * abs(m$log_lik), what, ": the log-likelihood on the series' units is ", ll, ", not ", m$log_lik)
    .tab_expect(.garch_rel(as.numeric(rugarch::sigma(flt)), m$sigma) < 1e-8, what, ": σ_t on the series' units")
    # the held-out σ and the forecast after the last instant: rugarch's rolling forecasts with the parameters fixed
    k <- o$holdout
    fc <- rugarch::ugarchforecast(fit, n.ahead = 1L, n.roll = k)
    .tab_expect(k == 0L || .garch_rel(as.numeric(rugarch::sigma(fc))[seq_len(k)] * d$scale, m$sigma[d$train + seq_len(k)]) < 1e-10,
                what, ": the holdout's σ is not the rolling forecast's")
    # the forecast after T (garch_forecast): rugarch's from the same state; for GJR with the skewed t from the second step on rugarch
    # weighs γ by P(z < 0), the runner by E[z² 1(z < 0)] (garch_kappa), so only the first step is rugarch's there
    fc <- rugarch::ugarchforecast(fit, n.ahead = 5L, n.roll = k)
    steps <- if (o$variance_model == "gjr" && o$distribution == "sstd") 1L else 1:5
    ours <- garch_forecast(o, m$theta, m$kappa, m$eabs, d$r, m$mu, m$sigma, 5L)
    .tab_expect(.garch_rel(ours$sigma[steps], rugarch::sigma(fc)[steps, k + 1L] * d$scale) < 1e-10 &&
                  max(abs(ours$mean - rugarch::fitted(fc)[, k + 1L] * d$scale)) < 1e-12, what, ": the forecast")
    if (o$variance_model == "gjr") {
      .tab_expect(abs(m$persistence - sum(m$theta[c("alpha1", "beta1")]) - m$kappa * m$theta[["gamma1"]]) < 1e-15 &&
                    abs(rugarch::persistence(fit) - sum(m$theta[c("alpha1", "beta1")]) -
                          rugarch:::pneg(0, m$shape, m$skew, "sstd") * m$theta[["gamma1"]]) < 1e-12, "rugarch's persistence uses P(z < 0)")
    }
    # the covariance: J V Jᵀ, so a standard error of a parameter multiplied by s is s times the fit's
    H <- solve(fit@fit$hessian)
    se <- stats::setNames(sqrt(diag(if (o$se_type == "robust") H %*% crossprod(fit@fit$scores) %*% H else fit@fit$cvar)), names(m$est))
    .tab_expect(abs(m$coef$std_error[m$coef$term == "alpha1"] - se[["alpha1"]]) < 1e-12 &&
                  abs(m$coef$std_error[m$coef$term == "mu"] - d$scale * se[["mu"]]) < 1e-12 * d$scale * se[["mu"]],
                what, ": the standard errors are not J V Jᵀ's")
  }
}

TAB_COMMON_TESTS[["garch: Nyblom's statistics are rugarch::nyblom's; the bridge law gives Anderson–Darling's quantiles and Hansen's table back"]] <- function(code) {
  df <- .garch_frame(code)
  task <- .garch_task(df, list(value = "brk", distribution = "norm"))
  o <- garch_params(task$params)
  d <- garch_series(task, o)
  fit <- garch_fit(o, d)
  ny <- rugarch::nyblom(fit)
  mine <- nyblom_test(fit@fit$scores, names(rugarch::coef(fit)), 0.05)
  .tab_expect(abs(mine$joint$statistic - ny$JointStat) < 1e-10, "the joint statistic")
  .tab_expect(.garch_rel(vapply(mine$parts, `[[`, 0, "statistic"), as.numeric(ny$IndividualStat)) < 1e-10, "the individual statistics")
  # the mean's break at the 401st instant: the joint test rejects, and μ is the parameter that moves
  .tab_expect(mine$joint$verdict == "failed" && mine$parts[[1L]]$verdict == "failed", "the break is not seen: p = ", mine$joint$p_value)
  # ∫B² of one bridge: Anderson & Darling's (1952) exact quantiles 0.34730, 0.46136, 0.74346 (10 %, 5 %, 1 %), to their 5 digits
  for (i in 1:3) {
    q <- c(0.34730, 0.46136, 0.74346)[[i]]
    p <- c(0.10, 0.05, 0.01)[[i]]
    .tab_expect(nyblom_p(q + 5e-6, 1) <= p && nyblom_p(q - 5e-6, 1) >= p, "P(W_1 > ", q, ") is ", nyblom_p(q, 1))
  }
  # Hansen's (1992) critical values for k = 1…10 as rugarch holds them: simulated on finite samples and written with two or three
  # digits, so the law's p at them is the level within 12 % of it (measured: 3.5 % at 10 %, 5 % at 5 %, 10 % at 1 %)
  for (k in 1:10) {
    cv <- rugarch:::.nyblomCritical(k)
    for (i in 1:3) {
      lvl <- c(0.10, 0.05, 0.01)[[i]]
      .tab_expect(abs(nyblom_p(cv[[i]], k) - lvl) <= 0.12 * lvl, "k = ", k, ": ", cv[[i]], " gives p ", nyblom_p(cv[[i]], k))
    }
  }
}

TAB_COMMON_TESTS[["garch: the weighted portmanteau is rugarch's, the sign bias with ε is rugarch::signbias's, the ARCH-LM is FinTS's"]] <- function(code) {
  df <- .garch_frame(code)
  task <- .garch_task(df, list(variance_model = "gjr", distribution = "sstd"))
  o <- garch_params(task$params)
  d <- garch_series(task, o)
  fit <- garch_fit(o, d)
  z <- fit@fit$z
  for (K in 0:2) for (sq in c(FALSE, TRUE)) {
    w <- rugarch:::Weighted.Box.test(z, lag = 12, type = "Ljung-Box", fitdf = K, sqrd.res = sq)
    mine <- garch_weighted_lb(if (sq) "ljung_box_z2" else "ljung_box_z", if (sq) z^2 else z, 12L, if (sq) 0L else K, 0.05)
    .tab_expect(abs(mine$statistic - w$statistic) < 1e-10 * w$statistic && abs(mine$p_value - w$p.value) < 1e-12, "the weighted test, K = ", K)
  }
  .tab_expect(garch_weighted_lb("ljung_box_z", z, 5L, 3L, 0.05)$verdict == "not_computed", "5 lags with 3 coefficients")
  sb <- rugarch::signbias(fit)
  mine <- garch_sign_bias(z, 0.05, "gjr", x = as.numeric(fit@fit$residuals))
  .tab_expect(abs(mine$statistic - sb[4L, 1L]) < 1e-8 * sb[4L, 1L] && abs(mine$p_value - sb[4L, 2L]) < 1e-10, "the sign bias's joint effect")
  a <- FinTS::ArchTest(z, lags = 12)
  ch <- garch_checks(task, o, d, garch_model(o, d, fit))
  lm <- Filter(function(x) identical(x$id, "arch_lm"), ch)[[1L]]
  .tab_expect(identical(lm$statistic, unname(a$statistic)) && identical(lm$p_value, a$p.value), "the ARCH-LM")
}

TAB_COMMON_TESTS[["garch: Kupiec's and Christoffersen's tests are rugarch::VaRTest's and the definition's; not computed without a violation"]] <- function(code) {
  set.seed(5)
  r <- stats::runif(250, -2, 2)
  var <- rep(stats::qnorm(0.01), 250)
  var[c(40, 41, 120, 200, 201, 202)] <- 10  # six violations, two clusters
  bt <- ts_backtest(r, var, 0.99, 0.05)
  v <- rugarch::VaRTest(0.01, r, var, 0.95)
  .tab_expect(abs(bt$tests$kupiec$statistic - v$uc.LRstat) < 1e-10, "Kupiec: ", bt$tests$kupiec$statistic, " ", v$uc.LRstat)
  ind <- v$cc.LRstat - v$uc.LRstat  # rugarch: cc = uc on T days + ind
  .tab_expect(abs(bt$tests$christoffersen_ind$statistic - ind) < 1e-10, "independence")
  x <- bt$counts[["n01"]] + bt$counts[["n11"]]
  uc1 <- -2 * ((249 - x) * log(0.99) + x * log(0.01)) + 2 * ((249 - x) * log(1 - x / 249) + x * log(x / 249))
  .tab_expect(abs(bt$tests$christoffersen_cc$statistic - (uc1 + ind)) < 1e-10, "conditional coverage: uc on the T − 1 transitions + ind")
  .tab_expect(bt$counts[["violations"]] == 6 && bt$counts[["longest_run"]] == 3 && abs(bt$counts[["expected_violations"]] - 2.5) < 1e-12, "the counts")
  none <- ts_backtest(r, rep(-100, 250), 0.99, 0.05)
  .tab_expect(none$tests$kupiec$verdict != "not_computed" && none$tests$christoffersen_ind$verdict == "not_computed" &&
                none$tests$christoffersen_cc$verdict == "not_computed", "no violation")
  .tab_expect(all(vapply(ts_backtest(1, 0, 0.99, 0.05)$tests, `[[`, "", "verdict") == "not_computed"), "one day")
}

TAB_COMMON_TESTS[["garch: the volatility output keeps the time's own type (Int64 above 2^31, Int32, date32, timestamp UTC) and the catalogue's columns"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  raw <- tab_ref_table(file.path(code, "reference", "data", "dec_types.csv"), colClasses = "character", encoding = "UTF-8")
  value <- .garch_frame(code)$gjr[seq_len(nrow(raw))]
  types <- list(int64 = list(bit64::as.integer64(raw$t_int64), arrow::int64(), "day"), int32 = list(as.integer(raw$t_int), arrow::int32(), "day"),
                date32 = list(as.Date(raw$t_date), arrow::date32(), "index"), ts = list(as.POSIXct(raw$t_hour, tz = "UTC"), arrow::timestamp("us", "UTC"), "hour"))
  path <- tempfile(fileext = ".parquet")
  on.exit({ Sys.unsetenv("TABULARIA_INPUT"); unlink(path) }, add = TRUE)
  want <- tab_variant_spec("garch", "default")$outputs$volatility
  for (name in names(types)) {
    ty <- types[[name]]
    arrow::write_parquet(arrow::arrow_table(t = arrow::Array$create(ty[[1L]])$cast(ty[[2L]]), v = arrow::Array$create(value)), path)
    Sys.setenv(TABULARIA_INPUT = path)
    task <- .garch_task(NULL, list(value = "v", frequency = ty[[3L]], horizon = 3, distribution = "norm"))
    out <- fit_garch(task)$output
    .tab_expect(out$schema$GetFieldByName("time")$type == ty[[2L]], name, ": the time is ", out$schema$GetFieldByName("time")$type$ToString())
    .tab_expect(identical(names(out), setdiff(unlist(want), "return")), name, ": the columns ", paste(names(out), collapse = ","))
    .tab_expect(out$num_rows == nrow(raw) + 3L, name, ": the rows")
    if (name == "int64") {
      got <- out$GetColumnByName("time")$as_vector()
      .tab_expect(identical(as.character(got[[1L]]), raw$t_int64[[1L]]) && identical(as.character(got[[103L]]), "5000000102"), "every digit of an Int64")
    }
  }
}

TAB_COMMON_TESTS[["garch: a series it cannot fit is refused with a sentence"]] <- function(code) {
  df <- .garch_frame(code)
  prices <- .garch_frame(code, "garch_prices.csv")
  run <- function(d, p) fit_garch(.garch_task(d, p))
  .tab_refused_with("too_short", run(prices, list(time = "day", value = "price", input_kind = "prices", holdout = 250)))
  .tab_refused_with("too_short", run(df, list(holdout = 500)))
  .tab_refused_with("too_short", run(df[1:99, ], list()))
  bad <- prices
  bad$price[[10L]] <- 0
  .tab_refused_with("not_positive", run(bad, list(time = "day", value = "price", input_kind = "prices")))
  gap <- df
  gap$gjr[[50L]] <- NA
  msg <- .tab_refused_with("gaps", run(gap, list()))
  .tab_expect(grepl("filter out the rows without a return", msg), msg)
  flat <- df
  flat$gjr <- 0.01
  .tab_refused_with("constant", run(flat, list()))
  tiny <- df
  tiny$gjr <- tiny$gjr * 1e-170
  .tab_refused_with("scale", run(tiny, list()))
  .tab_refused_with("duplicate_time", run(rbind(df, df[800, ]), list()))
}

# The node in a child process (a whole Rscript: R, arrow, rugarch, the fit, every check, the output), its peak resident memory
# (VmHWM) below 1 GiB. TABULARIA_GARCH_MEMORY_N sets the returns (10,000 here; lot G1 measured 100,000 with it).
TAB_COMMON_TESTS[["garch: the node at 10,000 returns peaks below 1 GiB in a child process (VmHWM)"]] <- function(code) {
  if (!file.exists("/proc/self/status")) return("skipped")
  n <- as.integer(Sys.getenv("TABULARIA_GARCH_MEMORY_N", "10000"))
  out <- tempfile(fileext = ".txt")
  err <- tempfile(fileext = ".err")
  script <- tempfile(fileext = ".R")
  on.exit(unlink(c(out, script)), add = TRUE)
  writeLines(c(
    sprintf('code <- "%s"', code),
    'for (f in sort(list.files(file.path(code, c("common", "runner"), "R"), pattern = "[.]R$", full.names = TRUE))) source(f)',
    'loadNamespace("rugarch")',
    'sp <- rugarch::ugarchspec(variance.model = list(model = "gjrGARCH"), mean.model = list(armaOrder = c(1, 0)), distribution.model = "sstd",',
    '  fixed.pars = list(mu = 2e-4, ar1 = 0.1, omega = 4e-6, alpha1 = 0.03, gamma1 = 0.1, beta1 = 0.88, skew = 0.85, shape = 7))',
    sprintf('r <- as.numeric(rugarch::fitted(rugarch::ugarchpath(sp, n.sim = %d, rseed = 9)))', n),
    'df <- data.frame(t = seq_along(r), v = r)',
    'rm(r, sp); invisible(gc())',
    'p <- list(time = "t", value = "v", frequency = "index", variance_model = "gjr", distribution = "sstd", ar_order = 1, holdout = 250)',
    'task <- list(protocol = 1L, mode = "fit", group = "timeseries", group_version = "selftest", model = "garch", variant = "default", params = p,',
    '  output = "volatility", write_output = FALSE, rows_from = "run", limits = tab_catalog(code)$limits, .frame = df)',
    'tab_seed(task)',
    't0 <- proc.time()[["elapsed"]]',
    'fit <- fit_garch(task)',
    'secs <- proc.time()[["elapsed"]] - t0',
    'hwm <- as.numeric(sub("[^0-9]*([0-9]+).*", "\\\\1", grep("^VmHWM", readLines("/proc/self/status"), value = TRUE))) * 1024',
    sprintf('writeLines(as.character(c(hwm, secs, fit$rows$n)), "%s")', out)), script)
  status <- system2(file.path(R.home("bin"), "Rscript"), c("--vanilla", script), stdout = FALSE, stderr = err,
                    env = paste0("TABULARIA_CODE_DIR=", code))
  .tab_expect(status == 0 && file.exists(out), "the child process failed: ", paste(utils::tail(readLines(err), 3L), collapse = " "))
  got <- as.numeric(readLines(out))
  message(sprintf("garch at %d returns: VmHWM %.0f MiB, %.1f s, %d rows", n, got[[1L]] / 2^20, got[[2L]], got[[3L]]))
  .tab_expect(got[[1L]] < 2^30, "VmHWM ", got[[1L]], " bytes at ", n, " returns")
}
