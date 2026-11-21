# The reference values of the group «experiments», computed OUTSIDE the runner (docs/specs/ab-testing.md,
# «Self-test, reference values, witnesses»): from the raw rows of each table, with base R's own functions
# (t.test, lm, oneway.test, chisq.test, p.adjust, quantile, var, cov, aggregate), never with the runner's
# code, which reduces the rows to sufficient statistics first. Run it in the group's image, so that the
# versions are the recipe's:
#
#   docker run --rm --network none --user "$(id -u)" \
#     -v "$PWD/gateway-go/internal/modelgroups/groups/experiments/reference:/ref" \
#     tabularia-models-experiments:dev Rscript /ref/make_reference.R [--data]
#
# --data writes the small tables of data/ again (synthetic, fixed seeds, R's default generators); without it
# they are read as they are. It writes expected.json in the format common/R/selftest.R reads ({"format_version":
# 1, "generated", "cases": [{"id", "model", "data", "types", "params", "expect": [{"key", "value" | "text",
# "rel", "abs"}], "refuses"}]}), and per case `reading`: the rows of the explanation and the template id each
# must hold, [key, id] in the order written, which the group's own test (ab_compare_tests.R) compares exactly
# (the numbers inside a line are the runner's 17 digits: they are checked as numbers, through the keys above).
# The Python witness (make_witness.py, witness.json's pins) recomputes these values with SciPy and statsmodels.
#
# Lot E2 (the comparisons): binary_two (z, Fieller), continuous_three (Welch's t = t.test, Holm; the cap at 0.99
# with Benjamini–Hochberg), ratio_four (the delta method, Bonferroni, planned shares), cuped (fill with the
# mean of the unit's own variant, and exclude; θ against lm), cuped_unbalanced (both covariate checks failed),
# srm (a 53/47 split planned 50/50), sessions (the unit collapse, 12 crossover units), aggregated (the summed
# rows give the numbers of the unit rows), edge_* (a control without conversions, a constant metric, Fieller
# unbounded, one variant, names that look like code), types_int (a whole-number variant). Its cases run with
# `bayes` off: the Bayesian and the sequential parts (lots E3, E4) and the planning node (E5) add their own
# cases in make_reference_<part>.R beside this file, sourced in name order at the end (each calls add_case;
# add_expect adds keys to a case). srm.csv has 4,000 rows: a 53/47 split of 2,000 units is not a mismatch at
# 0.001 (χ² = 7.2, p = 0.007). The tables and expected.json travel in the self-test's ConfigMap (at most
# 900 KiB with the runner and the catalogue): keep them small.

here <- dirname(normalizePath(sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE))))
args <- commandArgs(trailingOnly = TRUE)
data_dir <- file.path(here, "data")
REF_SE_COMPARE <- list()  # per case and variant, the standard error the comparisons use (with CUPED, not the variant's own)

# Quoted only where a value or a name holds a comma or a quote: the self-test's ConfigMap holds the tables, at
# most 900 KiB with the runner and the catalogue (groups_test.go), so every byte of them counts.
write_table <- function(df, name, quote = FALSE) {
  dir.create(data_dir, showWarnings = FALSE)
  utils::write.csv(df, file.path(data_dir, name), row.names = FALSE, na = "", quote = quote, fileEncoding = "UTF-8")
}

read_table <- function(name, types) {
  df <- utils::read.csv(file.path(data_dir, name), colClasses = "character", check.names = FALSE, na.strings = "",
                        encoding = "UTF-8", stringsAsFactors = FALSE)
  for (col in names(types)) {
    df[[col]] <- switch(types[[col]], number = as.numeric(df[[col]]), integer = as.integer(df[[col]]), text = df[[col]],
                        date = as.Date(df[[col]]), boolean = tolower(df[[col]]) %in% c("true", "1"))
  }
  df
}

