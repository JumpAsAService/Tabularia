# The explanation of the report, chosen by the runner (docs/specs/ab-testing.md, «Explaining the method in the
# report», «How: templates selected by the runner, filled by the layout»). No sentence is written here: each
# row of the section `reading` is a template's id with the run's raw numbers and names,
#
#   reading|<block>|<variant or empty>|line_<k>  =  @<id> name=value name=value…
#
# in the notes' format (a space, a comma, a percent inside a value are %20, %2C, %25; a list of values is
# joined with «,»; a number keeps the digits that read back as the same double). The layout says each one
# in the page's language from report_texts (`reading_texts`, `formula_texts`).
#
# One function per block, each returning a list of blocks list(block, level_2, lines): the A/B test's
# decision, comparison, srm, cuped, cap and power here (lot E2), method and formulas from what every part
# that ran lists; the Bayesian, sequential and planning blocks are their lots' (E3, E4, E5), in their own
# files. A block's lines say verdicts with the verdict's words, and measures (the power, the detectable
# lift, the minimum lift) by their numbers only. The ids and the names each one carries are the words' (lot E6,
# report_texts/experiments.json and ab_test.json; the spec's «As built / E6»): a line carries exactly those.

# A value of a line: a number as the shortest text that reads back as the same double, a text with its
# «%», « », «,» encoded; a vector is a list (each item encoded, joined with «,»).
ab_reading_number <- function(x) {
  vapply(x, function(v) {
    if (is.na(v)) return(NA_character_)
    if (v == 0) return("0")
    for (digits in 15:17) {
      s <- sprintf("%.*g", digits, v)
      if (as.numeric(s) == v) break
    }
    s
  }, "", USE.NAMES = FALSE)
}

ab_reading_text <- function(x) {
  x <- enc2utf8(as.character(x))
  x <- gsub("%", "%25", x, fixed = TRUE)
  x <- gsub(" ", "%20", x, fixed = TRUE)
  gsub(",", "%2C", x, fixed = TRUE)
}

# «@id name=value …»: NULL and NA values are left out (the template says what it can without them).
ab_line <- function(id, ...) {
  v <- list(...)
  parts <- character()
  for (name in names(v)) {
    x <- v[[name]]
    if (is.null(x) || !length(x) || all(is.na(x))) next
    said <- if (is.numeric(x)) ab_reading_number(x) else ab_reading_text(x)
    said[is.na(said)] <- ""
    parts <- c(parts, paste0(name, "=", paste(said, collapse = ",")))
  }
  paste0("@", id, if (length(parts)) paste0(" ", paste(parts, collapse = " ")) else "")
}

ab_block <- function(block, lines, level_2 = NULL) list(block = block, level_2 = level_2, lines = lines[!is.na(lines)])

# The rows of the blocks, in the order given.
ab_write_reading <- function(rows, blocks) {
  for (b in blocks) {
    if (is.null(b) || !length(b$lines)) next
    for (k in seq_along(b$lines)) tab_text(rows, "reading", paste0("line_", k), b$lines[[k]], b$block, b$level_2)
  }
  invisible(rows)
}

# ── the blocks of lot E2 ─────────────────────────────────────────────────────────
# r: the run as fit_ab_test holds it: ctx (the parameters), d (ab_read), est (ab_estimates), cmp (ab_compare).

# The treatment a headline's numbers are of: the smallest adjusted p-value among those it names (the report's
# cards show the same one), the largest lift in the better direction between equal ones.
.ab_reading_best <- function(r, among = r$cmp$rows) {
  p <- vapply(among, function(x) if (is.na(x$p_adjusted)) Inf else x$p_adjusted, 0)
  lifts <- vapply(among, function(x) if (is.na(x$lift)) -Inf else if (isTRUE(r$ctx$higher_is_better)) x$lift else -x$lift, 0)
  among[[order(p, -lifts)[[1L]]]]
}

# The decision: the headline's template (better, worse, no difference: the SRM's failure has its own block),
# the counts or the means it rests on, the lift's interval. Several treatments better (or worse): their names
# as a list, the numbers of the one with the smallest adjusted p-value.
ab_reading_decision <- function(r) {
  ctx <- r$ctx
  est <- r$est
  rows <- r$cmp$rows
  ids <- vapply(rows, `[[`, "", "decision")
  kind <- if (any(ids == "better")) "better" else if (any(ids == "worse")) "worse" else "no_difference"
  named <- if (kind == "no_difference") rows else rows[ids == kind]
  best <- .ab_reading_best(r, named)
  control <- est$labels[[est$control]]
  who <- if (kind == "no_difference") est$labels[[best$t]] else est$labels[vapply(named, `[[`, 0L, "t")]
  # a sentence that lacks one of its numbers is not said: no adjusted p (nothing computed), an interval with an end
  # NULL (one-sided, or Fieller's set not bounded)
  head <- if (is.na(best$p_adjusted)) NA_character_ else
    ab_line(paste0("decision_", kind), variant = who, control = control, lift = if (kind != "no_difference") best$lift,
            p_adjusted = best$p_adjusted, alpha = ctx$alpha)
  t <- best$t
  c0 <- est$control
  counts <- if (est$type == "binary") {
    ab_line("decision_counts", variant = est$labels[[t]], units = est$n[[t]], converted = est$conversions[[t]], rate = est$m[[t]],
            control = control, units_control = est$n[[c0]], rate_control = est$m[[c0]])
  } else {
    ab_line("decision_means", variant = est$labels[[t]], units = est$n[[t]], value = est$m[[t]], control = control,
            units_control = est$n[[c0]], value_control = est$m[[c0]])
  }
  interval <- if (is.na(best$lift_low) || is.na(best$lift_high)) NA_character_ else
    ab_line("decision_interval", lift_low = best$lift_low, lift_high = best$lift_high, conf = r$cmp$conf)
  list(ab_block("decision", c(head, counts, interval)))
}

