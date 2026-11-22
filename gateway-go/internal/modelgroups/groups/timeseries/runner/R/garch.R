# Volatility (GARCH) (docs/specs/garch-risk.md, «Part 1 — the node `garch`»; lot G1): fit_garch(task), predict_garch(task, card) (lot G6).
#
#   garch_params(params)          the node's options, the catalogue's defaults where a key is not given
#   garch_series(task, o)         the series (ts_series.R) and its returns, the training part, the scale s
#   garch_fit(o, d)               rugarch::ugarchfit on r / s: solnp, then nlminb, gosolnp twice (rugarch's `hybrid`, in its order)
#   garch_model(o, d, fit)        the parameters on the series' units, the filter through the holdout, the forecast, the measures
#   garch_checks(task, o, d, m)   the checks of z (weighted portmanteau on z and z², ARCH-LM, Nyblom, sign bias) and the facts
#
# The scale. The fit runs on y = r / s, s the training returns' standard deviation: the solver's tolerances are absolute, and
# daily returns in fractions put ω near 1e-6. Everything is brought back exactly: μ·s, ω·s² (ARCH, GARCH, GJR), ω + (1 − Σβ)·ln s²
# (EGARCH), the other parameters unchanged, their covariance by the same linear map J (J V Jᵀ), σ_t and μ_t times s, z_t unchanged,
# the log-likelihood ℓ(y) − n·ln s. Nothing here draws a random number but `gosolnp`'s restarts, from the node's seed; no
# simulation, no bootstrap. A verdict comes only from a test's p-value against alpha; persistence, half-life, the long-run σ, the
# news impact are measures. The explanation is the layout's (layouts/garch.py, common.READINGS): the runner writes no reading.

GARCH_PACKAGES <- c("rugarch", "Rsolnp", "FinTS")
GARCH_MIN_RETURNS <- 100L  # rugarch 1.5-6 warns below it («using less than 100 data points for estimation»)
GARCH_SOLVERS <- c("solnp", "nlminb", "gosolnp", "gosolnp")  # rugarch's `hybrid`, in its order (.hybridsolver)
GARCH_PERSISTENCE_BOUND <- 0.999  # rugarch's stationarity constraint on P (.garchconbounds): the solver's, not ours
GARCH_RUGARCH <- c(arch = "sGARCH", garch = "sGARCH", gjr = "gjrGARCH", egarch = "eGARCH")
GARCH_LAW <- c(norm = "normal", std = "t", sstd = "skewed t")
GARCH_ARCH_LAGS <- 12L     # the ARCH-LM's lags without a season (ARIMA's)
GARCH_QQ_POINTS <- 200L
GARCH_NEWS <- seq(-4, 4, by = 0.08)  # ε_{t−1} in units of the long-run σ: 101 points

fit_garch <- function(task) {
  loadNamespace("rugarch")
  o <- garch_params(task$params)
  d <- garch_series(task, o)
  fit <- garch_fit(o, d)
  m <- garch_model(o, d, fit)
  checks <- garch_checks(task, o, d, m)
  rows <- tab_rows_start(task)
  garch_model_rows(rows, o, d, m, checks)
  garch_coef_rows(rows, o, m)
  for (ch in checks) ts_check_rows(rows, ch)
  for (ch in checks) if (!is.null(ch$p_adjusted)) tab_number(rows, "assumption", "p_adjusted", ch$p_adjusted, level = ch$id, level_2 = ch$level_2)
  if (o$holdout > 0L) ts_backtest_rows(rows, m$backtest)
  garch_charts(task, rows, o, d, m, attr(checks, "acf"))
  output <- if (identical(o$output, "volatility")) garch_output(o, d, m) else NULL
  list(rows = rows, output = output, card = garch_card(task, o, d, m), native = garch_native(fit, d$scale))
}


garch_params <- function(params) {
  o <- tab_model_spec("garch")$defaults
  for (k in names(params)) if (!is.null(params[[k]])) o[[k]] <- params[[k]]
  for (k in c("arch_order", "garch_order", "ar_order", "ma_order", "horizon", "holdout")) o[[k]] <- as.integer(o[[k]])
  o$q <- o$arch_order
  o$p <- if (identical(o$variance_model, "arch")) 0L else o$garch_order
  o$mean <- isTRUE(o$mean)
  for (k in c("alpha", "var_level")) o[[k]] <- as.numeric(o[[k]])
  o
}

# What the options make of the node, as catalog.garch_name says it: «AR(1)-GJR-GARCH(1,1), skewed t».
garch_name <- function(o) {
  core <- if (o$variance_model == "arch") sprintf("ARCH(%d)", o$q) else
    sprintf("%s(%d,%d)", c(garch = "GARCH", gjr = "GJR-GARCH", egarch = "EGARCH")[[o$variance_model]], o$p, o$q)
  m <- o$ar_order
  n <- o$ma_order
  mean <- if (!m && !n) "" else if (!n) sprintf("AR(%d)-", m) else if (!m) sprintf("MA(%d)-", n) else sprintf("ARMA(%d,%d)-", m, n)
  paste0(mean, core, ", ", GARCH_LAW[[o$distribution]])
}

