# The checks of a decomposition (docs/specs/ts-decomposition.md, «The seasonality test», «The checks of the
# remainder»; lot D1). A verdict comes only from a test's p-value against the node's alpha; the strengths and the
# largest autocorrelation are measures, written with a value and no verdict. Nothing here is random or simulated.
#
#   ts_lb_lag(lb_lag, n, periods, most)   Ljung–Box's lag: the given one, or `auto` = min(2·p_max, ⌊n/5⌋), min(10, ⌊n/5⌋)
#   ts_acf(r, lag_max)                    the remainder's autocorrelations at lags 1…lag_max (stats::acf)
#   ts_ljung_box(r, h, alpha, acf)        Q = n(n + 2) Σ ρ̂_k² / (n − k), χ² with h df (no fitted coefficient)
#   ts_largest_acf(acf, h)                the largest |ρ̂_k| for k ≤ h and its lag (a measure)
#   ts_adf(r, alpha)                      ADF with a constant, ⌊(n − 1)^⅓⌋ lags, MacKinnon's p (urca::punitroot)
#   ts_kpss(r, alpha)                     KPSS around a constant (tseries), «not computed» at the table's ends
#   ts_ma(y, p)                           the centred moving average of order p (stats::decompose's trend)
#   ts_seasonality(y, p, test, alpha, type)   Friedman or Kruskal–Wallis on the series detrended by ts_ma
#   ts_strength(component, remainder)     F = max(0, 1 − Var(R) / Var(C + R)) (a measure)
#   ts_measure(id, value, …)              a measure's check
#   ts_check_rows(rows, check)            a check's rows in the section `assumption`
#
# A check is a list: id, level_2 (the period, or NULL), kind (test, measure), h0, verdict (ok, failed,
# not_computed), statistic, df, p_value, advice, advice_id, value, count, lag; ts_kpss adds p_side (inside, below:
# p ≤ 0.01, above: p ≥ 0.10) for the reading. A remainder has empty values only at its ends (the classical method's
# moving average): they are left out; an empty value inside it is a runner's bug.

TS_UNIT_ROOT_MIN <- 12L  # values ADF and KPSS need, ARIMA's bound

# KPSS's table for a level (Kwiatkowski et al. 1992, table 1), as tseries 0.10-63 interpolates it (kpss.test:
# approx(table, tablep, STAT, rule = 2)); the self-test checks that tseries still reads it so.
TS_KPSS_LEVEL <- list(statistic = c(0.347, 0.463, 0.574, 0.739), p = c(0.10, 0.05, 0.025, 0.01))

# The remainder without the empty values at its ends.
.ts_values <- function(r) {
  r <- as.numeric(r)
  ok <- which(!is.na(r))
  if (!length(ok)) return(numeric())
  r <- r[seq.int(min(ok), max(ok))]
  if (anyNA(r)) stop("the remainder has an empty value inside it")
  r
}

.ts_not_computed <- function(id, h0, advice, advice_id, level_2 = NULL, ...) {
  c(list(id = id, level_2 = level_2, kind = "test", h0 = h0, verdict = "not_computed", advice = advice,
         advice_id = advice_id), list(...))
}

.ts_constant <- function(r) length(r) < 2L || !is.finite(stats::var(r)) || stats::var(r) == 0

# ── Ljung–Box ───────────────────────────────────────────────────────────────────
# `auto` is also kept within `most` (the catalogue's max_lb_lags, the lags a given lb_lag may have): hourly data with
# a yearly period would otherwise ask for 17,532 lags.
ts_lb_lag <- function(lb_lag, n, periods = integer(), most = 1000L) {
  periods <- as.integer(unlist(periods))
  if (identical(lb_lag %||% "auto", "auto")) {
    return(as.integer(min(most, if (length(periods)) min(2L * max(periods), n %/% 5L) else min(10L, n %/% 5L))))
  }
  as.integer(lb_lag)
}

ts_acf <- function(r, lag_max) {
  r <- .ts_values(r)
  lag_max <- min(as.integer(lag_max), length(r) - 1L)
  if (lag_max < 1L) return(numeric())
  as.numeric(stats::acf(r, lag.max = lag_max, plot = FALSE, demean = TRUE)$acf)[-1L]
}

