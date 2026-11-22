# ARIMA with manual orders, from AR to SARIMAX (docs/specs/statistical-models.md, `arima`,
# lane M6): one estimator, forecast::Arima (regression with ARIMA errors, maximum
# likelihood), forecasts by forecast::forecast. AR, MA, ARMA, ARIMA, SARIMA and SARIMAX are
# what the orders and the exogenous columns make of it.
#
#   fit_arima(task)            the orders of the node (orders = manual)
#   predict_arima(task, card)  a forecast with the card's coefficients, no re-estimation
#
# The pieces the automatic orders (lane M7, arima_auto.R) build on, so that both variants
# write the same rows:
#
#   arima_prepare(task)                         the series: read, checked, on its grid, split
#   arima_fit_orders(task, prep, orders, extra)  fits those orders and writes every row; `extra`
#                                               (a function of rows) adds the variant's own rows
#                                               (the candidates, orders_reason) after the
#                                               model section's numbers
#   arima_lambda(prep)                          the transform's λ, which the search needs first
#
# The series. One row per period of `frequency`: a time held twice (two rows in one month)
# stops the node; a period without a row, or with an empty value, is a missing value that the
# likelihood skips. The rows are sorted by time. Without exogenous columns the forecast runs
# `horizon` periods after the last observed value; with them it runs over the rows after it
# whose exogenous values are given (their `value` empty): the future rows. `holdout` keeps the
# last k observations out of the fit; the forecast is then measured on them, and the
# forecast after the last observation applies the same coefficients to the whole series (the
# Kalman filter run again with every coefficient fixed, as forecast's own `Arima(model = )`).
#
# Columns never become code: the exogenous columns are the regressors xreg1, xreg2… of the
# fit, the drift is xdrift (so that forecast() does not add one of its own), and each is named
# back when the rows are written.
#
# The time's axis: x0 of the charts is days since 1970-01-01 for a date or datetime column,
# the integer itself for an integer column; the text `time_kind` says which (the layout reads
# it, so an integer column of months is not drawn as dates of 1970).

ARIMA_PACKAGES <- c("forecast", "tseries", "FinTS")
ARIMA_NEAR_UNIT <- 1.05      # a root whose modulus is below this is «near the unit circle»
ARIMA_ARCH_LAGS <- 12L       # ARCH LM's lags without a season (with one: s)
ARIMA_MAX_PERIODS <- 200000L # periods of the grid, gaps included
ARIMA_CARD_SERIES <- 5000L   # observations the card keeps for a forecast from the card alone

fit_arima <- function(task) {
  prep <- arima_prepare(task)
  o <- prep$params
  arima_fit_orders(task, prep, list(p = o$p, d = o$d, q = o$q, P = o$P, D = o$D, Q = o$Q))
}

predict_arima <- function(task, card) {
  a <- card$arima
  params <- card$params
  horizon <- as.integer(task$params$horizon %||% a$horizon %||% params$horizon %||% 12L)
  interval <- isTRUE(task$params$interval)
  exog <- unlist(a$exog %||% list())
  cols <- c(params$time, params$value, exog)
  df <- tab_input(task)
  missing <- setdiff(c(params$time, exog), names(df))
  if (length(missing)) {
    tab_refuse("predict_column", paste0("the rows have no column «", missing[[1L]], "», which the model was fitted with"),
               list(column = missing[[1L]]))
  }
  if (!params$value %in% names(df)) df[[params$value]] <- rep(NA_real_, nrow(df))
  type <- ts_read(task, params$time)$type[[params$time]]  # the time column's own Arrow type: the output keeps it
  # an Int64 past 2^31 arrives as a 64-bit integer, which the card's numeric times (rbind) do not mix with
  if (inherits(df[[params$time]], "integer64")) df[[params$time]] <- as.numeric(df[[params$time]])
  history <- arima_card_history(card)
  observed <- df[!is.na(df[[params$value]]), , drop = FALSE]
  if (!nrow(observed)) {
    # only future rows (or none): the series is the card's, the input brings the future
    last <- ts_period(history[[params$time]], a$last$time_kind, a$frequency)
    after <- df[!is.na(df[[params$time]]), , drop = FALSE]
    after <- after[ts_period(after[[params$time]], a$last$time_kind, a$frequency) > max(last), , drop = FALSE]
    df <- rbind(history[, cols, drop = FALSE], after[, cols, drop = FALSE])
  }
  task$params <- utils::modifyList(params, list(horizon = horizon, holdout = 0L))
  prep <- arima_prepare(task, df = df[, intersect(cols, names(df)), drop = FALSE], t0 = arima_card_t0(card), type = type)
  model <- arima_model_of_card(card)
  out <- arima_filter(model, prep, prep$span, levels = arima_levels(card$alpha %||% params$alpha %||% 0.05))
  future <- prep$future
  if (!length(future)) return(list(output = data.frame()))
  output <- if (length(exog)) prep$rows_of(future) else data.frame(row.names = seq_along(future))
  output$prediction <- out$forecast$mean
  if (interval) {
    output$lower_80 <- out$forecast$lower_80
    output$upper_80 <- out$forecast$upper_80
    output$lower <- out$forecast$lower
    output$upper <- out$forecast$upper
  }
  list(output = arima_table(output, params$time, prep$time_array(future)))
}

# ── the parameters ──────────────────────────────────────────────────────────
# The node's parameters with the catalogue's defaults where a key is not given, and `s` from
# the frequency when it is not.
arima_params <- function(params) {
  spec <- tab_model_spec("arima")
  out <- spec$defaults
  for (k in names(params)) if (!is.null(params[[k]])) out[[k]] <- params[[k]]
  seasons <- c(hour = 24L, day = 7L, week = 52L, month = 12L, quarter = 4L, year = 1L, index = 1L)
  if (is.null(out$s)) out$s <- seasons[[out$frequency %||% "index"]]
  for (k in c("p", "d", "q", "P", "D", "Q", "s", "horizon", "holdout")) out[[k]] <- as.integer(out[[k]] %||% 0L)
  out$exog <- unlist(out$exog %||% list())
  out$alpha <- as.numeric(out$alpha %||% 0.05)
  out
}

# The bands: 80% and 1 − alpha (one band twice when alpha is 0.2).
arima_levels <- function(alpha) c(80, round(100 * (1 - as.numeric(alpha)), 10))

# What the orders make of the node, as catalog.arima_name says it.
arima_model_name <- function(o, s, exog) {
  seasonal <- s > 1L && (o$P + o$D + o$Q) > 0L
  if (seasonal) return(sprintf("%s(%d,%d,%d)(%d,%d,%d)[%d]", if (length(exog)) "SARIMAX" else "SARIMA",
                               o$p, o$d, o$q, o$P, o$D, o$Q, s))
  if (length(exog)) return(sprintf("ARIMAX(%d,%d,%d)", o$p, o$d, o$q))
  if (o$d == 0L && o$q == 0L) return(sprintf("AR(%d)", o$p))
  if (o$d == 0L && o$p == 0L) return(sprintf("MA(%d)", o$q))
  if (o$d == 0L) return(sprintf("ARMA(%d,%d)", o$p, o$q))
  sprintf("ARIMA(%d,%d,%d)", o$p, o$d, o$q)
}

