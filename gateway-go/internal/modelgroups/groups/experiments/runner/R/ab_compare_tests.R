# The self-test of the comparisons (docs/specs/ab-testing.md, lot E2): added to TAB_COMMON_TESTS, so that
# `Rscript runner/run.R --selftest` runs them after the common library's and the data module's. Each formula is
# compared with base R computing the same thing the long way, on the raw rows (t.test, lm, oneway.test,
# chisq.test, var, cov); the reference cases of reference/expected.json are checked here for what the common
# self-test does not read: the template id of every row of the explanation, and Fieller ⇔ the test.

.abc_task <- function(params, frame, variant = params$metric_type) {
  params <- utils::modifyList(list(variant = "v", control = "A", bayes = FALSE), params)
  list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = "ab_test", variant = variant,
       params = params, output = "results", write_output = FALSE, rows_from = "run", limits = tab_catalog()$limits, .frame = frame)
}

.abc_fit <- function(params, frame) {
  task <- .abc_task(params, frame)
  out <- fit_ab_test(task)
  problems <- tab_check_declared(out$rows, "ab_test", task$variant)
  .tab_expect(!length(problems), "the rows do not match the catalogue: ", paste(head(problems, 3L), collapse = "; "))
  out$frame <- tab_results_frame(out$rows)
  out$value <- function(key) out$frame$value[match(key, out$frame$key)]
  out$text <- function(key) out$frame$text[match(key, out$frame$key)]
  out
}

.abc_rel <- function(got, want) max(abs(got - want) / pmax(abs(want), 1e-300))

.abc_cases <- function(code) {
  ref <- jsonlite::fromJSON(file.path(code, "reference", "expected.json"), simplifyVector = FALSE)
  Filter(function(c) identical(c$model, "ab_test") && is.null(c$refuses), ref$cases)
}

.abc_run_case <- function(case, code) {
  df <- tab_read_csv(file.path(code, "reference", case$data), case$types)
  task <- list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = case$model,
               variant = tab_variant_of(case$model, case$params), params = case$params, output = "results", write_output = FALSE,
               rows_from = "run", limits = tab_catalog()$limits, .frame = df)
  tab_results_frame(fit_ab_test(task)$rows)
}

# ── the reference cases: their readings, Fieller and the test ───────────────────
TAB_COMMON_TESTS[["experiments: every reference case's explanation holds the template ids of expected.json, row by row"]] <- function(code) {
  for (case in .abc_cases(code)) {
    if (is.null(case$reading)) next
    df <- .abc_run_case(case, code)
    r <- df[df$section == "reading", ]
    got <- paste(r$key, sub("^@([^ ]+).*$", "\\1", r$text))
    want <- vapply(case$reading, function(x) paste(x[[1L]], x[[2L]]), "")
    .tab_expect(identical(got, want), case$id, ": the reading is\n  ", paste(got, collapse = "\n  "), "\nexpected\n  ", paste(want, collapse = "\n  "))
    .tab_expect(all(grepl("^@[a-z0-9_]+( [a-z0-9_]+=[^ ]*)*$", r$text)), case$id, ": a line is not «@id name=value…»: ",
                paste(r$text[!grepl("^@[a-z0-9_]+( [a-z0-9_]+=[^ ]*)*$", r$text)], collapse = " | "))
  }
}

# The lift's interval leaves 0 out exactly when the difference's test rejects at the interval's level (alpha, or
# alpha/m with Bonferroni), whenever Fieller's set is an interval; one-sided, its one end against 0.
.abc_fieller_agrees <- function(lo, hi, p, level, alternative) {
  if (is.na(p)) return(TRUE)
  if (is.na(lo) && is.na(hi)) return(TRUE)  # not bounded: no interval to read (the note fieller_unbounded)
  out <- (!is.na(lo) && lo > 0) || (!is.na(hi) && hi < 0)
  identical(out, p < level)
}

