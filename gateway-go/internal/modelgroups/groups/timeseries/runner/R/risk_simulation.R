# Risk simulation (docs/specs/garch-risk.md, «Part 2 — the node `risk_simulation`»; lot G3): fit_risk_simulation(task).
#
# The node fits its own model of the series with the GARCH node's functions (garch.R: garch_series, garch_fit, garch_model,
# garch_checks) or ARIMA's (arima.R: arima_prepare, arima_fit_core; arima_auto.R's search with automatic orders), so that the same
# options give the same estimates, then simulates N paths of h steps from the end of the series with the engine of lot G2
# (ts_sim.R: seed blocks on L'Ecuyer-CMRG streams, batches from the memory limit, the Parquet file, two passes). It makes no model:
# `card = NULL`, no `predict` route.
#
#   risk_params(params)              the node's options, the catalogue's defaults where a key is not given
#   risk_garch_state(g, d, m)        a GARCH's end state on the series' units: the last returns, shocks, variances, z
#   risk_garch_paths(st, Z)          the GARCH recursion given the innovations Z (h × n): returns and σ, elementwise
#   risk_arima_state(prep, core)     an ARIMA's end state: the transformed series without its regression, differenced
#   risk_arima_paths(st, E)          the ARIMA recursion given the innovations E (h × n): the series' values
#
# The recursions are the runner's own, vectorized across a block's paths step by step and elementwise only (no matrix product: a
# BLAS kernel may round differently with the matrix's size, which would break the batch invariance). The self-test checks the
# GARCH's against rugarch::ugarchpath (1e-12) and the ARIMA's against forecast:::simulate.Arima(future = TRUE) (1e-10), given
# the same innovations.
#
# The risk variable H_k (per path and step k = 1…h) carries every measure; the engine computes them on H's scale:
#
#   GARCH                               H_k = C_k, the cumulative simple return: exp(r_1 + … + r_k) − 1 (log returns) or
#                                       Π(1 + r_j) − 1 (simple); the fan shows C_k, or the price P_T (1 + C_k) with prices
#   ARIMA, aggregate = last             H_k = y_{T+k}
#   ARIMA, aggregate = sum              H_k = y_{T+1} + … + y_{T+k}
#   ARIMA, aggregate = relative_change  H_k = y_{T+k} / y_T − 1 (y_T > 0); the fan shows y_{T+k} = y_T (1 + H_k)
#
# The fan's unit is an increasing linear map a + b·H of H (b > 0): quantiles, means and expected shortfalls map exactly, and the
# threshold, given in the fan's unit, is mapped back to H before the engine counts it. A position V (a relative change only) is
# an amount invested: its money is V·H, said by the page from `model|position` and the measures.
#
# Verdicts come only from tests with a p-value against alpha (the fitted model's checks, Kupiec's and Christoffersen's backtest
# of the 1-step VaR on the holdout); VaR, ES, the probabilities, the drawdown and the moments are measures. The explanation is
# the layout's (layouts/risk_simulation.py, common.READINGS): the runner writes no reading.

RISK_PATHS_FILE <- "/tmp/paths.parquet"

# The memory constants of each model's batch (ts_sim.R: growth over R0 = F + 8·(φ·B_s·h + ρ·m·P·h)), MEASURED in fresh processes
# with rugarch loaded (the spec's «As built / G3»): 50 runs, GARCH t on prices, GJR skewed t on returns, SARIMA with a log, parametric
# and bootstrapped innovations, 10, 100, 250 and 1,000 steps, 1 block to the row group's cap a batch, three batches, both passes.
# The engine's constants bound every one (the largest growth is 0.905 of the bound: SARIMA, 100 steps, 3 blocks), so they are kept;
# lot G9 raised the engine's F to 48 MB for its second pass (ts_sim.R), and these with it.
RISK_MEMORY <- list(
  garch = list(fixed_bytes = 48e6, block_factor = 31, batch_factor = 4),
  arima = list(fixed_bytes = 48e6, block_factor = 31, batch_factor = 4)
)