# ── the time ────────────────────────────────────────────────────────────────
# The grid of a frequency (ts_period, ts_time_after, ts_x0, ts_time_kind, ts_grid) is the group's, in ts_series.R:
# ARIMA and the decomposition share it.

# ── the series ──────────────────────────────────────────────────────────────
# The input read and checked, on the grid of its frequency from its first observation: the
# observed span, the future rows, the training part and the holdout. `df` and `t0` serve
# Predict (the rows to apply the card to; the time the drift counts from).
arima_prepare <- function(task, df = NULL, t0 = NULL, type = NULL) {
  o <- arima_params(task$params)
  time <- o$time
  value <- o$value
  exog <- o$exog
  cols <- c(time, value, exog)
  if (is.null(df)) {
    df <- tab_input(task, cols)
    # the time column's own Arrow type, read apart (the 64-bit option stays inside ts_read): the output keeps it
    type <- ts_read(task, time)$type[[time]]
  }
  for (name in cols) if (!name %in% names(df)) tab_refuse("no_column", paste0("the input has no column «", name, "»"), list(column = name))
  # an exogenous column takes its name as its coefficient's label, beside the model's own terms: one
  # named as a term would give two coefficients one label
  clash <- exog[grepl(ARIMA_TERM_NAMES, exog)]
  if (length(clash)) {
    tab_refuse("exog_name", paste0("the exogenous column «", clash[[1L]], "» has the name of a term of the model (ar1, ma1, sar1, ",
                                   "sma1, intercept, drift): rename it"), list(column = clash[[1L]]))
  }
  kind <- ts_time_kind(df[[time]], time, o$frequency)
  whole_time <- kind == "integer" && (is.integer(df[[time]]) || inherits(df[[time]], "integer64"))
  if (kind == "integer") df[[time]] <- as.numeric(df[[time]])
  for (name in c(value, exog)) {
    if (!tab_kind(df[[name]]) %in% c("number", "integer")) {
      tab_refuse("not_a_number", paste0("the column «", name, "» is not a number"), list(column = name))
    }
    df[[name]] <- as.numeric(df[[name]])
  }
  tab_complete_rows(df, c(value, exog))  # an infinite value stops the node and names the column
  no_time <- sum(is.na(df[[time]]))
  df <- df[!is.na(df[[time]]), , drop = FALSE]
  if (!nrow(df)) tab_refuse("no_values", paste0("the column «", value, "» has no value to model"), list(column = value))
  grid <- ts_grid(df, time, kind, o$frequency, ARIMA_MAX_PERIODS)
  df <- grid$df
  pos <- grid$pos
  periods <- grid$periods
  # the grid: one place a period, a time each (the row's own where there is one)
  y <- rep(NA_real_, periods)
  y[pos] <- df[[value]]
  X <- matrix(NA_real_, periods, length(exog))
  for (j in seq_along(exog)) X[pos, j] <- df[[exog[[j]]]]
  has_row <- rep(FALSE, periods)
  has_row[pos] <- TRUE
  first_time <- df[[time]][[1L]]
  observed <- which(!is.na(y))
  if (!length(observed)) tab_refuse("no_values", paste0("the column «", value, "» has no value to model"), list(column = value))
  first <- min(observed)
  last <- max(observed)
  # the future: `horizon` periods, or the rows after the last value whose exogenous are given
  future <- integer()
  dropped_future <- 0L
  if (length(exog)) {
    after <- if (last < periods) seq.int(last + 1L, periods) else integer()
    given <- after[has_row[after] & stats::complete.cases(X[after, , drop = FALSE])]
    k <- 0L
    while (k < length(given) && given[[k + 1L]] == last + k + 1L) k <- k + 1L
    future <- if (k) seq.int(last + 1L, last + k) else integer()
    dropped_future <- sum(has_row[after]) - k
    if (length(future) > tab_limit(task, "max_horizon")) {
      tab_refuse("too_long_horizon", paste0("the input holds ", length(future), " future rows (at most ",
                                            tab_limit(task, "max_horizon"), "): fewer rows after the last value"),
                 list(rows = length(future)))
    }
  } else {
    future <- seq.int(last + 1L, length.out = o$horizon)
  }
  span <- seq.int(first, last)
  # inside the span a period whose exogenous values are missing is a missing value
  if (length(exog)) y[span][!stats::complete.cases(X[span, , drop = FALSE])] <- NA_real_
  missing_in_span <- sum(is.na(y[span]))
  outside <- sum(has_row) - sum(has_row[span]) - length(intersect(future, which(has_row)))
  holdout <- o$holdout
  train <- if (holdout > 0L) span[seq_len(max(0L, length(span) - holdout))] else span
  if (holdout > 0L && sum(!is.na(y[train])) < 2L) {
    tab_refuse("holdout_too_long", paste0("the holdout of ", holdout, " observations leaves too few to fit the model: ",
                                          "hold out fewer"), list(holdout = holdout))
  }
  test <- setdiff(span, train)
  # the times of the grid, and the drift's count from the first observation (or the card's t0)
  grid_len <- max(periods, if (length(future)) max(future) else 0L)
  times <- ts_time_after(first_time, seq_len(grid_len) - 1L, kind, o$frequency)
  times[pos] <- df[[time]]
  if (o$frequency == "index" && kind != "integer" && grid_len > periods) {
    step <- if (periods > 1L) as.numeric(times[[periods]]) - as.numeric(times[[periods - 1L]]) else 1
    times[seq.int(periods + 1L, grid_len)] <- times[[periods]] + step * seq_len(grid_len - periods)
  }
  drift_index <- seq_len(grid_len) - first + 1L
  if (!is.null(t0)) {
    base <- ts_period(c(t0, times[[first]]), kind, o$frequency)
    if (o$frequency != "index" || kind == "integer") drift_index <- drift_index + as.integer(round(base[[2L]] - base[[1L]]))
  }
  row_of <- match(seq_len(grid_len), pos)
  prep <- list(params = o, kind = kind, y = c(y, rep(NA_real_, grid_len - periods)),
               X = rbind(X, matrix(NA_real_, grid_len - periods, length(exog))),
               times = times, x0 = ts_x0(times, kind), drift_index = drift_index, first = first, last = last,
               span = span, train = train, test = test, future = future, holdout = holdout,
               n_missing = no_time + missing_in_span + max(0L, outside),
               gaps = missing_in_span, dropped_future = max(0L, dropped_future),
               time_array = function(i) ts_time_array(if (whole_time && is.null(type)) as.integer(times[i]) else times[i], type),
               rows_of = function(i) {
                 r <- df[row_of[i], , drop = FALSE]
                 rownames(r) <- NULL
                 r
               })
  if (length(future) && length(exog)) prep$X[future, ] <- X[future, , drop = FALSE]
  prep
}

# ── the model: its orders, its regressors, its fit ──────────────────────────
# What the fit and the filter share: orders, season, mean and drift as differencing allows,
# the transform's λ (NULL: none), the coefficients when known.
arima_spec <- function(o, orders, lambda = NULL) {
  dd <- orders$d + orders$D
  list(orders = orders, s = o$s, mean = isTRUE(o$mean) && dd == 0L, drift = isTRUE(o$drift) && dd <= 1L,
       drift_dropped = isTRUE(o$drift) && dd > 1L, exog = o$exog, lambda = lambda, transform = o$transform)
}

