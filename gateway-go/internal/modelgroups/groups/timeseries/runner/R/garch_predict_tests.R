# The self-test of Predict with the volatility (GARCH) node (garch.R, predict_garch; lot G6): added to TAB_COMMON_TESTS. A model is fitted
# on the first rows of a series, its card goes through JSON as the gateway hands it to the Job, and Predict is run on the rows after:
# σ and the 1-step VaR of each are the fit's own filter's (rugarch::ugarchfilter with the parameters fixed) to 1e-10.

.garch_pred_task <- function(frame, params) {
  list(protocol = TAB_PROTOCOL, mode = "predict", group = "timeseries", group_version = "selftest", model = "garch", variant = "default",
       params = params, output = "rows", write_output = FALSE, rows_from = "run", limits = tab_catalog()$limits, .frame = frame)
}

# fit on `frame[train, ]`, the card as the Job reads it
.garch_pred_fit <- function(frame, params, train) {
  task <- .garch_task(frame[train, , drop = FALSE], params)
  res <- fit_garch(task)
  list(res = res, task = task, o = garch_params(task$params), card = jsonlite::fromJSON(tab_card_text(res$card), simplifyVector = FALSE))
}

.garch_pred_col <- function(out, name) as.numeric(out$GetColumnByName(name)$as_vector())

