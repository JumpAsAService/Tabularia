# The comparisons of an experiment (docs/specs/ab-testing.md, «The metrics», «CUPED», «The comparison
# (frequentist)», «Power at this size», «The other checks»). Everything here works on the sufficient
# statistics ab_read() gives (its `totals` and `totals_own`), never on the rows.
#
#   ab_estimates(d)        the estimate m_j and the variance of that estimate v_j of every variant, for the
#                          four metrics: binary (p̂, p̂(1 − p̂)/n), continuous (ȳ, s²/n), ratio (ΣY/ΣX, the delta
#                          method), and any of them with CUPED (ȳ − θ̂(x̄_j − x̄), s²(y − θ̂x)/n)
#   ab_compare(est, ctx)   each treatment against the control: the difference with z or Welch's t, the relative
#                          lift with Fieller's interval and the delta method's standard error, the p-values
#                          adjusted with p.adjust, the decision (a verdict: the adjusted p against alpha), where
#                          the lift's interval lies against the minimum lift (a statement), the power at this
#                          size (a measure)
#   ab_fieller(…)          Fieller's interval of a ratio of two means
#   ab_welch_anova(…)      Welch's one-way F from means, variances and counts (covariate_balance)
#   ab_missing_chi2(…)     Pearson's χ² of «empty / given» by variant (covariate_missing_balance)
#
# A verdict comes only from a p-value against the node's alpha (or srm_alpha); the minimum lift, the power and
# the detectable lift are measures, with no threshold of their own. No random number is drawn.

AB_CORRECTIONS <- c(holm = "holm", benjamini_hochberg = "BH", bonferroni = "bonferroni", none = "none")

# ── the estimates ────────────────────────────────────────────────────────────────
# The within-variant sums of cross products S_ab,j = Σ_i (a_ij − ā_j)(b_ij − b̄_j), from the shifted sums
# directly (not the (co)variance times n − 1: a variant with one unit of its own covariate has S = 0, not NaN).
.abc_s <- function(d, a, b = a, own = FALSE) {
  t <- if (own && !is.null(d$totals_own)) d$totals_own else d$totals
  col <- paste0("ss_", a, b)
  if (!col %in% colnames(t)) col <- paste0("ss_", b, a)
  n <- t[, "n"]
  ca <- t[, paste0("sum_", a)] - n * d$shift[[a]]
  cb <- t[, paste0("sum_", b)] - n * d$shift[[b]]
  t[, col] - ca * cb / n
}

ab_estimates <- function(d) {
  type <- d$metric_type
  t <- d$totals
  n <- unname(t[, "n"])
  out <- list(type = type, labels = d$variants$labels, k = d$variants$k, control = d$variants$control, n = n,
              reference = if (type == "continuous") "welch" else "z", cuped = NULL)
  if (type == "binary") {
    conv <- unname(t[, "sum_y"])
    p <- conv / n
    out$m <- p
    out$v <- p * (1 - p) / n
    out$sd <- sqrt(p * (1 - p))
    out$conversions <- conv
    out$s2 <- n * p * (1 - p) / (n - 1)  # the sample variance of 0/1 values, from the exact counts
  } else if (type == "continuous") {
    out$m <- unname(ab_mean(d, "y"))
    out$s2 <- .abc_floor(unname(ab_variance(d, "y")), d$totals, "ss_yy")
    out$v <- out$s2 / n
    out$sd <- sqrt(out$s2)
    if ("s3_yyy" %in% colnames(t)) out$skewness <- .abc_skewness(d)
  } else {
    sx <- unname(t[, "sum_x"])
    r <- unname(t[, "sum_y"]) / sx
    xbar <- sx / n
    s2l <- unname(ab_variance(d, "y") - 2 * r * ab_variance(d, "y", "x") + r^2 * ab_variance(d, "x")) / xbar^2
    out$m <- r
    out$s2 <- s2l
    out$v <- s2l / n
    out$sd <- sqrt(s2l)
    out$numerator_sum <- unname(t[, "sum_y"])
    out$denominator_sum <- sx
    out$xbar <- xbar
  }
  if (!is.null(d$covariate)) out <- .abc_cuped(d, out)
  out
}