# The regressors at grid positions i: xdrift first (forecast's own order), then xreg1, xreg2…
arima_xreg <- function(model, prep, i) {
  parts <- list()
  if (model$drift) parts$xdrift <- as.numeric(prep$drift_index[i])
  for (j in seq_along(model$exog)) parts[[paste0("xreg", j)]] <- prep$X[i, j]
  if (!length(parts)) return(NULL)
  do.call(cbind, parts)
}

arima_ts <- function(model, y) stats::ts(y, frequency = max(1L, model$s))

arima_call <- function(model, prep, i, fixed = NULL, method = "CSS-ML") {
  o <- model$orders
  args <- list(y = arima_ts(model, prep$y[i]), order = c(o$p, o$d, o$q),
               seasonal = list(order = c(o$P, o$D, o$Q), period = max(1L, model$s)),
               include.mean = model$mean, method = method)
  x <- arima_xreg(model, prep, i)
  if (!is.null(x)) args$xreg <- x
  if (!is.null(model$lambda)) args$lambda <- model$lambda
  if (!is.null(fixed)) args$fixed <- fixed
  do.call(forecast::Arima, args)
}

# The labels of the model's own coefficients, which no exogenous column may take (arima_prepare).
ARIMA_TERM_NAMES <- "^((s?ar|s?ma)[0-9]+|intercept|drift)$"

# The coefficients' names as a person reads them: xdrift → drift, xreg2 → its column (all at once:
# a column named «xreg2» is not renamed again).
arima_coef_labels <- function(model, names) {
  out <- names
  out[out == "xdrift"] <- "drift"
  at <- match(names, paste0("xreg", seq_along(model$exog)))
  out[!is.na(at)] <- unlist(model$exog)[at[!is.na(at)]]
  out
}

# The model applied with fixed coefficients to the grid positions `span`, and its forecast
# over prep$future: fitted values on the span, the forecast with its bands.
arima_filter <- function(model, prep, span, levels) {
  refit <- arima_call(model, prep, span, fixed = model$coef)
  refit$sigma2 <- model$sigma2
  fc <- NULL
  if (length(prep$future)) {
    args <- list(refit, h = length(prep$future), level = unique(levels))
    x <- arima_xreg(model, prep, prep$future)
    if (!is.null(x)) args$xreg <- x
    f <- do.call(forecast::forecast, args)
    band <- function(side, level) as.numeric(f[[side]][, match(level, unique(levels))])
    fc <- list(mean = as.numeric(f$mean), lower_80 = band("lower", levels[[1L]]), upper_80 = band("upper", levels[[1L]]),
               lower = band("lower", levels[[2L]]), upper = band("upper", levels[[2L]]))
  }
  list(refit = refit, fitted = as.numeric(stats::fitted(refit)), forecast = fc)
}

# ── the fit and its rows ────────────────────────────────────────────────────
# The fit alone, shared with the risk simulation (risk_simulation.R): the orders checked against the series, the transform's λ,
# forecast::Arima on the training part (CSS-ML, then ML alone when CSS gives no start). It returns the fit, the model as
# arima_filter applies it (its coefficients and σ² set), the model's name, the observations fitted and the notes so far.
arima_fit_core <- function(prep, orders) {
  o <- prep$params
  s <- o$s
  name <- arima_model_name(orders, s, o$exog)
  if ((orders$P + orders$D + orders$Q) > 0L && s < 2L) {
    tab_refuse("no_season", paste0("seasonal orders need a seasonal period: set s above 1, or P = D = Q = 0"), list(s = s))
  }
  n <- sum(!is.na(prep$y[prep$train]))
  # the spec's 2·s + p + q + d + 10, the season counted when the model has one
  seasonal <- (orders$P + orders$D + orders$Q) > 0L
  need <- 2L * (if (seasonal) s else 1L) + orders$p + orders$q + orders$d + 10L
  if (n < need) {
    tab_refuse("too_short", paste0("the series has ", n, " observations", if (prep$holdout) " before the holdout" else "",
                                   "; ", name, " needs at least ", need, ": a longer series, or lower orders"),
               list(n = n, needed = need))
  }
  lambda <- arima_lambda(prep)
  model <- arima_spec(o, orders, lambda)
  notes <- character()
  method <- "CSS-ML"
  fit <- tryCatch(arima_call(model, prep, prep$train, method = method), error = function(e) e)
  if (inherits(fit, "error")) {
    method <- "ML"
    fit <- tryCatch(arima_call(model, prep, prep$train, method = method), error = function(e) e)
    if (inherits(fit, "error")) {
      tab_refuse("fit_failed", paste0(name, " could not be fitted (", conditionMessage(fit), "): try lower orders, or ",
                                      "another difference"), list(model = name))
    }
    # «The conditional sum of squares gave no start for these orders: the model was fitted by maximum likelihood alone.»
    notes <- c(notes, arima_note("ml_only"))
  }
  model$coef <- fit$coef
  model$sigma2 <- fit$sigma2
  list(fit = fit, model = model, name = name, n = n, lambda = lambda, notes = notes)
}