TAB_COMMON_TESTS[["experiments: Fieller's interval leaves out a lift of 0 exactly when the test rejects, on every reference case and 400 drawn comparisons"]] <- function(code) {
  seen <- 0L
  for (case in .abc_cases(code)) {
    df <- .abc_run_case(case, code)
    alpha <- as.numeric(case$params$alpha %||% 0.05)
    cmp <- df[df$section == "comparison", ]
    m <- length(unique(cmp$level))
    level <- if (identical(case$params$correction, "bonferroni")) alpha / m else alpha
    for (t in unique(cmp$level)) {
      at <- function(stat) cmp$value[cmp$level == t & cmp$stat == stat]
      .tab_expect(.abc_fieller_agrees(at("lift_ci_low"), at("lift_ci_high"), at("p_value"), level, case$params$alternative %||% "two_sided"),
                  case$id, " ", t, ": lift [", at("lift_ci_low"), ", ", at("lift_ci_high"), "], p ", at("p_value"))
      seen <- seen + 1L
    }
  }
  .tab_expect(seen >= 20L, "only ", seen, " comparisons")
  # drawn estimates: ratios near 1 (the hard cases), negative control means, one-sided tests, Welch's quantiles
  set.seed(5)
  for (i in seq_len(400L)) {
    mc <- sample(c(-1, 1), 1L) * stats::rexp(1L, 0.2)
    mt <- mc * (1 + stats::rnorm(1L, 0, 0.05))
    vc <- (abs(mc) * stats::runif(1L, 0.001, 0.05))^2
    vt <- vc * stats::runif(1L, 0.2, 5)
    alt <- sample(c("two_sided", "greater", "less"), 1L)
    est <- list(m = c(mc, mt), v = c(vc, vt), n = c(50, 60), k = 2L, control = 1L, labels = c("A", "B"),
                reference = sample(c("z", "welch"), 1L))
    ctx <- list(alpha = sample(c(0.05, 0.01, 0.1), 1L), alternative = alt, correction = "holm", higher_is_better = TRUE,
                min_lift = 0.02, power = 0.8)
    x <- ab_compare(est, ctx)$rows[[1L]]
    .tab_expect(.abc_fieller_agrees(x$lift_low, x$lift_high, x$p, ctx$alpha, alt), "draw ", i, ": lift [", x$lift_low, ", ", x$lift_high,
                "], p ", x$p, " (", alt, ")")
    if (alt == "two_sided" && x$fieller_bounded) {
      # the ends are the roots of Fieller's quadratic: (m_t − ρ m_c)² = q² (v_t + ρ² v_c) at ρ = 1 + end
      for (end in c(x$lift_low, x$lift_high)) {
        rho <- 1 + end
        .tab_expect(abs((mt - rho * mc)^2 - x$q^2 * (vt + rho^2 * vc)) <= 1e-9 * (mt^2 + x$q^2 * (vt + rho^2 * vc)), "draw ", i, ": not a root")
      }
    }
  }
}

# ── the four estimates against the long way ──────────────────────────────────────
.abc_table <- function(seed = 3, n = 600) {
  set.seed(seed)
  v <- sample(c("A", "B", "C"), n, replace = TRUE)
  pre <- stats::rgamma(n, 3, 0.3)
  df <- data.frame(v = v, conv = as.integer(stats::runif(n) < ifelse(v == "B", 0.3, 0.22)),
                   rev = round(5 + 0.6 * pre + stats::rexp(n, 0.1) + ifelse(v == "C", 2, 0), 2), pre = round(pre, 2),
                   num = stats::rpois(n, 4) + 0, den = stats::rpois(n, 30) + 1, stringsAsFactors = FALSE)
  df$pre[c(3, 17, 40, 41, 99)] <- NA
  df
}

