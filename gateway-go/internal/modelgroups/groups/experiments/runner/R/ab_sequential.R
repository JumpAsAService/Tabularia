# The sequential test of an experiment (docs/specs/ab-testing.md, «Sequential: always-valid p-values», lot E4): the
# part `sequential` of fit_ab_test (ab_test.R's header), ab_part_sequential(r).
#
# What it means in a node that runs on a snapshot. The node does not run during the experiment and remembers
# nothing from one run to the next: at each Run it reads the data as they are and computes, for each treatment, the
# always-valid p-value at the date of the data, whose guarantee (P(p ever below alpha | no effect) ≤ alpha) holds
# however many times, and whenever, someone looks and decides. With a `date` its curve over the days is a
# RECONSTRUCTION from this snapshot: the units are ordered by the day they entered (a unit's first row), each with
# its metric as it is now, and every look is computed on the cumulated days. It is not what a report would have
# shown on that day (a unit's revenue then was smaller; the cap's value and CUPED's fill values are today's), and
# two runs a week apart may give a later p-value higher than the earlier one: each run is valid for its data.
#
# The statistic: the mixture sequential probability ratio test (Robbins 1970; Johari, Koomen, Pekelis, Walsh 2017,
# 2022: the normal case with the variance plugged in), on the relative lift λ̂ = m_t / m_c − 1 with its delta-method
# variance v = v_t / m_c² + m_t² v_c / m_c⁴ (the comparison's lift_std_error²), and the mixture θ ~ N(0, τ²) of
# lifts under the alternative, τ = `sequential_tau`. For λ̂ ~ N(θ, v), the likelihood ratio of the mixture against
# θ = 0 is
#
#   Λ = ∫ φ((λ̂ − θ)/√v) / φ(λ̂/√v) dN(0, τ²)(θ) = √(v / (v + τ²)) · exp(τ² λ̂² / (2 v (v + τ²)))
#
# which is Johari et al.'s Λ_n = √(V/(V + nτ²)) exp(n²τ²Δ̄²/(2V(V + nτ²))) with v = V/n, written for unequal arms.
# Under no effect Λ is a nonnegative martingale in the looks (the estimate of a difference of two running means
# has Cov(λ̂_s, λ̂_n) = Var(λ̂_n) for s ≤ n, whatever the split of the arms: a Brownian motion in the information
# 1/v), so Ville's inequality gives P(sup Λ ≥ 1/α) ≤ α, and p_n = min(1, min_{s ≤ n} 1/Λ_s) is an always-valid
# p-value. With the variance plugged in, and the lift linearized by the delta method, the guarantee is asymptotic
# (Johari et al. 2022; Waudby-Smith, Arbour, Sinha, Kennedy, Ramdas 2024, time-uniform central limit theory).
# Looking only on the days (not after every unit) takes the minimum over a subset of the looks: valid, at most
# conservative.
#
# Two-sided only. The mixture is symmetric, so Λ grows with λ̂² and its p-value is two-sided. A one-sided
# always-valid p-value would need another mixture (one-sided, e.g. a half-normal on θ > 0); halving the two-sided
# one is not valid under optional stopping: a path may come near the bound on both sides, the two events are not
# disjoint as they are for a single look, and Ville's bound does not split in two. Hence the catalogue's
# `sequential_two_sided` (`greater` and `less` forbidden with `sequential`), refused here too.
#
# The confidence sequence of the lift at 1 − alpha (alpha/m with Bonferroni, as the comparisons' intervals): the
# lifts θ that the mixture centred on θ does not reject, λ̂ ± √(v (v + τ²)/τ² · (2 log(1/alpha) + log((v + τ²)/v))),
# intersected over the looks. Its ends leave out a lift of 0 exactly when the always-valid p is at most alpha.
#
# The looks: the days of `date` (E1's axis: calendar days from the first to the last; beyond `max_looks` equal
# periods of days, with the note looks_grouped), without `date` one look, the whole snapshot. A look's estimates
# are ab_estimates (E2) on the variant × day sums cumulated over the days: CUPED's θ̂ and the ratio's delta variance
# are recomputed on each look's sums. A look is computed when every variant has at least 2 units (with CUPED, 2 with
# a covariate of their own); a treatment, when the control's estimate is not 0 and the lift's variance is above 0.
#
# The verdict: the always-valid p-values of the treatments at each look, adjusted with `correction` (each is a
# valid p-value at any stopping time, so Holm and BH apply), against `alpha`; the adjusted ones never increase
# along the looks (p.adjust is monotone in each p-value, and each p-value is a running minimum). first_crossing:
# the last day of the first look whose adjusted p fell below alpha. The decision's direction is the side of 0 the
# confidence sequence lies on. No threshold of its own, no random number.