arima_fit_orders <- function(task, prep, orders, extra = NULL) {
  o <- prep$params
  s <- o$s
  core <- arima_fit_core(prep, orders)
  fit <- core$fit
  model <- core$model
  name <- core$name
  n <- core$n
  lambda <- core$lambda
  notes <- core$notes
  rows <- tab_rows_start(task)
  levels <- arima_levels(o$alpha)
  whole <- arima_filter(model, prep, prep$span, levels)

  # ── model: texts, then the numbers of the fit ──
  tab_text(rows, "model", "formula", arima_formula(o, model, name))
  tab_text(rows, "model", "software", tab_software(ARIMA_PACKAGES))
  tab_text(rows, "model", "model_name", name)
  tab_text(rows, "model", "frequency", o$frequency)
  tab_text(rows, "model", "transform", o$transform)
  tab_text(rows, "model", "time_kind", prep$kind)
  acc <- forecast::accuracy(fit)
  tab_number(rows, "model", "n", n)
  tab_number(rows, "model", "n_missing", prep$n_missing)
  for (k in c("p", "d", "q", "P", "D", "Q")) tab_number(rows, "model", k, orders[[k]])
  tab_number(rows, "model", "s", s)
  tab_number(rows, "model", "log_lik", fit$loglik)
  tab_number(rows, "model", "aic", fit$aic)
  tab_number(rows, "model", "aicc", fit$aicc)
  tab_number(rows, "model", "bic", fit$bic)
  tab_number(rows, "model", "sigma2", fit$sigma2)
  tab_number(rows, "model", "rmse", acc[1L, "RMSE"])
  tab_number(rows, "model", "mae", acc[1L, "MAE"])
  tab_number(rows, "model", "mape", acc[1L, "MAPE"])
  tab_number(rows, "model", "mase", acc[1L, "MASE"])
  tab_number(rows, "model", "holdout", prep$holdout)
  if (prep$holdout > 0L) {
    args <- list(fit, h = length(prep$test), level = unique(levels))
    x <- arima_xreg(model, prep, prep$test)
    if (!is.null(x)) args$xreg <- x
    fc <- do.call(forecast::forecast, args)
    test <- forecast::accuracy(fc, prep$y[prep$test])
    measures <- c(rmse = "RMSE", mae = "MAE", mape = "MAPE", mase = "MASE")
    for (k in names(measures)) tab_number(rows, "model", paste0("holdout_", k), test["Test set", measures[[k]]])
    # «The coefficients are fitted on the observations before the last {n}; the forecast after the last observation
    # applies them to the whole series.»
    notes <- c(notes, arima_note("holdout_fit", n = prep$holdout))
  }
  if (o$transform == "box_cox") tab_number(rows, "model", "box_cox_lambda", lambda)
  tab_number(rows, "model", "horizon", length(prep$future))
  roots <- arima_roots(fit$coef, orders)
  for (i in seq_along(roots$ar)) tab_number(rows, "model", paste0("ar_root_modulus_", i), roots$ar[[i]])
  for (i in seq_along(roots$ma)) tab_number(rows, "model", paste0("ma_root_modulus_", i), roots$ma[[i]])
  if (!is.null(extra)) extra(rows)

  # ── coefficients ──
  est <- fit$coef
  se <- if (length(est)) sqrt(pmax(diag(fit$var.coef), 0)) else numeric()
  se[!is.finite(se)] <- NA_real_
  if (length(est) && any(is.na(se))) {
    # «Some standard errors could not be computed (the likelihood is flat there): an order too many?»
    notes <- c(notes, arima_note("se_missing"))
  }
  z <- est / se
  pv <- 2 * stats::pnorm(-abs(z))
  crit <- stats::qnorm(1 - o$alpha / 2)
  labels <- arima_coef_labels(model, names(est))
  for (i in seq_along(est)) {
    tab_number(rows, "coef", "estimate", est[[i]], level = labels[[i]])
    tab_number(rows, "coef", "std_error", se[[i]], level = labels[[i]])
    tab_number(rows, "coef", "statistic", z[[i]], level = labels[[i]])
    tab_p(rows, "coef", pv[[i]], level = labels[[i]])
    tab_number(rows, "coef", "ci_low", est[[i]] - crit * se[[i]], level = labels[[i]])
    tab_number(rows, "coef", "ci_high", est[[i]] + crit * se[[i]], level = labels[[i]])
  }

  # ── assumptions ──
  z_series <- arima_differenced(model, prep$y[prep$train])
  resid <- as.numeric(stats::residuals(fit))
  arima_checks(rows, model, orders, o, z_series, resid, roots, fit)

  # ── charts ──
  arima_charts(task, rows, prep, model, whole, z_series, resid)

  # ── notes ──
  if (prep$gaps > 0L) {
    # «{n} period(s) has/have no value: the likelihood skips them; ADF, KPSS and the correlograms read the series
    # without them.»
    notes <- c(notes, arima_note(if (prep$gaps == 1L) "gap" else "gaps", n = prep$gaps))
  }
  if (prep$dropped_future > 0L) {
    # «{n} rows after the last value are left out: a future row needs every exogenous value, and the future rows must
    # follow each other without a gap.»
    notes <- c(notes, arima_note(if (prep$dropped_future == 1L) "future_dropped_one" else "future_dropped", n = prep$dropped_future))
  }
  if (length(o$exog) && !length(prep$future)) {
    # «No forecast: with exogenous columns the forecast covers the rows after the last value whose exogenous values are
    # given, and there are none.»
    notes <- c(notes, arima_note("no_forecast"))
  }
  if (model$drift_dropped) notes <- c(notes, arima_note("drift_dropped"))  # «The drift is left out: with d + D = 2 the model has no drift.»
  if (!identical(as.integer(fit$code %||% 0L), 0L)) {
    # «The optimizer did not report convergence (code {code}): read the coefficients with care.»
    notes <- c(notes, arima_note("not_converged", code = fit$code))
  }
  for (sentence in notes) tab_note(rows, sentence)

  # ── output, card ──
  output <- if (identical(task$params$output %||% "results", "forecast")) arima_output(prep, whole) else NULL
  card <- arima_card(task, prep, model, fit, levels)
  list(rows = rows, output = output, card = card, native = fit)
}

# The transform's λ: NULL for none, 0 for the log, Box–Cox's given or estimated on the training
# part (forecast::BoxCox.lambda); a value at or below 0 stops the node.
arima_lambda <- function(prep) {
  o <- prep$params
  if (!o$transform %in% c("log", "box_cox")) return(NULL)
  if (any(prep$y[prep$span] <= 0, na.rm = TRUE)) {
    tab_refuse("not_positive", paste0("the column «", o$value, "» holds 0 or a negative value: a log or Box–Cox transform ",
                                      "needs positive values; use transform = none"), list(column = o$value))
  }
  if (o$transform == "log") return(0)
  if (identical(o$box_cox_lambda %||% "auto", "auto")) {
    return(forecast::BoxCox.lambda(stats::ts(prep$y[prep$train], frequency = max(1L, o$s))))
  }
  as.numeric(o$box_cox_lambda)
}

# «boxes ~ SARIMA(1,1,1)(0,1,1)[12]», «log(boxes) ~ price + promo + drift, errors ARIMA(1,0,1)».
arima_formula <- function(o, model, name) {
  left <- switch(o$transform, log = paste0("log(", o$value, ")"),
                 box_cox = paste0("BoxCox(", o$value, ", λ = ", signif(model$lambda, 4), ")"), o$value)
  right <- c(o$exog, if (model$drift) "drift")
  core <- sub("^S?ARIMAX", "SARIMA", sub("^ARIMAX", "ARIMA", name))
  if (!length(right)) return(paste0(left, " ~ ", name))
  paste0(left, " ~ ", paste(right, collapse = " + "), ", errors ", core)
}

# The moduli of the roots of each fitted polynomial in its own operator: the AR ones first
# (non-seasonal, then seasonal in B^s), then the MA ones. Above 1: stationary, invertible.
arima_roots <- function(coef, orders) {
  modulus <- function(prefix, k, sign) {
    if (k == 0L) return(numeric())
    phi <- coef[paste0(prefix, seq_len(k))]
    last <- max(c(0L, which(abs(phi) > 0)))
    if (last == 0L) return(rep(Inf, k))
    sort(Mod(polyroot(c(1, sign * phi[seq_len(last)]))))
  }
  list(ar = c(modulus("ar", orders$p, -1), modulus("sar", orders$P, -1)),
       ma = c(modulus("ma", orders$q, 1), modulus("sma", orders$Q, 1)))
}

# The training series as the model sees it (transformed), differenced d times and D seasonal
# times.
arima_differenced <- function(model, y) {
  x <- if (is.null(model$lambda)) y else as.numeric(forecast::BoxCox(y, model$lambda))
  if (model$orders$D > 0L) x <- diff(x, lag = model$s, differences = model$orders$D)
  if (model$orders$d > 0L) x <- diff(x, differences = model$orders$d)
  as.numeric(x)
}

