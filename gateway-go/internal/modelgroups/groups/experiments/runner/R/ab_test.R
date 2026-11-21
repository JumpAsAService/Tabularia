# A/B test (docs/specs/ab-testing.md, «The node ab_test»): fit_ab_test(task).
#
# ab_read (ab_data.R) reduces the rows to sufficient statistics; ab_estimates and ab_compare (ab_compare.R)
# make the estimates and the comparisons; this file writes the sections of «The output» (model, variant,
# comparison, assumption, curve, hist, reading), the decision of the first page and the `variants` output.
# It returns no card: `card = NULL` writes no model.json, so nothing can be registered or predicted.
#
# The other analyses are parts, each in its lot's own file: a function ab_part_<name>(r) for `bayes` (lot E3),
# `sequential` (E4), `secondary` (E8), called here when it exists. It returns NULL when it does not run, or
#
#   list(rows = function(rows) …,           its sections' rows (bayes, sequential, secondary, its curves, its
#                                            numbers of the model section: prior_alpha, loss_threshold, n_looks…)
#        reading = list(ab_block(…), …),     its blocks of the explanation (ab_reading.R)
#        method = c(ab_line(…)), formulas = c(ab_line(…)),   its lines of the last page
#        output = list(<column> = <one value per variant>),  its columns of the `variants` output
#        output_rows = list(list(metric, role, j, <column> = …)), further rows of that output (secondary metrics)
#        notes = c("@id …"))                  its notes
#
# `r`, what every part reads: r$task, r$ctx (the parameters, defaults filled), r$d (ab_read), r$est
# (ab_estimates: m, v, n per variant…), r$cmp (ab_compare: rows per treatment with p, p_adjusted, decision…).

AB_TEST_PARTS <- c("bayes", "sequential", "secondary")

AB_CHECK_H0 <- list(
  srm = "The units are split between the variants as planned (the expected shares).",
  covariate_balance = "The covariate has the same mean in every variant: the variants were alike before the experiment.",
  covariate_missing_balance = "The share of units without a value of the covariate is the same in every variant.",
  crossover = "No unit was seen in more than one variant.",
  variance_zero = "The metric varies between the units of every variant.")

# The node's parameters with the catalogue's defaults for what the task does not say, and the names the
# sentences use.
ab_context <- function(task, d) {
  spec <- tab_model_spec("ab_test")
  p <- spec$defaults
  for (k in names(task$params)) p[k] <- list(task$params[[k]])
  num <- function(x) as.numeric(x %||% NA_real_)
  type <- d$metric_type
  metric <- if (identical(d$shape, "aggregated")) p$metric_sum else if (type == "ratio") p$numerator else p$metric
  list(alpha = num(p$alpha), srm_alpha = num(p$srm_alpha), alternative = p$alternative %||% "two_sided",
       correction = p$correction %||% "holm", higher_is_better = !isFALSE(p$higher_is_better), min_lift = num(p$min_lift),
       power = num(p$power), covariate = if (.ab_given(p$covariate)) p$covariate else NULL, metric_name = metric,
       denominator = if (type == "ratio") p$denominator else NULL, unit = if (.ab_given(p$unit)) p$unit else NULL,
       date = if (.ab_given(p$date)) p$date else NULL, output = p$output %||% "results", params = p)
}

fit_ab_test <- function(task) {
  # the byte compiler cost 0.8 s at the first run of the node (measured: 1.45 s against 0.61 s on 1,000 rows), for
  # code with no loop over the rows; off for the whole node, as in ab_read, back on after it
  jit <- compiler::enableJIT(0L)
  on.exit(compiler::enableJIT(jit), add = TRUE)
  d <- ab_read(task)
  ctx <- ab_context(task, d)
  est <- ab_estimates(d)
  cmp <- ab_compare(est, ctx)
  r <- list(task = task, ctx = ctx, d = d, est = est, cmp = cmp)
  parts <- list()
  for (name in AB_TEST_PARTS) {
    f <- get0(paste0("ab_part_", name), mode = "function")
    part <- if (is.null(f)) NULL else f(r)
    if (!is.null(part)) parts[[name]] <- part
  }
  rows <- tab_rows_start(task)
  .abt_model_rows(rows, r, parts)
  .abt_variant_rows(rows, r)
  .abt_comparison_rows(rows, r)
  .abt_checks(rows, r)
  for (part in parts) if (!is.null(part$rows)) part$rows(rows)
  .abt_curves(rows, r)
  ab_write_reading(rows, ab_test_reading(r, parts))
  output <- if (identical(ctx$output, "variants")) ab_variants_output(r, parts) else NULL
  list(rows = rows, output = output, card = NULL, native = NULL)
}