AB_SEQ_DECISION <- c("better", "worse", "not_significant", "not_computed")

ab_part_sequential <- function(r) {
  p <- r$ctx$params
  if (!isTRUE(as.logical(p$sequential %||% FALSE))) return(NULL)
  if (!identical(r$ctx$alternative, "two_sided")) {
    tab_refuse("sequential_two_sided", "the always-valid p-value of this node is two-sided")
  }
  s <- ab_sequential(r, as.numeric(p$sequential_tau %||% 0.05))
  list(rows = function(rows) .abs_rows(rows, r, s), reading = ab_reading_sequential(r, s),
       method = ab_line("method_sequential"), formulas = ab_line("formula_mixture"), output = .abs_output(r, s))
}

# The sums of every (variant, look) cumulated over the looks: an array looks × variants × columns.
.abs_cumulate <- function(cells, k, D) {
  if (is.null(cells)) return(NULL)
  s <- cells$sums
  C <- ncol(s)
  arr <- array(0, c(k, D, C))
  for (c in seq_len(C)) arr[cbind(cells$variant, cells$day + 1L, c)] <- s[, c]
  array(apply(arr, c(1L, 3L), cumsum), c(D, k, C), dimnames = list(NULL, NULL, colnames(s)))
}

# The mixture's log Λ and the half-width of the confidence sequence at 1 − a, for a lift λ̂ with variance v.
ab_mixture <- function(lift, v, tau, a) {
  t2 <- tau^2
  list(log_lambda = -0.5 * log1p(t2 / v) + t2 * lift^2 / (2 * v * (v + t2)),
       half = sqrt(v * (v + t2) / t2 * (-2 * log(a) + log1p(t2 / v))))
}

# The day of each look: its period's last day (the data it holds), days since 1970-01-01; NA without a date.
.abs_look_days <- function(days) {
  if (is.na(days$first)) return(NA_real_)
  pmin(days$first + seq_len(days$D) * days$period - 1, days$last)
}

ab_sequential <- function(r, tau) {
  d <- r$d
  est <- r$est
  ctx <- r$ctx
  k <- est$k
  D <- d$days$D
  ci <- est$control
  tr <- r$cmp$treatments
  m <- length(tr)
  a_int <- r$cmp$alpha_interval
  cum <- .abs_cumulate(d$cells, k, D)
  own <- .abs_cumulate(d$own, k, D)
  lift <- v <- mc <- matrix(NA_real_, m, D)
  for (l in seq_len(D)) {
    dl <- d
    dl$totals <- matrix(cum[l, , ], k, dimnames = list(NULL, dimnames(cum)[[3L]]))
    if (any(dl$totals[, "n"] < 2)) next
    if (!is.null(own)) {
      dl$totals_own <- matrix(own[l, , ], k, dimnames = list(NULL, dimnames(own)[[3L]]))
      if (any(dl$totals_own[, "n"] < 2)) next
    }
    e <- ab_estimates(dl)
    c0 <- e$m[[ci]]
    if (!is.finite(c0) || c0 == 0) next
    lv <- e$v[tr] / c0^2 + e$m[tr]^2 * e$v[[ci]] / c0^4
    ok <- is.finite(lv) & lv > 0
    lift[ok, l] <- e$m[tr][ok] / c0 - 1
    v[ok, l] <- lv[ok]
    mc[ok, l] <- c0
  }
  mix <- ab_mixture(lift, v, tau, a_int)
  log_lambda <- matrix(mix$log_lambda, m)
  computed <- !is.na(lift)
  # the running minimum of 1/Λ (a look not computed adds nothing: 1); NA for a treatment never computed
  p <- t(apply(matrix(ifelse(computed, pmin(1, exp(-log_lambda)), 1), m), 1L, cummin))
  if (D == 1L) p <- matrix(p, m)
  p[rowSums(computed) == 0L, ] <- NA_real_
  # the confidence sequence: each look's interval, intersected with the looks before
  low <- t(apply(matrix(ifelse(computed, lift - mix$half, -Inf), m), 1L, cummax))
  high <- t(apply(matrix(ifelse(computed, lift + mix$half, Inf), m), 1L, cummin))
  if (D == 1L) {
    low <- matrix(low, m)
    high <- matrix(high, m)
  }
  empty <- low > high
  low[!is.finite(low) | empty] <- NA_real_
  high[!is.finite(high) | empty] <- NA_real_
  method <- AB_CORRECTIONS[[ctx$correction]]
  p_adj <- matrix(apply(p, 2L, stats::p.adjust, method = method), m)
  days <- .abs_look_days(d$days)
  rows <- lapply(seq_len(m), function(i) {
    pa <- p_adj[i, D]
    hit <- which(p_adj[i, ] < ctx$alpha)
    lo <- low[i, D]
    hi <- high[i, D]
    last <- max(c(0L, which(computed[i, ])))
    up <- if (!is.na(lo) && lo > 0) TRUE else if (!is.na(hi) && hi < 0) FALSE else if (last) lift[i, last] > 0 else NA
    if (!is.na(up) && last && mc[i, last] < 0) up <- !up  # a lift above 0 on a negative control's mean is a lower metric
    decision <- if (is.na(pa)) "not_computed" else if (pa < ctx$alpha) {
      if (isTRUE(up == isTRUE(ctx$higher_is_better))) "better" else "worse"
    } else "not_significant"
    list(t = tr[[i]], p = p[i, D], p_adjusted = pa, log_lambda = log_lambda[i, D], lift = lift[i, D], cs_low = lo, cs_high = hi,
         cs_empty = empty[i, D], first_crossing = if (length(hit)) days[[hit[[1L]]]] else NA_real_, decision = decision)
  })
  list(tau = tau, n_looks = D, days = days, lift = lift, log_lambda = log_lambda, p = p, p_adjusted = p_adj, low = low,
       high = high, computed = computed, rows = rows, treatments = tr)
}