TAB_COMMON_TESTS[["experiments: binary z, Welch's t (= t.test), the delta method (denominators of 1 give the mean's), CUPED (= lm), against the long way"]] <- function(code) {
  df <- .abc_table()
  for (alt in c("two_sided", "greater", "less")) {
    w <- .abc_fit(list(metric_type = "continuous", metric = "rev", alternative = alt, correction = "none"), df)
    for (t in c("B", "C")) {
      tt <- stats::t.test(df$rev[df$v == t], df$rev[df$v == "A"], var.equal = FALSE,
                          alternative = c(two_sided = "two.sided", greater = "greater", less = "less")[[alt]])
      key <- function(s) paste0("comparison|", t, "|A|", s)
      .tab_expect(.abc_rel(c(w$value(key("statistic")), w$value(key("df")), w$value(key("p_value"))),
                           c(tt$statistic, tt$parameter, tt$p.value)) < 1e-10, "Welch ", t, " ", alt)
      ends <- c(w$value(key("difference_ci_low")), w$value(key("difference_ci_high")))
      .tab_expect(.abc_rel(ends[is.finite(tt$conf.int)], tt$conf.int[is.finite(tt$conf.int)]) < 1e-10 && identical(is.na(ends), !is.finite(tt$conf.int)),
                  "Welch's interval ", t, " ", alt)
    }
  }
  b <- .abc_fit(list(metric_type = "binary", metric = "conv"), df)
  p <- tapply(df$conv, df$v, mean)
  n <- table(df$v)
  z <- (p[["B"]] - p[["A"]]) / sqrt(p[["B"]] * (1 - p[["B"]]) / n[["B"]] + p[["A"]] * (1 - p[["A"]]) / n[["A"]])
  .tab_expect(.abc_rel(b$value("comparison|B|A|statistic"), z) < 1e-12 &&
                .abc_rel(b$value("comparison|B|A|p_value"), 2 * stats::pnorm(-abs(z))) < 1e-10, "the unpooled z")
  .tab_expect(b$value("variant|B||conversions") == sum(df$conv[df$v == "B"]), "the conversions are exact")
  # the delta method: every denominator 1 makes the ratio the numerator's mean, with the mean's variance
  one <- df
  one$den <- 1
  r <- .abc_fit(list(metric_type = "ratio", numerator = "num", denominator = "den"), one)
  m <- .abc_fit(list(metric_type = "continuous", metric = "num"), one)
  for (j in c("A", "B", "C")) {
    .tab_expect(.abc_rel(r$value(paste0("variant|", j, "||std_error")), m$value(paste0("variant|", j, "||std_error"))) < 1e-12, "delta, ", j)
  }
  r <- .abc_fit(list(metric_type = "ratio", numerator = "num", denominator = "den"), df)
  for (j in c("A", "B")) {
    y <- df$num[df$v == j]
    x <- df$den[df$v == j]
    rr <- sum(y) / sum(x)
    v <- (stats::var(y) - 2 * rr * stats::cov(y, x) + rr^2 * stats::var(x)) / (mean(x)^2 * length(y))
    .tab_expect(.abc_rel(c(r$value(paste0("variant|", j, "||mean")), r$value(paste0("variant|", j, "||std_error"))), c(rr, sqrt(v))) < 1e-12,
                "the delta method's variance, ", j)
  }
  # CUPED, both ways of an empty covariate: θ is lm's common slope on the units with a covariate; the adjusted
  # difference is the variant's coefficient of y ~ variant + x on the units the comparison uses
  for (mode in c("fill", "exclude")) {
    cu <- .abc_fit(list(metric_type = "continuous", metric = "rev", covariate = "pre", covariate_missing = mode), df)
    has <- !is.na(df$pre)
    x <- df$pre
    fill <- tapply(df$pre[has], df$v[has], mean)
    x[!has] <- fill[df$v[!has]]
    keep <- if (mode == "exclude") has else rep(TRUE, nrow(df))
    ols <- stats::lm(rev ~ factor(v) + x, data = data.frame(rev = df$rev, v = df$v, x = x)[keep, ])
    .tab_expect(.abc_rel(cu$value("model|||theta"), stats::coef(ols)[["x"]]) < 1e-10, mode, ": θ")
    .tab_expect(.abc_rel(cu$value("comparison|B|A|difference"), stats::coef(ols)[["factor(v)B"]]) < 1e-10 &&
                  .abc_rel(cu$value("comparison|C|A|difference"), stats::coef(ols)[["factor(v)C"]]) < 1e-10, mode, ": the adjusted difference")
    s2 <- stats::var((df$rev - cu$value("model|||theta") * x)[keep & df$v == "B"])
    s2a <- stats::var((df$rev - cu$value("model|||theta") * x)[keep & df$v == "A"])
    .tab_expect(.abc_rel(cu$value("comparison|B|A|difference_std_error"), sqrt(s2 / sum(keep & df$v == "B") + s2a / sum(keep & df$v == "A"))) < 1e-10,
                mode, ": the variance of y − θx")
    if (mode == "fill") {
      .tab_expect(.abc_rel(c(cu$value("variant|A||covariate_fill_value"), cu$value("variant|C||covariate_fill_value")), unname(fill[c("A", "C")])) < 1e-12,
                  "the fill value is the mean of the unit's own variant")
      # beside it, the pooled mean the spec first recommended (decision 14 chose the variant's): both unbiased;
      # the two differences are close but not the same
      xp <- df$pre
      xp[!has] <- mean(df$pre[has])
      pooled <- stats::lm(df$rev ~ factor(df$v) + xp)
      .tab_expect(abs(stats::coef(pooled)[["factor(df$v)B"]] - cu$value("comparison|B|A|difference")) > 0, "the two fills give the same difference")
    }
  }
}