# The blocks of the explanation, in the report's order.
ab_test_reading <- function(r, parts = list()) {
  of <- function(name) if (is.null(parts[[name]])) list() else parts[[name]]$reading
  c(ab_reading_decision(r), ab_reading_srm(r), ab_reading_comparison(r), ab_reading_cuped(r), ab_reading_cap(r),
    of("bayes"), of("sequential"), ab_reading_power(r), of("secondary"),
    ab_reading_method(r, parts), ab_reading_formulas(r, parts))
}

# ── the headline ─────────────────────────────────────────────────────────────────
# By precedence, from verdicts only: the SRM's failure, a treatment better, one worse, none different.
ab_decision <- function(r) {
  ids <- vapply(r$cmp$rows, `[[`, "", "decision")
  under <- if (any(ids == "better")) "better" else if (any(ids == "worse")) "worse" else "no_difference"
  named <- if (under == "no_difference") integer() else vapply(r$cmp$rows[ids == under], `[[`, 0L, "t")
  list(id = if (identical(r$d$srm$verdict, "failed")) "srm_failed" else under, under = under,
       variants = r$est$labels[named])
}

.abt_formula <- function(r) {
  ctx <- r$ctx
  est <- r$est
  d <- r$d
  per <- if (is.null(ctx$unit)) "row" else paste0("«", ctx$unit, "»")
  what <- if (identical(d$shape, "aggregated")) {
    paste0("Σ «", ctx$metric_name, "» / Σ «", ctx$params$units_count, "» per variant")
  } else if (est$type == "ratio") {
    paste0("Σ «", ctx$metric_name, "» / Σ «", ctx$denominator, "» per variant, per ", per)
  } else {
    paste0("mean of «", ctx$metric_name, "» per ", per)
  }
  paste0(what, " · control «", est$labels[[est$control]], "» · ", ctx$correction,
         if (!is.null(est$cuped)) paste0(" · CUPED on «", ctx$covariate, "»") else "",
         if (!is.null(d$cap)) paste0(" · capped at the ", format(100 * d$cap$percentile, digits = 15), "th percentile") else "")
}

.abt_model_rows <- function(rows, r, parts) {
  ctx <- r$ctx
  d <- r$d
  est <- r$est
  dec <- ab_decision(r)
  sec <- "model"
  tab_text(rows, sec, "formula", .abt_formula(r))
  tab_text(rows, sec, "software", tab_software("arrow"))
  tab_text(rows, sec, "metric_type", est$type)
  tab_text(rows, sec, "input_shape", d$shape)
  tab_text(rows, sec, "control", est$labels[[est$control]])
  tab_text(rows, sec, "alternative", ctx$alternative)
  tab_text(rows, sec, "correction", ctx$correction)
  if (!is.null(est$cuped)) tab_text(rows, sec, "covariate_missing", est$cuped$mode)
  tab_text(rows, sec, "decision", dec$id)
  if (length(dec$variants)) tab_text(rows, sec, "decision_variants", paste(ab_reading_text(dec$variants), collapse = ","))
  numbers <- list(alpha = ctx$alpha, srm_alpha = ctx$srm_alpha, n = sum(d$n), n_assigned = sum(d$n_assigned), n_missing = sum(d$n_missing),
                  n_variants = est$k, n_crossover = if (identical(d$shape, "units")) d$n_crossover else NULL)
  if (!is.null(est$cuped)) {
    numbers <- c(numbers, list(n_covariate_filled = sum(est$cuped$n_filled), n_covariate_excluded = sum(est$cuped$n_excluded),
                               theta = est$cuped$theta, covariate_correlation = est$cuped$rho))
  }
  if (!is.null(d$cap)) numbers <- c(numbers, list(cap_value = d$cap$value, n_capped = sum(d$cap$n_capped)))
  numbers <- c(numbers, list(min_lift = ctx$min_lift, power = ctx$power))
  if (!is.null(ctx$date)) numbers <- c(numbers, list(first_day = d$days$first, last_day = d$days$last))
  for (k in names(numbers)) if (!is.null(numbers[[k]])) tab_number(rows, sec, k, numbers[[k]])
  for (note in .abt_notes(r, parts)) tab_note(rows, note)
}

