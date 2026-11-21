# The self-test of the planning node (docs/specs/ab-testing.md, lot E5), added to TAB_COMMON_TESTS: the sizes against
# the textbook formula and the 16 s^2 / D^2 rule, the plan against the A/B node's own minimum detectable effect, the
# two directions against each other, CUPED, the grid and its output, the refusals. The reference cases of
# expected.json (values computed by hand from the raw rows) are run by the common self-test.

.abp_frame <- function(n = 600, seed = 3) {
  df <- .abc_table(seed, n)
  df$day <- as.Date("2026-03-02") + (seq_len(n) %% 14)
  df
}

.abp_fit_task <- function(params, frame) {
  list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = "ab_plan",
       variant = params$metric_type, params = params, output = "results", write_output = FALSE, rows_from = "run", limits = tab_catalog()$limits,
       .frame = frame)
}

.abp_run <- function(params, frame) {
  out <- fit_ab_plan(.abp_fit_task(params, frame))
  problems <- tab_check_declared(out$rows, "ab_plan", params$metric_type)
  .tab_expect(!length(problems), "the rows do not match the catalogue: ", paste(head(problems, 3L), collapse = "; "))
  f <- tab_results_frame(out$rows)
  list(value = function(key) f$value[match(key, f$key)], text = function(key) f$text[match(key, f$key)], frame = f, output = out$output)
}

TAB_COMMON_TESTS[["experiments: the plan's sizes are the textbook formula, and two equal arms need 16 s^2 / D^2 units each (within 2 %)"]] <- function(code) {
  df <- .abp_frame()
  for (alpha in c(0.05, 0.01)) {
    r <- .abp_run(list(metric_type = "continuous", metric = "rev", min_lift = 0.04, alpha = alpha, power = 0.8), df)
    m0 <- mean(df$rev)
    s0 <- stats::sd(df$rev)
    n <- 2 * (stats::qnorm(1 - alpha / 2) + stats::qnorm(0.8))^2 * s0^2 / (0.04 * m0)^2
    .tab_expect(r$value("model|||n_per_variant") == ceiling(n - 1e-9) && r$value("model|||n_control") == r$value("model|||n_per_variant"), "n at alpha ", alpha)
    .tab_expect(r$value("model|||n_total") == 2 * r$value("model|||n_per_variant"), "the total")
    if (alpha == 0.05) {
      rule <- 16 * s0^2 / (0.04 * m0)^2
      .tab_expect(abs(r$value("model|||n_per_variant") / rule - 1) < 0.02, "the 16 s^2 / D^2 rule: ", r$value("model|||n_per_variant"), " against ", rule)
    }
  }
  # the shares: the control's units are its share of the whole, n_c / n_t = w_c / w_t
  r <- .abp_run(list(metric_type = "continuous", metric = "rev", min_lift = 0.04, arms = 4, control_share = 0.4, correction = "bonferroni"), df)
  .tab_expect(abs(r$value("model|||n_control") / r$value("model|||n_per_variant") - 2) < 1e-3, "n_c / n_t")
  .tab_expect(r$value("model|||n_total") == r$value("model|||n_control") + 3 * r$value("model|||n_per_variant"), "the total of four arms")
  .tab_expect(abs(r$value("model|||alpha_per_comparison") - 0.05 / 3) < 1e-15, "alpha / (arms - 1)")
}