TAB_COMMON_TESTS[["experiments: the corrections are p.adjust's, the decision is the adjusted p against alpha, the measures have no verdict"]] <- function(code) {
  df <- .abc_table(11, 900)
  df$rev[df$v == "C"] <- df$rev[df$v == "C"] + 3
  for (how in c("holm", "benjamini_hochberg", "bonferroni", "none")) {
    w <- .abc_fit(list(metric_type = "continuous", metric = "rev", correction = how), df)
    p <- c(w$value("comparison|B|A|p_value"), w$value("comparison|C|A|p_value"))
    padj <- c(w$value("comparison|B|A|p_adjusted"), w$value("comparison|C|A|p_adjusted"))
    .tab_expect(isTRUE(all.equal(padj, stats::p.adjust(p, AB_CORRECTIONS[[how]]), tolerance = 1e-15)), how, ": p.adjust")
    for (t in c("B", "C")) {
      i <- match(t, c("B", "C"))
      want <- if (padj[[i]] < 0.05) (if (w$value(paste0("comparison|", t, "|A|difference")) > 0) "better" else "worse") else "not_significant"
      .tab_expect(identical(w$text(paste0("comparison|", t, "|A|decision")), want), how, " ", t, ": the decision")
    }
    level <- if (how == "bonferroni") 0.05 / 2 else 0.05
    q <- stats::qt(1 - level / 2, w$value("comparison|C|A|df"))
    .tab_expect(.abc_rel(w$value("comparison|C|A|difference_ci_high") - w$value("comparison|C|A|difference"),
                         q * w$value("comparison|C|A|difference_std_error")) < 1e-12, how, ": the interval's level")
  }
  # a cost: lower is better, the same p-values, the decisions turned round; the headline by precedence
  up <- .abc_fit(list(metric_type = "continuous", metric = "rev"), df)
  down <- .abc_fit(list(metric_type = "continuous", metric = "rev", higher_is_better = FALSE), df)
  .tab_expect(identical(up$text("comparison|C|A|decision"), "better") && identical(down$text("comparison|C|A|decision"), "worse") &&
                identical(up$text("model|||decision"), "better") && identical(down$text("model|||decision"), "worse") &&
                identical(up$text("model|||decision_variants"), "C"), "higher_is_better")
  # the power at this size: at the detectable lift, the test's side towards it has the asked power (the other
  # side adds a few millionths); the power at the minimum lift is the two sides' (measures: no verdict, no text)
  se <- up$value("comparison|C|A|difference_std_error")
  mc <- up$value("variant|A||mean")
  mde <- up$value("comparison|C|A|mde")
  z <- stats::qnorm(1 - 0.05 / 2 / 2)
  .tab_expect(abs(stats::pnorm(mde * mc / se - z) - 0.8) < 1e-12, "the power at the detectable lift")
  .tab_expect(abs(up$value("comparison|C|A|power_at_min_lift") - (stats::pnorm(0.02 * mc / se - z) + stats::pnorm(-0.02 * mc / se - z))) < 1e-15,
              "the power at the minimum lift")
  pw <- up$frame[up$frame$section == "curve" & up$frame$level == "power" & up$frame$level_2 == "C", ]
  .tab_expect(nrow(pw) == tab_catalog()$limits$power_points && !is.unsorted(pw$value) && pw$value[[1L]] > 0 && pw$value[[1L]] < 0.05,
              "the power curve")
  # the minimum lift against the interval: a statement
  pos <- up$text("comparison|C|A|min_lift_position")
  lo <- up$value("comparison|C|A|lift_ci_low")
  hi <- up$value("comparison|C|A|lift_ci_high")
  .tab_expect(identical(pos, if (lo > 0.02) "above" else if (hi < 0.02) "below" else "straddles"), "min_lift_position ", pos)
  # one treatment: the correction changes nothing, a note says so
  two <- df[df$v != "C", ]
  one <- .abc_fit(list(metric_type = "continuous", metric = "rev"), two)
  .tab_expect(identical(one$value("comparison|B|A|p_adjusted"), one$value("comparison|B|A|p_value")) &&
                any(one$frame$text %in% "@one_treatment"), "one treatment")
}

