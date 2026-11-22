# The self-test of the risk simulation node (risk_simulation.R; lot G3): added to TAB_COMMON_TESTS. The reference cases
# (reference/expected.json, ids «risk_*», make_reference_risk.R: rugarch's ugarchpath and forecast's simulate.Arima on the same
# streams, the measures on the whole matrix) run by the common self-test; these check what a row table cannot: the recursions
# against rugarch's and forecast's given the same innovations, the node's measures against the whole matrix of its own file,
# the same bits at every batch size, the outputs' types, the refusals, the memory of a batch in a child process.

.risk_task <- function(df, params, output = "risk_summary", sim = list()) {
  task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "timeseries", group_version = "selftest", model = "risk_simulation",
               variant = tab_variant_of("risk_simulation", params), params = params, output = output, write_output = FALSE,
               rows_from = "run", limits = tab_catalog()$limits, .frame = df, .sim = sim)
  tab_seed(task)
  task
}

.risk_rel <- function(a, b, scale = pmax(abs(b), 1e-300)) max(abs(a - b) / scale)

.risk_prices <- function(code) .garch_frame(code, "garch_prices.csv")

.risk_frame <- function(code, file, types) tab_read_csv(file.path(code, "reference", "data", file), types)

# A GARCH's end state without a fit: the parameters fixed, rugarch's filter of the series on its units.
.risk_garch_fixed <- function(r, g, pars) {
  spec <- rugarch::ugarchspec(variance.model = list(model = GARCH_RUGARCH[[g$variance_model]], garchOrder = c(g$q, g$p)),
                              mean.model = list(armaOrder = c(g$ar_order, g$ma_order), include.mean = g$mean),
                              distribution.model = g$distribution, fixed.pars = pars)
  flt <- rugarch::ugarchfilter(spec, r)
  m <- list(theta = unlist(pars), mu = as.numeric(rugarch::fitted(flt)), sigma = as.numeric(rugarch::sigma(flt)),
            eabs = garch_abs_mean(g$distribution, pars$shape %||% 0, pars$skew %||% 0))
  list(spec = spec, m = m)
}

TAB_COMMON_TESTS[["risk: the GARCH recursion is rugarch::ugarchpath's given the same innovations (1e-12): ARCH, GARCH, GJR, EGARCH, an AR and an MA term"]] <- function(code) {
  loadNamespace("rugarch")
  r <- .garch_frame(code)$ar_t
  designs <- list(
    list(o = list(variance_model = "garch", ar_order = 1, ma_order = 1, distribution = "std"),
         pars = list(mu = 4e-4, ar1 = 0.15, ma1 = -0.05, omega = 3e-6, alpha1 = 0.07, beta1 = 0.9, shape = 6)),
    list(o = list(variance_model = "arch", arch_order = 2, ma_order = 1, mean = FALSE, distribution = "norm"),
         pars = list(ma1 = 0.1, omega = 8e-5, alpha1 = 0.2, alpha2 = 0.15)),
    list(o = list(variance_model = "gjr", ar_order = 2, distribution = "sstd"),
         pars = list(mu = 2e-4, ar1 = 0.1, ar2 = -0.05, omega = 4e-6, alpha1 = 0.03, gamma1 = 0.1, beta1 = 0.88, skew = 0.85, shape = 7)),
    list(o = list(variance_model = "egarch", garch_order = 2, ar_order = 1, ma_order = 2, distribution = "norm"),
         pars = list(mu = 1e-4, ar1 = 0.2, ma1 = 0.1, ma2 = -0.1, omega = -0.3, alpha1 = -0.05, beta1 = 0.6, beta2 = 0.35, gamma1 = 0.15)))
  for (de in designs) {
    g <- garch_params(de$o)
    f <- .risk_garch_fixed(r, g, de$pars)
    d <- list(r = r)
    st <- risk_garch_state(g, d, f$m)
    n <- 7L
    h <- 12L
    set.seed(3)
    Z <- matrix(stats::rnorm(n * h), nrow = h)
    mine <- risk_garch_paths(st, Z)
    M <- max(g$q, g$p, g$ar_order, g$ma_order, 1L)
    eps <- r - f$m$mu
    p <- rugarch::ugarchpath(f$spec, n.sim = h, m.sim = n, presigma = utils::tail(f$m$sigma, M), prereturns = utils::tail(r, M),
                             preresiduals = utils::tail(eps, M), custom.dist = list(name = "sample", distfit = Z))
    what <- garch_name(g)
    rr <- matrix(as.numeric(rugarch::fitted(p)), nrow = h)
    ss <- matrix(as.numeric(rugarch::sigma(p)), nrow = h)
    .tab_expect(.risk_rel(mine$value, rr, max(abs(rr))) < 1e-12, what, ": the returns differ from ugarchpath's by ",
                format(.risk_rel(mine$value, rr, max(abs(rr))), digits = 3))
    .tab_expect(.risk_rel(mine$sigma, ss) < 1e-12, what, ": σ differs from ugarchpath's by ", format(.risk_rel(mine$sigma, ss), digits = 3))
  }
}

