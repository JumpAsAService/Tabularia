# Lot E8 (the secondary metrics): its reference cases, sourced by make_reference.R (after E2's cases, with its helpers:
# read_table, write_table, ab_case, add_case, labels_of, fieller, stars, DEFAULTS, P_ADJUST). Every number is computed
# here the long way from the raw rows (docs/specs/ab-testing.md, «Secondary metrics»): each metric's units (the rows
# of a unit together, a unit seen in two variants left out, its value the sum of its rows' values or true if any is,
# a unit without any value left out of that metric only), then t.test(var.equal = FALSE) for a number, the unpooled
# z for a true/false column, Fieller's interval, ONE p.adjust over every metric × every treatment, the decision, the
# detectable lift at alpha / M. The primary's keys are E2's ab_case's, computed without the secondary metrics: the
# self-test finding them in a run with three secondary metrics is the proof that adding them changes nothing of the
# primary. make_witness.py's witness_secondary recomputes the secondary keys with SciPy and statsmodels.
#
# Table: secondary.csv (360 users of three arms in 1 or 2 rows each, two of them in two arms; a conversion, a
# revenue with empty values, a bounce as true/false, the pages seen; B lowers the revenue, C bounces more). Each
# metric is read in its own direction (`secondary_directions`, the primary's where none is given): the bounce, lower
# is better, comes out worse for C beside a primary where higher is better.