.abt_notes <- function(r, parts) {
  est <- r$est
  out <- unname(unlist(r$d$notes))
  if (r$cmp$m == 1L && !identical(r$ctx$correction, "none")) out <- c(out, ab_line("one_treatment"))
  if (est$m[[est$control]] == 0) out <- c(out, ab_line("control_zero"))
  for (x in r$cmp$rows) {
    if (x$computed && !x$fieller_bounded && est$m[[est$control]] != 0) out <- c(out, ab_line("fieller_unbounded", variant = est$labels[[x$t]]))
  }
  c(out, unlist(lapply(parts, `[[`, "notes")))
}

# The interval of a variant's own estimate at 1 − alpha: Student's t with n − 1 degrees of freedom for a plain
# continuous mean, the normal otherwise (as its comparison's reference).
.abt_variant_interval <- function(est, alpha) {
  se <- sqrt(est$v)
  q <- if (est$reference == "welch") stats::qt(1 - alpha / 2, est$n - 1) else stats::qnorm(1 - alpha / 2)
  list(low = est$m - q * se, high = est$m + q * se, se = se)
}

.abt_variant_rows <- function(rows, r) {
  d <- r$d
  est <- r$est
  iv <- .abt_variant_interval(est, r$ctx$alpha)
  cu <- est$cuped
  for (j in seq_len(est$k)) {
    v <- list(is_control = as.numeric(j == est$control), n = est$n[[j]], n_assigned = d$n_assigned[[j]], n_missing = d$n_missing[[j]],
              share = d$srm$share[[j]], expected_share = d$srm$expected_share[[j]], mean = est$m[[j]], std_dev = est$sd[[j]],
              std_error = iv$se[[j]], ci_low = iv$low[[j]], ci_high = iv$high[[j]])
    if (!is.null(est$skewness)) v$skewness <- est$skewness[[j]]
    if (!is.null(d$cap)) v$n_capped <- d$cap$n_capped[[j]]
    if (est$type == "binary") v$conversions <- est$conversions[[j]]
    if (est$type == "ratio") v <- c(v, list(numerator_sum = est$numerator_sum[[j]], denominator_sum = est$denominator_sum[[j]]))
    if (!is.null(cu)) {
      v <- c(v, list(mean_unadjusted = cu$raw$m[[j]], std_error_unadjusted = sqrt(cu$raw$v[[j]]), covariate_mean = cu$covariate_mean[[j]],
                     variance_reduction = cu$reduction[[j]], n_covariate_filled = cu$n_filled[[j]], n_covariate_excluded = cu$n_excluded[[j]]))
      if (identical(cu$mode, "fill")) v$covariate_fill_value <- cu$fill_value[[j]]
    }
    for (k in names(v)) tab_number(rows, "variant", k, v[[k]], level = est$labels[[j]])
  }
}

.abt_comparison_rows <- function(rows, r) {
  est <- r$est
  control <- est$labels[[est$control]]
  for (x in r$cmp$rows) {
    lv <- est$labels[[x$t]]
    num <- function(stat, value) tab_number(rows, "comparison", stat, value, lv, control)
    num("difference", x$difference)
    num("difference_std_error", x$std_error)
    num("difference_ci_low", x$difference_low)
    num("difference_ci_high", x$difference_high)
    num("lift", x$lift)
    num("lift_std_error", x$lift_std_error)
    num("lift_ci_low", x$lift_low)
    num("lift_ci_high", x$lift_high)
    num("statistic", x$statistic)
    if (est$reference == "welch") num("df", x$df)
    tab_p(rows, "comparison", x$p, level = lv, level_2 = control)
    num("p_adjusted", x$p_adjusted)
    if (!is.na(x$p_adjusted)) tab_text(rows, "comparison", "p_adjusted_significance", tab_significance(x$p_adjusted), lv, control)
    num("mde", x$mde)
    num("power_at_min_lift", x$power_at_min_lift)
    tab_text(rows, "comparison", "decision", x$decision, lv, control)
    tab_text(rows, "comparison", "min_lift_position", x$min_lift_position, lv, control)
  }
}

