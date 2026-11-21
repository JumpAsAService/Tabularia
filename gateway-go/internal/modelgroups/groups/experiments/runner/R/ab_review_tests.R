# The self-test of the final review (docs/specs/ab-testing.md, lot E10): each check guards a correction of the review,
# on small tables built here (the helpers of ab_compare_tests.R and ab_plan_tests.R).

# CUPED: a variant's own interval carries the covariate's grand mean (θ̂² s²_x / N more than the comparisons' v_j), the
# differences keep v_j; and a variant with one unit of its own covariate is computed, not an error of R's.
TAB_COMMON_TESTS[["experiments: CUPED's variant interval counts the covariate's grand mean, the differences do not; one unit of its own covariate is enough to run"]] <- function(code) {
  df <- .abc_table()
  w <- .abc_fit(list(metric_type = "continuous", metric = "rev", covariate = "pre", covariate_missing = "fill", correction = "none"), df)
  theta <- w$value("model|||theta")
  cv <- df$pre
  for (l in c("A", "B", "C")) cv[is.na(cv) & df$v == l] <- mean(df$pre[df$v == l], na.rm = TRUE)
  v <- vapply(c("A", "B", "C"), function(l) stats::var((df$rev - theta * cv)[df$v == l]) / sum(df$v == l), 0)
  level <- v + theta^2 * stats::var(cv) / nrow(df)
  got <- vapply(c("A", "B", "C"), function(l) w$value(paste0("variant|", l, "||std_error")), 0)
  .tab_expect(.abc_rel(got, sqrt(level)) < 1e-10, "the variants' standard errors ", paste(got, collapse = " "), " against ", paste(sqrt(level), collapse = " "))
  q <- stats::qnorm(0.975)
  .tab_expect(.abc_rel(w$value("variant|B||ci_high") - w$value("variant|B||ci_low"), 2 * q * sqrt(level[["B"]])) < 1e-10, "B's interval")
  for (t in c("B", "C")) {
    se <- w$value(paste0("comparison|", t, "|A|difference_std_error"))
    .tab_expect(.abc_rel(se, sqrt(v[[t]] + v[["A"]])) < 1e-10, "the difference's standard error of ", t, " keeps v_j")
  }
  one <- df
  one$pre[one$v == "C"] <- NA
  one$pre[which(one$v == "C")[[1L]]] <- 7.5
  w1 <- .abc_fit(list(metric_type = "continuous", metric = "rev", covariate = "pre", covariate_missing = "fill"), one)
  .tab_expect(is.finite(w1$value("model|||theta")) && is.finite(w1$value("comparison|C|A|p_value")), "one unit of C with its own covariate")
}

# Bonferroni: the family is the comparisons computed, as p.adjust counts them; the interval at 1 − alpha/that, so that
# the lift's interval and the decision agree. Every comparison not computed: no verdict, the decision `not_computed`.
TAB_COMMON_TESTS[["experiments: a comparison not computed is not in the family, and none computed is no verdict"]] <- function(code) {
  df <- .abc_table(5, 600)
  df$y <- df$rev
  df$y[df$v == "A"] <- 10
  df$y[df$v == "B"] <- 10  # B against A: both without variance, not computed
  w <- .abc_fit(list(metric_type = "continuous", metric = "y", correction = "bonferroni"), df)
  .tab_expect(identical(w$text("comparison|B|A|decision"), "not_computed"), "B is not computed")
  p <- w$value("comparison|C|A|p_value")
  .tab_expect(.abc_rel(w$value("comparison|C|A|p_adjusted"), min(1, p)) < 1e-12, "a family of one: C's p is not doubled")
  tt <- stats::t.test(df$y[df$v == "C"], df$y[df$v == "A"], var.equal = FALSE, conf.level = 0.95)
  ends <- c(w$value("comparison|C|A|difference_ci_low"), w$value("comparison|C|A|difference_ci_high"))
  .tab_expect(.abc_rel(ends, unname(tt$conf.int)) < 1e-10, "C's interval at 1 − alpha/1")
  flat <- df
  flat$y <- 10
  w0 <- .abc_fit(list(metric_type = "continuous", metric = "y"), flat)
  .tab_expect(identical(w0$text("model|||decision"), "not_computed"), "no comparison computed: ", w0$text("model|||decision"))
  .tab_expect(!any(grepl("^@decision_(better|worse|no_difference)", w0$frame$text[w0$frame$section == "reading"])), "no headline line without a test")
  .tab_expect(identical(w0$text("assumption|variance_zero||verdict"), "failed") && w0$value("assumption|variance_zero||count") == 3,
              "the variants without variance, counted")
}

