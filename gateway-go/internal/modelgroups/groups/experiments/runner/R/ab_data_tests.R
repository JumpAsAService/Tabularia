# The self-test of the data module (docs/specs/ab-testing.md, lot E1): added to TAB_COMMON_TESTS, so that
# `Rscript runner/run.R --selftest` runs them after the common library's. The data are made here (tiny
# tables, fixed seeds; the CSV of the types is in reference/data); each rule is compared with a computation
# written the long way (a loop over the units, `aggregate`, `quantile`, `chisq.test`).

.ab_task <- function(params, frame, variant = params$metric_type, limits = tab_catalog()$limits) {
  params <- utils::modifyList(list(variant = "v", control = "A"), params)
  list(protocol = TAB_PROTOCOL, mode = "fit", group = "experiments", group_version = "selftest", model = "ab_test", variant = variant,
       params = params, output = "results", write_output = FALSE, rows_from = "run", limits = limits, .frame = frame)
}

.ab_tiny <- function() {
  data.frame(v = c("A", "A", "A", "B", "B", "B"), y = c(0, 1, 1, 1, 0, 1), u = c("a", "b", "c", "d", "e", "f"),
             x = c(1, 2, 3, 4, 5, 6), stringsAsFactors = FALSE)
}

.ab_limits <- function(...) utils::modifyList(tab_catalog()$limits, list(...))

.ab_say <- function(code, params, frame, limits = tab_catalog()$limits) {
  .tab_refused_with(code, ab_read(.ab_task(params, frame, limits = limits)))
}

.ab_close <- function(got, want, tol = 1e-10) max(abs(got - want) / pmax(abs(want), 1e-300))

# A table of rows of units, as the node reads it: some units with several rows, some NULLs, some crossover.
.ab_rows <- function(seed = 7, n_units = 120, covariate = FALSE) {
  set.seed(seed)
  unit <- sample(seq_len(n_units), 420, replace = TRUE)
  unit[1:4] <- c(5L, 5L, 17L, 17L)
  arm <- c("A", "B", "C")[(seq_len(n_units) %% 3) + 1L]
  v <- arm[unit]
  v[1:4] <- c("A", "B", "B", "C")  # two units that saw two variants each
  df <- data.frame(id = unit, v = v, y = round(stats::rexp(420, 0.2), 3), stringsAsFactors = FALSE)
  df$y[sample(420, 15)] <- NA
  df$day <- as.Date("2026-04-01") + sample(0:9, 420, replace = TRUE)
  df$conv <- as.numeric(stats::runif(420) < 0.2)
  df$conv[is.na(df$y)] <- NA
  if (covariate) {
    xu <- stats::rnorm(n_units, 10, 3)
    xu[sample(n_units, 25)] <- NA
    df$x <- xu[unit]
  }
  df
}

# The units as a long computation sees them: a loop over the ids.
.ab_brute <- function(df, metric = "y", type = "continuous") {
  out <- list()
  for (id in unique(df$id)) {
    rows <- df[df$id == id, , drop = FALSE]
    if (length(unique(rows$v)) > 1L) { out[[length(out) + 1L]] <- list(id = id, cross = TRUE); next }
    ok <- !is.na(rows[[metric]])
    y <- if (!any(ok)) NA_real_ else if (type == "binary") as.numeric(any(rows[[metric]][ok] > 0)) else sum(rows[[metric]][ok])
    out[[length(out) + 1L]] <- list(id = id, cross = FALSE, v = rows$v[[1L]], y = y,
                                    day = if ("day" %in% names(rows) && any(ok)) min(as.numeric(rows$day[ok])) else NA_real_,
                                    x = if ("x" %in% names(rows)) rows$x[[1L]] else NA_real_)
  }
  list(cross = vapply(out, `[[`, TRUE, "cross"), v = vapply(out, function(z) if (z$cross) NA_character_ else z$v, ""),
       y = vapply(out, function(z) if (z$cross) NA_real_ else z$y, 0), day = vapply(out, function(z) if (z$cross) NA_real_ else z$day, 0),
       x = vapply(out, function(z) if (z$cross) NA_real_ else z$x, 0))
}