# ── the series and its returns ──────────────────────────────────────────────────────────────────────────────────
# ts_series (ARIMA's rules: one row per period, the grid, the input's time type kept); a period without a value inside the span is
# refused with this node's sentence (a missing return is not interpolated). With prices the first row has no return: `lead` = 1.
garch_series <- function(task, o) {
  what <- if (o$input_kind == "prices") "price" else "return"
  s <- tryCatch(ts_series(task, o$time, o$value, o$frequency), tabularia_refusal = function(e) {
    if (!identical(e$code, "gaps")) stop(e)
    tab_refuse("gaps", paste0("the series has ", e$params$n, " ", if (e$params$n == 1) "period" else "periods", " without a ", what,
                              " inside its span (the first: ", e$params$time, "): filter out the rows without a ", what,
                              ", or fill them in the flow; a missing return is not interpolated"), e$params)
  })
  y <- s$y
  if (o$input_kind == "prices") {
    if (any(y <= 0)) {
      tab_refuse("not_positive", paste0("the column «", o$value, "» holds 0 or a negative price: returns need positive prices; ",
                                        "choose input_kind = returns if it holds returns"), list(column = o$value))
    }
    r <- if (identical(o$returns_type, "simple")) y[-1L] / y[-length(y)] - 1 else diff(log(y))
  } else {
    r <- y
  }
  lead <- length(y) - length(r)
  n <- length(r)
  k <- o$holdout
  train <- n - k
  if (train < GARCH_MIN_RETURNS) {
    tab_refuse("too_short", paste0("the series has ", n, " returns", if (k) paste0(", ", train, " after the holdout of ", k) else "",
                                   ": a GARCH needs at least ", GARCH_MIN_RETURNS, " to fit; a longer series",
                                   if (k) " or a shorter holdout" else ""), list(n = n, holdout = k))
  }
  if (k > 0L && train < k) {
    tab_refuse("too_short", paste0("the series has ", train, " returns after the holdout of ", k, ": shorten the holdout"),
               list(n = n, holdout = k))
  }
  rt <- r[seq_len(train)]
  if (max(rt) == min(rt)) {
    tab_refuse("constant", paste0("the returns of «", o$value, "» are the same everywhere: there is no volatility to model"),
               list(column = o$value))
  }
  v <- stats::var(rt)
  if (!is.finite(v) || v == 0) {
    tab_refuse("scale", paste0("the returns of «", o$value, "» are too ", if (is.finite(v)) "small" else "large", " to compute ",
                               "with: their squares leave the range of a number; rescale the column first"), list(column = o$value))
  }
  c(s, list(r = r, lead = lead, n_ret = n, train = train, scale = sqrt(v)))
}

# ── the fit ─────────────────────────────────────────────────────────────────────────────────────────────────────
garch_spec <- function(o, fixed = NULL) {
  rugarch::ugarchspec(variance.model = list(model = GARCH_RUGARCH[[o$variance_model]], garchOrder = c(o$q, o$p)),
                      mean.model = list(armaOrder = c(o$ar_order, o$ma_order), include.mean = o$mean),
                      distribution.model = o$distribution, fixed.pars = fixed)
}

# rugarch's `hybrid` solver run one solver at a time, so that the report can say which one converged: the same calls, in the same
# order, the random restarts of gosolnp from the node's seed (tab_seed).
garch_fit <- function(o, d) {
  y <- d$r / d$scale
  spec <- garch_spec(o)
  for (solver in GARCH_SOLVERS) {
    fit <- tryCatch(rugarch::ugarchfit(spec, y, out.sample = d$n_ret - d$train, solver = solver), error = function(e) NULL)
    if (!is.null(fit) && identical(fit@fit$convergence, 0) && !is.null(fit@fit$matcoef)) {
      fit@fit$solver_used <- solver
      return(fit)
    }
  }
  tab_refuse("no_convergence", paste0("no solver of rugarch converged on this series (solnp, nlminb, gosolnp): try fewer lags, ",
                                      "or the normal distribution"))
}

# The covariance of the fitted parameters: the inverse Hessian of −ℓ (classical), or Bollerslev & Wooldridge's sandwich H⁻¹(GᵀG)H⁻¹
# from the same Hessian and the per-observation scores G (fit@fit$scores, by Richardson's extrapolation: those Nyblom's test reads).
# rugarch 1.5-6's own robust.cvar takes its meat from other scores (robustvcv), which differ from these by up to 30 % on the
# reference cases while the witness's numerical scores agree with these to 1e-6: the runner computes the sandwich itself.
garch_vcov <- function(fit, se_type) {
  H <- fit@fit$hessian
  Hi <- tryCatch(solve(H), error = function(e) NULL)
  if (is.null(Hi)) return(matrix(NA_real_, nrow(H), ncol(H)))
  if (identical(se_type, "robust")) Hi %*% crossprod(fit@fit$scores) %*% Hi else Hi
}

# J = ∂θ_reported / ∂θ_fitted, the scale's back-transform; θ_reported = θ_fitted·diag(J) but EGARCH's ω (+ (1 − Σβ) ln s²).
garch_jacobian <- function(names, vm, s) {
  d <- stats::setNames(rep(1, length(names)), names)
  d[names == "mu"] <- s
  if (vm != "egarch") d[names == "omega"] <- s^2
  J <- diag(d, length(d))
  dimnames(J) <- list(names, names)
  if (vm == "egarch") J["omega", startsWith(names, "beta")] <- -log(s^2)
  J
}

garch_model <- function(o, d, fit) {
  s <- d$scale
  k <- d$n_ret - d$train
  est <- rugarch::coef(fit)
  names <- names(est)
  J <- garch_jacobian(names, o$variance_model, s)
  theta <- est * diag(J)
  if (o$variance_model == "egarch") theta[["omega"]] <- est[["omega"]] + (1 - sum(est[startsWith(names, "beta")])) * log(s^2)
  V <- J %*% garch_vcov(fit, o$se_type) %*% t(J)
  se <- sqrt(pmax(diag(V), 0))
  se[!is.finite(se) | diag(V) < 0] <- NA_real_
  zc <- stats::qnorm(1 - o$alpha / 2)
  coef <- data.frame(term = names, estimate = theta, std_error = se, statistic = theta / se,
                     p_value = 2 * stats::pnorm(-abs(theta / se)), ci_low = theta - zc * se, ci_high = theta + zc * se,
                     stringsAsFactors = FALSE)
  # the filter of the whole span with the parameters fixed, its recursion started as the fit's (n.old): the fit's own σ and z on the
  # training part to the bit, then each held-out return from the returns before it
  y <- d$r / s
  flt <- rugarch::ugarchfilter(garch_spec(o, as.list(est)), y, n.old = d$train)
  sigma <- as.numeric(rugarch::sigma(flt)) * s
  mu <- as.numeric(rugarch::fitted(flt)) * s
  z <- as.numeric(flt@filter$z)
  shape <- if ("shape" %in% names) est[["shape"]] else 0
  skew <- if ("skew" %in% names) est[["skew"]] else 0
  q <- rugarch::qdist(o$distribution, 1 - o$var_level, mu = 0, sigma = 1, skew = skew, shape = shape)
  kappa <- garch_kappa(o$distribution, shape, skew)
  P <- garch_persistence(o, theta, kappa)
  uncond <- if (P >= 1) NA_real_ else if (o$variance_model == "egarch") sqrt(exp(theta[["omega"]] / (1 - P))) else sqrt(theta[["omega"]] / (1 - P))
  eabs <- garch_abs_mean(o$distribution, shape, skew)
  fc <- garch_forecast(o, theta, kappa, eabs, d$r, mu, sigma, o$horizon)
  ll <- as.numeric(rugarch::likelihood(fit)) - d$train * log(s)
  np <- length(est)
  var <- mu + sigma * q
  out <- list(fit = fit, coef = coef, theta = theta, vcov = V, est = est, J = J, sigma = sigma, mu = mu, z = z, var = var, q = q,
              f_sigma = fc$sigma, f_mean = fc$mean, f_var = fc$mean[[1L]] + fc$sigma[[1L]] * q, persistence = P, kappa = kappa, eabs = eabs,
              half_life = if (P > 0 && P < 1) log(0.5) / log(P) else NA_real_, uncond = uncond, log_lik = ll,
              aic = -2 * ll + 2 * np, bic = -2 * ll + np * log(d$train), shape = shape, skew = skew,
              solver = fit@fit$solver_used, bounds = fit@fit$ipars)
  if (k > 0L) {
    held <- seq.int(d$train + 1L, d$n_ret)
    out$backtest <- ts_backtest(d$r[held], var[held], o$var_level, o$alpha)
  }
  out
}