# One check's rows: H0, the numbers, the verdict, the advice when it is not ok.
arima_check <- function(rows, id, h0, verdict, advice = NA_character_, statistic = NULL, df = NULL, df_2 = NULL,
                        p = NULL, threshold = NULL, value = NULL, advice_id = NULL) {
  tab_text(rows, "assumption", "h0", h0, level = id)
  if (!is.null(statistic)) tab_number(rows, "assumption", "statistic", statistic, level = id)
  if (!is.null(df)) tab_number(rows, "assumption", "df", df, level = id)
  if (!is.null(df_2)) tab_number(rows, "assumption", "df_2", df_2, level = id)
  if (!is.null(p)) tab_p(rows, "assumption", p, level = id)
  if (!is.null(threshold)) tab_number(rows, "assumption", "threshold", threshold, level = id)
  if (!is.null(value)) tab_number(rows, "assumption", "value", value, level = id)
  tab_text(rows, "assumption", "verdict", verdict, level = id)
  if (verdict != "ok" && !is.na(advice)) tab_text(rows, "assumption", "advice", advice, level = id)
  # the sentence the report says, when the check has several (a test at the end of its table)
  if (!is.null(advice_id)) tab_text(rows, "assumption", "advice_id", advice_id, level = id)
  invisible(rows)
}

# ── the tables of ADF and KPSS ──────────────────────────────────────────────
# tseries::adf.test and kpss.test read their p-value from a table by linear interpolation (`approx(..., rule = 2)`)
# and clip it at the table's ends: ADF's 0.01 and 0.99 (Banerjee et al., table 4.2), KPSS's 0.01 and 0.10 (Kwiatkowski
# et al., table 1). A p-value at an end says only p ≤ 0.01 (or ≥ 0.99, ≥ 0.10): it answers an alpha on the side of the
# end it lies on and none on the other. The side is read from the statistic (the quantile it passes), not from the
# clipped p-value, because `p == 0.01` also holds for a statistic exactly at the 1 % quantile and, at alpha = 0.01,
# `p < alpha` would read a p below 0.01 as false. The self-test checks these tables against tseries'.
ARIMA_ADF_P <- c(0.01, 0.025, 0.05, 0.1, 0.9, 0.95, 0.975, 0.99)
ARIMA_ADF_N <- c(25, 50, 100, 250, 500, 1e+05)
ARIMA_ADF_Q <- -cbind(c(4.38, 4.15, 4.04, 3.99, 3.98, 3.96), c(3.95, 3.8, 3.73, 3.69, 3.68, 3.66),
                      c(3.6, 3.5, 3.45, 3.43, 3.42, 3.41), c(3.24, 3.18, 3.15, 3.13, 3.13, 3.12),
                      c(1.14, 1.19, 1.22, 1.23, 1.24, 1.25), c(0.8, 0.87, 0.9, 0.92, 0.93, 0.94),
                      c(0.5, 0.58, 0.62, 0.64, 0.65, 0.66), c(0.15, 0.24, 0.28, 0.31, 0.32, 0.33))
ARIMA_KPSS_P <- c(0.01, 0.025, 0.05, 0.1)
ARIMA_KPSS_Q <- list(Level = c(0.739, 0.574, 0.463, 0.347), Trend = c(0.216, 0.176, 0.146, 0.119))

# Where the ADF statistic of a regression on `n` differences falls against the table's ends at that size:
# "below" (p < 0.01), "above" (p > 0.99) or "inside".
arima_adf_side <- function(stat, n) {
  q <- vapply(seq_along(ARIMA_ADF_P), function(i) stats::approx(ARIMA_ADF_N, ARIMA_ADF_Q[, i], n, rule = 2)$y, numeric(1))
  if (stat < q[[1L]]) "below" else if (stat > q[[length(q)]]) "above" else "inside"
}

# The same for KPSS, whose large statistics are the small p-values: "below" (η above the 1 % quantile, p < 0.01),
# "above" (η under the 10 % quantile, p > 0.10) or "inside".
arima_kpss_side <- function(eta, null) {
  q <- ARIMA_KPSS_Q[[null]]
  if (eta > max(q)) "below" else if (eta < min(q)) "above" else "inside"
}

# Whether the test rejects at alpha: TRUE, FALSE, or NA when the table's end cannot say (p < lo rejects at alpha ≥ lo;
# p > hi keeps at alpha ≤ hi). Inside the table p is compared with alpha.
arima_table_rejects <- function(p, side, alpha, lo, hi) {
  switch(side, inside = p < alpha, below = if (alpha >= lo) TRUE else NA, above = if (alpha <= hi) FALSE else NA)
}