TAB_COMMON_TESTS[["risk: the ARIMA recursion is forecast:::simulate.Arima(future = TRUE)'s given the same innovations (1e-10): AR with a mean, SARIMA with a log, a drift with Box–Cox"]] <- function(code) {
  designs <- list(
    list(file = "ar_index.csv", types = list(t = "integer", level = "number"),
         p = list(time = "t", value = "level", frequency = "index", p = 2)),
    list(file = "sarima_monthly.csv", types = list(month = "date", boxes = "number"),
         p = list(time = "month", value = "boxes", frequency = "month", p = 1, d = 1, q = 1, P = 0, D = 1, Q = 1, transform = "log", holdout = 12)),
    list(file = "drift_quarterly.csv", types = list(q = "integer", revenue = "number"),
         p = list(time = "q", value = "revenue", frequency = "quarter", p = 1, d = 1, q = 1, drift = TRUE, transform = "box_cox")))
  for (de in designs) {
    df <- .risk_frame(code, de$file, de$types)
    task <- .risk_task(df, c(list(model = "arima", horizon = 9), de$p))
    prep <- arima_prepare(task)
    a <- prep$params
    core <- arima_fit_core(prep, list(p = a$p, d = a$d, q = a$q, P = a$P, D = a$D, Q = a$Q))
    refit <- arima_call(core$model, prep, prep$span, fixed = core$model$coef)
    refit$sigma2 <- core$model$sigma2
    st <- risk_arima_state(prep, core$model, refit)
    set.seed(4)
    E <- matrix(stats::rnorm(9 * 6, 0, sqrt(core$model$sigma2)), nrow = 9)
    mine <- risk_arima_paths(st, E)
    xreg <- if (core$model$drift) cbind(xdrift = as.numeric(prep$drift_index[prep$future])) else NULL
    want <- vapply(1:6, function(j) as.numeric(forecast:::simulate.Arima(refit, nsim = 9, future = TRUE, innov = E[, j], xreg = xreg)), numeric(9))
    .tab_expect(.risk_rel(mine, want, max(abs(want))) < 1e-10, core$name, ": the paths differ from simulate.Arima's by ",
                format(.risk_rel(mine, want, max(abs(want))), digits = 3))
  }
}

