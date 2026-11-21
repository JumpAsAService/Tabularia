# The planning node's reference cases (lot E5), sourced by make_reference.R after the A/B test's: `add_case` adds a case,
# its values computed here BY HAND from the raw rows of data/history.csv with base R (mean, var, cov, lm, qnorm, uniroot),
# never with the runner's code. `--data` writes history.csv again (600 rows: the self-test's ConfigMap has room for little).

make_data_plan <- function() {
  set.seed(20261201)
  n <- 600
  pre <- round(stats::rgamma(n, 4, 0.2), 1)
  impressions <- 1 + stats::rpois(n, 12)
  df <- data.frame(day = as.Date("2026-03-02") + sort(sample(0:13, n, replace = TRUE)),
                   converted = stats::rbinom(n, 1, stats::plogis(-2.4 + 0.03 * (pre - 20))),
                   revenue = round(pmax(0, 20 + 0.6 * (pre - 20) + stats::rnorm(n, 0, 9)), 1), revenue_pre = pre,
                   clicks = stats::rbinom(n, impressions, 0.04 * stats::rgamma(n, 4, 4)), impressions = impressions)
  df$revenue_pre[stats::runif(n) < 0.12] <- NA
  write_table(df, "history.csv")
}
if ("--data" %in% args || !file.exists(file.path(data_dir, "history.csv"))) make_data_plan()

PLAN_TYPES <- list(day = "date", converted = "number", revenue = "number", revenue_pre = "number", clicks = "number", impressions = "number")
PLAN_DEFAULTS <- list(solve_for = "sample_size", min_lift = 0.02, arms = 2, alpha = 0.05, power = 0.8, alternative = "two_sided", correction = "holm",
                      traffic_share = 1, covariate_missing = "fill")
plan_label <- function(x) sprintf("%.15g", signif(x, 4))

# One case of ab_plan by hand: the expected keys and the ids of the reading.
plan_case <- function(id, params, note = NULL, grid = c("asked", "first")) {
  p <- utils::modifyList(PLAN_DEFAULTS, params)
  df <- read_table("history.csv", PLAN_TYPES)
  type <- p[["metric_type"]]
  dated <- !is.null(p[["date"]])
  n <- nrow(df)
  if (type == "binary") { y <- df[[p[["metric"]]]]; m0 <- mean(y); s0 <- sqrt(m0 * (1 - m0)) }
  if (type == "continuous") { y <- df[[p[["metric"]]]]; m0 <- mean(y); s0 <- stats::sd(y) }
  if (type == "ratio") {
    y <- df[[p[["numerator"]]]]; x <- df[[p[["denominator"]]]]
    m0 <- sum(y) / sum(x)
    s0 <- sqrt((stats::var(y) - 2 * m0 * stats::cov(y, x) + m0^2 * stats::var(x)) / mean(x)^2)
  }
  r <- NULL
  if (!is.null(p[["covariate"]])) {  # the units without the covariate get its mean; theta from the units that have it
    cv <- df[[p[["covariate"]]]]
    own <- !is.na(cv)
    theta <- stats::cov(y[own], cv[own]) / stats::var(cv[own])
    filled <- ifelse(own, cv, mean(cv[own]))
    r <- 1 - stats::var(y - theta * filled) / stats::var(y)
    rho <- stats::cor(y[own], cv[own])
  }
  k <- p[["arms"]]
  wc <- if (is.null(p[["control_share"]])) 1 / k else p[["control_share"]]
  wt <- (1 - wc) / (k - 1)
  a1 <- if (p[["correction"]] == "none") p[["alpha"]] else p[["alpha"]] / (k - 1)
  za <- if (p[["alternative"]] == "two_sided") stats::qnorm(1 - a1 / 2) else stats::qnorm(1 - a1)
  zb <- stats::qnorm(p[["power"]])
  span <- if (dated) as.numeric(max(df[[p[["date"]]]]) - min(df[[p[["date"]]]])) + 1 else NA_real_
  daily <- if (dated) n / span * p[["traffic_share"]] else NA_real_
  vt <- function(L, sc) if (type == "binary") m0 * (1 + L) * (1 - m0 * (1 + L)) * sc else s0^2 * sc
  total_n <- function(L, sc = 1) (za + zb)^2 * (s0^2 * sc / wc + vt(L, sc) / wt) / (m0 * L)^2
  mde_at <- function(nc, nt, sc = 1) {
    f <- function(L) m0 * L - (za + zb) * sqrt(s0^2 * sc / nc + vt(L, sc) / nt)
    stats::uniroot(f, c(0, if (type == "binary") (1 - m0) / m0 else 1e6), tol = 1e-12)$root
  }
  up <- function(v) ceiling(v - 1e-9)
  sizes <- function(L, sc = 1) { N <- total_n(L, sc); nt <- up(N * wt); nc <- up(N * wc); c(nt = nt, nc = nc, total = nc + (k - 1) * nt) }
  if (p[["solve_for"]] == "sample_size") {
    L <- p[["min_lift"]]
    s <- sizes(L)
    nt <- s[["nt"]]; nc <- s[["nc"]]
  } else {
    nt <- if (p[["solve_for"]] == "mde_for_units") p[["units_per_variant"]] else p[["days"]] * daily * wt
    nc <- nt * wc / wt
    L <- mde_at(nc, nt)
  }
  nt0 <- nt; nc0 <- nc  # the units as the lift was solved from them
  if (p[["solve_for"]] == "mde_for_days") { nt <- floor(nt); nc <- floor(nc) } else if (p[["solve_for"]] == "mde_for_units") nc <- up(nc)
  total <- nc + (k - 1) * nt
  days <- if (dated) (if (p[["solve_for"]] == "mde_for_days") p[["days"]] else up(total / daily)) else NA_real_
  ex <- list()
  num <- function(key, value, rel = NULL) { e <- list(key = key, value = if (is.na(value)) NULL else value); if (!is.null(rel)) e$rel <- rel; ex[[length(ex) + 1L]] <<- e }
  num("model|||baseline_mean", m0); num("model|||baseline_sd", s0); num("model|||n_history", n)
  num("model|||alpha_per_comparison", a1); num("model|||n_per_variant", nt); num("model|||n_control", nc); num("model|||n_total", total)
  num("model|||mde", L); num("model|||mde_absolute", abs(m0) * L)
  if (dated) { num("model|||history_days", span); num("model|||daily_units", daily); num("model|||days", days); num("model|||weeks", ceiling(days / 7)) }
  cu <- NULL
  if (!is.null(r)) {
    cu <- sizes(L, 1 - r)
    num("model|||covariate_correlation", rho); num("model|||variance_reduction", r); num("model|||n_per_variant_cuped", cu[["nt"]])
    num("model|||n_total_cuped", cu[["total"]]); num("model|||mde_cuped", mde_at(nc0, nt0, 1 - r))
    if (dated) num("model|||days_cuped", up(cu[["total"]] / daily))
  }
  # three rows of the grid: the lift of the answer, the smallest and the largest
  lifts <- exp(seq(log(L / 4), log(4 * L), length.out = 30))
  if (type == "binary") lifts <- lifts[m0 * (1 + lifts) <= 1]
  for (g in grid) {
    l <- switch(g, asked = L, first = lifts[[1L]], last = lifts[[length(lifts)]])
    s <- sizes(l)
    lab <- plan_label(l)
    num(paste0("plan|", lab, "||lift"), l); num(paste0("plan|", lab, "||n_per_variant"), s[["nt"]]); num(paste0("plan|", lab, "||n_total"), s[["total"]])
    if (dated) num(paste0("plan|", lab, "||days"), up(s[["total"]] / daily))
    if (!is.null(r)) num(paste0("plan|", lab, "||n_per_variant_cuped"), sizes(l, 1 - r)[["nt"]])
    num(paste0("plan|", lab, "||asked"), as.numeric(g == "asked"))
  }
  # the explanation: the template id of each line, in the order written
  reading <- list()
  say <- function(block, ids) for (i in seq_along(ids)) reading[[length(reading) + 1L]] <<- list(paste0("reading|", block, "||line_", i), ids[[i]])
  say("plan", switch(p[["solve_for"]], sample_size = if (dated) "plan_size" else "plan_size_no_days", mde_for_units = "plan_mde_units", mde_for_days = "plan_mde_days"))
  if (dated) say("baseline", "baseline_read")
  if (!is.null(r) && dated) say("cuped", "cuped_plan")
  say("correction", if (p[["correction"]] == "none") "correction_none" else paste0("correction_", p[["correction"]]))
  say("method", c("method_plan", "method_power", if (!is.null(r)) "method_cuped", if (dated) "method_weeks"))
  say("formulas", c("formula_sample_size", if (!is.null(r)) "formula_cuped"))
  case <- list(id = id, model = "ab_plan", data = "data/history.csv", types = PLAN_TYPES[names(PLAN_TYPES) %in% unlist(params[c("metric", "numerator", "denominator", "date", "covariate")])],
               params = params, expect = ex, reading = reading)
  if (!is.null(note)) case$note <- note
  case
}