# The A/B node's minimum detectable effect (ab_compare: (z_a + z_b) se / |m_c|, with its own comparison level) is the plan's
# mde, for the same hypotheses: the estimates a plan stands for, given to ab_compare, give back the plan's lift.
TAB_COMMON_TESTS[["experiments: the plan's minimum detectable effect is the A/B node's (ab_compare), on the estimates the plan stands for and end to end"]] <- function(code) {
  df <- .abp_frame()
  set.seed(11)
  configs <- list(list(metric_type = "binary", metric = "conv"), list(metric_type = "continuous", metric = "rev"),
                  list(metric_type = "ratio", numerator = "num", denominator = "den"))
  for (cfg in configs) for (i in 1:4) {
    arms <- sample(2:5, 1L)
    extra <- list(solve_for = "mde_for_units", units_per_variant = sample(c(300, 2000, 20000), 1L), arms = arms, alpha = sample(c(0.05, 0.1), 1L),
                  power = sample(c(0.8, 0.9), 1L), correction = sample(c("holm", "bonferroni", "benjamini_hochberg", "none"), 1L),
                  alternative = sample(c("two_sided", "greater"), 1L))
    if (i > 2) extra$control_share <- 0.5 / (arms - 1) * 1.4
    if (arms == 2L) extra$control_share <- 0.35
    params <- c(cfg, extra)
    r <- .abp_run(params, df)
    mde <- r$value("model|||mde")
    m0 <- r$value("model|||baseline_mean")
    s0 <- r$value("model|||baseline_sd")
    n_t <- extra$units_per_variant
    wc <- r$value("model|||control_share")
    n_c <- n_t * wc / ((1 - wc) / (arms - 1))
    m_t <- m0 * (1 + mde)
    v_t <- if (cfg$metric_type == "binary") m_t * (1 - m_t) / n_t else s0^2 / n_t
    est <- list(m = c(m0, rep(m_t, arms - 1L)), v = c(s0^2 / n_c, rep(v_t, arms - 1L)), n = c(n_c, rep(n_t, arms - 1L)), k = arms, control = 1L,
                labels = LETTERS[seq_len(arms)], reference = "z")
    ctx <- list(alpha = extra$alpha, alternative = extra$alternative, correction = extra$correction, higher_is_better = TRUE, min_lift = 0.02, power = extra$power)
    got <- vapply(ab_compare(est, ctx)$rows, `[[`, 0, "mde")
    .tab_expect(all(abs(got / mde - 1) < 1e-10), cfg$metric_type, " arms ", arms, ": the A/B node's mde ", got[[1L]], ", the plan's ", mde)
  }
  # end to end: a table whose second variant repeats the first one's values has the same variance and units in both: the A/B
  # node's mde on it is the plan's mde of the history, at the units of each variant
  ab <- rbind(transform(df, v = "A"), transform(df, v = "B"))
  for (alt in c("two_sided", "less")) {
    t <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = "ab_test", variant = "continuous",
              params = list(variant = "v", control = "A", metric_type = "continuous", metric = "rev", alternative = alt, bayes = FALSE, power = 0.9), output = "results",
              write_output = FALSE, rows_from = "run", limits = tab_catalog()$limits, .frame = ab)
    node <- tab_results_frame(fit_ab_test(t)$rows)
    plan <- .abp_run(list(metric_type = "continuous", metric = "rev", solve_for = "mde_for_units", units_per_variant = nrow(df), alternative = alt, power = 0.9), df)
    .tab_expect(abs(node$value[node$key == "comparison|B|A|mde"] / plan$value("model|||mde") - 1) < 1e-12, "end to end, ", alt)
  }
}

TAB_COMMON_TESTS[["experiments: the sizes and the lift they detect are each other's inverse, whole units rounded up"]] <- function(code) {
  df <- .abp_frame()
  for (cfg in list(list(metric_type = "binary", metric = "conv"), list(metric_type = "continuous", metric = "rev"),
                   list(metric_type = "ratio", numerator = "num", denominator = "den"))) {
    for (extra in list(list(min_lift = 0.3), list(min_lift = 0.2, arms = 3, control_share = 0.5, power = 0.9))) {
      size <- .abp_run(c(cfg, extra), df)
      n <- size$value("model|||n_per_variant")
      with_n <- function(units) .abp_run(c(cfg, extra[setdiff(names(extra), "min_lift")], list(solve_for = "mde_for_units", units_per_variant = units)), df)$value("model|||mde")
      .tab_expect(with_n(n) <= extra$min_lift && with_n(n - 1) > extra$min_lift, cfg$metric_type, ": ", n, " units detect ", with_n(n), ", one fewer ", with_n(n - 1))
    }
  }
}

TAB_COMMON_TESTS[["experiments: CUPED takes the variance down by rho^2 (the sizes with it), the days follow the history's traffic, no random number"]] <- function(code) {
  df <- .abp_frame()
  df$pre[is.na(df$pre)] <- mean(df$pre, na.rm = TRUE)
  p <- list(metric_type = "continuous", metric = "rev", covariate = "pre", date = "day", min_lift = 0.05)
  r <- .abp_run(p, df)
  rho <- stats::cor(df$rev, df$pre)
  .tab_expect(abs(r$value("model|||variance_reduction") - rho^2) < 1e-12 && abs(r$value("model|||covariate_correlation") - rho) < 1e-12, "rho^2")
  n <- r$value("model|||n_per_variant")
  .tab_expect(abs(r$value("model|||n_per_variant_cuped") - n * (1 - rho^2)) <= 2, "the sizes with CUPED")
  .tab_expect(abs(r$value("model|||mde_cuped") / (r$value("model|||mde") * sqrt(1 - rho^2)) - 1) < 1e-3, "mde_cuped")
  daily <- nrow(df) / 14
  .tab_expect(abs(r$value("model|||daily_units") - daily) < 1e-12 && r$value("model|||days") == ceiling(r$value("model|||n_total") / daily - 1e-9), "the days")
  .tab_expect(r$value("model|||weeks") == ceiling(r$value("model|||days") / 7) && r$value("model|||days_cuped") <= r$value("model|||days"), "the weeks")
  # the traffic share keeps the units a day of the experiment; the days grow in proportion
  half <- .abp_run(c(p, list(traffic_share = 0.5)), df)
  .tab_expect(abs(half$value("model|||daily_units") - daily / 2) < 1e-12 && half$value("model|||days") >= 2 * r$value("model|||days") - 1, "the traffic share")
  .tab_expect(identical(.abp_run(p, df)$frame, r$frame), "no random number: two runs differ")
}