# `acf`: ts_acf(r, ≥ h), when the chart's correlogram already computed it. The statistic is stats::Box.test's to the
# bit (the same sum in the same order); its p-value is pchisq's upper tail, which keeps the digits Box.test's
# 1 − pchisq loses below 1e-16.
ts_ljung_box <- function(r, h, alpha, acf = NULL) {
  r <- .ts_values(r)
  n <- length(r)
  h0 <- paste0("the remainder is not autocorrelated up to lag ", h)
  if (h < 1L || h >= n) {
    return(.ts_not_computed("ljung_box", h0, paste0("the remainder has ", n, " values: Ljung–Box at lag ", h, " needs more"),
                            "ljung_box_short", lag = h))
  }
  if (.ts_constant(r)) {
    return(.ts_not_computed("ljung_box", h0, "the remainder is constant: it has no autocorrelation to test", "remainder_constant",
                            lag = h))
  }
  if (is.null(acf) || length(acf) < h) acf <- ts_acf(r, h)
  obs <- acf[seq_len(h)]
  q <- n * (n + 2) * sum(1 / seq.int(n - 1, n - h) * obs^2)
  p <- stats::pchisq(q, h, lower.tail = FALSE)
  list(id = "ljung_box", kind = "test", h0 = h0, statistic = q, df = h, p_value = p, verdict = if (p < alpha) "failed" else "ok",
       advice = "structure left in the remainder: a season missing (another period), a wider trend window, or a model of the remainder",
       advice_id = "ljung_box_failed", lag = h)
}

ts_largest_acf <- function(acf, h) {
  h <- min(h, length(acf))
  if (h < 1L || anyNA(acf[seq_len(h)])) return(ts_measure("largest_acf", NA_real_))
  k <- which.max(abs(acf[seq_len(h)]))
  ts_measure("largest_acf", abs(acf[[k]]), lag = k)
}

# ── ADF ─────────────────────────────────────────────────────────────────────────
# Δr_t = α + γ r_{t−1} + Σ_{j=1}^{k} β_j Δr_{t−j} + ε_t, k = ⌊(n − 1)^⅓⌋ (tseries::adf.test's rule), τ = γ̂ / se(γ̂).
# τ is urca::ur.df(r, type = "drift", lags = k, selectlags = "Fixed")'s tau2 to the bit: the same design matrix, the
# same lm.fit and summary.lm's standard error, without ur.df's second regression (its φ₁ F test, which nothing
# reads) and its formula machinery — about a third of its time (spec, «As built / D1»). The p-value is MacKinnon's
# (1996) response surface for a constant, urca::punitroot(τ, N, "c"), N the regression's observations (n − k − 1),
# continuous: any alpha is answered.
ts_adf <- function(r, alpha) {
  r <- .ts_values(r)
  n <- length(r)
  h0 <- "the remainder has a unit root"
  if (n < TS_UNIT_ROOT_MIN) {
    return(.ts_not_computed("adf", h0, paste0("the remainder has ", n, " values: ADF needs at least ", TS_UNIT_ROOT_MIN), "adf_short"))
  }
  if (.ts_constant(r)) return(.ts_not_computed("adf", h0, "the remainder is constant: it has no unit root to test", "remainder_constant"))
  k <- as.integer(floor((n - 1)^(1 / 3)))
  fit <- ts_adf_tau(r, k)
  if (!is.finite(fit$tau)) return(.ts_not_computed("adf", h0, "the ADF regression is singular: the remainder is too regular", "adf_singular"))
  p <- urca::punitroot(fit$tau, N = fit$nobs, trend = "c", statistic = "t")
  list(id = "adf", kind = "test", h0 = h0, statistic = fit$tau, p_value = p, verdict = if (p < alpha) "ok" else "failed",
       advice = "ADF keeps a unit root in the remainder: slow movement is left in it; a narrower trend window",
       advice_id = "adf_failed", lag = k, count = fit$nobs)
}

# τ of the ADF regression with k lagged differences, as urca::ur.df(type = "drift") computes it.
ts_adf_tau <- function(r, k) {
  z <- diff(r)
  n <- length(z)
  lags <- k + 1L
  x <- stats::embed(z, lags)
  X <- cbind("(Intercept)" = 1, z.lag.1 = r[lags:n], x[, -1L, drop = FALSE])
  fit <- stats::lm.fit(X, x[, 1L])
  p <- fit$rank
  if (p < ncol(X) || fit$qr$pivot[[2L]] != 2L) return(list(tau = NA_real_, nobs = nrow(X)))
  p1 <- seq_len(p)
  resvar <- sum(fit$residuals^2) / (nrow(X) - p)
  se <- sqrt(diag(chol2inv(fit$qr$qr[p1, p1, drop = FALSE])) * resvar)
  list(tau = unname(fit$coefficients[[2L]] / se[[2L]]), nobs = nrow(X))
}