.abs_rows <- function(rows, r, s) {
  est <- r$est
  tab_number(rows, "model", "sequential_tau", s$tau)
  tab_number(rows, "model", "n_looks", s$n_looks)
  for (x in s$rows) {
    lv <- est$labels[[x$t]]
    for (stat in c("p_value", "p_adjusted", "log_lambda", "cs_low", "cs_high", "first_crossing")) {
      value <- x[[if (stat == "p_value") "p" else stat]]
      if (!is.na(value)) tab_number(rows, "sequential", stat, value, lv)
    }
    tab_text(rows, "sequential", "significance", tab_significance(x$p_adjusted), lv)
    tab_text(rows, "sequential", "decision", x$decision, lv)
  }
  if (is.null(r$ctx$date)) return(invisible(rows))
  # the curves over the looks each treatment was computed on: the lift with its confidence sequence, and the
  # always-valid p-value adjusted for the treatments (its first fall below alpha is first_crossing)
  for (i in seq_along(s$rows)) {
    at <- which(s$computed[i, ])
    if (!length(at)) next
    lv <- est$labels[[s$treatments[[i]]]]
    x <- s$days[at]
    tab_points(rows, "curve", "sequential_lift", "y", x = x, y = s$lift[i, at], level_2 = lv)
    tab_points(rows, "curve", "sequential_lift", "low", x = x, y = s$low[i, at], level_2 = lv)
    tab_points(rows, "curve", "sequential_lift", "high", x = x, y = s$high[i, at], level_2 = lv)
    tab_points(rows, "curve", "sequential_p", "y", x = x, y = s$p_adjusted[i, at], level_2 = lv)
  }
  invisible(rows)
}

# The `variants` output's sequential columns, one value per variant (NULL for the control).
.abs_output <- function(r, s) {
  out <- list()
  for (col in c("sequential_p_value", "sequential_p_adjusted", "sequential_lift_low", "sequential_lift_high")) {
    out[[col]] <- rep(NA_real_, r$est$k)
  }
  for (x in s$rows) {
    out$sequential_p_value[[x$t]] <- x$p
    out$sequential_p_adjusted[[x$t]] <- x$p_adjusted
    out$sequential_lift_low[[x$t]] <- x$cs_low
    out$sequential_lift_high[[x$t]] <- x$cs_high
  }
  out
}

# The reading of each treatment (E6's words, «Explaining the method in the report»): which decision to read and the
# always-valid p before the correction, on the data up to the last day (without `date`, on the data of this run: one
# look, no day to name); the first day the corrected one fell below alpha; that the confidence sequence is empty when
# the looks' intervals have no lift in common (a fact: the table then shows no interval); that the curves are
# rebuilt from today's data (only with `date`: without it there is no curve).
ab_reading_sequential <- function(r, s) {
  dated <- !is.null(r$ctx$date)
  out <- list()
  for (x in s$rows) {
    if (is.na(x$p)) next
    lv <- r$est$labels[[x$t]]
    read <- if (dated) ab_line("sequential_read", variant = lv, p = x$p, day = r$d$days$last, alpha = r$ctx$alpha) else
      ab_line("sequential_read_no_day", variant = lv, p = x$p, alpha = r$ctx$alpha)
    lines <- c(read, if (!is.na(x$first_crossing)) ab_line("sequential_crossed", day = x$first_crossing),
               if (isTRUE(x$cs_empty)) ab_line("sequential_empty", variant = lv), if (dated) ab_line("sequential_snapshot"))
    out[[length(out) + 1L]] <- ab_block("sequential", lines, lv)
  }
  out
}