fit_risk_simulation <- function(task) {
  ts_params_within("risk_simulation", task$params)  # paths, steps, levels, threshold, position: bounded before anything is allocated
  o <- risk_params(task$params)
  # the fitted model's functions read the options from the task: the risk node's own, its defaults included
  for (k in c("horizon", "holdout", "var_level", "alpha", "seed", "mean")) task$params[[k]] <- o[[k]]
  fm <- if (o$model == "garch") risk_fit_garch(task, o) else risk_fit_arima(task, o)
  h <- o$horizon
  # the threshold, given in the fan's unit, on H's scale
  thr <- if (is.null(o$threshold)) NULL else (o$threshold - fm$fan$a) / fm$fan$b
  measures <- list(var_level = o$var_level, es_level = o$es_level, tail = o$tail, quantiles = o$quantiles, threshold = thr,
                   alpha = o$alpha, drawdown = fm$relative && isTRUE(o$drawdown), max_sample_paths = tab_limit(task, "max_sample_paths"))
  keep <- identical(o$output, "paths")
  args <- list(model = fm$sim, paths = o$paths, horizon = h, seed = o$seed, limits = task$limits, measures = measures,
               time = fm$time, file = RISK_PATHS_FILE, keep = keep, after_seconds = 0)
  for (k in names(task$.sim)) args[[k]] <- task$.sim[[k]]  # the self-test's hooks: the file, the baseline, the batch
  sim <- do.call(ts_sim_run, args)
  rows <- fm$rows
  risk_model_rows(rows, o, fm, sim)
  risk_rows(rows, o, fm, sim)
  risk_fan_rows(task, rows, o, fm, sim)
  if (!is.null(fm$backtest)) risk_backtest_rows(task, rows, fm$backtest)
  for (k in names(fm$counts)) {
    n <- fm$counts[[k]]
    if (n > 0) tab_note(rows, paste0("@", k, " n=", format(n, scientific = FALSE, trim = TRUE)))
  }
  r <- sim$run
  message(sprintf("risk simulation: %s paths × %d steps, %d batches of %s paths, %.0f MB on disk, R0 %.0f MB, peak %.0f MB (%s), %.2f s + %.2f s",
                  format(r$paths, scientific = FALSE), r$horizon, r$batches, format(r$batch_paths, scientific = FALSE),
                  r$paths_bytes / 1e6, r$baseline_bytes / 1e6, r$peak_bytes / 1e6, r$peak_source, r$seconds_simulation,
                  r$seconds_pass_2))
  output <- NULL
  if (keep) {
    if (isTRUE(task$write_output)) ts_sim_copy(sim$file, Sys.getenv("TABULARIA_OUTPUT"))
    unlink(sim$file)
  } else if (identical(o$output, "risk_summary")) {
    output <- risk_summary(o, fm, sim)
  }
  list(rows = rows, output = output, card = NULL, native = NULL)
}

risk_params <- function(params) {
  o <- tab_model_spec("risk_simulation")$defaults
  for (k in names(params)) if (!is.null(params[[k]])) o[[k]] <- params[[k]]
  o$paths <- as.numeric(o$paths)
  for (k in c("horizon", "holdout", "seed")) o[[k]] <- as.integer(o[[k]])
  for (k in c("alpha", "var_level", "es_level")) o[[k]] <- as.numeric(o[[k]])
  o$quantiles <- sort(unique(as.numeric(unlist(o$quantiles %||% list()))))
  o$threshold <- if (length(o$threshold)) as.numeric(o$threshold) else NULL
  o$position <- if (length(o$position)) as.numeric(o$position) else NULL
  o$drawdown <- isTRUE(o$drawdown)
  o$aggregate <- if (identical(o$model, "garch")) "relative_change" else o$aggregate %||% "last"
  o
}

# The quantile's name as the catalogue writes a number (catalog._number_label: the shortest repr, «0.01», «0.5»).
risk_label <- function(q) vapply(q, function(v) if (v == trunc(v) && abs(v) < 1e15) format(v, scientific = FALSE) else tab_number_label(v), "")