add_case(plan_case("plan_binary_size", list(metric_type = "binary", metric = "converted", date = "day", min_lift = 0.15),
                   "a binary metric: the treatment's variance p1 (1 - p1), whole days of the history's traffic"))
add_case(plan_case("plan_continuous_units", list(metric_type = "continuous", metric = "revenue", date = "day", solve_for = "mde_for_units",
                                                 units_per_variant = 3000, arms = 3), "the lift detectable with the units of a treatment, three variants and Holm"))
add_case(plan_case("plan_ratio_arms4", list(metric_type = "ratio", numerator = "clicks", denominator = "impressions", date = "day", min_lift = 0.1,
                                            arms = 4, control_share = 0.4, correction = "bonferroni"),
                   "a ratio metric (the delta method's variance), the control's share 0.4 of four variants"))
add_case(plan_case("plan_cuped_days", list(metric_type = "continuous", metric = "revenue", covariate = "revenue_pre", date = "day", solve_for = "mde_for_days",
                                           days = 60, traffic_share = 0.5, correction = "none", alternative = "greater"),
                   "CUPED with the empty covariates filled, the lift detectable in 60 days of half the traffic, one-sided, no correction"))
add_case(plan_case("plan_binary_days", list(metric_type = "binary", metric = "converted", date = "day", solve_for = "mde_for_days", days = 30), grid = c("asked", "last"),
                   "the binary equation solved for the lift (uniroot), in days"))
add_case(plan_case("plan_continuous_no_date", list(metric_type = "continuous", metric = "revenue", min_lift = 0.05, power = 0.9, alpha = 0.01),
                   "without a date: the units only, no days, weeks or traffic"))
add_case(list(id = "plan_binary_lift_too_large", model = "ab_plan", data = "data/history.csv", types = PLAN_TYPES["converted"],
              params = list(metric_type = "binary", metric = "converted", min_lift = 20), expect = list(),
              refuses = list(code = "lift_too_large", message = paste0("a rate of ", format(signif(mean(read_table("history.csv", PLAN_TYPES)$converted), 6), scientific = FALSE),
                                                                        " cannot rise by 20: it would pass 100 %"))))