TAB_COMMON_TESTS[["garch: Predict gives the fit's own σ and VaR on the rows after the card's last instant (1e-10), and the forecast after them"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  loadNamespace("rugarch")
  df <- .garch_frame(code)
  prices <- .garch_frame(code, "garch_prices.csv")
  cases <- list(
    list(df, list(value = "ar_t", ar_order = 1, distribution = "norm"), 700L),
    list(df, list(variance_model = "gjr", distribution = "sstd", garch_order = 1), 650L),
    list(df, list(variance_model = "egarch", distribution = "norm", ma_order = 1), 700L),
    list(df, list(variance_model = "egarch", distribution = "std", arch_order = 2, garch_order = 1), 700L),
    list(df, list(variance_model = "arch", arch_order = 2, ma_order = 1, mean = FALSE, value = "ar_t", distribution = "norm"), 700L),
    list(df, list(variance_model = "garch", arch_order = 2, garch_order = 2, ar_order = 2, value = "ar_t", distribution = "std"), 700L),
    list(prices, list(time = "day", value = "price", input_kind = "prices", returns_type = "simple", distribution = "norm"), 250L),
    list(prices, list(time = "day", value = "price", input_kind = "prices", distribution = "std"), 250L))
  for (cs in cases) {
    frame <- cs[[1L]]
    train <- seq_len(cs[[3L]])
    f <- .garch_pred_fit(frame, cs[[2L]], train)
    o <- f$o
    what <- garch_name(o)
    new <- frame[-train, , drop = FALSE]
    pred <- predict_garch(.garch_pred_task(new, list(horizon = 5L)), f$card)$output
    n <- nrow(new)
    .tab_expect(pred$num_rows == n + 5L, what, ": the rows ", pred$num_rows)
    .tab_expect(identical(names(pred), c(o$time, "prediction", "prediction_var")), what, ": the columns ", paste(names(pred), collapse = ","))
    # the reference: rugarch's filter of the whole series with the fitted parameters fixed, the recursion started as the fit's (n.old)
    y <- frame[[o$value]]
    r <- if (o$input_kind == "prices") (if (identical(o$returns_type, "simple")) y[-1L] / y[-length(y)] - 1 else diff(log(y))) else y
    lead <- length(y) - length(r)
    ntrain <- length(train) - lead
    s <- attr(f$res$native, "scale")
    est <- f$res$native@fit$coef
    flt <- rugarch::ugarchfilter(garch_spec(o, as.list(est)), r / s, n.old = ntrain)
    at <- seq.int(ntrain + 1L, length(r))
    sigma <- as.numeric(rugarch::sigma(flt))[at] * s
    mu <- as.numeric(rugarch::fitted(flt))[at] * s
    q <- f$card$garch$var_quantile
    got <- .garch_pred_col(pred, "prediction")
    var <- .garch_pred_col(pred, "prediction_var")
    .tab_expect(.garch_rel(got[seq_len(n)], sigma) < 1e-10, what, ": σ of the new rows is not the fit's filter's (", .garch_rel(got[seq_len(n)], sigma), ")")
    .tab_expect(.garch_rel(var[seq_len(n)], mu + sigma * q) < 1e-10, what, ": the VaR of the new rows (", .garch_rel(var[seq_len(n)], mu + sigma * q), ")")
    # the forecast after the last row: the model's own recursion from the filter's state of the whole series
    eps <- r - as.numeric(rugarch::fitted(flt)) * s
    mu_all <- as.numeric(rugarch::fitted(flt)) * s
    sig_all <- as.numeric(rugarch::sigma(flt)) * s
    th <- unlist(f$card$garch$coefficients)
    shape <- if ("shape" %in% names(th)) th[["shape"]] else 0
    skew <- if ("skew" %in% names(th)) th[["skew"]] else 0
    fc <- garch_forecast(o, th, garch_kappa(o$distribution, shape, skew), garch_abs_mean(o$distribution, shape, skew), r, mu_all, sig_all, 5L)
    .tab_expect(.garch_rel(got[n + 1:5], fc$sigma) < 1e-10, what, ": the forecast after the new rows (", .garch_rel(got[n + 1:5], fc$sigma), ")")
    .tab_expect(abs(var[[n + 1L]] - (fc$mean[[1L]] + fc$sigma[[1L]] * q)) < 1e-10 * abs(var[[n + 1L]]) && all(is.na(var[n + 2:5])),
                what, ": the VaR of the first forecast row, and none after it")
    # no new row: the card's own horizon, whose first step is the card's σ_next
    none <- predict_garch(.garch_pred_task(new[0L, , drop = FALSE], list(horizon = 3L)), f$card)$output
    gn <- .garch_pred_col(none, "prediction")
    .tab_expect(none$num_rows == 3L && abs(gn[[1L]] - f$card$garch$last$sigma_next) < 1e-12 * f$card$garch$last$sigma_next, what,
                ": the forecast from the card alone is not the card's σ_next")
    # no horizon, new rows: the rows only; no horizon and no rows: the card's own (the node's default, 10)
    .tab_expect(predict_garch(.garch_pred_task(new[0L, , drop = FALSE], list()), f$card)$output$num_rows == 10L, what, ": the card's own horizon")
    only <- predict_garch(.garch_pred_task(new, list()), f$card)$output
    .tab_expect(only$num_rows == n, what, ": without a horizon the output is the new rows (", only$num_rows, ")")
  }
}

TAB_COMMON_TESTS[["garch: Predict refuses rows that do not continue the card's series: before its last day, with a gap, twice, off the calendar"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  df <- .garch_frame(code)
  p <- list(distribution = "norm")
  f <- .garch_pred_fit(df, p, 1:600)
  run <- function(frame, card = f$card) predict_garch(.garch_pred_task(frame, list()), card)
  msg <- .tab_refused_with("predict_before_last", run(df[600:620, ]))
  .tab_expect(grepl("start before the model's last day", msg, fixed = TRUE) && grepl("Predict continues a series", msg, fixed = TRUE), msg)
  .tab_refused_with("predict_before_last", run(df[100:120, ]))
  msg <- .tab_refused_with("predict_gap", run(df[602:620, ]))
  .tab_expect(grepl("the row of 602", msg, fixed = TRUE) && grepl("right after it", msg, fixed = TRUE), msg)
  msg <- .tab_refused_with("predict_gap", run(df[c(601:610, 612:620), ]))
  .tab_expect(grepl("the row of 612", msg, fixed = TRUE) && grepl("after the row before it", msg, fixed = TRUE), msg)
  .tab_refused_with("duplicate_time", run(df[c(601:610, 610:620), ]))
  gap <- df[601:620, ]
  gap$gjr[[5L]] <- NA
  msg <- .tab_refused_with("gaps", run(gap))
  .tab_expect(grepl("filter out the rows without a return", msg, fixed = TRUE), msg)
  .tab_refused_with("predict_column", run(data.frame(t = as.Date("2025-01-01") + 0:5, gjr = 0.01)))
  .tab_refused_with("no_column", run(df[601:620, c("t", "ar_t")]))
  .tab_refused_with("bad_card", run(df[601:620, ], card = list(params = f$card$params)))
  # the rows are sorted by time: the order they arrive in does not matter
  shuffled <- df[c(605:610, 601:604, 611:620), ]
  a <- .garch_pred_col(run(shuffled)$output, "prediction")
  b <- .garch_pred_col(run(df[601:620, ])$output, "prediction")
  .tab_expect(identical(a, b), "the order of the rows")
  # a calendar: months, from a card of monthly dates; a missing month is a gap, a month off the grid is one too
  m <- data.frame(month = seq(as.Date("2010-01-01"), by = "month", length.out = 160L), gjr = df$gjr[1:160])
  fm <- .garch_pred_fit(m, list(time = "month", frequency = "month", distribution = "norm"), 1:130)
  runm <- function(frame) predict_garch(.garch_pred_task(frame, list()), fm$card)
  ok <- runm(m[131:160, ])$output
  .tab_expect(ok$num_rows == 30L, "monthly rows")
  .tab_refused_with("predict_gap", runm(m[132:160, ]))
  .tab_refused_with("predict_gap", runm(m[c(131:140, 142:160), ]))
  .tab_refused_with("predict_before_last", runm(m[130:160, ]))
  # a month's other day is the same period; the horizon's times follow the calendar
  late <- m[131:135, ]
  late$month <- late$month + 14
  .tab_expect(runm(late)$output$num_rows == 5L, "another day of the month")
  h <- predict_garch(.garch_pred_task(m[131:135, ], list(horizon = 2L)), fm$card)$output
  .tab_expect(identical(as.character(h$GetColumnByName("month")$as_vector()[6:7]), c("2021-04-01", "2021-05-01")), "the horizon's months")
}

# The output keeps the time's own type, as the fit's does: an Int64 above 2^31 to the last digit, an Int32, a date32, a timestamp UTC.
TAB_COMMON_TESTS[["garch: Predict keeps the time's own type (Int64 above 2^31, Int32, date32, timestamp UTC), the forecast rows' times included"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  value <- .garch_frame(code)$gjr[1:700]
  start <- list(int64 = 5000000000, int32 = 100000, date32 = as.numeric(as.Date("2024-03-01")), ts = as.numeric(as.POSIXct("2024-03-01 00:00:00", tz = "UTC")))
  types <- list(int64 = c("integer", "index"), int32 = c("integer", "day"), date32 = c("date", "day"), ts = c("datetime", "hour"))
  path <- tempfile(fileext = ".parquet")
  on.exit({ Sys.unsetenv("TABULARIA_INPUT"); unlink(path) }, add = TRUE)
  make <- function(name, from, to) {
    k <- seq.int(from, to) - 1L
    switch(name,
           int64 = arrow::Array$create(bit64::as.integer64(start$int64 + k))$cast(arrow::int64()),
           int32 = arrow::Array$create(as.integer(start$int32 + k)),
           date32 = arrow::Array$create(as.Date(start$date32 + k, origin = "1970-01-01"))$cast(arrow::date32()),
           ts = arrow::Array$create(as.POSIXct(start$ts + 3600 * k, origin = "1970-01-01", tz = "UTC"))$cast(arrow::timestamp("us", "UTC")))
  }
  for (name in names(types)) {
    kind <- types[[name]]
    # the model's series: written to a Parquet file too, so that the card holds the label of an Int64 as the runner writes it
    arrow::write_parquet(arrow::arrow_table(t = make(name, 1L, 600L), v = arrow::Array$create(value[1:600])), path)
    Sys.setenv(TABULARIA_INPUT = path)
    task <- .garch_task(NULL, list(time = "t", value = "v", frequency = kind[[2L]], distribution = "norm"))
    card <- jsonlite::fromJSON(tab_card_text(fit_garch(task)$card), simplifyVector = FALSE)
    arrow::write_parquet(arrow::arrow_table(t = make(name, 601L, 650L), v = arrow::Array$create(value[601:650])), path)
    out <- predict_garch(.garch_pred_task(NULL, list(horizon = 3L)), card)$output
    ty <- out$schema$GetFieldByName("t")$type
    want <- make(name, 1L, 2L)$type
    .tab_expect(ty == want, name, ": the time is ", ty$ToString(), ", not ", want$ToString())
    .tab_expect(out$num_rows == 53L && identical(names(out), c("t", "prediction", "prediction_var")), name, ": the rows ", out$num_rows)
    got <- out$GetColumnByName("t")$as_vector()
    if (name == "int64") {
      .tab_expect(identical(as.character(got[[1L]]), "5000000600") && identical(as.character(got[[53L]]), "5000000652"), "every digit of an Int64: ",
                  as.character(got[[1L]]), " ", as.character(got[[53L]]))
    } else {
      back <- make(name, 601L, 653L)$as_vector()
      .tab_expect(identical(as.numeric(got), as.numeric(back)), name, ": the times")
    }
  }
}

# Predict on new rows in a child process (a whole Rscript: R, arrow, rugarch, the card, the Parquet read, the filter, the output), its peak
# resident memory (VmHWM) below 1 GiB. TABULARIA_GARCH_PREDICT_N sets the new rows (20,000 here; lot G6 measured 100,000 with it).
TAB_COMMON_TESTS[["garch: Predict on new rows peaks below 1 GiB in a child process (VmHWM)"]] <- function(code) {
  if (!file.exists("/proc/self/status") || !requireNamespace("arrow", quietly = TRUE)) return("skipped")
  n <- as.integer(Sys.getenv("TABULARIA_GARCH_PREDICT_N", "20000"))
  dir <- tempfile("garch-predict")
  dir.create(dir)
  on.exit(unlink(dir, recursive = TRUE), add = TRUE)
  # 1. the model, fitted on 2,000 returns of a simulated GJR(1,1) with a skewed t: its card and the new rows, in files
  script1 <- file.path(dir, "fit.R")
  writeLines(c(
    sprintf('code <- "%s"', code),
    'for (f in sort(list.files(file.path(code, c("common", "runner"), "R"), pattern = "[.]R$", full.names = TRUE))) source(f)',
    'loadNamespace("rugarch")',
    'sp <- rugarch::ugarchspec(variance.model = list(model = "gjrGARCH"), mean.model = list(armaOrder = c(1, 0)), distribution.model = "sstd",',
    '  fixed.pars = list(mu = 2e-4, ar1 = 0.1, omega = 4e-6, alpha1 = 0.03, gamma1 = 0.1, beta1 = 0.88, skew = 0.85, shape = 7))',
    sprintf('r <- as.numeric(rugarch::fitted(rugarch::ugarchpath(sp, n.sim = %d, rseed = 9)))', 2000L + n),
    'p <- list(time = "t", value = "v", frequency = "index", variance_model = "gjr", distribution = "sstd", ar_order = 1)',
    'task <- list(protocol = 1L, mode = "fit", group = "timeseries", group_version = "selftest", model = "garch", variant = "default", params = p,',
    '  output = "volatility", write_output = FALSE, rows_from = "run", limits = tab_catalog(code)$limits, .frame = data.frame(t = seq_len(2000L), v = r[1:2000]))',
    'tab_seed(task)',
    sprintf('writeLines(tab_card_text(fit_garch(task)$card), "%s")', file.path(dir, "model.json")),
    sprintf('arrow::write_parquet(arrow::arrow_table(t = bit64::as.integer64(2000L + seq_len(%d)), v = r[-(1:2000)]), "%s")', n, file.path(dir, "rows.parquet"))), script1)
  err <- file.path(dir, "err.txt")
  env <- paste0("TABULARIA_CODE_DIR=", code)
  st <- system2(file.path(R.home("bin"), "Rscript"), c("--vanilla", script1), stdout = FALSE, stderr = err, env = env)
  .tab_expect(st == 0, "the fit failed: ", paste(utils::tail(readLines(err), 3L), collapse = " "))
  # 2. Predict alone, in a process of its own
  script2 <- file.path(dir, "predict.R")
  out <- file.path(dir, "out.txt")
  writeLines(c(
    sprintf('code <- "%s"', code),
    'for (f in sort(list.files(file.path(code, c("common", "runner"), "R"), pattern = "[.]R$", full.names = TRUE))) source(f)',
    sprintf('Sys.setenv(TABULARIA_INPUT = "%s")', file.path(dir, "rows.parquet")),
    sprintf('card <- jsonlite::fromJSON(tab_read_text("%s"), simplifyVector = FALSE)', file.path(dir, "model.json")),
    'task <- list(protocol = 1L, mode = "predict", group = "timeseries", model = "garch", variant = "default", params = list(horizon = 10L),',
    '  output = "rows", write_output = FALSE, rows_from = "run", limits = tab_catalog(code)$limits)',
    't0 <- proc.time()[["elapsed"]]',
    'res <- predict_garch(task, card)',
    'tab_write_output(res$output, file.path(dirname(Sys.getenv("TABULARIA_INPUT")), "predicted.parquet"))',
    'secs <- proc.time()[["elapsed"]] - t0',
    'hwm <- as.numeric(sub("[^0-9]*([0-9]+).*", "\\\\1", grep("^VmHWM", readLines("/proc/self/status"), value = TRUE))) * 1024',
    sprintf('writeLines(as.character(c(hwm, secs, res$output$num_rows)), "%s")', out)), script2)
  st <- system2(file.path(R.home("bin"), "Rscript"), c("--vanilla", script2), stdout = FALSE, stderr = err, env = env)
  .tab_expect(st == 0 && file.exists(out), "Predict failed: ", paste(utils::tail(readLines(err), 3L), collapse = " "))
  got <- as.numeric(readLines(out))
  message(sprintf("garch Predict at %d new rows: VmHWM %.0f MiB, %.1f s, %d rows", n, got[[1L]] / 2^20, got[[2L]], got[[3L]]))
  .tab_expect(got[[3L]] == n + 10L, "the rows ", got[[3L]])
  .tab_expect(got[[1L]] < 2^30, "VmHWM ", got[[1L]], " bytes at ", n, " new rows")
}