# ── the refusals ─────────────────────────────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: the input's rules, each refusal with its sentence on a tiny table"]] <- function(code) {
  df <- .ab_tiny()
  base <- list(metric_type = "binary", metric = "y")
  d <- ab_read(.ab_task(base, df))
  .tab_expect(identical(d$n, c(3L, 3L)) && identical(d$variants$labels, c("A", "B")) && d$variants$control == 1L, "the valid table")
  say <- function(code, params = base, frame = df, ...) .ab_say(code, utils::modifyList(params, list(...)), frame)
  m <- say("control_missing", control = "Z")
  .tab_expect(m == "the control «Z» has no row: check the value, or the filter before the node", m)
  one <- df
  one$v <- "A"
  m <- say("one_variant", frame = one)
  .tab_expect(m == "only the control has rows: an experiment needs at least one treatment", m)
  three <- rbind(df, data.frame(v = c("C", "C"), y = c(0, 1), u = c("g", "h"), x = c(7, 8)))
  m <- .ab_say("too_many_variants", base, three, limits = .ab_limits(max_variants = 2))
  .tab_expect(m == "the column «v» has 3 values; at most 2: filter the variants you compare", m)
  m <- say("share_without_rows", expected_shares = list(A = 1, B = 1, C = 2))
  .tab_expect(m == "the variant «C» has an expected share but no row", m)
  m <- say("share_missing", frame = three, expected_shares = list(A = 1, B = 1))
  .tab_expect(m == "«C» has rows but no expected share: give a share for every variant, or none", m)
  bad <- df
  bad$y[2] <- 2
  m <- say("binary_values", frame = bad)
  .tab_expect(m == "a binary metric is true/false or 0/1: the column «y» has 2", m)
  boolean <- df
  boolean$y <- boolean$y > 0
  m <- say("binary_boolean_continuous", list(metric_type = "continuous", metric = "y"), boolean)
  .tab_expect(m == "a true/false metric is a conversion: choose the type binary", m)
  .tab_expect(ab_read(.ab_task(base, boolean))$n_assigned[[1L]] == 3L, "a true/false binary metric")
  few <- df[-(5:6), ]
  m <- say("few_units", frame = few)
  .tab_expect(m == "the variant «B» has 1 unit: each variant needs at least 2", m)
  # a covariate that varies in a unit, that nobody has, that a variant lacks
  vary <- rbind(df, data.frame(v = "A", y = 1, u = "a", x = 9))
  m <- say("covariate_varies", list(metric_type = "binary", metric = "y", unit = "u", covariate = "x"), vary)
  .tab_expect(m == "the unit a has two values of the covariate: a pre-experiment value is one per unit", m)
  none <- df
  none$x <- NA_real_
  m <- say("covariate_all_missing", list(metric_type = "binary", metric = "y", covariate = "x"), none)
  .tab_expect(m == "no unit has a value of «x»: CUPED needs the covariate on some units", m)
  lacks <- df
  lacks$x[4:6] <- NA
  m <- say("covariate_variant_empty", list(metric_type = "binary", metric = "y", covariate = "x"), lacks)
  .tab_expect(grepl("no unit of the variant «B» has a value of «x»", m, fixed = TRUE) && grepl("leave them out", m, fixed = TRUE), m)
  # the empty covariates left out, on the same table: the variant has no unit then
  m <- say("few_units", list(metric_type = "binary", metric = "y", covariate = "x", covariate_missing = "exclude"), lacks)
  .tab_expect(m == "the variant «B» has 0 units: each variant needs at least 2", m)
  # the ratio
  ratio <- data.frame(v = df$v, a = c(1, 2, 3, 0, 0, 0), b = c(10, 20, 30, 0, 0, 0))
  m <- .ab_say("denominator_zero", list(metric_type = "ratio", numerator = "a", denominator = "b"), ratio)
  .tab_expect(m == "the variant «B»'s denominators sum to 0: the ratio has no value", m)
  m <- .ab_say("ratio_needs_units", list(metric_type = "ratio", input_shape = "aggregated"), ratio)
  .tab_expect(grepl("needs one row per unit", m, fixed = TRUE), m)
  # the aggregated rows
  agg <- data.frame(v = c("A", "A", "B", "B"), n = c(100, 100, 100, 100), k = c(10, 120, 8, 9), q = c(10, 120, 8, 9))
  ap <- list(metric_type = "binary", input_shape = "aggregated", units_count = "n", metric_sum = "k")
  m <- .ab_say("aggregated_counts", ap, agg)
  .tab_expect(m == "units and conversions are whole numbers, conversions at most units: the row 2 has 120 conversions of 100 units", m)
  agg$k[2] <- 12.5
  m <- .ab_say("aggregated_counts", ap, agg)
  .tab_expect(grepl("the row 2 has 12.5 conversions of 100 units", m, fixed = TRUE), m)
  cont <- data.frame(v = c("A", "A", "B", "B"), n = c(4, 4, 4, 4), s = c(8, 10, 9, 11), q = c(20, 26, 21, 31))
  cp <- list(metric_type = "continuous", input_shape = "aggregated", units_count = "n", metric_sum = "s", metric_sum_squares = "q")
  .tab_expect(ab_read(.ab_task(cp, cont))$n_assigned[[1L]] == 8, "the valid aggregated rows")
  cont$q[3] <- 15
  m <- .ab_say("aggregated_squares", cp, cont)
  .tab_expect(m == "the row 3's sum of squares is below its sum squared over its units: the sums do not come from the same values", m)
  # the columns
  m <- say("missing_column", metric = "nothing")
  .tab_expect(m == "the table has no column «nothing»", m)
  inf <- df
  inf$y[1] <- Inf
  m <- say("infinite", frame = inf)
  .tab_expect(grepl("«y» holds an infinite value", m, fixed = TRUE), m)
  text <- df
  text$y <- as.character(text$y)
  m <- say("column_type", frame = text)
  .tab_expect(grepl("the column «y» is text", m, fixed = TRUE), m)
}