# ── the tables (lot E2) ──────────────────────────────────────────────────────────
make_data_e2 <- function() {
  day0 <- as.Date("2026-03-02")

  set.seed(20261121)
  n <- 4000
  v <- ifelse(stats::runif(n) < 0.5, "A", "B")
  write_table(data.frame(variant = v, converted = as.integer(stats::runif(n) < ifelse(v == "A", 0.10, 0.125)),
                         day = day0 + sample(0:19, n, replace = TRUE)), "binary_two.csv")

  set.seed(20261122)
  n <- 3000
  v <- sample(c("A", "B", "C"), n, replace = TRUE)
  bought <- stats::runif(n) < 0.3
  amount <- stats::rlnorm(n, 3.5, 1) * c(A = 1, B = 1.15, C = 0.95)[v]
  bulk <- stats::runif(n) < 0.01
  amount[bulk] <- amount[bulk] * (1 + 20 * stats::runif(sum(bulk))^-0.6)
  write_table(data.frame(variant = v, revenue = round(ifelse(bought, amount, 0), 2)), "continuous_three.csv")

  set.seed(20261123)
  n <- 2000
  v <- sample(c("A", "B", "C", "D"), n, replace = TRUE)
  impressions <- 1 + stats::rpois(n, 15)
  ctr <- pmin(1, 0.05 * stats::rgamma(n, 2, 2) * c(A = 1, B = 1.15, C = 0.9, D = 1)[v])
  clicks_pre <- stats::rbinom(n, 1 + stats::rpois(n, 15), pmin(1, ctr / c(A = 1, B = 1.15, C = 0.9, D = 1)[v]))
  clicks_pre[stats::runif(n) < 0.1] <- NA
  write_table(data.frame(variant = v, clicks = stats::rbinom(n, impressions, ctr), impressions = impressions, clicks_pre = clicks_pre),
              "ratio_four.csv")

  set.seed(20261124)
  n <- 2000
  v <- ifelse(stats::runif(n) < 0.5, "A", "B")
  pre <- stats::rgamma(n, 4, 0.2)
  y <- 20 + 0.75 * (pre - 20) + stats::rnorm(n, 0, 7.5) + ifelse(v == "B", 1.2, 0)
  pre[stats::runif(n) < 0.15] <- NA
  write_table(data.frame(variant = v, revenue = round(y, 2), revenue_pre = round(pre, 2)), "cuped.csv")

  set.seed(20261125)
  n <- 1000
  v <- ifelse(stats::runif(n) < 0.5, "A", "B")
  pre <- stats::rgamma(n, 4, 0.2)
  y <- 20 + 0.75 * (pre - 20) + stats::rnorm(n, 0, 7.5) + ifelse(v == "B", 1, 0)
  after <- pre + ifelse(v == "B", 3, 0)  # measured after the start: the treatment moved it
  after[stats::runif(n) < ifelse(v == "B", 0.30, 0.05)] <- NA
  write_table(data.frame(variant = v, revenue = round(y, 2), revenue_pre = round(after, 2)), "cuped_unbalanced.csv")

  set.seed(20261126)
  v <- sample(rep(c("A", "B"), c(2120, 1880)))
  visits <- stats::rpois(4000, 3)
  write_table(data.frame(variant = v, converted = as.integer(stats::runif(4000) < 0.05 + 0.02 * pmin(visits, 6)), visits = visits), "srm.csv")

  set.seed(20261127)
  units <- 1000
  arm <- ifelse(stats::runif(units) < 0.5, "A", "B")
  unit <- c(seq_len(units), sample(units, 2000, replace = TRUE))
  v <- arm[unit]
  cross <- sample(units, 12)
  extra <- data.frame(unit = cross, variant = ifelse(arm[cross] == "A", "B", "A"))
  rows <- data.frame(unit = unit, variant = v)
  rows[sample(nrow(rows), 12), ] <- extra  # 12 rows of 12 units in the other variant: 12 crossover units
  rows$revenue <- round(stats::rexp(nrow(rows), 1 / 8) * ifelse(rows$variant == "B", 1.1, 1), 2)
  rows$revenue[sample(nrow(rows), 30)] <- NA
  write_table(rows[sample(nrow(rows)), ], "sessions.csv")

  set.seed(20261128)
  n <- 1400
  v <- ifelse(stats::runif(n) < 0.5, "A", "B")
  conv <- stats::runif(n) < ifelse(v == "A", 0.08, 0.1)
  revenue <- round(ifelse(conv, stats::rlnorm(n, 3, 0.8), 0), 2)
  day <- day0 + sample(0:27, n, replace = TRUE)
  write_table(data.frame(variant = v, day = day, converted = as.integer(conv), revenue = revenue), "aggregated_units.csv")
  key <- paste(v, day)
  agg <- data.frame(variant = tapply(v, key, `[`, 1), day = as.Date(tapply(as.numeric(day), key, `[`, 1)),
                    units = as.vector(table(key)), conversions = as.vector(tapply(as.integer(conv), key, sum)),
                    revenue_sum = round(as.vector(tapply(revenue, key, sum)), 2),
                    revenue_squares = round(as.vector(tapply(revenue^2, key, sum)), 4))
  write_table(agg[order(agg$day, agg$variant), ], "aggregated.csv")

  write_table(data.frame(variant = rep(c("A", "B"), each = 20), converted = c(rep(0L, 20), rep(0L, 17), 1L, 1L, 1L)), "edge_control_zero.csv")
  write_table(data.frame(variant = rep(c("A", "B"), each = 5), revenue = 5), "edge_constant.csv")
  set.seed(20261129)
  write_table(data.frame(variant = rep(c("A", "B"), each = 15), profit = round(c(stats::rnorm(15, 0.5, 10), stats::rnorm(15, 3, 10)), 2)),
              "edge_fieller.csv")
  write_table(data.frame(variant = rep("A", 5), converted = c(0L, 1L, 0L, 1L, 1L)), "edge_one_variant.csv")
  set.seed(20261130)
  nm <- data.frame(v = sample(c("ü ñ", "B,1", "50% off"), 45, replace = TRUE))
  nm[["x); system(\"id\")"]] <- round(stats::rnorm(45, 10, 3) + (nm$v == "50% off") * 4, 2)
  names(nm)[[1L]] <- "a`b"
  write_table(nm, "edge_names.csv", quote = TRUE)
}