make_data_e8 <- function() {
  set.seed(20261181)
  u <- 360
  v <- rep(c("A", "B", "C"), each = u / 3)
  rows <- rep(seq_len(u), 1L + (stats::runif(u) < 0.5))
  m <- length(rows)
  vr <- v[rows]
  vr[rows %in% c(7L, 250L) & duplicated(rows)] <- "C"  # two users seen in two arms
  conv <- as.integer(stats::runif(m) < c(A = 0.3, B = 0.38, C = 0.31)[vr])
  revenue <- round(stats::rgamma(m, 3, 3 / c(A = 45, B = 30, C = 46)[vr]), 1)
  revenue[stats::runif(m) < 0.08] <- NA
  write_table(data.frame(unit = rows, variant = vr, converted = conv, revenue = revenue,
                         bounced = as.integer(stats::runif(m) < c(A = 0.35, B = 0.35, C = 0.6)[vr]), pages = 1L + stats::rpois(m, c(A = 3, B = 3, C = 3.6)[vr])),
              "secondary.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "secondary.csv"))) make_data_e8()

SECONDARY_TYPES <- list(unit = "integer", variant = "text", converted = "number", revenue = "number", bounced = "boolean", pages = "integer")

# One metric's units: list(g = the variant of each unit, y = its value), as the node makes them.
sec_units <- function(raw, p, col, labels, binary) {
  vcol <- p[["variant"]]
  raw <- raw[!is.na(raw[[vcol]]), ]
  if (is.null(p[["unit"]])) {
    keep <- !is.na(raw[[col]])
    y <- as.numeric(raw[[col]][keep])
    return(list(g = match(raw[[vcol]][keep], labels), y = if (binary) as.numeric(y > 0) else y))
  }
  per <- split(raw, raw[[p[["unit"]]]])
  per <- per[vapply(per, function(x) length(unique(x[[vcol]])) == 1L, TRUE)]
  per <- per[vapply(per, function(x) any(!is.na(x[[col]])), TRUE)]
  g <- vapply(per, function(x) match(x[[vcol]][[1L]], labels), 0L)
  y <- vapply(per, function(x) { z <- as.numeric(x[[col]][!is.na(x[[col]])]); if (binary) as.numeric(any(z > 0)) else sum(z) }, 0)
  list(g = unname(g), y = unname(y))
}

sec_case <- function(id, data, types, params, secondary, directions = list(), note = NULL) {
  p <- utils::modifyList(DEFAULTS, params)
  extra <- c(list(secondary_metrics = as.list(secondary)), if (length(directions)) list(secondary_directions = directions))
  case <- ab_case(id, data, types, c(params, extra), note)
  raw <- read_table(data, types)
  labels <- labels_of(raw[[p[["variant"]]]])
  k <- length(labels)
  ci <- match(p[["control"]], labels)
  tr <- setdiff(seq_len(k), ci)
  two <- p[["alternative"]] == "two_sided"
  alt <- c(two_sided = "two.sided", greater = "greater", less = "less")[[p[["alternative"]]]]
  # every comparison's p-value first: the family's size fixes the intervals' level (Bonferroni) and the detectable lift's
  res <- list()
  for (col in secondary) {
    binary <- identical(types[[col]], "boolean")
    u <- sec_units(raw, p, col, labels, binary)
    n <- tabulate(u$g, k)
    m <- vapply(seq_len(k), function(j) mean(u$y[u$g == j]), 0)
    v <- if (binary) m * (1 - m) / n else vapply(seq_len(k), function(j) stats::var(u$y[u$g == j]) / n[[j]], 0)
    for (t in tr) res[[length(res) + 1L]] <- list(col = col, binary = binary, t = t, u = u, m = m, v = v, n = n)
  }
  pv <- vapply(res, function(x) {
    if (x$binary) { z <- (x$m[[x$t]] - x$m[[ci]]) / sqrt(x$v[[x$t]] + x$v[[ci]]); if (two) 2 * stats::pnorm(-abs(z)) else stats::pnorm(z, lower.tail = alt == "less") }
    else stats::t.test(x$u$y[x$u$g == x$t], x$u$y[x$u$g == ci], var.equal = FALSE, alternative = alt)$p.value
  }, 0)
  M <- length(pv)
  padj <- stats::p.adjust(pv, P_ADJUST[[p[["correction"]]]])
  a_int <- if (p[["correction"]] == "bonferroni") p[["alpha"]] / M else p[["alpha"]]
  a_cmp <- if (p[["correction"]] == "none") p[["alpha"]] else p[["alpha"]] / M
  z_a <- if (two) stats::qnorm(1 - a_cmp / 2) else stats::qnorm(1 - a_cmp)
  ex <- list()
  num <- function(key, value, rel = NULL) if (!is.na(value)) ex[[length(ex) + 1L]] <<- c(list(key = key, value = value), if (!is.null(rel)) list(rel = rel))
  txt <- function(key, text) ex[[length(ex) + 1L]] <<- list(key = key, text = text)
  worse <- list()
  for (i in seq_along(res)) {
    x <- res[[i]]
    t <- x$t
    key <- function(stat) paste0("secondary|", x$col, "|", labels[[t]], "|", stat)
    d <- x$m[[t]] - x$m[[ci]]
    se <- sqrt(x$v[[t]] + x$v[[ci]])
    if (x$binary) {
      stat <- d / se
      q <- if (two) stats::qnorm(1 - a_int / 2) else stats::qnorm(1 - a_int)
      dci <- c(if (alt == "less") NA else d - q * se, if (alt == "greater") NA else d + q * se)
      rel <- NULL
    } else {
      tt <- stats::t.test(x$u$y[x$u$g == t], x$u$y[x$u$g == ci], var.equal = FALSE, alternative = alt, conf.level = 1 - a_int)
      stat <- unname(tt$statistic)
      q <- if (two) stats::qt(1 - a_int / 2, tt$parameter) else stats::qt(1 - a_int, tt$parameter)
      dci <- ifelse(is.finite(tt$conf.int), unname(tt$conf.int), NA)
      rel <- 1e-12  # Welch's t against t.test: the spec's tolerance
    }
    lci <- fieller(x$m[[t]], x$v[[t]], x$m[[ci]], x$v[[ci]], q)
    if (!two) lci[[if ((alt == "greater") == (x$m[[ci]] > 0)) 2L else 1L]] <- NA_real_
    num(key("difference"), d)
    num(key("difference_std_error"), se)
    num(key("difference_ci_low"), dci[[1L]], rel)
    num(key("difference_ci_high"), dci[[2L]], rel)
    num(key("lift"), x$m[[t]] / x$m[[ci]] - 1)
    num(key("lift_std_error"), sqrt(x$v[[t]] / x$m[[ci]]^2 + x$m[[t]]^2 * x$v[[ci]] / x$m[[ci]]^4))
    num(key("lift_ci_low"), lci[[1L]])
    num(key("lift_ci_high"), lci[[2L]])
    num(key("statistic"), stat, rel)
    num(key("p_value"), pv[[i]], rel)
    num(key("p_adjusted"), padj[[i]])
    num(key("mde"), (z_a + stats::qnorm(p[["power"]])) * se / abs(x$m[[ci]]))
    num(key("mean_treatment"), x$m[[t]])
    num(key("mean_control"), x$m[[ci]])
    txt(key("significance"), stars(pv[[i]]))
    txt(key("p_adjusted_significance"), stars(padj[[i]]))
    higher <- if (is.null(directions[[x$col]])) isTRUE(p[["higher_is_better"]]) else directions[[x$col]] == "higher"
    dec <- if (padj[[i]] < p[["alpha"]]) (if ((d > 0) == higher) "better" else "worse") else "not_significant"
    txt(key("decision"), dec)
    txt(key("metric_type"), if (x$binary) "binary" else "continuous")
    txt(key("direction"), if (higher) "higher" else "lower")
    if (dec == "worse") worse[[length(worse) + 1L]] <- padj[[i]]
  }
  case$expect <- c(case$expect, ex)
  # the reading: the block after the power's, before the method's; the method's and the formulas' lines appended
  ids <- c(if (M == 1L) "secondary_family_one" else if (p[["correction"]] == "none") "secondary_family_none" else "secondary_family",
           if (length(worse)) rep("secondary_worse", min(5L, length(worse))), if (length(worse) > 5L) "secondary_worse_more",
           if (!length(worse)) "secondary_none_worse")
  block <- lapply(seq_along(ids), function(j) list(paste0("reading|secondary||line_", j), ids[[j]]))
  keys <- vapply(case$reading, `[[`, "", 1L)
  at <- which(startsWith(keys, "reading|method|"))[[1L]]
  rd <- c(case$reading[seq_len(at - 1L)], block, case$reading[at:length(case$reading)])
  primary <- if (p[["metric_type"]] == "ratio") "formula_delta" else if (p[["metric_type"]] == "continuous" && is.null(p[["covariate"]])) "formula_welch" else "formula_z"
  kinds <- vapply(secondary, function(col) identical(types[[col]], "boolean"), TRUE)
  formulas <- setdiff(c(if (any(kinds)) "formula_z", if (any(!kinds)) "formula_welch"), primary)
  if (p[["correction"]] != "none" && M > 1L && length(tr) == 1L) {
    formulas <- c(formulas, c(holm = "formula_holm", benjamini_hochberg = "formula_bh", bonferroni = "formula_bonferroni")[[p[["correction"]]]])
  }
  for (block in c("method", "formulas")) {
    extra <- if (block == "method") "method_secondary" else formulas
    if (!length(extra)) next
    keys <- vapply(rd, `[[`, "", 1L)
    n <- sum(startsWith(keys, paste0("reading|", block, "|")))
    last <- max(which(startsWith(keys, paste0("reading|", block, "|"))))
    rows <- lapply(seq_along(extra), function(j) list(paste0("reading|", block, "||line_", n + j), extra[[j]]))
    rd <- c(rd[seq_len(last)], rows, rd[-seq_len(last)])
  }
  case$reading <- rd
  case
}

off <- list(bayes = FALSE, sequential = FALSE)
add_case(sec_case("secondary_three", "secondary.csv", SECONDARY_TYPES,
                  c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", unit = "unit"), off),
                  c("revenue", "bounced", "pages"), list(bounced = "lower"),
                  note = "three secondary metrics (a number with empty values, a true/false lower is better, a whole number), Holm over 6; the primary's keys are E2's"))
add_case(sec_case("secondary_rows_bonferroni", "secondary.csv", SECONDARY_TYPES,
                  c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "pages", correction = "bonferroni"), off),
                  c("converted", "bounced", "revenue"), list(revenue = "lower", converted = "higher"),
                  note = "one row per unit, a 0/1 number is a mean, Bonferroni's intervals at alpha / 6; a fall of the revenue read as better"))
