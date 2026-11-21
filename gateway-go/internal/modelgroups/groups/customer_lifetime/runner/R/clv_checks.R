# The checks of a customer lifetime fit, both nodes (docs/specs/customer-lifetime-models.md,
# «Diagnostics», as the owner decided on 2026-11-20: nothing hardcoded). `clv_checks(rows, fit)`
# writes the section `assumption` for a clv_fit(); clv.R calls it, and bg_bb.R the same.
#
# A verdict comes from two places only.
#   - A test with a real p-value, against the node's `alpha` (p ≥ alpha «ok», p < alpha «failed»,
#     alpha in `threshold`; no «doubtful»): frequency_fit (the χ² of the repeat purchases' classes,
#     pooled from the top until each expects 5 customers, df = classes − 1 − the parameters; «not
#     computed» when no degree of freedom is left) and spend_independence (the slope of the average
#     spend on the frequency, HC3 standard errors; Pearson's r is its `value`).
#   - A certain fact: convergence (the optimizer's code, the guard against a likelihood that is numerical
#     noise, a positive definite Hessian; an estimate at an edge, where the likelihood keeps growing
#     towards the limit, leaves no positive definite Hessian: «doubtful», advice `convergence_edge`),
#     spend_tail (q ≤ 2: the spend's variance is infinite, a mathematical fact; q ≤ 1 stopped the node
#     before).
# Every other check is a measure: a `value` with no verdict and no advice, said by the report in a
# sentence (`measure_texts`): sample_size (the customers, and the repeat customers in `count`),
# holdout (the relative error of the holdout's total), horizon_length (the horizon over the longest
# age) and boundary (an estimate at an edge: its value, and in `advice_id` what such an edge means for
# the model; no row when no estimate is at one). The share gap of the frequency classes is
# frequency_fit's `value`.
#
# An edge is a fact of the likelihood, not a number of ours (clv_edges, as decided in B10): one step
# further towards 0 or ∞ (a factor e on the parameter, alone, or on both parameters of a mixing
# distribution together) the log-likelihood is not lower than at the estimate. No constant decides it.
#
# Light: nothing is refitted; the χ², the holdout and the HC3 test are numbers clv_fit() already has, and
# the edges a dozen evaluations of the likelihood made once by clv_fit().
# Names here start with `clv_check` or `CLV_CHECK`: the files of the group share one environment.

# What each check assumes («H0»); the report says it in the page's language by the check's id.
CLV_CHECK_H0 <- list(
  convergence = "The optimizer found a maximum: it converged and the Hessian is positive definite.",
  frequency_fit = "The distribution of repeat purchases is the model's.",
  spend_independence = "The average spend does not move with the frequency (Gamma-Gamma's assumption).",
  spend_tail = "The spend's variance is finite (q above 2).")

# One check's rows, in the order the report reads them. `verdict` NULL: a measure (no advice).
clv_check_rows <- function(rows, id, verdict = NULL, h0 = CLV_CHECK_H0[[id]], advice = NULL, advice_id = id, level_2 = NULL,
                           statistic = NULL, df = NULL, p = NULL, threshold = NULL, value = NULL, count = NULL) {
  section <- "assumption"
  if (!is.null(verdict) && !is.null(h0)) tab_text(rows, section, "h0", h0, id, level_2)
  if (!is.null(statistic)) tab_number(rows, section, "statistic", statistic, id, level_2)
  if (!is.null(df)) tab_number(rows, section, "df", df, id, level_2)
  if (!is.null(p)) tab_p(rows, section, p, level = id, level_2 = level_2)
  if (!is.null(threshold)) tab_number(rows, section, "threshold", threshold, id, level_2)
  if (!is.null(value)) tab_number(rows, section, "value", value, id, level_2)
  if (!is.null(count)) tab_number(rows, section, "count", count, id, level_2)
  if (!is.null(verdict)) {
    tab_text(rows, section, "verdict", verdict, id, level_2)
    if (!is.null(advice)) {
      tab_text(rows, section, "advice", advice, id, level_2)
      tab_text(rows, section, "advice_id", advice_id, id, level_2)
    }
  }
  invisible(rows)
}

# p against the node's alpha: the only rule that turns a p-value into a verdict.
clv_check_verdict_p <- function(p, alpha) if (is.na(p)) "not_computed" else if (p < alpha) "failed" else "ok"

