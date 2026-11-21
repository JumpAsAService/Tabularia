# The self-test of the sequential test (lot E4), in TAB_COMMON_TESTS; the reference cases seq_* of expected.json are
# checked value by value by the common self-test, and here for what it does not read.

TAB_COMMON_TESTS[["experiments: the mixture's log Λ is the integral over N(0, τ²); the sequence's ends are where Λ(θ) = 1/alpha"]] <- function(code) {
  for (x in list(c(0.04, 0.0009, 0.05), c(-0.3, 0.02, 0.2), c(0.01, 1e-5, 0.05), c(0, 0.5, 1))) {
    lift <- x[[1L]]; v <- x[[2L]]; tau <- x[[3L]]
    mix <- ab_mixture(lift, v, tau, 0.05)
    centre <- lift * tau^2 / (v + tau^2); spread <- sqrt(v * tau^2 / (v + tau^2))
    f <- function(th) exp(th * lift / v - th^2 / (2 * v)) * stats::dnorm(th, 0, tau)
    lr <- stats::integrate(f, centre - 40 * spread, centre + 40 * spread, rel.tol = 1e-12)$value
    .tab_expect(abs(log(lr) - mix$log_lambda) < 1e-9, "log Λ ", mix$log_lambda, " against the integral ", log(lr))
    # Λ of the mixture centred on an end of the sequence is 1/alpha
    for (end in lift + c(-1, 1) * mix$half) {
      .tab_expect(abs(ab_mixture(lift - end, v, tau, 0.05)$log_lambda - log(20)) < 1e-9, "the end ", end)
    }
  }
}

TAB_COMMON_TESTS[["experiments: the sequential test of every reference case: p never increases, the first crossing, the sequence ⇔ p, the last look = the comparison"]] <- function(code) {
  seen <- 0L
  for (case in .abc_cases(code)) {
    if (!isTRUE(case$params$sequential)) next
    df <- .abc_run_case(case, code)
    alpha <- as.numeric(case$params$alpha %||% 0.05)
    tau <- as.numeric(case$params$sequential_tau %||% 0.05)
    m <- sum(df$section == "comparison" & df$stat == "lift")
    a_int <- if (identical(case$params$correction, "bonferroni")) alpha / m else alpha
    for (t in unique(df$level[df$section == "sequential"])) {
      at <- function(stat) df$value[df$section == "sequential" & df$level == t & df$stat == stat]
      cmp <- function(stat) df$value[df$section == "comparison" & df$level == t & df$stat == stat]
      curve <- df[df$section == "curve" & df$level == "sequential_p" & df$level_2 == t, ]
      if (nrow(curve)) {
        .tab_expect(!is.unsorted(rev(curve$value)), case$id, " ", t, ": the corrected p increases")
        hit <- which(curve$value < alpha)
        want <- if (length(hit)) curve$x0[[hit[[1L]]]] else numeric()
        .tab_expect(identical(at("first_crossing"), want), case$id, " ", t, ": first_crossing ", at("first_crossing"))
        lift <- df[df$section == "curve" & df$level == "sequential_lift" & df$level_2 == t & df$stat == "y", "value"]
        .tab_expect(abs(lift[[length(lift)]] / cmp("lift") - 1) < 1e-12, case$id, " ", t, ": the last look's lift")
      }
      # the last look holds every unit: its log Λ from the comparison's lift and the lift's standard error
      .tab_expect(abs(ab_mixture(cmp("lift"), cmp("lift_std_error")^2, tau, a_int)$log_lambda - at("log_lambda")) < 1e-9 * max(1, abs(at("log_lambda"))),
                  case$id, " ", t, ": log Λ")
      out <- (length(at("cs_low")) && at("cs_low") > 0) || (length(at("cs_high")) && at("cs_high") < 0)
      .tab_expect(identical(out, at("p_value") <= a_int), case$id, " ", t, ": the sequence and p")
      seen <- seen + 1L
    }
  }
  .tab_expect(seen >= 10L, "only ", seen, " treatments")
}

TAB_COMMON_TESTS[["experiments: looks grouped beyond max_looks equal the days put together; one-sided refused; off, nothing written"]] <- function(code) {
  set.seed(11)
  n <- 900
  df <- data.frame(v = sample(c("A", "B", "C"), n, TRUE), day = as.Date("2026-01-05") + sample(0:39, n, TRUE), stringsAsFactors = FALSE)
  df$day[1:2] <- as.Date("2026-01-05") + c(0, 39)
  df$y <- round(stats::rexp(n, 0.1) * ifelse(df$v == "B", 1.6, 1), 2)
  p <- list(metric_type = "continuous", metric = "y", date = "day", sequential = TRUE, sequential_tau = 0.2)
  run <- function(frame, most) {
    task <- .abc_task(p, frame)
    task$limits$max_looks <- most
    tab_results_frame(fit_ab_test(task)$rows)
  }
  grouped <- run(df, 9)  # 40 days in periods of 5: 8 looks
  starts <- df
  starts$day <- as.Date("2026-01-05") + 5 * ((as.numeric(df$day - as.Date("2026-01-05"))) %/% 5)
  daily <- run(starts, 1000)
  value <- function(f, key) f$value[match(key, f$key)]
  .tab_expect(value(grouped, "model|||n_looks") == 8 && any(grepl("^@looks_grouped days=40 period=5$", grouped$text)), "8 looks of 5 days")
  for (t in c("B", "C")) {
    for (stat in c("p_value", "p_adjusted", "log_lambda", "cs_low", "cs_high")) {
      key <- paste0("sequential|", t, "||", stat)
      .tab_expect(identical(is.na(value(grouped, key)), is.na(value(daily, key))) &&
                    (is.na(value(daily, key)) || abs(value(grouped, key) - value(daily, key)) <= 1e-12 * abs(value(daily, key))), key)
    }
    # a crossing is said on its period's last day
    key <- paste0("sequential|", t, "||first_crossing")
    .tab_expect(identical(value(grouped, key), if (is.na(value(daily, key))) NA_real_ else value(daily, key) + 4), key, " ", value(grouped, key))
  }
  .tab_expect(!is.na(value(grouped, "sequential|B||first_crossing")), "B crosses")
  refused <- tryCatch(fit_ab_test(.abc_task(c(p, list(alternative = "greater")), df)), tabularia_refusal = function(e) e)
  .tab_expect(inherits(refused, "tabularia_refusal") && identical(refused$code, "sequential_two_sided"), "greater with sequential")
  off <- tab_results_frame(fit_ab_test(.abc_task(utils::modifyList(p, list(sequential = FALSE)), df))$rows)
  .tab_expect(!any(off$section == "sequential" | off$level %in% c("sequential_p", "sequential_lift", "sequential") |
                     grepl("sequential|mixture", off$text)), "sequential off writes rows")
  # the `variants` output carries the treatments' always-valid p and sequence, NULL for the control
  task <- .abc_task(utils::modifyList(p, list(output = "variants")), df)
  out <- as.data.frame(fit_ab_test(task)$output)
  full <- tab_results_frame(fit_ab_test(.abc_task(p, df))$rows)
  .tab_expect(isTRUE(all.equal(out$sequential_p_adjusted, c(NA, value(full, "sequential|B||p_adjusted"), value(full, "sequential|C||p_adjusted")))) &&
                isTRUE(all.equal(out$sequential_lift_low, c(NA, value(full, "sequential|B||cs_low"), value(full, "sequential|C||cs_low")))),
              "the variants output")
}
