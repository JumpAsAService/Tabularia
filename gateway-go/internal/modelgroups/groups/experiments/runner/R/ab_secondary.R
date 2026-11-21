# The secondary metrics of an experiment (docs/specs/ab-testing.md, «Secondary metrics», lot E8): the part
# `secondary` of fit_ab_test (ab_test.R's header), ab_part_secondary(r).
#
# `secondary_metrics` names further columns of the units: a true/false column is a binary metric (a rate, the
# unpooled z), a number column a continuous one (a mean, Welch's t); no ratio, no CUPED, no cap (those are the
# primary's). Each gets the comparison of every treatment against the control, by E2's own code (ab_estimates,
# ab_compare) on its own sufficient statistics. The p-values of all of them, every secondary metric × every
# treatment, are ONE family, adjusted together with the node's `correction`; never with the primary's: the
# primary's comparisons are computed before this part runs and nothing here touches them, so adding a guardrail
# costs the primary nothing and the decision stays the primary's alone. With Bonferroni the intervals of the family
# are at 1 − alpha/M (M: the family's comparisons that were computed, as p.adjust counts them), at 1 − alpha with the
# other corrections, as E2's; the detectable lift at alpha/M with a correction (the plan's convention). A guardrail
# is `worse` or `better` only from its adjusted p-value against alpha, in its own direction: `secondary_directions`
# ({column: "higher" | "lower"}, the owner's decision: a latency, returns, cancellations are better lower), the
# primary's `higher_is_better` for a metric it does not name. The direction changes no number, only the word.
#
# The units: the rows are read again (only the variant, the unit and these columns: the light path); a unit is the
# primary's (one row, or the rows of `unit` together, a unit seen in two variants left out); its value of a metric
# is the sum of its rows' values (true if any row is true, for a binary one), over the rows that have one. A unit
# without any value of a metric is left out of that metric's comparisons only (a note says how many); the primary's
# own rules (its NULLs, the covariate left out) do not choose the units of a secondary metric. A metric with fewer
# than 2 units in the control, or in a treatment, is not computed there.

AB_SECONDARY_WORSE_LINES <- 5L  # the guardrails worse than the control said one by one; the rest are counted

ab_part_secondary <- function(r) {
  cols <- as.character(unlist(r$ctx$params$secondary_metrics))
  if (!length(cols) || !identical(r$d$shape, "units")) return(NULL)
  s <- ab_secondary(r, cols)
  list(rows = function(rows) .abx_rows(rows, r, s), reading = ab_reading_secondary(r, s), method = ab_line("method_secondary"),
       formulas = .abx_formulas(r, s), output_rows = .abx_output(r, s), notes = s$notes)
}

# Each secondary metric's units, as the primary's are made: list(name, type, g (the variant of each unit), y,
# missing (units assigned without a value)).
.abx_units <- function(r, cols) {
  task <- r$task
  p <- task$params
  read <- c(list(variant = p$variant), if (!is.null(r$ctx$unit)) list(unit = r$ctx$unit), as.list(cols))
  df <- .ab_input(task, read)
  code <- match(ab_label(df[[p$variant]]), r$est$labels)
  keep <- !is.na(code)
  if (!is.null(r$ctx$unit)) keep <- keep & !is.na(df[[r$ctx$unit]])
  ri <- which(keep)
  metrics <- lapply(cols, function(col) {
    x <- df[[col]]
    y <- .ab_numeric(x, col, "a secondary metric is true/false or a number")[ri]
    list(name = col, type = if (tab_kind(x) == "boolean") "binary" else "continuous", y = y)
  })
  if (is.null(r$ctx$unit)) {
    unit_variant <- code[ri]
    values <- lapply(metrics, function(m) list(y = m$y, has = !is.na(m$y)))
    assigned <- rep(TRUE, length(ri))
  } else {
    sums <- list()
    for (i in seq_along(metrics)) {
      y <- metrics[[i]]$y
      sums[[paste0("y", i)]] <- ifelse(is.na(y), 0, y)
      sums[[paste0("c", i)]] <- as.numeric(!is.na(y))
    }
    cl <- .ab_collapse(.ab_unit_key(df[[r$ctx$unit]][ri]), code[ri], rep(TRUE, length(ri)), sums)
    unit_variant <- cl$variant
    values <- lapply(seq_along(metrics), function(i) list(y = cl$sums[[paste0("y", i)]], has = cl$sums[[paste0("c", i)]] > 0))
    assigned <- !cl$crossover
  }
  lapply(seq_along(metrics), function(i) {
    m <- metrics[[i]]
    has <- values[[i]]$has & assigned
    y <- values[[i]]$y[has]
    if (m$type == "binary") y <- as.numeric(y > 0)
    list(name = m$name, type = m$type, g = unit_variant[has], y = y, missing = sum(assigned & !values[[i]]$has))
  })
}