# The hook: every check of the fit, into `rows`.
clv_checks <- function(rows, fit) {
  clv_check_convergence(rows, fit)
  clv_check_boundary(rows, fit)
  clv_check_sample_size(rows, fit)
  clv_check_frequency_fit(rows, fit)
  if (isTRUE(fit$s$has_holdout)) clv_check_holdout(rows, fit)
  clv_check_horizon(rows, fit)
  if (!is.null(fit$spend)) {
    clv_check_spend_independence(rows, fit)
    clv_check_spend_tail(rows, fit)
  }
  invisible(rows)
}

# ── convergence (a fact) ─────────────────────────────────────────────────────
# Both parts (the purchases, the spend) converged with a positive definite Hessian and no estimate at an edge:
# «ok». The optimizer did not converge, its end point is numerical noise (clv_sound: the fit is `fault`, not
# converged), or the Hessian is not positive definite while no estimate is at an edge: «failed». An estimate
# at an edge, where the likelihood keeps growing towards the limit (clv_edges; the boundary measure says which
# and what it means): «doubtful» (advice_id convergence_edge), whether or not the Hessian there is still
# positive definite to the last digits: the maximum is the limit, not a point, and a standard error there
# means little. Why the edge is not «failed» (B2): customers who never drop out push the dropout's parameter
# to its limit, a legitimate limit of the model, and their fit came out failed. `value`: the Newton step left
# at the purchase model's estimates, in its standard errors.
clv_check_convergence <- function(rows, fit) {
  parts <- Filter(Negate(is.null), list(fit$purchases, fit$spend))
  stopped <- vapply(parts, function(f) !isTRUE(f$converged), logical(1))
  flat <- vapply(parts, function(f) is.null(f$vcov_log), logical(1))
  edge <- vapply(parts, function(f) !is.null(f$edges) && nrow(f$edges) > 0L, logical(1))
  p <- fit$purchases
  step <- NULL
  if (!is.null(p$vcov_log) && !is.null(p$gradient) && all(is.finite(p$gradient))) {
    step <- max(abs(drop(p$vcov_log %*% p$gradient)) / sqrt(pmax(diag(p$vcov_log), 1e-300)))
  }
  if (any(stopped | (flat & !edge))) {
    clv_check_rows(rows, "convergence", "failed", value = step,
                   advice = "the estimates may not be the best ones: the data may hold too few repeat customers for this model; try another purchase model")
  } else if (any(edge)) {
    clv_check_rows(rows, "convergence", "doubtful", advice_id = "convergence_edge",
                   advice = "the optimizer stopped at an edge of a parameter's range, where the likelihood keeps growing towards the limit: the estimate is a limit and its standard error means little; read the estimates at an edge")
  } else {
    clv_check_rows(rows, "convergence", "ok", value = step)
  }
}

# ── boundary (a measure) ─────────────────────────────────────────────────────
# One row per estimate at an edge (clv_edges: `level_2` the parameter, the spend's as spend_p, spend_q,
# spend_gamma): its `value`, and `advice_id` the model's reading of that edge (boundary_no_dropout,
# boundary_homogeneous, boundary), which the report says in a sentence. No verdict: whether the model is
# still right there is not something a number can settle, and the convergence check already says when the
# flat likelihood left no standard errors. No row when no estimate is at an edge.
clv_check_boundary <- function(rows, fit) {
  for (part in Filter(Negate(is.null), list(fit$purchases, fit$spend))) {
    e <- part$edges
    if (is.null(e) || !nrow(e)) next
    spend <- identical(part$model, "gamma_gamma")
    for (name in unique(e$param)) {
      label <- if (spend) paste0("spend_", name) else name
      clv_check_rows(rows, "boundary", level_2 = label, value = part$par[[name]])
      id <- if (spend) "" else clv_check_edge_reading(fit$model, name, e[e$param == name, , drop = FALSE])
      tab_text(rows, "assumption", "advice_id", if (nzchar(id)) id else "boundary", "boundary", label)
    }
  }
  invisible(rows)
}