# κ = E[z² 1(z < 0)] under the fitted law (½ for a symmetric one), by quadrature: the share of a shock's square that GJR's γ adds,
# in the persistence and the forecasts. rugarch 1.5-6's persistence() and forecasts use P(z < 0) instead (its `pneg`), the same
# number for a symmetric law and not for the skewed t; the runner keeps the definition (Glosten, Jagannathan & Runkle 1993).
garch_kappa <- function(dist, shape, skew) {
  if (dist != "sstd") return(0.5)
  stats::integrate(function(z) z^2 * rugarch::ddist("sstd", z, 0, 1, skew = skew, shape = shape), -Inf, 0, rel.tol = 1e-12)$value
}

# E|z| under the fitted law: EGARCH's size term is |z| − E|z| (rugarch's egarchKappa)
garch_abs_mean <- function(dist, shape, skew) {
  if (dist == "norm") return(sqrt(2 / pi))
  f <- function(z) abs(z) * rugarch::ddist(dist, z, 0, 1, skew = skew, shape = shape)
  stats::integrate(f, -Inf, 0, rel.tol = 1e-12)$value + stats::integrate(f, 0, Inf, rel.tol = 1e-12)$value
}

garch_persistence <- function(o, theta, kappa) {
  b <- sum(theta[startsWith(names(theta), "beta")])
  if (o$variance_model == "egarch") return(b)
  b + sum(theta[startsWith(names(theta), "alpha")]) + kappa * sum(theta[startsWith(names(theta), "gamma")])
}

# The forecasts after the last instant T, from the filter's state on the series' units: future shocks at their expectation
# (E[ε²] = σ², E[1(ε < 0) ε²] = κσ², E z = 0, E(|z| − E|z|) = 0, future ε = 0 in the mean), the recursion of each model otherwise.
garch_forecast <- function(o, theta, kappa, eabs, r, mu, sigma, h) {
  n <- length(r)
  eps <- r - mu
  th <- function(nm) if (nm %in% names(theta)) theta[[nm]] else 0
  m0 <- th("mu")
  x <- c(r, numeric(h))
  e <- c(eps, numeric(h))
  v <- c(if (o$variance_model == "egarch") log(sigma^2) else sigma^2, numeric(h))
  for (t in n + seq_len(h)) {
    mean <- m0
    for (i in seq_len(o$ar_order)) mean <- mean + th(paste0("ar", i)) * (x[t - i] - m0)
    for (j in seq_len(o$ma_order)) mean <- mean + th(paste0("ma", j)) * e[t - j]
    x[t] <- mean
    s2 <- th("omega")
    for (i in seq_len(o$q)) {
      past <- t - i <= n
      a <- th(paste0("alpha", i))
      g <- th(paste0("gamma", i))
      if (o$variance_model == "egarch") {
        if (past) s2 <- s2 + a * e[t - i] / sigma[t - i] + g * (abs(e[t - i] / sigma[t - i]) - eabs)
      } else {
        s2 <- s2 + if (past) (a + g * (e[t - i] < 0)) * e[t - i]^2 else (a + g * kappa) * v[t - i]
      }
    }
    for (j in seq_len(o$p)) s2 <- s2 + th(paste0("beta", j)) * v[t - j]
    v[t] <- s2
  }
  f <- v[n + seq_len(h)]
  list(mean = x[n + seq_len(h)], sigma = sqrt(if (o$variance_model == "egarch") exp(f) else f))
}

# The news impact curve: σ²_t against ε_{t−1} = x·σ_∞, every other lag at its long-run level (Engle & Ng 1993)
garch_news <- function(o, theta, kappa, eabs, uncond, x) {
  th <- function(nm) if (nm %in% names(theta)) theta[[nm]] else 0
  if (o$variance_model == "egarch") {
    b <- sum(theta[startsWith(names(theta), "beta")])
    return(exp(th("omega") + th("alpha1") * x + th("gamma1") * (abs(x) - eabs) + b * log(uncond^2)))
  }
  rest <- garch_persistence(o, theta, kappa) - th("alpha1") - kappa * th("gamma1")
  eps <- x * uncond
  th("omega") + (th("alpha1") + th("gamma1") * (eps < 0)) * eps^2 + rest * uncond^2
}

