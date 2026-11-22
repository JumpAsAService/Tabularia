# The Bayesian reading of an experiment (docs/specs/ab-testing.md, «Bayesian (a reading by measures)», lot E3):
# the posterior of every variant, P(a treatment is ahead of the control), P(each variant is the best), the expected
# losses, the credible intervals and the posterior curves, as the part `bayes` of fit_ab_test (ab_test.R).
#
# The posteriors, independent across variants:
#   a binary metric                       the rate ~ Beta(prior_alpha + k_j, prior_beta + n_j − k_j)
#   a continuous or ratio metric, CUPED   the mean ~ N(m_j, v_j), a flat prior on the mean (the estimates of
#                                          ab_estimates: with CUPED the adjusted ones; a binary metric with CUPED too)
#
# With CUPED a variant's own posterior is wider than v_j: m_j = ȳ_j − θ̂(x̄_j − x̄) also carries x̄, the covariate's mean
# over all the units, an estimate too (lot E10, `est$v_level` = v_j + θ̂² s²_x / N). That term is the same for every
# variant: it moves all the means together and cancels in a difference. So a variant's own numbers (its sd, its credible
# interval, its curve) read v_level, and the measures, which are all of differences (P(beat), P(best), the losses), read
# v_j: P(t ahead of c) = P(μ_t − μ_c > 0) does not see a shift common to both. Without CUPED v_level is v_j.
#
# «Ahead» is in the direction of `higher_is_better`: with it false, a treatment is ahead where its value is lower, the
# best variant is the lowest, and the losses are the mirrored ones. Write B_j(x) = P(variant j is behind x) (F_j when
# higher is better, the survival function 1 − F_j when lower) and A_j = 1 − B_j; then, with the posteriors independent,
#
#   P(t ahead of c)             = ∫ f_t B_c dx                  (normal: Φ(d / s), d = (m_t − m_c) · direction, s = √(v_t + v_c))
#   P(j is the best)            = ∫ f_j Π_{k≠j} B_k dx
#   loss of choosing t          = E[(c − t)⁺ in the direction] = ∫ B_t A_c dx          (normal: s φ(d/s) − d Φ(−d/s))
#   loss of keeping the control = ∫ B_c A_t dx                                         (normal: s φ(d/s) + d Φ(d/s))
#   loss of j against the best  = E[best] − E[μ_j] = ∫ (B_j − Π_k B_k) dx
#
# the last one is the spec's E[max_k μ_k] − E[μ_j] with E[max] = lo + ∫ (1 − Π F_k): both means are lo + an integral,
# so their difference is written as one integral with no subtraction of two large numbers (it differs from the spec's
# by the posterior mass outside [lo, hi], 2e-15 per variant). Every loss is also written relative to the control's
# posterior mean (its absolute value), and compared with `loss_threshold` as a position, «below» or «above»: the
# owner's margin, not a test (the report says «a reading, not a test»).
#
# Quadrature, deterministic (no Monte Carlo): R's `integrate` (QUADPACK dqags), rel.tol 1e-11, abs.tol 1e-13,
# 200 subdivisions, on [q(1e-15), q(1 − 1e-15)] of the variants involved, both tails with lower.tail = FALSE. The
# range is cut where each variant has its body and its tails (its 1e-15, 0.001, 0.999 and 1 − 1e-15 quantiles), so
# that a variant with a very narrow posterior (a 99 / 1 split) is never a peak between two evaluation points; the
# integral is the sum over the pieces. An integral dqags does not finish (an error code, and an error estimate above
# 1e-10 of the scale) is NA: its measures are not written.

AB_BAYES_RANGE <- 1e-15
AB_BAYES_CUTS <- c(AB_BAYES_RANGE, 1e-3, 1 - 1e-3, 1 - AB_BAYES_RANGE)

# ── the posteriors ────────────────────────────────────────────────────────────────
# NULL where the Bayesian reading cannot be made: a normal posterior needs a variance above 0 (a metric constant in a
# variant has none: `variance_zero` says so in the checks).
ab_bayes_posteriors <- function(r) {
  est <- r$est
  p <- r$ctx$params
  num <- function(x, default) { x <- as.numeric(x %||% default); if (!length(x) || !is.finite(x)) default else x }
  if (identical(est$type, "binary") && is.null(est$cuped)) {
    a0 <- num(p$prior_alpha, 1)
    b0 <- num(p$prior_beta, 1)
    a <- a0 + est$conversions
    b <- b0 + est$n - est$conversions
    s <- a + b
    return(list(kind = "beta", a = a, b = b, mean = a / s, sd = sqrt(a * b / (s^2 * (s + 1))), k = est$k, prior = c(a0, b0)))
  }
  if (!all(is.finite(est$m)) || !all(is.finite(est$v)) || any(est$v <= 0)) return(NULL)
  level <- est$v_level %||% est$v
  if (length(level) != length(est$v) || !all(is.finite(level))) level <- est$v
  level <- pmax(level, est$v)  # θ̂² s²_x / N is never negative: a variant's own variance is never below v_j
  list(kind = "normal", m = est$m, s = sqrt(est$v), s_level = sqrt(level), mean = est$m, sd = sqrt(level), k = est$k)
}