# Each treatment's comparison: significant or not (the adjusted p against alpha: the verdict), then the
# correction: with two treatments or more its step from the p-value to the adjusted one (`correction_none` says
# none was asked for), with one treatment `correction_one`. A comparison not computed has no line.
ab_reading_comparison <- function(r) {
  ctx <- r$ctx
  est <- r$est
  control <- est$labels[[est$control]]
  out <- list()
  for (x in r$cmp$rows) {
    if (is.na(x$p_adjusted)) next
    id <- if (x$p_adjusted < ctx$alpha) "comparison_significant" else "comparison_not_significant"
    lines <- ab_line(id, variant = est$labels[[x$t]], control = control, p = x$p_adjusted, alpha = ctx$alpha)
    lines <- c(lines, if (r$cmp$m > 1L) ab_line(paste0("correction_", ctx$correction), p = x$p, p_adjusted = x$p_adjusted, treatments = r$cmp$m)
                      else ab_line("correction_one"))
    out[[length(out) + 1L]] <- ab_block("comparison", lines, est$labels[[x$t]])
  }
  out
}

# The split of the units: as planned or not (the χ² against srm_alpha: the verdict).
ab_reading_srm <- function(r) {
  s <- r$d$srm
  id <- if (identical(s$verdict, "failed")) "srm_failed" else "srm_ok"
  list(ab_block("srm", ab_line(id, observed = s$share, shares = s$expected_share, p = s$p_value, srm_alpha = s$alpha)))
}

# CUPED: the variance it took away (over the arms' means together: for two arms the difference's), θ̂ and the
# correlation; then the units without the covariate, filled with their own variant's mean (the report's CUPED
# table has each variant's fill value: `covariate_fill_value`, decision 14) or left out.
ab_reading_cuped <- function(r) {
  cu <- r$est$cuped
  if (is.null(cu)) return(list())
  lines <- ab_line("cuped_reduced", covariate = r$ctx$covariate, reduction = cu$reduction_all, theta = cu$theta,
                   correlation = cu$rho)
  if (identical(cu$mode, "exclude") && sum(cu$n_excluded) > 0) {
    lines <- c(lines, ab_line("cuped_excluded", count = sum(cu$n_excluded)))
  } else if (sum(cu$n_filled) > 0) {
    lines <- c(lines, ab_line("cuped_filled", count = sum(cu$n_filled)))
  }
  list(ab_block("cuped", lines))
}

# The cap: the percentile, its value, the units capped, the variants they are in.
ab_reading_cap <- function(r) {
  cap <- r$d$cap
  if (is.null(cap)) return(list())
  j <- which(cap$n_capped > 0)
  list(ab_block("cap", ab_line("cap_applied", percentile = cap$percentile, value = cap$value, count = sum(cap$n_capped),
                               variants = r$est$labels[j])))
}

# The power at this size (a measure): the largest of the treatments' detectable lifts, with the power asked
# and the minimum lift; no verdict.
ab_reading_power <- function(r) {
  mde <- vapply(r$cmp$rows, `[[`, 0, "mde")
  if (all(is.na(mde))) return(list())
  i <- which.max(mde)
  x <- r$cmp$rows[[i]]
  list(ab_block("power", ab_line("power_detectable", mde = x$mde, power = r$ctx$power, min_lift = r$ctx$min_lift)))
}

# ── what was computed, and how: the last page ────────────────────────────────────
# The method's paragraphs and the formula box list the parts that ran, in this order; a part of another lot
# (Bayes, sequential, secondary metrics) adds its own lines (ab_line) through its `method` and `formulas`
# (fit_ab_test, «parts»).
ab_reading_method <- function(r, parts = list()) {
  ids <- c("method_decision", "method_comparison", if (r$est$type == "ratio") "method_ratio", "method_srm", "method_power",
           if (!is.null(r$est$cuped)) "method_cuped", if (!is.null(r$d$cap)) "method_cap")
  lines <- c(vapply(ids, function(id) ab_line(id), "", USE.NAMES = FALSE), unlist(lapply(parts, `[[`, "method")))
  list(ab_block("method", lines))
}

ab_reading_formulas <- function(r, parts = list()) {
  est <- r$est
  ctx <- r$ctx
  test <- if (est$type == "ratio") "formula_delta" else if (est$reference == "welch") "formula_welch" else "formula_z"
  ids <- c(test, "formula_fieller",
           if (!identical(ctx$correction, "none") && r$cmp$m > 1L) {
             c(holm = "formula_holm", benjamini_hochberg = "formula_bh", bonferroni = "formula_bonferroni")[[ctx$correction]]
           },
           "formula_srm", if (!is.null(est$cuped)) "formula_cuped")
  list(ab_block("formulas", c(vapply(ids, function(id) ab_line(id), "", USE.NAMES = FALSE), unlist(lapply(parts, `[[`, "formulas")))))
}