# ── the checks ──────────────────────────────────────────────────────────────────────────────────────────────────
# Fisher & Gallagher's (2012) weighted Ljung–Box on x (z or z²) up to lag m, K fitted coefficients: Q_W = n(n + 2) Σ_k ((m − k + 1)/m)
# ρ̂_k² / (n − k), its p-value from their gamma approximation as rugarch 1.5-6 transcribes it (Weighted.Box.test): shape
# (3/4)(m + 1)² m / (2m² + 3m + 1 − 6mK), scale (2/3)(2m² + 3m + 1 − 6mK) / (m(m + 1)); it needs m ≥ 3K − 1 (rugarch's own bound).
garch_weighted_lb <- function(id, x, m, K, alpha, acf = NULL) {
  n <- length(x)
  what <- if (id == "ljung_box_z") "standardized residuals" else "squared standardized residuals"
  h0 <- paste0("the ", what, " are not autocorrelated up to lag ", m)
  if (m < max(1L, 3L * K - 1L) || m >= n) {
    return(.ts_not_computed(id, h0, paste0("the weighted test with ", K, " fitted coefficients needs between ", max(1L, 3L * K - 1L),
                                           " and ", n - 1L, " lags, not ", m), "lb_lag_range", lag = m))
  }
  if (is.null(acf) || length(acf) < m) acf <- ts_acf(x, m)
  k <- seq_len(m)
  qw <- n * (n + 2) * sum((m - k + 1) / m * acf[k]^2 / (n - k))
  dd <- 2 * m^2 + 3 * m + 1 - 6 * m * K
  p <- stats::pgamma(qw, shape = 0.75 * (m + 1)^2 * m / dd, scale = (2 / 3) * dd / (m * (m + 1)), lower.tail = FALSE)
  advice <- if (id == "ljung_box_z") "structure left in the mean: raise the AR or MA order" else
    "volatility the model misses: raise an order, or try GJR or EGARCH"
  list(id = id, kind = "test", h0 = h0, statistic = qw, p_value = p, verdict = if (p < alpha) "failed" else "ok", advice = advice,
       advice_id = paste0(id, "_failed"), lag = m)
}

# Engle & Ng (1993): z²_t on 1, S⁻_{t−1}, S⁻_{t−1}x_{t−1}, S⁺_{t−1}x_{t−1} (S⁻ = 1 when x < 0), x = z; the joint Wald statistic of the
# three slopes with the regression's covariance (s² (XᵀX)⁻¹), χ²₃. With x = ε it is rugarch::signbias's «Joint Effect» (the
# self-test's check); Engle & Ng's regressors are standardized, and so are these.
garch_sign_bias <- function(z, alpha, vm, x = z) {
  n <- length(z)
  h0 <- "the volatility responds alike to falls and rises"
  neg <- as.numeric(x[-n] < 0)
  X <- cbind(1, neg, neg * x[-n], (1 - neg) * x[-n])
  y <- z[-1L]^2
  XtX <- crossprod(X)
  inv <- tryCatch(solve(XtX), error = function(e) NULL)
  if (is.null(inv) || sum(neg) == 0 || sum(neg) == length(neg)) {
    return(.ts_not_computed("sign_bias", h0, "the residuals do not change sign: the sign bias regression cannot be fitted",
                            "sign_bias_singular"))
  }
  b <- as.numeric(inv %*% crossprod(X, y))
  s2 <- sum((y - X %*% b)^2) / (nrow(X) - 4)
  V <- s2 * inv[2:4, 2:4]
  w <- as.numeric(b[2:4] %*% solve(V, b[2:4]))
  p <- stats::pchisq(w, 3, lower.tail = FALSE)
  sym <- vm %in% c("arch", "garch")
  list(id = "sign_bias", kind = "test", h0 = h0, statistic = w, df = 3, p_value = p, verdict = if (p < alpha) "failed" else "ok",
       advice = if (sym) "falls and rises move the volatility differently: choose GJR or EGARCH" else
         "the asymmetry left is not captured by the model's terms",
       advice_id = if (sym) "sign_bias_failed" else "sign_bias_failed_asymmetric")
}

garch_checks <- function(task, o, d, m) {
  z <- m$z[seq_len(d$train)]
  n <- length(z)
  most <- tab_limit(task, "max_lb_lags")
  kz <- o$ar_order + o$ma_order
  kz2 <- o$q + o$p
  K <- max(kz, kz2)
  # auto: min(10, ⌊n/5⌋) + K (FPP3's 10, counted beyond what the test subtracts), at least the 3K − 1 the gamma law needs
  lag <- if (identical(o$lb_lag %||% "auto", "auto")) as.integer(min(most, max(min(10L, n %/% 5L) + K, 3L * K - 1L))) else as.integer(o$lb_lag)
  span <- min(n - 1L, most, max(lag, 20L))
  acf <- list(z = ts_acf(z, span), z2 = ts_acf(z^2, span))
  out <- list(garch_weighted_lb("ljung_box_z", z, lag, kz, o$alpha, acf$z), garch_weighted_lb("ljung_box_z2", z^2, lag, kz2, o$alpha, acf$z2))
  L <- if (identical(o$arch_lags %||% "auto", "auto")) as.integer(TS_SEASONS[o$frequency] %|na|% GARCH_ARCH_LAGS) else as.integer(o$arch_lags)
  h0 <- "there is no ARCH effect left in the standardized residuals"
  out[[3L]] <- if (n < 3L * L + 10L) {
    .ts_not_computed("arch_lm", h0, paste0(L, " lags need at least ", 3L * L + 10L, " residuals"), "arch_lm_short", lag = L)
  } else {
    a <- FinTS::ArchTest(z, lags = L)
    list(id = "arch_lm", kind = "test", h0 = h0, statistic = unname(a$statistic), df = unname(a$parameter), p_value = a$p.value,
         verdict = if (a$p.value < o$alpha) "failed" else "ok", advice = "volatility the model misses: raise an order, or try GJR or EGARCH",
         advice_id = "arch_lm_failed", lag = L)
  }
  ny <- nyblom_test(m$fit@fit$scores, names(m$est), o$alpha, m$J)
  out <- c(out, list(ny$joint), ny$parts, list(garch_sign_bias(z, o$alpha, o$variance_model)))
  # facts: the parameters at a bound of the solver (their standard errors are not reliable), the variance's stationarity
  b <- m$bounds[names(m$est), , drop = FALSE]
  tol <- 1e-6 * pmax(1, b[, "UB"] - b[, "LB"])
  at <- names(m$est)[abs(m$est - b[, "LB"]) <= tol | abs(b[, "UB"] - m$est) <= tol]
  rp <- as.numeric(rugarch::persistence(m$fit))  # the solver's constraint is on rugarch's own persistence
  if (o$variance_model != "egarch" && rp >= GARCH_PERSISTENCE_BOUND - 1e-6) at <- c(at, "persistence")
  out[[length(out) + 1L]] <- ts_measure("at_bound", length(at), count = length(at))
  for (a in at) out[[length(out) + 1L]] <- ts_measure("at_bound", if (a == "persistence") rp else m$theta[[a]], level_2 = a)
  out[[length(out) + 1L]] <- ts_measure("variance_stationary", m$persistence, count = as.integer(m$persistence < 1))
  attr(out, "lb_lag") <- lag
  attr(out, "arch_lags") <- L
  attr(out, "acf") <- acf
  attr(out, "at_bound") <- at
  out
}

