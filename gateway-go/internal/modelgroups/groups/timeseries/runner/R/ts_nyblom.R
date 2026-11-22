# Nyblom's stability test (docs/specs/garch-risk.md, «The checks»; lot G1): the statistics from a fit's scores, their p-values
# from the asymptotic law, Holm's adjustment for the individual ones. A verdict comes only from a p-value against alpha.
#
#   nyblom_p(stat, k)                   P(W_k > stat), W_k = Σ_{j≥1} χ²_{k,j} / (j²π²): k Brownian bridges' ∫B²
#   nyblom_test(scores, names, alpha, jac)   the joint and the individual tests, as ts_checks.R's lists
#
# The law. W_k's Laplace transform is E[e^{−sW_k}] = (√(2s) / sinh √(2s))^{k/2}: the product over j of (1 + 2s/(j²π²))^{−k/2}, all
# the eigenvalues, nothing truncated. P(W_k > x) is the inverse transform of (1 − E[e^{−sW_k}]) / s at x, by Talbot's fixed contour
# (Abate & Valkó 2004) with M = 24 nodes: its error is about 10^{−0.6M} and its rounding about 1e−16·e^{2M/5}, below 1e−11 in all.
# rugarch::nyblom gives three critical values (10 %, 5 %, 1 %): a verdict from them would answer three alphas only.

NYBLOM_TALBOT_M <- 24L

nyblom_p <- function(stat, k) {
  if (!is.finite(stat) || k < 1) return(NA_real_)
  if (stat <= 0) return(1)
  M <- NYBLOM_TALBOT_M
  r <- 2 * M / (5 * stat)
  th <- seq_len(M - 1L) * pi / M
  s <- r * th * complex(real = 1 / tan(th), imaginary = 1)
  sig <- th + (th / tan(th) - 1) / tan(th)
  # (1 − L(s)) / s, log L = (k/2)(log w − log sinh w), w = √(2s) in the first quadrant: log sinh w = w + log((1 − e^{−2w}) / 2)
  f <- function(s) {
    w <- sqrt(2 * s)
    (1 - exp((k / 2) * (log(w) - w - log((1 - exp(-2 * w)) / 2)))) / s
  }
  p <- (r / M) * (0.5 * Re(f(complex(real = r))) * exp(r * stat) + sum(Re(exp(stat * s) * f(s) * complex(real = 1, imaginary = sig))))
  min(1, max(0, p))
}

# scores: n × k, the per-observation scores of the log-likelihood at the estimates (rugarch's fit@fit$scores). `jac`: the
# Jacobian ∂θ_reported / ∂θ_fitted (the scale's back-transform), so that each individual test is about the parameter reported:
# the scores become g J⁻¹. The joint statistic does not change with it. As rugarch::nyblom: S_t the cumulated scores, V = Σ g gᵀ,
# L = (1/n) Σ_t S_tᵀ V⁻¹ S_t, L_i = Σ_t S_{t,i}² / (n Σ_t g_{t,i}²).
nyblom_test <- function(scores, names, alpha, jac = NULL) {
  g <- as.matrix(scores)
  if (!is.null(jac)) g <- g %*% solve(jac)
  n <- nrow(g)
  k <- ncol(g)
  h0 <- "every parameter is constant over the sample"
  S <- apply(g, 2L, cumsum)
  if (n == 1L) S <- matrix(S, nrow = 1L)
  xx <- crossprod(S)
  V <- crossprod(g)
  inv <- tryCatch(solve(V), error = function(e) NULL)
  if (is.null(inv) || anyNA(g)) {
    return(list(joint = .ts_not_computed("nyblom", h0, "the scores' variance is singular: Nyblom's test cannot be computed",
                                          "nyblom_singular", count = k), parts = list()))
  }
  joint <- sum(diag(xx %*% inv)) / n
  pj <- nyblom_p(joint, k)
  advice <- "the parameters move over the sample: fit a shorter, more recent period, or read the parameters named below"
  out <- list(joint = list(id = "nyblom", kind = "test", h0 = h0, statistic = joint, p_value = pj,
                           verdict = if (pj < alpha) "failed" else "ok", advice = advice, advice_id = "nyblom_failed", count = k))
  ind <- diag(xx) / (diag(V) * n)
  p <- vapply(ind, nyblom_p, 0, k = 1)
  adj <- stats::p.adjust(p, method = "holm")
  out$parts <- lapply(seq_len(k), function(i) {
    list(id = "nyblom", level_2 = names[[i]], kind = "test", h0 = paste0(names[[i]], " is constant over the sample"),
         statistic = ind[[i]], p_value = p[[i]], p_adjusted = adj[[i]], verdict = if (adj[[i]] < alpha) "failed" else "ok",
         advice = advice, advice_id = "nyblom_failed")
  })
  out
}