# One metric's estimates by E2's ab_estimates, from its sums per variant (n, Σy, Σ(y − c)²).
.abx_estimates <- function(r, u) {
  k <- r$est$k
  shift <- if (length(u$y)) mean(u$y) else 0
  tot <- matrix(0, k, 3L, dimnames = list(NULL, c("n", "sum_y", "ss_yy")))
  tot[, "n"] <- tabulate(u$g, k)
  if (length(u$y)) {
    s <- rowsum(cbind(u$y, (u$y - shift)^2), u$g, reorder = TRUE)
    tot[as.integer(rownames(s)), c("sum_y", "ss_yy")] <- s
  }
  dd <- list(totals = tot, shift = c(y = shift), metric_type = u$type, covariate = NULL,
             variants = list(labels = r$est$labels, k = k, control = r$est$control))
  est <- suppressWarnings(ab_estimates(dd))
  few <- est$n < 2
  est$m[few] <- NA_real_
  est$v[few] <- NA_real_
  est
}

# Whether a higher value of the secondary metric `col` is better: its own direction, else the primary's.
.abx_higher_is_better <- function(ctx, col) {
  own <- ctx$params$secondary_directions[[col]]
  if (is.null(own)) isTRUE(ctx$higher_is_better) else identical(own, "higher")
}

ab_secondary <- function(r, cols) {
  ctx <- r$ctx
  units <- .abx_units(r, cols)
  ci <- r$est$control
  tr <- r$cmp$treatments
  # the comparisons of each metric at a level fixed below; first their p-values, which do not depend on it
  first <- lapply(units, function(u) {
    est <- .abx_estimates(r, u)
    list(unit = u, est = est, rows = .abx_compare(est, ctx, ctx$alpha))
  })
  p <- unlist(lapply(first, function(f) vapply(f$rows, `[[`, 0, "p")))
  M <- max(1L, sum(!is.na(p)))
  correction <- ctx$correction
  a_int <- if (identical(correction, "bonferroni")) ctx$alpha / M else ctx$alpha
  a_cmp <- if (identical(correction, "none")) ctx$alpha else ctx$alpha / M
  two <- identical(ctx$alternative, "two_sided")
  z_a <- if (two) stats::qnorm(1 - a_cmp / 2) else stats::qnorm(1 - a_cmp)
  p_adj <- stats::p.adjust(p, method = AB_CORRECTIONS[[correction]])
  out <- list()
  i <- 0L
  metrics <- list()
  for (f in first) {
    rows <- if (identical(a_int, ctx$alpha)) f$rows else .abx_compare(f$est, ctx, a_int)
    mc <- f$est$m[[ci]]
    for (x in rows) {
      i <- i + 1L
      x$metric <- f$unit$name
      x$type <- f$unit$type
      x$higher_is_better <- .abx_higher_is_better(ctx, f$unit$name)
      x$p_adjusted <- p_adj[[i]]
      x$decision <- if (is.na(x$p_adjusted)) "not_computed" else if (x$p_adjusted < ctx$alpha) {
        if ((x$difference > 0) == x$higher_is_better) "better" else "worse"
      } else "not_significant"
      x$mde <- if (x$computed && !is.na(mc) && mc != 0) (z_a + stats::qnorm(ctx$power)) * x$std_error / abs(mc) else NA_real_
      x$mean_treatment <- f$est$m[[x$t]]
      x$mean_control <- mc
      out[[i]] <- x
    }
    metrics[[length(metrics) + 1L]] <- list(name = f$unit$name, type = f$unit$type, est = f$est, missing = f$unit$missing)
  }
  notes <- unlist(lapply(metrics, function(m) if (m$missing > 0) ab_line("secondary_missing", metric = m$name, count = m$missing)))
  list(rows = out, metrics = metrics, comparisons = sum(!is.na(p)), treatments = length(tr), conf = 1 - a_int, notes = notes)
}

# ab_compare at the interval's level `alpha` (correction none: the family is adjusted above), or every comparison
# not computed when the control has fewer than 2 units.
.abx_compare <- function(est, ctx, alpha) {
  ci <- est$control
  if (is.na(est$m[[ci]])) {
    blank <- list(difference = NA_real_, std_error = NA_real_, df = NA_real_, statistic = NA_real_, p = NA_real_, difference_low = NA_real_,
                  difference_high = NA_real_, lift = NA_real_, lift_std_error = NA_real_, lift_low = NA_real_, lift_high = NA_real_,
                  fieller_bounded = FALSE, computed = FALSE)
    return(lapply(setdiff(seq_len(est$k), ci), function(t) c(list(t = t), blank)))
  }
  ab_compare(est, utils::modifyList(ctx, list(alpha = alpha, correction = "none")))$rows
}

AB_SECONDARY_NUMBERS <- c("difference", "difference_std_error", "difference_ci_low", "difference_ci_high", "lift", "lift_std_error",
                          "lift_ci_low", "lift_ci_high", "statistic", "p_value", "p_adjusted", "mde", "mean_treatment", "mean_control")