# ── GARCH ───────────────────────────────────────────────────────────────────────────────────────────────────────
risk_fit_garch <- function(task, o) {
  loadNamespace("rugarch")
  g <- garch_params(task$params)
  d <- garch_series(task, g)
  fit <- garch_fit(g, d)
  m <- garch_model(g, d, fit)
  checks <- garch_checks(task, g, d, m)
  rows <- tab_rows_start(task)
  garch_model_rows(rows, g, d, m, checks)
  garch_coef_rows(rows, g, m)
  for (ch in checks) ts_check_rows(rows, ch)
  for (ch in checks) if (!is.null(ch$p_adjusted)) tab_number(rows, "assumption", "p_adjusted", ch$p_adjusted, level = ch$id, level_2 = ch$level_2)
  st <- risk_garch_state(g, d, m)
  # rugarch lets a GJR's γ_i go below −α_i: a fall then lowers the variance, and a large one makes it negative, which no path can
  # continue from (the GARCH node reports such a fit; it cannot be simulated)
  low <- which(st$alpha + st$gamma < 0)
  if (identical(g$variance_model, "gjr") && length(low)) {
    i <- low[[1L]]
    tab_refuse("negative_variance", paste0("the fitted GJR-GARCH has α", i, " + γ", i, " = ", signif(st$alpha[[i]] + st$gamma[[i]], 3),
                                           ", below 0: a large fall would make the variance negative, so no path can be simulated; ",
                                           "choose GARCH or EGARCH"), list(sum = st$alpha[[i]] + st$gamma[[i]]))
  }
  prices <- identical(g$input_kind, "prices")
  last <- d$y[[d$n]]
  counts <- new.env(parent = emptyenv())
  counts$returns_below_minus_one <- 0
  simple <- identical(g$returns_type, "simple")
  innov <- o$innovations
  zt <- m$z[seq_len(d$train)]
  zt <- zt[is.finite(zt)]
  columns <- c("value", "cumulative", if (prices) "price", "sigma")
  draw <- function(n, h) {
    Z <- if (identical(innov, "bootstrap")) {
      matrix(zt[sample.int(length(zt), n * h, replace = TRUE)], nrow = h)
    } else if (identical(g$distribution, "norm")) {
      matrix(stats::rnorm(n * h), nrow = h)
    } else {
      matrix(rugarch::rdist(g$distribution, n * h, mu = 0, sigma = 1, skew = m$skew, shape = m$shape), nrow = h)
    }
    out <- risk_garch_paths(st, Z)
    rm(Z)
    R <- out$value
    C <- R
    if (simple) {
      counts$returns_below_minus_one <- counts$returns_below_minus_one + sum(R < -1)
      w <- rep(1, n)
      for (k in seq_len(h)) {
        w <- w * (1 + R[k, ])
        C[k, ] <- w - 1
      }
    } else {
      s <- numeric(n)
      for (k in seq_len(h)) {
        s <- s + R[k, ]
        C[k, ] <- expm1(s)
      }
    }
    res <- list(value = R, cumulative = C)
    if (prices) res$price <- last * (1 + C)
    res$sigma <- out$sigma
    res
  }
  h <- o$horizon
  future <- ts_time_after(d$times[[d$n]], seq_len(h), d$kind, g$frequency)
  if (d$kind == "datetime") attr(future, "tzone") <- attr(d$times, "tzone")
  x_last <- d$x0[[d$n]]
  hist <- NULL
  if (prices) {
    keep <- seq.int(max(1L, d$n - min(3L * h, tab_limit(task, "max_chart_points")) + 1L), d$n)
    hist <- list(x = d$x0[keep], y = d$y[keep])
  }
  # the backtest of the 1-step VaR on the holdout, on the node's tail (garch_model's is the lower tail's)
  bt <- NULL
  if (g$holdout > 0L) {
    held <- seq.int(d$train + 1L, d$n_ret)
    if (identical(o$tail, "upper")) {
      qu <- rugarch::qdist(g$distribution, o$var_level, mu = 0, sigma = 1, skew = m$skew, shape = m$shape)
      var <- m$mu[held] + m$sigma[held] * qu
      b <- ts_backtest(-d$r[held], -var, o$var_level, o$alpha)
    } else {
      var <- m$var[held]
      b <- m$backtest
    }
    bt <- list(bt = b, x = d$x0[held + d$lead], obs = d$r[held], var = var)
  }
  list(rows = rows, sim = c(list(columns = columns, draw = draw), RISK_MEMORY$garch),
       time = ts_time_array(future, d$type), x0 = ts_x0(future, d$kind), x_last = x_last, last_value = last,
       fan = if (prices) list(a = last, b = last) else list(a = 0, b = 1), h0 = 0, history = hist, relative = TRUE,
       variable = "cumulative_return", backtest = bt, counts = counts, garch = TRUE, prices = prices)
}

# The end state the recursions start from, on the series' units: the last returns (the AR terms), the last shocks ε = r − μ_t (the
# ARCH and MA terms), the last variances (log-variances for EGARCH), the last z = ε / σ (EGARCH), the coefficients by lag.
risk_garch_state <- function(g, d, m) {
  th <- function(nm) if (nm %in% names(m$theta)) m$theta[[nm]] else 0
  by_lag <- function(prefix, k) vapply(seq_len(k), function(i) th(paste0(prefix, i)), 0)
  eps <- d$r - m$mu
  last <- function(v, k) if (k > 0L) rev(utils::tail(v, k)) else numeric()  # lag 1 first
  egarch <- identical(g$variance_model, "egarch")
  list(vm = g$variance_model, q = g$q, p = g$p, ar = g$ar_order, ma = g$ma_order, mu = th("mu"),
       phi = by_lag("ar", g$ar_order), theta = by_lag("ma", g$ma_order), omega = th("omega"),
       alpha = by_lag("alpha", g$q), gamma = by_lag("gamma", g$q), beta = by_lag("beta", g$p), eabs = m$eabs,
       r = last(d$r, g$ar_order), eps = last(eps, max(g$q, g$ma_order)),
       v = last(if (egarch) log(m$sigma^2) else m$sigma^2, g$p), z = last(eps / m$sigma, g$q))
}