# ── the checks ───────────────────────────────────────────────────────────────────
# A verdict from a test (srm against srm_alpha; covariate_balance and covariate_missing_balance against alpha),
# or a fact (crossover, variance_zero). The advice says what to do about a check that is not ok: an ok row has
# none; its id is «<check>_failed».
.abt_check <- function(rows, id, verdict, advice = NULL, statistic = NULL, df = NULL, df_2 = NULL, p = NULL, threshold = NULL,
                       value = NULL, count = NULL) {
  sec <- "assumption"
  tab_text(rows, sec, "h0", AB_CHECK_H0[[id]], id)
  if (!is.null(statistic)) tab_number(rows, sec, "statistic", statistic, id)
  if (!is.null(df)) tab_number(rows, sec, "df", df, id)
  if (!is.null(df_2)) tab_number(rows, sec, "df_2", df_2, id)
  if (!is.null(p)) tab_p(rows, sec, p, level = id)
  if (!is.null(threshold)) tab_number(rows, sec, "threshold", threshold, id)
  if (!is.null(value)) tab_number(rows, sec, "value", value, id)
  if (!is.null(count)) tab_number(rows, sec, "count", count, id)
  tab_text(rows, sec, "verdict", verdict, id)
  if (!is.null(advice) && !identical(verdict, "ok")) {
    tab_text(rows, sec, "advice", advice, id)
    tab_text(rows, sec, "advice_id", paste0(id, "_failed"), id)
  }
}

.abt_verdict <- function(p, alpha) if (is.na(p)) "not_computed" else if (p < alpha) "failed" else "ok"

ab_test_checks <- function(r) {
  d <- r$d
  est <- r$est
  out <- list()
  s <- d$srm
  out$srm <- list(verdict = s$verdict, statistic = s$statistic, df = s$df, p = s$p_value, threshold = s$alpha,
                  advice = "the units are not split as planned: the comparisons may be biased; look for what removed units from one variant (a filter, a bot rule, a redirect that fails) before reading them")
  if (!is.null(est$cuped)) {
    own <- !is.null(d$totals_own)
    t <- if (own) d$totals_own else d$totals
    w <- ab_welch_anova(unname(ab_mean(d, "c", own = own)), unname(ab_variance(d, "c", own = own)), unname(t[, "n"]))
    out$covariate_balance <- if (is.null(w)) list(verdict = "not_computed") else
      list(verdict = .abt_verdict(w$p_value, r$ctx$alpha), statistic = w$statistic, df = w$df, df_2 = w$df_2, p = w$p_value,
           threshold = r$ctx$alpha,
           advice = paste0("the variants differed in «", r$ctx$covariate, "» before the experiment: check that it was measured before the assignment, and how the units were assigned"))
    cu <- est$cuped
    if (sum(cu$n_empty) > 0) {
      x <- ab_missing_chi2(cu$n_empty, cu$n_given)
      out$covariate_missing_balance <- if (is.null(x)) list(verdict = "not_computed") else
        list(verdict = .abt_verdict(x$p_value, r$ctx$alpha), statistic = x$statistic, df = x$df, p = x$p_value, threshold = r$ctx$alpha,
             count = sum(cu$n_empty),
             advice = paste0("the variants differ in their units without a value of «", r$ctx$covariate, "»: ",
                             if (identical(cu$mode, "exclude")) "leaving them out biases the comparison; fill them, or find why they differ" else
                               "find why they differ (a new kind of unit in one variant)"))
    }
  }
  if (identical(d$shape, "units") && !is.null(r$ctx$unit)) {
    out$crossover <- list(verdict = if (d$n_crossover > 0) "failed" else "ok", count = d$n_crossover,
                          advice = paste0(d$n_crossover, if (d$n_crossover == 1) " unit was" else " units were",
                                          " seen in more than one variant and left out: a unit should keep its variant; check the assignment"))
  }
  s2 <- if (is.null(est$cuped)) est$s2 else est$cuped$raw$s2
  zero <- which(!(s2 > 0))
  out$variance_zero <- list(verdict = if (length(zero)) "failed" else "ok", value = length(zero),
                            advice = paste0("the metric has the same value on every unit of ", paste0("«", est$labels[zero], "»", collapse = ", "),
                                            ": a comparison where both sides have no variance is not computed"))
  out
}

.abt_checks <- function(rows, r) {
  checks <- ab_test_checks(r)
  for (id in names(checks)) do.call(.abt_check, c(list(rows = rows, id = id), checks[[id]]))
}