# A variance from shifted sums, Σ(y − c)² − (Σ(y − c))²/n: where every value of a variant is the same, the two
# terms are equal and their difference is the summation's rounding (a few ulps of Σ(y − c)²), not a spread of
# the data; below that bound the variance is 0, so that a constant metric is a variance of 0 (variance_zero).
.abc_floor <- function(s2, t, col) {
  n <- t[, "n"]
  noise <- 4 * n * .Machine$double.eps * t[, col] / pmax(n - 1, 1)
  s2[s2 <= noise] <- 0
  s2
}

# The skewness of a continuous metric per variant, m₃ / m₂^(3/2) with the moments over n (scipy's skew with
# bias = True), from the shifted sums: a = y − shift, Σ(a − ā)³ = Σa³ − 3ā Σa² + 2nā³.
.abc_skewness <- function(d) {
  t <- d$totals
  n <- t[, "n"]
  a1 <- (t[, "sum_y"] - n * d$shift[["y"]]) / n
  m2 <- (t[, "ss_yy"] - n * a1^2) / n
  m3 <- (t[, "s3_yyy"] - 3 * a1 * t[, "ss_yy"] + 2 * n * a1^3) / n
  out <- unname(m3 / m2^1.5)
  out[!(m2 > 0)] <- NA_real_
  out
}

# CUPED (Deng, Xu, Kohavi, Walker 2013): θ̂ pooled within the variants, on the units that have a covariate of
# their own (a filled value is the mean of its variant: it carries no covariance), so that a treatment effect
# never leaks into θ̂; with it the adjusted difference of two variants is the coefficient of the variant in
# `y ~ variant + x`. A ratio metric is adjusted through its linearized form L = (Y − R̂_j X) / X̄_j (Deng et al.
# 2018, §4). The variance is the sample variance of y − θ̂x within each variant (unequal variances allowed).
.abc_cuped <- function(d, est) {
  n <- est$n
  own <- !is.null(d$totals_own)
  scc_own <- .abc_s(d, "c", own = own)
  if (est$type == "ratio") {
    r <- est$m
    xb <- est$xbar
    syc_own <- (.abc_s(d, "y", "c", own = own) - r * .abc_s(d, "x", "c", own = own)) / xb
    syy_own <- (.abc_s(d, "y", own = own) - 2 * r * .abc_s(d, "y", "x", own = own) + r^2 * .abc_s(d, "x", own = own)) / xb^2
    s_yc <- (ab_variance(d, "y", "c") - r * ab_variance(d, "x", "c")) / xb
  } else {
    syc_own <- .abc_s(d, "y", "c", own = own)
    syy_own <- .abc_s(d, "y", own = own)
    s_yc <- ab_variance(d, "y", "c")
  }
  s_cc <- ab_variance(d, "c")
  den <- sum(scc_own)
  theta <- if (den > 0) sum(syc_own) / den else 0
  rho <- if (den > 0 && sum(syy_own) > 0) sum(syc_own) / sqrt(sum(syy_own) * den) else NA_real_
  cbar_j <- unname(ab_mean(d, "c"))
  cbar <- sum(d$totals[, "sum_c"]) / sum(n)
  s2_adj <- unname(est$s2 - 2 * theta * s_yc + theta^2 * s_cc)
  s2_adj[s2_adj < 0] <- 0
  raw <- list(m = est$m, v = est$v, s2 = est$s2, sd = est$sd)
  est$m <- est$m - theta * (cbar_j - cbar)
  est$s2 <- s2_adj
  est$v <- s2_adj / n
  # a variant's own adjusted mean also carries x̄, the covariate's mean over all the units, estimated too: its
  # variance is v_j + θ̂² s²_x / N (s²_x over all the units, filled values included, as x̄ is). The term is the same
  # for every variant and cancels in a difference: the comparisons, the Bayesian measures and the power keep v_j;
  # only a variant's own interval (the variants' table, the `variants` output) reads v_level.
  nn <- sum(n)
  s2c_all <- if (nn > 1) (sum(.abc_s(d, "c")) + sum(n * (cbar_j - cbar)^2)) / (nn - 1) else 0
  est$v_level <- est$v + theta^2 * s2c_all / nn
  est$sd <- sqrt(s2_adj)
  est$reference <- "z"
  cov <- d$covariate
  est$cuped <- list(theta = theta, rho = rho, raw = raw, covariate_mean = cbar_j, covariate_mean_all = cbar,
                    reduction = ifelse(raw$s2 > 0, 1 - s2_adj / raw$s2, NA_real_),
                    reduction_all = if (sum(raw$v) > 0) 1 - sum(est$v) / sum(raw$v) else NA_real_,
                    mode = cov$mode, n_filled = cov$n_filled, n_excluded = cov$n_excluded, fill_value = cov$fill_value,
                    n_given = cov$n_given, n_empty = cov$n_empty)
  est
}