TAB_COMMON_TESTS[["experiments: covariate_balance is Welch's one-way F (= oneway.test), covariate_missing_balance Pearson's chi-squared (= chisq.test)"]] <- function(code) {
  df <- .abc_table(13, 700)
  df$pre[sample(700, 60)] <- NA
  for (mode in c("fill", "exclude")) {
    cu <- .abc_fit(list(metric_type = "continuous", metric = "rev", covariate = "pre", covariate_missing = mode), df)
    w <- stats::oneway.test(pre ~ v, data = df[!is.na(df$pre), ], var.equal = FALSE)
    .tab_expect(.abc_rel(c(cu$value("assumption|covariate_balance||statistic"), cu$value("assumption|covariate_balance||df_2"),
                           cu$value("assumption|covariate_balance||p_value")), c(w$statistic, w$parameter[[2L]], w$p.value)) < 1e-10,
                mode, ": oneway.test")
    x <- stats::chisq.test(table(df$v, is.na(df$pre)), correct = FALSE)
    .tab_expect(.abc_rel(c(cu$value("assumption|covariate_missing_balance||statistic"), cu$value("assumption|covariate_missing_balance||p_value")),
                         c(x$statistic, x$p.value)) < 1e-10, mode, ": chisq.test")
  }
  two <- df[df$v != "C", ]
  cu <- .abc_fit(list(metric_type = "continuous", metric = "rev", covariate = "pre"), two)
  tt <- stats::t.test(two$pre[two$v == "B"], two$pre[two$v == "A"], var.equal = FALSE)
  .tab_expect(.abc_rel(c(cu$value("assumption|covariate_balance||statistic"), cu$value("assumption|covariate_balance||p_value")),
                       c(tt$statistic^2, tt$p.value)) < 1e-10, "two variants: Welch's t squared")
}