`%|na|%` <- function(a, b) if (length(a) == 0L || is.na(a)) b else a

# ── the rows ────────────────────────────────────────────────────────────────────────────────────────────────────
.garch_sub <- function(i) chartr("0123456789", "₀₁₂₃₄₅₆₇₈₉", as.character(i))

garch_formula <- function(o) {
  sub <- .garch_sub
  mean <- c(if (o$mean) "μ", if (o$ar_order) paste0("φ", sub(seq_len(o$ar_order)), "(r₋", sub(seq_len(o$ar_order)), if (o$mean) " − μ", ")"),
            if (o$ma_order) paste0("θ", sub(seq_len(o$ma_order)), "ε₋", sub(seq_len(o$ma_order))), "ε")
  i <- seq_len(o$q)
  j <- seq_len(o$p)
  var <- switch(o$variance_model,
    egarch = paste0("ln σ² = ω + ", paste0("α", sub(i), "z₋", sub(i), " + γ", sub(i), "(|z₋", sub(i), "| − E|z|)", collapse = " + "),
                    if (o$p) paste0(" + ", paste0("β", sub(j), "·ln σ²₋", sub(j), collapse = " + "))),
    gjr = paste0("σ² = ω + ", paste0("(α", sub(i), " + γ", sub(i), "·I(ε₋", sub(i), " < 0))·ε²₋", sub(i), collapse = " + "),
                 if (o$p) paste0(" + ", paste0("β", sub(j), "·σ²₋", sub(j), collapse = " + "))),
    paste0("σ² = ω + ", paste0("α", sub(i), "·ε²₋", sub(i), collapse = " + "), if (o$p) paste0(" + ", paste0("β", sub(j), "·σ²₋", sub(j), collapse = " + "))))
  paste0("r = ", paste(mean, collapse = " + "), ", ", var, ", z ~ ", GARCH_LAW[[o$distribution]])
}

garch_note <- function(id, ...) {
  values <- list(...)
  paste(c(paste0("@", id), vapply(names(values), function(k) paste0(k, "=", format(values[[k]], digits = 17, scientific = FALSE, trim = TRUE)), "")),
        collapse = " ")
}

garch_model_rows <- function(rows, o, d, m, checks) {
  txt <- function(stat, v) tab_text(rows, "model", stat, v)
  num <- function(stat, v) tab_number(rows, "model", stat, v)
  txt("formula", garch_formula(o))
  txt("software", tab_software(GARCH_PACKAGES))
  txt("model_name", garch_name(o))
  for (k in c("variance_model", "distribution", "input_kind", "se_type", "frequency")) txt(k, o[[k]])
  if (o$input_kind == "prices") txt("returns_type", o$returns_type)
  txt("time_kind", d$kind)
  txt("solver", m$solver)
  num("n", d$n_ret)
  num("n_missing", d$n_missing)
  num("first", d$x0[[1L]])
  num("last", d$x0[[d$n]])
  for (k in c("alpha", "holdout", "horizon", "var_level")) num(k, o[[k]])
  num("scale", d$scale)
  for (k in c("log_lik", "aic", "bic", "persistence", "half_life")) num(k, m[[k]])
  num("half_life_time", m$half_life)  # a step is one unit of the frequency (a trading day with `index`)
  num("uncond_sigma", m$uncond)
  num("sigma_last", m$sigma[[d$n_ret]])
  num("sigma_next", m$f_sigma[[1L]])
  num("lb_lag_used", attr(checks, "lb_lag"))
  num("arch_lags_used", attr(checks, "arch_lags"))
  if (o$variance_model == "gjr") num("kappa", m$kappa)
  tab_note(rows, garch_note("garch_scale", scale = d$scale))
  if (!identical(m$solver, "solnp")) tab_note(rows, garch_note("garch_solver", solver = m$solver))
  for (a in attr(checks, "at_bound")) tab_note(rows, garch_note("garch_at_bound", parameter = a))
  invisible(rows)
}

garch_coef_rows <- function(rows, o, m) {
  cf <- m$coef
  for (i in seq_len(nrow(cf))) {
    t <- cf$term[[i]]
    for (stat in c("estimate", "std_error", "statistic")) tab_number(rows, "coef", stat, cf[[stat]][[i]], level = t)
    tab_p(rows, "coef", cf$p_value[[i]], level = t)
    tab_number(rows, "coef", "ci_low", cf$ci_low[[i]], level = t)
    tab_number(rows, "coef", "ci_high", cf$ci_high[[i]], level = t)
  }
  invisible(rows)
}