# ── Fieller's interval of m_t / m_c − 1 (Fieller 1954) ───────────────────────────
# The ratios ρ with (m_t − ρ m_c)² ≤ q² (v_t + ρ² v_c): with g = q² v_c / m_c² < 1 they are
# [r ∓ (q / |m_c|) √(v_t (1 − g) + r² v_c)] / (1 − g), r = m_t / m_c; g ≥ 1: the control's mean is not told apart
# from 0, the set is not a bounded interval (both ends NULL). At ρ = 1 the inequality is the difference's test
# |m_t − m_c| / √(v_t + v_c) ≤ q, so the interval leaves out a lift of 0 exactly when the test rejects. `side`:
# «both», «low» (only the lower end), «high» (only the upper end), for a one-sided alternative.
ab_fieller <- function(mt, vt, mc, vc, q, side = "both") {
  if (!is.finite(mc) || mc == 0 || !is.finite(q)) return(list(low = NA_real_, high = NA_real_, bounded = FALSE))
  g <- q^2 * vc / mc^2
  if (!(g < 1)) return(list(low = NA_real_, high = NA_real_, bounded = FALSE))
  r <- mt / mc
  half <- (q / abs(mc)) * sqrt(vt * (1 - g) + r^2 * vc)
  low <- (r - half) / (1 - g) - 1
  high <- (r + half) / (1 - g) - 1
  if (side == "low") high <- NA_real_
  if (side == "high") low <- NA_real_
  list(low = low, high = high, bounded = TRUE)
}

# ── the comparisons ──────────────────────────────────────────────────────────────
# ctx: alpha, alternative, correction, higher_is_better, min_lift, power.
ab_compare <- function(est, ctx) {
  ci <- est$control
  tr <- setdiff(seq_len(est$k), ci)
  m <- length(tr)
  alpha <- ctx$alpha
  alt <- ctx$alternative
  mc <- est$m[[ci]]
  vc <- est$v[[ci]]
  # the family: the comparisons that can be computed (a standard error above 0), as p.adjust counts the p-values
  # that are not NA; with every treatment computed it is m
  m_family <- max(1L, sum(vapply(tr, function(t) { se <- sqrt(est$v[[t]] + vc); is.finite(se) && se > 0 }, TRUE)))
  # the intervals: simultaneous at 1 − alpha/m with Bonferroni, at 1 − alpha each otherwise
  a_int <- if (identical(ctx$correction, "bonferroni")) alpha / m_family else alpha
  rows <- lapply(tr, function(t) {
    mt <- est$m[[t]]
    vt <- est$v[[t]]
    d <- mt - mc
    se <- sqrt(vt + vc)
    df <- if (est$reference == "welch") (vt + vc)^2 / (vt^2 / (est$n[[t]] - 1) + vc^2 / (est$n[[ci]] - 1)) else Inf
    ok <- is.finite(se) && se > 0
    stat <- if (ok) d / se else NA_real_
    p <- if (!ok) NA_real_ else switch(alt, two_sided = 2 * stats::pt(-abs(stat), df), greater = stats::pt(stat, df, lower.tail = FALSE),
                                       less = stats::pt(stat, df))
    q <- if (!ok) NA_real_ else if (alt == "two_sided") stats::qt(1 - a_int / 2, df) else stats::qt(1 - a_int, df)
    d_low <- if (alt == "less" || !ok) NA_real_ else d - q * se
    d_high <- if (alt == "greater" || !ok) NA_real_ else d + q * se
    lift <- if (mc != 0) mt / mc - 1 else NA_real_
    lift_se <- if (mc != 0) sqrt(vt / mc^2 + mt^2 * vc / mc^4) else NA_real_
    # one side: the end of the lift that bounds it on the test's side (a negative control's mean turns the lift
    # the other way round from the difference)
    side <- if (alt == "two_sided") "both" else if ((alt == "greater") == (mc > 0)) "low" else "high"
    f <- if (ok) ab_fieller(mt, vt, mc, vc, q, side) else list(low = NA_real_, high = NA_real_, bounded = mc != 0)
    list(t = t, difference = d, std_error = se, df = if (is.finite(df)) df else NA_real_, statistic = stat, p = p, q = q,
         difference_low = d_low, difference_high = d_high, lift = lift, lift_std_error = lift_se, lift_low = f$low,
         lift_high = f$high, fieller_bounded = f$bounded, computed = ok)
  })
  p <- vapply(rows, `[[`, 0, "p")
  p_adj <- stats::p.adjust(p, method = AB_CORRECTIONS[[ctx$correction]])
  # the level of each comparison in the power at this size: α/(arms − 1) with a correction, as the plan's
  a_cmp <- if (identical(ctx$correction, "none")) alpha else alpha / m_family
  z_a <- if (alt == "two_sided") stats::qnorm(1 - a_cmp / 2) else stats::qnorm(1 - a_cmp)
  z_b <- stats::qnorm(ctx$power)
  for (i in seq_along(rows)) {
    r <- rows[[i]]
    r$p_adjusted <- p_adj[[i]]
    up <- r$difference > 0
    r$decision <- if (is.na(r$p_adjusted)) "not_computed" else if (r$p_adjusted < alpha) {
      if (up == isTRUE(ctx$higher_is_better)) "better" else "worse"
    } else "not_significant"
    # the minimum lift: a business margin, read against the lift's interval (a statement, no verdict)
    goal <- if (isTRUE(ctx$higher_is_better)) ctx$min_lift else -ctx$min_lift
    lo <- if (is.na(r$lift_low)) -Inf else r$lift_low
    hi <- if (is.na(r$lift_high)) Inf else r$lift_high
    r$min_lift_position <- if (is.na(r$lift) || (is.na(r$lift_low) && is.na(r$lift_high))) NA_character_ else
      if (lo > goal) "above" else if (hi < goal) "below" else "straddles"
    # the power at this size: the lift detectable with `power`, and the power at the minimum lift (measures)
    if (r$computed && mc != 0) {
      r$mde <- (z_a + z_b) * r$std_error / abs(mc)
      delta <- ctx$min_lift * abs(mc) / r$std_error
      r$power_at_min_lift <- if (alt == "two_sided") stats::pnorm(delta - z_a) + stats::pnorm(-delta - z_a) else stats::pnorm(delta - z_a)
    } else {
      r$mde <- NA_real_
      r$power_at_min_lift <- NA_real_
    }
    rows[[i]] <- r
  }
  list(rows = rows, treatments = tr, m = m, m_family = m_family, alpha_interval = a_int, alpha_comparison = a_cmp, z_a = z_a, z_b = z_b,
       conf = 1 - a_int)
}