# ── KPSS ────────────────────────────────────────────────────────────────────────
# η = Σ S_t² / (n² σ̂²), S the partial sums of r − r̄, σ̂² Newey–West with Bartlett weights and
# l = ⌊4 (n/100)^¼⌋ lags (tseries::kpss.test(null = "Level", lshort = TRUE), its rule in 0.10-63). Its p-value is
# interpolated in the table and clipped to [0.01, 0.10]: inside the table the verdict is p against alpha; beyond it the
# table says only p ≤ 0.01 (η above 0.739) or p ≥ 0.10 (η below 0.347), which answers an alpha on the right side of
# the end (p ≤ 0.01 rejects at any alpha ≥ 0.01, p ≥ 0.10 keeps at any alpha ≤ 0.10) and none on the other: then
# «not computed», with η shown.
ts_kpss <- function(r, alpha) {
  r <- .ts_values(r)
  n <- length(r)
  h0 <- "the remainder is stationary around a constant"
  if (n < TS_UNIT_ROOT_MIN) {
    return(.ts_not_computed("kpss", h0, paste0("the remainder has ", n, " values: KPSS needs at least ", TS_UNIT_ROOT_MIN), "kpss_short"))
  }
  if (.ts_constant(r)) return(.ts_not_computed("kpss", h0, "the remainder is constant: KPSS has no variance to compare", "remainder_constant"))
  k <- suppressWarnings(tseries::kpss.test(r, null = "Level", lshort = TRUE))
  eta <- unname(k$statistic)
  p <- k$p.value
  l <- unname(k$parameter)
  table <- TS_KPSS_LEVEL
  side <- if (eta > max(table$statistic)) "below" else if (eta < min(table$statistic)) "above" else "inside"
  verdict <- if (side == "inside") {
    if (p < alpha) "failed" else "ok"
  } else if (side == "below") {
    if (alpha >= min(table$p)) "failed" else "not_computed"
  } else {
    if (alpha <= max(table$p)) "ok" else "not_computed"
  }
  if (verdict == "not_computed") {
    bound <- if (side == "below") "p ≤ 0.01" else "p ≥ 0.10"
    return(.ts_not_computed("kpss", h0, paste0("the KPSS table gives ", bound, "; it cannot say whether p < ", .ts_alpha_label(alpha)),
                            "kpss_table_end", statistic = eta, lag = l, p_side = side))
  }
  list(id = "kpss", kind = "test", h0 = h0, statistic = eta, p_value = p, verdict = verdict,
       advice = "KPSS rejects a stationary remainder: a level that moves is left in it; a narrower trend window",
       advice_id = "kpss_failed", lag = l, p_side = side)
}

# alpha as a person writes it: 0.001, 0.2.
.ts_alpha_label <- function(v) format(v, scientific = FALSE, drop0trailing = TRUE, trim = TRUE)

# ── the seasonality test ────────────────────────────────────────────────────────
# The centred moving average of order p, stats::decompose's trend to the bit: for odd p the mean of p values, for
# even p the 2×p average (weights 1/2p, 1/p…, 1/2p on p + 1 values); empty on the first and last ⌊p/2⌋ instants.
ts_ma <- function(y, p) {
  w <- if (p %% 2L) rep_len(1, p) / p else c(0.5, rep_len(1, p - 1L), 0.5) / p
  as.numeric(stats::filter(as.numeric(y), w, method = "convolution", sides = 2L))
}

# Does y repeat a pattern of period p? y detrended by ts_ma (d = y − MA, or y / MA for a multiplicative
# decomposition), then:
#   friedman        blocks of p consecutive detrended values from the first one (b = ⌊values / p⌋ of them, every
#                   position of the cycle once in each), ranks within a block, Q corrected for ties, χ² with p − 1 df
#                   (stats::friedman.test on the b × p matrix);
#   kruskal_wallis  every detrended value grouped by its position, H corrected for ties, χ² with p − 1 df
#                   (stats::kruskal.test).
# Both need two complete blocks. H0: no seasonal pattern of period p; p < alpha: the pattern is supported (ok).
# `count`: the complete blocks (cycles). test = none: NULL, no check.
ts_seasonality <- function(y, p, test, alpha, type = "additive") {
  if (identical(test, "none")) return(NULL)
  p <- as.integer(p)
  level_2 <- as.character(p)
  h0 <- paste0("the series has no seasonal pattern of period ", p)
  y <- as.numeric(y)
  ma <- ts_ma(y, p)
  d <- if (identical(type, "multiplicative")) y / ma else y - ma
  valid <- which(!is.na(d))
  b <- length(valid) %/% p
  if (b < 2L) {
    return(.ts_not_computed("seasonality", h0, paste0("fewer than two complete cycles of period ", p, " after the moving average: ",
                                                      "a longer series"), "seasonality_few_cycles", level_2 = level_2, count = b))
  }
  start <- valid[[1L]]
  if (identical(test, "kruskal_wallis")) {
    x <- d[valid]
    g <- (valid - 1L) %% p + 1L
    out <- if (.ts_constant(x)) NULL else stats::kruskal.test(x, g)
  } else {
    m <- matrix(d[seq.int(start, length.out = b * p)], nrow = b, byrow = TRUE)
    out <- if (.ts_constant(as.numeric(m))) NULL else ts_friedman(m)
  }
  if (is.null(out) || !is.finite(out$statistic)) {
    return(.ts_not_computed("seasonality", h0, "the detrended series is constant: there is nothing to rank", "seasonality_constant",
                            level_2 = level_2, count = b))
  }
  pv <- unname(out$p.value)
  list(id = "seasonality", level_2 = level_2, kind = "test", h0 = h0, statistic = unname(out$statistic), df = unname(out$parameter),
       p_value = pv, verdict = if (pv < alpha) "ok" else "failed",
       advice = paste0("the seasonal component of period ", p, " may be noise: read its strength and its chart; try without this period"),
       advice_id = "seasonality_failed", count = b)
}