if ("--data" %in% args) make_data_e2()

# ── the hand computations ────────────────────────────────────────────────────────
# (p[["name"]], never p$name: `$` would match «covariate» to «covariate_missing», «unit» to «units_count»)
DEFAULTS <- list(alternative = "two_sided", alpha = 0.05, correction = "holm", higher_is_better = TRUE, srm_alpha = 0.001,
                 min_lift = 0.02, power = 0.8, covariate_missing = "fill", input_shape = "units")
P_ADJUST <- c(holm = "holm", benjamini_hochberg = "BH", bonferroni = "bonferroni", none = "none")
labels_of <- function(x) sort(unique(x[!is.na(x)]), method = "radix")
stars <- function(p) if (p < 0.001) "***" else if (p < 0.01) "**" else if (p < 0.05) "*" else if (p < 0.1) "." else ""

# The units of the table as the node sees them: one per row, or the rows of a unit together (a binary metric
# true if any row is, a sum otherwise, the first day; a unit in two variants left out).
units_of <- function(df, p) {
  metric <- if (identical(p[["metric_type"]], "ratio")) c(p[["numerator"]], p[["denominator"]]) else p[["metric"]]
  if (is.null(p[["unit"]])) return(list(df = df, crossover = 0L))
  per <- split(df, df[[p[["unit"]]]])
  cross <- vapply(per, function(u) length(unique(u[[p[["variant"]]]])) > 1L, TRUE)
  out <- do.call(rbind, lapply(per[!cross], function(u) {
    ok <- stats::complete.cases(u[, metric, drop = FALSE])
    row <- u[1L, , drop = FALSE]
    for (m in metric) row[[m]] <- if (!any(ok)) NA_real_ else if (identical(p[["metric_type"]], "binary")) as.numeric(any(u[[m]][ok] > 0)) else sum(u[[m]][ok])
    row
  }))
  list(df = out, crossover = sum(cross))
}

fieller <- function(mt, vt, mc, vc, q) {
  g <- q^2 * vc / mc^2
  if (!(g < 1)) return(c(NA_real_, NA_real_))
  r <- mt / mc
  h <- (q / abs(mc)) * sqrt(vt * (1 - g) + r^2 * vc)
  c((r - h) / (1 - g) - 1, (r + h) / (1 - g) - 1)
}