# Several treatments better: their names as a list, the numbers of the one with the smallest adjusted p.
TAB_COMMON_TESTS[["experiments: several treatments better are said as a list, with the numbers of one"]] <- function(code) {
  set.seed(11)
  n <- 900
  df <- data.frame(v = rep(c("A", "B", "C"), each = n / 3), stringsAsFactors = FALSE)
  df$y <- stats::rnorm(n, ifelse(df$v == "A", 10, 13), 2)
  w <- .abc_fit(list(metric_type = "continuous", metric = "y"), df)
  line <- w$text("reading|decision||line_1")
  .tab_expect(startsWith(line, "@decision_better_many ") && grepl(" variants=B,C ", line, fixed = TRUE) && grepl(" variant=[BC] ", line),
              "the headline line: ", line)
}

# Names in the notes: a variant called «A count=9» or «x%41y» stays one value of the note, never another one.
TAB_COMMON_TESTS[["experiments: a variant's name in a note is encoded, so that it never adds a value to the note"]] <- function(code) {
  df <- .abc_table()
  df$v[df$v == "B"] <- "A count=9"
  df$v[df$v == "C"] <- "x%41y,z"
  df$pre[df$v == "x%41y,z"][1:3] <- NA
  w <- .abc_fit(list(metric_type = "continuous", metric = "rev", covariate = "pre", covariate_missing = "fill", control = "A"), df)
  notes <- w$frame$text[w$frame$section == "model" & startsWith(w$frame$stat, "note_")]
  filled <- notes[startsWith(notes, "@covariate_filled ")]
  .tab_expect(length(filled) >= 2L, "the notes of the filled covariate: ", paste(notes, collapse = " | "))
  for (note in filled) {
    parts <- strsplit(sub("^@[a-z_]+ ", "", note), " ", fixed = TRUE)[[1L]]
    keys <- sub("=.*$", "", parts)
    .tab_expect(identical(sort(keys), sort(c("variant", "count", "share", "mean"))), "a note with other values: ", note)
    name <- utils::URLdecode(sub("^variant=", "", parts[keys == "variant"]))
    .tab_expect(name %in% c("A", "A count=9", "x%41y,z"), "the name read back: ", name)
  }
}

# The histogram: every bar has its two edges (the open bins run to the smallest and the largest value), so that the
# report says each one by its interval, never «(blank)»; in the A/B test and in the planning node.
TAB_COMMON_TESTS[["experiments: every bar of the histogram has its two edges, in the test and in the plan"]] <- function(code) {
  df <- .abp_frame()
  df$rev[1:4] <- c(-50, 500, 900, 1200)
  w <- .abc_fit(list(metric_type = "continuous", metric = "rev"), df)
  plan <- .abp_run(list(metric_type = "continuous", metric = "rev"), df)
  for (f in list(w$frame, plan$frame)) {
    h <- f[f$section == "hist", ]
    .tab_expect(nrow(h) > 0 && all(is.finite(h$x0)) && all(is.finite(h$x1)) && all(h$x1 >= h$x0), "a bar without its edges")
    .tab_expect(min(h$x0) == -50 && max(h$x1) == 1200, "the open bins run to the smallest and the largest value")
  }
  .tab_expect(sum(plan$frame$value[plan$frame$section == "hist"]) == nrow(df), "the plan's bars count every unit")
}

# The planning node: a lift a double cannot plan with is refused with a sentence.
TAB_COMMON_TESTS[["experiments: a lift too large or too small to plan is refused with a sentence"]] <- function(code) {
  df <- .abp_frame()
  said <- function(params, code_) .tab_refused_with(code_, fit_ab_plan(.abp_fit_task(params, df)))
  said(list(metric_type = "continuous", metric = "rev", min_lift = 1e308), "lift_too_large")
  said(list(metric_type = "continuous", metric = "rev", min_lift = 1e-300), "lift_too_small")
}