# Power against the lift, for one comparison (the curve `power`): the same normal approximation as the MDE.
ab_power_at <- function(lift, se, mc, z_a, two_sided = TRUE) {
  delta <- lift * abs(mc) / se
  if (two_sided) stats::pnorm(delta - z_a) + stats::pnorm(-delta - z_a) else stats::pnorm(delta - z_a)
}

# ── the other checks ─────────────────────────────────────────────────────────────
# Welch's one-way F (Welch 1951) from the groups' means, variances (n − 1) and counts: with two groups it is
# Welch's t squared, the same p-value. NULL when a group has fewer than 2 units or no variance.
ab_welch_anova <- function(mean, var, n) {
  k <- length(mean)
  if (k < 2L || any(n < 2) || any(!(var > 0))) return(NULL)
  w <- n / var
  sw <- sum(w)
  centre <- sum(w * mean) / sw
  a <- sum(w * (mean - centre)^2) / (k - 1)
  h <- sum((1 - w / sw)^2 / (n - 1))
  b <- 1 + 2 * (k - 2) / (k^2 - 1) * h
  f <- a / b
  df2 <- (k^2 - 1) / (3 * h)
  list(statistic = f, df = k - 1, df_2 = df2, p_value = stats::pf(f, k - 1, df2, lower.tail = FALSE))
}

# Pearson's χ² of a 2 × k table (empty, given) by variant, no continuity correction.
ab_missing_chi2 <- function(empty, given) {
  tab <- rbind(empty, given)
  e <- outer(rowSums(tab), colSums(tab)) / sum(tab)
  if (any(!(e > 0))) return(NULL)
  stat <- sum((tab - e)^2 / e)
  df <- length(empty) - 1L
  list(statistic = stat, df = df, p_value = stats::pchisq(stat, df, lower.tail = FALSE))
}