# ── the unit collapse ────────────────────────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: the unit collapse (any, sum, first day), the crossover, the NULL rows, against a loop over the units"]] <- function(code) {
  df <- .ab_rows()
  for (type in c("continuous", "binary")) {
    metric <- if (type == "binary") "conv" else "y"
    params <- list(metric_type = type, metric = metric, unit = "id", date = "day")
    d <- ab_read(.ab_task(params, df))
    b <- .ab_brute(df, metric, type)
    ok <- !b$cross & !is.na(b$y) & !is.na(b$day)
    .tab_expect(d$n_crossover == sum(b$cross), type, ": crossover ", d$n_crossover, " against ", sum(b$cross))
    .tab_expect(identical(d$n_assigned, tabulate(match(b$v[!b$cross], c("A", "B", "C")), 3L)), type, ": assigned units")
    # a unit with only NULL rows is assigned, not used
    .tab_expect(identical(d$n, tabulate(match(b$v[ok], c("A", "B", "C")), 3L)), type, ": used units")
    .tab_expect(identical(d$n_missing, d$n_assigned - d$n), type, ": n_missing")
    for (j in 1:3) {
      y <- b$y[ok & b$v == c("A", "B", "C")[[j]]]
      .tab_expect(.ab_close(ab_mean(d)[[j]], mean(y)) < 1e-12, type, ": mean of ", j)
      .tab_expect(.ab_close(ab_variance(d)[[j]], stats::var(y)) < 1e-10, type, ": variance of ", j)
      if (type == "binary") .tab_expect(d$totals[j, "sum_y"] == sum(y), "the conversions are exact counts")
    }
    # the daily counts: a unit counts on its first day
    day <- b$day[ok] - min(b$day[ok]) + 1
    want <- table(factor(b$v[ok], c("A", "B", "C")), factor(day, seq_len(d$days$D)))
    .tab_expect(all(d$daily_units == unclass(want)), type, ": units per variant and day")
    .tab_expect(d$days$first == min(b$day[ok]) && d$days$D == 10L && d$days$period == 1, "the day axis")
  }
  .tab_expect(identical(d$notes$crossover_excluded, paste0("@crossover_excluded units=", sum(b$cross))) && sum(b$cross) >= 1L, d$notes$crossover_excluded)
  .tab_expect(grepl("^@metric_missing count=[0-9]+ variants=[ABC,]+$", d$notes$metric_missing), d$notes$metric_missing)
  .tab_expect(d$n_unassigned_rows == 0L, "no row without a variant")
  # without a unit, every row is a unit: the rows with a NULL are left out and counted
  flat <- df[, c("v", "y")]
  flat$v[c(3, 9)] <- NA
  d <- ab_read(.ab_task(list(metric_type = "continuous", metric = "y"), flat))
  keep <- !is.na(flat$v) & !is.na(flat$y)
  .tab_expect(sum(d$n) == sum(keep) && d$n_unassigned_rows == 2L, "rows as units")
  .tab_expect(sum(d$n_assigned) == sum(!is.na(flat$v)) && sum(d$n_missing) == sum(!is.na(flat$v) & is.na(flat$y)), "assigned and missing")
  .tab_expect(.ab_close(d$totals[, "sum_y"], as.numeric(tapply(flat$y[keep], flat$v[keep], sum))) < 1e-12, "sums by variant")
  # a ratio: the numerator and the denominator are summed per unit, the denominators of a variant must not sum to 0
  r <- data.frame(id = c(1, 1, 2, 3, 3, 4, 5, 6), v = c("A", "A", "A", "A", "B", "B", "B", "B"), a = c(1, 2, 3, 4, 5, 6, 7, 8),
                  b = c(10, 10, 20, 30, 40, 50, 60, 70))
  r$v[5] <- "B"
  r$v[4] <- "B"
  d <- ab_read(.ab_task(list(metric_type = "ratio", numerator = "a", denominator = "b", unit = "id"), r))
  .tab_expect(d$n_crossover == 0L && identical(d$n, c(2L, 4L)), "the ratio's units")
  .tab_expect(identical(unname(d$totals[, "sum_y"]), c(6, 30)) && identical(unname(d$totals[, "sum_x"]), c(40, 250)), "the ratio's sums")
  # a 64-bit id is never a double: two ids that differ in their last digits are two units
  ids <- bit64::as.integer64(c("9007199254740993", "9007199254740992", "9007199254740993", "9007199254740994", "9007199254740995", "9007199254740995"))
  big <- data.frame(v = c("A", "A", "A", "B", "B", "B"), y = c(1, 2, 3, 4, 5, 6))
  big$id <- ids
  d <- ab_read(.ab_task(list(metric_type = "continuous", metric = "y", unit = "id"), big))
  .tab_expect(identical(d$n, c(2L, 2L)) && d$n_crossover == 0L, "the 64-bit ids: ", paste(d$n, collapse = " "))
  .tab_expect(identical(unname(d$totals[, "sum_y"]), c(6, 15)), "the 64-bit ids' sums")
  big$id <- bit64::as.integer64(c("1", "2", "3", "4", "5", "6"))
  big$id[1] <- bit64::as.integer64("5")  # a unit in two variants, with an id that fits a double
  d <- ab_read(.ab_task(list(metric_type = "continuous", metric = "y", unit = "id"), big))
  .tab_expect(d$n_crossover == 1L, "a crossover with an Int64 id")
}