# The GARCH recursion over a block's paths, given their standardized innovations Z (h × n, column j the path j). At step k:
#   σ²_k = ω + Σ_i (α_i + γ_i 1(ε_{k−i} < 0)) ε²_{k−i} + Σ_j β_j σ²_{k−j}          (ARCH, GARCH: γ = 0; GJR)
#   ln σ²_k = ω + Σ_i (α_i z_{k−i} + γ_i (|z_{k−i}| − E|z|)) + Σ_j β_j ln σ²_{k−j}  (EGARCH, rugarch's form)
#   ε_k = σ_k z_k,  r_k = μ + Σ_i φ_i (r_{k−i} − μ) + Σ_j θ_j ε_{k−j} + ε_k
# the lags before the first step from the series' end (risk_garch_state); elementwise only.
risk_garch_paths <- function(st, Z) {
  h <- nrow(Z)
  n <- ncol(Z)
  egarch <- identical(st$vm, "egarch")
  R <- matrix(0, h, n)
  S <- matrix(0, h, n)
  lag_r <- as.list(st$r)
  lag_e <- as.list(st$eps)
  lag_v <- as.list(st$v)
  lag_z <- as.list(st$z)
  push <- function(l, x) if (length(l)) c(list(x), l[-length(l)]) else l
  for (k in seq_len(h)) {
    v <- st$omega
    for (i in seq_len(st$q)) {
      if (egarch) {
        zi <- lag_z[[i]]
        v <- v + st$alpha[[i]] * zi + st$gamma[[i]] * (abs(zi) - st$eabs)
      } else {
        e <- lag_e[[i]]
        v <- v + (st$alpha[[i]] + st$gamma[[i]] * (e < 0)) * (e * e)
      }
    }
    for (j in seq_len(st$p)) v <- v + st$beta[[j]] * lag_v[[j]]
    sig <- if (egarch) sqrt(exp(v)) else sqrt(v)
    if (length(sig) == 1L) sig <- rep(sig, n)
    z <- Z[k, ]
    e <- sig * z
    mean <- st$mu
    for (i in seq_len(st$ar)) mean <- mean + st$phi[[i]] * (lag_r[[i]] - st$mu)
    for (j in seq_len(st$ma)) mean <- mean + st$theta[[j]] * lag_e[[j]]
    r <- mean + e
    R[k, ] <- r
    S[k, ] <- sig
    lag_r <- push(lag_r, r)
    lag_e <- push(lag_e, e)
    lag_v <- push(lag_v, v)
    lag_z <- push(lag_z, z)
  }
  list(value = R, sigma = S)
}