arima_checks <- function(rows, model, orders, o, z, resid, roots, fit) {
  alpha <- o$alpha
  zz <- z[!is.na(z)]
  diff_said <- "the differenced series"
  short <- length(zz) < 12L
  adf <- if (short) NULL else suppressWarnings(tseries::adf.test(zz))
  kpss_null <- if (model$drift && (orders$d + orders$D) == 0L) "Trend" else "Level"
  kpss <- if (short) NULL else suppressWarnings(tseries::kpss.test(zz, null = kpss_null))
  if (short) {
    for (id in c("adf", "kpss", "stationarity")) {
      arima_check(rows, id, paste0("Not computed: ", diff_said, " has fewer than 12 values."), "not_computed",
                  "The differenced series is too short for the unit-root tests: a longer series.")
    }
  } else {
    # tseries reads both p-values from a table and clips them at its ends; the verdict is read from the side of the
    # statistic, never from `p == 0.01` (which is wrong at alpha = 0.01), and where the end cannot answer alpha it is
    # not computed
    adf_rejects <- arima_table_rejects(adf$p.value, arima_adf_side(unname(adf$statistic), length(zz) - 1L), alpha,
                                       min(ARIMA_ADF_P), max(ARIMA_ADF_P))
    kpss_rejects <- arima_table_rejects(kpss$p.value, arima_kpss_side(unname(kpss$statistic), kpss_null), alpha,
                                        min(ARIMA_KPSS_P), max(ARIMA_KPSS_P))
    end_advice <- "The p-value is at an end of the test's table and alpha lies beyond it: the table cannot say whether p < alpha."
    if (is.na(adf_rejects)) {
      arima_check(rows, "adf", "The differenced series has a unit root.", "not_computed", end_advice,
                  statistic = unname(adf$statistic), advice_id = "table_end")
    } else {
      arima_check(rows, "adf", "The differenced series has a unit root.", if (adf_rejects) "ok" else "failed",
                  "ADF does not reject a unit root: difference once more (d + 1), or let orders = auto choose.",
                  statistic = unname(adf$statistic), p = adf$p.value)
    }
    kpss_h0 <- if (kpss_null == "Trend") "The differenced series is stationary around a trend." else "The differenced series is stationary."
    if (is.na(kpss_rejects)) {
      arima_check(rows, "kpss", kpss_h0, "not_computed", end_advice, statistic = unname(kpss$statistic), advice_id = "table_end")
    } else {
      arima_check(rows, "kpss", kpss_h0, if (kpss_rejects) "failed" else "ok",
                  "KPSS rejects stationarity: difference once more (d + 1), or let orders = auto choose.",
                  statistic = unname(kpss$statistic), p = kpss$p.value)
    }
    # the four cases of the two tests together: not computed when either test is
    if (is.na(adf_rejects) || is.na(kpss_rejects)) {
      arima_check(rows, "stationarity", "ADF rejects a unit root and KPSS keeps stationarity.", "not_computed", end_advice,
                  advice_id = "table_end")
    } else {
      case <- if (adf_rejects && !kpss_rejects) "ok" else if (!adf_rejects && kpss_rejects) "failed" else "doubtful"
      advice <- if (!adf_rejects && kpss_rejects) {
        "ADF keeps a unit root and KPSS rejects stationarity: difference once more (d + 1), or let orders = auto choose."
      } else if (adf_rejects && kpss_rejects) {
        "Both tests reject: a trend or a break, look at the series."
      } else {
        "Neither test rejects: the series is too short or too noisy for them to tell; read the correlograms."
      }
      arima_check(rows, "stationarity", "ADF rejects a unit root and KPSS keeps stationarity.", case, advice)
    }
  }
  # the roots
  moduli <- c(roots$ar, roots$ma)
  if (!length(moduli)) {
    arima_check(rows, "roots", "Every root of the AR and MA polynomials is outside the unit circle.", "not_computed",
                "No AR or MA order: there is no root to check.")
  } else {
    smallest <- min(moduli)
    verdict <- if (smallest <= 1 + 1e-8) "failed" else if (smallest < ARIMA_NEAR_UNIT) "doubtful" else "ok"
    arima_check(rows, "roots", "Every root of the AR and MA polynomials is outside the unit circle.", verdict,
                "A root near the unit circle: an order too many, or a difference missing.",
                threshold = ARIMA_NEAR_UNIT, value = smallest)
  }
  # white noise: Ljung–Box at 10 and 2·s lags, fitdf = p + q + P + Q
  fitdf <- orders$p + orders$q + orders$P + orders$Q
  n_resid <- sum(!is.na(resid))
  ljung <- function(id, lag, h0, advice, none) {
    if (is.null(lag) || lag <= fitdf || lag >= n_resid) {
      arima_check(rows, id, h0, "not_computed", none)
      return(invisible())
    }
    b <- stats::Box.test(resid, lag = lag, type = "Ljung-Box", fitdf = fitdf)
    arima_check(rows, id, h0, if (b$p.value < alpha) "failed" else "ok", advice,
                statistic = unname(b$statistic), df = unname(b$parameter), p = b$p.value)
  }
  ljung("ljung_box_10", 10L, "The residuals are not autocorrelated up to lag 10.",
        "Structure left in the residuals: raise an order, or try orders = auto.",
        paste0("Not computed: the model has ", fitdf, " ARMA coefficients, as many as the 10 lags or more."))
  ljung("ljung_box_2s", if (o$s > 1L) 2L * o$s else NULL, "The residuals are not autocorrelated up to two seasons.",
        "Seasonal structure left in the residuals: raise a seasonal order, or try orders = auto.",
        if (o$s > 1L) "Not computed: two seasons are more lags than the residuals allow, or fewer than the coefficients." else
          "Not computed: the series has no season (s = 1).")
  # constant variance: ARCH LM and the first third against the last third
  e <- resid[!is.na(resid)]
  lags <- if (o$s > 1L) o$s else ARIMA_ARCH_LAGS
  if (length(e) < 3L * lags + 10L) {
    arima_check(rows, "arch_lm", "The residuals' variance does not depend on the past residuals.", "not_computed",
                paste0("Not computed: ", lags, " lags need at least ", 3L * lags + 10L, " residuals."))
  } else {
    a <- FinTS::ArchTest(e, lags = lags)
    arima_check(rows, "arch_lm", "The residuals' variance does not depend on the past residuals.",
                if (a$p.value < alpha) "failed" else "ok", "The variance changes: try transform = log or box_cox.",
                statistic = unname(a$statistic), df = unname(a$parameter), p = a$p.value)
  }
  burn <- orders$d + orders$D * model$s
  e2 <- if (burn > 0L) resid[-seq_len(min(burn, length(resid)))] else resid
  e2 <- e2[!is.na(e2)]
  h <- as.integer(round(length(e2) / 3))
  if (h < 5L) {
    arima_check(rows, "variance_ratio", "The residuals' variance is the same at the start and at the end.", "not_computed",
                "Not computed: too few residuals to compare thirds.")
  } else {
    ratio <- sum(utils::tail(e2, h)^2) / sum(utils::head(e2, h)^2)
    p <- 2 * min(stats::pf(ratio, h, h), stats::pf(ratio, h, h, lower.tail = FALSE))
    arima_check(rows, "variance_ratio", "The residuals' variance is the same at the start and at the end.",
                if (p < alpha) "failed" else "ok", "The variance changes: try transform = log or box_cox.",
                statistic = ratio, df = h, df_2 = h, p = min(1, p))
  }
  # normality
  if (length(e) < 3L) {
    arima_check(rows, "jarque_bera", "The residuals' skewness and kurtosis are a normal distribution's.", "not_computed",
                "Not computed: too few residuals.")
  } else {
    jb <- tseries::jarque.bera.test(e)
    arima_check(rows, "jarque_bera", "The residuals' skewness and kurtosis are a normal distribution's.",
                if (jb$p.value < alpha) "failed" else "ok", "The bands assume normal errors: read them with care.",
                statistic = unname(jb$statistic), df = unname(jb$parameter), p = jb$p.value)
  }
  invisible(rows)
}

# Evenly spaced positions, the first and the last kept, at most `max`.
arima_thin <- function(n, max) {
  if (n <= max) return(seq_len(n))
  unique(round(seq(1, n, length.out = max)))
}

arima_charts <- function(task, rows, prep, model, whole, z, resid) {
  max_points <- tab_limit(task, "max_chart_points")
  span <- prep$span
  future <- prep$future
  # the series, the fitted values and the forecast, which starts at the last observation
  series <- 2L + if (length(future)) 5L else 0L
  keep <- arima_thin(length(span), max(2L, max_points %/% series))
  tab_points(rows, "curve", "forecast", "observed", x = prep$x0[span][keep], y = prep$y[span][keep])
  tab_points(rows, "curve", "forecast", "fitted", x = prep$x0[span][keep], y = whole$fitted[keep])
  if (length(future)) {
    at <- arima_thin(length(future) + 1L, max(2L, max_points %/% series))
    x <- c(prep$x0[prep$last], prep$x0[future])[at]
    joined <- prep$y[prep$last]
    for (stat in c("forecast", "lower_80", "upper_80", "lower", "upper")) {
      tab_points(rows, "curve", "forecast", stat, x = x, y = c(joined, whole$forecast[[if (stat == "forecast") "mean" else stat]])[at])
    }
  }
  # the residuals over time
  train <- prep$train
  keep <- arima_thin(length(train), max_points)
  tab_points(rows, "curve", "residuals", "y", x = prep$x0[train][keep], y = resid[keep])
  # Q–Q of the residuals with its line, their histogram
  e <- resid[is.finite(resid)]
  qq <- tab_qq(e, tab_limit(task, "qq_points"))
  tab_points(rows, "point", "qq", "y", x = qq$x, y = qq$y)
  if (nrow(qq) >= 2L) {
    line <- tab_qq_line(e)
    ends <- range(qq$x)
    tab_points(rows, "curve", "qq", "y", x = ends, y = line[["intercept"]] + line[["slope"]] * ends, level_2 = "line")
  }
  h <- tab_hist(e)
  tab_points(rows, "hist", "residual", "count", x = h$x0, y = h$count, x1 = h$x1)
  # the correlograms: the differenced series and the residuals, with ±1.96/√n
  lag_max <- tab_limit(task, "max_lags")
  correlogram <- function(id, x, partial) {
    n <- sum(!is.na(x))
    k <- min(lag_max, n - 1L)
    if (k < 1L) return(invisible())
    r <- if (partial) {
      stats::pacf(x, lag.max = k, plot = FALSE, na.action = stats::na.pass)
    } else {
      stats::acf(x, lag.max = k, plot = FALSE, na.action = stats::na.pass)
    }
    values <- as.numeric(r$acf)
    lags <- as.numeric(r$lag)
    if (!partial) {
      values <- values[-1L]
      lags <- lags[-1L]
    }
    # acf() counts lags in seasons on a seasonal ts; these are plain vectors, so lags are steps
    tab_points(rows, "lag", id, if (partial) "pacf" else "acf", x = lags, y = values)
    tab_number(rows, "lag", "bound", stats::qnorm(0.975) / sqrt(n), level = id)
  }
  correlogram("acf", z, FALSE)
  correlogram("pacf", z, TRUE)
  correlogram("resid_acf", resid, FALSE)
  correlogram("resid_pacf", resid, TRUE)
  invisible(rows)
}