# ── aggregated = the unit rows summed ────────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: aggregated rows give the numbers of the unit rows they were summed from"]] <- function(code) {
  set.seed(11)
  n <- 600
  u <- data.frame(v = sample(c("A", "B", "C"), n, TRUE, prob = c(0.5, 0.3, 0.2)), day = as.Date("2026-05-01") + sample(0:13, n, TRUE),
                  y = round(stats::rexp(n, 0.05), 2), stringsAsFactors = FALSE)
  u$conv <- as.numeric(stats::runif(n) < 0.3)
  key <- paste(u$v, u$day)
  rows <- do.call(rbind, lapply(split(u, key), function(z) data.frame(v = z$v[[1L]], day = z$day[[1L]], n = nrow(z), k = sum(z$conv), s = sum(z$y), q = sum(z$y^2))))
  rows <- rows[sample(nrow(rows)), ]
  for (type in c("binary", "continuous")) {
    metric <- if (type == "binary") "conv" else "y"
    unit_d <- ab_read(.ab_task(list(metric_type = type, metric = metric, date = "day"), u))
    ap <- list(metric_type = type, input_shape = "aggregated", units_count = "n", metric_sum = if (type == "binary") "k" else "s", date = "day")
    if (type == "continuous") ap$metric_sum_squares <- "q"
    agg_d <- ab_read(.ab_task(ap, rows))
    .tab_expect(identical(as.integer(agg_d$n), as.integer(unit_d$n)) && all(agg_d$n_assigned == unit_d$n_assigned), type, ": units")
    .tab_expect(.ab_close(agg_d$totals[, "sum_y"], unit_d$totals[, "sum_y"]) < 1e-12, type, ": sums")
    .tab_expect(.ab_close(ab_variance(agg_d), ab_variance(unit_d)) < 1e-9, type, ": variances ", paste(ab_variance(agg_d), ab_variance(unit_d)))
    .tab_expect(all(agg_d$daily_units == unit_d$daily_units) && identical(dim(agg_d$daily_units), dim(unit_d$daily_units)), type, ": units per variant and day")
    .tab_expect(agg_d$days$first == unit_d$days$first && agg_d$days$D == unit_d$days$D, type, ": days")
    .tab_expect(.ab_close(agg_d$srm$p_value, unit_d$srm$p_value) < 1e-12, type, ": SRM")
    # the cells themselves, one per variant and day
    .tab_expect(identical(as.integer(agg_d$cells$variant), as.integer(unit_d$cells$variant)) &&
                identical(as.integer(agg_d$cells$day), as.integer(unit_d$cells$day)), type, ": cells")
    .tab_expect(.ab_close(agg_d$cells$sums[, "sum_y"], unit_d$cells$sums[, "sum_y"]) < 1e-12, type, ": cell sums")
  }
  # without a date: one cell per variant
  nod <- ab_read(.ab_task(list(metric_type = "binary", input_shape = "aggregated", units_count = "n", metric_sum = "k"), rows))
  .tab_expect(nrow(nod$cells$sums) == 3L && nod$days$D == 1L, "no date, one cell per variant")
  # a NULL in a row leaves the row out, counted
  rows$k[2] <- NA
  nul <- ab_read(.ab_task(list(metric_type = "binary", input_shape = "aggregated", units_count = "n", metric_sum = "k"), rows))
  .tab_expect(sum(nul$n) == sum(rows$n[-2]) && nul$n_missing_rows == 1L && grepl("^@metric_missing count=1 variants=", nul$notes$metric_missing), "a NULL row")
}