# What an edge means for the model (`at`: the parameter's rows of clv_edges). The pair of a mixing distribution
# going up together: its spread is not identified (everyone alike: boundary_homogeneous). The dropout's first
# parameter towards 0 or its second towards ∞ (Pareto/NBD s → 0 or β → ∞, BG/NBD a → 0 or b → ∞, BG/BB γ → 0 or
# δ → ∞): the mean dropout goes to 0 (boundary_no_dropout), read first. "" when the edge has no reading.
clv_check_edge_reading <- function(model, name, at) {
  two <- CLV_PAIRS[[model]]$dropout
  towards <- if (!is.null(two) && identical(name, two[[1L]])) "low" else "high"
  if (!is.null(two) && name %in% two && any(at$pair == "" & at$side == towards)) return("boundary_no_dropout")
  if (any(at$pair != "")) return("boundary_homogeneous")
  ""
}

# ── how many customers (a measure) ───────────────────────────────────────────
clv_check_sample_size <- function(rows, fit) {
  clv_check_rows(rows, "sample_size", value = fit$s$n, count = sum(fit$s$x > 0))
}

# ── the repeat purchases' distribution (a test) ──────────────────────────────
# The χ² of clv_frequency_fit(): `value` the largest gap between the observed and expected shares of a
# class, `count` the classes after pooling.
clv_check_frequency_fit <- function(rows, fit) {
  f <- fit$freq
  p <- if (is.finite(f$df) && f$df >= 1) f$p_value else NA_real_
  verdict <- clv_check_verdict_p(p, fit$alpha)
  advice <- if (identical(verdict, "not_computed")) {
    paste0("not computed: ", f$n_classes, " classes of repeat purchases remain once the small ones are pooled, and the ",
           length(fit$purchases$par), " parameters leave no degree of freedom")
  } else {
    "the model misses the distribution of repeat purchases: compare the purchase models (compare = true)"
  }
  clv_check_rows(rows, "frequency_fit", verdict, advice = advice, statistic = if (is.na(p)) NULL else f$statistic,
                 df = if (is.na(p)) NULL else f$df, p = if (is.na(p)) NULL else p, threshold = fit$alpha, value = f$max_share_gap,
                 count = f$n_classes)
}

# ── the holdout (a measure) ──────────────────────────────────────────────────
# The relative error of the holdout's total, (expected − observed)/observed; «not computed» when nothing
# was bought in the holdout (the error has no denominator).
clv_check_holdout <- function(rows, fit) {
  h <- fit$holdout
  if (is.null(h) || !is.finite(h$rel_error)) {
    clv_check_rows(rows, "holdout", "not_computed", h0 = "The total of purchases in the holdout is the model's.",
                   advice = "not computed: no purchase was observed in the holdout", value = NULL)
    return(invisible(rows))
  }
  clv_check_rows(rows, "holdout", value = h$rel_error)
}

# ── the horizon (a measure) ──────────────────────────────────────────────────
# The horizon over the longest age (BG/BB: over the most opportunities): above 1 the far periods are an
# extrapolation of the model's form.
clv_check_horizon <- function(rows, fit) {
  clv_check_rows(rows, "horizon_length", value = fit$plan$horizon / max(fit$s$T))
}

# ── the spend's independence of the frequency (a test) ───────────────────────
clv_check_spend_independence <- function(rows, fit) {
  si <- clv_spend_independence(fit$s$x, fit$s$m)
  verdict <- clv_check_verdict_p(si$p_value, fit$alpha)
  advice <- if (identical(verdict, "not_computed")) {
    "not computed: the repeat customers' frequencies or spends do not vary"
  } else {
    paste0("the average spend moves with the frequency: Gamma-Gamma's CLV is then biased; segment the customers by frequency, ",
           "or read the CLV with care")
  }
  clv_check_rows(rows, "spend_independence", verdict, advice = advice, statistic = if (is.finite(si$statistic)) si$statistic else NULL,
                 p = if (is.finite(si$p_value)) si$p_value else NULL, threshold = fit$alpha, value = if (is.finite(si$r)) si$r else NULL,
                 count = si$n)
}

# ── the spend's tail (a fact) ────────────────────────────────────────────────
# Gamma–Gamma's mean spend has a finite variance only when q > 2 (the variance of ν ~ Gamma(q, γ)'s
# inverse): q ≤ 2 is said, q ≤ 1 stopped the node (spend_no_mean).
clv_check_spend_tail <- function(rows, fit) {
  q <- fit$spend$par[["q"]]
  clv_check_rows(rows, "spend_tail", if (q > 2) "ok" else "doubtful",
                 advice = "the spend varies so much across customers that its average has no finite variance: a few large spenders drive it",
                 threshold = 2, value = q)
}