# The output as an Arrow table with the time column first, in the type of the input's own (an Int64 stays Int64, a
# timestamp keeps its unit and zone): `df` holds the other columns. The columns are passed by position and named after:
# a time column called «schema» passed by its name would be arrow_table's own `schema` argument.
arima_table <- function(df, time_name, time_array) {
  others <- setdiff(names(df), time_name)
  cols <- c(list(time_array), unname(as.list(df[others])))
  tbl <- do.call(arrow::arrow_table, stats::setNames(cols, paste0("c", seq_along(cols))))
  names(tbl) <- c(time_name, others)
  tbl
}

# The forecast output: one row a period of the span and of the future.
arima_output <- function(prep, whole) {
  i <- c(prep$span, prep$future)
  nf <- length(prep$future)
  na <- rep(NA_real_, length(prep$span))
  out <- data.frame(observed = c(prep$y[prep$span], rep(NA_real_, nf)),
                    fitted = c(whole$fitted, rep(NA_real_, nf)), stringsAsFactors = FALSE)
  fc <- whole$forecast
  out$forecast <- c(na, if (nf) fc$mean else numeric())
  out$lower_80 <- c(na, if (nf) fc$lower_80 else numeric())
  out$upper_80 <- c(na, if (nf) fc$upper_80 else numeric())
  out$lower <- c(na, if (nf) fc$lower else numeric())
  out$upper <- c(na, if (nf) fc$upper else numeric())
  out$is_forecast <- c(rep(FALSE, length(prep$span)), rep(TRUE, nf))
  arima_table(out, "time", prep$time_array(i))
}

# A note the report says in the page's language (layouts/common.py, structured_note): «@id name=value …», the
# values whole numbers or a code. The English of each is in a comment where it is written.
arima_note <- function(id, ...) {
  values <- list(...)
  paste(c(paste0("@", id), vapply(names(values), function(k) paste0(k, "=", values[[k]]), "")), collapse = " ")
}

# ── the card ────────────────────────────────────────────────────────────────
arima_card <- function(task, prep, model, fit, levels) {
  o <- prep$params
  card <- tab_card(task, NULL, ARIMA_PACKAGES)
  card$inputs <- c(list(list(name = o$time, type = "time"), list(name = o$value, type = "number")),
                   lapply(o$exog, function(e) list(name = e, type = "number")))
  labels <- arima_coef_labels(model, names(fit$coef))
  card$terms <- lapply(seq_along(labels), function(i) {
    list(term = labels[[i]], level = NULL, variables = if (labels[[i]] %in% o$exog) list(labels[[i]]) else list())
  })
  card$coefficients <- as.list(unname(tab_finite(fit$coef)))  # in the order of `terms`
  card$vcov <- if (length(fit$coef)) tab_card_vcov(task, as.matrix(fit$var.coef)) else NULL
  card$alpha <- o$alpha
  card$formula <- arima_formula(o, model, arima_model_name(model$orders, o$s, o$exog))
  keep <- utils::tail(prep$span, ARIMA_CARD_SERIES)
  exog <- list()
  for (j in seq_along(o$exog)) exog[[o$exog[[j]]]] <- as.list(prep$X[keep, j])
  card$arima <- list(
    orders = c(model$orders, list(s = o$s)), frequency = o$frequency,
    # by name, as card.schema.json says (tab_finite drops them); arima_model_of_card reads them in this order
    # an object even without coefficients (ARIMA(0,1,0) without a mean): an empty list is written [], which the schema refuses
    coefficients = tab_object(as.list(stats::setNames(tab_finite(fit$coef), names(fit$coef)))), sigma2 = fit$sigma2, transform = o$transform,
    box_cox_lambda = if (is.null(model$lambda)) NULL else model$lambda, drift = model$drift, mean = model$mean,
    exog = as.list(o$exog), horizon = o$horizon,
    last = list(time_kind = prep$kind, t0 = tab_label(prep$times[[prep$first]]),
                time = as.list(tab_label(prep$times[keep])), value = as.list(prep$y[keep]), exog = tab_object(exog)))
  card
}

# The model a card holds, as arima_filter applies it (the card's numbers are the fit's to the
# last bit: tab_json writes 17 significant digits).
arima_model_of_card <- function(card) {
  a <- card$arima
  orders <- lapply(a$orders[c("p", "d", "q", "P", "D", "Q")], as.integer)
  coef <- unlist(lapply(a$coefficients, function(x) if (is.null(x)) NA_real_ else as.numeric(x)))
  list(orders = orders, s = as.integer(a$orders$s), mean = isTRUE(a$mean), drift = isTRUE(a$drift),
       exog = unlist(a$exog %||% list()), lambda = if (is.null(a$box_cox_lambda)) NULL else as.numeric(a$box_cox_lambda),
       transform = a$transform, coef = coef, sigma2 = as.numeric(a$sigma2))
}

# A time of the card (a label) back to its kind.
arima_card_time <- function(labels, kind) {
  labels <- unlist(labels)
  switch(kind, date = as.Date(labels), datetime = as.POSIXct(labels, tz = "UTC"), as.numeric(labels))
}

arima_card_t0 <- function(card) arima_card_time(card$arima$last$t0, card$arima$last$time_kind)

# The series the card keeps, as rows of the input.
arima_card_history <- function(card) {
  a <- card$arima
  p <- card$params
  df <- data.frame(arima_card_time(a$last$time, a$last$time_kind))
  names(df) <- p$time
  df[[p$value]] <- vapply(a$last$value, function(x) if (is.null(x)) NA_real_ else as.numeric(x), numeric(1))
  for (e in unlist(a$exog %||% list())) {
    df[[e]] <- vapply(a$last$exog[[e]], function(x) if (is.null(x)) NA_real_ else as.numeric(x), numeric(1))
  }
  df
}

TAB_COMMON_TESTS[["arima: the notes are ids the report words, and the card's coefficients are by name"]] <- function(code) {
  df <- tab_read_csv(file.path(code, "reference", "data", "sarima_monthly.csv"), list(month = "date", boxes = "number"))
  params <- list(time = "month", value = "boxes", frequency = "month", p = 1, d = 1, q = 1, P = 0, D = 1, Q = 1, holdout = 12)
  task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "timeseries", group_version = "selftest", model = "arima",
               variant = tab_variant_of("arima", params), params = params, output = "results", write_output = FALSE,
               rows_from = "run", limits = tab_catalog()$limits, .frame = df)
  out <- fit_arima(task)
  f <- tab_results_frame(out$rows)
  notes <- f$text[startsWith(f$stat, "note_")]
  .tab_expect("@holdout_fit n=12" %in% notes && !any(grepl(" the ", notes, fixed = TRUE)), paste(notes, collapse = " | "))
  coef <- out$card$arima$coefficients
  .tab_expect(identical(names(coef), c("ar1", "ma1", "sma1")) && is.numeric(coef[["ar1"]]), paste(names(coef), collapse = ", "))
  .tab_expect(identical(arima_note("gaps", n = 3L), "@gaps n=3"), "the encoding")
}