# ── the reading's values ─────────────────────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: a reading's values: a number reads back as the same double, a space, a comma, a percent are encoded"]] <- function(code) {
  set.seed(9)
  x <- c(0.1, 1 / 3, 0.05, 12480, -0.03, 2e-9, 1.5e20, stats::rnorm(200) * 10^stats::runif(200, -12, 12))
  back <- as.numeric(ab_reading_number(x))
  .tab_expect(identical(back, x) && all(grepl("^-?[0-9]+(\\.[0-9]+)?(e[-+][0-9]+)?$", ab_reading_number(x))), "numbers")
  .tab_expect(identical(ab_reading_number(c(0.05, 12480, 2e-9)), c("0.05", "12480", "2e-09")), paste(ab_reading_number(c(0.05, 12480, 2e-9))))
  line <- ab_line("srm_ok", variants = c("50% off", "B,1", "ü ñ"), p = 0.62, none = NULL, nothing = NA_real_)
  .tab_expect(identical(line, "@srm_ok variants=50%25%20off,B%2C1,ü%20ñ p=0.62"), line)
}

# ── the editor's labels of the expected shares, and the variants output's types ──
# The form keys `expected_shares` by the labels the editor writes of the variant column's values (statLevelLabels:
# String(n) of an integer, «true»/«false», a date as YYYY-MM-DD, the text): the runner meets them with the same
# variants, whatever the column's type, and the `variants` output keeps that type.
TAB_COMMON_TESTS[["experiments: the expected shares keyed as the editor labels them meet their variants; the variants output keeps the variant's type"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  raw <- utils::read.csv(file.path(code, "reference", "data", "types.csv"), colClasses = "character", encoding = "UTF-8", stringsAsFactors = FALSE)
  cols <- list(
    v_int64 = list(x = bit64::as.integer64(raw$v_int64), type = arrow::int64(), control = "5000000001",
                   shares = list("4294967303" = 1, "5000000001" = 2, "5000000002" = 5)),
    v_int32 = list(x = as.integer(raw$v_int32), type = arrow::int32(), control = "-7", shares = list("-7" = 2, "12" = 5, "2147483647" = 1)),
    v_bool = list(x = raw$v_bool == "true", type = arrow::boolean(), control = "true", shares = list("false" = 1, "true" = 3)),
    v_text = list(x = raw$v_text, type = arrow::utf8(), control = "control", shares = list("control" = 2, "new layout" = 5, "été" = 1)),
    v_date = list(x = as.Date(raw$v_date), type = arrow::date32(), control = "2026-03-02",
                  shares = list("2026-03-02" = 2, "2026-03-03" = 5, "2026-03-04" = 1)))
  path <- tempfile(fileext = ".parquet")
  on.exit({ Sys.unsetenv("TABULARIA_INPUT"); unlink(path) }, add = TRUE)
  tbl <- do.call(arrow::arrow_table, c(lapply(cols, function(z) arrow::Array$create(z$x, type = z$type)), list(converted = as.numeric(raw$converted))))
  arrow::write_parquet(tbl, path)
  Sys.setenv(TABULARIA_INPUT = path)
  names_out <- names(tab_catalog()$output_types$variants)
  for (name in names(cols)) {
    z <- cols[[name]]
    task <- .abc_task(list(variant = name, control = z$control, metric_type = "binary", metric = "converted", expected_shares = z$shares,
                           output = "variants"), NULL)
    out <- fit_ab_test(task)
    f <- tab_results_frame(out$rows)
    w <- unlist(z$shares)
    for (label in names(w)) {
      got <- f$value[f$key == paste0("variant|", label, "||expected_share")]
      .tab_expect(length(got) == 1L && abs(got - w[[label]] / sum(w)) < 1e-15, name, ": the share of «", label, "»")
    }
    o <- out$output
    .tab_expect(identical(o$schema$names, names_out), name, ": the output's columns ", paste(o$schema$names, collapse = ","))
    .tab_expect(o$schema$GetFieldByName("variant")$type == z$type && o$schema$GetFieldByName("units")$type == arrow::int64() &&
                  o$schema$GetFieldByName("is_control")$type == arrow::boolean(), name, ": the output's types")
    od <- as.data.frame(o)
    .tab_expect(identical(ab_label(od$variant[od$is_control]), z$control) && all(od$role == "primary") &&
                  all(is.na(od$difference[od$is_control])) && all(!is.na(od$p_value[!od$is_control])), name, ": the output's rows")
  }
}