TAB_COMMON_TESTS[["risk: the node's measures are the whole matrix's of its own paths (VaR, ES, a price threshold, drawdown, prices), at every batch size the same bits"]] <- function(code) {
  if (!requireNamespace("arrow", quietly = TRUE)) return("skipped")
  prices <- .risk_prices(code)
  p <- list(model = "garch", time = "day", value = "price", frequency = "index", input_kind = "prices", holdout = 50, threshold = 100,
            paths = 6000, horizon = 8, quantiles = list(0.05, 0.5, 0.95))
  # the summary, in batches of one block, and in one batch: the same bits
  one <- fit_risk_simulation(.risk_task(prices, p, sim = list(batch_blocks = 1L, file = tempfile(fileext = ".parquet"))))
  all <- fit_risk_simulation(.risk_task(prices, p, sim = list(file = tempfile(fileext = ".parquet"))))
  strip <- function(res) {
    df <- tab_results_frame(res$rows)
    df <- df[!(df$section == "model" & grepl("^(batches|batch_paths|seconds_|paths_bytes)", df$stat)), c("key", "value", "text", "x0")]
    list(df, as.data.frame(res$output))
  }
  .tab_expect(identical(strip(one), strip(all)), "one block a batch differs from one batch")
  df <- tab_results_frame(all$rows)
  .tab_expect(df$value[df$key == "model|||batches"] == 1 && tab_results_frame(one$rows)$value[tab_results_frame(one$rows)$key == "model|||batches"] == 6,
              "the batches")
  # the paths file: the node's measures against the whole matrix
  file <- tempfile(fileext = ".parquet")
  out <- tempfile(fileext = ".parquet")
  Sys.setenv(TABULARIA_OUTPUT = out)
  on.exit({ Sys.unsetenv("TABULARIA_OUTPUT"); unlink(c(file, out)) }, add = TRUE)
  task <- .risk_task(prices, c(p[setdiff(names(p), "output")], list(output = "paths")), output = "paths", sim = list(file = file))
  task$write_output <- TRUE
  res <- fit_risk_simulation(task)
  .tab_expect(is.null(res$output) && file.exists(out) && !file.exists(file), "the paths are copied to the output and the file deleted")
  tab <- as.data.frame(arrow::read_parquet(out))
  .tab_expect(identical(names(tab), c("path", "step", "time", "value", "cumulative", "price", "sigma")), "the paths' columns ", paste(names(tab), collapse = " "))
  h <- 8L
  N <- 6000
  H <- matrix(tab$cumulative, nrow = h)
  last <- prices$price[[nrow(prices)]]
  .tab_expect(.risk_rel(tab$price, last * (1 + tab$cumulative)) < 1e-15, "the price is P_T (1 + C)")
  r <- matrix(tab$value, nrow = h)
  .tab_expect(.risk_rel(H, expm1(apply(r, 2L, cumsum)), 1) < 1e-15, "C is exp(Σr) − 1")
  x <- sort(H[h, ])
  rows <- tab_results_frame(res$rows)
  got <- function(key) rows$value[rows$key == key]
  .tab_expect(identical(got("risk|var||value"), x[[ceiling(round(N * 0.01, 6))]]), "the VaR")
  ke <- N * 0.025
  .tab_expect(abs(got("risk|es||value") - mean(x[seq_len(ke)])) <= 1e-12 * abs(got("risk|es||value")), "the ES")
  thr <- 100 / last - 1
  .tab_expect(got("risk|prob_crossed||count") == sum(colSums(H < thr) > 0) && got("risk|prob_beyond||count") == sum(H[h, ] < thr),
              "the price threshold's counts")
  D <- apply(1 + H, 2L, function(w) max(0, 1 - w / cummax(pmax(w, 1))))
  .tab_expect(identical(got("risk|drawdown||median"), sort(D)[[N / 2]]), "the drawdown's median")
  # the summary of the same seed: the prices' quantiles are P_T (1 + the quantiles)
  s <- as.data.frame(all$output)
  .tab_expect(identical(s$price_quantile_0.05, last * (1 + s$quantile_0.05)) && identical(s$price_mean, last * (1 + s$mean)), "the prices' columns")
  .tab_expect(identical(s$var[[h]], got("risk|var||value")), "the summary's VaR at the horizon is the row's")
  for (k in seq_len(h)) {
    q <- sort(H[k, ])[[ceiling(round(N * 0.95, 6))]]
    .tab_expect(abs(s$quantile_0.95[[k]] - q) <= s$band_error[[k]] * (1 + 1e-9) + 1e-15, "step ", k, ": the 95 % band within its error")
  }
}