# ── the covariate ────────────────────────────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: an empty covariate is filled with the mean of its own variant, or the unit is left out; SRM counts every unit"]] <- function(code) {
  df <- .ab_rows(covariate = TRUE)
  df$y[is.na(df$y)] <- 1
  df <- df[!df$id %in% c(5L, 17L), ]
  params <- list(metric_type = "continuous", metric = "y", unit = "id", covariate = "x")
  fill <- ab_read(.ab_task(c(params, list(covariate_missing = "fill")), df))
  excl <- ab_read(.ab_task(c(params, list(covariate_missing = "exclude")), df))
  b <- .ab_brute(df)
  labs <- c("A", "B", "C")
  given <- !is.na(b$x)
  want_fill <- vapply(labs, function(l) mean(b$x[b$v == l & given]), 0)
  .tab_expect(.ab_close(fill$covariate$fill_value, want_fill) < 1e-13, "the fill values: ", paste(fill$covariate$fill_value, collapse = " "))
  .tab_expect(identical(fill$covariate$n_filled, tabulate(match(b$v[!given], labs), 3L)), "the units filled")
  .tab_expect(identical(excl$covariate$n_excluded, fill$covariate$n_filled) && all(excl$covariate$n_filled == 0L), "the units left out")
  .tab_expect(identical(fill$n, fill$n_used) && identical(excl$n, fill$n - fill$covariate$n_filled), "the units compared")
  .tab_expect(identical(fill$n_assigned, excl$n_assigned) && identical(fill$srm$p_value, excl$srm$p_value), "SRM counts every assigned unit")
  .tab_expect(identical(fill$covariate$n_given + fill$covariate$n_empty, fill$n_used), "given and empty")
  # theta's ingredients come from the units that have a covariate only
  for (j in 1:3) {
    ok <- b$v == labs[[j]] & given
    own_cov <- stats::cov(b$y[ok], b$x[ok])
    .tab_expect(.ab_close(ab_variance(fill, "y", "c", own = TRUE)[[j]], own_cov) < 1e-10, "own covariance ", j)
    .tab_expect(.ab_close(ab_variance(fill, "c", "c", own = TRUE)[[j]], stats::var(b$x[ok])) < 1e-10, "own variance of the covariate ", j)
    .tab_expect(fill$totals_own[j, "n"] == sum(ok), "own count ", j)
    # the filled column: the given values and the mean for the empty
    xf <- ifelse(given, b$x, want_fill[b$v])
    keep <- b$v == labs[[j]]
    .tab_expect(.ab_close(ab_variance(fill, "c", "c")[[j]], stats::var(xf[keep])) < 1e-10, "variance of the filled covariate ", j)
    .tab_expect(.ab_close(ab_variance(fill, "y", "c")[[j]], stats::cov(b$y[keep], xf[keep])) < 1e-10, "covariance with the filled covariate ", j)
    .tab_expect(.ab_close(ab_variance(excl, "y", "c")[[j]], own_cov) < 1e-10, "excluded: covariance ", j)
  }
  .tab_expect(is.null(excl$totals_own), "with `exclude` every unit has its own covariate")
  .tab_expect(grepl("^@covariate_filled variant=A count=[0-9]+ share=[0-9.]+ mean=", fill$notes$covariate_filled_1) &&
              grepl("^@covariate_excluded variant=A count=[0-9]+ share=", excl$notes$covariate_excluded_1), "the notes")
  # nobody missing: no «own» table, the fill values are the variants' means
  full <- df
  full$x[is.na(full$x)] <- 0
  f <- ab_read(.ab_task(c(params, list(covariate_missing = "fill")), full))
  .tab_expect(is.null(f$totals_own) && all(f$covariate$n_filled == 0L) && length(f$notes) == 0L, "a covariate without empty values")
}