# ── the charts ──────────────────────────────────────────────────────────────────────────────────────────────────
# A long series is thinned by envelope (ts_envelope: each bucket's lowest and highest, never sampled); the numbers use every instant.
garch_charts <- function(task, rows, o, d, m, acf) {
  most <- tab_limit(task, "max_chart_points")
  ret <- seq_len(d$n_ret)
  x <- d$x0[ret + d$lead]
  zc <- stats::qnorm(1 - o$alpha / 2)
  keep <- ts_envelope(d$r, most)
  tab_points(rows, "curve", "returns", "y", x = x[keep], y = d$r[keep], level_2 = "return")
  tab_points(rows, "curve", "returns", "y", x = x[keep], y = (m$mu - zc * m$sigma)[keep], level_2 = "band_low")
  tab_points(rows, "curve", "returns", "y", x = x[keep], y = (m$mu + zc * m$sigma)[keep], level_2 = "band_high")
  ks <- ts_envelope(m$sigma, most)
  tab_points(rows, "curve", "sigma", "y", x = x[ks], y = m$sigma[ks], level_2 = "sigma")
  future <- ts_time_after(d$times[[d$n]], seq_len(o$horizon), d$kind, o$frequency)
  tab_points(rows, "curve", "sigma", "y", x = ts_x0(future, d$kind), y = m$f_sigma, level_2 = "forecast")
  if (is.finite(m$uncond)) tab_points(rows, "curve", "sigma", "y", x = x[ks], y = rep(m$uncond, length(ks)), level_2 = "uncond")
  kz <- ts_envelope(m$z, most)
  tab_points(rows, "point", "std_residual", "y", x = x[kz], y = m$z[kz])
  if (is.finite(m$uncond)) {
    ni <- garch_news(o, m$theta, m$kappa, m$eabs, m$uncond, GARCH_NEWS)
    tab_points(rows, "curve", "news_impact", "y", x = GARCH_NEWS, y = ni, level_2 = "sigma2")
  }
  zt <- m$z[seq_len(d$train)]
  h <- tab_hist(zt)
  tab_points(rows, "hist", "std_residual", "count", x = h$x0, y = h$count, x1 = h$x1)
  grid <- seq(min(h$x0), max(h$x1), length.out = 101L)
  dens <- rugarch::ddist(o$distribution, grid, mu = 0, sigma = 1, skew = m$skew, shape = m$shape)
  tab_points(rows, "curve", "density", "y", x = grid, y = dens, level_2 = "density")
  zs <- sort(zt)
  pp <- stats::ppoints(min(length(zs), GARCH_QQ_POINTS))
  qy <- if (length(zs) <= GARCH_QQ_POINTS) zs else stats::quantile(zs, pp, type = 7, names = FALSE)
  tab_points(rows, "point", "qq", "y", x = rugarch::qdist(o$distribution, pp, mu = 0, sigma = 1, skew = m$skew, shape = m$shape), y = qy)
  bound <- stats::qnorm(1 - o$alpha / 2) / sqrt(d$train)
  for (k in c("z", "z2")) {
    a <- acf[[k]]
    if (!length(a)) next
    tab_points(rows, "lag", paste0(k, "_acf"), "acf", x = seq_along(a), y = a)
    tab_number(rows, "lag", "bound", bound, level = paste0(k, "_acf"))
  }
  if (o$holdout > 0L) {
    held <- seq.int(d$train + 1L, d$n_ret)
    kb <- held[sort(unique(c(ts_envelope(d$r[held], most), which(m$backtest$hit == 1L))))]
    kb <- kb[seq_len(min(length(kb), most))]
    tab_points(rows, "curve", "backtest", "y", x = x[kb], y = d$r[kb], level_2 = "return")
    tab_points(rows, "curve", "backtest", "y", x = x[kb], y = m$var[kb], level_2 = "var")
    tab_points(rows, "point", "violation", "y", x = x[kb], y = m$backtest$hit[kb - d$train])
  }
  invisible(rows)
}

# ── the output ──────────────────────────────────────────────────────────────────────────────────────────────────
# `volatility`: a row per instant of the span (a price series' first row with its price and empty model columns), then `horizon`
# forecast rows; `time` in the input field's own Arrow type; `return` with prices; `var` the 1-step VaR (in-sample, holdout and the
# first forecast step); `violation` r_t < VaR_t.
garch_output <- function(o, d, m) {
  pad <- function(v, f = rep(NA_real_, o$horizon)) c(rep(NA_real_, d$lead), v, f)
  h <- o$horizon
  future <- ts_time_after(d$times[[d$n]], seq_len(h), d$kind, o$frequency)
  if (d$kind == "datetime") attr(future, "tzone") <- attr(d$times, "tzone")
  time <- arrow::concat_arrays(ts_time_array(d$times, d$type), ts_time_array(future, d$type))
  hold <- seq_len(d$n_ret) > d$train
  cols <- list(time = time, observed = c(d$y, rep(NA_real_, h)))
  if (o$input_kind == "prices") cols$return <- pad(d$r)
  cols$mean <- pad(m$mu, m$f_mean)
  cols$sigma <- pad(m$sigma, m$f_sigma)
  cols$std_residual <- pad(m$z)
  cols$var <- pad(m$var, c(m$f_var, rep(NA_real_, h - 1L)))
  cols$violation <- c(rep(NA, d$lead), d$r < m$var, rep(NA, h))
  cols$is_holdout <- c(rep(FALSE, d$lead), hold, rep(FALSE, h))
  cols$is_forecast <- c(rep(FALSE, d$n), rep(TRUE, h))
  for (k in names(cols)) if (is.numeric(cols[[k]])) cols[[k]][!is.finite(cols[[k]])] <- NA_real_
  do.call(arrow::arrow_table, cols)
}

# ── the card and the native object ──────────────────────────────────────────────────────────────────────────────
# What Predict (lot G6) needs to continue the filter from the last instant without the series: the specification, the parameters on
# the series' units and their covariance, the last returns, residuals, variances (log-variances for EGARCH) and z the recursions
# read, the last price with prices.
garch_card <- function(task, o, d, m) {
  card <- tab_card(task, NULL, GARCH_PACKAGES)
  card$inputs <- list(list(name = o$time, type = "time"), list(name = o$value, type = "number"))
  card$terms <- lapply(m$coef$term, function(t) list(term = t, level = NULL, variables = list()))
  card$coefficients <- as.list(unname(tab_finite(m$theta)))
  card$vcov <- tab_card_vcov(task, m$vcov)
  card$alpha <- o$alpha
  card$formula <- garch_formula(o)
  n <- d$n_ret
  last <- function(v, k) if (k > 0L) as.list(utils::tail(v, k)) else list()
  eps <- d$r - m$mu
  lag <- max(o$q, o$ma_order, 1L)
  card$garch <- list(
    variance_model = o$variance_model, arch_order = o$q, garch_order = o$p, ar_order = o$ar_order, ma_order = o$ma_order,
    mean = o$mean, distribution = o$distribution, input_kind = o$input_kind, returns_type = o$returns_type, var_level = o$var_level,
    frequency = o$frequency, coefficients = as.list(stats::setNames(tab_finite(m$theta), m$coef$term)), var_quantile = m$q,
    last = list(time_kind = d$kind, time = tab_label(d$times[[d$n]]), price = if (o$input_kind == "prices") d$y[[d$n]] else NULL,
                returns = last(d$r, max(o$ar_order, 1L)), residuals = last(eps, lag),
                sigma2 = if (o$variance_model == "egarch") NULL else last(m$sigma^2, max(o$p, 1L)),
                log_sigma2 = if (o$variance_model == "egarch") last(log(m$sigma^2), max(o$p, 1L)) else NULL,
                z = last(m$z, lag), sigma_next = m$f_sigma[[1L]], mean_next = m$f_mean[[1L]]))
  card
}