# A variant's own posterior: with CUPED its sd is the level's (see the top of this file); the Beta, and a normal
# posterior without CUPED, are their own.
.abb_level <- function(post) {
  if (identical(post$kind, "normal") && !is.null(post$s_level)) post$s <- post$s_level
  post
}

# The distribution function of variant j at x, lower tail or upper (the upper tail for the far end of the range).
.abb_cdf <- function(post, j, x, lower = TRUE) {
  if (identical(post$kind, "beta")) stats::pbeta(x, post$a[[j]], post$b[[j]], lower.tail = lower)
  else stats::pnorm(x, post$m[[j]], post$s[[j]], lower.tail = lower)
}

.abb_pdf <- function(post, j, x) {
  if (identical(post$kind, "beta")) stats::dbeta(x, post$a[[j]], post$b[[j]]) else stats::dnorm(x, post$m[[j]], post$s[[j]])
}

.abb_quantile <- function(post, j, prob, lower = TRUE) {
  if (identical(post$kind, "beta")) stats::qbeta(prob, post$a[[j]], post$b[[j]], lower.tail = lower)
  else stats::qnorm(prob, post$m[[j]], post$s[[j]], lower.tail = lower)
}

# The points where the integration range of some variants is cut: each one's 1e-15, 0.001, 0.999 and 1 − 1e-15 quantiles.
.abb_breaks <- function(post, which) {
  q <- unlist(lapply(which, function(j) c(.abb_quantile(post, j, AB_BAYES_CUTS[1:2]), .abb_quantile(post, j, AB_BAYES_CUTS[1:2], lower = FALSE))))
  sort(unique(q[is.finite(q)]))
}

# ∫ f over the pieces between `breaks`; NA where a piece does not finish.
.abb_integral <- function(f, breaks) {
  total <- 0
  for (i in seq_len(length(breaks) - 1L)) {
    lo <- breaks[[i]]
    hi <- breaks[[i + 1L]]
    if (!(hi > lo)) next
    res <- tryCatch(stats::integrate(f, lo, hi, rel.tol = 1e-11, abs.tol = 1e-13, subdivisions = 200L, stop.on.error = FALSE), error = function(e) NULL)
    if (is.null(res) || !is.finite(res$value)) return(NA_real_)
    if (res$ierr != 0L && !(res$abs.error <= 1e-10 * max(1, abs(res$value), hi - lo))) return(NA_real_)
    total <- total + res$value
  }
  total
}

# B_j(x): the posterior mass of variant j behind x (below it when higher is better), A_j = 1 − B_j (the other tail,
# computed on its own for the precision).
.abb_behind <- function(post, j, x, higher) .abb_cdf(post, j, x, lower = higher)
.abb_ahead <- function(post, j, x, higher) .abb_cdf(post, j, x, lower = !higher)

# ── the measures ──────────────────────────────────────────────────────────────────
# P(t ahead of c).
ab_bayes_prob_beat <- function(post, t, c, higher) {
  if (identical(post$kind, "normal")) {
    d <- (post$m[[t]] - post$m[[c]]) * if (higher) 1 else -1
    return(stats::pnorm(d / sqrt(post$s[[t]]^2 + post$s[[c]]^2)))
  }
  .abb_integral(function(x) .abb_pdf(post, t, x) * .abb_behind(post, c, x, higher), .abb_breaks(post, c(t, c)))
}

# The expected loss of choosing t instead of the control (`choose` TRUE) or of keeping the control instead of t.
ab_bayes_loss <- function(post, t, c, higher, choose = TRUE) {
  if (identical(post$kind, "normal")) {
    d <- (post$m[[t]] - post$m[[c]]) * if (higher) 1 else -1
    s <- sqrt(post$s[[t]]^2 + post$s[[c]]^2)
    z <- d / s
    return(if (choose) s * stats::dnorm(z) - d * stats::pnorm(-z) else s * stats::dnorm(z) + d * stats::pnorm(z))
  }
  f <- if (choose) {
    function(x) .abb_behind(post, t, x, higher) * .abb_ahead(post, c, x, higher)
  } else {
    function(x) .abb_behind(post, c, x, higher) * .abb_ahead(post, t, x, higher)
  }
  .abb_integral(f, .abb_breaks(post, c(t, c)))
}

# P(j is the best) for every variant, and the expected loss of j against the best.
ab_bayes_best <- function(post, higher) {
  k <- post$k
  everyone <- seq_len(k)
  breaks <- .abb_breaks(post, everyone)
  behind <- function(x, skip = 0L) {
    out <- rep(1, length(x))
    for (i in everyone) if (i != skip) out <- out * .abb_behind(post, i, x, higher)
    out
  }
  prob <- vapply(everyone, function(j) .abb_integral(function(x) .abb_pdf(post, j, x) * behind(x, j), breaks), 0)
  loss <- vapply(everyone, function(j) .abb_integral(function(x) .abb_behind(post, j, x, higher) - behind(x), breaks), 0)
  list(prob = prob, loss = loss)
}