.arima_test_task <- function(df, params) {
  list(protocol = TAB_PROTOCOL, mode = "fit", group = "timeseries", group_version = "selftest", model = "arima",
       variant = tab_variant_of("arima", params), params = params, output = "results", write_output = FALSE,
       rows_from = "run", limits = tab_catalog()$limits, .frame = df)
}

TAB_COMMON_TESTS[["arima: an exogenous column named as a term is refused with a sentence, and one named as a place keeps its name"]] <- function(code) {
  df <- tab_read_csv(file.path(code, "reference", "data", "sarima_monthly.csv"), list(month = "date", boxes = "number"))
  task <- function(params) .arima_test_task(df, params)
  set.seed(3)
  for (name in c("drift", "intercept", "ar1", "ma2", "sar1", "sma1")) {
    df[[name]] <- stats::rnorm(nrow(df))
    params <- list(time = "month", value = "boxes", frequency = "month", p = 1, d = 1, q = 0, exog = list(name), drift = TRUE)
    msg <- .tab_refused_with("exog_name", fit_arima(task(params)))
    .tab_expect(grepl(paste0("«", name, "» has the name of a term of the model"), msg, fixed = TRUE), name, ": ", msg)
    df[[name]] <- NULL
  }
  # a column named as the place of another (xreg2) keeps its name, and the other its own
  df$xreg2 <- stats::rnorm(nrow(df))
  df$price <- stats::rnorm(nrow(df))
  params <- list(time = "month", value = "boxes", frequency = "month", p = 1, d = 1, q = 0, exog = list("xreg2", "price"))
  .tab_expect(identical(arima_coef_labels(list(exog = c("xreg2", "price")), c("ar1", "xreg1", "xreg2")), c("ar1", "xreg2", "price")),
              "the labels")
  keys <- tab_results_frame(fit_arima(task(params))$rows)$key
  .tab_expect(all(c("coef|ar1||estimate", "coef|xreg2||estimate", "coef|price||estimate") %in% keys) && !"coef|xreg1||estimate" %in% keys,
              "the rows' coefficients: ", paste(grep("^coef\\|.*\\|estimate$", keys, value = TRUE), collapse = ", "))
}

TAB_COMMON_TESTS[["arima: a card without coefficients (ARIMA(0,1,0) without a mean) matches the schema"]] <- function(code) {
  df <- tab_read_csv(file.path(code, "reference", "data", "sarima_monthly.csv"), list(month = "date", boxes = "number"))
  # no coefficient: an object {}, not an array
  out <- fit_arima(.arima_test_task(df, list(time = "month", value = "boxes", frequency = "month", p = 0, d = 1, q = 0, P = 0, D = 0, Q = 0)))
  text <- tab_card_text(out$card)
  back <- jsonlite::fromJSON(text, simplifyVector = FALSE)
  problems <- tab_schema_problems(back, jsonlite::fromJSON(file.path(code, "card.schema.json"), simplifyVector = FALSE))
  .tab_expect(!length(problems), "the card against the schema: ", paste(problems, collapse = "; "))
  .tab_expect(grepl('"coefficients":{}', gsub("[[:space:]]", "", text), fixed = TRUE), "the coefficients are not {}")
}

TAB_COMMON_TESTS[["arima: ADF's and KPSS's tables are tseries', and an end of the table answers only the alphas on its side"]] <- function(code) {
  # the sides read from the statistic agree with the clipped p-values of tseries, on series of every kind
  set.seed(11)
  for (z in list(stats::rnorm(120), cumsum(stats::rnorm(120)), as.numeric(stats::arima.sim(list(ar = 0.5), 80)),
                 cumsum(stats::rnorm(60)) + 0.5 * seq_len(60), stats::rnorm(40, sd = 3))) {
    adf <- suppressWarnings(tseries::adf.test(z))
    side <- arima_adf_side(unname(adf$statistic), length(z) - 1L)
    p <- adf$p.value
    .tab_expect(identical(side == "below", p == 0.01) && identical(side == "above", p == 0.99), "ADF side ", side, " with p = ", p)
    for (null in c("Level", "Trend")) {
      kpss <- suppressWarnings(tseries::kpss.test(z, null = null))
      side <- arima_kpss_side(unname(kpss$statistic), null)
      .tab_expect(identical(side == "below", kpss$p.value == 0.01) && identical(side == "above", kpss$p.value == 0.1),
                  "KPSS ", null, " side ", side, " with p = ", kpss$p.value)
    }
  }
  # an end answers the alphas on its side: p < 0.01 rejects at 0.01 (where `p < alpha` would not), is not computed at 0.001
  .tab_expect(isTRUE(arima_table_rejects(0.01, "below", 0.01, 0.01, 0.99)) && is.na(arima_table_rejects(0.01, "below", 0.001, 0.01, 0.99)) &&
                isTRUE(arima_table_rejects(0.01, "below", 0.2, 0.01, 0.99)), "the lower end")
  .tab_expect(identical(arima_table_rejects(0.1, "above", 0.1, 0.01, 0.1), FALSE) && is.na(arima_table_rejects(0.1, "above", 0.2, 0.01, 0.1)) &&
                identical(arima_table_rejects(0.1, "above", 0.001, 0.01, 0.1), FALSE), "the upper end")
  .tab_expect(identical(arima_table_rejects(0.03, "inside", 0.05, 0.01, 0.1), TRUE) &&
                identical(arima_table_rejects(0.03, "inside", 0.02, 0.01, 0.1), FALSE), "inside the table")
  # the table keeps the type of the time
  for (type in list(arrow::int64(), arrow::int32(), arrow::date32(), arrow::timestamp("ms", "Europe/Rome"))) {
    values <- if (type$ToString() %in% c("int64", "int32")) c(1, 2) else if (type$ToString() == "date32") as.Date("2024-01-01") + 0:1 else
      as.POSIXct("2024-01-01", tz = "UTC") + c(0, 3600)
    tbl <- arima_table(data.frame(observed = c(1, 2)), "t", ts_time_array(values, type))
    .tab_expect(tbl$schema$GetFieldByName("t")$type == type && identical(names(tbl), c("t", "observed")), "the type ", type$ToString())
  }
  # a time column called like one of arrow_table's own arguments is a column like any other (lot D9)
  for (name in c("schema", "...")) {
    tbl <- arima_table(data.frame(observed = c(1, 2), pred = c(3, 4)), name, ts_time_array(c(1, 2), arrow::int64()))
    .tab_expect(identical(names(tbl), c(name, "observed", "pred")) && tbl$schema$GetFieldByName(name)$type == arrow::int64() &&
                  identical(as.numeric(as.data.frame(tbl)$pred), c(3, 4)), "a time called ", name)
  }
}