TAB_COMMON_TESTS[["risk: the summary and the paths keep the time's own type (Int64 above 2^31, Int32, date32, timestamp UTC) and the catalogue's columns"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  raw <- tab_ref_table(file.path(code, "reference", "data", "dec_types.csv"), colClasses = "character", encoding = "UTF-8")
  value <- 100 + cumsum(.garch_frame(code)$gjr[seq_len(nrow(raw))] * 10)
  types <- list(int64 = list(bit64::as.integer64(raw$t_int64), arrow::int64(), "day"), int32 = list(as.integer(raw$t_int), arrow::int32(), "day"),
                date32 = list(as.Date(raw$t_date), arrow::date32(), "index"), ts = list(as.POSIXct(raw$t_hour, tz = "UTC"), arrow::timestamp("us", "UTC"), "hour"))
  path <- tempfile(fileext = ".parquet")
  out <- tempfile(fileext = ".parquet")
  on.exit({ Sys.unsetenv(c("TABULARIA_INPUT", "TABULARIA_OUTPUT")); unlink(c(path, out)) }, add = TRUE)
  Sys.setenv(TABULARIA_OUTPUT = out)
  spec <- tab_variant_spec("risk_simulation", "arima")$outputs
  for (name in names(types)) {
    ty <- types[[name]]
    arrow::write_parquet(arrow::arrow_table(t = arrow::Array$create(ty[[1L]])$cast(ty[[2L]]), v = arrow::Array$create(value)), path)
    Sys.setenv(TABULARIA_INPUT = path)
    p <- list(model = "arima", time = "t", value = "v", frequency = ty[[3L]], s = 1, p = 1, d = 1, paths = 1000, horizon = 3,
              quantiles = list(0.1, 0.9), var_level = 0.95, es_level = 0.95)
    sm <- fit_risk_simulation(.risk_task(NULL, p, sim = list(file = tempfile(fileext = ".parquet"))))$output
    .tab_expect(sm$schema$GetFieldByName("time")$type == ty[[2L]], name, ": the summary's time is ", sm$schema$GetFieldByName("time")$type$ToString())
    .tab_expect(sm$schema$GetFieldByName("step")$type == arrow::int32() && sm$num_rows == 3L, name, ": the summary's steps")
    want <- c("step", "time", "mean", "sd", "quantile_0.1", "quantile_0.9", "var", "var_low", "var_high", "es", "es_low", "es_high", "band_error")
    .tab_expect(identical(names(sm), want), name, ": the summary's columns ", paste(names(sm), collapse = ","))
    task <- .risk_task(NULL, c(p, list(output = "paths")), output = "paths", sim = list(file = tempfile(fileext = ".parquet")))
    task$write_output <- TRUE
    fit_risk_simulation(task)
    pa <- arrow::read_parquet(out, as_data_frame = FALSE)
    .tab_expect(pa$schema$GetFieldByName("time")$type == ty[[2L]] && identical(names(pa), c("path", "step", "time", "value", "cumulative")),
                name, ": the paths' columns and time ", paste(names(pa), collapse = ","))
    if (name == "int64") {
      got <- as.character(pa$GetColumnByName("time")$as_vector()[1:3])
      last <- bit64::as.integer64(raw$t_int64[[nrow(raw)]])
      .tab_expect(identical(got, as.character(last + 1:3)), "every digit of an Int64: ", paste(got, collapse = " "))
    }
  }
  .tab_expect(all(c("quantile_*", "var", "band_error") %in% unlist(spec$risk_summary)), "the catalogue's summary")
}

TAB_COMMON_TESTS[["risk: a position without a relative change, a relative change from 0, too few paths are refused with a sentence"]] <- function(code) {
  ar <- .risk_frame(code, "ar_index.csv", list(t = "integer", level = "number"))
  base <- list(model = "arima", time = "t", value = "level", frequency = "index", p = 1)
  .tab_refused_with("position_not_relative", fit_risk_simulation(.risk_task(ar, c(base, list(position = 1000)))))
  zero <- ar
  zero$level[[nrow(zero)]] <- 0
  msg <- .tab_refused_with("zero_last", fit_risk_simulation(.risk_task(zero, c(base, list(aggregate = "relative_change")))))
  .tab_expect(grepl("needs a positive last value", msg), msg)
  msg <- .tab_refused_with("too_few_paths", fit_risk_simulation(.risk_task(ar, c(base, list(paths = 100, var_level = 0.999)))))
  .tab_expect(identical(msg, "with 100 paths and a 99.9 % level no simulated value lies beyond the VaR: at least 1,000 paths"), msg)
}

# The node in a child process (R, arrow, rugarch, a GJR-GARCH with skewed t errors fitted, 200,000 paths of 10 steps simulated):
# the engine is given 256 MiB for its batches (baseline 0), and the resident size it adds over the process's size just before
# the simulation (VmHWM, reset when the first batch starts, minus that size) stays below them, over more than one batch.
# TABULARIA_RISK_MEMORY_PATHS and _STEPS set another size (lot G3 measured 10 million values with them).
TAB_COMMON_TESTS[["risk: the simulation adds less than its 256 MiB over the fitted node's size, at 2 million values, in a child process"]] <- function(code) {
  if (!file.exists("/proc/self/status") || !requireNamespace("arrow", quietly = TRUE)) return("skipped")
  N <- as.numeric(Sys.getenv("TABULARIA_RISK_MEMORY_PATHS", "200000"))
  h <- as.integer(Sys.getenv("TABULARIA_RISK_MEMORY_STEPS", "10"))
  out <- tempfile(fileext = ".txt")
  err <- tempfile(fileext = ".err")
  script <- tempfile(fileext = ".R")
  on.exit(unlink(c(out, err, script)), add = TRUE)
  writeLines(c(
    sprintf('code <- "%s"', code),
    'for (f in sort(list.files(file.path(code, c("common", "runner"), "R"), pattern = "[.]R$", full.names = TRUE))) source(f)',
    'suppressMessages(library(arrow))',
    'loadNamespace("rugarch")',
    'invisible(tab_catalog(code))',
    'df <- tab_read_csv(file.path(code, "reference", "data", "garch_returns.csv"), list(t = "integer", gjr = "number"))',
    'engine <- ts_sim_run',
    'before <- NA_real_',
    'ts_sim_run <- function(...) { before <<- ts_sim_rss(); engine(...) }',
    sprintf('p <- list(model = "garch", time = "t", value = "gjr", frequency = "index", variance_model = "gjr", distribution = "sstd", paths = %s, horizon = %d, threshold = -0.05)',
            format(N, scientific = FALSE), h),
    'task <- list(protocol = 1L, mode = "fit", group = "timeseries", group_version = "selftest", model = "risk_simulation", variant = "garch",',
    '  params = p, output = "risk_summary", write_output = FALSE, rows_from = "run", .frame = df,',
    '  limits = utils::modifyList(tab_catalog()$limits, list(memory_bytes = 256 * 2^20, tmp_bytes = 50e9)),',
    sprintf('  .sim = list(baseline_bytes = 0, file = "%s"))', tempfile(fileext = ".parquet")),
    'tab_seed(task)',
    't0 <- proc.time()[["elapsed"]]',
    'res <- fit_risk_simulation(task)',
    'secs <- proc.time()[["elapsed"]] - t0',
    'rows <- tab_results_frame(res$rows)',
    sprintf('writeLines(as.character(c(ts_sim_hwm(), before, rows$value[rows$key == "model|||batches"], secs)), "%s")', out)), script)
  status <- system2(file.path(R.home("bin"), "Rscript"), c("--vanilla", script), stdout = FALSE, stderr = err)
  .tab_expect(status == 0 && file.exists(out), "the child process failed: ", paste(utils::tail(readLines(err), 3L), collapse = " "))
  got <- as.numeric(readLines(out))
  grown <- got[[1L]] - got[[2L]]
  message(sprintf("risk at %s values: %.0f MiB over %.0f MiB, %d batches, %.1f s", format(N * h, big.mark = ",", scientific = FALSE), grown / 2^20, got[[2L]] / 2^20,
                  as.integer(got[[3L]]), got[[4L]]))
  .tab_expect(grown < 256 * 2^20, sprintf("the simulation added %.0f MiB, above its 256 MiB", grown / 2^20))
  .tab_expect(got[[3L]] > 1, "one batch: ", got[[3L]])
}