# The uGARCHfit without its data (the series, σ, z, the scores): the estimates, their covariance, the solver's answer. Its numbers
# are on the scale r / s: `scale` holds s.
garch_native <- function(fit, scale) {
  fit@model$modeldata$data <- numeric()
  fit@model$modeldata$index <- integer()
  for (k in c("sigma", "z", "residuals", "fitted.values", "log.likelihoods", "scores", "condH")) fit@fit[[k]] <- NULL
  attr(fit, "scale") <- scale
  fit
}

# ── Predict (lot G6) ────────────────────────────────────────────────────────────────────────────────────────────
# A registered GARCH applied to the rows after the card's last instant (docs/specs/garch-risk.md, «Card, MLflow and Predict»): the
# filter runs the card's parameters, fixed, from the card's state (the last returns, shocks, variances and z the recursions read)
# through the new rows, one row at a time, so that the series is never held beyond the rows themselves: per row `prediction` is σ_t
# given the rows before it and `prediction_var` the 1-step VaR μ_t + σ_t·q at the card's level. With `horizon` (or with no new row at
# all, the card's own) the forecast rows follow, from the filter's last state (garch_forecast): their σ, and the 1-step VaR of the
# first of them. The rows must continue the card's series: the first one the period after its last instant, none missing, none twice.
#
#   predict_garch(task, card)        the output table: time (the input's own type), prediction, prediction_var
#   garch_predict_rows(task, card)   the new rows, checked: times, values, and the returns (the first from the card's last price)
#   garch_predict_filter(g, th, r)   σ, the conditional mean and the final state of the recursion, from the card's state
predict_garch <- function(task, card) {
  g <- card$garch
  p <- card$params
  th <- unlist(g$coefficients)
  if (is.null(g) || is.null(g$last) || !length(th) || is.null(p$time) || is.null(p$value)) {
    tab_refuse("bad_card", "the model card holds no volatility model to continue (its `garch` object): fit the model again")
  }
  rows <- garch_predict_rows(task, card)
  o <- list(variance_model = g$variance_model, ar_order = g$ar_order, ma_order = g$ma_order, q = g$arch_order, p = g$garch_order,
            distribution = g$distribution)
  fl <- garch_predict_filter(o, th, g, rows$r)
  n <- length(rows$r)
  h <- task$params$horizon
  h <- if (!is.null(h)) as.integer(h) else if (n == 0L) as.integer(p$horizon %||% 10L) else 0L
  shape <- if ("shape" %in% names(th)) th[["shape"]] else 0
  skew <- if ("skew" %in% names(th)) th[["skew"]] else 0
  q <- g$var_quantile
  sigma <- fl$sigma
  var <- fl$mean + sigma * q
  times <- if (n) ts_time_array(rows$times, rows$type) else NULL
  if (h > 0L) {
    kappa <- garch_kappa(o$distribution, shape, skew)
    eabs <- garch_abs_mean(o$distribution, shape, skew)
    fc <- garch_forecast(o, th, kappa, eabs, fl$state$r, fl$state$r - fl$state$e, fl$state$sigma, h)
    future <- ts_time_after(rows$last, seq_len(h), rows$kind, g$frequency)
    if (rows$kind == "datetime") attr(future, "tzone") <- attr(rows$last, "tzone")
    future <- ts_time_array(future, rows$type)
    times <- if (is.null(times)) future else arrow::concat_arrays(times, future)
    sigma <- c(sigma, fc$sigma)
    var <- c(var, fc$mean[[1L]] + fc$sigma[[1L]] * q, rep(NA_real_, h - 1L))
  }
  sigma[!is.finite(sigma)] <- NA_real_
  var[!is.finite(var)] <- NA_real_
  tbl <- arrow::arrow_table(c0 = times, c1 = sigma, c2 = var)  # named after: a time column called «schema» would be arrow_table's own argument
  names(tbl) <- c(p$time, "prediction", "prediction_var")
  list(output = tbl)
}