# ── the cap, the quantile, the histogram ─────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: the cap is a value of the data (quantile type 1), its counts; the histogram counts every unit"]] <- function(code) {
  set.seed(3)
  for (n in c(1, 2, 3, 7, 10, 99, 100, 101, 1000)) {
    x <- round(stats::rexp(n), 1)
    for (p in c(0.005, 0.25, 0.5, 0.9, 0.99, 0.995, 0.999, 1)) {
      .tab_expect(ab_quantile1(x, p) == unname(stats::quantile(x, p, type = 1)), "quantile ", p, " of ", n)
    }
  }
  .tab_expect(all(ab_quantile1(1:10, c(0.2, 0.6)) == stats::quantile(1:10, c(0.2, 0.6), type = 1)), "two quantiles at once")
  n <- 500
  df <- data.frame(v = rep(c("A", "B"), length.out = n), y = round(c(stats::rexp(n - 3, 0.1), 900, 1200, 5000), 2), stringsAsFactors = FALSE)
  d <- ab_read(.ab_task(list(metric_type = "continuous", metric = "y", cap_percentile = 0.99), df))
  c0 <- unname(stats::quantile(df$y, 0.99, type = 1))
  .tab_expect(d$cap$value == c0 && c0 %in% df$y, "the cap value ", d$cap$value, " against ", c0)
  .tab_expect(identical(d$cap$n_capped, as.integer(table(factor(df$v[df$y > c0], c("A", "B"))))), "the counts per variant")
  capped <- pmin(df$y, c0)
  .tab_expect(.ab_close(d$totals[, "sum_y"], as.numeric(tapply(capped, df$v, sum))) < 1e-12, "the sums are of the capped values")
  .tab_expect(identical(d$notes$cap_applied, paste0("@cap_applied percentile=0.99 value=", .ab_nf(c0), " count=", sum(d$cap$n_capped))), d$notes$cap_applied)
  .tab_expect(all(rowSums(d$hist$counts) == d$n) && ncol(d$hist$counts) == AB_HIST_BINS + 2L, "the histogram counts every unit")
  .tab_expect(d$hist$breaks[[1L]] == 0 && d$hist$breaks[[AB_HIST_BINS + 1L]] == c0 && all(d$hist$counts[, AB_HIST_BINS + 2L] == 0L), "with a cap: 0 to the cap, no open bin on the right")
  free <- ab_read(.ab_task(list(metric_type = "continuous", metric = "y"), df))
  q <- unname(stats::quantile(df$y, c(0.005, 0.995), type = 1))
  .tab_expect(is.null(free$cap) && free$hist$breaks[[1L]] == q[[1L]] && free$hist$breaks[[AB_HIST_BINS + 1L]] == q[[2L]], "without a cap: the 0.5th and 99.5th percentiles")
  bin <- findInterval(df$y, free$hist$breaks, rightmost.closed = TRUE)
  .tab_expect(all(free$hist$counts == unclass(table(factor(df$v, c("A", "B")), factor(bin, 0:(AB_HIST_BINS + 1L))))), "the histogram's counts")
  # a binary metric has no cap
  b <- ab_read(.ab_task(list(metric_type = "binary", metric = "conv", cap_percentile = 0.9), data.frame(v = df$v, conv = rep(c(0, 1, 1, 0), length.out = n))))
  .tab_expect(is.null(b$cap) && is.null(b$hist), "no cap, no histogram on a binary metric")
  # a metric without spread has no histogram
  flat <- ab_read(.ab_task(list(metric_type = "continuous", metric = "y"), data.frame(v = df$v, y = 3)))
  .tab_expect(is.null(flat$hist), "a constant metric")
}

