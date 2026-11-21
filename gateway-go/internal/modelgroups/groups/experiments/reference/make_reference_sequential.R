# Lot E4 (the sequential test): its reference cases, sourced by make_reference.R (after E2's cases, with its
# helpers: read_table, write_table, ab_case, add_case, refused, P_ADJUST, stars). Every number is computed here the
# long way, from the raw rows of each look (the units that entered up to that day: mean, var, cov, lm for CUPED's
# θ), never from the runner's cumulated sums; then the mixture's log Λ, the running minimum of 1/Λ, the confidence
# sequence intersected over the looks, p.adjust at each look, the first crossing (docs/specs/ab-testing.md,
# «Sequential: always-valid p-values»). make_witness.py's witness_sequential recomputes them in Python.
#
# Tables: sequential.csv (three arms of 10 units a day for 10 days, so every look is balanced: Johari et al.'s
# balanced formula applies; a revenue, a number of views, a covariate with empty values), sequential_long.csv
# (240 units over 2,100 days: the looks grouped in periods of 3 days). The E2 cases' keys are not repeated: a case
# here expects the sequential keys and the reading's ids only.

make_data_e4 <- function() {
  day0 <- as.Date("2026-03-02")
  set.seed(20261141)
  v <- rep(rep(c("A", "B", "C"), each = 10), 10)
  pre <- stats::rgamma(300, 6, 0.3)
  revenue <- 20 + 0.6 * (pre - 20) + stats::rnorm(300, 0, 5) + c(A = 0, B = 6, C = 0.4)[v]
  pre[stats::runif(300) < 0.15] <- NA
  write_table(data.frame(variant = v, day = rep(day0 + 0:9, each = 30), revenue = round(revenue, 1), views = 1 + stats::rpois(300, 4),
                         pre = round(pre, 1)), "sequential.csv")
  set.seed(20261142)
  v <- ifelse(stats::runif(240) < 0.5, "A", "B")
  write_table(data.frame(variant = v, day = day0 + sort(sample(0:2099, 240)), converted = as.integer(stats::runif(240) < ifelse(v == "B", 0.45, 0.3))),
              "sequential_long.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "sequential.csv"))) make_data_e4()

# A lift that changes sign during the experiment (aggregated rows, written by hand: B doubles A's mean on the first two
# days, falls to 0.4 of it on the next two, with 25 times the units): the looks' intervals have no lift in common, the
# confidence sequence is empty (lot E8 closes E4's gap: its sentence, `sequential_empty`).
make_data_drift <- function() {
  one <- function(v, day, n, mean) data.frame(variant = v, day = as.Date("2026-03-02") + day, units = n, s = n * mean, q = (n - 1) * 4 + n * mean^2)
  write_table(rbind(one("A", 0:1, 2000, 10), one("B", 0:1, 2000, 20), one("A", 2:3, 50000, 10), one("B", 2:3, 50000, 4)), "sequential_drift.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "sequential_drift.csv"))) make_data_drift()

SEQ_MAX_LOOKS <- 1000  # the catalogue's max_looks

# The estimate and its variance per variant, on the rows of one look, the long way (E2's formulas).
seq_estimates <- function(u, p, k) {
  type <- p[["metric_type"]]
  g <- u$g
  n <- tabulate(g, k)
  if (identical(p[["input_shape"]], "aggregated")) {
    s <- vapply(seq_len(k), function(j) sum(u$s[g == j]), 0)
    n <- vapply(seq_len(k), function(j) sum(u$n[g == j]), 0)
    m <- s / n
    v <- if (type == "binary") m * (1 - m) / n else (vapply(seq_len(k), function(j) sum(u$q[g == j]), 0) - s^2 / n) / (n - 1) / n
    return(list(m = m, v = v, n = n))
  }
  if (type == "ratio") {
    m <- vapply(seq_len(k), function(j) sum(u$y[g == j]) / sum(u$x[g == j]), 0)
    xbar <- vapply(seq_len(k), function(j) mean(u$x[g == j]), 0)
    lin <- (u$y - m[g] * u$x) / xbar[g]
    v <- vapply(seq_len(k), function(j) stats::var(lin[g == j]) / n[[j]], 0)
  } else {
    m <- vapply(seq_len(k), function(j) mean(u$y[g == j]), 0)
    v <- if (type == "binary") m * (1 - m) / n else vapply(seq_len(k), function(j) stats::var(u$y[g == j]) / n[[j]], 0)
    lin <- u$y
  }
  if (!is.null(u$cv)) {
    cv <- u$cv
    theta <- unname(stats::coef(stats::lm(lin ~ factor(g) + cv, subset = u$own))[["cv"]])
    m <- m - theta * (vapply(seq_len(k), function(j) mean(cv[g == j]), 0) - mean(cv))
    s2 <- vapply(seq_len(k), function(j) stats::var(lin[g == j] - theta * cv[g == j]), 0)
    v <- s2 / n
  }
  list(m = m, v = v, n = n)
}

# The units of a case with their variant and their look; the days axis as E1 makes it.
seq_units <- function(data, types, p) {
  raw <- read_table(data, types)
  vcol <- p[["variant"]]
  raw <- raw[!is.na(raw[[vcol]]), ]
  labels <- labels_of(raw[[vcol]])
  if (identical(p[["input_shape"]], "aggregated")) {
    u <- data.frame(g = match(raw[[vcol]], labels), n = raw[[p[["units_count"]]]], s = raw[[p[["metric_sum"]]]])
    if (!is.null(p[["metric_sum_squares"]])) u$q <- raw[[p[["metric_sum_squares"]]]]
  } else {
    mcols <- if (p[["metric_type"]] == "ratio") c(p[["numerator"]], p[["denominator"]]) else p[["metric"]]
    raw <- raw[stats::complete.cases(raw[, mcols, drop = FALSE]), ]
    u <- data.frame(g = match(raw[[vcol]], labels), y = raw[[mcols[[1L]]]])
    if (p[["metric_type"]] == "ratio") u$x <- raw[[p[["denominator"]]]]
    if (!is.null(p[["covariate"]])) {
      cv <- raw[[p[["covariate"]]]]
      has <- !is.na(cv)
      if (identical(p[["covariate_missing"]], "exclude")) {
        u <- u[has, ]
        raw <- raw[has, ]
        cv <- cv[has]
        has <- has[has]
      } else {
        fill <- vapply(seq_along(labels), function(j) mean(cv[u$g == j & has]), 0)  # today's fill values, at every look
        cv[!has] <- fill[u$g[!has]]
      }
      u$cv <- cv
      u$own <- has
    }
  }
  day <- if (is.null(p[["date"]])) rep(0, nrow(raw)) else as.numeric(raw[[p[["date"]]]])
  first <- min(day)
  span <- max(day) - first + 1
  period <- if (span > SEQ_MAX_LOOKS) ceiling(span / SEQ_MAX_LOOKS) else 1
  u$look <- floor((day - first) / period) + 1
  D <- floor((span - 1) / period) + 1
  list(u = u, labels = labels, D = D, day_of = if (is.null(p[["date"]])) NA_real_ else pmin(first + seq_len(D) * period - 1, max(day)))
}

# One case: the sequential keys, the curves asked for, and the reading's rows (E2's, with this block inserted
# before the power's and the method's and formula's lines appended).
seq_case <- function(id, data, types, params, curves = character(), note = NULL) {
  p <- utils::modifyList(DEFAULTS, params)
  tau <- p[["sequential_tau"]] %||% 0.05
  case <- ab_case(id, data, types, params, note)
  su <- seq_units(data, types, p)
  k <- length(su$labels)
  ci <- match(p[["control"]], su$labels)
  tr <- setdiff(seq_len(k), ci)
  mt <- length(tr)
  a_int <- if (p[["correction"]] == "bonferroni") p[["alpha"]] / mt else p[["alpha"]]
  D <- su$D
  lift <- lv <- mcl <- matrix(NA_real_, mt, D)
  for (l in seq_len(D)) {
    u <- su$u[su$u$look <= l, ]
    n <- if (identical(p[["input_shape"]], "aggregated")) vapply(seq_len(k), function(j) sum(u$n[u$g == j]), 0) else tabulate(u$g, k)
    if (any(n < 2) || (!is.null(u$own) && any(tabulate(u$g[u$own], k) < 2))) next
    e <- seq_estimates(u, p, k)
    c0 <- e$m[[ci]]
    if (c0 == 0) next
    for (i in seq_len(mt)) {
      t <- tr[[i]]
      w <- e$v[[t]] / c0^2 + e$m[[t]]^2 * e$v[[ci]] / c0^4
      if (!(w > 0)) next
      lift[i, l] <- e$m[[t]] / c0 - 1
      lv[i, l] <- w
      mcl[i, l] <- c0
    }
  }
  # the mixture: log Λ = ½ log(v/(v + τ²)) + τ² λ² / (2 v (v + τ²)); the sequence's half-width at 1 − a
  logl <- 0.5 * log(lv / (lv + tau^2)) + tau^2 * lift^2 / (2 * lv * (lv + tau^2))
  half <- sqrt(lv * (lv + tau^2) / tau^2 * (2 * log(1 / a_int) + log((lv + tau^2) / lv)))
  p_raw <- low <- high <- matrix(NA_real_, mt, D)
  for (i in seq_len(mt)) {
    run_p <- 1
    lo <- -Inf
    hi <- Inf
    for (l in seq_len(D)) {
      if (!is.na(logl[i, l])) {
        run_p <- min(run_p, 1 / exp(logl[i, l]))
        lo <- max(lo, lift[i, l] - half[i, l])
        hi <- min(hi, lift[i, l] + half[i, l])
      }
      p_raw[i, l] <- run_p
      low[i, l] <- if (is.finite(lo) && lo <= hi) lo else NA_real_
      high[i, l] <- if (is.finite(hi) && lo <= hi) hi else NA_real_
    }
    if (all(is.na(logl[i, ]))) p_raw[i, ] <- NA_real_
  }
  p_adj <- p_raw
  for (l in seq_len(D)) p_adj[, l] <- stats::p.adjust(p_raw[, l], P_ADJUST[[p[["correction"]]]])
  ex <- list(list(key = "model|||sequential_tau", value = tau), list(key = "model|||n_looks", value = D))
  num <- function(key, value) if (!is.na(value)) ex[[length(ex) + 1L]] <<- list(key = key, value = value)
  txt <- function(key, text) ex[[length(ex) + 1L]] <<- list(key = key, text = text)
  reading <- list()
  for (i in seq_len(mt)) {
    lab <- su$labels[[tr[[i]]]]
    key <- function(stat) paste0("sequential|", lab, "||", stat)
    pa <- p_adj[i, D]
    hit <- which(p_adj[i, ] < p[["alpha"]])
    crossing <- if (length(hit) && !is.null(p[["date"]])) su$day_of[[hit[[1L]]]] else NA_real_
    num(key("p_value"), p_raw[i, D])
    num(key("p_adjusted"), pa)
    num(key("log_lambda"), logl[i, D])
    num(key("cs_low"), low[i, D])
    num(key("cs_high"), high[i, D])
    num(key("first_crossing"), crossing)
    if (!is.na(pa)) txt(key("significance"), stars(pa))
    last <- max(c(0L, which(!is.na(lift[i, ]))))
    up <- if (!is.na(low[i, D]) && low[i, D] > 0) TRUE else if (!is.na(high[i, D]) && high[i, D] < 0) FALSE else last > 0 && lift[i, last] > 0
    if (last > 0 && mcl[i, last] < 0) up <- !up
    txt(key("decision"), if (is.na(pa)) "not_computed" else if (pa < p[["alpha"]]) (if (up == isTRUE(p[["higher_is_better"]])) "better" else "worse") else "not_significant")
    at <- which(!is.na(lift[i, ]))
    for (stat in curves) {
      series <- switch(stat, p = p_adj[i, at], y = lift[i, at], low = low[i, at], high = high[i, at])
      chart <- if (stat == "p") "sequential_p" else "sequential_lift"
      for (x in seq_along(at)) num(paste0("curve|", chart, "|", lab, "|", if (stat == "p") "y" else stat, "|", x - 1L), series[[x]])
    }
    if (!is.na(p_raw[i, D])) {
      dated <- !is.null(p[["date"]])
      empty <- is.na(low[i, D]) && is.na(high[i, D]) && any(!is.na(lift[i, ]))
      ids <- c(if (dated) "sequential_read" else "sequential_read_no_day", if (!is.na(crossing)) "sequential_crossed",
               if (empty) "sequential_empty", if (dated) "sequential_snapshot")
      for (j in seq_along(ids)) reading[[length(reading) + 1L]] <- list(paste0("reading|sequential|", lab, "|line_", j), ids[[j]])
    }
  }
  case$expect <- ex
  # the reading: this block before the power's (or the method's), the method's and the formula's lines last
  keys <- vapply(case$reading, `[[`, "", 1L)
  at <- which(startsWith(keys, "reading|power|") | startsWith(keys, "reading|method|"))[[1L]]
  rd <- c(case$reading[seq_len(at - 1L)], reading, case$reading[at:length(case$reading)])
  for (block in c("method", "formulas")) {
    n <- sum(startsWith(vapply(rd, `[[`, "", 1L), paste0("reading|", block, "|")))
    extra <- list(list(paste0("reading|", block, "||line_", n + 1L), if (block == "method") "method_sequential" else "formula_mixture"))
    last <- max(which(startsWith(vapply(rd, `[[`, "", 1L), paste0("reading|", block, "|"))))
    rd <- c(rd[seq_len(last)], extra, rd[-seq_len(last)])
  }
  case$reading <- rd
  case
}

on <- list(bayes = FALSE, sequential = TRUE)
add_case(seq_case("seq_binary", "binary_two.csv", list(variant = "text", converted = "number", day = "date"),
                  c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", date = "day"), on),
                  curves = c("p", "y"), note = "one treatment, unequal arms, 20 looks"))
add_case(seq_case("seq_holm", "sequential.csv", list(variant = "text", day = "date", revenue = "number"),
                  c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", date = "day"), on),
                  curves = "p", note = "balanced arms: Johari et al.'s balanced formula; Holm at every look"))
add_case(seq_case("seq_cuped_bh", "sequential.csv", list(variant = "text", day = "date", revenue = "number", pre = "number"),
                  c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", covariate = "pre", date = "day",
                         correction = "benjamini_hochberg", sequential_tau = 0.2), on),
                  curves = "p", note = "CUPED's θ recomputed at every look, the empty covariates filled with today's means"))
add_case(seq_case("seq_ratio_bonferroni", "sequential.csv", list(variant = "text", day = "date", revenue = "number", views = "number"),
                  c(list(variant = "variant", control = "A", metric_type = "ratio", numerator = "revenue", denominator = "views", date = "day",
                         correction = "bonferroni", higher_is_better = FALSE), on),
                  curves = c("low", "high"), note = "the delta method at every look; the sequence at alpha/m"))
add_case(seq_case("seq_aggregated", "aggregated.csv", list(variant = "text", day = "date", units = "number", revenue_sum = "number",
                                                          revenue_squares = "number"),
                  c(list(variant = "variant", control = "A", metric_type = "continuous", input_shape = "aggregated", units_count = "units",
                         metric_sum = "revenue_sum", metric_sum_squares = "revenue_squares", date = "day", sequential_tau = 0.3), on)))
add_case(seq_case("seq_grouped", "sequential_long.csv", list(variant = "text", day = "date", converted = "number"),
                  c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", date = "day", sequential_tau = 0.5), on),
                  note = "2,100 days: 700 looks of 3 days"))
add_case(seq_case("seq_no_date", "continuous_three.csv", list(variant = "text", revenue = "number"),
                  c(list(variant = "variant", control = "A", metric_type = "continuous", metric = "revenue", sequential_tau = 0.2), on),
                  note = "one look, the snapshot: no curve, no day"))
add_case(seq_case("seq_drift", "sequential_drift.csv", list(variant = "text", day = "date", units = "number", s = "number", q = "number"),
                  c(list(variant = "variant", control = "A", metric_type = "continuous", input_shape = "aggregated", units_count = "units",
                         metric_sum = "s", metric_sum_squares = "q", date = "day"), on),
                  note = "the lift changes sign: the confidence sequence is empty, its sentence instead of its ends"))
add_case(refused("seq_greater", "binary_two.csv", list(variant = "text", converted = "number"),
                 c(list(variant = "variant", control = "A", metric_type = "binary", metric = "converted", alternative = "greater"), on),
                 "sequential_two_sided", "the always-valid p-value of this node is two-sided"))
