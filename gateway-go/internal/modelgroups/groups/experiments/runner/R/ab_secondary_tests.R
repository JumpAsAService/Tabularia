# The self-test of the secondary metrics (docs/specs/ab-testing.md, «Secondary metrics», lot E8), added to
# TAB_COMMON_TESTS; their numbers against the long way are reference cases (make_reference_secondary.R), checked by the
# common self-test. Here: the primary is the same with them, the family is one p.adjust, the `variants` output, the
# small cases (one comparison, no correction, a metric too thin to compare, a column that is not a metric), and the
# sentences without days that close the gaps of E4 and E5.

.abx_without <- function(f) {
  drop <- f$section == "secondary" | (f$section == "reading" & f$level %in% c("secondary", "method", "formulas")) |
    (f$section == "model" & grepl("^@secondary_", f$text))
  f <- f[!drop, c("key", "value", "text")]
  rownames(f) <- NULL
  f
}

TAB_COMMON_TESTS[["experiments: three secondary metrics change nothing of the primary (every row, the variants output); their family is one p.adjust; each its own direction"]] <- function(code) {
  seen <- 0L
  for (case in .abc_cases(code)) {
    if (!length(case$params$secondary_metrics)) next
    df <- tab_read_csv(file.path(code, "reference", case$data), case$types)
    run <- function(params, output = "results") {
      task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = "ab_test",
                   variant = tab_variant_of("ab_test", params), params = c(params, list(output = output)), output = "results",
                   write_output = FALSE, rows_from = "run", limits = tab_catalog()$limits, .frame = df)
      fit_ab_test(task)
    }
    alone <- case$params
    alone$secondary_metrics <- list()
    with <- tab_results_frame(run(case$params)$rows)
    .tab_expect(identical(.abx_without(with), .abx_without(tab_results_frame(run(alone)$rows))), case$id, ": the primary's rows changed")
    k <- sum(with$section == "variant" & with$stat == "n")
    a <- as.data.frame(run(case$params, "variants")$output)
    b <- as.data.frame(run(alone, "variants")$output)
    .tab_expect(identical(a[seq_len(k), ], b) && nrow(a) == k * (1L + length(case$params$secondary_metrics)), case$id, ": the variants output")
    sec <- a[-seq_len(k), ]
    .tab_expect(all(sec$role == "secondary") && all(is.na(sec$decision[sec$is_control])) && all(is.na(sec$prob_best)) &&
                  all(is.na(sec$sequential_p_value)) && all(is.na(sec$share)), case$id, ": the secondary rows of the output")
    # one family: every metric × every treatment adjusted together, the decision from the adjusted p against alpha
    s <- with[with$section == "secondary", ]
    p <- s$value[s$stat == "p_value"]
    adj <- s$value[s$stat == "p_adjusted"]
    how <- AB_CORRECTIONS[[case$params$correction %||% "holm"]]
    .tab_expect(length(p) == 2L * length(case$params$secondary_metrics) && max(abs(stats::p.adjust(p, how) - adj)) < 1e-15, case$id, ": the family")
    decided <- s$text[s$stat == "decision"]
    .tab_expect(identical(decided != "not_significant", adj < 0.05), case$id, ": the decisions")
    # each metric in its own direction (`secondary_directions`), the primary's where none is given
    up <- s$value[s$stat == "difference"] > 0
    higher <- s$text[s$stat == "direction"] == "higher"
    dirs <- case$params$secondary_directions
    want <- vapply(rep(unlist(case$params$secondary_metrics), each = 2L), function(m) if (is.null(dirs[[m]])) !isFALSE(case$params$higher_is_better) else dirs[[m]] == "higher", TRUE)
    .tab_expect(identical(unname(want), higher) && identical(decided[adj < 0.05] == "better", (up == higher)[adj < 0.05]), case$id, ": the directions")
    seen <- seen + 1L
  }
  .tab_expect(seen >= 2L, "only ", seen, " cases with secondary metrics")
}

TAB_COMMON_TESTS[["experiments: a secondary metric alone, without a correction, too thin, or not a metric; the sentences without days"]] <- function(code) {
  df <- data.frame(v = rep(c("A", "B"), each = 8), y = rep(c(0, 1), 8), g = c(1, NA, NA, NA, NA, NA, NA, NA, 3:10),
                   ok = rep(c(TRUE, FALSE, TRUE, TRUE), 4), name = "x", stringsAsFactors = FALSE)
  lines <- function(out) out$frame$text[which(out$frame$section == "reading" & out$frame$level == "secondary")]
  ids <- function(out) sub("^@([a-z_]+).*$", "\\1", lines(out))
  one <- .abc_fit(list(metric_type = "binary", metric = "y", secondary_metrics = list("ok")), df)
  .tab_expect(identical(ids(one), c("secondary_family_one", "secondary_none_worse")) && one$text("secondary|ok|B|metric_type") == "binary", "one comparison")
  none <- .abc_fit(list(metric_type = "binary", metric = "y", correction = "none", secondary_metrics = list("ok", "g")), df)
  .tab_expect(identical(ids(none)[[1L]], "secondary_family_one"), "the thin metric is not counted: ", ids(none)[[1L]])
  .tab_expect(none$text("secondary|g|B|decision") == "not_computed" && is.na(none$value("secondary|g|B|p_value")) &&
                any(grepl("^@secondary_missing metric=g count=7$", none$frame$text)), "a control with 1 unit of g: not computed, 7 units without g")
  df$h <- df$y * 2 + rep(1:4, 4)
  two <- .abc_fit(list(metric_type = "binary", metric = "y", correction = "none", secondary_metrics = list("ok", "h")), df)
  .tab_expect(identical(ids(two)[[1L]], "secondary_family_none") && grepl(" comparisons=2$", lines(two)[[1L]]), "no correction")
  .tab_expect(two$value("secondary|h|B|p_value") == two$value("secondary|h|B|p_adjusted"), "none: p_adjusted is p")
  .tab_refused_with("column_type", fit_ab_test(.abc_task(list(metric_type = "binary", metric = "y", secondary_metrics = list("name")), df)))
  .tab_refused_with("missing_column", fit_ab_test(.abc_task(list(metric_type = "binary", metric = "y", secondary_metrics = list("nope")), df)))
  # the planning node without a date: the baseline and CUPED said without days (E5's gap)
  set.seed(5)
  h <- data.frame(y = stats::rnorm(200, 20, 4), x = stats::rnorm(200))
  h$y <- h$y + 3 * h$x
  task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = "ab_plan", variant = "continuous",
               params = list(metric_type = "continuous", metric = "y", covariate = "x"), output = "results", write_output = FALSE, rows_from = "run",
               limits = tab_catalog()$limits, .frame = h)
  r <- tab_results_frame(fit_ab_plan(task)$rows)
  said <- r$text[r$section == "reading" & r$level %in% c("baseline", "cuped")]
  .tab_expect(grepl("^@baseline_read_no_days mean=[^ ]+ sd=[^ ]+ units=200$", said[[1L]]) && grepl("^@cuped_plan_no_days reduction=[^ ]+ units=[0-9]+$", said[[2L]]),
              "the plan without a date: ", paste(said, collapse = " | "))
}