# One case of ab_test, by hand: the expected keys and the reading's ids.
ab_case <- function(id, data, types, params, note = NULL) {
  p <- utils::modifyList(DEFAULTS, params)
  raw <- read_table(data, types)
  ex <- list()
  # the runner's sums against these raw rows: measured within 7e-13 relative on every key; the self-test's default
  # tolerance (1e-9 relative, 1e-12 absolute) but for Welch's t, at the spec's 1e-12 (written only where it differs)
  num <- function(key, value, rel = NULL) {
    e <- list(key = key, value = if (is.na(value)) NULL else value)
    if (!is.null(rel)) e$rel <- rel
    ex[[length(ex) + 1L]] <<- e
  }
  txt <- function(key, text) ex[[length(ex) + 1L]] <<- list(key = key, text = text)
  type <- p[["metric_type"]]
  aggregated <- identical(p[["input_shape"]], "aggregated")
  vcol <- p[["variant"]]
  # the units, their variant, the metric (y), the denominator (x), the covariate (cv)
  if (aggregated) {
    ok <- stats::complete.cases(raw[, c(vcol, p[["units_count"]], p[["metric_sum"]], if (type == "continuous") p[["metric_sum_squares"]])])
    a <- raw[ok, ]
    labels <- labels_of(a[[vcol]])
    n_assigned <- vapply(labels, function(l) sum(a[[p[["units_count"]]]][a[[vcol]] == l]), 0)
    units <- NULL
    crossover <- 0L
    n_missing <- rep(0, length(labels))
  } else {
    raw <- raw[!is.na(raw[[vcol]]), ]
    u <- units_of(raw, p)
    units <- u$df
    crossover <- u$crossover
    labels <- labels_of(units[[vcol]])
    n_assigned <- vapply(labels, function(l) sum(units[[vcol]] == l), 0)
    mcols <- if (type == "ratio") c(p[["numerator"]], p[["denominator"]]) else p[["metric"]]
    used <- stats::complete.cases(units[, mcols, drop = FALSE])
    n_missing <- vapply(labels, function(l) sum(units[[vcol]] == l & !used), 0)
    units <- units[used, ]
  }
  k <- length(labels)
  ci <- match(p[["control"]], labels)
  tr <- setdiff(seq_len(k), ci)
  expected <- if (length(p[["expected_shares"]])) unlist(p[["expected_shares"]])[labels] / sum(unlist(p[["expected_shares"]])) else rep(1 / k, k)
  srm <- suppressWarnings(stats::chisq.test(n_assigned, p = expected))
  # the covariate
  cuped <- !is.null(p[["covariate"]])
  if (cuped) {
    has <- !is.na(units[[p[["covariate"]]]])
    n_given <- vapply(labels, function(l) sum(units[[vcol]] == l & has), 0)
    n_empty <- vapply(labels, function(l) sum(units[[vcol]] == l & !has), 0)
    if (identical(p[["covariate_missing"]], "exclude")) {
      units <- units[has, ]
      own <- rep(TRUE, nrow(units))
    } else {
      own <- has
      fill <- vapply(labels, function(l) mean(units[[p[["covariate"]]]][units[[vcol]] == l & has]), 0)
      units[[p[["covariate"]]]][!has] <- fill[match(units[[vcol]][!has], labels)]
    }
  }
  # the cap: a value of the data (quantile type 1), over all units
  if (!is.null(p[["cap_percentile"]])) {
    cap <- unname(stats::quantile(units[[p[["metric"]]]], p[["cap_percentile"]], type = 1))
    n_capped <- vapply(labels, function(l) sum(units[[p[["metric"]]]][units[[vcol]] == l] > cap), 0)
    units[[p[["metric"]]]] <- pmin(units[[p[["metric"]]]], cap)
  }
  # m_j, v_j per variant
  welch <- type == "continuous" && !cuped
  if (aggregated) {
    n <- n_assigned
    s <- vapply(labels, function(l) sum(a[[p[["metric_sum"]]]][a[[vcol]] == l]), 0)
    m <- s / n
    if (type == "binary") {
      v <- m * (1 - m) / n
    } else {
      q <- vapply(labels, function(l) sum(a[[p[["metric_sum_squares"]]]][a[[vcol]] == l]), 0)
      v <- (q - s^2 / n) / (n - 1) / n
    }
  } else {
    g <- match(units[[vcol]], labels)
    n <- tabulate(g, k)
    y <- units[[mcols[[1L]]]]
    if (type == "ratio") {
      x <- units[[p[["denominator"]]]]
      m <- vapply(seq_len(k), function(j) sum(y[g == j]) / sum(x[g == j]), 0)
      lin <- (y - m[g] * x) / vapply(seq_len(k), function(j) mean(x[g == j]), 0)[g]   # the linearized metric
      v <- vapply(seq_len(k), function(j) {
        yy <- y[g == j]; xx <- x[g == j]
        (stats::var(yy) - 2 * m[[j]] * stats::cov(yy, xx) + m[[j]]^2 * stats::var(xx)) / (mean(xx)^2 * length(yy))
      }, 0)
    } else {
      m <- vapply(seq_len(k), function(j) mean(y[g == j]), 0)
      v <- if (type == "binary") m * (1 - m) / n else vapply(seq_len(k), function(j) stats::var(y[g == j]) / n[[j]], 0)
      lin <- y
    }
    if (cuped) {
      cv <- units[[p[["covariate"]]]]
      fit <- stats::lm(lin ~ factor(g) + cv, subset = own)  # θ: the common slope, on the units with their own covariate
      theta <- unname(stats::coef(fit)[["cv"]])
      within <- function(z, keep) z[keep] - stats::ave(z[keep], g[keep])
      rho <- sum(within(lin, own) * within(cv, own)) / sqrt(sum(within(lin, own)^2) * sum(within(cv, own)^2))
      raw_m <- m
      raw_v <- v
      raw_s2 <- vapply(seq_len(k), function(j) stats::var(lin[g == j]), 0)
      cbar_j <- vapply(seq_len(k), function(j) mean(cv[g == j]), 0)
      m <- m - theta * (cbar_j - mean(cv))
      s2 <- vapply(seq_len(k), function(j) stats::var(lin[g == j] - theta * cv[g == j]), 0)
      v <- s2 / n
      # a variant's own adjusted mean also carries the covariate's grand mean, estimated too: θ² s²_x / N more
      # (it cancels in a difference, so the comparisons keep v)
      v_level <- v + theta^2 * stats::var(cv) / length(cv)
      num("model|||theta", theta)
      num("model|||covariate_correlation", rho)
      if (type == "continuous") {
        # the adjusted difference is the variant's coefficient in y ~ variant + x, on the units the comparison uses
        ols <- stats::lm(y ~ factor(g, levels = c(ci, tr)) + cv)
        for (i in seq_along(tr)) num(paste0("comparison|", labels[[tr[[i]]]], "|", labels[[ci]], "|difference"), unname(stats::coef(ols)[[i + 1L]]))
      }
      for (j in seq_len(k)) {
        num(paste0("variant|", labels[[j]], "||mean_unadjusted"), raw_m[[j]])
        num(paste0("variant|", labels[[j]], "||std_error_unadjusted"), sqrt(raw_v[[j]]))
        num(paste0("variant|", labels[[j]], "||covariate_mean"), cbar_j[[j]])
        num(paste0("variant|", labels[[j]], "||variance_reduction"), 1 - s2[[j]] / raw_s2[[j]])
        num(paste0("variant|", labels[[j]], "||n_covariate_filled"), if (identical(p[["covariate_missing"]], "fill")) n_empty[[j]] else 0)
        num(paste0("variant|", labels[[j]], "||n_covariate_excluded"), if (identical(p[["covariate_missing"]], "exclude")) n_empty[[j]] else 0)
        if (identical(p[["covariate_missing"]], "fill")) num(paste0("variant|", labels[[j]], "||covariate_fill_value"), fill[[j]])
      }
    }
  }
  # the model and the variants
  txt("model|||metric_type", type)
  num("model|||n", sum(n))
  num("model|||n_assigned", sum(n_assigned))
  num("model|||n_missing", sum(n_missing))
  num("model|||n_variants", k)
  if (!aggregated && !is.null(p[["unit"]])) num("model|||n_crossover", crossover)
  if (!is.null(p[["cap_percentile"]])) {
    num("model|||cap_value", cap)
    num("model|||n_capped", sum(n_capped))
  }
  for (j in seq_len(k)) {
    lv <- paste0("variant|", labels[[j]], "||")
    num(paste0(lv, "n"), n[[j]])
    num(paste0(lv, "n_assigned"), n_assigned[[j]])
    num(paste0(lv, "share"), n_assigned[[j]] / sum(n_assigned))
    num(paste0(lv, "expected_share"), expected[[j]])
    num(paste0(lv, "mean"), m[[j]])
    num(paste0(lv, "std_error"), sqrt(if (cuped) v_level[[j]] else v[[j]]))
    if (type == "binary") num(paste0(lv, "conversions"), round(m[[j]] * n[[j]]))
    if (type == "continuous" && !aggregated) {
      yy <- units[[p[["metric"]]]][g == j]
      num(paste0(lv, "skewness"), mean((yy - mean(yy))^3) / mean((yy - mean(yy))^2)^1.5)
      if (!is.null(p[["cap_percentile"]])) num(paste0(lv, "n_capped"), n_capped[[j]])
    }
    if (type == "ratio") {
      num(paste0(lv, "numerator_sum"), sum(units[[p[["numerator"]]]][g == j]))
      num(paste0(lv, "denominator_sum"), sum(units[[p[["denominator"]]]][g == j]))
    }
  }
  REF_SE_COMPARE[[id]] <<- stats::setNames(sqrt(v), labels)  # the comparisons' own (the Bayesian reference reads them)
  # the comparisons
  m_t <- length(tr)
  # the family: the comparisons with a standard error above 0 (p.adjust counts the p-values that are not NA)
  m_fam <- max(1L, sum(vapply(tr, function(t) sqrt(v[[t]] + v[[ci]]) > 0, TRUE)))
  a_int <- if (p[["correction"]] == "bonferroni") p[["alpha"]] / m_fam else p[["alpha"]]
  a_cmp <- if (p[["correction"]] == "none") p[["alpha"]] else p[["alpha"]] / m_fam
  two <- p[["alternative"]] == "two_sided"
  z_a <- if (two) stats::qnorm(1 - a_cmp / 2) else stats::qnorm(1 - a_cmp)
  res <- lapply(tr, function(t) {
    d <- m[[t]] - m[[ci]]
    se <- sqrt(v[[t]] + v[[ci]])
    if (welch && !aggregated && se > 0) {
      tt <- stats::t.test(units[[p[["metric"]]]][g == t], units[[p[["metric"]]]][g == ci], var.equal = FALSE,
                          alternative = c(two_sided = "two.sided", greater = "greater", less = "less")[[p[["alternative"]]]],
                          conf.level = 1 - a_int)
      stat <- unname(tt$statistic); df <- unname(tt$parameter); pv <- tt$p.value; dci <- unname(tt$conf.int)
      q <- if (two) stats::qt(1 - a_int / 2, df) else stats::qt(1 - a_int, df)
    } else {
      df <- if (welch) (v[[t]] + v[[ci]])^2 / (v[[t]]^2 / (n[[t]] - 1) + v[[ci]]^2 / (n[[ci]] - 1)) else Inf
      stat <- if (se > 0) d / se else NA_real_
      pv <- if (is.na(stat)) NA_real_ else if (two) 2 * stats::pt(-abs(stat), df) else if (p[["alternative"]] == "greater") stats::pt(stat, df, lower.tail = FALSE) else stats::pt(stat, df)
      q <- if (two) stats::qt(1 - a_int / 2, df) else stats::qt(1 - a_int, df)
      dci <- if (is.na(stat)) c(NA_real_, NA_real_) else c(if (p[["alternative"]] == "less") -Inf else d - q * se, if (p[["alternative"]] == "greater") Inf else d + q * se)
    }
    lift <- if (m[[ci]] != 0) m[[t]] / m[[ci]] - 1 else NA_real_
    lci <- if (m[[ci]] != 0 && !is.na(stat)) fieller(m[[t]], v[[t]], m[[ci]], v[[ci]], q) else c(NA_real_, NA_real_)
    if (!two) lci[[if ((p[["alternative"]] == "greater") == (m[[ci]] > 0)) 2L else 1L]] <- NA_real_
    list(t = t, d = d, se = se, stat = stat, df = df, p = pv, q = q, dci = dci, lift = lift, lci = lci,
         lift_se = if (m[[ci]] != 0) sqrt(v[[t]] / m[[ci]]^2 + m[[t]]^2 * v[[ci]] / m[[ci]]^4) else NA_real_)
  })
  padj <- stats::p.adjust(vapply(res, `[[`, 0, "p"), P_ADJUST[[p[["correction"]]]])
  decisions <- character()
  for (i in seq_along(res)) {
    r <- res[[i]]
    lv <- paste0("comparison|", labels[[r$t]], "|", labels[[ci]], "|")
    fin <- function(x) if (is.finite(x)) x else NA_real_
    tw <- if (welch && !aggregated) 1e-12 else NULL  # Welch's t: the spec's tolerance against t.test
    if (!(cuped && type == "continuous")) num(paste0(lv, "difference"), r$d, tw)
    num(paste0(lv, "difference_std_error"), r$se)
    num(paste0(lv, "statistic"), r$stat, tw)
    if (welch) num(paste0(lv, "df"), r$df, tw)
    num(paste0(lv, "p_value"), r$p, tw)
    num(paste0(lv, "p_adjusted"), padj[[i]])
    num(paste0(lv, "difference_ci_low"), fin(r$dci[[1L]]), tw)
    num(paste0(lv, "difference_ci_high"), fin(r$dci[[2L]]), tw)
    num(paste0(lv, "lift"), r$lift)
    num(paste0(lv, "lift_std_error"), r$lift_se)
    num(paste0(lv, "lift_ci_low"), r$lci[[1L]])
    num(paste0(lv, "lift_ci_high"), r$lci[[2L]])
    dec <- if (is.na(padj[[i]])) "not_computed" else if (padj[[i]] < p[["alpha"]]) (if ((r$d > 0) == isTRUE(p[["higher_is_better"]])) "better" else "worse") else "not_significant"
    decisions[[i]] <- dec
    txt(paste0(lv, "decision"), dec)
    if (!is.na(r$lift) && !all(is.na(r$lci))) {
      goal <- if (isTRUE(p[["higher_is_better"]])) p[["min_lift"]] else -p[["min_lift"]]
      lo <- if (is.na(r$lci[[1L]])) -Inf else r$lci[[1L]]
      hi <- if (is.na(r$lci[[2L]])) Inf else r$lci[[2L]]
      txt(paste0(lv, "min_lift_position"), if (lo > goal) "above" else if (hi < goal) "below" else "straddles")
    }
    if (!is.na(r$stat) && m[[ci]] != 0) {
      num(paste0(lv, "mde"), (z_a + stats::qnorm(p[["power"]])) * r$se / abs(m[[ci]]))
      delta <- p[["min_lift"]] * abs(m[[ci]]) / r$se
      num(paste0(lv, "power_at_min_lift"), if (two) stats::pnorm(delta - z_a) + stats::pnorm(-delta - z_a) else stats::pnorm(delta - z_a))
    }
  }
  srm_failed <- srm$p.value < p[["srm_alpha"]]
  under <- if (any(decisions == "better")) "better" else if (any(decisions == "worse")) "worse" else "no_difference"
  txt("model|||decision", if (srm_failed) "srm_failed" else if (under == "no_difference" && !any(decisions == "not_significant")) "not_computed" else under)
  # the checks
  num("assumption|srm||statistic", unname(srm$statistic))
  num("assumption|srm||p_value", srm$p.value)
  txt("assumption|srm||verdict", if (srm_failed) "failed" else "ok")
  if (cuped) {
    keep <- own
    w <- stats::oneway.test(cv ~ factor(g), subset = keep, var.equal = FALSE)
    num("assumption|covariate_balance||statistic", unname(w$statistic))
    num("assumption|covariate_balance||df_2", unname(w$parameter[[2L]]))
    num("assumption|covariate_balance||p_value", w$p.value)
    txt("assumption|covariate_balance||verdict", if (w$p.value < p[["alpha"]]) "failed" else "ok")
    if (sum(n_empty) > 0) {
      x <- stats::chisq.test(rbind(n_empty, n_given), correct = FALSE)
      num("assumption|covariate_missing_balance||statistic", unname(x$statistic))
      num("assumption|covariate_missing_balance||p_value", x$p.value)
      txt("assumption|covariate_missing_balance||verdict", if (x$p.value < p[["alpha"]]) "failed" else "ok")
    }
  }
  if (!aggregated && !is.null(p[["unit"]])) {
    num("assumption|crossover||count", crossover)
    txt("assumption|crossover||verdict", if (crossover > 0) "failed" else "ok")
  }
  zero <- vapply(seq_len(k), function(j) if (type == "binary") m[[j]] %in% c(0, 1) else !(v[[j]] > 0), TRUE)
  if (cuped) zero <- !(raw_s2 > 0)
  txt("assumption|variance_zero||verdict", if (any(zero)) "failed" else "ok")
  # the reading: which template each row must hold
  reading <- list()
  say <- function(block, level_2, ids) for (i in seq_along(ids)) reading[[length(reading) + 1L]] <<- list(paste0("reading|", block, "|", level_2, "|line_", i), ids[[i]])
  lifts <- vapply(res, function(r) if (is.na(r$lift)) -Inf else if (isTRUE(p[["higher_is_better"]])) r$lift else -r$lift, 0)
  pool <- if (under == "no_difference") seq_along(res) else which(decisions == under)
  pa <- ifelse(is.na(padj), Inf, padj)
  best <- res[[pool[[order(pa[pool], -lifts[pool])[[1L]]]]]]
  say("decision", "", c(if (!is.na(padj[[match(best$t, tr)]])) paste0("decision_", under, if (under != "no_difference" && length(pool) > 1L) "_many"), if (type == "binary") "decision_counts" else "decision_means",
                        if (!any(is.na(best$lci))) "decision_interval"))
  say("srm", "", if (srm_failed) "srm_failed" else "srm_ok")
  for (i in seq_along(res)) {
    if (is.na(padj[[i]])) next
    say("comparison", labels[[res[[i]]$t]], c(if (padj[[i]] < p[["alpha"]]) "comparison_significant" else "comparison_not_significant",
                                              if (m_t > 1) paste0("correction_", p[["correction"]]) else "correction_one"))
  }
  if (cuped) {
    say("cuped", "", c("cuped_reduced", if (identical(p[["covariate_missing"]], "exclude")) { if (sum(n_empty)) "cuped_excluded" } else if (sum(n_empty)) "cuped_filled"))
  }
  if (!is.null(p[["cap_percentile"]])) say("cap", "", "cap_applied")
  if (m[[ci]] != 0 && any(!is.na(vapply(res, `[[`, 0, "stat")))) say("power", "", "power_detectable")
  say("method", "", c("method_decision", "method_comparison", if (type == "ratio") "method_ratio", "method_srm", "method_power", if (cuped) "method_cuped",
                      if (!is.null(p[["cap_percentile"]])) "method_cap"))
  say("formulas", "", c(if (type == "ratio") "formula_delta" else if (welch) "formula_welch" else "formula_z", "formula_fieller",
                        if (m_t > 1 && p[["correction"]] != "none") c(holm = "formula_holm", benjamini_hochberg = "formula_bh", bonferroni = "formula_bonferroni")[[p[["correction"]]]],
                        "formula_srm", if (cuped) "formula_cuped"))
  case <- list(id = id, model = "ab_test", data = file.path("data", data), types = types, params = params, expect = ex, reading = reading)
  if (!is.null(note)) case$note <- note
  case
}