# The new rows: the card's two columns, sorted by time, checked against the card's last instant. `times` (the input's class, an
# Int64 as a double), `type` (the input's Arrow type, for the output), `kind`, `last` (the last time: the card's, or the last row's),
# `r` the returns (with prices, the first from the card's last price).
garch_predict_rows <- function(task, card) {
  g <- card$garch
  time <- card$params$time
  value <- card$params$value
  read <- ts_read(task, c(time, value))
  df <- read$df
  # an Int64 arrives as a 64-bit integer: its digits are kept in the output's type, the computation needs the number
  if (inherits(df[[time]], "integer64")) df[[time]] <- as.numeric(df[[time]])
  kind <- ts_time_kind(df[[time]], time, g$frequency)
  if (!identical(kind, g$last$time_kind)) {
    tab_refuse("predict_column", paste0("the column «", time, "» holds ", c(date = "dates", datetime = "datetimes", integer = "whole numbers")[[kind]],
                                        ", and the model was fitted with ", c(date = "dates", datetime = "datetimes", integer = "whole numbers")[[g$last$time_kind]]),
               list(column = time))
  }
  if (!tab_kind(df[[value]]) %in% c("number", "integer")) {
    tab_refuse("not_a_number", paste0("the column «", value, "» is not a number"), list(column = value))
  }
  df[[value]] <- as.numeric(df[[value]])
  tab_complete_rows(df, value)
  df <- df[!is.na(df[[time]]), , drop = FALSE]
  last <- switch(kind, date = as.Date(g$last$time), datetime = as.POSIXct(g$last$time, tz = "UTC"), as.numeric(g$last$time))
  out <- list(kind = kind, type = read$type[[time]], times = df[[time]][0L], last = last, r = numeric())
  n <- nrow(df)
  if (!n) return(out)
  df <- df[order(as.numeric(df[[time]]), method = "radix"), , drop = FALSE]
  tt <- df[[time]]
  if (kind == "datetime") attr(tt, "tzone") <- attr(df[[time]], "tzone")
  what <- if (g$input_kind == "prices") "price" else "return"
  empty <- which(is.na(df[[value]]))
  if (length(empty)) {
    tab_refuse("gaps", paste0("the rows have ", length(empty), " ", if (length(empty) == 1L) "period" else "periods", " without a ", what,
                              " (the first: ", tab_label(tt[[empty[[1L]]]]), "): filter out the rows without a ", what,
                              ", or fill them in the flow; a missing return is not interpolated"),
               list(n = length(empty), time = tab_label(tt[[empty[[1L]]]])))
  }
  twice <- which(duplicated(as.numeric(tt)))
  if (length(twice)) {
    at <- tab_label(tt[[twice[[1L]]]])
    tab_refuse("duplicate_time", paste0("the column «", time, "» holds two rows for the period of ", at, ": a series has one row per ",
                                        "period; aggregate first with Group by"), list(column = time, time = at))
  }
  if (as.numeric(tt[[1L]]) <= as.numeric(last)) {
    tab_refuse("predict_before_last", paste0("the rows start before the model's last day (", tab_label(last), "): Predict continues a ",
                                             "series, so the first row must be the period after it (", tab_label(tt[[1L]]), " is not)"),
               list(first = tab_label(tt[[1L]]), last = tab_label(last)))
  }
  # one row a period after the card's last instant, none missing: a frequency `index` has no calendar, so only the order counts
  if (kind == "integer" || g$frequency != "index") {
    gap <- ts_period(tt, kind, g$frequency) - ts_period(last, kind, g$frequency) - seq_len(n)
    off <- which(abs(gap) > 1e-9)
    if (length(off)) {
      at <- tab_label(tt[[off[[1L]]]])
      tab_refuse("predict_gap", paste0("the rows do not continue the model's series (its last day is ", tab_label(last), "): the row of ", at,
                                       " is not the period ", if (off[[1L]] == 1L) "right after it" else "after the row before it",
                                       "; Predict needs one row per period, none missing: fill them in the flow"),
                 list(time = at, last = tab_label(last)))
    }
  }
  y <- df[[value]]
  if (g$input_kind == "prices") {
    if (any(y <= 0)) {
      tab_refuse("not_positive", paste0("the column «", value, "» holds 0 or a negative price: returns need positive prices"),
                 list(column = value))
    }
    prev <- c(g$last$price, y[-n])
    out$r <- if (identical(g$returns_type, "simple")) y / prev - 1 else log(y / prev)
  } else {
    out$r <- y
  }
  out$times <- tt
  out$last <- tt[[n]]
  out
}

# The recursion of the model on the series' units from the card's state, row by row (the same as rugarch's filter with the parameters
# fixed: the self-test checks it equal to 1e-10): the conditional mean μ_t = μ + Σ φ_i (r_{t−i} − μ) + Σ θ_j ε_{t−j}, then σ²_t from
# the shocks, the variances before (EGARCH: ln σ²_t from the z and ln σ²), then ε_t = r_t − μ_t and z_t = ε_t / σ_t. Returns σ_t and
# μ_t of each row and the last `L` values of r, ε and σ, which the forecast reads.
garch_predict_filter <- function(o, th, g, r) {
  egarch <- identical(o$variance_model, "egarch")
  get <- function(nm) if (nm %in% names(th)) th[[nm]] else 0
  coef <- function(stem, k) vapply(seq_len(k), function(i) get(paste0(stem, i)), 0)
  mu0 <- get("mu")
  omega <- get("omega")
  phi <- coef("ar", o$ar_order)
  theta <- coef("ma", o$ma_order)
  alpha <- coef("alpha", o$q)
  gamma <- coef("gamma", o$q)
  beta <- coef("beta", o$p)
  L <- max(o$ar_order, o$ma_order, o$q, o$p, 1L)
  n <- length(r)
  # the card's state, right-aligned in vectors of L (the lags beyond what the card holds are never read)
  tail_of <- function(x, fill) {
    x <- as.numeric(unlist(x))
    c(rep(fill, max(L - length(x), 0L)), utils::tail(x, L))
  }
  eps <- tail_of(g$last$residuals, 0)
  z0 <- tail_of(g$last$z, 0)
  sg <- ifelse(z0 != 0, abs(eps / z0), 1)  # a lag the variances do not cover (EGARCH, q > p): its z fixes it
  var_state <- if (egarch) exp(as.numeric(unlist(g$last$log_sigma2))) else as.numeric(unlist(g$last$sigma2))
  have <- min(length(var_state), L)
  if (have) sg[seq.int(L - have + 1L, L)] <- sqrt(utils::tail(var_state, have))
  X <- c(tail_of(g$last$returns, 0), numeric(n))
  E <- c(eps, numeric(n))
  S <- c(sg, numeric(n))
  Z <- c(z0, numeric(n))
  MU <- numeric(n)
  ar <- seq_len(o$ar_order)
  ma <- seq_len(o$ma_order)
  qi <- seq_len(o$q)
  pj <- seq_len(o$p)
  eabs <- if (egarch) {
    sh <- if ("shape" %in% names(th)) th[["shape"]] else 0
    sk <- if ("skew" %in% names(th)) th[["skew"]] else 0
    garch_abs_mean(o$distribution, sh, sk)
  } else 0
  for (t in seq_len(n)) {
    i <- L + t
    m <- mu0 + sum(phi * (X[i - ar] - mu0)) + sum(theta * E[i - ma])
    if (egarch) {
      z <- Z[i - qi]
      s2 <- exp(omega + sum(alpha * z + gamma * (abs(z) - eabs)) + sum(beta * log(S[i - pj]^2)))
    } else {
      e <- E[i - qi]
      s2 <- omega + sum((alpha + gamma * (e < 0)) * e^2) + sum(beta * S[i - pj]^2)
    }
    s <- sqrt(s2)
    if (!is.finite(s)) {
      tab_refuse("filter_diverged", paste0("the variance of the row ", t, " of the new rows is not a number: the model's recursion leaves ",
                                           "the range of a number on these values; check the column"), list(row = t))
    }
    X[i] <- r[[t]]
    MU[t] <- m
    E[i] <- r[[t]] - m
    S[i] <- s
    Z[i] <- E[i] / s
  }
  keep <- seq.int(n + 1L, n + L)
  list(sigma = S[L + seq_len(n)], mean = MU, state = list(r = X[keep], e = E[keep], sigma = S[keep]))
}