TAB_COMMON_TESTS[["experiments: the grid holds 30 lifts and the asked one, fewer units for a larger lift, and the `plan` output has the columns the catalogue declares"]] <- function(code) {
  df <- .abp_frame()
  for (type in c("binary", "continuous")) {
    p <- list(metric_type = type, metric = if (type == "binary") "conv" else "rev", date = "day", min_lift = 0.1, output = "plan")
    r <- .abp_run(p, df)
    lifts <- r$frame[r$frame$section == "plan" & r$frame$stat == "lift", ]
    .tab_expect(nrow(lifts) == 31L && !is.unsorted(lifts$value) && lifts$value[[1L]] > 0.1 / 4 - 1e-9 && max(lifts$value) <= 0.4 + 1e-12, type, ": the grid")
    n <- r$frame$value[r$frame$section == "plan" & r$frame$stat == "n_per_variant"]
    .tab_expect(all(diff(n) <= 0), type, ": a larger lift needs fewer units")
    asked <- r$frame$key[r$frame$section == "plan" & r$frame$stat == "asked" & r$frame$value == 1]
    .tab_expect(length(asked) == 1L && r$value(sub("asked$", "n_per_variant", asked)) == r$value("model|||n_per_variant"), type, ": the asked row is the answer")
    o <- r$output
    .tab_expect(identical(names(o), c("lift", "mde_absolute", "n_per_variant", "n_control", "n_total", "days", "weeks", "n_per_variant_cuped", "days_cuped", "asked")) &&
                  o$num_rows == 31L && o$schema$GetFieldByName("asked")$type$ToString() == "bool" && o$days_cuped$null_count == 31L, type, ": the output")
  }
  # a rate near 1 keeps the grid to the lifts a rate can make
  tight <- .abp_run(list(metric_type = "binary", metric = "hi", min_lift = 0.05), transform(df, hi = as.integer(seq_len(nrow(df)) %% 10 != 0)))
  .tab_expect(all(tight$frame$value[tight$frame$section == "plan" & tight$frame$stat == "lift"] <= (1 - 0.9) / 0.9 + 1e-9), "a rate cannot pass 100 %")
}

TAB_COMMON_TESTS[["experiments: the planning node refuses a history it cannot plan from, with a sentence"]] <- function(code) {
  df <- .abp_frame()
  df$zero <- 0
  df$same <- 5
  said <- function(params, code_) .tab_refused_with(code_, fit_ab_plan(.abp_fit_task(params, df)))
  said(list(metric_type = "continuous", metric = "zero"), "baseline_zero")
  said(list(metric_type = "continuous", metric = "same"), "variance_zero")
  said(list(metric_type = "continuous", metric = "rev", solve_for = "mde_for_days", days = 10), "days_need_date")
  said(list(metric_type = "binary", metric = "conv", min_lift = 50), "lift_too_large")
  said(list(metric_type = "binary", metric = "conv", solve_for = "mde_for_units", units_per_variant = 2), "mde_unreachable")
}

TAB_COMMON_TESTS[["experiments: every planning case's explanation holds the template ids of expected.json, row by row, as «@id name=value…»"]] <- function(code) {
  ref <- jsonlite::fromJSON(file.path(code, "reference", "expected.json"), simplifyVector = FALSE)
  seen <- 0L
  for (case in ref$cases) {
    if (!identical(case$model, "ab_plan") || is.null(case$reading)) next
    df <- tab_read_csv(file.path(code, "reference", case$data), case$types)
    task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = "ab_plan",
                 variant = tab_variant_of("ab_plan", case$params), params = case$params, output = "results", write_output = FALSE, rows_from = "run",
                 limits = tab_catalog()$limits, .frame = df)
    r <- tab_results_frame(fit_ab_plan(task)$rows)
    r <- r[r$section == "reading", ]
    .tab_expect(identical(paste(r$key, sub("^@([^ ]+).*$", "\\1", r$text)), vapply(case$reading, function(x) paste(x[[1L]], x[[2L]]), "")), case$id, ": the reading differs")
    .tab_expect(all(grepl("^@[a-z0-9_]+( [a-z0-9_]+=[^ ]*)*$", r$text)), case$id, ": a line is not «@id name=value…»")
    seen <- seen + 1L
  }
  .tab_expect(seen >= 6L, "only ", seen, " planning cases")
}