# ── ARIMA ───────────────────────────────────────────────────────────────────────────────────────────────────────
risk_fit_arima <- function(task, o) {
  if (!is.null(o$position) && !identical(o$aggregate, "relative_change")) {
    tab_refuse("position_not_relative", paste0("a position is an amount invested: it needs a relative change (aggregate = relative_change); ",
                                               "with aggregate = ", o$aggregate, " the measures are in the units of «", o$value, "»: leave the position empty"),
               list(aggregate = o$aggregate))
  }
  prep <- arima_prepare(task)
  a <- prep$params
  search <- NULL
  constant <- FALSE
  if (tab_auto_orders(task)) {
    lambda <- arima_lambda(prep)
    y <- stats::ts(prep$y[prep$train], frequency = max(1L, a$s))
    search <- arima_auto_search(y, NULL, lambda, a)
    ar <- search$fit$arma
    orders <- lapply(list(p = ar[[1L]], d = ar[[6L]], q = ar[[2L]], P = ar[[3L]], D = ar[[7L]], Q = ar[[4L]]), as.integer)
    constant <- any(c("drift", "intercept") %in% names(search$fit$coef))
    prep$params$mean <- constant && (orders$d + orders$D) == 0L
    prep$params$drift <- constant && (orders$d + orders$D) == 1L
    a <- prep$params
  } else {
    orders <- list(p = a$p, d = a$d, q = a$q, P = a$P, D = a$D, Q = a$Q)
  }
  core <- arima_fit_core(prep, orders)
  fit <- core$fit
  model <- core$model
  rows <- tab_rows_start(task)
  risk_arima_rows(rows, a, prep, core, orders)
  if (!is.null(search)) arima_auto_rows(rows, a, search, orders, constant)
  # the coefficients, the checks: ARIMA's own
  est <- fit$coef
  se <- if (length(est)) sqrt(pmax(diag(fit$var.coef), 0)) else numeric()
  se[!is.finite(se)] <- NA_real_
  crit <- stats::qnorm(1 - a$alpha / 2)
  labels <- arima_coef_labels(model, names(est))
  for (i in seq_along(est)) {
    tab_number(rows, "coef", "estimate", est[[i]], level = labels[[i]])
    tab_number(rows, "coef", "std_error", se[[i]], level = labels[[i]])
    tab_number(rows, "coef", "statistic", est[[i]] / se[[i]], level = labels[[i]])
    tab_p(rows, "coef", 2 * stats::pnorm(-abs(est[[i]] / se[[i]])), level = labels[[i]])
    tab_number(rows, "coef", "ci_low", est[[i]] - crit * se[[i]], level = labels[[i]])
    tab_number(rows, "coef", "ci_high", est[[i]] + crit * se[[i]], level = labels[[i]])
  }
  arima_checks(rows, model, orders, a, arima_differenced(model, prep$y[prep$train]), as.numeric(stats::residuals(fit)),
               arima_roots(fit$coef, orders), fit)
  notes <- core$notes
  if (prep$gaps > 0L) notes <- c(notes, arima_note(if (prep$gaps == 1L) "gap" else "gaps", n = prep$gaps))
  if (model$drift_dropped) notes <- c(notes, arima_note("drift_dropped"))
  if (!identical(as.integer(fit$code %||% 0L), 0L)) notes <- c(notes, arima_note("not_converged", code = fit$code))
  for (sentence in notes) tab_note(rows, sentence)

  # the model applied with its coefficients fixed to the whole span (through the holdout): the state the paths start from
  refit <- arima_call(model, prep, prep$span, fixed = model$coef)
  refit$sigma2 <- model$sigma2
  st <- risk_arima_state(prep, model, refit)
  y_last <- prep$y[[prep$last]]
  agg <- o$aggregate
  if (agg == "relative_change" && !(y_last > 0)) {
    tab_refuse("zero_last", paste0("the last value of «", o$value, "» is ", format(y_last, scientific = FALSE, trim = TRUE),
                                   ": a relative change needs a positive last value; choose aggregate = last or sum"),
               list(column = o$value, value = y_last))
  }
  counts <- new.env(parent = emptyenv())
  counts$box_cox_domain <- 0
  innov <- o$innovations
  sd <- sqrt(model$sigma2)
  res <- as.numeric(stats::residuals(fit))
  res <- res[is.finite(res)]
  draw <- function(n, h) {
    E <- if (identical(innov, "bootstrap")) matrix(res[sample.int(length(res), n * h, replace = TRUE)], nrow = h) else
      matrix(stats::rnorm(n * h, 0, sd), nrow = h)
    Y <- risk_arima_paths(st, E, counts)
    rm(E)
    H <- Y
    if (agg == "sum") {
      s <- numeric(n)
      for (k in seq_len(h)) {
        s <- s + Y[k, ]
        H[k, ] <- s
      }
    } else if (agg == "relative_change") {
      H <- Y / y_last - 1
    }
    list(value = Y, cumulative = H)
  }
  h <- o$horizon
  future <- prep$future
  hist <- NULL
  if (agg != "sum") {
    span <- prep$span[!is.na(prep$y[prep$span])]
    keep <- utils::tail(span, min(3L * h, tab_limit(task, "max_chart_points")))
    hist <- list(x = prep$x0[keep], y = prep$y[keep])
  }
  bt <- if (prep$holdout > 0L) risk_arima_backtest(o, prep, model, refit) else NULL
  list(rows = rows, sim = c(list(columns = c("value", "cumulative"), draw = draw), RISK_MEMORY$arima),
       time = prep$time_array(future), x0 = prep$x0[future], x_last = prep$x0[[prep$last]], last_value = y_last,
       fan = if (agg == "relative_change") list(a = y_last, b = y_last) else list(a = 0, b = 1),
       h0 = if (agg == "last") y_last else 0, history = hist, relative = agg == "relative_change",
       variable = c(last = "value", sum = "running_total", relative_change = "relative_change")[[agg]], backtest = bt,
       counts = counts, garch = FALSE, prices = FALSE)
}

# The rows of the ARIMA fitted, as the ARIMA node writes its model section (the fit's measures, the orders, the roots).
risk_arima_rows <- function(rows, a, prep, core, orders) {
  fit <- core$fit
  model <- core$model
  tab_text(rows, "model", "formula", arima_formula(a, model, core$name))
  tab_text(rows, "model", "software", tab_software(ARIMA_PACKAGES))
  tab_text(rows, "model", "model_name", core$name)
  tab_text(rows, "model", "frequency", a$frequency)
  tab_text(rows, "model", "transform", a$transform)
  tab_text(rows, "model", "time_kind", prep$kind)
  acc <- forecast::accuracy(fit)
  tab_number(rows, "model", "n", core$n)
  tab_number(rows, "model", "n_missing", prep$n_missing)
  for (k in c("p", "d", "q", "P", "D", "Q")) tab_number(rows, "model", k, orders[[k]])
  tab_number(rows, "model", "s", a$s)
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
  if (a$transform == "box_cox") tab_number(rows, "model", "box_cox_lambda", core$lambda)
  tab_number(rows, "model", "horizon", length(prep$future))
  roots <- arima_roots(fit$coef, orders)
  for (i in seq_along(roots$ar)) tab_number(rows, "model", paste0("ar_root_modulus_", i), roots$ar[[i]])
  for (i in seq_along(roots$ma)) tab_number(rows, "model", paste0("ma_root_modulus_", i), roots$ma[[i]])
  invisible(rows)
}