# ── SRM and the days ─────────────────────────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: SRM is Pearson's chi-squared on the assigned units, the days beyond max_looks are grouped"]] <- function(code) {
  n <- c(5300, 4700)
  s <- ab_srm(n, c(0.5, 0.5), 0.001)
  ref <- stats::chisq.test(n, p = c(0.5, 0.5))
  .tab_expect(.ab_close(s$statistic, unname(ref$statistic)) < 1e-12 && .ab_close(s$p_value, ref$p.value) < 1e-12 && s$df == 1, "two arms")
  .tab_expect(s$verdict == "failed" && ab_srm(c(5010, 4990), c(0.5, 0.5), 0.001)$verdict == "ok", "the verdict is the test's, against srm_alpha")
  s <- ab_srm(c(1010, 1980, 1010), c(0.25, 0.5, 0.25), 0.001)
  ref <- stats::chisq.test(c(1010, 1980, 1010), p = c(0.25, 0.5, 0.25))
  .tab_expect(.ab_close(s$p_value, ref$p.value) < 1e-12 && s$df == 2, "three arms, planned 1:2:1")
  df <- data.frame(v = rep(c("A", "B"), 600), y = rep(c(0, 1, 1), 400), stringsAsFactors = FALSE)
  d <- ab_read(.ab_task(list(metric_type = "binary", metric = "y", expected_shares = list(A = 1, B = 3)), df))
  ref <- stats::chisq.test(c(600, 600), p = c(0.25, 0.75))
  .tab_expect(.ab_close(d$srm$p_value, ref$p.value) < 1e-12 && identical(d$srm$expected_share, c(0.25, 0.75)), "the planned shares are normalized")
  d <- ab_read(.ab_task(list(metric_type = "binary", metric = "y", srm_alpha = 0.5), df))
  .tab_expect(d$srm$verdict == "ok" && d$srm$alpha == 0.5, "srm_alpha")
  # a day axis of 25 days with at most 10 looks: periods of 3 days
  df$day <- as.Date("2026-06-01") + (seq_len(nrow(df)) %% 25)
  small <- c(tab_catalog()$limits[names(tab_catalog()$limits) != "max_looks"], list(max_looks = 10))
  d <- ab_read(.ab_task(list(metric_type = "binary", metric = "y", date = "day"), df, limits = small))
  .tab_expect(d$days$span == 25 && d$days$period == 3 && d$days$D == 9L && ncol(d$daily_units) == 9L, "grouped looks: ", d$days$D)
  .tab_expect(sum(d$daily_units) == 1200 && identical(d$notes$looks_grouped, "@looks_grouped days=25 period=3"), "the note and the counts")
  .tab_expect(all(abs(ab_srm_daily(d)[, 9L] - d$srm$share) < 1e-12), "the cumulative share ends at the observed share")
  d1 <- ab_read(.ab_task(list(metric_type = "binary", metric = "y"), df))
  .tab_expect(d1$days$D == 1L && ncol(d1$daily_units) == 1L && is.na(d1$days$first), "no date: one day")
}