.abx_rows <- function(rows, r, s) {
  labels <- r$est$labels
  for (x in s$rows) {
    lv <- x$metric
    lv2 <- labels[[x$t]]
    v <- list(difference = x$difference, difference_std_error = x$std_error, difference_ci_low = x$difference_low,
              difference_ci_high = x$difference_high, lift = x$lift, lift_std_error = x$lift_std_error, lift_ci_low = x$lift_low,
              lift_ci_high = x$lift_high, statistic = x$statistic, p_value = x$p, p_adjusted = x$p_adjusted, mde = x$mde,
              mean_treatment = x$mean_treatment, mean_control = x$mean_control)
    for (stat in AB_SECONDARY_NUMBERS) {
      value <- v[[stat]]
      if (is.na(value) || !is.finite(value)) next
      tab_number(rows, "secondary", stat, value, lv, lv2)
      if (stat == "p_value") tab_text(rows, "secondary", "significance", tab_significance(value), lv, lv2)
      if (stat == "p_adjusted") tab_text(rows, "secondary", "p_adjusted_significance", tab_significance(value), lv, lv2)
    }
    tab_text(rows, "secondary", "decision", x$decision, lv, lv2)
    tab_text(rows, "secondary", "metric_type", x$type, lv, lv2)
    tab_text(rows, "secondary", "direction", if (x$higher_is_better) "higher" else "lower", lv, lv2)
  }
  invisible(rows)
}

# The rows of the `variants` output: one per secondary metric and variant (role «secondary»), the comparison's
# numbers on the treatments' rows; the Bayesian and sequential columns NULL (they are the primary's).
.abx_output <- function(r, s) {
  k <- r$est$k
  lapply(s$metrics, function(m) {
    est <- m$est
    iv <- suppressWarnings(.abt_variant_interval(est, r$ctx$alpha))
    na <- function(v) { v <- as.numeric(v); v[!is.finite(v)] <- NA_real_; v }
    out <- list(metric = rep(m$name, k), role = rep("secondary", k), j = seq_len(k), is_control = seq_len(k) == est$control,
                units = est$n, mean = na(est$m), std_error = na(iv$se), ci_low = na(iv$low), ci_high = na(iv$high))
    mine <- Filter(function(x) identical(x$metric, m$name), s$rows)
    of_t <- function(stat, empty = NA_real_) {
      v <- rep(empty, k)
      for (x in mine) v[[x$t]] <- x[[stat]]
      v
    }
    columns <- c(difference = "difference", difference_ci_low = "difference_low", difference_ci_high = "difference_high", lift = "lift",
                 lift_ci_low = "lift_low", lift_ci_high = "lift_high", p_value = "p", p_adjusted = "p_adjusted")
    for (col in names(columns)) out[[col]] <- na(of_t(columns[[col]]))
    out$decision <- of_t("decision", NA_character_)
    out
  })
}

# The formula box: the tests of the secondary metrics' types and their correction, where the primary's lines do not
# already hold them (ab_reading_formulas).
.abx_formulas <- function(r, s) {
  est <- r$est
  primary <- if (est$type == "ratio") "formula_delta" else if (est$reference == "welch") "formula_welch" else "formula_z"
  types <- unique(vapply(s$metrics, `[[`, "", "type"))
  ids <- setdiff(c(if ("binary" %in% types) "formula_z", if ("continuous" %in% types) "formula_welch"), primary)
  correction <- r$ctx$correction
  if (!identical(correction, "none") && s$comparisons > 1L && !(r$cmp$m > 1L)) {
    ids <- c(ids, c(holm = "formula_holm", benjamini_hochberg = "formula_bh", bonferroni = "formula_bonferroni")[[correction]])
  }
  vapply(ids, function(id) ab_line(id), "", USE.NAMES = FALSE)
}

# The reading of the block `secondary` (one block for the whole experiment): how the family was corrected (a fact),
# then the guardrails that came out worse (a verdict: the adjusted p against alpha), the smallest adjusted p first,
# five at most and the count of the others; or that none is worse.
ab_reading_secondary <- function(r, s) {
  M <- s$comparisons
  if (M == 0L) return(list())
  family <- if (M == 1L) ab_line("secondary_family_one") else if (identical(r$ctx$correction, "none")) {
    ab_line("secondary_family_none", comparisons = M)
  } else ab_line("secondary_family", comparisons = M, metrics = length(s$metrics))
  worse <- Filter(function(x) identical(x$decision, "worse"), s$rows)
  lines <- family
  if (length(worse)) {
    worse <- worse[order(vapply(worse, `[[`, 0, "p_adjusted"))]
    for (x in utils::head(worse, AB_SECONDARY_WORSE_LINES)) {
      lines <- c(lines, ab_line("secondary_worse", variant = r$est$labels[[x$t]], metric = x$metric, p_adjusted = x$p_adjusted))
    }
    if (length(worse) > AB_SECONDARY_WORSE_LINES) lines <- c(lines, ab_line("secondary_worse_more", count = length(worse) - AB_SECONDARY_WORSE_LINES))
  } else {
    lines <- c(lines, ab_line("secondary_none_worse", comparisons = M, alpha = r$ctx$alpha))
  }
  list(ab_block("secondary", lines))
}