# What forecast:::simulate.Arima(future = TRUE) starts from, written out: u = x − (intercept + drift·t) on the span, x the series
# transformed (BoxCox); its differences v = (1 − B)^d (1 − B^s)^D u; the fit's residuals (the MA terms); φ*(B) = φ(B)Φ(B^s) and
# θ*(B) = θ(B)Θ(B^s) expanded (refit$model$phi, $theta); δ(B) = 1 − (1 − B)^d (1 − B^s)^D, whose coefficients put the differences
# back; the regression's value at each future time. Lags are kept lag 1 first.
risk_arima_state <- function(prep, model, refit) {
  span <- prep$span
  x <- prep$y[span]
  if (!is.null(model$lambda)) x <- as.numeric(forecast::BoxCox(x, model$lambda))
  co <- model$coef
  reg <- function(i) {
    out <- rep(if (isTRUE(model$mean) && "intercept" %in% names(co)) co[["intercept"]] else 0, length(i))
    if (isTRUE(model$drift) && "xdrift" %in% names(co)) out <- out + co[["xdrift"]] * as.numeric(prep$drift_index[i])
    out
  }
  u <- x - reg(span)
  d <- model$orders$d
  D <- model$orders$D
  s <- max(1L, model$s)
  v <- u
  if (d > 0L) v <- diff(v, lag = 1L, differences = d)
  if (D > 0L) v <- diff(v, lag = s, differences = D)
  phi <- as.numeric(refit$model$phi)
  theta <- as.numeric(refit$model$theta)
  # δ: (1 − B)^d (1 − B^s)^D = 1 − Σ δ_j B^j
  poly <- 1
  for (i in seq_len(d)) poly <- c(poly, 0) - c(0, poly)
  for (i in seq_len(D)) poly <- c(poly, rep(0, s)) - c(rep(0, s), poly)
  delta <- -poly[-1L]
  need <- c(v = length(phi), u = length(delta))
  last <- function(z, k) if (k > 0L) rev(utils::tail(z, k)) else numeric()
  if (anyNA(last(v, need[["v"]])) || anyNA(last(u, need[["u"]]))) {
    tab_refuse("last_missing", paste0("the last values of «", prep$params$value, "» are missing: the simulation starts from them; ",
                                      "fill them in the flow, or filter the series to end at an observed value"),
               list(column = prep$params$value))
  }
  e <- as.numeric(stats::residuals(refit))
  e[!is.finite(e)] <- 0  # a period without a value has no shock: its expectation
  list(phi = phi, theta = theta, delta = delta, v = last(v, length(phi)), u = last(u, length(delta)),
       e = last(e, length(theta)), reg = reg(prep$future), lambda = model$lambda)
}

# The ARIMA recursion over a block's paths, given their innovations E (h × n): w_k = e_k + Σ θ*_j e_{k−j} (the MA), v_k = w_k + Σ φ*_j
# v_{k−j} (the AR, on the differences), u_k = v_k + Σ δ_j u_{k−j} (the differences undone), y_k = InvBoxCox(u_k + regression_k).
# A Box–Cox value outside the transform's domain (λ·x + 1 < 0) is counted in `counts$box_cox_domain`, as forecast's InvBoxCox
# gives it (not dropped).
risk_arima_paths <- function(st, E, counts = NULL) {
  h <- nrow(E)
  n <- ncol(E)
  Y <- matrix(0, h, n)
  lag_e <- as.list(st$e)
  lag_v <- as.list(st$v)
  lag_u <- as.list(st$u)
  push <- function(l, x) if (length(l)) c(list(x), l[-length(l)]) else l
  lambda <- st$lambda
  for (k in seq_len(h)) {
    e <- E[k, ]
    w <- e
    for (j in seq_along(st$theta)) if (st$theta[[j]] != 0) w <- w + st$theta[[j]] * lag_e[[j]]
    v <- w
    for (j in seq_along(st$phi)) if (abs(st$phi[[j]]) > .Machine$double.eps) v <- v + st$phi[[j]] * lag_v[[j]]
    u <- v
    for (j in seq_along(st$delta)) if (st$delta[[j]] != 0) u <- u + st$delta[[j]] * lag_u[[j]]
    x <- u + st$reg[[k]]
    if (!is.null(lambda)) {
      if (lambda != 0 && !is.null(counts)) counts$box_cox_domain <- counts$box_cox_domain + sum(lambda * x + 1 < 0)
      x <- forecast::InvBoxCox(x, lambda)
    }
    Y[k, ] <- x
    lag_e <- push(lag_e, e)
    lag_v <- push(lag_v, v)
    lag_u <- push(lag_u, u)
  }
  Y
}