# ── the charts ───────────────────────────────────────────────────────────────────
.abt_curves <- function(rows, r) {
  d <- r$d
  est <- r$est
  if (!is.null(r$ctx$date)) {
    day <- d$days$first + (seq_len(d$days$D) - 1) * d$days$period
    share <- ab_srm_daily(d)
    for (j in seq_len(est$k)) tab_points(rows, "curve", "srm_share", "y", x = day, y = share[j, ], level_2 = est$labels[[j]])
    for (j in seq_len(est$k)) tab_points(rows, "curve", "daily_units", "y", x = day, y = d$daily_units[j, ], level_2 = est$labels[[j]])
  }
  # the power against the lift, per treatment: one axis for all, from 0 to twice the largest of the detectable
  # lifts and the minimum lift
  mde <- vapply(r$cmp$rows, `[[`, 0, "mde")
  if (any(!is.na(mde))) {
    top <- 2 * max(c(mde, r$ctx$min_lift), na.rm = TRUE)
    lifts <- seq(0, top, length.out = tab_limit(r$task, "power_points"))
    mc <- est$m[[est$control]]
    for (x in r$cmp$rows) {
      if (is.na(x$mde)) next
      tab_points(rows, "curve", "power", "y", x = lifts,
                 y = ab_power_at(lifts, x$std_error, mc, r$cmp$z_a, identical(r$ctx$alternative, "two_sided")), level_2 = est$labels[[x$t]])
    }
  }
  h <- d$hist
  if (!is.null(h)) {
    b <- h$breaks
    nb <- length(b)
    x0 <- c(NA_real_, b)
    x1 <- c(b, NA_real_)
    for (j in seq_len(est$k)) tab_points(rows, "hist", "metric", "count", x = x0, y = h$counts[j, ], level_2 = est$labels[[j]], x1 = x1)
  }
}

# ── the `variants` output ────────────────────────────────────────────────────────
# One row per variant of the primary metric (and the rows a part adds: the secondary metrics), every column of
# OUTPUT_TYPES["variants"] always there, NULL where an analysis is off or a row is the control. `variant` keeps
# the input column's own type (ab_variant_array).
AB_VARIANTS_FLOATS_1 <- c("share", "expected_share", "mean", "std_error", "ci_low", "ci_high", "difference", "difference_ci_low",
                          "difference_ci_high", "lift", "lift_ci_low", "lift_ci_high", "p_value", "p_adjusted")
AB_VARIANTS_FLOATS_2 <- c("prob_beat_control", "prob_best", "expected_loss", "sequential_p_value", "sequential_p_adjusted",
                          "sequential_lift_low", "sequential_lift_high")

ab_variants_output <- function(r, parts = list()) {
  est <- r$est
  k <- est$k
  iv <- .abt_variant_interval(est, r$ctx$alpha)
  of_t <- function(stat) {
    v <- rep(NA_real_, k)
    for (x in r$cmp$rows) v[[x$t]] <- x[[stat]]
    v
  }
  primary <- list(metric = rep(r$ctx$metric_name, k), role = rep("primary", k), j = seq_len(k), is_control = seq_len(k) == est$control,
                  units = est$n, share = r$d$srm$share, expected_share = r$d$srm$expected_share, mean = est$m, std_error = iv$se,
                  ci_low = iv$low, ci_high = iv$high, difference = of_t("difference"), difference_ci_low = of_t("difference_low"),
                  difference_ci_high = of_t("difference_high"), lift = of_t("lift"), lift_ci_low = of_t("lift_low"),
                  lift_ci_high = of_t("lift_high"), p_value = of_t("p"), p_adjusted = of_t("p_adjusted"),
                  decision = { v <- rep(NA_character_, k); for (x in r$cmp$rows) v[[x$t]] <- x$decision; v })
  for (part in parts) for (col in names(part$output)) primary[[col]] <- part$output[[col]]
  blocks <- c(list(primary), unlist(lapply(parts, `[[`, "output_rows"), recursive = FALSE))
  col <- function(name, fill) unlist(lapply(blocks, function(b) {
    n <- length(b$j)
    if (is.null(b[[name]])) rep(fill, n) else b[[name]]
  }), use.names = FALSE)
  j <- col("j", NA_integer_)
  variant <- ab_variant_array(r$d)$Take(as.integer(j) - 1L)
  cols <- list(metric = arrow::Array$create(col("metric", NA_character_), type = arrow::utf8()),
               role = arrow::Array$create(col("role", NA_character_), type = arrow::utf8()),
               variant = variant,
               is_control = arrow::Array$create(as.logical(col("is_control", NA)), type = arrow::boolean()),
               units = arrow::Array$create(as.numeric(col("units", NA_real_)), type = arrow::int64()))
  for (name in AB_VARIANTS_FLOATS_1) cols[[name]] <- arrow::Array$create(as.numeric(col(name, NA_real_)), type = arrow::float64())
  cols$decision <- arrow::Array$create(as.character(col("decision", NA_character_)), type = arrow::utf8())
  for (name in AB_VARIANTS_FLOATS_2) cols[[name]] <- arrow::Array$create(as.numeric(col(name, NA_real_)), type = arrow::float64())
  do.call(arrow::Table$create, cols)
}