cases <- list()
add_case <- function(case) cases[[length(cases) + 1L]] <<- case
add_expect <- function(id, expect) {
  i <- which(vapply(cases, `[[`, "", "id") == id)
  cases[[i]]$expect <<- c(cases[[i]]$expect, expect)
}
refused <- function(id, data, types, params, code, message) {
  list(id = id, model = "ab_test", data = file.path("data", data), types = types, params = params, expect = list(),
       refuses = list(code = code, message = message))
}

# ── lot E2's cases ───────────────────────────────────────────────────────────────
off <- list(bayes = FALSE, sequential = FALSE)
add_case(ab_case("binary_two", "binary_two.csv", list(variant = "text", converted = "number", day = "date"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", date = "day"), off)))
add_case(ab_case("binary_two_greater", "binary_two.csv", list(variant = "text", converted = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", alternative = "greater",
                        alpha = 0.01, min_lift = 0.1), off)))
add_case(ab_case("continuous_three_holm", "continuous_three.csv", list(variant = "text", revenue = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue"), off),
                 "Welch's t of each treatment equals t.test(var.equal = FALSE) and the engine's stat_t_two_sample"))
add_case(ab_case("continuous_three_cap_bh", "continuous_three.csv", list(variant = "text", revenue = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", cap_percentile = 0.99,
                        correction = "benjamini_hochberg"), off)))
add_case(ab_case("continuous_three_less_none", "continuous_three.csv", list(variant = "text", revenue = "number"),
                 c(list(variant = "variant", control = "B", metric_type = "continuous", metric = "revenue", alternative = "less",
                        correction = "none", higher_is_better = FALSE), off)))
add_case(ab_case("ratio_four", "ratio_four.csv", list(variant = "text", clicks = "number", impressions = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "ratio", numerator = "clicks", denominator = "impressions",
                        correction = "bonferroni", expected_shares = list(A = 1, B = 1, C = 1, D = 1)), off)))
add_case(ab_case("cuped_fill", "cuped.csv", list(variant = "text", revenue = "number", revenue_pre = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", covariate = "revenue_pre"), off)))
add_case(ab_case("cuped_exclude", "cuped.csv", list(variant = "text", revenue = "number", revenue_pre = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", covariate = "revenue_pre",
                        covariate_missing = "exclude"), off)))
add_case(ab_case("cuped_binary", "srm.csv", list(variant = "text", converted = "number", visits = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", covariate = "visits"), off),
                 "a binary metric with CUPED: the normal reference, the sample variances of y − θx"))
add_case(ab_case("cuped_ratio", "ratio_four.csv", list(variant = "text", clicks = "number", impressions = "number", clicks_pre = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "ratio", numerator = "clicks", denominator = "impressions",
                        covariate = "clicks_pre"), off),
                 "a ratio metric with CUPED: the linearized metric adjusted with the unit's covariate"))
add_case(ab_case("cuped_unbalanced", "cuped_unbalanced.csv", list(variant = "text", revenue = "number", revenue_pre = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", covariate = "revenue_pre",
                        covariate_missing = "exclude"), off)))
add_case(ab_case("srm", "srm.csv", list(variant = "text", converted = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted"), off)))
add_case(ab_case("sessions", "sessions.csv", list(unit = "integer", variant = "text", revenue = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", unit = "unit"), off)))
add_case(ab_case("aggregated_binary", "aggregated.csv", list(variant = "text", day = "date", units = "number", conversions = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", input_shape = "aggregated", units_count = "units",
                        metric_sum = "conversions", date = "day"), off)))
add_case(ab_case("aggregated_binary_units", "aggregated_units.csv", list(variant = "text", day = "date", converted = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", date = "day"), off)))
add_case(ab_case("aggregated_continuous", "aggregated.csv",
                 list(variant = "text", units = "number", revenue_sum = "number", revenue_squares = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", input_shape = "aggregated", units_count = "units",
                        metric_sum = "revenue_sum", metric_sum_squares = "revenue_squares"), off)))
add_case(ab_case("aggregated_continuous_units", "aggregated_units.csv", list(variant = "text", revenue = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue"), off)))
add_case(ab_case("edge_control_zero", "edge_control_zero.csv", list(variant = "text", converted = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted"), off),
                 "a control without conversions: the lift and its interval NULL, the difference tested"))
add_case(ab_case("edge_constant", "edge_constant.csv", list(variant = "text", revenue = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue"), off),
                 "a metric with no variance in either variant: not computed"))
add_case(ab_case("edge_fieller", "edge_fieller.csv", list(variant = "text", profit = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "profit"), off),
                 "the control's mean is not told apart from 0: Fieller's set is not bounded"))
add_case(ab_case("edge_names", "edge_names.csv", list("a`b" = "text", "x); system(\"id\")" = "number"),
                 c(list(variant = "a`b", control = "ü ñ", metric_type = "continuous", metric = "x); system(\"id\")"), off),
                 "names that look like code, values with a comma, a space and a percent"))
add_case(ab_case("types_int", "types.csv", list(v_int32 = "integer", converted = "number"),
                 c(list(variant = "v_int32", control = "-7", metric_type = "binary", metric = "converted"), off),
                 "a whole-number variant (lot E1's table of the types): the output keeps its type (make_output_columns.py)"))
add_case(refused("edge_one_variant", "edge_one_variant.csv", list(variant = "text", converted = "number"),
                 list(variant = "variant", control = "A", metric_type = "binary", metric = "converted"), "one_variant",
                 "only the control has rows: an experiment needs at least one treatment"))

# ── the other lots' cases ────────────────────────────────────────────────────────
for (f in sort(list.files(here, pattern = "^make_reference_.+[.]R$", full.names = TRUE))) source(f, local = TRUE)

# expected.json, one line per expected value (the tables and the cases share the self-test's 900 KiB)
j <- function(x) as.character(jsonlite::toJSON(x, auto_unbox = TRUE, null = "null", na = "null", digits = I(17)))
case_text <- function(c) {
  head <- c[setdiff(names(c), c("expect", "reading"))]
  out <- paste0("  ", sub("}$", "", j(head)))
  if (!is.null(c$expect)) out <- paste0(out, ",\n   \"expect\": [", if (length(c$expect)) paste0("\n    ", vapply(c$expect, j, ""), collapse = ",") else "", "]")
  if (!is.null(c$reading)) out <- paste0(out, ",\n   \"reading\": [", paste0("\n    ", vapply(c$reading, function(r) j(unlist(r)), ""), collapse = ","), "]")
  paste0(out, "}")
}
generated <- list(by = "make_reference.R", R = paste(R.version$major, R.version$minor, sep = "."),
                  packages = list(arrow = as.character(utils::packageVersion("arrow")), jsonlite = as.character(utils::packageVersion("jsonlite"))))
text <- paste0("{\"format_version\": 1,\n \"generated\": ", j(generated), ",\n \"cases\": [\n",
               paste(vapply(cases, case_text, ""), collapse = ",\n"), "\n ]}\n")
writeLines(text, file.path(here, "expected.json"), sep = "", useBytes = TRUE)
cat(length(cases), "cases written to expected.json\n")