# The 1-step VaR of the held-out days: the model with its coefficients fixed, run through the span (refit); on the transformed
# scale the 1-step forecast is x_t − e_t (e_t the innovation) and the VaR that forecast plus σ̂ times the normal quantile (the
# innovation's variance at its steady state σ̂²: the holdout follows the training part), brought back by the inverse transform
# (increasing: quantiles map). A day without a value is left out.
risk_arima_backtest <- function(o, prep, model, refit) {
  test <- prep$test
  y <- prep$y[test]
  x <- if (is.null(model$lambda)) y else as.numeric(forecast::BoxCox(y, model$lambda))
  e <- as.numeric(stats::residuals(refit))[match(test, prep$span)]
  q <- stats::qnorm(if (identical(o$tail, "upper")) o$var_level else 1 - o$var_level)
  vx <- x - e + sqrt(model$sigma2) * q
  var <- if (is.null(model$lambda)) vx else forecast::InvBoxCox(vx, model$lambda)
  ok <- is.finite(y) & is.finite(var)
  b <- if (identical(o$tail, "upper")) ts_backtest(-y[ok], -var[ok], o$var_level, o$alpha) else ts_backtest(y[ok], var[ok], o$var_level, o$alpha)
  list(bt = b, x = prep$x0[test][ok], obs = y[ok], var = var[ok])
}

# ── the rows ────────────────────────────────────────────────────────────────────────────────────────────────────
risk_model_rows <- function(rows, o, fm, sim) {
  r <- sim$run
  tab_text(rows, "model", "risk_variable", fm$variable)
  tab_text(rows, "model", "tail", o$tail)
  tab_text(rows, "model", "innovations", o$innovations)
  tab_text(rows, "model", "aggregate", o$aggregate)
  num <- function(stat, v) if (length(v) && !is.na(v)) tab_number(rows, "model", stat, v)
  num("paths", r$paths)
  # the fitted model's rows hold the horizon (and a GARCH's the var_level)
  num("seed", r$seed)
  num("seed_block", r$seed_block)
  num("batches", r$batches)
  num("batch_paths", min(r$batch_paths, r$paths))
  num("bins", r$bins)
  if (!fm$garch) num("var_level", o$var_level)
  num("es_level", o$es_level)
  num("threshold", o$threshold)
  num("position", o$position)
  num("last_value", fm$last_value)
  num("simulated_values", r$simulated_values)
  num("paths_bytes", r$paths_bytes)
  num("seconds_simulation", r$seconds_simulation)
  num("seconds_pass_2", r$seconds_pass_2)
  invisible(rows)
}

# The measures at the horizon (`risk`, no level_2), on H's scale.
risk_rows <- function(rows, o, fm, sim) {
  s <- sim$steps
  h <- nrow(s)
  N <- o$paths
  put <- function(level, stat, v) if (length(v) && !is.na(v)) tab_number(rows, "risk", stat, v, level = level)
  put("var", "value", s$var[[h]])
  put("var", "low", s$var_low[[h]])
  put("var", "high", s$var_high[[h]])
  put("es", "value", s$es[[h]])
  put("es", "low", s$es_low[[h]])
  put("es", "high", s$es_high[[h]])
  put("es", "se", s$es_se[[h]])
  put("es", "count", round(N * round(1 - o$es_level, 12), 6))
  if (!is.null(o$position)) {
    # the money of a relative change: position × the value (an ARIMA of another risk variable takes no position)
    put("var", "money", o$position * s$var[[h]])
    put("es", "money", o$position * s$es[[h]])
  }
  if (!is.null(o$threshold)) {
    for (what in c("beyond", "crossed")) {
      lv <- paste0("prob_", what)
      put(lv, "value", s[[lv]][[h]])
      put(lv, "low", s[[paste0(lv, "_low")]][[h]])
      put(lv, "high", s[[paste0(lv, "_high")]][[h]])
      put(lv, "count", s[[paste0("count_", what)]][[h]])
    }
  }
  dd <- sim$drawdown
  if (!is.null(dd)) {
    put("drawdown", "value", dd$var)
    put("drawdown", "low", dd$var_low)
    put("drawdown", "high", dd$var_high)
    put("drawdown", "median", dd$median)
    put("drawdown", "mean", dd$mean)
    put("drawdown", "sd", dd$sd)
  }
  put("moments", "mean", s$mean[[h]])
  put("moments", "sd", s$sd[[h]])
  put("moments", "skewness", s$skewness[[h]])
  put("moments", "kurtosis", s$kurtosis[[h]])
  invisible(rows)
}