# The grid the curves of the variants' own posteriors (`.abb_level`) share.
ab_bayes_grid <- function(post, points) {
  lo <- min(vapply(seq_len(post$k), function(j) .abb_quantile(post, j, 5e-4), 0))
  hi <- max(vapply(seq_len(post$k), function(j) .abb_quantile(post, j, 5e-4, lower = FALSE), 0))
  seq(lo, hi, length.out = points)
}

# Everything the part writes, as numbers: one list per variant.
ab_bayes_measures <- function(r, post) {
  est <- r$est
  higher <- isTRUE(r$ctx$higher_is_better)
  k <- est$k
  ctrl <- est$control
  alpha <- r$ctx$alpha
  best <- ab_bayes_best(post, higher)
  own <- .abb_level(post)  # the sd and the credible interval are a variant's own; the measures are of differences
  base <- abs(post$mean[[ctrl]])
  rel <- function(x) if (is.finite(base) && base > 0) x / base else NA_real_
  out <- vector("list", k)
  for (j in seq_len(k)) {
    out[[j]] <- list(j = j, mean = post$mean[[j]], sd = post$sd[[j]], low = .abb_quantile(own, j, alpha / 2),
                     high = .abb_quantile(own, j, alpha / 2, lower = FALSE), prob_best = best$prob[[j]],
                     loss_best = best$loss[[j]], loss_best_relative = rel(best$loss[[j]]))
    if (j != ctrl) {
      out[[j]]$prob_beat <- ab_bayes_prob_beat(post, j, ctrl, higher)
      out[[j]]$loss <- ab_bayes_loss(post, j, ctrl, higher, TRUE)
      out[[j]]$loss_control <- ab_bayes_loss(post, j, ctrl, higher, FALSE)
      out[[j]]$loss_relative <- rel(out[[j]]$loss)
      out[[j]]$loss_control_relative <- rel(out[[j]]$loss_control)
    }
  }
  out
}

# ── the part ──────────────────────────────────────────────────────────────────────
ab_part_bayes <- function(r) {
  if (!isTRUE(r$ctx$params$bayes %||% TRUE)) return(NULL)
  post <- ab_bayes_posteriors(r)
  if (is.null(post)) return(NULL)
  est <- r$est
  ctx <- r$ctx
  labels <- est$labels
  ctrl <- est$control
  threshold <- as.numeric(ctx$params$loss_threshold %||% 0.001)
  m <- ab_bayes_measures(r, post)
  treatments <- setdiff(seq_len(est$k), ctrl)

  rows <- function(rows) {
    for (x in m) {
      lv <- labels[[x$j]]
      num <- function(stat, value) tab_number(rows, "bayes", stat, value, lv)
      num("posterior_mean", x$mean)
      num("posterior_sd", x$sd)
      num("cred_low", x$low)
      num("cred_high", x$high)
      num("prob_best", x$prob_best)
      num("expected_loss_best", x$loss_best)
      num("expected_loss_best_relative", x$loss_best_relative)
      if (x$j != ctrl) {
        num("prob_beat_control", x$prob_beat)
        num("expected_loss", x$loss)
        num("expected_loss_relative", x$loss_relative)
        num("expected_loss_control", x$loss_control)
        num("expected_loss_control_relative", x$loss_control_relative)
        if (is.finite(x$loss_relative)) tab_text(rows, "bayes", "loss_position", if (x$loss_relative <= threshold) "below" else "above", lv)
      }
    }
    if (identical(post$kind, "beta")) {
      tab_number(rows, "model", "prior_alpha", post$prior[[1L]])
      tab_number(rows, "model", "prior_beta", post$prior[[2L]])
    }
    tab_number(rows, "model", "loss_threshold", threshold)
    own <- .abb_level(post)
    grid <- ab_bayes_grid(own, as.integer(tab_limit(r$task, "posterior_points")))
    for (j in seq_len(est$k)) tab_points(rows, "curve", "posterior", "y", x = grid, y = .abb_pdf(own, j, grid), level_2 = labels[[j]])
  }

  reading <- lapply(m[treatments], function(x) {
    lv <- labels[[x$j]]
    beat <- if (is.finite(x$prob_beat)) ab_line("bayes_beat", variant = lv, control = labels[[ctrl]], probability = x$prob_beat) else NA_character_
    loss <- if (is.finite(x$loss_relative)) ab_line("bayes_loss", variant = lv, loss = x$loss_relative, threshold = threshold) else NA_character_
    ab_block("bayes", c(beat, loss), lv)
  })

  # one value per variant, NA for the control where a measure is of a treatment
  of <- function(stat) vapply(m, function(x) x[[stat]] %||% NA_real_, 0)
  list(rows = rows, reading = reading, method = ab_line("method_bayes"),
       formulas = ab_line(if (identical(post$kind, "beta")) "formula_beta" else "formula_normal"),
       output = list(prob_beat_control = of("prob_beat"), prob_best = of("prob_best"), expected_loss = of("loss")))
}