# Friedman's test on a b × p matrix (blocks in rows), stats::friedman.test's statistic and p-value computed with
# the ranks of every block at once: friedman.test ranks block by block in R (apply and tapply over b blocks), which
# at 28,000 weekly blocks takes seconds. Q = 12 Σ_j (R_j − b(p + 1)/2)² / (b p (p + 1) − Σ (t³ − t) / (p − 1)), t the
# sizes of the ties within each block; the self-test compares it with friedman.test.
ts_friedman <- function(m) {
  b <- nrow(m)
  k <- ncol(m)
  block <- rep(seq_len(b), times = k)
  v <- as.numeric(m)
  o <- order(block, v, method = "radix")
  bo <- block[o]
  vo <- v[o]
  # ties: runs of equal values within a block; each gets the mean of the ranks it spans
  run <- cumsum(c(TRUE, bo[-1L] != bo[-length(bo)] | vo[-1L] != vo[-length(vo)]))
  within <- seq_along(o) - match(bo, bo) + 1L  # 1…k inside each block
  size <- tabulate(run)
  first <- match(seq_len(max(run)), run)
  mean_rank <- within[first] + (size - 1) / 2
  r <- numeric(length(v))
  r[o] <- mean_rank[run]
  R <- colSums(matrix(r, nrow = b))
  ties <- sum(size^3 - size)
  q <- 12 * sum((R - b * (k + 1) / 2)^2) / (b * k * (k + 1) - ties / (k - 1))
  list(statistic = q, parameter = k - 1L, p.value = stats::pchisq(q, k - 1L, lower.tail = FALSE))
}

# ── strengths and measures ──────────────────────────────────────────────────────
# Wang, Smith & Hyndman (2006): F = max(0, 1 − Var(R) / Var(C + R)) over the instants where both are given; NA (not
# computed) when Var(C + R) is 0. For a multiplicative decomposition the caller passes the logarithms.
ts_strength <- function(component, remainder) {
  ok <- !is.na(component) & !is.na(remainder)
  if (sum(ok) < 2L) return(NA_real_)
  total <- stats::var(component[ok] + remainder[ok])
  if (!is.finite(total) || total == 0) return(NA_real_)
  max(0, 1 - stats::var(remainder[ok]) / total)
}

ts_measure <- function(id, value, level_2 = NULL, count = NULL, lag = NULL) {
  list(id = id, level_2 = level_2, kind = "measure", value = value, count = count, lag = lag)
}

# ── the rows ────────────────────────────────────────────────────────────────────
# A check's rows (section assumption, level = its id, level_2 = the period or NULL): h0, statistic, df, p_value
# and its significance, verdict, advice and advice_id (when the verdict is not ok), value, count, lag. A measure has
# no h0, verdict or advice.
ts_check_rows <- function(rows, check) {
  if (is.null(check)) return(invisible(rows))
  id <- check$id
  l2 <- check$level_2
  num <- function(stat, v) if (!is.null(v)) tab_number(rows, "assumption", stat, v, level = id, level_2 = l2)
  if (!identical(check$kind, "measure")) tab_text(rows, "assumption", "h0", check$h0, level = id, level_2 = l2)
  num("statistic", check$statistic)
  num("df", check$df)
  if (!is.null(check$p_value)) tab_p(rows, "assumption", check$p_value, level = id, level_2 = l2)
  if (!identical(check$kind, "measure")) {
    tab_text(rows, "assumption", "verdict", check$verdict, level = id, level_2 = l2)
    if (!identical(check$verdict, "ok")) {
      tab_text(rows, "assumption", "advice", check$advice %||% NA_character_, level = id, level_2 = l2)
      tab_text(rows, "assumption", "advice_id", check$advice_id %||% NA_character_, level = id, level_2 = l2)
    }
  }
  num("value", check$value)
  num("count", check$count)
  num("lag", check$lag)
  invisible(rows)
}