# The fan in its unit (a + b·H), the history on the same scale, the sample paths, the bands' declared error; the measures per
# step (`term`, on H's scale); the histograms of H_h and of the drawdown (`hist`, on their own scales).
risk_fan_rows <- function(task, rows, o, fm, sim) {
  a <- fm$fan$a
  b <- fm$fan$b
  x <- fm$x0
  # the fan starts at the last observation: H_0 (0, or y_T for the value itself) in the fan's unit
  x_all <- c(fm$x_last, x)
  start <- a + b * fm$h0
  qs <- o$quantiles
  for (i in seq_along(qs)) {
    tab_points(rows, "curve", "fan", "y", x = x_all, y = c(start, a + b * sim$quantiles[, i]), level_2 = paste0("quantile_", risk_label(qs[[i]])))
  }
  if (!is.null(fm$history)) tab_points(rows, "curve", "fan", "y", x = fm$history$x, y = fm$history$y, level_2 = "history")
  H <- sim$sample$cumulative
  for (j in seq_len(ncol(H))) tab_points(rows, "curve", "fan", "y", x = x_all, y = c(start, a + b * H[, j]), level_2 = paste0("path_", j))
  tab_points(rows, "curve", "fan", "band_error", x = x, y = b * sim$steps$band_error, level_2 = "band_error")
  s <- sim$steps
  # `es_quantile`: the VaR at the ES's level, the quantile the ES averages beyond (lot G9)
  for (k in c("var", "var_low", "var_high", "es", "es_low", "es_high", "es_quantile")) tab_points(rows, "curve", "term", "y", x = x, y = s[[k]], level_2 = k)
  if (!is.null(o$threshold)) for (k in c("prob_crossed", "prob_beyond")) tab_points(rows, "curve", "term", "y", x = x, y = s[[k]], level_2 = k)
  for (k in c("horizon", "drawdown")) {
    bars <- sim$hist[[k]]
    if (!is.null(bars) && nrow(bars)) tab_points(rows, "hist", k, "count", x = bars$low, y = bars$count, x1 = bars$high)
  }
  invisible(rows)
}

# The backtest's tests and counts, and its chart: the held-out observation, its 1-step VaR, the observation on the days it broke
# the VaR (thinned by envelope, every break kept).
risk_backtest_rows <- function(task, rows, b) {
  ts_backtest_rows(rows, b$bt)
  most <- tab_limit(task, "max_chart_points")
  hit <- which(b$bt$hit == 1L)
  keep <- sort(unique(c(ts_envelope(b$obs, most), hit)))
  keep <- keep[seq_len(min(length(keep), most))]
  tab_points(rows, "curve", "backtest", "y", x = b$x[keep], y = b$obs[keep], level_2 = "return")
  tab_points(rows, "curve", "backtest", "y", x = b$x[keep], y = b$var[keep], level_2 = "var")
  if (length(hit)) tab_points(rows, "curve", "backtest", "y", x = b$x[hit], y = b$obs[hit], level_2 = "violation")
  invisible(rows)
}

# ── the output ──────────────────────────────────────────────────────────────────────────────────────────────────
# `risk_summary`: one row per step of the risk variable's distribution, on H's scale; with prices the same quantiles and the mean
# on the price (P_T (1 + ·)); with a threshold its two probabilities; the time in the input field's own type.
risk_summary <- function(o, fm, sim) {
  s <- sim$steps
  h <- nrow(s)
  cols <- list(step = seq_len(h), time = fm$time, mean = s$mean, sd = s$sd)
  labels <- risk_label(o$quantiles)
  for (i in seq_along(labels)) cols[[paste0("quantile_", labels[[i]])]] <- sim$quantiles[, i]
  if (fm$prices) {
    for (i in seq_along(labels)) cols[[paste0("price_quantile_", labels[[i]])]] <- fm$last_value * (1 + sim$quantiles[, i])
    cols$price_mean <- fm$last_value * (1 + s$mean)
  }
  for (k in c("var", "var_low", "var_high", "es", "es_low", "es_high")) cols[[k]] <- s[[k]]
  if (!is.null(o$threshold)) {
    cols$prob_beyond <- s$prob_beyond
    cols$prob_crossed <- s$prob_crossed
  }
  cols$band_error <- s$band_error
  for (k in names(cols)) if (is.numeric(cols[[k]]) && k != "step") cols[[k]][!is.finite(cols[[k]])] <- NA_real_
  tbl <- do.call(arrow::arrow_table, stats::setNames(unname(cols), paste0("c", seq_along(cols))))
  names(tbl) <- names(cols)
  tbl
}