# ── the types an output keeps ────────────────────────────────────────────────────
TAB_COMMON_TESTS[["experiments: the `variants` output's variant has the input column's own type, and joins back to the input"]] <- function(code) {
  skip <- identical(Sys.getenv("TABULARIA_SELFTEST_NO_ARROW"), "1") || !requireNamespace("arrow", quietly = TRUE)
  if (skip) return("skipped")
  raw <- tab_ref_table(file.path(code, "reference", "data", "types.csv"), colClasses = "character", encoding = "UTF-8", stringsAsFactors = FALSE)
  .tab_expect(nrow(raw) == 200L, "types.csv has 200 rows")
  cols <- list(
    v_int64 = list(x = bit64::as.integer64(raw$v_int64), type = arrow::int64(), control = "5000000001"),
    v_int32 = list(x = as.integer(raw$v_int32), type = arrow::int32(), control = "-7"),
    v_bool = list(x = raw$v_bool == "true", type = arrow::boolean(), control = "true"),
    v_text = list(x = raw$v_text, type = arrow::utf8(), control = "control"),
    v_date = list(x = as.Date(raw$v_date), type = arrow::date32(), control = "2026-03-02"),
    v_int16 = list(x = as.integer(raw$v_int32 != "-7") + 1L, type = arrow::int16(), control = "1"))
  path <- tempfile(fileext = ".parquet")
  on.exit({ Sys.unsetenv("TABULARIA_INPUT"); unlink(path) }, add = TRUE)
  tbl <- do.call(arrow::arrow_table, c(lapply(cols, function(z) arrow::Array$create(z$x, type = z$type)), list(converted = as.numeric(raw$converted))))
  arrow::write_parquet(tbl, path)
  Sys.setenv(TABULARIA_INPUT = path)
  for (name in names(cols)) {
    task <- .ab_task(list(variant = name, control = cols[[name]]$control, metric_type = "binary", metric = "converted"), NULL)
    task$.frame <- NULL
    d <- ab_read(task)
    arr <- ab_variant_array(d)
    .tab_expect(arr$type == cols[[name]]$type, name, ": the output's type is ", arr$type$ToString(), ", the input's is ", cols[[name]]$type$ToString())
    got <- arr$as_vector()
    want <- cols[[name]]$x
    .tab_expect(length(got) == d$variants$k && all(match(ab_label(got), ab_label(want)) > 0L), name, ": the values join back to the input")
    .tab_expect(identical(d$variants$labels, tab_sort_labels(ab_label(want))) && d$variants$labels[[d$variants$control]] == cols[[name]]$control, name, ": labels")
    if (name == "v_int64") {
      .tab_expect(inherits(got, "integer64") && any(abs(bit64::as.double.integer64(got)) > 2^31), "an Int64 above 2^31 stays a 64-bit integer: ", paste(ab_label(got), collapse = " "))
      .tab_expect(identical(d$variants$labels, c("4294967303", "5000000001", "5000000002")), "the labels of an Int64: its decimal digits")
    }
    .tab_expect(sum(d$n_assigned) == 200L, name, ": every row is a unit")
  }
}
